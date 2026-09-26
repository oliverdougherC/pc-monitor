"""Replay sleep / monitor-off / lock at app/hoststate.py — no laptop lid needed.

    .venv\\Scripts\\python tools\\hoststate_selftest.py

Sleep cannot be scheduled from a test and a monitor timeout is a 10-minute wait,
so the event layer is driven the way Windows drives it: `_dispatch(msg, wParam,
lParam)` is the exact function the WndProc calls, and the sequences below are the
exact message orders observed on this desk (see hoststate_probe.py --trace):

  suspend   QUERY → SUSPEND → (frozen) → RESUMEAUTOMATIC, then the unlock and
            usually a display-on a second later
  monitor   a PBT_POWERSETTINGCHANGE for each display GUID in turn, value 0 then 1
  lock      SESSION_LOCK / SESSION_UNLOCK
  no-events a tick gap of minutes with the event window dead

Each case asserts what the *loop* would act on: is the panel dark, and is there
a resume edge to consume (which is what reconnects the COM port and restarts the
ETW capture). The gap case is the one that proves the fallback: with no events at
all, a frozen process must still produce exactly one resume edge.

The display GUIDs and the session/power event numbers are literals written into
this file (see the SDK block below), not imported from `app.hoststate`: a test that
generates its events from the constants under test agrees with them whatever they
say, and that is how two wrong GUID byte strings and two wrong session codes sat
behind a passing suite.
"""
import ctypes
import sys
import time
import uuid
from ctypes import c_ubyte as ctypes_byte

sys.path.insert(0, ".")
from app import hoststate as hs          # noqa: E402
from app.hoststate import (PBT_APMSUSPEND, PBT_APMQUERYSUSPEND,  # noqa: E402
                           PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND,
                           PBT_POWERSETTINGCHANGE, WM_DISPLAYCHANGE,
                           WM_POWERBROADCAST, WM_WTSSESSION_CHANGE,
                           WTS_CONSOLE_DISCONNECT, WTS_SESSION_LOCK,
                           WTS_SESSION_LOGOFF, WTS_SESSION_UNLOCK, HostState)

sys.stdout.reconfigure(errors="replace")

# --- the SDK's own numbers, written down here rather than imported ----------------
# Copied out of Microsoft's power-setting GUID and WM_WTSSESSION_CHANGE pages. The
# GUIDs are the bytes `RegisterPowerSettingNotification` actually takes: Data1, Data2
# and Data3 little-endian, then Data4 and the six node bytes as written, grouped here
# so the field boundaries are visible next to the canonical string they came from.
#
# The reason these are literals and not `from app.hoststate import ...`: a test that
# builds its events from the production constants can only ever agree with them, so
# the two wrong GUID byte strings and the two wrong session codes in `hoststate` were
# invisible to a suite that printed SELFTEST PASSED.
GUID_SESSION_DISPLAY_STATUS = "2B84C20E-AD23-4DDF-93DB-05FFBD7EFCA5"
SDK_SESSION_DISPLAY_STATUS = bytes.fromhex("0ec2842b 23ad df4d 93db 05ffbd7efca5")
GUID_CONSOLE_DISPLAY_STATE = "6FE69556-704A-47A0-8F24-C28D936FDA47"
SDK_CONSOLE_DISPLAY_STATE = bytes.fromhex("5695e66f 4a70 a047 8f24 c28d936fda47")
GUID_MONITOR_POWER_ON = "02731015-4510-4526-99E6-E5A17EBD1AEA"
SDK_MONITOR_POWER_ON = bytes.fromhex("15107302 1045 2645 99e6 e5a17ebd1aea")
GUID_ACDC_POWER_SOURCE = "5D3E9A59-E9D5-4B00-A6BD-FF34FF516548"
SDK_ACDC_POWER_SOURCE = bytes.fromhex("599a3e5d d5e9 004b a6bd ff34ff516548")

SDK_WTS_LOGON, SDK_WTS_LOGOFF = 0x5, 0x6
SDK_WTS_LOCK, SDK_WTS_UNLOCK = 0x7, 0x8
SDK_WTS_SESSION_CREATE, SDK_WTS_SESSION_TERMINATE = 0xA, 0xB     # reserved codes
SDK_PBT_QUERYSUSPEND, SDK_PBT_SUSPEND = 0x0, 0x4
SDK_PBT_RESUMECRITICAL, SDK_PBT_RESUMEAUTOMATIC = 0x6, 0x12

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
    # The unlock and the display come up after RESUMEAUTOMATIC.
    h._on_event(WM_WTSSESSION_CHANGE, WTS_SESSION_UNLOCK, 0)
    check("unlocked", h.locked, False)
    check("unlock re-arms", h.take_resume(), "session-unlock")
    h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, 0)   # wrong-payload path
    h.close()


def case_monitor() -> None:
    print("case: the displays time out")
    h = new_state()
    # Each notification carries the bytes Windows would put in it, so this case fails
    # the moment the code's own GUID construction drifts from the documented one. An
    # unrecognised GUID is silently dropped, which is exactly how the wrong ones
    # behaved: the registration succeeded, and nothing ever arrived to contradict it.
    for guid, label in ((SDK_SESSION_DISPLAY_STATUS, "session-display"),
                        (SDK_CONSOLE_DISPLAY_STATE, "console-display"),
                        (SDK_MONITOR_POWER_ON, "monitor-power")):
        buf = setting(bytes(guid), 0)
        h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
        check(f"{label}: monitor off seen", h.monitor_on, False)
        check(f"{label}: last_event", h.last_event, f"{label}-off")
        buf = setting(bytes(guid), 1)
        h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
        check(f"{label}: monitor on seen", h.monitor_on, True)
        check(f"{label}: back-on raises an edge", h.take_resume(), f"{label}-on")
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
    check("re-sync edge", h.take_resume(), "display-change")
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
        buf = setting(bytes(SDK_CONSOLE_DISPLAY_STATE), 1)
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


def case_fixtures_agree_with_the_documented_strings() -> None:
    print("case: the fixtures themselves are transcribed right")
    # A literal GUID can be mis-copied just as easily as the production one was, so
    # each byte string above is checked against the canonical string it was copied
    # from, under the documented memory layout. `uuid` is stdlib, not this repo: the
    # point of the fixtures is that nothing here is derived from `app.hoststate`.
    for text, blob in ((GUID_SESSION_DISPLAY_STATUS, SDK_SESSION_DISPLAY_STATUS),
                       (GUID_CONSOLE_DISPLAY_STATE, SDK_CONSOLE_DISPLAY_STATE),
                       (GUID_MONITOR_POWER_ON, SDK_MONITOR_POWER_ON),
                       (GUID_ACDC_POWER_SOURCE, SDK_ACDC_POWER_SOURCE)):
        check(text[:8], uuid.UUID(text).bytes_le, blob)


def case_session_event_numbers() -> None:
    print("case: the session event numbers are the documented ones")
    h = new_state()
    h._on_event(WM_WTSSESSION_CHANGE, SDK_WTS_LOGON, 0)
    check("logon 0x5 clears the lock", h.locked, False)
    check("logon means somebody is at the desk", h.monitor_on, True)
    check("logon takes the same wake edge as an unlock", h.take_resume(), "session-unlock")
    h._on_event(WM_WTSSESSION_CHANGE, SDK_WTS_LOCK, 0)
    h._on_event(WM_WTSSESSION_CHANGE, SDK_WTS_LOGOFF, 0)
    check("logoff 0x6 loses the console", h.console_lost, True)
    check("logoff does not fake an unlock", h.locked, True)

    # 0xA and 0xB are the reserved session-create/terminate codes, which is what the
    # code used to call logon and logoff. Read as a logon, 0xA cleared the lock and
    # claimed the display was on; read as a logoff, 0xB said the console had been
    # handed to another session. Neither is evidence of anything here.
    h2 = new_state()
    h2._on_event(WM_WTSSESSION_CHANGE, SDK_WTS_LOCK, 0)
    h2._on_event(WM_WTSSESSION_CHANGE, SDK_WTS_SESSION_CREATE, 0)
    check("reserved 0xA leaves the lock alone", h2.locked, True)
    check("reserved 0xA makes no claim about the display", h2.monitor_on, None)
    check("reserved 0xA raises no edge", h2.resume, None)
    check("reserved 0xA is still named", h2.last_event, "session:0xa")
    h3 = new_state()
    h3._on_event(WM_WTSSESSION_CHANGE, SDK_WTS_SESSION_TERMINATE, 0)
    check("reserved 0xB does not lose the console", h3.console_lost, False)
    h.close()
    h2.close()
    h3.close()


def case_critical_resume_is_a_resume() -> None:
    print("case: 0x6 is the critical resume, not a suspend")
    # WinUser.h spells 0x6 both PBT_APMRESUMECRITICAL and PBT_APMUSERSUSPEND, but the
    # documented meaning is "the system has resumed operation" (a critical suspension,
    # e.g. a failing battery), and its support ended with Windows XP. Handling it as a
    # suspend is the one direction that can leave the panel dark on a live desktop
    # with no further event left to save it.
    h = new_state()
    h._on_event(WM_POWERBROADCAST, SDK_PBT_QUERYSUSPEND, 0)
    check("asleep on the query", h.asleep, True)
    h._on_event(WM_POWERBROADCAST, SDK_PBT_RESUMECRITICAL, 0)
    check("0x6 wakes it", h.asleep, False)
    check("named as the resume it is", h.take_resume(), "resume:0x6")
    check("counted as a resume", h.resumes, 1)
    h2 = new_state()
    h2._on_event(WM_POWERBROADCAST, SDK_PBT_RESUMECRITICAL, 0)
    check("0x6 alone does not invent a suspend", h2.asleep, False)
    check("no suspend counted", h2.suspends, 0)
    h.close()
    h2.close()


def case_display_guids_are_the_documented_ones() -> None:
    print("case: the registered display GUIDs are the documented bytes")
    check("GUID_SESSION_DISPLAY_STATUS", bytes(hs.GUID_SESSION_DISPLAY_STATUS),
          SDK_SESSION_DISPLAY_STATUS)
    check("GUID_CONSOLE_DISPLAY_STATE", bytes(hs.GUID_CONSOLE_DISPLAY_STATE),
          SDK_CONSOLE_DISPLAY_STATE)
    check("GUID_MONITOR_POWER_ON", bytes(hs.GUID_MONITOR_POWER_ON), SDK_MONITOR_POWER_ON)
    # Two sources sharing a byte string would mean one setting asked about twice, with
    # the other silently never answering: the same failure shape as a wrong one.
    check("three distinct settings", len({bytes(hs.GUID_SESSION_DISPLAY_STATUS),
                                          bytes(hs.GUID_CONSOLE_DISPLAY_STATE),
                                          bytes(hs.GUID_MONITOR_POWER_ON)}), 3)


def case_source_precedence() -> None:
    print("case: which display source is allowed to answer")
    # All three settings are registered and, on a modern desk, all three fire for one
    # screen timeout, so precedence has to be decided rather than left to whichever
    # message arrives last: while a better-ranked setting is live, a worse one may not
    # overrule what it said.
    h = new_state()
    h._ev.reg["session-display"] = True
    buf = setting(bytes(SDK_CONSOLE_DISPLAY_STATE), 0)
    h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
    check("console does not answer while the session setting is live", h.monitor_on, None)
    check("and says who it lost to", h.last_event,
          "console-display-off behind session-display")
    check("an ignored source raises no edge", h.resume, None)
    buf = setting(bytes(SDK_SESSION_DISPLAY_STATUS), 0)
    h._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
    check("the session setting does answer", h.monitor_on, False)

    # With the session setting unavailable (an older build, or a registration that
    # failed), the fallbacks have to be believed. Otherwise there is no display
    # source at all and the panel stops following the screen.
    h2 = new_state()
    h2._ev.reg["monitor-power"] = True
    buf = setting(bytes(SDK_MONITOR_POWER_ON), 0)
    h2._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
    check("legacy source answers when it is the only live one", h2.monitor_on, False)
    check("named for the source that answered", h2.last_event, "monitor-power-off")

    # A power setting that is not about the display is neither a display answer nor an
    # event to act on; it is only named, so the probe can show what else arrives.
    buf = setting(bytes(SDK_ACDC_POWER_SOURCE), 1)
    h2._on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, ctypes.addressof(buf))
    check("an unrelated setting changes nothing", h2.monitor_on, False)
    check("and is named by its bytes", h2.last_event, "setting:599a3e5d=1")
    h.close()
    h2.close()


def main() -> int:
    for fn in (case_fixtures_agree_with_the_documented_strings,
               case_suspend_resume, case_monitor, case_lock, case_session_event_numbers,
               case_critical_resume_is_a_resume, case_display_change,
               case_gap_without_events, case_slow_start_is_not_a_suspend,
               case_gap_after_event_suspend, case_input_proves_awake,
               case_monitor_seed, case_wake_flag,
               case_display_guids_are_the_documented_ones, case_source_precedence):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
