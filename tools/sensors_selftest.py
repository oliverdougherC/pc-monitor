"""Prove the sensor supervisor recovers - without drivers, admin, or waiting.

    .venv\\Scripts\\python tools\\sensors_selftest.py

Everything here is a fake on purpose: backends that raise, hang, or answer
late; a fake psutil; a fake NVML whose driver arrives late and resets once.
Issue #17's contract is exactly the stuff a real-hardware test cannot
schedule - a wedge must not own the control loop, a failed tick must never
re-present old data as live forever, a reset driver must come back without
restarting the app - so it is pinned against fakes that do those things on a
pinned clock. Real driver behaviour on real hardware stays the documented
Windows manual step (`tools/sensor_probe.py`).
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

from app import sensors as sens            # noqa: E402
from app.snapshot import Snapshot          # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def cfg(**over) -> dict:
    """A config with the supervisor's numbers pinned, so the test waits (and
    fails) on its own clock, not on shipped defaults."""
    s = {"tick_timeout_s": 0.05, "stale_grace_s": 30.0, "reopen_after": 3,
         "reopen_backoff_s": 5.0, "reopen_backoff_max_s": 60.0,
         "nvml_retry_s": 0.0}
    s.update(over)
    return {"sensors": s}


class OkBackend:
    """Answers everything, and remembers being closed."""

    def __init__(self, tag: str = "a"):
        self.tag = tag
        self.closed = 0

    def sample(self, snap: Snapshot) -> None:
        snap.cpu.load_pct = 42.0
        snap.cpu.temp_c = 55.0
        snap.ram_used_mb = 1000.0

    def close(self) -> None:
        self.closed += 1


class RaisingBackend(OkBackend):
    def sample(self, snap):
        raise RuntimeError("driver is gone")


class HangingBackend(OkBackend):
    """The wedged native call: it never answers inside the test's budget."""

    def sample(self, snap):
        time.sleep(30)
        snap.cpu.load_pct = 1.0


class HalfDeadBackend(OkBackend):
    """Fills CPU, then raises - the old hub threw the CPU answer away too."""

    def sample(self, snap):
        snap.cpu.load_pct = 10.0
        raise RuntimeError("died mid-sample")


class PartialBackend(OkBackend):
    """The per-group contract: GPU did not answer, everything else did."""

    def sample(self, snap):
        snap.cpu.load_pct = 10.0
        snap.ram_used_mb = 2000.0
        snap.failed = ("gpu",)


class NanBackend(OkBackend):
    def sample(self, snap):
        snap.cpu.load_pct = float("nan")
        snap.cpu.temp_c = float("inf")
        snap.ram_used_mb = float("-inf")


def hub_with(backend, **over):
    return sens.SensorHub(backend, cfg=cfg(**over), want="fake")


def preflight() -> bool:
    """Before the fix the hub was a pass-through with no supervision knobs at
    all; say so once in the house format instead of crashing on case one."""
    try:
        sens.SensorHub(OkBackend(), cfg=cfg(), want="fake")
        return True
    except TypeError:
        check("hub is a supervisor (takes cfg and want)",
              "TypeError: SensorHub() takes 1 positional argument",
              "SensorHub(backend, cfg, want)")
        return False


def case_ok() -> None:
    print("case: a healthy backend passes through, with provenance")
    h = hub_with(OkBackend())
    s = h.tick(now=100.0)
    check("fresh values", (s.cpu.load_pct, s.ram_used_mb), (42.0, 1000.0))
    check("not held, no failures", (s.held, s.failed), (False, ()))
    check("provenance names the backend", s.source, "fake")
    check("the state change is logged once", h.changed_to_log() is not None, True)
    check("and it does not repeat", h.changed_to_log(), None)


def case_hold_and_blank() -> None:
    print("case: a failed tick holds for the grace, then blanks - never re-lives")
    h = hub_with(OkBackend())
    h.tick(now=100.0)
    h.backend = RaisingBackend()
    s = h.tick(now=101.0)
    check("held: last good values, marked as held",
          (s.cpu.load_pct, s.held, s.age_s > 0), (42.0, True, True))
    s = h.tick(now=131.0)                      # past stale_grace_s
    check("past the grace: unavailable, not old data", s.cpu.load_pct, None)
    check("blank is not presented as held", s.held, False)
    check("blank says why", "blind" in s.failed, True)
    # And the history contract main.py enforces: a blank tick pushes None (a
    # gap), a held tick is not pushed at all - never the old number as live.
    from app.history import History
    hist = History(8)
    hist.push("cpu.temp", 55.0)                # the last measured sample
    h2 = hub_with(OkBackend())
    h2.tick(now=100.0)
    h2.backend = RaisingBackend()
    held = h2.tick(now=101.0)
    if not held.held:                          # main's rule: measured samples only
        hist.push("cpu.temp", held.cpu.temp_c)
    blind = h2.tick(now=200.0)
    hist.push("cpu.temp", blind.cpu.temp_c)
    check("history shows one measured point and a gap, not a flat lie",
          hist.series("cpu.temp"), [55.0, None])


def case_bounded() -> None:
    print("case: a stuck native sample is abandoned, not waited on")
    h = hub_with(OkBackend())
    h.tick(now=100.0)                          # establishes a good sample
    h.backend = HangingBackend()               # then the driver wedges
    t0 = time.monotonic()
    s = h.tick(now=101.0)
    waited = time.monotonic() - t0
    check("the tick came back inside its budget", waited < 1.0, True)
    check("and it is the held sample, marked", (s.cpu.load_pct, s.held), (42.0, True))


def case_partial() -> None:
    print("case: one failed metric does not discard the healthy ones")
    h = hub_with(OkBackend())
    h.tick(now=100.0)
    h.backend = PartialBackend()
    s = h.tick(now=101.0)
    check("healthy groups survive the tick", (s.cpu.load_pct, s.ram_used_mb),
          (10.0, 2000.0))
    check("the missing group is a None (a history gap)", s.gpu.temp_c, None)
    check("and is named as failed", "gpu" in s.failed, True)
    check("a partial sample is not counted blind", s.held, False)
    h.backend = HalfDeadBackend()
    s = h.tick(now=102.0)
    check("a mid-sample raise keeps what was measured", s.cpu.load_pct, 10.0)
    check("and says the sample died", "sample" in s.failed, True)


def case_nan() -> None:
    print("case: non-finite readings are rejected before the layout sees them")
    h = hub_with(NanBackend())
    s = h.tick(now=100.0)
    check("NaN becomes None", s.cpu.load_pct, None)
    check("inf becomes None", s.cpu.temp_c, None)
    check("-inf becomes None", s.ram_used_mb, None)


def case_reopen() -> None:
    print("case: persistent failure rebuilds the backend, without an app restart")
    old = RaisingBackend()
    h = hub_with(old)
    made: list[OkBackend] = []

    def fake_make(_cfg, _want):
        b = OkBackend(tag=f"made{len(made) + 1}")
        made.append(b)
        return b

    real = sens._make_backend
    sens._make_backend = fake_make
    try:
        h.tick(now=100.0)                      # failure 1
        h.tick(now=101.0)                      # failure 2
        s = h.tick(now=102.0)                  # failure 3 -> rebuild + retry now
        check("rebuilt once after reopen_after failures", h._rebuilds, 1)
        check("the rebuild answers the same tick", (s.cpu.load_pct, s.held),
              (42.0, False))
        check("the dead backend was closed", old.closed, 1)
        check("the new backend is the made one", bool(made) and h.backend is made[0], True)
    finally:
        sens._make_backend = real


def case_reopen_backoff() -> None:
    print("case: a reopen that fails backs off instead of hammering the driver")
    h = hub_with(RaisingBackend())

    def broken_make(_cfg, _want):
        raise RuntimeError("still gone")

    real = sens._make_backend
    sens._make_backend = broken_make
    try:
        h.tick(now=100.0)
        h.tick(now=101.0)
        h.tick(now=102.0)                      # reopen #1 fails -> wait 5 s
        check("first backoff is the configured one", h._backoff, 10.0)
        s = h.tick(now=104.0)                  # inside the backoff window
        check("stays blind during the backoff", s.cpu.load_pct, None)
        h.tick(now=107.0)                      # reopen #2 fails -> wait 10 s
        check("backoff doubles", h._backoff, 20.0)
        sens._make_backend = lambda c, w: OkBackend()
        s = h.tick(now=127.0)                  # after the window
        check("and it recovers on its own clock", (s.cpu.load_pct, s.held),
              (42.0, False))
    finally:
        sens._make_backend = real


def case_recover() -> None:
    print("case: resume re-acquires the backend even while healthy")
    old = OkBackend(tag="old")
    h = hub_with(old)
    h.tick(now=100.0)
    real = sens._make_backend
    sens._make_backend = lambda c, w: OkBackend(tag="new")
    try:
        h.recover("test-resume", now=101.0)
    finally:
        sens._make_backend = real
    check("the old backend was closed", old.closed, 1)
    check("the fresh backend answers", h.tick(now=101.0).cpu.load_pct, 42.0)
    # A demo backend holds no driver resources; swapping it would only lose
    # the synthetic clock the preview depends on.
    from app.sensors.demo import DemoBackend
    d = sens.SensorHub(DemoBackend({}), cfg=cfg(), want="demo")
    before = d.backend
    d.recover("test-resume")
    check("demo is left alone", d.backend is before, True)


def case_close() -> None:
    print("case: shutdown releases the backend")
    b = OkBackend()
    hub_with(b).close()
    check("close reached the backend", b.closed, 1)


def make_fake_pynvml(state: dict):
    """A stand-in NVML: the driver comes late (1 failed open), then resets."""

    class NVMLError(Exception):
        pass

    def nvmlInit():
        if state["ready_at"] > state["opens"]:
            state["opens"] += 1
            raise NVMLError("driver not ready")
        state["opens"] += 1

    fake = SimpleNamespace(
        NVMLError=NVMLError,
        nvmlInit=nvmlInit,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda i: f"handle{i}",
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=77.0),
        nvmlDeviceGetTemperature=lambda h, t: 61.0,
        nvmlDeviceGetClockInfo=lambda h, c: 2400.0,
        nvmlDeviceGetPowerUsage=lambda h: 300_000.0,
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(used=8e9, total=32e9),
        nvmlShutdown=lambda: state.__setitem__("shutdown", state.get("shutdown", 0) + 1),
        NVML_TEMPERATURE_GPU=0, NVML_CLOCK_GRAPHICS=0,
    )
    return fake


def fake_psutil(disk_raises: bool = False):
    def disk_io_counters():
        if disk_raises:
            raise OSError("the disk counter died")
        return SimpleNamespace(read_bytes=0, write_bytes=0)

    return SimpleNamespace(
        cpu_percent=lambda interval=None: 33.0,
        cpu_times_percent=lambda interval=None: None,
        cpu_freq=lambda: SimpleNamespace(current=3000.0),
        virtual_memory=lambda: SimpleNamespace(total=64e9, available=48e9),
        net_io_counters=lambda: SimpleNamespace(bytes_recv=0, bytes_sent=0),
        disk_io_counters=disk_io_counters,
    )


def case_fallback_backend() -> None:
    print("case: the real fallback backend - late driver, dead counter, driver reset")
    import app.sensors.fallback as fb
    from app.sensors.fallback import FallbackBackend

    had = "pynvml" in sys.modules
    old_pynvml = sys.modules.get("pynvml")
    real_psutil = fb.psutil
    # ready_at: the first nvmlInit fails (driver still loading); reset_on: the
    # second utilization call errors (a driver reset) and the third, after the
    # handle has been re-acquired, answers again.
    state = {"ready_at": 1, "opens": 0, "utils": 0, "reset_on": 2}
    fake = make_fake_pynvml(state)

    def util(h):
        state["utils"] += 1
        if state["reset_on"] == state["utils"]:
            raise fake.NVMLError("driver reset")
        return SimpleNamespace(gpu=77.0)

    fake.nvmlDeviceGetUtilizationRates = util
    sys.modules["pynvml"] = fake
    fb.psutil = fake_psutil()
    try:
        b = FallbackBackend(cfg())
        h = sens.SensorHub(b, cfg=cfg(), want="fallback")
        s = h.tick(now=100.0)
        check("late driver: host metrics survive", (s.cpu.load_pct, s.ram_used_mb),
              (33.0, 16000.0))
        check("GPU not ready is unavailable, not a failure",
              (s.gpu.load_pct, "gpu" in s.failed), (None, False))
        s = h.tick(now=101.0)                  # the driver is ready now
        check("GPU answers once the driver is up", s.gpu.load_pct, 77.0)
        s = h.tick(now=102.0)                  # driver resets mid-call
        check("the reset tick reports GPU as failed", "gpu" in s.failed, True)
        check("and healthy groups still answer", s.cpu.load_pct, 33.0)
        s = h.tick(now=103.0)                  # handle re-acquired
        check("GPU comes back without an app restart", s.gpu.load_pct, 77.0)
        b.close()
        check("close shut NVML down", state.get("shutdown"), 1)
    finally:
        fb.psutil = real_psutil
        if had:
            sys.modules["pynvml"] = old_pynvml
        else:
            sys.modules.pop("pynvml", None)

    # A dead psutil counter costs only its own group.
    state2 = {"ready_at": 0, "opens": 0, "utils": 0, "reset_on": -1}
    sys.modules["pynvml"] = make_fake_pynvml(state2)
    fb.psutil = fake_psutil(disk_raises=True)
    try:
        hub = sens.SensorHub(FallbackBackend(cfg()), cfg=cfg(), want="fallback")
        s = hub.tick(now=1.0)
        check("a dead disk counter is a named gap", s.failed, ("disk",))
        check("disk values are None", (s.disk_read_bps, s.disk_write_bps), (None, None))
        # Reconciled during integration (#17 x #18): this check used to demand
        # `net_down_bps is not None` on the very first tick, which was true of the
        # backend that primed its counter baseline in `__init__`. #18 removed that
        # priming on purpose - a constructor baseline divides whatever the machine
        # did during start-up by the milliseconds since `__init__`, which is where
        # the absurd first-second figures came from - and its own suite pins that
        # the first valid sample re-primes and says nothing. What this case is
        # about is *isolation*: net must survive disk's death as a group and be
        # measuring again once its own baseline exists. Same claim, with the
        # re-priming tick accounted for.
        check("cpu/ram survived", (s.cpu.load_pct is not None,
                                   s.ram_used_mb is not None), (True, True))
        check("net survived as a group (not named failed)", "net" in s.failed, False)
        # The backend's rate is measured on its own monotonic clock, not the hub's
        # injected one, and #18 removed the old `max(dt, 1e-6)` clamp: two samples
        # taken in the same instant genuinely have no measurable interval, and the
        # honest answer for them is "no number". Let a real interval elapse before
        # asking net to measure again.
        time.sleep(0.02)
        s = hub.tick(now=2.0)
        check("net is measuring again the tick after",
              (s.net_down_bps is not None, "net" in s.failed), (True, False))
        check("disk is still the only named gap", s.failed, ("disk",))
    finally:
        fb.psutil = real_psutil
        sys.modules.pop("pynvml", None)
        if had:
            sys.modules["pynvml"] = old_pynvml


def main() -> int:
    if not preflight():
        print()
        print("SELFTEST FAILED" + (f": {fails}" if fails else ""))
        return 1
    for fn in (case_ok, case_hold_and_blank, case_bounded, case_partial, case_nan,
               case_reopen, case_reopen_backoff, case_recover, case_close,
               case_fallback_backend):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
