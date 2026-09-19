"""LibreHardwareMonitorLib backend via pythonnet (best fidelity on Windows).

Gives: CPU Tctl temp, total socket power (Core+SOC+Misc), per-core clocks
(→ single-thread peak + average), GPU detail. Requires Administrator; without
it, register-backed sensors are absent and the host (psutil/NVML) values stand.

Verified against: AMD Ryzen 9 7950X + NVIDIA RTX 5090, LHM lib 0.9.6.
Notes from lhm_dump.txt:
  - pythonnet: SensorType stringifies as "SensorType.Power" → use ToString()
  - hardware "name" is e.g. "AMD Ryzen 9 7950X"; match on HardwareType
    (Cpu / GpuNvidia / GpuAmd / ...) instead
  - per-core clocks must exclude "(Effective)" variants
  - GPU SmallData memory values are already in MB
  - AMD CPU exposes "Total Power" (core+soc+misc) — better than "Package"
"""
from __future__ import annotations

import os
import sys

from app.snapshot import Snapshot

CLR_OK = True
try:
    import clr  # noqa: F401  (from pythonnet)
except Exception:
    CLR_OK = False

_GPU_PRIORITY = ("GpuNvidia", "GpuAmd", "GpuIntel")


class LhmBackend:
    def __init__(self, cfg: dict):
        if os.name != "nt":
            raise RuntimeError("LHM backend is Windows-only")
        if not CLR_OK:
            raise RuntimeError("pythonnet not installed")
        # host backend underneath: disk/net deltas always from psutil, NVML for
        # GPU unless LHM overrides; LHM only fills/overrides what it reports
        from app.sensors.fallback import FallbackBackend
        self._host = FallbackBackend(cfg)

        dll = os.path.join(cfg["_vendor"], "external", "LibreHardwareMonitor",
                           "LibreHardwareMonitorLib.dll")
        if not os.path.exists(dll):
            raise RuntimeError(f"LHM dll not found: {dll}")

        sys.path.insert(0, os.path.dirname(dll))
        clr.AddReference(dll)  # type: ignore[no-redef]
        from LibreHardwareMonitor.Hardware import Computer

        c = Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsStorageEnabled = True
        c.IsControllerEnabled = True
        c.Open()
        self._c = c
        self._sensors: list[tuple[str, str, str, str]] = []  # (hwt, srt, name, sensor-idx)
        self._objs: list = []
        self._rescan()

    # ------------------------------------------------------------------ walk
    def _rescan(self) -> None:
        self._sensors.clear()
        self._objs.clear()
        for hw in self._c.Hardware:
            try:
                hw.Update()
            except Exception:
                continue
            nodes = [hw] + list(hw.SubHardware)
            for node in nodes:
                hwt = node.HardwareType.ToString()
                for s in node.Sensors:
                    try:
                        self._objs.append(s)
                        self._sensors.append(
                            (hwt, s.SensorType.ToString(), str(s.Name), len(self._objs) - 1))
                    except Exception:
                        pass

    def _vals(self, hw_types: tuple[str, ...], sensor_type: str, names: tuple[str, ...],
              exclude: tuple[str, ...] = (), exact: bool = False) -> list[float]:
        """Sensor values matching (hardware type, sensor type, name filter)."""
        out = []
        for hwt, srt, name, idx in self._sensors:
            if hwt not in hw_types or srt != sensor_type:
                continue
            ln = name.lower()
            if any(x in ln for x in exclude):
                continue
            ok = (ln in names if exact else any(n in ln for n in names))
            if not ok:
                continue
            v = self._objs[idx].Value
            if v is not None and v == v:
                out.append(float(v))
        return out

    def _first(self, hw_types, sensor_type, names: tuple[str, ...], exclude=()):
        """First name in preference order that yields a value."""
        for n in names:
            v = self._vals(hw_types, sensor_type, (n,), exclude=exclude, exact=True)
            if not v:
                v = self._vals(hw_types, sensor_type, (n,), exclude=exclude)
            if v:
                return v[0]
        return None

    # ---------------------------------------------------------------- sample
    def sample(self, snap: Snapshot) -> None:
        self._host.sample(snap)
        self._rescan()
        c, g = snap.cpu, snap.gpu

        v = self._vals(("Cpu",), "Load", ("cpu total",))
        if v:
            c.load_pct = v[0]
        v = self._vals(("Cpu",), "Temperature",
                       ("core (tctl/tdie)", "core (tctl)", "cpu package"))
        if v:
            c.temp_c = max(v)
        clocks = self._vals(("Cpu",), "Clock", ("core #",), exclude=("effective",))
        if clocks:
            c.clock_max_mhz = max(clocks)
            c.clock_avg_mhz = sum(clocks) / len(clocks)
        pw = self._first(("Cpu",), "Power",
                         ("total power", "cpu package", "processor", "package"))
        if pw is not None:
            c.power_w = pw

        gpu_types = next((t for t in _GPU_PRIORITY
                          if any(hwt == t for hwt, _, _, _ in self._sensors)), ())
        if gpu_types:
            gt = ("GpuNvidia",) if gpu_types == "GpuNvidia" else (gpu_types,)
            v = self._vals(gt, "Temperature", ("gpu core",))
            if v: g.temp_c = v[0]
            v = self._vals(gt, "Load", ("gpu core",))
            if v: g.load_pct = v[0]
            v = self._vals(gt, "Clock", ("gpu core",), exclude=("effective",))
            if v: g.core_mhz = v[0]
            pw = self._first(gt, "Power", ("gpu package", "gpu board power", "gpu power"))
            if pw is not None: g.power_w = pw
            v = self._vals(gt, "SmallData", ("gpu memory used",))
            if v: g.vram_used_mb = v[0]          # already MB
            v = self._vals(gt, "SmallData", ("gpu memory total",))
            if v: g.vram_total_mb = v[0]
