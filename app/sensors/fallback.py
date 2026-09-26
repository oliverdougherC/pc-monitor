"""psutil + NVML backend: no admin required, no install beyond pip deps."""
from __future__ import annotations

import time

import psutil

from app.snapshot import Snapshot

try:
    import pynvml
    pynvml.nvmlInit()
    _NVML_HANDLES = [pynvml.nvmlDeviceGetHandleByIndex(i)
                     for i in range(pynvml.nvmlDeviceGetCount())]
except Exception:
    _NVML_HANDLES = []


class FallbackBackend:
    def __init__(self, cfg: dict):
        psutil.cpu_percent(interval=None)  # prime
        psutil.cpu_times_percent(interval=None)
        self._last_net = psutil.net_io_counters()
        self._last_net_ts = time.monotonic()
        self._last_disk = psutil.disk_io_counters()
        self._last_disk_ts = time.monotonic()
        self._gpu = _NVML_HANDLES[0] if _NVML_HANDLES else None

    def sample(self, snap: Snapshot) -> None:
        c = snap.cpu
        c.load_pct = psutil.cpu_percent(interval=None)
        freq = psutil.cpu_freq()
        if freq and freq.current:
            c.clock_avg_mhz = freq.current  # no per-core detail without LHM
        # psutil has no CPU temp on Windows; leave None → "--"

        vm = psutil.virtual_memory()
        snap.ram_used_mb = (vm.total - vm.available) / 1e6
        snap.ram_total_mb = vm.total / 1e6

        now = time.monotonic()
        net = psutil.net_io_counters()
        dtn = max(now - self._last_net_ts, 1e-6)
        # psutil counts bytes; the snapshot carries network in bits/s (see app/snapshot.py),
        # and the panel labels it accordingly — Mbps, not MB/s.
        snap.net_down_bps = (net.bytes_recv - self._last_net.bytes_recv) * 8 / dtn
        snap.net_up_bps = (net.bytes_sent - self._last_net.bytes_sent) * 8 / dtn
        self._last_net, self._last_net_ts = net, now

        disk = psutil.disk_io_counters()
        if disk:
            dtd = max(now - self._last_disk_ts, 1e-6)
            snap.disk_read_bps = (disk.read_bytes - self._last_disk.read_bytes) / dtd
            snap.disk_write_bps = (disk.write_bytes - self._last_disk.write_bytes) / dtd
            self._last_disk, self._last_disk_ts = disk, now

        g = snap.gpu
        if self._gpu is not None:
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu)
                g.load_pct = float(util.gpu)
                g.temp_c = float(pynvml.nvmlDeviceGetTemperature(
                    self._gpu, pynvml.NVML_TEMPERATURE_GPU))
                g.core_mhz = float(pynvml.nvmlDeviceGetClockInfo(
                    self._gpu, pynvml.NVML_CLOCK_GRAPHICS))
                try:
                    g.power_w = pynvml.nvmlDeviceGetPowerUsage(self._gpu) / 1000.0
                except pynvml.NVMLError:
                    pass
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._gpu)
                g.vram_used_mb = mem.used / 1e6
                g.vram_total_mb = mem.total / 1e6
            except pynvml.NVMLError:
                pass
