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

Why one worker instead of a thread per attempt: bounding the *wait* is not the
same promise as bounding the *work*. Waiting on a daemon thread that is thrown
away when it loses its race left that thread inside the driver, and the next
tick started a second one beside it - eight blocked ticks were eight live
Computer.Open() calls, eight NVML owners, eight immortal threads, with nothing
in the process saying so. So the hub owns its backend through ONE supervisor
thread per backend *generation*, and every native operation it can start -
acquire, sample, release - goes through that one thread, one at a time. A tick
that runs out of patience leaves the operation outstanding and collects it
later; it never grows a second one. When native work genuinely cannot be
cancelled the worker is retired rather than joined (see `_escalate`), which
leaks a thread on purpose, counts it, and stops at `MAX_LIVE_WORKERS` - a
deliberate, observable escalation is a better release contract than unbounded
native work wearing the costume of a timeout.

Generation is the fence. Every handed operation carries the generation it
belongs to and reports its outcome into the operation, never into the hub, so
a driver that finally answers after its backend was retired has nothing left
to overwrite: the hub drops late results and late errors on the floor and says
so in `describe()`.
"""
from __future__ import annotations

import atexit
import copy
import dataclasses
import queue
import threading
import time

from app.snapshot import Snapshot

# The supervisor's two ceilings. A close gets its own budget because it runs on
# the shutdown and rebuild paths, where the loop has already decided to move on;
# the worker ceiling is what turns "this driver will not let go" from one
# immortal thread per tick into a small, counted, logged leak.
_CLOSE_BUDGET_S = 2.0
MAX_LIVE_WORKERS = 4

_SAMPLE, _REOPEN, _CLOSE, _STOP = "sample", "reopen", "close", "stop"


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


class _Job:
    """One unit of owned driver work - and the fence around whatever it reports.

    The supervisor writes into the job, never into the hub: `gen` says which
    backend generation the work was handed under, `done` is the only signal the
    loop waits on, and `result`/`backend`/`error` are collected by whoever is
    still holding this generation. A job whose generation has been retired is
    dropped whole, which is what stops a driver that answers two minutes late
    from rewriting diagnostics and provenance behind the loop's back.

    The three failures are kept apart because they mean different things about
    ownership. `build_error` is a construction that failed: the hub is left
    holding the backend it tried and failed to replace. `error` is a sample that
    threw, which - when the backend itself was freshly built - leaves the hub
    holding a *new* driver that must not be thrown away with the bad tick.
    `why_close_failed` is the one nobody is waiting for: a release that threw on
    its way out, recorded on the job so the *next* operation can ask again
    (every backend's close is safe to repeat) rather than the reason being lost
    with the generation that had it.
    """

    __slots__ = ("kind", "gen", "snap", "done", "result", "backend",
                 "error", "build_error", "why_close_failed")

    def __init__(self, kind: str, gen: int, snap: Snapshot | None = None):
        self.kind = kind
        self.gen = gen
        self.snap = snap
        self.done = threading.Event()
        self.result: Snapshot | None = None
        self.backend = None                # the backend a `_REOPEN` built
        self.error: str | None = None      # why this operation is not healthy
        self.build_error: str | None = None
        self.why_close_failed: str | None = None


_STOP_JOB = _Job(_STOP, -1)


class SensorHub:
    """Wraps a backend; emits one Snapshot per tick(), and never raises.

    Freshness and provenance travel on the snapshot itself: `source` names
    the backend, `held` marks a re-published last-good sample (with `age_s`),
    and `failed` names the metric groups that did not answer this tick - so
    the layout can dim them, and history can draw a gap instead of a flat
    lie. Backends report per-group failures inside `sample()`; the hub owns
    the timeout, the grace, the blanking, and the rebuild-with-backoff.

    Every failure counts toward the rebuild, including the ones that answered:
    a backend that returns a snapshot marked failed is a backend that is dying
    partway, and only re-acquiring the driver can fix a driver.
    """

    def __init__(self, backend, cfg: dict | None = None, want: str = ""):
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
        # Ownership: one supervisor per generation, one outstanding job per
        # supervisor. `self._lock` guards the hub's state, never a driver call -
        # nothing blocks on a driver while holding it.
        self._lock = threading.Lock()
        self._backend = backend
        self._gen = 0
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._retired: list[threading.Thread] = []
        self._inflight: _Job | None = None
        self._handed_at = 0.0
        self._want_reopen = False
        self._wedged = 0                     # deliberate retirements (escalations)
        self._dropped = 0                    # fenced results from retired generations
        self._stopping = False

    # ------------------------------------------------------------------ ownership
    @property
    def backend(self):
        """The backend the supervisor owns right now (None while one is wedged).

        Assigning one is how the tests swap fakes, and how a caller takes a driver
        out of the loop: the generation is retired, so whatever the old thread is
        still doing inside the old backend is dropped rather than published, and
        the new backend gets a fresh supervisor. The retired one is *not* closed
        here - it may be the call that is wedged, and a second thread calling
        Close() into a driver the first is still inside is exactly the concurrent
        native work this ownership exists to prevent.
        """
        return self._backend

    @backend.setter
    def backend(self, value) -> None:
        self._retire_locked(value)

    def _retire_locked(self, value) -> None:
        """Cut the hub over to `value` as a new generation, without closing the old."""
        with self._lock:
            old_q, old_worker = self._q, self._worker
            self._backend = value
            self._gen += 1
            self._inflight = None
            self._worker = None
            self._q = queue.Queue()
            self._want_reopen = False
            self._remember_retired_locked(old_worker)
        if old_worker is not None:
            old_q.put(_STOP_JOB)

    def _remember_retired_locked(self, worker) -> None:
        """Track a worker this hub stopped owning, pruning the ones that got free."""
        self._retired = [t for t in self._retired if t.is_alive()]
        if worker is not None and worker.is_alive():
            self._retired.append(worker)

    def _live_workers_locked(self) -> int:
        n = len([t for t in self._retired if t.is_alive()])
        if self._worker is not None and self._worker.is_alive():
            n += 1
        return n

    def _ensure_worker_locked(self) -> None:
        # Deliberately *not* gated on `_stopping`: the release handed to the
        # supervisor by `close()` is the one job that must still run while the
        # hub is stopping, and a worker refused here is a close that waits its
        # whole budget and then leaks the driver it was asked to give up.
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._supervise,
                                        args=(self._q, self._gen),
                                        daemon=True, name="sensor-supervisor")
        self._worker.start()

    def _supervise(self, q, gen: int) -> None:
        """The only thread that touches the owned backend, one job at a time.

        It is persistent on purpose: a tick that gives up does not leave it
        behind, it leaves the job outstanding, and the same worker reports the
        answer when the driver eventually lets go. It exits as soon as its
        generation is retired *and* the call it is inside returns, which is the
        one form of cancellation Python has for a native call.
        """
        while True:
            job = q.get()
            if job.kind == _STOP or gen != self._gen:
                return
            try:
                self._perform(job)
            finally:
                job.done.set()
            if gen != self._gen:
                return

    def _perform(self, job: _Job) -> None:
        """Run one operation. Shared hub state is read here and never written."""
        if job.kind == _CLOSE:
            self._release(self._backend, job)
            return
        with self._lock:
            backend = self._backend
        if job.kind == _REOPEN:
            # Close what we own before building more of it - a leaked Computer or
            # NVML owner is the reason the rebuild was asked for. A previous
            # release attempt that threw leaves the same object still held, so it
            # is asked again; every backend's close is safe to repeat.
            self._release(backend, job)
            try:
                job.backend = _make_backend(self._cfg, self.want)
            except Exception as e:  # noqa: BLE001 - the hub backs off on this
                job.error = f"{type(e).__name__}: {e}"
                return
            backend = job.backend
        if job.snap is None:
            return
        if backend is None:
            # Nothing owned: the wedged previous backend was abandoned, or the
            # last rebuild could not be built. Say so rather than sample None.
            job.error = "no backend is owned right now"
            return
        try:
            backend.sample(job.snap)
        except Exception as e:  # noqa: BLE001 - the raise is a failed sample
            job.snap.failed = tuple(sorted({*job.snap.failed, "sample"}))
            job.error = f"sample raised {type(e).__name__}: {e}"
        job.result = job.snap

    def _release(self, backend, job: _Job) -> None:
        """Ask an owned backend to give up its handles, on *this* thread.

        The old helper spawned a short-lived thread so the loop would not wait
        for a close that never returns; that made the close concurrent with
        whatever the abandoned sampler was still doing to the same driver, and
        it grew a thread per rebuild. Here the close is just another bounded
        operation: the loop stops waiting after `_CLOSE_BUDGET_S`, the worker
        stays with the driver, and `_escalate` retires it once the grace window
        says the wait is over. Leaking a hung handle is still the accepted cost
        of never blocking the loop - it is just counted now.
        """
        close = getattr(backend, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as e:  # noqa: BLE001 - a half-dead backend may not close clean
            job.why_close_failed = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------ dispatch
    def _idle(self) -> bool:
        """True when the supervisor owes this hub nothing uncollected."""
        with self._lock:
            job = self._inflight
            return job is None or job.done.is_set()

    def _new_snap(self) -> Snapshot:
        return Snapshot(ts=time.time(), source=self.want)

    def _dispatch(self, kind: str, now: float,
                  snap: Snapshot | None = None) -> Snapshot | None:
        """Hand one bounded operation to the supervisor and wait this tick's budget.

        This is the whole answer to "bounded native work": the wait is bounded
        *and* there is only ever one operation outstanding, so a driver that
        never answers costs one patient worker instead of one thread per tick.
        When the budget runs out the job stays in flight and the next tick
        collects it - a slow driver still gets to produce fresh samples, just not
        on the tick it was late on.
        """
        with self._lock:
            if self._stopping:
                return None
            job = self._inflight
            if job is not None and not job.done.is_set():
                self._why = (f"{job.kind} has been inside the driver for "
                             f"{now - self._handed_at:.1f}s (gen {job.gen})")
                return None
            job = _Job(kind, self._gen, snap)
            self._inflight = job
            self._handed_at = now
            self._ensure_worker_locked()
            self._q.put(job)
        budget = _CLOSE_BUDGET_S if kind == _CLOSE else self.tick_timeout_s
        if not job.done.wait(budget):
            with self._lock:
                if self._inflight is job:
                    self._why = (f"{kind} did not answer in {budget:.1f}s "
                                 f"(gen {job.gen}, still owned)")
            return None
        return self._drain(now)

    def _drain(self, now: float) -> Snapshot | None:
        """Collect a finished operation, if one is waiting to be collected."""
        with self._lock:
            job = self._inflight
            if job is None or not job.done.is_set():
                return None
            self._inflight = None
            if job.gen != self._gen:
                self._dropped += 1
                self._why = f"dropped a late {job.kind} from retired gen {job.gen}"
                return None
            return self._apply_locked(job, now)

    def _apply_locked(self, job: _Job, now: float) -> Snapshot | None:
        """Publish one collected operation - the only place ownership state moves."""
        if job.kind == _CLOSE:
            return None
        if job.kind == _REOPEN:
            if job.error is not None or job.backend is None:
                # Construction itself failed (or hung and this is its answer):
                # retry on the backoff clock, never per tick.
                self._why = f"reopen failed: {job.error or 'produced no backend'}"
                self._next_try = now + self._backoff
                self._backoff = min(self._backoff * 2, self._backoff_max_s)
                self._want_reopen = True
                return None
            self._backend = job.backend
            self._rebuilds += 1
            self._fails = 0
            self._next_try = now
            self._backoff = self._backoff_s
            self._want_reopen = False
            self._why = f"rebuilt the backend (#{self._rebuilds})"
        snap = job.result
        if snap is None:
            if job.error is not None:
                self._why = job.error
            return None
        _sanitize(snap)
        if "sample" in snap.failed and _empty(snap):
            if job.error is not None:
                self._why = job.error
            return None
        return snap

    # ---------------------------------------------------------------------- tick
    def tick(self, now: float | None = None) -> Snapshot:
        """One bounded sample: fresh if the backend answered, the last good
        one within the grace window, blank after it. `now` is a pinned
        monotonic clock for the deterministic tests."""
        now = time.monotonic() if now is None else now
        snap = self._attempt(now)
        if snap is not None:
            self._good, self._good_at = snap, now
            if not snap.failed:
                # A clean answer ends the failure streak. Anything named as
                # failed - a sample that died partway *or* a group that keeps
                # not answering - keeps it counting: a metric group that is
                # quiet forever is a driver that needs re-acquiring, and the
                # only repair this hub has is the rebuild below.
                self._fails = 0
            self._phase = "ok" if not snap.failed else "partial"
            self._why = "ok" if not snap.failed else \
                "groups failed: " + ",".join(snap.failed) + self._detail()
            return snap
        if self._good is not None and now - self._good_at <= self.stale_grace_s:
            self._phase = "held"
            return self._held(now)
        self._phase = "blind"
        return self._blank(now)

    def _detail(self) -> str:
        """The backend's own note about the driver behind a named gap.

        `failed` says which pane is dim; `last_error` (which the backends keep
        for exactly this) says why, and that is the half a log line needs."""
        err = getattr(self._backend, "last_error", "") or ""
        return f" ({err})" if err else ""

    def _attempt(self, now: float) -> Snapshot | None:
        snap = self._drain(now)                 # a deferred answer, if one landed
        if snap is None and self._idle():
            if now < self._next_try:
                return None                      # reopening on the backoff clock
            if self._want_reopen:
                snap = self._request_reopen(now)
            if snap is None and not self._want_reopen:
                snap = self._dispatch(_SAMPLE, now, self._new_snap())
        if snap is None:
            self._fails += 1
        elif snap.failed:
            self._fails += 1
        if self._cfg is not None and self._fails >= self.reopen_after:
            # Checked on *every* failing tick, not only on the blind ones: a
            # backend that hands back a partial sample forever used to count
            # failures it would never act on.
            rebuilt = self._request_reopen(now)
            if rebuilt is not None:
                snap = rebuilt
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
    def _request_reopen(self, now: float) -> Snapshot | None:
        """Rebuild the owned backend, and let the fresh one answer this tick.

        `recover()` and the failure streak both end up here, and none of them
        gets to wait for a driver: the work is handed to the supervisor, whose
        answer is waited on for one tick budget and otherwise collected later.
        While an operation is already in flight the rebuild is *deferred*, not
        started beside it - the one exception is a call that has outlasted the
        whole grace window, which is escalated instead (see `_escalate`).
        """
        if self._cfg is None or self._stopping:
            return None                          # without a cfg, rebuilding is off
        if now < self._next_try:
            return None                          # on the backoff clock, not per tick
        if not self._idle():
            with self._lock:
                job = self._inflight
                wedged = (job is not None and not job.done.is_set()
                          and now - self._handed_at >= self.stale_grace_s)
            if not wedged:
                self._want_reopen = True         # ask again once the driver lets go
                return None
            if not self._escalate(now):
                return None                      # at the ceiling: say so, stay bounded
        return self._dispatch(_REOPEN, now, self._new_snap())

    def _escalate(self, now: float) -> bool:
        """Retire a supervisor that has been inside one call for the whole grace.

        A driver that has not answered in `stale_grace_s` is not going to answer
        this session, and a hub that only waits is a panel that stays dark until
        the app is restarted - the failure this whole module was written against.
        So the owner is given up: its thread and its handles are leaked *on
        purpose*, counted in `_wedged`, capped at `MAX_LIVE_WORKERS`, and a fresh
        supervisor takes a freshly built backend. The abandoned backend is not
        closed (something is still inside it), which is why its generation is
        retired rather than re-used.
        """
        with self._lock:
            job = self._inflight
            if job is None or job.done.is_set() or self._stopping:
                return False
            live = self._live_workers_locked()
            if live >= MAX_LIVE_WORKERS:
                self._why = (f"still wedged in gen {job.gen}: {live} supervisors "
                             f"alive, ceiling {MAX_LIVE_WORKERS}")
                return False
            old_q, old_worker = self._q, self._worker
            self._gen += 1
            self._inflight = None
            self._worker = None
            self._q = queue.Queue()
            self._backend = None                 # owned by the retired worker now
            self._remember_retired_locked(old_worker)
            self._wedged += 1
            self._want_reopen = True
            self._next_try = now + self._backoff
            self._backoff = min(self._backoff * 2, self._backoff_max_s)
            self._why = (f"retired the wedged supervisor (gen {job.gen}, "
                         f"escalation #{self._wedged} of at most {MAX_LIVE_WORKERS})")
        if old_worker is not None:
            old_q.put(_STOP_JOB)
        return True

    def recover(self, reason: str, now: float | None = None) -> None:
        """Re-acquire everything the backends hold (GPU handles, the LHM
        Computer) after a resume or a device change. A demo backend holds no
        driver resources, and swapping it would only lose the synthetic
        clock the preview depends on.

        Bounded like everything else here: the resume path asks for the rebuild
        and waits one tick's budget for it, because a `Computer.Open()` that
        hangs on a wake would otherwise stop the loop before it ever got to the
        frame it woke for. The answer is collected by the next tick if it is
        late, and by `_escalate` if it never comes.
        """
        if self._cfg is None or self.want == "demo":
            return
        self._why = f"recover: {reason}"
        self._request_reopen(time.monotonic() if now is None else now)

    def close(self) -> None:
        """Release backend resources on shutdown (registered by make_hub)."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            job = self._inflight
            if job is not None and not job.done.is_set():
                self._q.put(_STOP_JOB)
                self._why = f"shutdown while gen {job.gen} is still inside the driver"
                return                           # bounded: leak it, do not join it
            job = _Job(_CLOSE, self._gen)
            self._inflight = job
            q = self._q
            self._ensure_worker_locked()
            q.put(job)
        job.done.wait(_CLOSE_BUDGET_S)
        q.put(_STOP_JOB)

    # ------------------------------------------------------------------- log
    def describe(self) -> str:
        with self._lock:
            job = self._inflight
            owed = ("-" if job is None or job.done.is_set()
                    else f"{job.kind}@gen{job.gen}")
            return (f"sensors={self._phase} backend={type(self._backend).__name__} "
                    f"fails={self._fails} rebuilds={self._rebuilds} "
                    f"gen={self._gen} owed={owed} "
                    f"workers={self._live_workers_locked()}/{MAX_LIVE_WORKERS} "
                    f"wedged={self._wedged} dropped={self._dropped} ({self._why})")

    def changed_to_log(self) -> str | None:
        """One sentence per distinct state. A blind window is loggable; a
        blind window that logs every second is noise, so the key is the
        phase, not the per-tick detail. `_wedged` is in the key because an
        escalation is exactly the kind of thing an operator needs to see."""
        key = (self._phase, type(self.backend).__name__, self._rebuilds, self._wedged)
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
