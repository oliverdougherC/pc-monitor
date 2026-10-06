"""Replay fake present-streams and fake focus through the real detector.

    .venv\\Scripts\\python tools\\gamewatch_selftest.py

Requirements 4 and 5 of the "make the frame view reliable" list are behavioural, and
they are exactly the kind that no live spot-check catches, because they only happen
at the moment you tab out. Each case below drives `app/frames.py` (real parser,
real rings) and `app/gamewatch.py` (real state machine) with a stubbed foreground
window and a synthetic stream clock, and asserts what the panel would have shown:

  enter fast      a game that starts in the foreground is game mode in ~1 s,
                  not after the old flat four seconds
  alt-tab holds   tab out to a browser that is itself presenting 60 fps and the
                  frame target does NOT move — the browser never gets the numbers
  quit is quick   the game process disappears → idle in ~3 s, not 25
  video is not a game   a browser presenting 60 fps fullscreen never enters game mode
  switch          a second game starts while the first goes quiet → the lock moves
  hold, don't lie  the game stops presenting while alive: the last measurement is
                  kept and marked stale, never replaced by a fabrication
  held below floor a live 20 fps game never oscillates in and out on the 24 fps
                  entry floor; a loading gap does not count against it either;
                  `present_detection: false` holds the same contract as entering;
                  a configured `game.processes` name enters and holds without any
                  ETW at all; a configured name holds with the window heuristic
                  *off* too, so the panel does not flip idle↔game every
                  `exit_after_s`; and a pid whose identity no longer matches the
                  lock cannot inherit it, whether or not a present stream exists

Pids are chosen so the liveness rule can be exercised both ways: the alive game is
this test process (guaranteed to exist), the dead one is a pid nobody can own.
"""
from __future__ import annotations

import io
import os
import sys
import time

sys.path.insert(0, ".")          # our tree first: vendor has its own main.py
from app import config as cfgmod          # noqa: E402
from app import gamewatch                 # noqa: E402
from app.frames import FrameMonitor       # noqa: E402

sys.stdout.reconfigure(errors="replace")

HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,"
          "PresentFlags,AllowsTearing,PresentMode,TimeInSeconds,"
          "MsBetweenSimulationStart,MsBetweenPresents,MsBetweenDisplayChange,"
          "MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,"
          "CPUStartQPCTimeInMs,MsBetweenAppStart,MsCPUBusy,MsCPUWait,"
          "MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,MsAnimationError,"
          "AnimationTime,MsAllInputToPhotonLatency,MsClickToPhotonLatency")
COLS = HEADER.split(",")
ALIVE = os.getppid()     # a pid that exists ("alt-tabbed, still running") and is not us
DEAD = 999_999          # a pid that cannot: "the game exited"

# The foreground window, as the test sets it. gamewatch._foreground_info is the one
# call in the loop that reaches outside the process, so it is the one thing replaced.
FG = {"pid": None, "name": None, "covers": False, "borderless": False}
fails: list[str] = []


def fg(pid, name, covers=True, borderless=True) -> None:
    FG.update(pid=pid, name=name, covers=covers, borderless=borderless)


gamewatch._foreground_info = lambda: (FG["pid"], FG["name"], FG["covers"], FG["borderless"])


def check(name: str, got, want) -> None:
    ok = (abs(got - want) <= max(0.02 * abs(want), 0.02)) if isinstance(got, float) \
        else got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}"
          + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


class Stream:
    """A virtual stream clock that rows can be appended to, per source."""

    def __init__(self, m: FrameMonitor):
        self.m = m
        self.t = 1_000_000.0          # qpc ms origin, arbitrary

    def present(self, pid: int, name: str, seconds: float, fps: float,
                gpu_busy_pct: float = 90.0, mode: str = "Hardware Composed: "
                                                        "Independent Flip") -> None:
        """Feed `seconds` of presents for one process at `fps`."""
        step = 1000.0 / fps
        n = max(1, int(seconds * fps))
        lines = [HEADER]
        for _ in range(n):
            self.t += step
            busy = gpu_busy_pct / 100.0 * step
            vals = []
            for c in COLS:
                v = "NA"
                if c == "Application":
                    v = name
                elif c == "ProcessID":
                    v = str(pid)
                elif c == "SwapChainAddress":
                    v = f"0x{pid:x}000"
                elif c == "PresentRuntime":
                    v = "DX12"
                elif c in ("SyncInterval", "PresentFlags", "AllowsTearing"):
                    v = "0"
                elif c == "PresentMode":
                    v = mode
                elif c == "CPUStartQPCTimeInMs":
                    v = f"{self.t:.4f}"
                elif c in ("MsBetweenPresents", "MsBetweenDisplayChange", "MsGPUTime"):
                    v = f"{step:.4f}"
                elif c == "MsGPUBusy":
                    v = f"{busy:.4f}"
                elif c == "TimeInSeconds":
                    v = f"{self.t / 1000.0:.6f}"
                vals.append(v)
            lines.append(",".join(vals))
        blob = ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")
        self.m._read_stream(_Stub(blob))

    def idle(self, seconds: float) -> None:
        """Advance the clock: the compositor keeps presenting, the game does not."""
        self.present(4, "dwm.exe", seconds, 60.0, gpu_busy_pct=5.0,
                     mode="Hardware: Legacy Flip")


class _Stub:
    def __init__(self, blob: bytes):
        self.stdout = io.BytesIO(blob)
        self.pid = -1

    def wait(self) -> int:
        return 0

    def poll(self) -> int | None:
        return 0


def new_pair(cfg: dict) -> tuple[FrameMonitor, Stream, gamewatch.GameWatch]:
    cfg = cfgmod.load(None) if cfg is None else cfg
    c = dict(cfg)
    c["frames"] = dict(cfg["frames"])
    c["frames"]["path"] = "does-not-exist.exe"    # keep __init__ from spawning
    m = FrameMonitor(c, role="selftest")
    m.error = None
    m._spawned = time.monotonic() - 30.0
    FG.update(pid=None, name=None, covers=False, borderless=False)
    return m, Stream(m), gamewatch.GameWatch(c)


def tick(w: gamewatch.GameWatch, m: FrameMonitor, dt: float = 1.0, snap=None,
         steam=None) -> str:
    # None, not {}: "no capture" and "nothing is presenting" are different facts to
    # the state machine, and main.py passes it the same way.
    return w.tick(snap or _Snap(), dt, m.presenters() if m.ok else None, steam)


class _Gpu:
    load_pct = 42.0


class _Snap:
    gpu = _Gpu()


def case_enter_fast(cfg) -> None:
    print("case: a game starts in the foreground")
    m, s, w = new_pair(cfg)
    fg(ALIVE, "re9.exe")
    s.present(ALIVE, "re9.exe", 5.0, 118.0, gpu_busy_pct=99.0)
    states = [tick(w, m) for _ in range(4)]
    print(f"  ticks → {states}")
    check("enters game mode", states[-1], "game")
    check("within 2 s (was 4)", states.index("game") + 1, 2)
    check("target is the game", w.game_pid, ALIVE)
    check("evidence names the reason", "foreground+presenting" in w.evidence
          or "held" in w.evidence, True)
    st = m.stats(ALIVE)
    ok = st is not None and 117 <= float(st.fps) <= 120
    print(f"  {'ok  ' if ok else 'FAIL'} fps measured: got {None if not st else st.fps!r} "
          f"want ~118")
    if not ok:
        fails.append("fps measured")
    check("gpu busy measured", round(float(st.gpu_pct)), 99)
    check("not stale", st.stale, False)


def case_alt_tab(cfg) -> None:
    print("case: tab out to a browser that is itself presenting")
    m, s, w = new_pair(cfg)
    fg(ALIVE, "re9.exe")
    s.present(ALIVE, "re9.exe", 5.0, 118.0, gpu_busy_pct=99.0)
    for _ in range(3):
        tick(w, m)
    check("in game mode", w.state, "game")

    # Alt-tab: chrome is now foreground and presenting 60 fps of scrolling, the game
    # keeps rendering. This is the exact case that used to steal the frame view.
    fg(4321, "chrome.exe")
    for _ in range(6):
        s.present(4321, "chrome.exe", 1.0, 60.0, gpu_busy_pct=15.0,
                  mode="Composed: Flip")
        s.present(ALIVE, "re9.exe", 1.0, 118.0, gpu_busy_pct=99.0)
        tick(w, m)
    check("still game mode", w.state, "game")
    check("still the game's pid", w.game_pid, ALIVE)
    st = m.stats(w.game_pid)
    check("numbers are still the game's", round(float(st.fps)), 118)
    check("the browser never became the target", w.game_name, "re9.exe")

    # ... and the game goes quiet while we stay on the browser: hold, dim, do not lie.
    for _ in range(5):
        s.idle(1.0)
        s.present(4321, "chrome.exe", 1.0, 60.0, gpu_busy_pct=15.0,
                  mode="Composed: Flip")
        tick(w, m)
    check("still game mode while it is alive", w.state, "game")
    st = m.stats(ALIVE)
    if st is None:
        fails.append("held stats")
        print("  FAIL held stats: stats() returned None instead of holding")
    else:
        check("held value kept", round(float(st.fps)), 118)
        check("marked stale", st.stale, True)
        check("age reported", st.age_s > 1.0, True)


def case_quit(cfg) -> None:
    print("case: the game exits")
    m, s, w = new_pair(cfg)
    fg(DEAD, "re9.exe")
    s.present(DEAD, "re9.exe", 5.0, 90.0, gpu_busy_pct=95.0)
    for _ in range(3):
        tick(w, m)
    check("in game mode", w.state, "game")
    ticks = 0
    while w.state == "game" and ticks < 30:
        s.idle(1.0)
        tick(w, m)
        ticks += 1
    check("idle again", w.state, "idle")
    check("target released", w.game_pid, None)
    # Two windows add up here, both deliberate: a pid is not "gone" until it has been
    # absent from the stream for the 2 s liveness window (a one-frame stall is not an
    # exit), then `dead_exit_s` confirms the process really went. 5 ticks total —
    # the regression this catches is the old 25 s exit timer.
    check("gone within liveness + dead_exit", 4 <= ticks <= 6, True)
    print(f"  evidence: {w.evidence}")


def case_video(cfg) -> None:
    print("case: a browser at 60 fps is not a game")
    m, s, w = new_pair(cfg)
    fg(4321, "chrome.exe")
    for _ in range(10):
        s.present(4321, "chrome.exe", 1.0, 60.0, gpu_busy_pct=20.0,
                  mode="Composed: Flip")
        tick(w, m)
    check("stays idle", w.state, "idle")
    check("no target", w.game_pid, None)
    print(f"  evidence: {w.evidence}")


def case_ourselves(cfg) -> None:
    print("case: our own preview window is not a game")
    m, s, w = new_pair(cfg)
    me = os.getpid()
    fg(me, "pythonw.exe")
    for _ in range(10):
        # Exactly what a `revision: SIMU` run looks like to the capture: our pid, in
        # the foreground, presenting at 60 fps. It must never become the frame target.
        s.present(me, "pythonw.exe", 1.0, 60.0, gpu_busy_pct=70.0)
        tick(w, m)
    check("stays idle", w.state, "idle")
    check("no target", w.game_pid, None)
    print(f"  evidence: {w.evidence}")


def case_switch(cfg) -> None:
    print("case: a second game takes over from a quiet one")
    m, s, w = new_pair(cfg)
    fg(ALIVE, "game_a.exe")
    s.present(ALIVE, "game_a.exe", 5.0, 60.0, gpu_busy_pct=90.0)
    for _ in range(3):
        tick(w, m)
    check("game A held", w.game_pid, ALIVE)

    other = 5150
    fg(other, "game_b.exe")
    for _ in range(8):
        s.idle(1.0)
        s.present(other, "game_b.exe", 1.0, 144.0, gpu_busy_pct=98.0)
        tick(w, m)
    check("still game mode", w.state, "game")
    check("lock moved to B", w.game_pid, other)
    check("switch counted", w.switches >= 2, True)


def case_legacy(cfg) -> None:
    print("case: no present stream at all (presentmon unavailable)")
    m, s, w = new_pair(cfg)
    m.ok = False                       # frames unavailable
    fg(ALIVE, "oldgame.exe")
    seen = None
    for _ in range(8):
        tick(w, m)
        if w.state == "game" and seen is None:
            seen = w.evidence
    check("window heuristic enters", w.state, "game")
    check("target is the foreground pid", w.game_pid, ALIVE)
    check("entered on the heuristic", "window heuristic" in (seen or ""), True)

    # With no stream, the window is the only evidence there is: alt-tab away and it
    # should hold for a while, then let go.
    fg(7, "explorer.exe", covers=False, borderless=False)
    n = 0
    while w.state == "game" and n < 40:
        tick(w, m)
        n += 1
    check("eventually lets go", w.state, "idle")
    check("after exit_after_s", 20 <= n <= 26, True)


def case_low_fps_holds(cfg) -> None:
    print("case: a live 20 fps game below the present floor must not oscillate")
    # Entering through the fullscreen/GPU fallback is fine; the bug was that once
    # locked, only `presenters is None` re-applied that fallback — a healthy capture
    # that simply never lists a sub-floor game read as "not presenting", so the lock
    # released every exit_after_s and re-entered four seconds later, forever.
    m, s, w = new_pair(cfg)
    fg(ALIVE, "lightgame.exe")
    states = []
    for _ in range(40):
        s.present(ALIVE, "lightgame.exe", 1.0, 20.0, gpu_busy_pct=90.0)
        states.append(tick(w, m))
    check("in game mode", states[-1], "game")
    check("never left across 40 s of live play", set(states[-10:]), {"game"})
    check("entered once, no oscillation", w.switches, 1)
    check("it really is below the present floor", m.presenters().get(ALIVE), None)
    st = m.stats(ALIVE)
    check("stats stay live", st is not None and not st.stale, True)
    check("stats follow it at ~20 fps", round(float(st.fps)), 20)


def case_loading_gap(cfg) -> None:
    print("case: a loading screen below the floor is not an exit")
    m, s, w = new_pair(cfg)
    fg(ALIVE, "lightgame.exe")
    for _ in range(6):
        s.present(ALIVE, "lightgame.exe", 1.0, 20.0, gpu_busy_pct=90.0)
        tick(w, m)
    check("in game mode", w.state, "game")
    # 10 s of nothing rendered and focus elsewhere — a real load behind a splash.
    fg(7, "explorer.exe", covers=False, borderless=False)
    for _ in range(10):
        s.idle(1.0)
        tick(w, m)
    check("holds through the gap", w.state, "game")
    # Back to sub-floor play: the quiet clock must reset, not carry over into
    # exit_after_s and release a game that never stopped being the game.
    fg(ALIVE, "lightgame.exe")
    for _ in range(20):
        s.present(ALIVE, "lightgame.exe", 1.0, 20.0, gpu_busy_pct=90.0)
        tick(w, m)
    check("never released across gap + sub-floor play", w.state, "game")
    check("entered once, switched none", w.switches, 1)


def case_present_detection_off(cfg) -> None:
    print("case: present_detection off is honored in the held state too")
    c = dict(cfg)
    c["game"] = dict(cfg["game"])
    c["game"]["present_detection"] = False
    m, s, w = new_pair(c)
    fg(ALIVE, "re9.exe")
    for _ in range(6):
        s.present(ALIVE, "re9.exe", 1.0, 118.0, gpu_busy_pct=99.0)
        tick(w, m)
    check("entered on the window heuristic", w.state, "game")
    check("the window is the contract", "window heuristic" in w.evidence, True)
    # Alt-tab: the capture still says the game presents 118 fps, but the user said
    # present-based detection is off — held state must not contradict idle state by
    # using it anyway, or "off" only means "not for entry".
    fg(4321, "chrome.exe", covers=False, borderless=False)
    n = 0
    while w.state == "game" and n < 40:
        s.present(ALIVE, "re9.exe", 1.0, 118.0, gpu_busy_pct=99.0)
        tick(w, m)
        n += 1
    check("lets go on the window clock", w.state, "idle")
    check("after exit_after_s", 24 <= n <= 27, True)


def case_configured(cfg) -> None:
    print("case: a configured executable without usable ETW")
    # `game.processes` is documented as always triggering game mode — it is the
    # user's own answer, so it must not wait for the exe to clear the present floor
    # or for a capture to exist at all.
    c = dict(cfg)
    c["game"] = dict(cfg["game"])
    c["game"]["processes"] = ["mygame.exe"]
    m, s, w = new_pair(c)
    m.ok = False                                  # no present stream at all
    fg(ALIVE, "mygame.exe", covers=False, borderless=False)   # windowed, not fullscreen
    states = [tick(w, m) for _ in range(4)]
    check("enters on the name alone", states[-1], "game")
    entered_at = (states.index("game") + 1) if "game" in states else -1
    check("on the strong clock, not the flat four", 1 <= entered_at <= 3, True)
    check("target is the configured process", w.game_pid, ALIVE)
    # Configured is not "forever": tab away and the intentional exit hysteresis
    # still applies — the name qualifies the *foreground* process.
    fg(7, "explorer.exe", covers=False, borderless=False)
    n = 0
    while w.state == "game" and n < 40:
        tick(w, m)
        n += 1
    check("lets go on the window clock", w.state, "idle")
    check("after exit_after_s", 24 <= n <= 27, True)
    # The explicit veto lists outrank the configured name.
    c2 = dict(cfg)
    c2["game"] = dict(cfg["game"])
    c2["game"]["processes"] = ["discord.exe"]
    m2, s2, w2 = new_pair(c2)
    m2.ok = False
    fg(4321, "discord.exe")
    for _ in range(8):
        tick(w2, m2)
    check("a non-game name in game.processes is still vetoed", w2.state, "idle")


def case_pid_reuse(cfg) -> None:
    print("case: a presenting pid whose identity no longer matches must not hold the lock")
    m, s, w = new_pair(cfg)
    fg(ALIVE, "re9.exe")
    for _ in range(3):
        s.present(ALIVE, "re9.exe", 1.0, 118.0, gpu_busy_pct=99.0)
        tick(w, m)
    check("in game mode", w.state, "game")
    check("identity recorded at lock", w._create_time is not None, True)
    # Windows recycles pids. The thing presenting under this pid now was created at
    # a different time than the process we locked onto — the exact shape of reuse —
    # and presenting alone must not inherit the lock.
    w._create_time -= 10_000.0
    n = 0
    while w.state == "game" and n < 10:
        s.present(ALIVE, "re9.exe", 1.0, 118.0, gpu_busy_pct=99.0)
        tick(w, m)
        n += 1
    check("released, not inherited", w.state, "idle")
    check("on the dead-exit clock", 2 <= n <= 5, True)
    check("no target left", w.game_pid, None)


def case_configured_no_heuristic(cfg) -> None:
    print("case: a configured game with fullscreen_heuristic off must not oscillate")
    # Issue #21(a), measured before the fix: with the heuristic off and
    # `present_detection: false` the *entry* rule still promoted the foreground
    # configured process to STRONG, but the held rule counted silence and let go —
    # transitions [(0,'idle'),(1,'game'),(26,'idle'),(28,'game'),(53,'idle'),
    # (55,'game')], switches=3 over 80 ticks. Entry and hold disagreeing about the
    # same fact is the boundary where the panel flickers, so the assertion has to be
    # about the whole run: how many times it switched, and where it ended up.
    c = dict(cfg)
    c["game"] = dict(cfg["game"])
    c["game"]["processes"] = ["mygame.exe"]
    c["game"]["fullscreen_heuristic"] = False
    c["game"]["present_detection"] = False
    m, s, w = new_pair(c)
    m.ok = False                       # no capture: the heuristic was the only other way
    fg(ALIVE, "mygame.exe", covers=False, borderless=False)   # windowed, not fullscreen
    states = [tick(w, m) for _ in range(80)]
    entered_at = (states.index("game") + 1) if "game" in states else -1
    check("enters on the name alone, on the strong clock", 1 <= entered_at <= 3, True)
    check("still game mode 80 s later", states[-1], "game")
    check("never left after entry", set(states[entered_at:]), {"game"})
    check("locked once: no idle↔game oscillation", w.switches, 1)
    check("and it is the configured process", w.game_pid, ALIVE)
    check("the capture really was unavailable", m.ok, False)


def case_pid_reuse_no_capture(cfg) -> None:
    print("case: a reused pid cannot hold the lock without a capture")
    # Issue #21(b): the present-stream path has always checked identity (case above);
    # the two no-capture hold paths did not — measured, after reuse was simulated:
    # eight further ticks still reported `game` under the old pid. Both shapes are
    # exercised, because the no-capture path has two ways to accept a live pid.
    # Shape 1 — the configured name, with the window heuristic off.
    c = dict(cfg)
    c["game"] = dict(cfg["game"])
    c["game"]["processes"] = ["mygame.exe"]
    c["game"]["fullscreen_heuristic"] = False
    c["game"]["present_detection"] = False
    m, s, w = new_pair(c)
    m.ok = False
    fg(ALIVE, "mygame.exe", covers=False, borderless=False)
    for _ in range(4):
        tick(w, m)
    check("configured: in game mode", w.state, "game")
    check("configured: identity recorded at lock", w._create_time is not None, True)
    w._create_time -= 10_000.0        # Windows recycled the pid onto a stranger
    n = 0
    while w.state == "game" and n < 10:
        tick(w, m)
        n += 1
    check("configured: released, not inherited", w.state, "idle")
    check("configured: on the dead-exit clock", 2 <= n <= 5, True)
    check("configured: no target left", w.game_pid, None)

    # Shape 2 — the window heuristic, where `_window_holds` is the one saying yes: the
    # target is still the foreground process and its window still covers the monitor
    # with the GPU busy, so only the identity check can tell that the pid is a stranger.
    c2 = dict(cfg)
    c2["game"] = dict(cfg["game"])
    c2["game"]["present_detection"] = False
    m2, s2, w2 = new_pair(c2)
    m2.ok = False
    fg(ALIVE, "oldgame.exe")
    for _ in range(8):
        tick(w2, m2)
    check("heuristic: entered on the window evidence", w2.state, "game")
    check("heuristic: identity recorded at lock", w2._create_time is not None, True)
    w2._create_time -= 10_000.0
    n = 0
    while w2.state == "game" and n < 10:
        tick(w2, m2)
        n += 1
    check("heuristic: released, not inherited", w2.state, "idle")
    check("heuristic: on the dead-exit clock", 2 <= n <= 5, True)
    check("heuristic: no target left", w2.game_pid, None)


def main() -> int:
    cfg = cfgmod.load(None)
    for fn in (case_enter_fast, case_alt_tab, case_quit, case_video, case_ourselves,
               case_switch, case_legacy, case_low_fps_holds, case_loading_gap,
               case_present_detection_off, case_configured, case_pid_reuse,
               case_configured_no_heuristic, case_pid_reuse_no_capture):
        fn(cfg)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
