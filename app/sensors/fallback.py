"""psutil + NVML backend: no admin required, no install beyond pip deps.

Every metric group answers on its own. psutil's counters are independent
calls - one dead Performance Counter must not take the rest of the sample
down with it - and a group that raised is named in `snap.failed`, so the hub
can count the failure and history can draw a gap. NVML is opened lazily and
re-opened after errors: the old module-import-time init meant a driver that
was late at boot, or reset later, left this backend without GPU handles for
the rest of the app's life.
"""
from __future__ import annotations

import time

import psutil

from app.snapshot import Snapshot


class FallbackBackend:
    def __init__(self, cfg: dict):
        psutil.cpu_percent(interval=None)  # prime
        psutil.cpu_times_percent(interval=None)
        self._last_net = psutil.net_io_counters()
        self._last_net_ts = time.monotonic()
        try:
            self._last_disk = psutil.disk_io_counters()
        except Exception:  # noqa: BLE001 - no counters is a gap, not a dead backend
            self._last_disk = None     # sample() retries and reports
        self._last_disk_ts = time.monotonic()
        s = cfg.get("sensors", {})
        # How soon to try NVML again after a failed open (driver still
        # loading, device mid-reset). The first attempt happens on the first
        # sample, so a driver that is late at boot costs GPU for a tick, not
        # for the life of the process.
        self._nvml_retry_s = float(s.get("nvml_retry_s", 60.0))
        self._pynvml = None
        self._gpu = None
        self._nvml_next = 0.0
        self.last_error = ""

    # ------------------------------------------------------------------ sample
    def sample(self, snap: Snapshot) -> None:
        self._group(snap, "cpu", self._sample_cpu)
        self._group(snap, "ram", self._sample_ram)
        self._group(snap, "net", self._sample_net)
        self._group(snap, "disk", self._sample_disk)
        self._group(snap, "gpu", self._sample_gpu)

    def _group(self, snap: Snapshot, name: str, fn) -> None:
        """One metric group: a raise here costs that group, not the sample."""
        try:
            fn(snap)
        except Exception as e:  # noqa: BLE001 - one dead counter is not a dead sample
            snap.failed = tuple(sorted({*snap.failed, name}))
            self.last_error = f"{name}: {type(e).__name__}: {e}"

    def _sample_cpu(self, snap: Snapshot) -> None:
        c = snap.cpu
        c.load_pct = psutil.cpu_percent(interval=None)
        freq = psutil.cpu_freq()
        if freq and freq.current:
            c.clock_avg_mhz = freq.current  # no per-core detail without LHM
        # psutil has no CPU temp on Windows; leave None → "--"

    def _sample_ram(self, snap: Snapshot) -> None:
        vm = psutil.virtual_memory()
        snap.ram_used_mb = (vm.total - vm.available) / 1e6
        snap.ram_total_mb = vm.total / 1e6

    def _sample_net(self, snap: Snapshot) -> None:
        now = time.monotonic()
        net = psutil.net_io_counters()
        dtn = max(now - self._last_net_ts, 1e-6)
        snap.net_down_bps = (net.bytes_recv - self._last_net.bytes_recv) * 8 / dtn
        snap.net_up_bps = (net.bytes_sent - self._last_net.bytes_sent) * 8 / dtn
        self._last_net, self._last_net_ts = net, now

    def _sample_disk(self, snap: Snapshot) -> None:
        now = time.monotonic()
        disk = psutil.disk_io_counters()
        if disk:
            if self._last_disk is not None:
                dtd = max(now - self._last_disk_ts, 1e-6)
                snap.disk_read_bps = (disk.read_bytes - self._last_disk.read_bytes) / dtd
                snap.disk_write_bps = (disk.write_bytes - self._last_disk.write_bytes) / dtd
            self._last_disk, self._last_disk_ts = disk, now

    # --------------------------------------------------------------------- gpu
    def _nvml_open(self, now: float) -> None:
        """Open NVML if it is not open and the retry window has passed.

        Failure here is deliberately quiet: a machine without NVIDIA (or with
        a driver still loading) is unavailable, not failing - the group
        answers None, and the retry window asks again later.
        """
        if self._gpu is not None or now < self._nvml_next:
            return
        self._nvml_next = now + self._nvml_retry_s
        try:
            import pynvml
        except ImportError:
            return                      # not an NVIDIA box: nothing to re-acquire
        try:
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() < 1:
                return                  # driver up, no device yet: ask again later
            self._pynvml = pynvml
            self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml_next = 0.0
        except Exception:  # noqa: BLE001 - driver not ready: retry on the window
            self._gpu = None

    def _sample_gpu(self, snap: Snapshot) -> None:
        self._nvml_open(time.monotonic())
        if self._gpu is None:
            return          # unavailable (no NVIDIA device, or the driver is not
                            # ready): None values, and not a failure
        p, g = self._pynvml, snap.gpu
        try:
            util = p.nvmlDeviceGetUtilizationRates(self._gpu)
            g.load_pct = float(util.gpu)
            g.temp_c = float(p.nvmlDeviceGetTemperature(
                self._gpu, p.NVML_TEMPERATURE_GPU))
            g.core_mhz = float(p.nvmlDeviceGetClockInfo(
                self._gpu, p.NVML_CLOCK_GRAPHICS))
            try:
                g.power_w = p.nvmlDeviceGetPowerUsage(self._gpu) / 1000.0
            except p.NVMLError:
                pass        # unsupported on this board: an honest None
            mem = p.nvmlDeviceGetMemoryInfo(self._gpu)
            g.vram_used_mb = mem.used / 1e6
            g.vram_total_mb = mem.total / 1e6
        except p.NVMLError:
            # A driver reset shows up as errors on every call: drop the handle
            # so the next tick re-acquires it (the reset itself already failed
            # this group, so no retry window - the failure answer *is* the
            # retry), and let the group fail so the hub counts it.
            self._gpu = None
            self._nvml_next = 0.0
            raise

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        p, self._pynvml, self._gpu = self._pynvml, None, None
        if p is not None:
            try:
                p.nvmlShutdown()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass