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


def main() -> int:
    cfg = cfgmod.load(None)
    for fn in (case_enter_fast, case_alt_tab, case_quit, case_video, case_ourselves,
               case_switch, case_legacy):
        fn(cfg)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
