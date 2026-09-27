"""Sensor backends, and the supervisor that owns them.

Backend selection:
  lhm      - LibreHardwareMonitorLib via pythonnet: full fidelity
             (package power, per-core clocks, Tctl temp, GPU detail). Needs admin.
  fallback - psutil + NVML: no admin, real host metrics, GPU via NVML.
             CPU temp/power/per-core clocks unavailable → None (rendered as "--").
  demo     - synthetic data to develop/preview the UI.
  auto     - lhm if importable, else fallback.

Why the hub is a supervisor and not a pass-through: the backends call into
drivers - NVML, pythonnet/LibreHardwareMonitor, psutil's counter paths - and
drivers are late to fail and quiet to hang. A raise inside one metric used to
discard the whole fresh sample; a raise out of the tick used to be answered
by re-presenting the *previous* snapshot forever, flat lines and all, pushed
into the trend bands as if they had been measured; and a wedged native call
owned the control loop. The hub bounds every backend call, re-publishes the
last good sample only for a documented grace and then blanks to honest "--",
rebuilds the backend on its own backoff clock after repeated failures, and
re-acquires everything on resume - so a driver reset heals without restarting
the app.
"""
from __future__ import annotations

import atexit
import copy
import dataclasses
import threading
import time

from app.snapshot import Snapshot


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and v == v and v not in (float("inf"), float("-inf"))


def _sanitize(snap: Snapshot) -> None:
    """Reject non-finite readings before anything downstream can draw them.

    A driver mid-reset happily returns NaN, and NaN sneaks past every
    `if v is None` guard in the layout - one of it can blank a whole render.
    Anything that is not a finite number becomes None, which every consumer
    already knows how to show as "--".
    """
    for obj in (snap, snap.cpu, snap.gpu, snap.frames):
        for f in dataclasses.fields(obj):
            v = getattr(obj, f.name)
            if isinstance(v, float) and not _finite(v):
                setattr(obj, f.name, None)


def _empty(snap: Snapshot) -> bool:
    """True when the backend answered nothing measurable at all.

    The nested CpuStats/GpuStats objects are *always* non-None (dataclass
    defaults), so their fields must be inspected individually - treating the
    objects themselves as values would call every empty sample measurable.
    """
    skip = {"ts", "source", "held", "age_s", "failed", "frames", "cpu", "gpu"}
    vals = [getattr(snap, f.name) for f in dataclasses.fields(snap)
            if f.name not in skip]
    vals += [getattr(snap.cpu, f.name) for f in dataclasses.fields(snap.cpu)]
    vals += [getattr(snap.gpu, f.name) for f in dataclasses.fields(snap.gpu)]
    return all(v is None for v in vals)


def _close_quietly(backend) -> None:
    """Ask a backend to release its handles; give up on it if it hangs.

    A driver that wedged the sample can wedge close() too, and waiting for it
    would put the control loop back in that driver's hands - so the close
    runs on a daemon thread with a short budget. Leaking a hung handle is the
    accepted cost of never blocking the loop.
    """
    close = getattr(backend, "close", None)
    if close is None:
        return
    t = threading.Thread(target=close, daemon=True, name="sensor-close")
    t.start()
    t.join(2.0)


class SensorHub:
    """Wraps a backend; emits one Snapshot per tick(), and never raises.

    Freshness and provenance travel on the snapshot itself: `source` names
    the backend, `held` marks a re-published last-good sample (with `age_s`),
    and `failed` names the metric groups that did not answer this tick - so
    the layout can dim them, and history can draw a gap instead of a flat
    lie. Backends report per-group failures inside `sample()`; the hub owns
    the timeout, the grace, the blanking, and the rebuild-with-backoff.
    """

    def __init__(self, backend, cfg: dict | None = None, want: str = ""):
        self.backend = backend
        self._cfg = cfg                      # without it, rebuilding is off
        self.want = want or type(backend).__name__.lower().replace("backend", "")
        s = (cfg or {}).get("sensors", {})
        self.tick_timeout_s = float(s.get("tick_timeout_s", 2.0))
        self.stale_grace_s = float(s.get("stale_grace_s", 30.0))
        self.reopen_after = max(1, int(s.get("reopen_after", 3)))
        self._backoff_s = float(s.get("reopen_backoff_s", 5.0))
        self._backoff_max_s = max(self._backoff_s,
                                  float(s.get("reopen_backoff_max_s", 60.0)))
        self._good: Snapshot | None = None
        self._good_at = 0.0
        self._fails = 0
        self._next_try = 0.0
        self._backoff = self._backoff_s
        self._rebuilds = 0
        self._why = "ok"
        self._phase = "ok"
        self._said: tuple = ()

    # ------------------------------------------------------------------ tick
    def tick(self, now: float | None = None) -> Snapshot:
        """One bounded sample: fresh if the backend answered, the last good
        one within the grace window, blank after it. `now` is a pinned
        monotonic clock for the deterministic tests."""
        now = time.monotonic() if now is None else now
        snap = self._attempt(now)
        if snap is not None:
            self._good, self._good_at = snap, now
            if "sample" not in snap.failed:
                # A clean answer ends the failure streak. A *partial* one (the
                # sample died partway) keeps the streak counting: a backend
                # that keeps dying mid-sample is a dead backend, just slower.
                self._fails = 0
            self._phase = "ok" if not snap.failed else "partial"
            self._why = "ok" if not snap.failed else \
                "groups failed: " + ",".join(snap.failed)
            return snap
        if self._good is not None and now - self._good_at <= self.stale_grace_s:
            self._phase = "held"
            return self._held(now)
        self._phase = "blind"
        return self._blank(now)

    def _attempt(self, now: float) -> Snapshot | None:
        if now < self._next_try:
            return None                      # reopening on the backoff clock
        snap = self._run()
        if snap is None:                     # timed out, or raised with nothing measured
            self._fails += 1
            if self._cfg is not None and self._fails >= self.reopen_after:
                if self._reopen(now):
                    snap = self._run()       # the fresh backend gets its chance now
            if snap is None:
                return None
        if "sample" in snap.failed:
            self._fails += 1                 # died partway; what answered is still real
        return snap

    def _run(self) -> Snapshot | None:
        """One backend call, bounded. None means it did not produce a sample."""
        snap = Snapshot(ts=time.time(), source=self.want)
        done = threading.Event()

        def run():
            try:
                self.backend.sample(snap)
            except Exception as e:  # noqa: BLE001 - the raise is a failed sample
                snap.failed = tuple(sorted({*snap.failed, "sample"}))
                self._why = f"sample raised {type(e).__name__}: {e}"
            finally:
                done.set()

        # A driver that never returns must not own the control loop: the wait
        # is bounded and the thread is abandoned when it loses. A hung thread
        # leaks (native calls cannot be killed from Python), which is why the
        # backoff below also throttles how fast the next attempt can start.
        threading.Thread(target=run, daemon=True, name="sensor-sample").start()
        if not done.wait(self.tick_timeout_s):
            self._why = f"backend did not answer in {self.tick_timeout_s:.1f}s"
            return None
        _sanitize(snap)
        if "sample" in snap.failed and _empty(snap):
            return None
        return snap

    def _held(self, now: float) -> Snapshot:
        """The last good sample, re-published and *marked* - never silently live.

        The panel may keep showing it (main renders held samples); history
        must not (main pushes measured samples only), because a re-published
        value drawn as a measured one is exactly the flat lie this module
        exists to stop. Frames belong to the frame monitor, which keeps its
        own hold semantics, so the copy carries an empty one.
        """
        return dataclasses.replace(self._good, held=True,
                                   age_s=now - self._good_at,
                                   frames=copy.copy(self._good.frames))

    def _blank(self, now: float) -> Snapshot:
        """Grace expired: every metric None, and the panel shows its honest
        "--". Pushing these into history draws gaps, which is the truth."""
        return Snapshot(ts=time.time(), source=self.want, failed=("blind",))

    # -------------------------------------------------------------- rebuilding
    def _reopen(self, now: float) -> bool:
        """Close what we can and re-make the backend: this is what a driver
        reset or a device change needs, and it must not need an app restart."""
        _close_quietly(self.backend)
        try:
            self.backend = _make_backend(self._cfg, self.want)
        except Exception as e:  # noqa: BLE001 - retry on the backoff clock
            self._why += f"; reopen failed: {type(e).__name__}: {e}"
            self._next_try = now + self._backoff
            self._backoff = min(self._backoff * 2, self._backoff_max_s)
            return False
        self._rebuilds += 1
        self._why = f"rebuilt the backend (#{self._rebuilds})"
        self._next_try = now
        self._fails = 0
        self._backoff = self._backoff_s
        return True

    def recover(self, reason: str, now: float | None = None) -> None:
        """Re-acquire everything the backends hold (GPU handles, the LHM
        Computer) after a resume or a device change. A demo backend holds no
        driver resources, and swapping it would only lose the synthetic
        clock the preview depends on."""
        if self._cfg is None or self.want == "demo":
            return
        self._why = f"recover: {reason}"
        self._reopen(time.monotonic() if now is None else now)

    def close(self) -> None:
        """Release backend resources on shutdown (registered by make_hub)."""
        _close_quietly(self.backend)

    # ------------------------------------------------------------------- log
    def describe(self) -> str:
        return (f"sensors={self._phase} backend={type(self.backend).__name__} "
                f"fails={self._fails} rebuilds={self._rebuilds} ({self._why})")

    def changed_to_log(self) -> str | None:
        """One sentence per distinct state. A blind window is loggable; a
        blind window that logs every second is noise, so the key is the
        phase, not the per-tick detail."""
        key = (self._phase, type(self.backend).__name__, self._rebuilds)
        if key == self._said:
            return None
        self._said = key
        return "[sensors] " + self.describe()


def _make_backend(cfg: dict, want: str):
    """Build the backend `want` names - the same ladder make_hub walks, kept
    as one function so the hub can rebuild itself after a persistent failure."""
    if want in ("auto", "lhm"):
        try:
            from app.sensors.lhm import LhmBackend
            return LhmBackend(cfg)
        except Exception as e:  # noqa: BLE001 - fallback by design
            if want == "lhm":
                raise
            print(f"[sensors] LHM unavailable ({e.__class__.__name__}: {e}); using fallback")

    if want in ("auto", "fallback"):
        from app.sensors.fallback import FallbackBackend
        return FallbackBackend(cfg)

    if want == "demo":
        from app.sensors.demo import DemoBackend
        return DemoBackend(cfg)

    raise ValueError(f"unknown sensor backend: {want}")


def make_hub(cfg: dict, force: str | None = None) -> SensorHub:
    want = (force or cfg["sensors"]["backend"]).lower()
    hub = SensorHub(_make_backend(cfg, want), cfg=cfg, want=want)
    # Shutdown releases the driver handles too, not just the process: NVML and
    # the LHM ring0 device are polite about it, and a leaked Computer() is one
    # more reason the next start behaves differently from this one.
    atexit.register(hub.close)
    return hub