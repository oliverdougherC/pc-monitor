"""Replay a missed wake at the seam between main.py's loop and `HostState.tick`.

    .venv\\Scripts\\python tools\\wake_gap_selftest.py

`tools/hoststate_selftest.py` drives the state machine with `tick(1.0)` while the
real loop hands it its elapsed time, so the two have never had to agree about
anything — and the gap watchdog's threshold used to be a multiple of whatever it was
handed. A 30-second suspend then arrived as `gap 30, dt 30`, was asked to outweigh
90, and was pronounced a normal tick. Nothing in the old case could see that, because
nothing in it used the loop's arithmetic.

So this file does not re-implement a flattering version of that arithmetic: it imports
`main.elapsed_dt`, the function the loop itself calls, and drives one turn at a time
with the three numbers the loop actually has — the gap `HostState` measures for
itself, the clamped `dt` for the interval counters, and how much of the window the
loop spent executing (`work_s`).

What has to hold, in both directions:

  * 6, 30, 60 and 120 seconds of gap with no notification of any kind each raise
    exactly one recovery request — that request is what reconnects the COM port and
    restarts the ETW capture, so "one" means the panel comes back and does not come
    back twice;
  * start-up, ordinary jitter, and a deliberately slow panel rebuild raise nothing.
    The last one is the storm: a recovery that takes half a minute used to look like
    the wake it was recovering from, and the app would "recover" from a sleep that
    never happened, on and on (see `app/panel.py`, `start_build`).

The input clock is pinned to unreadable on purpose: the gap watchdog is the fallback
for the case where nothing else can be trusted, including `GetLastInputInfo`. That
also makes this case deterministic, which is why it gates instead of being advisory.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

import main                                             # noqa: E402
from app import config as cfgmod                        # noqa: E402
from app import hoststate as hs                         # noqa: E402
from app.hoststate import PBT_APMQUERYSUSPEND, WM_POWERBROADCAST, HostState  # noqa: E402

# The shipped values, not test-friendly ones: the point of the case is that the
# five-second fallback works as configured, at the configured tick rate.
WAKE_GAP_S = float(cfgmod.DEFAULTS["power"]["wake_gap_s"])
INTERVAL_S = float(cfgmod.DEFAULTS["sensors"]["interval_s"])

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


class Loop:
    """One turn of main.py's loop at a time, on a clock the case controls.

    `prev` is the single source of truth for the window: it is both what the state
    machine remembers as its last tick and what the loop remembers, so the `dt` from
    `main.elapsed_dt` and the gap from `HostState.tick` are two readings of one
    elapsed time rather than two numbers chosen to make a test pass.
    """

    def __init__(self, h: HostState) -> None:
        self.h = h
        self.tick_dt = time.monotonic()

    def turn(self, gap_s: float, work_s: float | None = 0.05) -> float:
        """The loop ran `work_s` of the `gap_s` seconds since its previous tick."""
        now = time.monotonic()
        prev = now - gap_s
        self.tick_dt = prev
        self.h._tick_now = prev
        dt = main.elapsed_dt(self.tick_dt, now)      # the loop's own calculation
        self.tick_dt = now
        self.h.tick(dt, work_s=work_s)
        return dt

    def healthy(self, jitter_s: float = 0.0) -> float:
        """A tick that took about what it was asked to take."""
        return self.turn(INTERVAL_S + jitter_s, work_s=0.05 + jitter_s)


def new_state() -> HostState:
    """As main.py builds it, minus the window: these cases have no notifications."""
    return HostState(gap_s=WAKE_GAP_S, poll_s=3600.0, cadence_s=INTERVAL_S, events=False)


def recoveries(h: HostState) -> list[str]:
    """The recovery requests this tick earns — what `main.py` acts on, once each."""
    out: list[str] = []
    while True:
        r = h.take_resume()
        if r is None:
            return out
        out.append(r)


def case_missed_resumes() -> None:
    print("case: a suspend nobody was told about is still a suspend")
    for gap in (6.0, 30.0, 60.0, 120.0):
        h = new_state()
        loop = Loop(h)
        loop.healthy()                                   # prime, like the real loop
        dt = loop.turn(gap)                              # frozen: no events, no work
        check(f"{gap:.0f}s gap raises exactly one recovery", recoveries(h),
              [f"gap:{gap:.0f}s"])
        check(f"{gap:.0f}s gap is awake again afterwards", h.asleep, False)
        loop.healthy()
        check(f"{gap:.0f}s gap does not repeat next tick", recoveries(h), [])
        h.close()
    # The clamp is real: the 120-second window reaches the loop as 60 seconds of dt,
    # which is exactly the number the old threshold multiplied by three.
    h = new_state()
    loop = Loop(h)
    loop.healthy()
    check("a two-minute gap arrives as a clamped dt", loop.turn(120.0), 60.0)
    check("and is still measured as two minutes", recoveries(h), ["gap:120s"])
    h.close()


def case_wake_after_an_event_suspend() -> None:
    print("case: asleep by event, woken by the gap (no resume message arrived)")
    h = new_state()
    loop = Loop(h)
    loop.healthy()
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    loop.turn(30.0)
    check("one recovery, named for what it followed", recoveries(h),
          ["wake-after-query-suspend"])
    check("and not asleep any more", h.asleep, False)
    h.close()


def case_startup_is_not_a_wake() -> None:
    print("case: a slow start is not a suspend")
    # Between constructing HostState and the loop's first tick sits the panel
    # bring-up, which takes half a minute against a deaf screen. That is a slow
    # start, and announcing `[resume] gap:36s` before anything exists to recover
    # relinks a link that was just opened.
    h = new_state()
    loop = Loop(h)
    loop.turn(36.0, work_s=0.1)
    check("no recovery on the first tick", recoveries(h), [])
    loop.healthy()
    check("nor on the second", recoveries(h), [])
    h.close()


def case_jitter_is_not_a_wake() -> None:
    print("case: an ordinary tick that overran a bit is not a suspend")
    h = new_state()
    loop = Loop(h)
    loop.healthy()
    for jitter in (0.0, 0.4, 1.9, 3.8):
        loop.healthy(jitter)
        check(f"gap {INTERVAL_S + jitter:.1f}s raises nothing", recoveries(h), [])
    # The floor is still the floor: just under `wake_gap_s` of unexplained time is
    # the app being slow, and saying so every tick would be a storm of its own.
    loop.turn(WAKE_GAP_S - 0.1)
    check(f"under the {WAKE_GAP_S:.0f}s floor raises nothing", recoveries(h), [])
    h.close()


def case_slow_recovery_is_not_a_wake() -> None:
    print("case: a slow recovery is not another wake")
    # Once the threshold stopped moving with the gap, anything long looked like a
    # freeze — including the recovery itself. `panel.relink()` is synchronous, so a
    # half-minute bring-up of a deaf panel lands in the next window as elapsed time,
    # and the loop would ask for the same relink again, forever.
    h = new_state()
    loop = Loop(h)
    loop.healthy()
    loop.turn(30.0)
    first = recoveries(h)
    check("the wake is asked for once", first, ["gap:30s"])
    for rebuild in (35.0, 90.0):
        loop.turn(rebuild, work_s=rebuild)     # the loop spent all of it relinking
        check(f"a {rebuild:.0f}s rebuild asks for nothing", recoveries(h), [])
    for _ in range(3):
        loop.healthy()
    check("one recovery for the whole wake", first + recoveries(h), ["gap:30s"])
    h.close()
    # Same shape with no wake before it: a background retry going slow on a live
    # machine must not restart the capture either.
    h = new_state()
    loop = Loop(h)
    loop.healthy()
    loop.turn(40.0, work_s=40.0)
    check("a slow rebuild on its own asks for nothing", recoveries(h), [])
    h.close()


def case_caller_that_reports_nothing() -> None:
    print("case: a caller that cannot account for its gap still gets the fallback")
    # tools/hoststate_probe.py sleeps between its own ticks and reports no work, so
    # the whole gap stands. Without this the watchdog would quietly depend on a
    # caller that bothers to declare itself.
    h = new_state()
    h.tick(main.elapsed_dt(time.monotonic() - INTERVAL_S, time.monotonic()))
    h._tick_now = time.monotonic() - 400.0
    h.tick(main.elapsed_dt(h._tick_now, time.monotonic()))
    check("an unreported gap still raises one recovery", recoveries(h), ["gap:400s"])
    h.close()


def main_run() -> int:
    real = hs.idle_seconds
    hs.idle_seconds = lambda: None          # no input clock: the fallback is alone here
    try:
        for fn in (case_missed_resumes, case_wake_after_an_event_suspend,
                   case_startup_is_not_a_wake, case_jitter_is_not_a_wake,
                   case_slow_recovery_is_not_a_wake, case_caller_that_reports_nothing):
            fn()
            print()
    finally:
        hs.idle_seconds = real
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())
