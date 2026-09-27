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

# How much longer than a tick an interval may be before it stops being one. The
# loop samples at `sensors.interval_s` and this clock is monotonic, so an interval
# of thirty ticks or more is not a slow tick — it is a suspend, a hibernation, or a
# machine that slept while its counters kept counting. Dividing twelve hours of
# bytes by twelve hours is arithmetically correct and says nothing about the desk
# right now, so the sample is dropped and the baseline re-primed instead.
_GAP_FACTOR = 30.0
_MIN_GAP_S = 30.0


def _interval_s(cfg: dict) -> float:
    """The configured tick, defensively: tools build this backend with `{}`."""
    try:
        return max(0.05, float(cfg.get("sensors", {}).get("interval_s", 1.0)))
    except (AttributeError, TypeError, ValueError):     # a cfg that lies
        return 1.0


def _read(fn, **kw):
    """psutil's counters, or None when the counter path itself is gone.

    A dead Performance Counter is a missing number, not a dead sample. Letting it
    raise here would cost the CPU, memory and GPU readings that answered fine,
    because a `sample()` is one unit upstream: `SensorHub.tick()` propagates the
    raise and the loop throws the whole snapshot away, which is how one broken
    disk counter turned into a panel that stopped moving.
    """
    try:
        return fn(**kw)
    except Exception:  # noqa: BLE001 - unavailable is a value, not a fault
        return None


def _totals(reading, fields: tuple):
    """psutil's counters summed over the devices it saw, and which devices those were.

    `pernic`/`perdisk` is asked for in the *same* call that produces the numbers, not
    as a second one, because the aggregate and the device set behind it have to come
    from one instant: ask twice and a hot-plug landing between the two questions
    looks exactly like a counter discontinuity. A reader that hands back a single
    aggregate object instead of a per-device dict (an odd psutil, or a test double)
    is taken at its word and simply contributes no device set, which switches that
    one rule off rather than letting it invent a change.
    """
    if reading is None:
        return None, None
    try:
        if isinstance(reading, dict):
            if not reading:
                return None, None         # enumerated, and found nothing: no counters
            vals = tuple(sum(getattr(c, f) for c in reading.values())
                         for f in fields)
            return vals, frozenset(reading)
        return tuple(getattr(reading, f) for f in fields), None
    except (AttributeError, TypeError):   # not shaped like counters: treat as missing
        return None, None


class _Rate:
    """One psutil counter family, turned into a per-second rate honestly.

    psutil's network and disk counters are sums of per-device counters, and a sum is
    only a monotonic counter for as long as the devices behind it are the devices it
    was summed over. Take an adapter out, reset a driver, hibernate the box, or fail
    to get a reading at all, and the number stops being comparable with the one held
    from last tick. Subtracting anyway is what produced both halves of the bug this
    class ends: a missing baseline raised `AttributeError` on *every* tick from then
    on (a counter never re-primes itself, so the raise repeats until the process
    dies, and the whole snapshot is discarded with it), and a counter that went
    backwards produced rates like `-7.2e13` bits/s.

    So this holds the last totals, the moment they were taken, and the set of devices
    they were summed over, and answers `None` — no number yet — whenever the interval
    is not a valid one. `None` is what every consumer already renders as `--`, and it
    is the only honest answer on the tick a baseline is being re-established: the rate
    is not zero, and it is not the last value, it is not measurable until the next
    interval has actually elapsed.
    """

    def __init__(self, fields: tuple, scale: float = 1.0,
                 max_gap_s: float = _MIN_GAP_S) -> None:
        self.fields = fields          # the counter attributes to difference, in order
        self.scale = scale            # 8.0 for network: links are quoted in bits
        self.max_gap_s = max_gap_s
        self.last: tuple | None = None
        self.last_ts = 0.0
        self.last_ids: frozenset | None = None
        self.resets = 0               # how often this had to start over (tests, logs)

    def rate(self, now: float, reading):
        """Per-second rates for `fields`, or None when this tick cannot give one."""
        values, ids = _totals(reading, self.fields)
        if values is None:
            # An unavailable reading invalidates the baseline rather than being
            # subtracted from it, and the first sample after it comes back is spent
            # re-priming. Conservative by one tick, and the issue asks for exactly
            # that: an unavailable counter is a baseline-reset event.
            self._prime(None, now, None)
            return None
        if self.last is None or (ids is not None and ids != self.last_ids):
            # Nothing comparable to subtract, or the devices the baseline was summed
            # over are not the devices in front of us. A replacement can *raise* the
            # aggregate, so monotonicity alone would not have noticed it.
            self._prime(values, now, ids)
            return None
        dt = now - self.last_ts
        if dt <= 0.0 or dt > self.max_gap_s:
            # `dt <= 0` also retires the old `max(dt, 1e-6)` clamp, which was the
            # quiet half of the spike: two samples in the same instant divided a real
            # byte count by a microsecond and printed it as a rate.
            self._prime(values, now, ids)
            return None
        deltas = [v - b for v, b in zip(values, self.last)]
        if any(d < 0 for d in deltas):
            # A counter that went down is not a negative rate. It is a device that
            # left, a driver that reset, or a wrap, and in every case the old total
            # has nothing to do with the new one.
            self._prime(values, now, ids)
            return None
        self.last, self.last_ts = values, now
        return [d * self.scale / dt for d in deltas]

    def _prime(self, values, now: float, ids) -> None:
        """Start a new baseline; the rate it enables is the *next* tick's."""
        self.last, self.last_ts, self.last_ids = values, now, ids
        self.resets += 1


class FallbackBackend:
    def __init__(self, cfg: dict):
        psutil.cpu_percent(interval=None)  # prime
        psutil.cpu_times_percent(interval=None)
        # The two rate families are deliberately *not* primed here, the way the CPU
        # call above is. A baseline taken in the constructor is not a sample: the
        # first tick would divide whatever the machine did while the app was still
        # starting by the few milliseconds since `__init__`, which is where the
        # absurd first-second figures came from. The first `sample()` establishes the
        # baseline and reports `--`; the second one has a real interval to measure.
        gap = max(_MIN_GAP_S, _interval_s(cfg) * _GAP_FACTOR)
        self._net = _Rate(("bytes_recv", "bytes_sent"), scale=8.0, max_gap_s=gap)
        self._disk = _Rate(("read_bytes", "write_bytes"), max_gap_s=gap)
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
        # Each family answers for itself, and neither can raise: `sample()` is one
        # unit upstream, so a rate that could not be worked out leaves its two fields
        # at None (the panel draws `--`) and the CPU, memory and GPU readings that
        # were taken fine still arrive.
        rates = self._net.rate(now, _read(psutil.net_io_counters, pernic=True))
        if rates is not None:
            snap.net_down_bps, snap.net_up_bps = rates

        rates = self._disk.rate(now, _read(psutil.disk_io_counters, perdisk=True))
        if rates is not None:
            snap.disk_read_bps, snap.disk_write_bps = rates

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
