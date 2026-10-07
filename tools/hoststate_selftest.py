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

Each case asserts what the *loop* would act on: is the panel dark, and which of the
two edges is there to consume. `take_resume()` is the expensive one - the machine
stopped, so the COM port, the panel's orientation and the ETW capture all have to be
re-made - and `take_refresh()` is the cheap one: the display or the session changed
shape and the panel needs a whole frame, nothing else. Telling them apart is the
subject of the display cases below. The gap case is the one that proves the fallback:
with no events at all, a frozen process must still produce exactly one resume edge.

The display GUIDs and the session/power event numbers are literals written into
this file (see the SDK block below), not imported from `app.hoststate`: a test that
generates its events from the constants under test agrees with them whatever they
say, and that is how two wrong GUID byte strings and two wrong session codes sat
behind a passing suite.

The last-input clock is scripted (`FakeInputClock`), which is what makes this file
a gate instead of a report on the desk: it used to read the live `GetLastInputInfo`,
so it failed while a human was at the keyboard and passed while nobody was. The
suspend cases turn on *when* the input happened relative to the suspend request, and
a live clock cannot be asked what it reported three seconds before the click.
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
                           WTS_CONSOLE_CONNECT, WTS_CONSOLE_DISCONNECT,
                           WTS_SESSION_LOCK, WTS_SESSION_LOGOFF,
                           WTS_SESSION_UNLOCK, HostState)

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


class FakeInputClock:
    """The desk's last-input clock, scripted: no case may depend on this keyboard.

    `hoststate` reads exactly one function for this, `idle_seconds`, and measures
    elapsed time by the `dt` the loop hands `tick()` - so the fake answers the read
    from a scripted instant and is advanced by `tick()` below, by the same `dt`. That
    leaves the arithmetic under test untouched and removes the live call: the desk
    ages when the loop says time passed, and only then.

    `quiet(s)` is an input that happened `s` seconds ago and nothing since; `typing()`
    is somebody touching the keyboard now; `unknown()` is `GetLastInputInfo` failing,
    which has to stay a different answer from "input this instant".
    """

    def __init__(self) -> None:
        self.t = 1_000.0                    # arbitrary origin; only differences are read
        self.last_input = 1_000.0           # an empty desk, until a case says otherwise
        self.known = True
        self._real = hs.idle_seconds

    def install(self) -> None:
        hs.idle_seconds = self.read

    def remove(self) -> None:
        hs.idle_seconds = self._real

    def reset(self) -> None:
        self.t = 1_000.0
        self.last_input = 1_000.0
        self.known = True

    def read(self) -> float | None:
        return None if not self.known else max(0.0, self.t - self.last_input)

    def read_now(self) -> float:
        """The state's *own* clock: the same scripted instant its input ages against.

        `HostState` reads wall time to measure its tick gap and the interval between
        two input readings (`now=`), so that has to be this clock - the one the fake
        input ages against - or the two halves of one input reading belong to two
        different desks and the arithmetic under test is not being tested at all. A
        live `time.monotonic()` beside a scripted `idle_seconds()` is exactly what the
        #43 cases need made impossible: the request lands at a scripted instant, and
        the loop's reading has to be placed on that same timeline for `now - prev_at`
        to mean anything.
        """
        return self.t

    def quiet(self, s: float) -> None:
        self.last_input = self.t - float(s)

    def typing(self, s: float = 0.2) -> None:
        self.quiet(s)

    def unknown(self) -> None:
        self.known = False


CLOCK = FakeInputClock()


def new_state() -> HostState:
    """A HostState with the event *window* off: the cases below drive `_dispatch`
    directly, and a live window would race real broadcasts against the script.

    Each one starts on a desk nobody has touched for an hour; a case that wants
    fresher input says so, which is the whole point of pinning it.

    The state's own clock is the scripted one (`now=CLOCK.read_now`), for the same
    reason its input clock is: since #43 an input reading is stored with the instant
    it was taken, and the interval the rule compares against is the distance between
    two such instants. A scripted age paired with `time.monotonic()` would be two
    different desks, and the mid-interval request below could not be written.
    """
    CLOCK.reset()
    return HostState(gap_s=1.0, poll_s=3600.0, events=False, now=CLOCK.read_now)


def tick(h: HostState, dt: float = 1.0, gap: float | None = None) -> None:
    """Age the scripted desk and tick the state over the same interval.

    The state reads *both* of its clocks from the script (`now=CLOCK.read_now`), so
    advancing `CLOCK.t` is what makes time pass for it; `dt` is only what the loop
    *says* elapsed.

    `gap` is the interval the state *will measure* on this tick, and it is scripted
    by placing the previous tick exactly that far back - `dt` is the clock advance
    that follows, so the placement subtracts it. The number a case asks for is
    therefore the number the state reports (`gap=60.0` is `gap 60s`), which is what
    makes these cases assertable at all: the committed versions moved a live
    `time.monotonic()` back by 400 s and then measured whatever the next two
    `monotonic()` calls happened to span, so `gap:400s` was really `gap:400.003s`
    rounded down and the case read as a statement about the desk's timer.
    It stays separate from `dt` because the input rule's whole history is those two
    being different numbers: it used to be handed the loop's `dt` or the measured
    gap, and the #43 cases below pass a `gap` that deliberately disagrees with `dt`
    to prove the rule borrows neither.
    """
    if gap is not None:
        h._tick_now = CLOCK.t + dt - gap
    CLOCK.t += dt
    h.tick(dt)


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
        check(f"{label}: back-on repaints", h.take_refresh(), f"{label}-on")
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
    tick(h)                                     # the loop's first tick: primes
    tick(h, gap=400.0)                          # the loop's own gap: 400 s unaccounted
    check("gap raises an edge", h.take_resume(), "gap:400s")
    check("and clears the sleep flag", h.asleep, False)
    tick(h)
    check("a normal tick raises nothing", h.resume, None)
    h.close()


def case_slow_start_is_not_a_suspend() -> None:
    print("case: a slow start is not a suspend")
    # A deaf panel takes half a minute to declare itself deaf, between constructing
    # HostState and the loop's first tick. That used to be announced as
    # `[resume] gap:36s` before anything had been built — and a spurious resume
    # relinks the panel and restarts the ETW capture for no reason.
    h = new_state()
    tick(h, gap=36.0)
    check("no edge on the first tick", h.resume, None)
    check("state untouched", h.asleep, False)
    tick(h, gap=36.0)
    check("but the next tick does see a freeze", bool(h.take_resume()), True)
    h.close()


def case_gap_after_event_suspend() -> None:
    print("case: asleep by event, woken by gap (no resume message arrived)")
    h = new_state()
    tick(h)                                     # prime, like the real loop
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    tick(h, gap=60.0)
    check("asleep flag gone", h.asleep, False)
    check("gap reason recorded", h.last_event, "gap 60s")
    edge = h.take_resume()
    check("one edge, named for what it followed", edge, "wake-after-query-suspend")
    h.close()


def case_input_proves_awake() -> None:
    print("case: only input that came *after* the request proves the machine is awake")
    # This case used to assert the opposite - that any reading under two seconds old
    # ended the suspend - and the click on Start -> Sleep is exactly that, so the
    # click cancelled the request it had just made: the loop got a resume edge, the
    # panel was relit and relinked on its way into a suspend, and the desk lost the
    # one moment at which the screen could still be switched off. The question is
    # ordering, not age.
    h = new_state()
    CLOCK.typing()                          # the click that invoked Sleep
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    check("dark on the suspend query", h.asleep, True)
    tick(h)                                 # the loop wakes on the event and ticks
    check("the click that asked for the sleep is not a wake", h.asleep, True)
    check("nothing is queued for the loop", h.resume, None)
    check("the request is outstanding", h.suspend_pending, True)
    check("screen-off wins while it is outstanding", "pending-suspend" in h.summary(),
          True)
    tick(h)                                 # the same click, now two seconds old
    check("ageing the same input does not make it new", h.asleep, True)
    check("idle is still reported for the idle rule", round(h.idle_s, 1), 2.2)
    h._on_event(WM_POWERBROADCAST, PBT_APMSUSPEND, 0)
    tick(h)
    check("still dark at SUSPEND", h.asleep, True)
    check("and still outstanding", h.suspend_pending, True)
    h._on_event(WM_POWERBROADCAST, PBT_APMRESUMEAUTOMATIC, 0)
    check("the resume message ends it", (h.asleep, h.suspend_pending), (False, False))
    check("named for the message", h.take_resume(), "resume:0x12")
    check("the request label is gone", "pending-suspend" in h.summary(), False)
    h.close()


def case_aborted_sleep_resolves_on_new_input() -> None:
    print("case: a sleep that never happened is resolved by input that came after it")
    # Lid closed then reopened, or a suspend that failed to take: no resume message
    # is ever going to arrive, so the input fallback is the only thing that can bring
    # the panel back - and it has to be input that moved the clock forward, not the
    # input that was already the newest thing the desk had done.
    h = new_state()
    CLOCK.quiet(0.3)                        # the click that invoked Sleep
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    tick(h, gap=1.0)                        # production cadence: the readings are 1 s apart
    check("the request stands", h.asleep, True)
    check("and nothing is queued", h.resume, None)
    CLOCK.typing()                          # somebody really is back at the desk
    tick(h, gap=1.0)
    check("input after the request ends it", h.asleep, False)
    check("and the request with it", h.suspend_pending, False)
    check("named for the evidence", h.take_resume(), "input-after-suspend")
    check("the edge is one-shot", h.take_resume(), None)
    # A second sleep asks the same question from scratch: the keystroke that answered
    # the first one is pre-request input now.
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    tick(h, gap=1.0)
    check("re-armed: the last keystroke no longer counts", h.asleep, True)
    check("outstanding again", h.suspend_pending, True)
    tick(h, gap=1.0)
    CLOCK.typing()
    tick(h, gap=1.0)
    check("and new input answers it again", h.asleep, False)
    h.close()


def case_unreadable_input_clock() -> None:
    print("case: an input clock that cannot be read proves nothing, either way")
    h = new_state()
    CLOCK.unknown()
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    tick(h)
    check("unknown input does not fake a wake", h.asleep, True)
    check("nor cancel the request", h.suspend_pending, True)
    check("idle_s still usable for the screen-off rule", h.idle_s >= 0.0, True)
    check("summary says the clock is unknown", "idle=-" in h.summary(), True)
    # A clock that comes back has only pre-request input to report, and anchoring on
    # it is not the same as being woken by it.
    CLOCK.known = True
    CLOCK.quiet(5.0)
    tick(h)
    check("a returning clock cannot cancel with old input", h.asleep, True)
    CLOCK.typing()
    tick(h)
    check("input newer than the anchor can", h.asleep, False)
    check("and clears the request", h.suspend_pending, False)
    h.close()


def case_stale_sample_does_not_answer() -> None:
    print("case: an input sample is judged over the interval between samples (#43)")
    # The exact numbers from the finding. t=0: the click that invoked Sleep, age 0.
    # By t=.90 the desk has aged to .20, and by t=.91 to .21 - readings .01 apart,
    # aged .01, nobody touched anything. The loop hands `tick()` its own dt of .91,
    # and borrowing *that* interval to compare *these* samples is what turned a
    # stale pair into a keystroke and answered the sleep with it: .21 + slack is
    # less than .20 + .91, so the rule saw input. Judged over the interval between
    # the readings themselves, the same pair ages exactly as fast as it is read and
    # answers nothing.
    h = new_state()
    CLOCK.typing(0.0)                       # t=0: the click itself
    h._on_event(WM_POWERBROADCAST, PBT_APMQUERYSUSPEND, 0)
    check("the request anchors on the click", h._input_pair[0], 0.0)
    check("and on the instant it was read", h._input_pair[1], CLOCK.t)
    tick(h, 0.91, gap=0.90)                 # reads .90: age .20; interval .90: no input
    check("still pending after the .90 reading", h.suspend_pending, True)
    tick(h, 0.91, gap=0.01)                 # reads .91: age .21, .01 later; dt .91
    check("a .01 apart pair cannot answer .91 of dt", h.suspend_pending, True)
    check("and no resume is invented", h.resume, None)
    # The control: a real keystroke between two readings still answers, whatever
    # the loop's dt says - the belt still works, it just measures its own span.
    CLOCK.typing(0.02)
    tick(h, 0.91, gap=0.55)
    check("a real keystroke still answers", h.suspend_pending, False)
    check("named for the evidence", h.last_event, "input after suspend")
    h.close()


def case_monitor_seed() -> None:
    print("case: a start-up while the screens are already off can still know it")
    h = new_state()
    h2 = new_state()
    # The real query, on the real machine: this is the one part of the seed that
    # cannot be simulated, and it is what `monitor=?` at start-up is otherwise
    # paying for. 0 (never) or a missing powercfg both return None.
    timeout = h._display_timeout_s()
    check("powercfg gives a timeout or nothing", timeout is None
          or (isinstance(timeout, float) and timeout > 0), True)
    print(f"    this desk: display timeout = {timeout}")

    CLOCK.quiet(12_700.0)                       # hours since anyone was here
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
    CLOCK.quiet(12_700.0)
    h3._display_timeout_s = lambda: 300.0
    h3.seed_monitor()                            # idle 12 700 s against 300 s -> off
    check("seeded off", h3.monitor_on, False)
    CLOCK.typing(0.4)
    tick(h3)
    check("input beats a guessed off", (h3.monitor_on, h3.monitor_seeded),
          (True, False))
    check("named for the evidence", h3.last_event, "input-after-seed")
    h3.close()

    # Nothing to derive from: a scheme where the screens never time out.
    CLOCK.quiet(12_700.0)
    h2._display_timeout_s = lambda: None
    check("no timeout, no claim", h2.seed_monitor().startswith("no seed"), True)
    check("state stays unknown", h2.monitor_on, None)
    h.close()
    h2.close()


def case_wake_flag() -> None:
    print("case: the loop's wait wakes on an event")
    h = new_state()
    tick(h)                                # tick() arms the flag
    check("armed by tick", h.event.is_set(), False)
    h._on_event(WM_POWERBROADCAST, PBT_APMSUSPEND, 0)
    check("set by the message", h.event.is_set(), True)
    check("wait returns at once", h.wait(2.0), True)
    # wait() only reports; a tick consumes, because the tick is what reads the
    # state the message changed. Without that order a message landing between the
    # read and the wait would be swallowed for a whole interval.
    tick(h)
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
    # Reconciled during integration (#35 x #47): the claim is that a logon is taken
    # *exactly like* an unlock - same source, same edge, same name. Which edge that
    # is changed when the view-change events stopped being rebuilds: it is the
    # repaint edge now, and the two are still one and the same event.
    check("logon takes the same repaint edge as an unlock", h.take_refresh(),
          "session-unlock")
    check("and does not claim the machine woke", h.take_resume(), None)
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
    CLOCK.install()                        # nothing below reads this desk's keyboard
    print("last-input clock: scripted\n")
    try:
        for fn in (case_fixtures_agree_with_the_documented_strings,
                   case_suspend_resume, case_monitor, case_lock,
                   case_session_event_numbers, case_critical_resume_is_a_resume,
                   case_display_change, case_display_events_are_not_resumes,
                   case_gap_without_events, case_slow_start_is_not_a_suspend,
                   case_gap_after_event_suspend, case_input_proves_awake,
                   case_aborted_sleep_resolves_on_new_input,
                   case_stale_sample_does_not_answer,
                   case_unreadable_input_clock, case_monitor_seed, case_wake_flag,
                   case_display_guids_are_the_documented_ones, case_source_precedence):
            fn()
            print()
    finally:
        CLOCK.remove()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
