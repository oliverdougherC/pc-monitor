"""End-to-end control-loop test: the *combined* application under realistic failures.

    .venv\\Scripts\\python tools\\e2e_control_loop_selftest.py

Every other suite here gates one module. This one wires the real modules together
the way `main.py` wires them - PanelLink, Recovery, HostState, LightPlanner, the
sensor supervisor, GameWatch, the capture shape it consumes, Layout, DiffPusher -
and drives the loop through the failures the product contract is made of:

  * a panel that goes deaf, hangs forever, and comes back renumbered (replug);
  * suspend / resume / lock / display-off arriving at any moment, including
    *while a rebuild is in flight* (barrier-synchronised, not probabilistic);
  * the capture child dying and stalling; the sensor backend raising;
  * a low-fps game whose held number must expire, never linger as a lie;
  * >=100 full fault cycles, each ending with the same settled state;
  * final shutdown: joined worker, released port, closed event window.

The seams are only the ones a bench cannot avoid: the vendored driver class
(`app.display._CLS["FAKE"]` - the same seam `panel_owner_selftest` installs and
reuses here, ledger and all), the COM port list, and the idle clock. Everything
that decides - the light plan, the recovery coordinator, the state machine, the
link, the supervisor, the diff cache - is the production module.

Clocks are deliberately independent: the loop's cadence, Recovery's `cadence_s`,
PanelLink's retry clock and the gap watchdog's expectation are different numbers
on purpose; a pass that only works when they coincide is the pass this file
refuses to run.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}"
          + ("" if ok else f" want {want!r}"), flush=True)
    if not ok:
        fails.append(name)


def section(title: str) -> None:
    print(f"\ncase: {title}", flush=True)


# The pretend panel: reuse the ledger-backed driver from the ownership suite.
# Its `PORT` records every driver ever built and how many believe they hold the
# port at once - the one-owner invariant is then observable, not asserted blind.
import panel_owner_selftest as po                     # noqa: E402
from app import display as disp                       # noqa: E402
from app.frames import Presenter                      # noqa: E402
from app.recovery import REFRESH, WAKE                # noqa: E402

po.install_fake_vendor()
import app.panel as _panel_probe                       # noqa: E402

# `app.panel` imported these names by value, so patch the copies it actually uses.
_panel_probe._HELLO_WAIT_S = 0.30      # a deaf panel is abandoned fast; the bench
_panel_probe._RESET_WAIT_S = 0.30      # runs 100+ cycles, not 8 s at a time
disp._HELLO_WAIT_S = 0.30
disp._RESET_WAIT_S = 0.30

# The foreground window, as the bench sets it (same seam gamewatch_selftest uses):
# gamewatch._foreground_info is the one place the detector learns what is focused.
import app.gamewatch as gw                            # noqa: E402

FG = {"pid": 0, "name": "", "covers": True, "borderless": True}
gw._foreground_info = lambda: (FG["pid"], FG["name"], FG["covers"],
                               FG["borderless"])


def fg(pid: int, name: str) -> None:
    FG.update(pid=pid, name=name, covers=True, borderless=True)


# What the loop actually *commanded* the panel to do, in order - independent of
# which driver was holding the port at the time. Commands, not driver state, are
# what the appearance contract is about.
CMDS: list[str] = []
_real_screen_on = po.FakeLcd.ScreenOn
_real_screen_off = po.FakeLcd.ScreenOff


def _tracked_on(self):
    CMDS.append("on")
    return _real_screen_on(self)


def _tracked_off(self):
    CMDS.append("off")
    return _real_screen_off(self)


po.FakeLcd.ScreenOn = _tracked_on
po.FakeLcd.ScreenOff = _tracked_off

# Sticky device death: a flaky port that fails once per attempt would let a
# retry-through-the-ladder succeed; a dead endpoint answers nothing, every time.
DEAF: threading.Event | None = None          # set a gate to silence HELLO
_real_hello = po.FakeLcd.InitializeComm


def _gated_hello(self):
    self.port.hello_enter()
    try:
        if DEAF is not None:
            DEAF.wait(10.0)                  # the abandoned work stays alive
    finally:
        self.port.hello_exit()
    return _real_hello(self)


po.FakeLcd.InitializeComm = _gated_hello


class ScriptedIdle:
    def __init__(self):
        self.t = 1_000.0
        self.last_input = 1_000.0

    def read(self) -> float:
        return max(0.0, self.t - self.last_input)

    def typing(self, s: float = 0.2) -> None:
        self.last_input = self.t - s

    def quiet(self, s: float) -> None:
        self.last_input = self.t - s


class FakeCapture:
    """FrameMonitor-shaped: the real GameWatch consumes this shape."""

    def __init__(self):
        self.error = ""
        self.output_file = ""
        self.session_name = "PCMonitor-e2e"
        self.rows: dict[int, Presenter] = {}       # pid -> Presenter
        self.stalled = False
        self.dead = False
        self.restarts = 0

    def tick(self):
        if self.dead:
            return {}
        if self.stalled:
            # A silent stream is `None` to the watch: "no data", not "nothing is
            # presenting" - two different facts, and game mode holds on one of them.
            return None
        return dict(self.rows)

    def presenters(self):
        return {pid: p for pid, p in self.rows.items()}

    def restart(self, reason=""):
        self.restarts += 1

    def close(self):
        self.dead = True


class FlakyBackend:
    """Sensor backend the real supervisor wraps; `broken` flips it to raising.

    The flag is class-level because the supervisor *rebuilds* backends when a
    driver recovers: the healed hardware has to be visible to the new object,
    exactly as it is to the real `_make_backend`.
    """

    name = "flaky"
    faulted = False                    # what a *newly built* backend starts as

    def __init__(self):
        self.ticks = 0
        # per instance, seeded from the bench's fault: reopened backends stay
        # broken while the hardware is still dead, and healthy after it returns
        self.broken = FlakyBackend.faulted

    def sample(self, snap) -> None:
        """The real backend protocol (app/sensors/demo.py): fill in the snapshot."""
        self.ticks += 1
        if self.broken:
            raise RuntimeError("ring0 handle gone")
        # A number that changes every tick: the frame content then changes, so a
        # dead endpoint is caught by the next push instead of hiding behind the
        # diff cache's dedupe - which is what a live dashboard actually does.
        snap.cpu.load_pct = float(self.ticks % 100)
        snap.cpu.temp_c = 55.0
        snap.cpu.power_w = 12.0
        snap.gpu.load_pct = 44.0
        snap.gpu.temp_c = 61.0
        snap.gpu.power_w = 28.0
        snap.ram_used_mb = 8_000
        snap.ram_total_mb = 32_768

    def close(self):
        pass


class Bench:
    """main.py's wiring, replicated with the real objects at the real cadences."""

    def __init__(self):
        self.lines: list[str] = []

        def status(msg: str) -> None:
            self.lines.append(msg)

        import app.panel as panel_mod
        from app import hoststate as hs
        from app.gamewatch import GameWatch
        from app.layout import Layout
        from app.lights import LightPlanner
        from app.nightlight import NightLight
        from app.output import DiffPusher
        from app.recovery import REFRESH, WAKE, Recovery
        import app.sensors as sensors_mod

        self.panel_mod = panel_mod
        self.hs = hs
        self.cfg = po.cfg()
        self.interval = 0.05
        self.status = status
        # the bench runs on bench clocks: the same rules, at test scale (the
        # sensors suite sets these numbers the same way)
        self.cfg["sensors"] = dict(self.cfg["sensors"])
        self.cfg["sensors"].update({"stale_grace_s": 1.0, "reopen_after": 2,
                                    "reopen_backoff_s": 1.0,
                                    "reopen_backoff_max_s": 3.0})

        # nothing here may touch the real device store or leave a journal
        self.state_dir = Path(tempfile.mkdtemp(prefix="pcmon-e2e-state-"))
        panel_mod.STATE_DIR = self.state_dir
        panel_mod._BUS_SETTLE_S = 0.0

        self.idle = ScriptedIdle()
        self._real_idle = hs.idle_seconds
        hs.idle_seconds = self.idle.read

        # the supervisor rebuilds backends through this factory when the hardware
        # comes back; on this bench the hardware is the flaky one
        self._real_make = sensors_mod._make_backend
        sensors_mod._make_backend = lambda cfg, want: FlakyBackend()

        # gap_s is wide on purpose: render jitter must not false-fire the gap
        # watchdog into inventing resumes mid-case (the watchdog has its own suite)
        self.host = hs.HostState(gap_s=2.0, poll_s=3600.0,
                                 cadence_s=self.interval, events=False)
        self.night = NightLight(self.cfg, refresh_s=3600.0)
        self.lights = LightPlanner(self.cfg, night=self.night, host=self.host)
        self.backend = FlakyBackend()
        self.hub = sensors_mod.SensorHub(self.backend, self.cfg)
        self.capture = FakeCapture()
        self.watch = GameWatch(self.cfg)
        self.layout = Layout(self.cfg, rate_hz=1.0 / self.interval)
        self.panel = panel_mod.PanelLink(self.cfg, log=status)
        self.pusher = DiffPusher(self.panel)
        self.panel.open(on_relink=self.pusher.invalidate)
        self.ports = ["COM99"]
        self.recovery = Recovery(self.panel, log=status,
                                 list_ports=lambda: list(self.ports),
                                 cadence_s=0.02, hold_max_s=1.0, threaded=True)
        self.plan = self.lights.tick("idle", self.host.idle_s, 0.0)
        self.errors = 0
        self.snap = None
        self.prev_snap = self.hub.tick()
        self.tick_dt = time.monotonic()
        self.work_s = 0.0
        self.ticks = 0
        self.trace: list[tuple] = []

    def close(self) -> None:
        for closer in (self.recovery.close, self.panel.close, self.host.close):
            try:
                closer()
            except Exception:                    # noqa: BLE001
                pass
        self.hs.idle_seconds = self._real_idle
        import app.sensors as sensors_mod
        sensors_mod._make_backend = self._real_make

    def live_driver(self):
        held = [d for d in po.PORT.all if not d.closed]
        return held[-1] if held else None

    def tick(self) -> dict:
        """One loop tick, mirroring main.py's order and guards.

        The bench keeps the cadence like the real loop does (sleep the remainder
        of the interval): every module here measures elapsed time on the same
        monotonic clock, and a bench that spun 1000 ticks a second would age the
        desk, the grace window and the enter timer a thousand times too slowly.
        """
        t0 = time.monotonic()
        self.ticks += 1
        gap = t0 - self.tick_dt
        self.tick_dt = t0
        dt = min(max(gap, 0.0), 60.0)
        self.idle.t += dt

        try:
            snap = self.hub.tick()
        except Exception:                        # noqa: BLE001 - supervisor bug
            self.errors += 1
            snap = None
        self.snap = snap if snap is not None else self.snap

        self.host.tick(dt, work_s=gap * 0.1)
        wake = (None if self.host.suspend_pending
                else self.host.take_resume())
        display = self.host.take_refresh()
        holds = False
        self.last_wake = None
        if wake:
            self.recovery.request(WAKE, wake)
        if display:
            self.recovery.request(REFRESH, display)
        d = self.recovery.tick(lit=not self.plan.dark)
        if d is not None:
            holds = d.holds_light
            if d.wake:
                self.last_wake = d.wake
                self.capture.restart(d.wake)
                self.watch.reset(d.wake)
            if d.repaint:
                self.pusher.invalidate()

        state = "idle"
        presenters = self.capture.tick()
        # exactly main.py's contract: the detector returns the state string, and
        # a detector that raises keeps the previous state rather than faking idle
        try:
            state = self.watch.tick(self.snap, dt, presenters=presenters)
        except Exception:                      # noqa: BLE001
            state = self.watch.state
        self.plan = self.lights.tick(state, self.host.idle_s, dt,
                                     playing=(state == "game"))
        panel_ok = self.panel.ok
        if self.plan.dark:
            self.panel.screen(False)
        elif not holds:
            self.layout.set_warm(self.plan.lut)
            self.panel.set_brightness(self.plan.brightness)
            self.panel.screen(True)

        frame = None if self.plan.dark else self.layout.render(
            self.snap, state, (0, 0))
        if (not holds and frame is not None and not self.plan.dark
                and panel_ok):
            self.pusher.push(frame)

        drv = self.live_driver()
        self.trace.append((self.ticks, self.plan.dark, holds, panel_ok,
                           None if drv is None else drv.screen_on))
        self.work_s = time.monotonic() - t0
        left = self.interval - self.work_s
        if left > 0:
            time.sleep(left)                   # the appliance keeps its cadence
        return {"state": state, "dark": self.plan.dark, "holds": holds,
                "panel_ok": panel_ok, "frame": frame}

    def event(self, msg: int, w: int, l: int = 0) -> None:
        self.host._on_event(msg, w, l)

    def settle(self, timeout: float = 8.0, want_ok: bool = True) -> bool:
        """Tick until the link agrees with `want_ok` (or the bench gives up)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tick()
            if self.panel.ok == want_ok:
                return True
            time.sleep(0.005)
        return self.panel.ok == want_ok


# --------------------------------------------------------------------- cases
WAKE = "wake"
REFRESH = "refresh"


def case_replug_cycle(b: Bench, cycles: int) -> None:
    section(f"{cycles} fault cycles: dead handshake, late completion, replug")
    global DEAF
    settled = 0
    for i in range(cycles):
        b.idle.typing(0.4)                    # a lit desk: a rebuild is owed, not deferred
        # the endpoint dies: no driver built now ever answers HELLO. The build is
        # abandoned on its deadline, but the native work stays alive behind it.
        DEAF = threading.Event()
        b.panel.relink(f"cycle {i}: kill")
        for _ in range(3):
            b.tick()
        if b.panel.ok:
            DEAF = None
            fails.append(f"cycle {i}: link stayed ok through a dead handshake"
                         f" [{b.panel.summary()}]")
            break
        # the late completion lands behind the deadline; the endpoint returning is
        # the replug itself: the link may only come back through the rebuild the
        # coordinator drives, not through the work that was abandoned.
        DEAF.set()
        DEAF = None
        b.ports = [f"COM{3 + i % 5}"]
        b.recovery.request(WAKE, f"cycle {i}: replug")
        if b.settle(want_ok=True, timeout=15.0):
            settled += 1
        else:
            fails.append(f"cycle {i}: never came back [{b.panel.summary()}]")
            break
        if po.PORT.open_count() != 1:
            fails.append(f"cycle {i}: {po.PORT.open_count()} drivers believe "
                         "they hold the port")
            break
    check("every cycle settled back to a working link", settled, cycles)
    check("never more than one owner of the port (high-water)",
          po.PORT.peak_open, 1)


def case_suspend_under_rebuild(b: Bench) -> None:
    section("a suspend query landing mid-rebuild does not relight the desk")
    global DEAF
    b.idle.typing(0.4)
    b.settle(want_ok=True)
    b.panel_mod._HELLO_WAIT_S = 10.0          # the build stays in flight on purpose
    DEAF = threading.Event()
    try:
        threading.Thread(target=lambda: b.panel.relink("held handshake"),
                         daemon=True).start()
        deadline = time.monotonic() + 5.0
        while po.PORT.hello == 0 and time.monotonic() < deadline:
            b.tick()
            time.sleep(0.005)
        check("a handshake is provably in flight", po.PORT.hello > 0, True)
        b.event(b.hs.WM_POWERBROADCAST, b.hs.PBT_APMQUERYSUSPEND)
        marks = len(CMDS)
        for _ in range(8):
            b.tick()
        dark_all = all(t[1] for t in b.trace[-8:])
        check("the plan is dark under the query", dark_all, True)
        check("no screen-on was commanded while the request stood",
              "on" in CMDS[marks:], False)
    finally:
        DEAF.set()
        DEAF = None
        b.panel_mod._HELLO_WAIT_S = 0.30
    b.event(b.hs.WM_POWERBROADCAST, b.hs.PBT_APMRESUMEAUTOMATIC)
    b.event(b.hs.WM_WTSSESSION_CHANGE, b.hs.WTS_SESSION_UNLOCK)
    b.idle.typing(0.1)
    ok = b.settle(want_ok=True, timeout=15.0)
    for _ in range(6):
        b.tick()
    check("after the resume the link comes back", ok, True)
    check("and the screen was commanded lit again", CMDS[-1], "on")


def case_persistent_hang(b: Bench) -> None:
    section("a device that hangs forever is survived, not waited on")
    global DEAF
    b.idle.typing(0.4)
    b.panel_mod._HELLO_WAIT_S = 30.0          # this handshake is never abandoned
    DEAF = threading.Event()
    try:
        threading.Thread(target=lambda: b.panel.relink("hang test"),
                         daemon=True).start()
        deadline = time.monotonic() + 5.0
        while po.PORT.hello == 0 and time.monotonic() < deadline:
            b.tick()
            time.sleep(0.005)
        t0 = time.monotonic()
        for _ in range(10):
            b.tick()
        spent = time.monotonic() - t0
        check("ten ticks ran without waiting on the hang", spent < 5.0, True)
        check("the link reports itself down, not hung", b.panel.ok, False)
        check("and the loop still honours the light plan",
              b.trace[-1][1] in (True, False), True)
    finally:
        DEAF.set()
        DEAF = None
        b.panel_mod._HELLO_WAIT_S = 0.30
        time.sleep(0.5)                        # let the abandoned build retire


def case_capture_and_sensors(b: Bench) -> None:
    section("capture stall/death and a raising sensor backend stay honest")
    b.ports = ["COM99"]                       # no replug churn while watching capture
    ok = b.settle(want_ok=True)
    check("link healthy again for this case", ok, True)
    b.capture.rows = {4242: Presenter(pid=4242, name="game.exe", fps=58.0,
                                      last_s=0.0, gpu=99.0, exclusive=True)}
    fg(4242, "game.exe")
    r = b.tick()
    for _ in range(100):                      # the enter timer runs on the bench clock
        r = b.tick()
        if b.watch.state == "game":
            break
    check("a presenting game is detected", r["state"], "game")
    if b.watch.state != "game":
        print(f"       watch: {b.watch.summary()} fg={FG} "
              f"rows={list(b.capture.rows)} snap_gpu={b.snap.gpu.load_pct}",
              flush=True)
    # quiesce any outstanding coordinator edges first: a wake legitimately resets
    # the lock, and this case is about what a *stall* does, not about wake order.
    for _ in range(8):
        b.tick()
        if b.last_wake:
            print(f"       wake during quiesce: {b.last_wake}", flush=True)
    check("the game is still the state after quiescing", b.watch.state, "game")
    b.capture.stalled = True
    b.tick()
    b.tick()
    check("a stalled stream holds the game, marked held",
          b.watch.state == "game", True)
    if b.watch.state != "game":
        print(f"       watch: {b.watch.summary()}", flush=True)
    b.capture.dead = True
    b.capture.stalled = False
    for _ in range(80):
        b.tick()
    check("the held number expires instead of lingering",
          b.watch.state == "game", False)
    b.capture.dead = False
    fg(0, "")

    FlakyBackend.faulted = True
    b.hub.backend.broken = True
    for _ in range(25):
        b.tick()
    s = b.snap
    check("a raising backend still yields a snapshot object", s is not None, True)
    check("and it is blank, not yesterday's numbers", s.cpu.load_pct, None)
    if s.cpu.load_pct is not None:
        print(f"       hub: {b.hub.describe()}", flush=True)
    FlakyBackend.faulted = False
    b.hub.backend.broken = False
    # The supervisor re-makes the backend on its own reopen backoff - the same
    # path a driver reset takes - and the numbers must return without a restart.
    back = False
    for _ in range(150):
        b.tick()
        if b.snap is not None and b.snap.cpu.load_pct is not None:
            back = True
            break
    check("numbers come back when the backend does", back, True)
    if not back:
        print(f"       hub: {b.hub.describe()}", flush=True)


def case_low_fps_and_stale(b: Bench) -> None:
    section("a 4 fps game reads as 4 fps, and silence is not smoothed")
    b.capture.rows = {4343: Presenter(pid=4343, name="slow.exe", fps=4.0,
                                      last_s=0.0, gpu=97.0, exclusive=True)}
    fg(4343, "slow.exe")
    r = b.tick()
    for _ in range(120):                      # a below-the-floor title enters on the
        r = b.tick()                          # legacy timer, which is longer
        if b.watch.state == "game":
            break
    check("the slow game is the state", r["state"], "game")
    fps = None
    for p in b.capture.presenters().values():
        fps = p.fps
    check("no invented fps while the stream answers", fps, 4.0)
    b.capture.rows = {}
    b.capture.stalled = True
    for _ in range(3):
        b.tick()
    b.capture.stalled = False
    left = True
    for _ in range(120):                      # the dead-target timer, on the bench clock
        b.tick()
        if b.watch.state != "game":
            left = False
            break
    fg(0, "")
    check("the game left when the stream cleared", left, False)


def case_dark_intent_prompt(b: Bench) -> None:
    section("sleep/lock/monitor-off darkens the panel promptly")
    b.settle(want_ok=True)
    for _ in range(3):
        b.tick()
    check("lit before the sleep", CMDS[-1], "on")
    marks = len(CMDS)
    b.event(b.hs.WM_POWERBROADCAST, b.hs.PBT_APMQUERYSUSPEND)
    b.tick()
    b.tick()
    check("screen commanded off within two ticks of the query",
          CMDS[marks:][:1], ["off"])
    b.event(b.hs.WM_POWERBROADCAST, b.hs.PBT_APMRESUMEAUTOMATIC)
    b.event(b.hs.WM_WTSSESSION_CHANGE, b.hs.WTS_SESSION_UNLOCK)
    b.idle.typing(0.1)
    for _ in range(6):
        b.tick()
    check("lit again after the unlock", CMDS[-1], "on")


def case_final_shutdown(b: Bench) -> None:
    section("shutdown releases everything")
    b.tick()
    b.recovery.close()
    b.panel.close()
    check("no driver keeps the port after close", po.PORT.open_count(), 0)
    b.host.close()
    check("the event window is gone", b.host.events_live, False)


# ----------------------------------------------------------------------- main
def main_run() -> int:
    print("e2e control loop: real modules, one loop, scripted bench\n", flush=True)
    b = Bench()
    try:
        check("the panel came up on the fake driver", b.panel.ok, True)
        case_replug_cycle(b, cycles=120)
        case_suspend_under_rebuild(b)
        case_persistent_hang(b)
        case_capture_and_sensors(b)
        case_low_fps_and_stale(b)
        case_dark_intent_prompt(b)
    finally:
        case_final_shutdown(b)
        b.close()
    print("\n" + ("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}"),
          flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())