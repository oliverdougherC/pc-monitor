"""Replay sleep / monitor-off / lock at app/hoststate.py — no laptop lid needed.

    .venv\\Scripts\\python tools\\hoststate_selftest.py

Sleep cannot be scheduled from a test and a monitor timeout is a 10-minute wait,
so the event layer is driven the way Windows drives it: `_dispatch(msg, wParam,
lParam)` is the exact function the WndProc calls, and the sequences below are the
exact message orders observed on this desk (see hoststate_probe.py --trace):

  suspend   QUERY → SUSPEND → (frozen) → RESUMEAUTOMATIC, then the unlock and
            usually a display-on a second later
  monitor   a PBT_POWERSETTINGCHANGE for the console display GUID, value 0 then 1
  lock      SESSION_LOCK / SESSION_UNLOCK
  no-events a tick gap of minutes with the event window dead

Each case asserts what the *loop* would act on: is the panel dark, and which of the
two edges is there to consume. `take_resume()` is the expensive one - the machine
stopped, so the COM port, the panel's orientation and the ETW capture all have to be
re-made - and `take_refresh()` is the cheap one: the display or the session changed
shape and the panel needs a whole frame, nothing else. Telling them apart is the
subject of the display cases below. The gap case is the one that proves the fallback:
with no events at all, a frozen process must still produce exactly one resume edge.
"""
import ctypes
import sys
import time
from ctypes import c_ubyte as ctypes_byte

sys.path.insert(0, ".")
from app import hoststate as hs          # noqa: E402
from app.hoststate import (PBT_APMSUSPEND, PBT_APMQUERYSUSPEND,  # noqa: E402
                           PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND,
                           PBT_POWERSETTINGCHANGE, WM_DISPLAYCHANGE,
                           WM_POWERBROADCAST, WM_WTSSESSION_CHANGE,
                           WTS_CONSOLE_CONNECT, WTS_CONSOLE_DISCONNECT,
                           WTS_SESSION_LOCK, WTS_SESSION_LOGOFF,
                           WTS_SESSION_UNLOCK, HostState)

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def setting(guid: bytes, value: int):
    """A POWERBROADCAST_SETTING blob: GUID(16) + DWORD DataLength + DWORD data."""
    return (ctypes_byte * 24)(*guid, *bytes([4, 0, 0, 0]), *value.to_bytes(4, "little"))


def new_state() -> HostState:
    """A HostState with the event *window* off: the cases below drive `_dispatch`
    directly, and a live window would race real broadcasts against the script."""
    return HostState(gap_s=1.0, poll_s=3600.0, events=False)


def case_suspend_resume() -> None:
    print("case: sleep and wake up again")
    h = new_state()
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    check("dark on the suspend query", h.asleep, True)
    h._on_event(WM_POWERBROADCAST, PBT_APMSUSPEND, 0)
    check("still asleep at SUSPEND", h.asleep, True)
    check("suspends counted", h.suspends, 1)
    h._on_event(WM_POWERBROADCAST, PBT_APMRESUMEAUTOMATIC, 0)
    check("awake after RESUMEAUTOMATIC", h.asleep, False)
    check("monitor back to unknown", h.monitor_on, None)
    check("resume edge", h.take_resume(), "resume:0x12")
    check("edge is one-shot", h.take_resume(), None)
    check("resumes counted", h.resumes, 1)
    # The unlock and the display come up after RESUMEAUTOMATIC. They arrive on the
    # cheap edge: the machine is already awake by then, so there is nothing left to
    # re-make - only a panel that has been showing a locked desktop.
    h._on_event(WM_WTSSESSION_CHANGE, WTS_SESSION_UNLOCK, 0)
    check("unlocked", h.locked, False)
    check("unlock asks for a repaint", h.take_refresh(), "session-unlock")
    check("and not for a rebuild", h.take_resume(), None)
    h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, 0)   # wrong-payload path
    h.close()


def case_monitor() -> None:
    print("case: the displays time out")
    h = new_state()
    for guid, label in ((hs.GUID_CONSOLE_DISPLAY_STATE, "console"),
                        (hs.GUID_MONITOR_POWER_ON, "monitor")):
        buf = setting(bytes(guid), 0)
        h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
        check(f"{label}: monitor off seen", h.monitor_on, False)
        check(f"{label}: last_event", h.last_event, f"{label}-display-off")
        buf = setting(bytes(guid), 1)
        h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
        check(f"{label}: monitor on seen", h.monitor_on, True)
        check(f"{label}: back-on repaints", h.take_refresh(), f"{label}-display-on")
        check(f"{label}: back-on is not a wake", h.take_resume(), None)
    # A null payload pointer must be avoided, not read: from_address(0) is an
    # access violation, and an access violation is not an exception.
    h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, 0)
    check("null payload survived", h.monitor_on, True)
    h.close()


def case_lock() -> None:
    print("case: lock, logoff, console handover")
    h = new_state()
    h._on_event(WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK, 0)
    check("locked", h.locked, True)
    h._on_event(WM_WTSSESSION_CHANGE, WTS_SESSION_UNLOCK, 0)
    check("unlocked", h.locked, False)
    h._on_event(WM_WTSSESSION_CHANGE, WTS_SESSION_LOGOFF, 0)
    check("logoff noticed", h.console_lost, True)
    h._on_event(WM_WTSSESSION_CHANGE, WTS_CONSOLE_DISCONNECT, 0)
    check("still lost", h.console_lost, True)
    h.close()


def case_display_change() -> None:
    print("case: a display is added or removed")
    h = new_state()
    h._on_event(WM_DISPLAYCHANGE, 0, 0)
    check("repaint edge", h.take_refresh(), "display-change")
    check("and no rebuild", h.take_resume(), None)
    h.close()


def case_display_events_are_not_resumes() -> None:
    print("case: which events mean the machine stopped, and which only mean it moved")
    # The whole of issue #5 hangs on this split. `WM_DISPLAYCHANGE` arrives when a
    # second monitor is plugged in, when a driver re-modes one, and when a game toggles
    # full screen; the console and monitor display-on notifications arrive every time
    # the desk's screens time out and come back; an unlock arrives every time somebody
    # comes back from lunch. None of them means the serial link died or the present
    # capture went stale, and treating them as if they did cost a full bring-up - in the
    # render loop - plus a discarded capture and a dropped game target.
    h = new_state()
    for msg, w, want in ((WM_WTSSESSION_CHANGE, WTS_SESSION_UNLOCK, "session-unlock"),
                         (WM_WTSSESSION_CHANGE, WTS_CONSOLE_CONNECT, "console-connect"),
                         (WM_DISPLAYCHANGE, 0, "display-change")):
        h._on_event(msg, w, 0)
        check(f"{want}: repaint edge", h.take_refresh(), want)
        check(f"{want}: no rebuild edge", h.take_resume(), None)
    buf = setting(bytes(hs.GUID_CONSOLE_DISPLAY_STATE), 1)
    h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
    check("display-on: repaint edge", h.take_refresh(), "console-display-on")
    check("display-on: no rebuild edge", h.take_resume(), None)
    # A burst is one edge, not five: the first reason is what gets reported, because it
    # is the one that says what happened first.
    for _ in range(3):
        h._on_event(WM_DISPLAYCHANGE, 0, 0)
    h._on_event(WM_WTSSESSION_CHANGE, WTS_SESSION_UNLOCK, 0)
    check("a burst is one repaint edge", h.take_refresh(), "display-change")
    check("a burst is no rebuild at all", h.take_resume(), None)
    # And the real thing still is the real thing, whatever arrived alongside it.
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    h._on_event(WM_POWERBROADCAST, PBT_APMRESUMEAUTOMATIC, 0)
    check("a real wake still re-makes", h.take_resume(), "resume:0x12")
    h.close()


def case_gap_without_events() -> None:
    print("case: frozen with no events (the fallback that has to work)")
    h = new_state()
    h._ev.error = "no window station"
    h.tick(1.0)                                    # the loop's first tick: primes
    h._tick_now = time.monotonic() - 400.0
    h.tick(1.0)
    check("gap raises an edge", h.take_resume(), "gap:400s")
    check("and clears the sleep flag", h.asleep, False)
    h.tick(1.0)
    check("a normal tick raises nothing", h.resume, None)
    h.close()


def case_slow_start_is_not_a_suspend() -> None:
    print("case: a slow start is not a suspend")
    # A deaf panel takes half a minute to declare itself deaf, between constructing
    # HostState and the loop's first tick. That used to be announced as
    # `[resume] gap:36s` before anything had been built — and a spurious resume
    # relinks the panel and restarts the ETW capture for no reason.
    h = new_state()
    h._tick_now = time.monotonic() - 36.0
    h.tick(1.0)
    check("no edge on the first tick", h.resume, None)
    check("state untouched", h.asleep, False)
    h._tick_now = time.monotonic() - 36.0
    h.tick(1.0)
    check("but the next tick does see a freeze", bool(h.take_resume()), True)
    h.close()


def case_gap_after_event_suspend() -> None:
    print("case: asleep by event, woken by gap (no resume message arrived)")
    h = new_state()
    h.tick(1.0)                                    # prime, like the real loop
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    h._tick_now = time.monotonic() - 60.0
    h.tick(1.0)
    check("asleep flag gone", h.asleep, False)
    check("gap reason recorded", h.last_event, "gap 60s")
    edge = h.take_resume()
    check("one edge, named for what it followed", edge, "wake-after-query-suspend")
    h.close()


def case_input_proves_awake() -> None:
    print("case: fresh input proves the machine is awake (an aborted sleep)")
    real = hs.idle_seconds
    h = new_state()
    h2 = new_state()
    try:
        hs.idle_seconds = lambda: 0.3          # somebody is at the keyboard
        h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
        check("dark on the suspend query", h.asleep, True)
        h.tick(1.0)
        check("input cleared the flag", h.asleep, False)
        check("named for the evidence", h.take_resume(), "input-after-suspend")
        # An unanswerable idle clock must not be read as "just typed": that would
        # clear the sleep flag on every tick wherever GetLastInputInfo is unavailable.
        hs.idle_seconds = lambda: None
        h2._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
        h2.tick(1.0)
        check("unknown idle clock does not fake a wake", h2.asleep, True)
        check("idle_s still usable for the screen-off rule", h2.idle_s >= 0.0, True)
    finally:
        hs.idle_seconds = real
    h.close()
    h2.close()


def case_monitor_seed() -> None:
    print("case: a start-up while the screens are already off can still know it")
    real_idle = hs.idle_seconds
    h = new_state()
    h2 = new_state()
    try:
        # The real query, on the real machine: this is the one part of the seed that
        # cannot be simulated, and it is what `monitor=?` at start-up is otherwise
        # paying for. 0 (never) or a missing powercfg both return None.
        timeout = h._display_timeout_s()
        check("powercfg gives a timeout or nothing", timeout is None
              or (isinstance(timeout, float) and timeout > 0), True)
        print(f"    this desk: display timeout = {timeout}")

        hs.idle_seconds = lambda: 12_700.0          # hours since anyone was here
        h._display_timeout_s = lambda: 300.0
        why = h.seed_monitor()
        check("derived off", (h.monitor_on, h.monitor_seeded), (False, True))
        check("and says how it knows", "300s" in why and "12700s" in why, True)
        check("summary admits the guess", "(seed)" in h.summary(), True)
        check("seeding twice changes nothing", h.seed_monitor().startswith("no seed"),
              True)

        # An event wins over the derivation, and demotes the label.
        buf = setting(bytes(hs.GUID_CONSOLE_DISPLAY_STATE), 1)
        h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
        check("a real notification overrides it", (h.monitor_on, h.monitor_seeded),
              (True, False))
        check("and the label goes away", "(seed)" in h.summary(), False)

        # A seeded "off" must never be what keeps the panel dark while the user types.
        h3 = new_state()
        h3._display_timeout_s = lambda: 300.0
        h3.seed_monitor()                            # idle still 12 700 → off
        check("seeded off", h3.monitor_on, False)
        hs.idle_seconds = lambda: 0.4
        h3.tick(1.0)
        check("input beats a guessed off", (h3.monitor_on, h3.monitor_seeded),
              (True, False))
        check("named for the evidence", h3.last_event, "input-after-seed")
        h3.close()

        # Nothing to derive from: a scheme where the screens never time out.
        hs.idle_seconds = lambda: 12_700.0
        h2._display_timeout_s = lambda: None
        check("no timeout, no claim", h2.seed_monitor().startswith("no seed"), True)
        check("state stays unknown", h2.monitor_on, None)
    finally:
        hs.idle_seconds = real_idle
    h.close()
    h2.close()


def case_wake_flag() -> None:
    print("case: the loop's wait wakes on an event")
    h = new_state()
    h.tick(1.0)                          # tick() arms the flag
    check("armed by tick", h.event.is_set(), False)
    h._on_event(WM_POWERBROADCAST, PBT_APMSUSPEND, 0)
    check("set by the message", h.event.is_set(), True)
    check("wait returns at once", h.wait(2.0), True)
    # wait() only reports; a tick consumes, because the tick is what reads the
    # state the message changed. Without that order a message landing between the
    # read and the wait would be swallowed for a whole interval.
    h.tick(1.0)
    t0 = time.monotonic()
    check("wait times out once the tick has read it", h.wait(0.15), False)
    check("and actually waited", time.monotonic() - t0 > 0.1, True)
    h.close()


def main() -> int:
    for fn in (case_suspend_resume, case_monitor, case_lock, case_display_change,
               case_display_events_are_not_resumes,
               case_gap_without_events, case_slow_start_is_not_a_suspend,
               case_gap_after_event_suspend, case_input_proves_awake,
               case_monitor_seed, case_wake_flag):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
