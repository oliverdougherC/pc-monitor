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

# NVML is NOT opened at import time any more (issue #17): a driver that is still
# loading at logon used to poison GPU telemetry for the life of the process,
# because the module-level `nvmlInit()` ran once, failed once, and nothing tried
# again. The open now happens on the first sample and retries on a clock - see
# `FallbackBackend._nvml_next`.

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


class CounterUnavailable(RuntimeError):
    """A psutil counter path that could not answer at all.

    Distinct from "the counter is behind a gap" (which re-primes and reports
    `--`): this is the metric group being *named* as failed, because a number
    that is missing for a reason nobody recorded is indistinguishable, on the
    panel, from a number that stopped updating.
    """


def _read(fn, **kw):
    """psutil's counters, or None when the counter path itself is gone.

    A dead Performance Counter is a missing number, not a dead sample. Letting it
    raise here would cost the CPU, memory and GPU readings that answered fine,
    because a `sample()` is one unit upstream: `SensorHub.tick()` propagates the
    raise and the loop throws the whole snapshot away, which is how one broken
    disk counter turned into a panel that stopped moving.

    The per-device keyword is asked for because the device set behind a summed
    counter is what makes the sum comparable across ticks (`_Rate`). A psutil (or
    a test double) that does not take the keyword is not a dead counter: it is
    the same counter with no device set, so it is read without it and simply
    contributes no device set - one rule switches off, the numbers survive.
    """
    try:
        return fn(**kw)
    except TypeError:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - unavailable is a value, not a fault
            return None
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
        s = cfg.get("sensors", {})
        # How soon to try NVML again after a failed open (driver still
        # loading, device mid-reset). The first attempt happens on the first
        # sample, so a driver that is late at boot costs GPU for a tick, not
        # for the life of the process.
        self._nvml_retry_s = float(s.get("nvml_retry_s", 60.0))
        self._pynvml = None
        self._gpu = None
        self._nvml_next = 0.0
        # Successful `nvmlInit()` calls this backend still owns, as a count (#69).
        # NVML's init/shutdown contract is reference-counted: every successful init
        # takes a reference and only the matching `nvmlShutdown()` gives it back, with
        # the library unloading when the count reaches zero. So the *initialization*
        # and the *device handle* are two different pieces of state, and owning the
        # first while failing to find the second is the exact case the old code leaked:
        # `nvmlDeviceGetCount() < 1` returned before `self._pynvml` was ever assigned,
        # which meant `close()` could not even see the reference it had taken. Tracking
        # the count separately is what makes every early return balancable, and it is
        # also what lets a later attempt *reuse* an initialization instead of stacking
        # a second one on top of it.
        self._nvml_inits = 0
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
        # Each family answers for itself, and neither can kill the sample: `sample()`
        # is one unit upstream, so a rate that could not be worked out leaves its two
        # fields at None (the panel draws `--`) and the CPU, memory and GPU readings
        # that were taken fine still arrive. `pernic`/`perdisk` come from the same
        # call as the numbers so a hot-plug between two calls cannot masquerade as a
        # counter discontinuity; a gap over `max_gap_s` re-primes instead of
        # averaging a suspend into a plausible lie.
        reading = _read(psutil.net_io_counters, pernic=True)
        rates = self._net.rate(now, reading)   # a None reading re-primes the baseline
        if reading is None:
            # A counter path that cannot answer is a *named* gap, not a silent one:
            # the snapshot must say `net` failed, because `--` that nobody can
            # explain is exactly the stale-looking panel this whole module exists
            # to prevent. The baseline was already invalidated above, so the tick
            # after this one re-primes instead of spanning the outage.
            raise CounterUnavailable("net_io_counters did not answer")
        if rates is not None:
            snap.net_down_bps, snap.net_up_bps = rates

    def _sample_disk(self, snap: Snapshot) -> None:
        # Its own group, its own failure: a machine with no disk counters (a bare
        # VM, a dead performance-counter library) loses `disk`, not `net`.
        now = time.monotonic()
        reading = _read(psutil.disk_io_counters, perdisk=True)
        rates = self._disk.rate(now, reading)
        if reading is None:
            raise CounterUnavailable("disk_io_counters did not answer")
        if rates is not None:
            snap.disk_read_bps, snap.disk_write_bps = rates

    # --------------------------------------------------------------------- gpu
    def _nvml_release(self, p) -> None:
        """Give back one initialization, and only one. Never raises.

        Called on every path that stops owning a reference — an early return after a
        successful init, a handle that could not be fetched, and `close()`. It is
        deliberately separate from `close()` because the leak in #69 was not a bad
        close: it was an *early return* that abandoned a reference `close()` then had
        no way to know about.
        """
        if p is None or self._nvml_inits <= 0:
            return
        self._nvml_inits -= 1
        try:
            p.nvmlShutdown()
        except Exception:  # noqa: BLE001 - a release that raises is still a release
            pass

    def _nvml_open(self, now: float) -> None:
        """Open NVML if it is not open and the retry window has passed.

        Failure here is deliberately quiet: a machine without NVIDIA (or with
        a driver still loading) is *unavailable*, not failing - the group
        answers None, and the retry window asks again later.

        The lifetime rule (#69) is one sentence: **this backend owns at most one
        initialization, and `close()` gives it back.** Everything else follows from it,
        and each clause answers a specific way the old code unbalanced the reference
        count NVML keeps:

        * `nvmlInit()` is only reached when we own nothing. A query error drops the
          *handle* and reopens the retry window; it does not drop the initialization, so
          the next attempt reuses it instead of stacking a second reference on top. That
          is what turned a persistent driver error into one outstanding init per second,
          all of which `close()` — which shut down at most once — could never unwind.
        * the "driver up, no device yet" and "handle lookup failed" paths **keep** the
          initialization and back off on the window. Releasing it there was tempting and
          wrong twice over: it unloads and reloads the whole library for a device that is
          merely late, and it made the reference count flicker, which is exactly the
          ambiguity this tracking exists to remove.
        * so the only release path outside `close()` is a driver reset (`_sample_gpu`),
          where the library itself is suspect.
        """
        if self._gpu is not None or now < self._nvml_next:
            return
        self._nvml_next = now + self._nvml_retry_s
        if self._pynvml is None:
            try:
                import pynvml
            except ImportError:
                return                  # not an NVIDIA box: nothing to re-acquire
            try:
                pynvml.nvmlInit()
            except Exception:  # noqa: BLE001 - driver not ready: retry on the window
                return
            self._pynvml = pynvml
            self._nvml_inits += 1
        p = self._pynvml
        try:
            if p.nvmlDeviceGetCount() < 1:
                # Driver up, no device yet. The initialization stays ours (see the
                # docstring): the next window asks again, and `close()` releases it.
                return
            self._gpu = p.nvmlDeviceGetHandleByIndex(0)
            self._nvml_next = 0.0
        except Exception:  # noqa: BLE001 - handle not ready: retry on the window
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
            # A driver reset shows up as errors on every call: drop the handle so the
            # next tick re-acquires it, and let the group fail so the hub counts it.
            #
            # The *initialization* is deliberately kept (#69). Dropping it here as well
            # is what produced an init-per-tick: this window was reset to zero, so the
            # next tick called `nvmlInit()` again while the reference from this tick was
            # still outstanding. The handle is what a reset invalidates; re-fetching one
            # against the initialization we already own is exactly what NVML expects, and
            # it means a persistent query failure costs one owned reference, not one per
            # second. The retry window stays as the backoff instead of being zeroed.
            self._gpu = None
            self._nvml_next = time.monotonic() + self._nvml_retry_s
            raise

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        """Release every NVML initialization this backend owns. Never raises.

        `close()` used to call `nvmlShutdown()` at most once, which silently discarded
        any reference the early-return paths had leaked and left the library loaded on
        the count. It now gives back exactly as many references as were taken — the
        count is authoritative, so a repeated `close()` is a no-op rather than a second
        shutdown, and a backend replaced while it still owned two references releases
        both (#69).
        """
        p = self._pynvml
        self._gpu = None
        while self._nvml_inits > 0:
            self._nvml_release(p)
        self._pynvml = None
