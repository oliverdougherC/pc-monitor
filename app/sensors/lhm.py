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
        self._dll = os.path.join(cfg["_vendor"], "external", "LibreHardwareMonitor",
                                 "LibreHardwareMonitorLib.dll")
        self._reopen_after = max(1, int(cfg.get("sensors", {}).get("reopen_after", 3)))
        self._lhm_fails = 0
        # Hardware groups that refused to update on the last walk (#17), how many were
        # walked, and which ones refused (#5). Reset by every `_rescan`, read by
        # `_lhm_sample`, which fails the group only when *every* group refused or when
        # nothing readable came back - so a driver which has stopped answering still
        # reaches the rebuild instead of looking like a healthy scan that happens to
        # report nothing, while one dead controller cannot blank the CPU beside it.
        self._update_fails = 0
        self._update_total = 0
        self._update_fail_nodes: list[str] = []
        self._open()

    def _open(self) -> None:
        """Open the LHM Computer (the ring0-backed object) and walk it once.

        This is the resource the hub's rebuild and the resume hook re-acquire:
        a Computer() opened against a wedged driver keeps wedging, and only a
        fresh Open() sees a device that came back."""
        if not os.path.exists(self._dll):
            raise RuntimeError(f"LHM dll not found: {self._dll}")

        sys.path.insert(0, os.path.dirname(self._dll))
        clr.AddReference(self._dll)  # type: ignore[no-redef]
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
        """Walk the hardware tree, and *say* when the driver would not update.

        The failure that matters here is quiet: `hw.Update()` raising used to be
        swallowed with a `continue`, so a driver that had stopped answering produced a
        perfectly normal return with every sensor simply absent. `_lhm_sample` then
        finished normally, `sample()`'s `else` reset the failure streak, and
        `_reopen_lhm()` — the only repair this backend has — was unreachable for the one
        fault it exists for. So the count of hardware groups that refused to update is
        kept, together with which nodes they were and how many were walked, and
        `_lhm_sample` fails the group when *all* of them refused.

        A node that refuses is still only *one node* (#5). CPU, GPU, motherboard,
        storage and controller monitoring are all enabled, so one failing controller
        used to take every reading with it: the healthy 65 °C CPU sensor was collected
        into `_sensors` and then never copied, because `_lhm_sample` rejected the whole
        walk. The failing nodes are recorded and skipped, and the healthy ones are
        sampled as usual — the group only fails when there is nothing left to read.

        A *sensor* that raises while being listed is still skipped: that is one missing
        metric, not a dead driver, and losing the whole group for it would be the
        opposite mistake.
        """
        self._sensors.clear()
        self._objs.clear()
        self._update_fails = 0
        self._update_total = 0
        self._update_fail_nodes = []
        for hw in self._c.Hardware:
            self._update_total += 1
            try:
                hw.Update()
            except Exception:  # noqa: BLE001 - counted, not swallowed
                self._update_fails += 1
                try:
                    self._update_fail_nodes.append(hw.HardwareType.ToString())
                except Exception:  # noqa: BLE001 - a name is diagnostics, not the walk
                    self._update_fail_nodes.append("?")
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
            if v is None or v != v:
                continue
            v = float(v)
            # Without admin there is no ring0 driver, and LHM happily creates its
            # CPU temp/power/clock sensors reporting 0.0. A bright "0°" is a lie —
            # non-positive readings for these types are "missing", so the panel
            # draws its honest dim "--" instead. (Load legitimately reads 0.)
            if sensor_type in ("Temperature", "Power", "Clock") and v <= 0:
                continue
            out.append(v)
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
        # The host (psutil/NVML) answers first and stands on its own: if the
        # LHM override dies mid-pass - a sensor throwing while the driver is
        # resetting - the healthy host values survive and only "lhm" is
        # reported failed. Persistent LHM failure re-opens the Computer: a
        # wedged one never starts answering again on its own.
        self._host.sample(snap)
        try:
            self._lhm_sample(snap)
        except Exception:  # noqa: BLE001 - LHM dying must not cost the host data
            snap.failed = tuple(sorted({*snap.failed, "lhm"}))
            self._lhm_fails += 1
            if self._lhm_fails >= self._reopen_after:
                self._reopen_lhm()
        else:
            self._lhm_fails = 0

    def _reopen_lhm(self) -> None:
        try:
            self._c.Close()
        except Exception:  # noqa: BLE001 - a half-dead Computer may not close clean
            pass
        try:
            self._open()
            self._lhm_fails = 0
        except Exception:  # noqa: BLE001 - host values keep standing; try again next failure
            pass

    def close(self) -> None:
        try:
            self._c.Close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
        self._host.close()

    def _lhm_sample(self, snap: Snapshot) -> None:
        self._rescan()
        if self._update_fails and self._update_fails >= self._update_total:
            # Every group refused to update: this is the driver, not a metric. Raising
            # here is what lets `sample()` count it, mark the group failed, and reach the
            # rebuild — which is the repair a driver that has stopped answering needs.
            # Returning normally instead produced an empty-but-successful scan and reset
            # the streak that leads there.
            raise RuntimeError(
                f"LibreHardwareMonitor would not update any of its "
                f"{self._update_total} hardware group(s) "
                f"({self._node_names()}); the driver is not answering")
        c, g = snap.cpu, snap.gpu

        # How many LHM readings actually landed on the snapshot. This, and not the
        # update-failure count, is what decides whether the walk was usable (#5): the
        # healthy nodes below are still sampled when a sibling node refused.
        read = 0
        v = self._vals(("Cpu",), "Load", ("cpu total",))
        if v:
            c.load_pct = v[0]
            read += 1
        v = self._vals(("Cpu",), "Temperature",
                       ("core (tctl/tdie)", "core (tctl)", "cpu package"))
        if v:
            c.temp_c = max(v)
            read += 1
        clocks = self._vals(("Cpu",), "Clock", ("core #",), exclude=("effective",))
        if clocks:
            c.clock_max_mhz = max(clocks)
            c.clock_avg_mhz = sum(clocks) / len(clocks)
            read += 1
        pw = self._first(("Cpu",), "Power",
                         ("total power", "cpu package", "processor", "package"))
        if pw is not None:
            c.power_w = pw
            read += 1

        gpu_types = next((t for t in _GPU_PRIORITY
                          if any(hwt == t for hwt, _, _, _ in self._sensors)), ())
        if gpu_types:
            gt = ("GpuNvidia",) if gpu_types == "GpuNvidia" else (gpu_types,)
            v = self._vals(gt, "Temperature", ("gpu core",))
            if v:
                g.temp_c = v[0]
                read += 1
            v = self._vals(gt, "Load", ("gpu core",))
            if v:
                g.load_pct = v[0]
                read += 1
            v = self._vals(gt, "Clock", ("gpu core",), exclude=("effective",))
            if v:
                g.core_mhz = v[0]
                read += 1
            pw = self._first(gt, "Power", ("gpu package", "gpu board power", "gpu power"))
            if pw is not None:
                g.power_w = pw
                read += 1
            v = self._vals(gt, "SmallData", ("gpu memory used",))
            if v:
                g.vram_used_mb = v[0]            # already MB
                read += 1
            v = self._vals(gt, "SmallData", ("gpu memory total",))
            if v:
                g.vram_total_mb = v[0]
                read += 1

        if read == 0:
            # A walk that produced nothing readable is not a healthy sample, and it is
            # the shape the inverse bug hid behind: an empty scan used to reset the
            # streak that leads to `_reopen_lhm()`. Whether the nodes updated and had
            # no metric this backend uses, or updated and lost their sensors, the group
            # is named failed so the streak climbs — the host readings `sample()` took
            # before this call still stand, because only "lhm" is reported failed.
            raise RuntimeError(
                f"LibreHardwareMonitor answered nothing readable: "
                f"{self._update_fails} of {self._update_total} hardware group(s) "
                f"would not update ({self._node_names()}); the driver is not answering")

    def _node_names(self) -> str:
        """The nodes that refused to update, for the log line — never a raise."""
        try:
            names = ", ".join(self._update_fail_nodes)
        except Exception:  # noqa: BLE001 - diagnostics must not cost the sample
            return "unnamed"
        return names or "unnamed"
