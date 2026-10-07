"""The panel as a link that can fail — because it does, on this desk, for real.

Everything the app draws goes through one 115200-baud CDC-ACM port on a board
that is power-cycled by the PC's USB bus. Three failure shapes have actually
been seen in log.log:

  * the port vanishes across a sleep/resume, and the vendor driver's own recovery
    (`WriteLine` → close → `openSerial`) used to give up after 10 tries by calling
    `sys.exit(0)` — and `os._exit(0)` if that raises: a missing screen could
    terminate the whole app, which is the "it never came back" the panel gets
    blamed for. `app.display.harden_vendor` replaces that give-up with a bounded,
    catchable, retirement-checked raise, so ending the process is the app's call
    (and the monitor sleep / fast wake that triggered it is covered end to end in
    `tools/e2e_control_loop_selftest.py`);
  * the endpoint stops draining and a write blocks in `WriteFile` forever: the
    panel is frozen, the loop is frozen, and the log just stops;
  * the panel reboots (its own watchdog, a replug, a resume) and comes back in
    *portrait* at default brightness, while the diff transport still believes its
    copy of the pixels is current — so it sends only the changed bands of a frame
    the panel is no longer displaying in the right orientation.

So the link owns the device object and nothing else touches it:

  - every call is serialised, timed, and wrapped: a raise, a `SystemExit`, or a
    write that never returns marks the link down instead of escaping;
  - a down link is rebuilt here, on our own schedule, and **never** by letting the
    vendor's blind 10-try loop run while the port is missing — we ask for an awake
    port first and stay down (loudly, once) until there is one;
  - a rebuilt link replays the panel's setup — HELLO, orientation, brightness,
    screen-on — and demands a full repaint, because its memory is not ours.

"One owner" is the part that took a review to see, and it is now the shape of the class
rather than an aspiration: a deadline on a vendor call only stops *us* waiting, so the
call survives its own timeout and keeps the handle it was given. Hence three rules —
bring-up and teardown happen one at a time (`_gate`), every operation is handed the
specific handle it belongs to instead of reading whichever one is current when it gets
to run (`_Conn`), and a handle we walk away from is disposed of *before* another is
opened, with its `openSerial` refused so its retry loop cannot take the port back.
Publication and the recovery counters happen only after the panel has taken the setup
that makes a frame visible, and `close()` ends the argument: nothing publishes after it.

`DiffPusher` takes this object in place of the raw driver; the call surface it
uses (`DisplayPILImage`, `get_width`, `get_height`) is unchanged, so the render
path did not have to learn about any of this, except that `DisplayPILImage`
now answers with the link's verdict instead of nothing, and `generation` counts
bring-ups, which together let the diff cache commit only frames this connection
actually displayed.
"""
from __future__ import annotations

import json
import os
import re
import string
import subprocess
import threading
import time
from pathlib import Path

from app.display import (SERIAL_REVISIONS, _HELLO_WAIT_S, _RESET_WAIT_S,
                         _abandon, ensure_vendor_path, harden_vendor)

# The recovery journal: one small file that says "this device was stopped in order to
# be reset, and it still has to be brought back". It is written *before* the
# `/disable-device` and removed only once Windows says the device is no longer
# disabled, because a device left disabled stays disabled through a reboot - whoever
# starts next has to be able to find the debt. That is why it no longer lives in the
# working directory: a run started from anywhere else could not see the previous run's
# record, and the name was gitignored only for the one directory that happened to be
# current.
#
# LOCALAPPDATA is the same under every way this app is started (the scheduled task, a
# console, a selftest in a temp directory), it needs no elevation to write, and it does
# not move when the working directory does. `STATE_DIR` is the seam the offline tests
# use: the gate must not write into a real profile, and two simulated processes have to
# be able to share one journal.
_STATE_DIR_NAME = "PCMonitor"
_STATE_FILE_NAME = "usb_recovery.json"
_LEGACY_MARK_NAME = ".panel_reset_pending"
_JOURNAL_VERSION = 1
STATE_DIR: Path | None = None

# The device id has to survive two parsers (PowerShell's and WQL's) and pnputil's argv
# before it reaches the kernel, so a record holding anything outside the characters a
# real PnP instance id is made of is treated as damage rather than as a device to touch.
_ID_CHARS = frozenset(string.ascii_letters + string.digits + "._&#-\\")

# `ConfigManagerErrorCode` is the Device Manager problem code, read off the device node
# instead of off a command's wording: 0 is "working properly" and 22 is "this device is
# disabled". Only 22 proves the thing this journal exists to survive; only 0 proves the
# device came back whole.
_CM_PROB_NONE = 0
_CM_PROB_DISABLED = 22

# The bus needs a moment between the two words, and the enable is asked for more than
# once because the first attempt against a device that is still re-enumerating fails.
_BUS_SETTLE_S = 2.0
_ENABLE_TRIES = 3
_RECONCILE_LOG_S = 60.0       # how often to repeat "we still owe this device an enable"


class RecoveryPhase:
    """Where an in-progress USB recovery stands, as written in the journal.

    Three points on one path, each recorded *before* the step it describes, so a
    process killed anywhere along it leaves something a later process can act on:

        disabling  the disable has been asked for; whether it took is not known yet
        disabled   Windows confirmed it off the bus - the state this file exists for
        enabling   the enable has been asked for; the confirmation is still owed

    The record is deleted only on the far side of a re-enable that the device node
    confirmed, never because a command reported success.
    """

    DISABLING = "disabling"
    DISABLED = "disabled"
    ENABLING = "enabling"


def state_dir() -> Path:
    """The stable per-application directory for state that has to outlive the process.

    Nothing is created here: asking for a path should not have side effects. Whoever
    means to write makes the directory and fails closed if it cannot.
    """
    if STATE_DIR is not None:
        return Path(STATE_DIR)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / _STATE_DIR_NAME
    profile = os.environ.get("USERPROFILE")
    if profile:
        return Path(profile) / "AppData" / "Local" / _STATE_DIR_NAME
    raise OSError("no per-user state directory: LOCALAPPDATA and USERPROFILE are unset")


def _legacy_mark_paths() -> list[Path]:
    """Where the journal used to sit: the working directory, and the project beside it."""
    root = Path(__file__).resolve().parent.parent
    return [Path(_LEGACY_MARK_NAME), root / _LEGACY_MARK_NAME]


def _is_instance_id(dev: str) -> bool:
    """One plausible PnP instance id - the only thing worth handing to pnputil."""
    return (bool(dev) and "\\" in dev and len(dev) <= 400 and dev == dev.strip()
            and all(ch in _ID_CHARS for ch in dev))


def _wql_id(dev: str) -> str:
    """Escape an instance id for a WQL string literal inside a PowerShell literal.

    WQL wants every backslash doubled; a PowerShell single-quoted literal wants every
    quote doubled - and since WQL's own escaped quote is two quotes, one quote in the
    id becomes four here. Getting this wrong does not raise, it silently matches a
    different device, so it is spelled out once instead of improvised at a query.
    """
    return dev.replace("\\", "\\\\").replace("'", "''''")

# A push of the whole 800x480 frame is ~0.82 s on revision C, so "slow" starts
# above that; anything past _WRITE_GIVEUP_S is the endpoint that stopped draining.
_SLOW_PUSH_S = 1.6
_WRITE_GIVEUP_S = 8.0
_RETRY_S = 10.0             # wait between rebuild attempts after a failure
_RETRY_SLOW_S = 60.0        # same, once the device ladder has been walked to the end
_RELINK_LOG_S = 30.0        # how often to say "still no screen", while retrying
# After this long deaf, stop only reopening the port and ask Windows to restart the
# device itself (pnputil). Reopening a COM port cannot unstick an endpoint that
# stopped draining — measured here: the port opens, HELLO's write blocks, and the
# vendor's RESTART command never reaches the panel.
_USB_RESTART_AFTER_S = 120.0
# The ladder is stepped one rung per attempt, so the wait between attempts decides how
# long the strongest rung takes to arrive. All three are walked a minute apart — reaching
# the last resort about four minutes into a real outage instead of a quarter of an hour
# — and only once they have all been tried does the cadence settle down, because a
# device that survives three rungs will not be persuaded by a fourth every minute.
_USB_RESTART_FIRST_S = 60.0
_USB_RESTART_EVERY_S = 300.0
# How often to repeat "this needs a hand on the cable" once the ladder is exhausted.
_GIVEUP_LOG_S = 900.0
# …and how often to repeat a safety refusal. A refusal is usually permanent until a
# human changes something (the COM number moved, the panel is on a shared hub), so
# saying it every ten seconds would bury everything else in the log.
_REFUSAL_LOG_S = 300.0
# These are local reads of the device store, not the vendor's serial dance: twenty
# seconds is generous, and there can be four of them before one destructive verb.
_QUERY_TIMEOUT_S = 20.0
# Device Manager's problem code, read off the device node instead of off a command's
# wording: 0 is "working properly" and 22 is "this device is disabled".
_CM_PROB_NONE = 0
_CM_PROB_DISABLED = 22

# Which hardware ids this build is willing to call "a panel" without having heard it
# answer first. This mirrors what the vendored revision-C detector itself selects on
# (`_get_awake_com_port`: serial 20080411, 0525:a4a7, 1d6b:0121/0106) - it is not
# invented here. A revision that is not listed has no expectation, and then escalation
# waits for an identity that actually answered HELLO.
_EXPECTED_HARDWARE_IDS = {
    "C": ("USB\\VID_1D6B&PID_0106", "USB\\VID_1D6B&PID_0121", "USB\\VID_0525&PID_A4A7"),
}

# The one compatible id that says "this is a composite device - the thing inside the
# screen", as opposed to a hub. Above the panel's *wake-up* face on this desk sits
# `USB\VID_1A40&PID_0101\6&23491980&0&2`, a hub the rest of the desk shares, and
# disabling a hub to fix a screen is not a recovery, it is an outage with extra steps.
_COMPOSITE_COMPATIBLE_ID = "USB\\COMPOSITE"

# A COM number as Windows spells it inside a device's display name.
_COM_IN_NAME = re.compile(r"\bCOM\d+\b", re.IGNORECASE)


def _wql_id(dev: str) -> str:
    """Escape an instance id for a WQL string literal inside a PowerShell literal.

    WQL wants every backslash doubled; a PowerShell single-quoted literal wants every
    quote doubled - and since WQL's own escaped quote is two quotes, one quote in the
    id becomes four here. Getting this wrong does not raise, it silently matches a
    different device, so it is spelled out once instead of improvised at a query.
    """
    return dev.replace("\\", "\\\\").replace("'", "''''")


def _ps_id(dev: str) -> str:
    """Escape an instance id for a PowerShell single-quoted literal (no WQL layer)."""
    return dev.replace("'", "''")


def _port_in_name(name: str) -> str:
    """The COM number a device's display name claims, uppercased, or "" for none."""
    m = _COM_IN_NAME.search(name or "")
    return m.group(0).upper() if m else ""


class PanelNode:
    """What the device store says about exactly one instance id."""

    def __init__(self, dev: str, present: bool, code: int, name: str, pnpclass: str,
                 hardware_ids: tuple[str, ...], compatible_ids: tuple[str, ...]) -> None:
        self.device_id = dev
        self.present = present
        self.code = code
        self.name = name
        self.pnpclass = pnpclass
        self.hardware_ids = hardware_ids
        self.compatible_ids = compatible_ids

    @property
    def disabled(self) -> bool:
        """Problem code 22: Windows says this device is switched off."""
        return self.code == _CM_PROB_DISABLED

    @property
    def healthy(self) -> bool:
        """Present with problem code 0 - the answer that lets a verb be called done."""
        return self.present and self.code == _CM_PROB_NONE

    def is_composite(self) -> bool:
        """True for the composite device inside the screen, false for hubs and hosts."""
        return any(c.upper() == _COMPOSITE_COMPATIBLE_ID for c in self.compatible_ids)


class PanelIdentity:
    """Where the panel actually sits on the bus, recorded while we were talking to it.

    The COM number is in here, but it is not an address: Windows hands those out again
    whenever it likes, which is how a remembered COM4 ends up belonging to something
    else. The address is `interface_id`, and every destructive verb re-asks Windows that
    this id still exists, still claims this COM number, and still has the hardware ids
    it had when we captured it. `parent_id` is never derived by trimming the interface's
    id or by matching its VID/PID - two screens of the same model are an ordinary thing
    to own - it is read from the device relationship Windows itself recorded, and
    confirmed by asking the parent to name this interface back.

    `confirmed` means the device behind this id answered HELLO, which is the only
    evidence here that is not Windows' opinion about a name. `expected` means its
    hardware ids are ones this build recognises as a panel, which is what lets a
    recovery run during an outage, when nothing will answer HELLO.
    """

    def __init__(self, interface_id: str, port: str, hardware_ids: tuple[str, ...],
                 expected: bool, confirmed: bool) -> None:
        self.interface_id = interface_id
        self.port = port
        self.hardware_ids = hardware_ids
        self.expected = expected
        self.confirmed = confirmed

    def may_reset(self) -> bool:
        """An identity is good for a destructive verb once, and only on one of these."""
        return self.confirmed or self.expected

    def describe(self) -> str:
        how = "answered HELLO" if self.confirmed else (
            "matches a known panel" if self.expected else "not confirmed as the panel")
        return f"{self.interface_id} ({self.port}, {how})"

# How long a synchronous `relink()` waits for a rebuild that is already running before
# it decides the attempt in flight is not going to answer it. A vendor bring-up is
# bounded at roughly HELLO 8 s + reset 30 s + HELLO 8 s plus its sleeps, so this is that
# worst case with room — and it exists because the build we are waiting for can be stuck
# in a native call that no timeout can cancel. Waiting forever on it would move the
# freeze from the port to the loop, which is the bug this whole module is about.
_BUILD_WAIT_S = 90.0

# How long releasing a handle may take when the caller cannot afford to be held up by
# it. Two seconds is longer than any healthy `CloseHandle` on this desk and far shorter
# than the 8 s deadline the write that wedged it was given.
_RELEASE_WAIT_S = 2.0

# The three states a connection can be in, and the only three this class accepts.
# `closed` owns nothing, `building` is mid-bring-up, `ready` may be written to. There
# is no fourth "kind of up" state, because "answered HELLO" is not a state a frame can
# be drawn on — see `_build_owned`, which only leaves `ready` after setup succeeded.
_CLOSED, _BUILDING, _READY = "closed", "building", "ready"


class _Conn:
    """One owned connection: a driver object plus the generation it was opened under.

    Every device call captures *this* object rather than reading `self.lcd` when it
    eventually gets to run. That is the whole difference between a late write and a
    stale one: a worker we gave up on can still finish and still write — but only down
    the handle it was handed, which by then is closed and refuses it. Reading the
    current attribute instead would send yesterday's frame to today's panel.
    """

    __slots__ = ("gen", "lcd")

    def __init__(self, gen: int, lcd) -> None:
        self.gen = gen
        self.lcd = lcd


# Device operations, written against a captured handle for the same reason.
def _op_push(lcd, img, x: int, y: int) -> None:
    lcd.DisplayPILImage(img, x, y)


def _op_brightness(lcd, pct: int) -> None:
    lcd.SetBrightness(pct)


def _op_screen(lcd, on: bool) -> None:
    (lcd.ScreenOn if on else lcd.ScreenOff)()


def _op_orientation(lcd, orient) -> None:
    lcd.SetOrientation(orient)


def _dispose(conn) -> None:
    """Release a connection (or a bare driver, or None) we are finished with.

    Always `app.display._abandon`, never a bare `closeSerial()`: what has to stop is the
    driver's own retry loop, not just its current write.
    """
    _abandon(conn.lcd if isinstance(conn, _Conn) else conn)


def _dispose_later(conn: _Conn) -> None:
    """Release a handle on a helper thread, because waiting for it is not on the table.

    Closing a serial port whose endpoint stopped draining is itself a native call, and
    it can block precisely where the write blocked. The code that just timed out was the
    render loop, so handing it that close would trade a bounded deadline for an
    unbounded wait — the exact failure this module exists to end. Worst case this thread
    sits in the same wedged close; the port is then re-released by the next build, on the
    builder's own thread and under its own deadline, before it reopens anything.
    """
    def go() -> None:
        _dispose(conn)

    threading.Thread(target=go, daemon=True, name=f"panel-release-{conn.gen}").start()


def _release(conn, wait_s: float = _RELEASE_WAIT_S) -> bool:
    """Dispose of a handle, but never for longer than `wait_s`.

    The two demands here pull against each other: the port has to be free before a new
    driver is asked to open it, and the caller is the thread that owns `_gate`, so a
    close that never returns would strand every future rebuild behind it. So the close
    gets a deadline of its own. If it misses it, the wedged thread keeps its wedge, the
    new attempt is free to fail on a busy port, and the ladder carries on trying — which
    is a worse outcome than a released port and a much better one than a frozen app.
    """
    if conn is None:
        return True
    done = threading.Event()

    def go() -> None:
        try:
            _dispose(conn)
        finally:
            done.set()

    threading.Thread(target=go, daemon=True, name="panel-release").start()
    return done.wait(wait_s)


class PanelLink:
    """One guarded, self-healing connection to the desk panel.

    One owner, one generation at a time. The rule the rest of this class exists to
    keep: at any moment there is at most one driver object that can be written to, and
    work started for an older object can neither write nor report through the newer
    one. `self._gate` serialises bring-up and teardown, `self._lock` serialises device
    calls and the state they publish, and `_Conn` carries the handle a call was told to
    use so a late completion cannot reach a connection that replaced it.
    """

    def __init__(self, cfg: dict, log=print) -> None:
        self.cfg = cfg
        self.log = log
        self.d = cfg["display"]
        self.rev = str(self.d["revision"]).upper()
        self._conn: _Conn | None = None   # the one driver we own (may be a dead one)
        self.ok = False                   # mirror of `_state == _READY`, for callers
        self.port: str | None = None
        self.down_reason = ""
        self.rebuilds = 0
        self.slow_writes = 0
        self.errors = 0
        self.abandoned = 0                # workers we walked away from (all disposed)
        self._lock = threading.RLock()
        # A second lock on purpose: `_lock` is held across a device call (seconds), and
        # a rebuild must not queue behind a write that is already doomed — but two
        # rebuilds must queue behind each other, or both open the same COM port.
        self._gate = threading.Lock()
        self._state = _CLOSED
        self._gen = 0
        # The build lease (#6): True from the instant a bring-up claims the port until
        # the attempt is over and the gate is back. `_state` cannot stand in for it —
        # `_retire` parks the state at `_CLOSED` for the whole slow part of a build — so
        # every "is it safe to act?" question asks this instead. See `building`.
        #
        # The reservation is claimed by the *dispatching* thread (see `start_build`), so
        # there is no window between scheduling a bring-up and its lease being visible.
        # `_lease_seq` is what makes the release owner-checked.
        self._building = False
        self._lease_seq = 0
        self._closing = False
        self._retry_at = 0.0
        self._last_down_log = 0.0
        self._last_giveup_log = 0.0
        self._down_since: float | None = None
        self._last_usb_restart = 0.0
        self._restart_depth = 0
        self._restart_attempts = 0
        self._device = ""
        self.usb_restarts = 0
        self.usb_restart_error = ""
        self.recovery_pending = ""    # device a USB cycle still owes an enable
        self.recovery_error = ""      # last journal or device-state problem
        self._last_reconcile_log = 0.0
        self.usb_restart_refused = ""     # why the last escalation touched nothing
        self._restart_touched = 0         # destructive verbs actually issued, not asked for
        self._last_refusal_log = 0.0
        self._identity: PanelIdentity | None = None
        self._identity_error = ""
        self._brightness: int | None = None
        self._screen_on: bool | None = None
        self.needs_full = True       # panel memory is not trustworthy yet
        # One number per successful bring-up: a push that starts and finishes on
        # the same generation went to one live connection the whole way, and only
        # such a push may be remembered as displayed. `needs_full` on its own is
        # not enough: the diff cache has to be able to tell a late completion
        # from the old link apart from a fresh one on the new.
        self.generation = 0
        self._on_relink = None
        # The coordinator's contract (app/recovery.py is the only authority that
        # decides when the panel may illuminate):
        #
        #   tick(reconnect_only=True)  advance the retry clock and walk the device
        #                              ladder, but start no bring-up and send no
        #                              command that raises brightness or takes the
        #                              screen off — a rebuild mid-sleep wakes the
        #                              desk this app is supposed to leave dark;
        #   pending_setup              a bring-up is owed that dark passes declined;
        #   dark_deferred              how many passes were declined (the log must
        #                              be able to tell "nothing to do" from
        #                              "refused to do it, on purpose");
        #   authorize_light()          the intent is lit again: do the owed work now.
        #
        # One authority, one flag: the coordinator never guesses whether a rebuild
        # is safe, and the link never decides on its own to light the panel.
        self.pending_setup = False
        self.dark_deferred = 0

    # ------------------------------------------------------- the owned handle
    @property
    def lcd(self):
        """The driver we currently own — including one that has just failed.

        Kept as an attribute-shaped accessor because the selftests hand this object a
        deliberately bad device (`link.lcd = Deaf()`) and then read it back; adopting
        through the setter is what makes that a real ownership change (generation
        moves, the previous driver is disposed of) rather than a stray write to a field
        the link no longer looks at.
        """
        return self._conn.lcd if self._conn is not None else None

    @lcd.setter
    def lcd(self, driver) -> None:
        with self._lock:
            # `_retire` takes `_lock` itself; it is an RLock, so re-entering it from a
            # thread that already holds it is fine and keeps retirement in one place.
            stale = self._retire()
            if driver is not None:
                self._gen += 1
                self._conn = _Conn(self._gen, driver)
                self._set_state(_READY)
            self._brightness = self._screen_on = None
            self.needs_full = True
        _dispose(stale)

    # ---------------------------------------------------------------- setup
    def open(self, on_relink=None) -> bool:
        """First bring-up. Returns False instead of exiting when there is no screen.

        `app.display.make_lcd` deliberately exits the process when the panel will
        not talk — right for a manual run, wrong for the autostarted loop, which
        should keep trying: a PC that boots before the panel enumerates currently
        stays dead until someone notices. This keeps the same bring-up code and
        turns its fatal answers into a retryable False.

        The one synchronous build: at start-up the loop has no frames to be late
        for, and it is better to draw the first frame on a live link than to have
        the first ten ticks find nothing.
        """
        self._on_relink = on_relink
        return self._build_now(first=True)

    def _build(self, reason: str = "") -> bool:
        """Synchronous rebuild — used for a resume, where the caller wants the answer.

        `wait_s=-1` waits without a cap here because the caller (`relink`) has already
        chosen to queue and the attempt it is queueing behind is itself bounded by one
        build window — see `_claim_lease`. It cannot wait forever: the build it waits for
        either finishes or is retired by a deadline inside `_build_owned`.
        """
        return self._build_now(reason=reason, wait_s=-1.0)

    def start_build(self, reason: str = "background retry") -> None:
        """Rebuild on a background thread, so a deaf panel cannot stall the loop.

        A bring-up against a wedged device takes tens of seconds (auto-detect, HELLO
        bounded at 8 s, sometimes a reset and a second HELLO). Doing that inline made
        the loop's own tick gap look like a suspend — the watchdog fired every rebuild
        and the app "recovered" from a sleep that never happened. The link is down
        while this runs, so every call short-circuits and nothing waits on it.

        Claiming `_BUILDING` here — rather than a separate flag set once the thread is
        running — is what stops a second attempt starting while this one is still queued
        on `_gate`: one build per moment, whether it has reached its vendor traffic yet
        or not.

        The **lease** is taken here, in this dispatching thread, before the worker
        exists (#6 review, P1). Setting only `_state` and then spawning meant the honest
        `building` signal was false for the whole gap between `Thread.start()` and the
        worker's first instruction: during that window a second `start_build()` was
        accepted and `tick()`'s escalation guard saw `not building` and could enter
        `_usb_restart()` underneath the queued bring-up. With a scheduler that queues the
        target without running it, two calls created two workers with `building` false
        throughout. Reserving before scheduling removes the gap, and only the reservation's
        owner may release it, so a momentary contender can never drop a live build's lease.

        A background retry has nothing to do while the link is already up, so that check
        lives *here* rather than in the lease: `relink` is the deliberate exception, and a
        lease that refused whenever the state was `_READY` would refuse exactly the resume
        it exists for — a healthy link is precisely when a resume re-makes it.
        """
        with self._lock:
            if self._closing or self._state == _READY:
                return
        token = self._claim_lease()
        if token is None:
            return
        with self._lock:
            if self._state != _BUILDING:
                self._set_state(_BUILDING)
        # From here the lease belongs to `work`, which adopts it and is the only thread
        # that releases it. Releasing here as well would fight the worker.

        held: list = []                     # the token this worker adopted
        me = threading.get_ident

        def work() -> None:
            # The reservation is adopted here, on the worker, so the release token and
            # the work live on one thread. `start_build` published it in the dispatching
            # thread purely so it is visible *before* this thread is scheduled — the
            # window the review's P1 was about — and then hands ownership over.
            #
            # This `finally` is the *only* place a `start_build` lease is released, and it
            # is why the release cannot live inside `_build_now`: whatever `_build_now`
            # does — return, raise, or not be `_build_now` at all in a test — the lease
            # this worker owns is given back. An earlier draft released on the worker only
            # via `_build_now`, and a path that never reached its release left the link
            # permanently "building" (the `_READY` check then refused every later rebuild).
            try:
                held.append(self._claim_lease(adopt=True))
                self._build_now(reason=reason, adopt=True)
            finally:
                # `_build_now` clears the state on every path where it ran. This catches
                # the one where it did not: it could not get the gate, so the attempt now
                # inside it belongs to somebody else and the state would otherwise stay
                # `_BUILDING` forever with nothing left to drop it.
                if self._gate.acquire(blocking=False):
                    try:
                        with self._lock:
                            if self._state == _BUILDING:
                                self._set_state(_CLOSED)
                    finally:
                        self._gate.release()
                self._release_lease(held[0] if held else None, owner=me())

        threading.Thread(target=work, daemon=True, name="panel-bring-up").start()

    def _claim_lease(self, wait_s: float = 0.0, adopt: bool = False):
        """Take the build lease, or return None if one is already held.

        The reservation is a number, not a boolean, and that is the point: a contender
        that loses must not be able to *release* the winner's lease. The old code's
        `finally` cleared the same flag it may never have set, so a caller that timed out
        waiting for `_gate` could drop the flag belonging to the build still running
        inside it.

        `adopt=True` is for `start_build`'s worker: the dispatcher has already raised the
        lease so it is visible before the thread is scheduled, and the worker *adopts*
        that same reservation — same token, same owner, no second claim and no window in
        which the lease is unowned. That is what keeps the release with the thread that
        does the work while still closing the scheduling gap.

        `wait_s` is for the callers that are supposed to queue: `relink` is a resume
        asking for the link to be re-made, and its contract has always been to wait for a
        background attempt rather than refuse. A negative value waits until it is free,
        but never without a bound: an unbounded wait here is a loop that cannot exit, and
        the thing it would be waiting for is a rebuild that may itself be wedged. The cap
        is one build window, the same one `relink` documents.
        """
        if wait_s < 0:
            wait_s = float(_BUILD_WAIT_S)
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            with self._lock:
                if adopt and self._building:
                    # The dispatcher's reservation: same token, and the *owner* moves to
                    # this thread, so the release lands where the work does.
                    self._lease_owner = threading.get_ident()
                    return self._lease_seq
                if self._closing:
                    return None
                if not self._building:
                    self._building = True
                    self._lease_seq += 1
                    self._lease_owner = threading.get_ident()
                    return self._lease_seq
            # Somebody else owns the link's rebuild. Wait only if this caller was told
            # to; `start_build` and the loop's own paths must answer immediately.
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def _release_lease(self, token, owner=None) -> None:
        """Give back the lease, but only if `token` (and, if given, `owner`) still holds it.

        Two guards because there are two ways a stale release could land: a contender that
        never owned the lease (the token check), and the *dispatcher* releasing the
        reservation its worker has since adopted (the owner check). The second is why
        `start_build` passes its own thread identity — the worker adopts the same token,
        so `token` alone cannot tell them apart.

        The owner comparison is `!=`, not `is not`, and that is not a style choice:
        `threading.get_ident()` builds a new `int` object per call, so two identities that
        are equal can still be distinct objects. Identity comparison there silently
        refused *every* release — a bug that made the link permanently "building" and
        every later rebuild a no-op.
        """
        if token is None:
            return
        with self._lock:
            if not self._building or self._lease_seq != token:
                return
            if owner is not None and self._lease_owner != owner:
                return
            self._building = False
            self._lease_owner = None

    def _build_now(self, first: bool = False, reason: str = "",
                   wait_s: float = 0.0, adopt: bool = False) -> bool:
        """Run one bring-up on *this* thread, holding the lease for the whole attempt.

        Used by start-up, by `relink`, and by `start_build`'s worker. The lease is raised
        before the gate is taken and dropped after it is released, which is the property
        #6 asked for and the one `_state` cannot express: `_build_owned` retires the old
        connection as its first act, and `_retire` parks the state at `_CLOSED`, so for
        the slow half of a bring-up the lifecycle claims nothing is happening while this
        thread is inside the gate holding the port.

        `wait_s` is passed through to `_claim_lease`: 0 (the default) refuses at once,
        which is what the loop's own paths want; `relink` passes a negative value to queue
        behind a background attempt instead of failing the resume. `adopt=True` means this
        thread is `start_build`'s worker taking over the reservation its dispatcher
        already published.
        """
        token = self._claim_lease(wait_s=wait_s, adopt=adopt)
        if token is None:
            # Somebody else owns the link's rebuild right now, and this caller was not
            # asked to wait. Saying so is the answer; two attempts at one port is the
            # fault this whole mechanism prevents.
            self.down_reason = "a rebuild is already in flight"
            return False
        try:
            return self._build_attempt(first=first, reason=reason)
        finally:
            self._release_lease(token)

    def _build_attempt(self, first: bool = False, reason: str = "") -> bool:
        """Bring the link up: one build at a time, and never after `close()`.

        `_gate` is the single-owner rule. Two builds at once — a background retry and a
        resume's `relink()` used to be able to run simultaneously — is not two chances
        at recovery but usually one guaranteed failure: Windows lets a single handle
        open a COM port, so the loser spends its whole bounded bring-up being refused by
        the winner, and then (which is the part that mattered) published its half-open
        driver over the winner's live one.

        Waiting for that gate is bounded, because the build we would be waiting for may
        be stuck in a native call that no timeout can cancel: a resume that cannot get an
        answer inside one worst-case bring-up says so and retries, instead of hanging the
        loop it was meant to rescue.

        The slow part — auto-detect, HELLO, maybe a reset and a second HELLO — runs
        without `_lock` on purpose: `push()` takes that lock, and a build that held it
        for half a minute while the panel ignored us would freeze the render loop
        exactly as badly as doing the work inline. Nothing can use the device while the
        state is not `ready`, so there is nothing to guard during the attempt.
        """
        if first:
            # Start-up has nothing else running: it may take as long as it needs.
            with self._gate:
                return self._build_owned(first=True, reason=reason)
        if not self._gate.acquire(timeout=_BUILD_WAIT_S):
            # Leave the state alone here: an attempt is still inside the gate, and
            # clearing anything would invite a third.
            self.down_reason = "a rebuild is already in flight"
            return False
        try:
            return self._build_owned(first=first, reason=reason)
        finally:
            self._gate.release()
            with self._lock:
                # An attempt that left without publishing and without failing through its
                # own paths (a raise on the way out) must not strand the link mid-claim.
                if self._state == _BUILDING:
                    self._set_state(_CLOSED)

    def _build_owned(self, first: bool, reason: str) -> bool:
        """The body of a build, with `_gate` held and `_lock` not held."""
        if self._closing:
            self.down_reason = "the app is shutting down"
            return False
        # Dispose of the previous driver BEFORE reopening. The port has to be free
        # before anything can own it again, and a driver we are walking away from has
        # its `openSerial` taken away first, so its own retry loop cannot take the port
        # back a second later and race the bring-up below (see `app.display._abandon`).
        # Bounded because this thread holds `_gate`: a close that never returns must not
        # be able to strand every rebuild that comes after it.
        if not _release(self._retire()):
            self.log("[panel] the previous connection still has not released its port "
                     f"after {_RELEASE_WAIT_S:.0f}s — trying anyway")
        ensure_vendor_path()
        harden_vendor()
        # Before anything asks why the panel is silent: a device an earlier attempt
        # left disabled *is* an answer, and finishing that job is not something to do
        # later - the alternative is spending the recovery ladder fighting a device we
        # ourselves switched off. The journal reconciles itself here (and adopts the
        # legacy .panel_reset_pending mark, keeping it if the journal cannot be written).
        self._reconcile_recovery()
        from library.lcd.lcd_comm import Orientation   # noqa: F401 (per attempt)
        import importlib

        from app import display as disp

        rev = self.rev
        w, h = int(self.d["portrait_width"]), int(self.d["portrait_height"])
        port = self.d["com_port"]
        # `pending` is the handoff rule made explicit: from the moment this attempt
        # opens a driver, that driver is either handed to the link or disposed of here.
        # Every path out of the block — the ordinary failure, the vendor's SystemExit,
        # and a raise from code that never expected to be part of a lifecycle — goes
        # through the `finally`, because a driver that belongs to no connection is a
        # second owner of the port, which is the whole fault this change is about.
        pending = None
        try:
            if rev == "SIMU":
                from library.lcd.lcd_simulated import LcdSimulated

                # Before the first one exists: the vendor's browser-preview thread is
                # non-daemon and is not joined by `closeSerial`, which can abort the
                # interpreter at shutdown (see `display.harden_simulated`).
                disp.harden_simulated()

                def make() -> object:
                    sim = LcdSimulated(display_width=w, display_height=h)
                    sim.Reset()
                    sim.InitializeComm()
                    return sim

                lcd = make()
                pending = lcd
            else:
                cls = disp._CLS.get(rev)
                if cls is None:
                    self.down_reason = f"unknown revision {rev}"
                    return False
                auto = str(port).upper() == "AUTO"
                mod = importlib.import_module(cls[0])
                p = self._awake_port(mod, cls[1]) if auto else port
                if p is None:
                    return False        # nothing to open: stay down, retry later
                lcd = self._open_driver(mod, cls[1], p, w, h)
                # Where the panel actually sits, recorded before anything can go
                # wrong with it: every device-level recovery is addressed by this and
                # re-read immediately before it touches anything.
                self._capture_identity()
                pending = lcd

                def make() -> object:
                    """A fresh driver, awake port included, for a second attempt.

                    Never a second lease on the object we just abandoned: a deadline in
                    `_bring_up` only stops us waiting, it does not stop the call, so
                    that thread still owns whatever object it was handed.
                    """
                    m = importlib.import_module(cls[0])
                    p2 = self._awake_port(m, cls[1]) if auto else port
                    if p2 is None:
                        raise RuntimeError("no awake CDC-ACM port to reopen")
                    return self._open_driver(m, cls[1], p2, w, h)

            outcome, live = disp._bring_up(lcd, bool(self.d.get("reset_on_start", False)),
                                           remake=make)
            # Whatever the handshake ended on is what has to be accounted for: `_bring_up`
            # may have opened a driver of its own for a second attempt, and an attempt
            # that left its port open is a second owner.
            pending = live
            if outcome != "ok":
                self.down_reason = f"bring-up said {outcome}"
                self._set_state(_CLOSED)
                return False
            # HELLO answered down this exact port. Of everything Windows can tell us
            # about names and numbers, that is the one piece of evidence that the
            # device behind the id we captured really is the panel.
            self._capture_identity(confirmed=True)
            conn = pending = self._adopt(live)
            if not self._apply_setup(conn):
                # Says *why* the link had to be re-made, not just that it was:
                # `down_reason` describes the failure, which is a different question
                # from what asked.
                self.log(f"[panel] {self.down_reason} — not publishing this connection "
                         f"(gen {conn.gen}); it will be rebuilt")
                self._set_state(_CLOSED)
                return False
            # Says *why* the link had to be re-made, not just that it was.
            if not self._publish(conn, reason or self.down_reason or "relinked", first):
                # Only one thing can refuse a publish now: `close()` happened while this
                # bring-up was running. The driver it just opened belongs to nobody, so
                # it goes back with the same rule as every other unpublished handle.
                self.log("[panel] shutdown happened mid-bring-up — the new connection "
                         f"(gen {conn.gen}) was opened and immediately released")
                return False
            pending = None              # the link owns it from here
            return True
        except SystemExit as e:          # the vendor's "cannot open COM port" exit
            self.down_reason = f"driver gave up on the port (code {e.code})"
            self._set_state(_CLOSED)
            return False
        except Exception as e:  # noqa: BLE001 - an absent screen is a normal state
            self.down_reason = f"{type(e).__name__}: {e}"
            self._set_state(_CLOSED)
            return False
        finally:
            if pending is not None:
                # Bounded for the same reason the retire above is: this thread holds
                # `_gate`, and a close that never returns must not strand every rebuild
                # after it. The handle still goes, on its own thread.
                if not _release(pending):
                    self.log("[panel] an abandoned connection did not release its port "
                             f"within {_RELEASE_WAIT_S:.0f}s; it stays refused")

    def _open_driver(self, mod, cls_name: str, port, w: int, h: int):
        """Construct the vendor driver for `port`, and remember which port won."""
        lcd = getattr(mod, cls_name)(com_port=port, display_width=w, display_height=h)
        self.port = getattr(lcd, "com_port", port)
        return lcd

    # ------------------------------------------------------ state transitions
    def _set_state(self, state: str) -> None:
        """Move the link's state, keeping `ok` — what callers read — in step with it.

        Two spellings of one fact on purpose: `_state` is the lifecycle this class
        reasons about, `ok` is what `main.py`, `DiffPusher` and the beat line have
        always read. They are only ever written together, and only ever here — which is
        why the lock is taken by this method rather than trusted to each caller: a build
        that marks itself down on an error path two frames away from a `_lock` block
        would otherwise publish half the pair. `_lock` is an RLock, so the callers that
        already hold it just re-enter.
        """
        with self._lock:
            self._state = state
            self.ok = state == _READY

    @property
    def building(self) -> bool:
        """Is a bring-up lease held right now — from *before* the gate to *after* it?

        `_state == _BUILDING` cannot answer this (#6). `_build_owned` retires the old
        connection as its first act and `_retire` parks the state at `_CLOSED`, so for
        the whole slow part of a bring-up — auto-detect, HELLO, the vendor's reboot —
        the lifecycle says "closed, nothing happening" while a thread is in fact inside
        the gate holding the port. Everything that asks the state whether it is safe to
        act (escalate to `pnputil`, authorise a lit rebuild, start another attempt) was
        therefore acting against a false answer.
        """
        with self._lock:
            return self._building

    def _retire(self):
        """Give up our claim on the current driver and hand it back to be disposed.

        The generation moves with the claim, so anything still running on that handle is
        from now on provably late: it keeps its own driver object and can say nothing
        about the connection that replaced it.
        """
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                self._gen += 1
            self._set_state(_CLOSED)
            return conn

    def _adopt(self, lcd) -> _Conn:
        """Give a driver a generation. It is not usable until `_publish` says so."""
        with self._lock:
            self._gen += 1
            return _Conn(self._gen, lcd)

    def _publish(self, conn: _Conn, why: str, first: bool) -> bool:
        """The only place a connection becomes usable — after its setup worked.

        Publishing is what used to be the lie: a HELLO-only success marked the link up,
        zeroed the escalation counters and counted a rebuild even when the panel then
        refused every command. Now nothing is reset and nothing is announced unless the
        panel took the setup that makes a frame visible, so "the app came back" and "the
        panel shows the app" stay the same event.
        """
        with self._lock:
            if self._closing:
                return False            # shutdown already gave the port back
            self._conn = conn
            self._brightness = self._screen_on = None
            self.needs_full = True
            self.generation += 1     # a new connection identity starts here
            self.down_reason = ""
            self._restart_depth = 0     # it answered *and* took a command: start over
            self._restart_attempts = 0
            self._set_state(_READY)
            n = 0
            if not first:
                self.rebuilds += 1
                n = self.rebuilds
        if n:
            self.log(f"[panel] link rebuilt ({why}; rebuild #{n}) — full repaint")
        if self._on_relink is not None:
            try:
                self._on_relink()
            except Exception:  # noqa: BLE001
                pass
        return True

    def _awake_port(self, mod, cls_name: str) -> str | None:
        """An awake CDC-ACM port, or None. Also wakes a sleeping panel.

        The vendor's `openSerial()` opens whatever it is given up to ten times and
        then kills the process; asking first means that loop only ever runs against
        a port that exists. For revision C this also pokes the 1A86:CA21 "sleeping"
        face that wakes the panel over.
        """
        try:
            cls = getattr(mod, cls_name)
            return cls.auto_detect_com_port()
        except SystemExit:
            return None
        except Exception as e:  # noqa: BLE001
            self.down_reason = f"auto-detect: {type(e).__name__}: {e}"
            return None

    def _apply_setup(self, conn: _Conn) -> bool:
        """Replay what the panel needs to show landscape pixels at all.

        Runs on the candidate connection *before* it is published, and answers whether
        it may be published at all: "it answered HELLO" and "it will take a command" are
        different claims, and only the second one means a frame can be drawn on it.

        A panel that rebooted came back portrait at its default brightness, and the
        driver's `orientation` attribute — which decides the width/height the layout
        renders for — still says whatever we last told it. Both are re-said here, so
        "the app came back" and "the panel shows the app" are the same event.
        """
        from library.lcd.lcd_comm import Orientation
        land = str(self.d.get("orientation", "landscape")).lower() == "landscape"
        orient = Orientation.LANDSCAPE if land else Orientation.PORTRAIT
        # Guarded like every other write: SetOrientation is a command, and a panel
        # that answers HELLO but has stopped draining its endpoint would hang here.
        completed, res = self._run(conn, "orientation", _op_orientation, orient,
                                   giveup=4.0)
        if not completed:
            self.down_reason = "orientation timed out (panel not draining commands)"
            return False
        if isinstance(res, BaseException):
            self.down_reason = (f"orientation refused: {type(res).__name__}: {res}"
                                if not isinstance(res, SystemExit) else
                                f"orientation: driver exited the process (code {res.code})")
            return False
        # The driver's own notion of the orientation decides the width/height the
        # layout renders for, so it is said to the object as well as to the panel.
        conn.lcd.orientation = orient
        try:
            conn.lcd.lcd_serial.write_timeout = 2.0
        except Exception:  # noqa: BLE001 - optional hardening
            pass
        return True

    # ------------------------------------------------------- guarded calls
    def _run(self, conn: _Conn, what: str, op, *args,
             giveup: float = _WRITE_GIVEUP_S, bounded: bool = True):
        """One call against one captured handle. Returns (completed, result).

        `op` is handed `conn.lcd` rather than reading `self.lcd` when it eventually
        runs, which is the difference between a late write and a stale one: a worker we
        gave up on can still finish and still write, but only down the handle it was
        given — and that handle is closed and refuses `openSerial` by the time it does.
        No link state is touched here; the caller decides what a result means.
        """
        box: list = []

        def go() -> None:
            try:
                box.append(op(conn.lcd, *args))
            except BaseException as e:     # noqa: BLE001 - SystemExit included
                box.append(e)

        if not bounded:
            go()
            return True, (box[0] if box else None)
        th = threading.Thread(target=go, daemon=True, name=f"panel-io-{conn.gen}")
        th.start()
        th.join(giveup)
        if th.is_alive():
            self.abandoned += 1            # the cost of a deadline, counted not hidden
            return False, None
        return True, (box[0] if box else None)

    def _guarded(self, what: str, op, *args, giveup: float = _WRITE_GIVEUP_S,
                 bounded: bool = True) -> bool:
        """One device call on the current connection: serialised, timed, survivable.

        Returns True/False for "the panel took it". Never raises, and never lets a
        blocked write block the caller twice: after a timeout the link is marked down,
        the handle is released so the abandoned worker's next write raises, and every
        later call short-circuits until a rebuild.
        """
        verdict = False
        stale = None
        with self._lock:
            conn = self._conn
            if conn is None or self._state != _READY:
                return False
            gen = conn.gen
            t0 = time.monotonic()
            completed, res = self._run(conn, what, op, *args, giveup=giveup,
                                       bounded=bounded)
            if conn is not self._conn or gen != self._gen:
                # The connection was replaced while this was in flight, so nothing it
                # learned may be written onto the new one — not a down flag, not a retry
                # time, not a counters bump. Cheap insurance on the one-owner rule: the
                # lock is what makes it unreachable today.
                self.log(f"[panel] late {what} from gen {gen} ignored "
                         f"(current gen {self._gen})")
                return False
            if not completed:
                self.slow_writes += 1
                self.errors += 1
                self.down_reason = f"{what} blocked >{giveup:.0f}s"
                self._set_state(_CLOSED)
                self._retry_at = time.monotonic() + _RETRY_S
                stale = conn               # keep the handle to report; release the port
                self.log(f"[panel] {self.down_reason} — endpoint stopped draining; link "
                         f"down, worker gen {gen} abandoned and its port released; "
                         f"will rebuild")
                verdict = False
            else:
                verdict = self._verdict(conn, what, res, time.monotonic() - t0)
        if stale is not None:
            # Off this thread: it is the render loop, and the only reason we are here is
            # that a native call on this handle refused to come back.
            _dispose_later(stale)
        return verdict

    def _verdict(self, conn: _Conn, what: str, res, dt: float) -> bool:
        """What a completed call says about the link. Called with `_lock` held."""
        if dt > _SLOW_PUSH_S:
            self.slow_writes += 1
            self.log(f"[panel] slow {what}: {dt:.2f}s on a {self.rev} link")
        if isinstance(res, SystemExit):
            self._set_state(_CLOSED)
            self.errors += 1
            self.down_reason = f"{what}: driver exited the process (code {res.code})"
            self._retry_at = time.monotonic() + _RETRY_S
            self.log(f"[panel] {self.down_reason} — caught; the app stays up and "
                     f"the link will be rebuilt")
            return False
        if isinstance(res, BaseException):
            self._set_state(_CLOSED)
            self.errors += 1
            self.down_reason = f"{what}: {type(res).__name__}: {res}"
            self._retry_at = time.monotonic() + _RETRY_S
            self.log(f"[panel] {self.down_reason} — link down, will rebuild")
            return False
        return True

    def _call(self, what: str, fn, *args, giveup: float = _WRITE_GIVEUP_S,
              bounded: bool = True):
        """The old shape of `_guarded`, for a callable that takes no handle.

        Kept because `tools/panel_link_selftest.py` pokes it directly to drive a
        deliberately awful device. Production paths use `_guarded`, which hands the
        operation the handle it belongs to.
        """
        return self._guarded(what, lambda _lcd: fn(*args), giveup=giveup,
                             bounded=bounded)

    def push(self, img, x: int = 0, y: int = 0) -> bool:
        return self._guarded(f"push {x},{y}", _op_push, img, x, y)

    # Vendor-shaped passthroughs, so DiffPusher can hold this instead of the driver.
    def DisplayPILImage(self, image, x: int = 0, y: int = 0,   # noqa: N802
                        image_width: int = 0, image_height: int = 0) -> bool:
        # The bool is the point: the raw driver answers with None whether or not
        # the bytes arrived, and DiffPusher treats anything but an explicit True
        # as "not displayed". Returning the link's verdict here is what lets the
        # diff cache commit transactionally instead of optimistically.
        return self.push(image, x, y)

    def get_width(self) -> int:      # noqa: N802
        return int(self.d["portrait_height"]) if str(self.d.get("orientation", "landscape")) \
            .lower() == "landscape" else int(self.d["portrait_width"])

    def get_height(self) -> int:     # noqa: N802
        return int(self.d["portrait_width"]) if str(self.d.get("orientation", "landscape")) \
            .lower() == "landscape" else int(self.d["portrait_height"])

    def set_brightness(self, pct: int) -> bool:
        pct = max(0, min(100, int(pct)))
        if self.ok and self._brightness == pct:
            return True
        if self._guarded(f"brightness {pct}%", _op_brightness, pct, giveup=4.0):
            self._brightness = pct
            return True
        return False

    def screen(self, on: bool) -> bool:
        if self.ok and self._screen_on is on:
            return True
        what = "screen-on" if on else "screen-off"
        if self._guarded(what, _op_screen, on, giveup=4.0):
            self._screen_on = on
            return True
        return False

    # ------------------------------------------------------------- lifecycle
    def invalidate(self) -> None:
        """The next push must be a whole frame (the panel's memory is not ours)."""
        self.needs_full = True

    def relink(self, reason: str) -> bool:
        """Rebuild after a resume/replug/display change. Safe to call spuriously.

        Queues behind a background attempt rather than racing it, because `_build_now`
        takes `_gate`: this is the synchronous answer the resume path wants, and that
        answer is only worth having if it came from the only build that ran. The wait is
        one bounded bring-up long, which is what this call already cost — and the point
        of waiting rather than starting a second one is that a replug landing
        mid-attempt cannot leave two drivers fighting over the port it just got back.

        Note what this does *not* hold: `_lock`. Taking it across a build would invert
        the one lock order this class has (`_gate` before `_lock`, never the other way)
        and deadlock against a builder that is on its way to `_publish`; it also used to
        freeze every `push()` for the length of a vendor bring-up.
        """
        with self._lock:
            self._retry_at = 0.0
            was = self.ok
            self._screen_on = self._brightness = None
        # `down_reason` stays about the failure, not about the request: it is what
        # the beat line and the retry log quote while the link is down.
        built = self._build(reason=reason)
        if built and was:
            self.log(f"[panel] re-linked after {reason}")
        return built

    def tick(self, reconnect_only: bool = False) -> bool:
        """Keep the link honest: rebuild it when it is down and due.

        `reconnect_only=True` is the dark pass. The retry clock and the device
        ladder keep running — a device left disabled through a night is a worse
        morning than a panel that reconnects a minute late — but no bring-up is
        started and no command that raises brightness or wakes the screen is
        sent: the deferred work is remembered as `pending_setup` and done by
        `authorize_light()` when the coordinator says the desk is lit again.
        """
        if self.ok:
            self._down_since = None
            return True
        now = time.monotonic()
        if self._down_since is None:
            self._down_since = now
        if now < self._retry_at:
            return False
        wait = self._retry_interval()
        self._retry_at = now + wait
        if self.ok:
            return True
        if self.building:
            return False                      # an attempt is already in flight
        if reconnect_only:
            # The rebuild is owed, not skipped: `authorize_light()` will do it.
            # Counting the deferral is the point — "dark for 8 hours, nothing
            # attempted" and "dark for 8 hours, 2880 rebuilds refused" are
            # different machines, and only one of them is working.
            #
            # What the dark pass does *not* defer is the device ladder below.
            # Reopening the port and sending HELLO is what raises brightness and
            # wakes the screen, so that is what dark refuses; a device sitting
            # disabled, or an endpoint that stopped draining, is a fault that
            # keeps accumulating while nobody is watching, and a Wednesday-morning
            # refusal to touch it is how a night's sleep turns into a panel that
            # needs the cable pulled. Reconnect-safe and illuminate-are not the
            # same question, and only one of them is answered "no" here.
            self.dark_deferred += 1
            self.pending_setup = True
        else:
            self.start_build()
            if self.ok:
                return True
        # Reopening the port has been failing for a while: the device itself needs a
        # word. pnputil needs Administrator — which the scheduled task has — and when
        # it does not, the answer is logged, not swallowed.
        #
        # `self.building` is not a nicety: `start_build` above has only *started* its
        # thread, so without this the same tick that began a bring-up would reset the USB
        # device underneath it, yanking the port out from under the attempt that was ten
        # seconds from asking it for HELLO. The ladder waits for the next tick, which is
        # a second away; a build that just failed has already dropped its lease, so
        # escalation is delayed by one attempt, never suppressed.
        #
        # This used to read `self._state != _BUILDING`, which is a *false* answer for
        # almost the whole build (#6): `_build_owned` retires the old connection first
        # and `_retire` sets the state to `_CLOSED`, so the guard was open exactly while
        # a thread was inside the gate and the pnputil reset would do the most damage.
        if (self.cfg["display"].get("usb_restart_on_fail", True)
                and not self.building
                and now - self._down_since > _USB_RESTART_AFTER_S
                and now - self._last_usb_restart > self._restart_wait()):
            self._last_usb_restart = now
            self._usb_restart()
        if now - self._last_down_log > _RELINK_LOG_S:
            self._last_down_log = now
            self.log(f"[panel] no usable screen ({self.down_reason or 'not built'}); "
                     f"retrying every {wait:.0f}s — the app keeps running")
        self._exhausted_notice(now)
        return False

    def authorize_light(self) -> bool:
        """The coordinator says the desk is lit: do the bring-up dark passes refused.

        Returns whether the link is usable afterwards, which is not whether the work
        was done — a bring-up takes tens of seconds against a dead endpoint, so this
        starts it and reports the state as it stands. The flag clears either way:
        the debt has been taken up, and a `pending_setup` that outlives the
        authorization would have every later lit pass stampede the port. If the
        attempt fails, the ordinary retry clock owns the next one.
        """
        if not self.pending_setup:
            return self.ok
        self.pending_setup = False
        if self.ok or self.building:
            return self.ok
        self.log(f"[panel] light authorised after {self.dark_deferred} dark pass(es) — "
                 f"reopening the panel now")
        self.start_build()
        return self.ok

    def _retry_interval(self) -> float:
        """How long to sit before the next rebuild attempt.

        Ten seconds is right while there is something left to try — a replug, a port
        that has come back, a rung of the device ladder still unspent. It is wrong once
        the ladder has run out: an attempt is a full 35-second vendor bring-up, so
        attempts back-to-back spend the night re-timing-out against a board that only a
        power cycle will wake, and they fill the log the app itself has to read in the
        morning. After the last rung the cadence drops to `_RETRY_SLOW_S`, which still
        takes a replug inside a minute — and any real event (resume, replug, display
        change) calls `relink()`, which ignores this and tries immediately.
        """
        return _RETRY_S if self._restart_attempts < 3 else _RETRY_SLOW_S

    def _exhausted_notice(self, now: float) -> None:
        """Say it once, plainly, when the software ladder has run out.

        Every rung above logs its own result, which is right for a log but wrong for a
        person: three successful `pnputil` calls and three HELLO timeouts is the
        evidence that the fault is past software, and the app should draw that
        conclusion instead of leaving it to whoever is reading at 7 a.m. One line,
        repeated only every `_GIVEUP_LOG_S`, because after that it is the same sentence.

        It counts verbs actually issued (`_restart_touched`), not escalations asked
        for: a ladder that was refused at every rung has not proved anything about the
        firmware, and it says so in its own refusal line instead.
        """
        if (self._restart_touched < 3 or self.ok
                or now - self._last_giveup_log < _GIVEUP_LOG_S):
            return
        self._last_giveup_log = now
        self.log("[panel] software recovery exhausted — the device has been restarted "
                 "twice and then disabled/re-enabled, and it still will not answer "
                 "HELLO. That is past software: unplug the screen's USB (or its hub "
                 "port), wait five seconds, plug it back. This loop takes it back "
                 "within a minute on its own; nothing needs restarting.")

    def _restart_wait(self) -> float:
        """How long to sit before the next rung of the ladder.

        The whole ladder is walked a minute at a time: escalation is pointless if the
        strongest rung takes a quarter of an hour to arrive. Only after every rung has
        been tried does it settle down to `_USB_RESTART_EVERY_S` — a device that
        survives all three is not going to be persuaded by a fourth every minute.
        """
        return (_USB_RESTART_FIRST_S if self._restart_attempts < 3
                else _USB_RESTART_EVERY_S)

    # -------------------------------------------------- device-level recovery
    def _port_device(self) -> str:
        """The COM port to blame for a restart: the one we last tried to use."""
        want = str(self.port or self.d.get("com_port") or "").upper()
        return want if want.startswith("COM") else ""

    # ---------------------------------------------- the device store, read-only
    def _bus(self, script: str) -> str:
        """One machine-readable query against the device store: its stdout, or "".

        Single-quoted literals and `[char]39` only, no double quotes anywhere: the
        script travels as one argv element through Python's join and PowerShell's
        parser, and every extra quoting layer has to survive both. Every answer is
        prefixed with a marker word this module chose, so localized command output,
        progress noise and a module's own banner cannot be mistaken for one - and an
        answer that does not arrive in that shape is treated as no answer at all.
        """
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                "-Command", script], capture_output=True, text=True,
                               timeout=_QUERY_TIMEOUT_S,
                               creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW: pythonw has no console, so an un-flagged query flashes one per retry
        except Exception as e:  # noqa: BLE001 - a bus we cannot read is a state, not a crash
            self.usb_restart_error = f"query: {type(e).__name__}: {e}"
            return ""
        return r.stdout or ""

    @staticmethod
    def _answers(out: str, marker: str) -> list[str]:
        """The marked lines a query printed, in order, with the marker stripped."""
        return [ln.strip()[len(marker):] for ln in (out or "").splitlines()
                if ln.strip().startswith(marker)]

    @staticmethod
    def _node_script(where: str) -> str:
        """A query that prints exactly one node's facts, or how many nodes answered.

        `where` is the whole `Get-CimInstance` expression, so the same reading works for
        an exact instance id and for the one discovery-by-port-number query.
        """
        return ("$d = @(" + where + ");"
                " if ($d.Count -ne 1) { 'PANELNODE|count=' + [string]$d.Count }"
                " else { $x = $d[0]; 'PANELNODE|1|' + [string]$x.Present + '|'"
                " + [string]$x.ConfigManagerErrorCode + '|' + [string]$x.Name + '|'"
                " + [string]$x.PNPClass + '|' + ($x.HardwareID -join ';')"
                " + '|' + ($x.CompatibleID -join ';') + '|' + [string]$x.DeviceID }")

    def _read_node(self, out: str, dev: str) -> tuple[PanelNode | None, str]:
        """Parse one node line. `absent` and `ambiguous` come back as reasons, not None.

        They are the two answers that have to stop something destructive, and each needs
        saying out loud: "no such device any more" and "several nodes claim to be this
        one" are different faults with different fixes, and neither is guessable.
        """
        lines = self._answers(out, "PANELNODE|")
        if not lines:
            return None, f"Windows would not say anything about {dev}"
        fields = lines[-1].split("|")
        if fields[0].startswith("count="):
            n = fields[0][len("count="):]
            if n == "0":
                return None, f"{dev} is not in the device store any more"
            return None, f"{n} device nodes answer to the one id {dev}"
        if len(fields) != 8:
            return None, f"the node for {dev} did not answer in the expected shape"
        try:
            code = int(fields[2])
        except ValueError:
            return None, f"the node for {dev} reported problem code {fields[2]!r}"
        # The id comes back from the store, never from whatever we searched by: the one
        # query that is allowed to search by port name is how an instance id is *found*,
        # and reusing the search key as the answer would record "COM7" as the panel's
        # identity - a number Windows hands out again whenever it likes.
        return (PanelNode(fields[7] or dev, fields[1] == "True", code, fields[3],
                          fields[4],
                          tuple(h for h in fields[5].split(";") if h),
                          tuple(c for c in fields[6].split(";") if c))), ""

    def _node(self, dev: str) -> tuple[PanelNode | None, str]:
        """The device node that answers to this exact instance id - the only address.

        Never a name pattern and never a VID/PID prefix: a device is addressed by the
        one string that names it, escaped for WQL once in `_wql_id`.
        """
        wql = "'DeviceID=' + [char]39 + '" + _wql_id(dev) + "' + [char]39"
        return self._read_node(self._bus(self._node_script(
            "Get-CimInstance Win32_PnPEntity -Filter " + wql)), dev)

    def _node_owning(self, port: str) -> tuple[PanelNode | None, str]:
        """The single node that claims `port` - used once, at discovery, to find an id.

        A COM number is published nowhere but the display name, so this is the one query
        anywhere that searches by name, and it is how an instance id is *found*, never
        how a destructive verb is addressed. Two claimants (a Bluetooth outbound link
        takes COM numbers too) is a refusal, not a `Select-Object -First 1`.
        """
        wql = "'Name LIKE \"%(" + _wql_id(port) + ")%\"'"
        return self._read_node(self._bus(self._node_script(
            "Get-CimInstance Win32_PnPEntity -Filter " + wql)), port)

    def _parent_of(self, dev: str) -> str:
        """The instance id Windows itself recorded as this device's parent.

        `Win32_PnPEntity.Parent` is empty for a CDC interface, and the old code worked
        around that by trimming the `&MI_xx` suffix off the interface id and LIKE-matching
        what was left - a guess about a name, which finds *a* device of the same model
        rather than *this* device's parent. `DEVPKEY_Device_Parent` is the relationship
        the enumerator recorded, which is the same answer `CM_Get_Parent` gives.
        """
        script = ("$p = @(Get-PnpDeviceProperty -InstanceId '" + _ps_id(dev) + "'"
                  " -KeyName DEVPKEY_Device_Parent -ErrorAction SilentlyContinue);"
                  " if ($p.Count -eq 1 -and $p[0].Data) { 'PANELPARENT|' + [string]$p[0].Data }"
                  " else { 'PANELPARENT|' }")
        lines = self._answers(self._bus(script), "PANELPARENT|")
        return lines[-1].strip() if lines else ""

    def _children_of(self, dev: str) -> list[str]:
        """The children Windows recorded for a device, so a claimed parent can be asked
        to name this interface back. Case-insensitively: the property store hands the
        instance part of an id back in lower case (`…\\8&e9576a&0&0000` for a node whose
        DeviceID reads `8&E9576A`), measured here."""
        script = ("$c = @(Get-PnpDeviceProperty -InstanceId '" + _ps_id(dev) + "'"
                  " -KeyName DEVPKEY_Device_Children -ErrorAction SilentlyContinue);"
                  " if ($c.Count -eq 1 -and $c[0].Data) { 'PANELCHILD|' + (($c[0].Data) -join ';') }"
                  " else { 'PANELCHILD|' }")
        lines = self._answers(self._bus(script), "PANELCHILD|")
        return [c for c in (lines[-1].split(";") if lines else []) if c]

    def _verb_delivered(self, verb: str, dev: str) -> tuple[bool, str]:
        """Did the device end up where this verb promises it would?

        Each verb promises a state, and the device store is asked for that state: a
        disable has to leave the node reading disabled, a restart or an enable has to
        leave it present and not disabled. Anything else - including a node that cannot
        be read - is not a success. A restart that did nothing is indistinguishable from
        one that worked by state alone, so this proves the device is where it should be,
        not that the verb was the thing that put it there.
        """
        node, why = self._node(dev)
        if node is None:
            return False, f"{dev} cannot be read after {verb}: {why}"
        if verb == "/disable-device":
            if node.disabled:
                return True, f"{dev} now reads disabled"
            return False, (f"{dev} reads problem code {node.code}, not disabled - "
                           f"{verb} did not take")
        if verb in ("/enable-device", "/restart-device"):
            if node.disabled:
                return False, f"{dev} still reads disabled after {verb}"
            if not node.present:
                return False, f"{dev} is not present after {verb}"
            return True, ""
        return False, f"no device state is promised by {verb}, so it cannot be believed"

    # ------------------------------------------------------- the panel's identity
    def _capture_identity(self, confirmed: bool = False) -> None:
        """Record which device on the bus the panel is, while we are talking to it.

        Cheap on repeat: the store is only re-read when the port has changed, or when
        `confirmed` says a HELLO just came back down it. Everything device-level later
        is addressed by what is recorded here and re-read again immediately before it
        acts; a COM number on its own is never an address.
        """
        if self.rev not in SERIAL_REVISIONS:
            return                      # SIMU has no bus presence, TUR_USB no COM port
        port = self._port_device()
        if not port:
            self._identity = None
            self._identity_error = "no COM port was resolved for the panel"
            return
        if self._identity is not None and self._identity.port == port:
            if confirmed:
                self._identity.confirmed = True
            return
        node, why = self._node_owning(port)
        if node is None:
            self._identity = None
            self._identity_error = f"{port}: {why}"
            return
        known = _EXPECTED_HARDWARE_IDS.get(self.rev, ())
        expected = any(h.upper().startswith(k.upper())
                       for h in node.hardware_ids for k in known)
        self._identity = PanelIdentity(node.device_id, port, node.hardware_ids,
                                       expected, confirmed)
        self._identity_error = ""
        self.log(f"[panel] panel identified on the bus as {self._identity.describe()}")

    def _verified_target(self, parent: bool) -> tuple[str, str]:
        """The one device instance this escalation may touch, or ("" , why it may not).

        Asked immediately before the verb and never once at start-up, because everything
        it rests on can change in between: Windows reissues COM numbers, an interface
        vanishes across a resume, and the device above an interface is not always the
        screen. An empty answer means touch nothing - not "do the smaller thing
        instead", and not "assume it worked anyway".
        """
        ident = self._identity
        if ident is None:
            return "", (self._identity_error or
                        "the panel was never identified on the bus")
        if not ident.may_reset():
            return "", (f"{ident.interface_id} is not established as the panel: it never "
                        f"answered HELLO, and its hardware ids are not a revision {self.rev} "
                        f"screen this build knows")
        node, why = self._node(ident.interface_id)
        if node is None:
            return "", f"the panel's interface {ident.interface_id}: {why}"
        if node.disabled:
            return "", (f"{ident.interface_id} is already disabled - what it needs is "
                        f"enabling, which is not what any of these verbs are for")
        if node.hardware_ids != ident.hardware_ids:
            return "", (f"{ident.interface_id} now reports hardware ids "
                        f"{';'.join(node.hardware_ids) or 'none'} instead of "
                        f"{';'.join(ident.hardware_ids)} - that is a different device "
                        f"wearing this instance id")
        claimed = _port_in_name(node.name)
        if claimed != ident.port:
            return "", (f"{ident.interface_id} no longer claims {ident.port} (its name is "
                        f"{node.name or 'blank'}) - whatever owns {ident.port} now is not "
                        f"the panel and will not be touched")
        if not parent:
            return ident.interface_id, ""
        par = self._parent_of(ident.interface_id)
        if not par:
            return "", f"Windows will not say what the parent of {ident.interface_id} is"
        pnode, why = self._node(par)
        if pnode is None:
            return "", f"the parent {par}: {why}"
        if not pnode.is_composite():
            return "", (f"the parent of {ident.interface_id} is {par} "
                        f"({pnode.name or 'unnamed'}), which is not the screen's own "
                        f"composite device - other devices hang off it")
        if not pnode.present:
            return "", f"the parent {par} is not present"
        if ident.interface_id.lower() not in [k.lower() for k in self._children_of(par)]:
            return "", (f"{par} does not list {ident.interface_id} among its children, so "
                        f"the parent link does not go both ways")
        return par, ""

    def _refuse_restart(self, why: str) -> bool:
        """Nothing was touched, and that is reported as itself.

        A refusal is neither a recovery (`usb_restarts` stays where it was) nor the
        firmware verdict: the exhausted notice must not get to claim the ladder was
        walked when it was never allowed to step. The cadence still advances, because a
        refusal that outlives a reboot would otherwise be logged every ten seconds.
        """
        self.usb_restart_refused = why
        self._restart_attempts += 1
        now = time.monotonic()
        if now - self._last_refusal_log > _REFUSAL_LOG_S:
            self._last_refusal_log = now
            self.log(f"[panel] NOT touching any device: {why}. No restart and no disable "
                     f"were issued; the port is still being retried on its own schedule.")
        return False

    def _usb_restart(self) -> bool:
        """Restart the panel's USB device. The last resort that is not a hand on the cable.

        When the CDC endpoint has stopped draining, nothing can be delivered — not
        even the vendor's own RESTART — so retrying the port forever cannot help;
        measured here with `tools/screen_wake_probe.py`: the port opens, the first
        write blocks. It needs Administrator — the scheduled task has it — and pnputil
        **exits 0 even when it refuses**, so a verdict is read from the device store
        afterwards, never from what the command said.
        (`Restart-PnpDevice`, the cmdlet that would do the same, has been removed from
        this build's PnpDevice module.)

        Three rungs, each tried once before stepping up, all of them aimed at a device
        `_verified_target` has just confirmed is the panel's:

        1. restart the CDC interface (`…&MI_00`) — re-initialises the serial pipe;
        2. restart the composite device above it — the software unplug/replug;
        3. disable and re-enable that device - the strongest thing software can do to
           the port, and the only rung that can leave the device *disabled*, so it is
           bracketed by the recovery journal (see `_disable_and_enable`). Stopping a
           driver is not an electrical power cycle: the panel keeps its 5 V rail
           through a disabled port on a self-powered hub, which is why this rung is
           allowed to fail and to say so rather than being logged as a reboot.

        None of them runs on a guess. A COM number is reissued by Windows whenever it
        likes, and two screens of the same model differ only by their instance id, so
        the ladder aims at the id captured while the panel was answering and re-read
        immediately before the verb; if that reading is ambiguous, stale, or lands on a
        hub the rest of the desk shares, the answer is a refusal - which is neither a
        recovery nor a verdict about the panel's firmware.

        Measured on this desk: the interface restart brought the pipe back (a read
        returned where the write had blocked) but HELLO came back empty — pipe alive,
        MCU not — and the parent restart did not clear it either. So the ladder exists
        to take the fault as far down as it can go and to say plainly, when it runs
        out, that the answer is the cable.
        """
        # Refused outright while a bring-up holds the port (#6). The caller in `tick()`
        # already declines to escalate during a build, but `_usb_restart` is also
        # reachable from the recovery worker's retry path, and a pnputil reset — or the
        # disable/enable rung, which takes the device off the bus — issued underneath a
        # thread that is mid-HELLO is precisely the interference the single-owner rule
        # exists to prevent. A refusal is its own answer: not a recovery, and not a
        # verdict about the firmware (`_refuse_restart`).
        if self.building:
            self._refuse_restart("a bring-up is holding the port; its own attempt is "
                                 "the answer to this fault")
            return False
        port = self._port_device()
        if not port:
            self.usb_restart_error = "no COM port known for the panel"
            return False
        want_parent = self._restart_depth > 0
        target, why = self._verified_target(parent=want_parent)
        if not target and want_parent:
            # Losing the parent is a reason to take a smaller rung, not a reason to do
            # nothing: the interface is still ours to restart. Say which rung was lost.
            self.log(f"[panel] cannot reach the device above {port}: {why} - stepping "
                     f"down to the panel's serial interface")
            target, why = self._verified_target(parent=False)
        if not target:
            return self._refuse_restart(why)
        aimed_parent = target != self._identity.interface_id
        self.usb_restart_refused = ""
        self._device = target           # remembered: the heal path needs an id to fix
        depth = self._restart_depth
        self._restart_attempts += 1     # how long to wait for the next rung
        self._restart_depth += 1        # next time, aim one rung higher
        self._restart_touched += 1      # something is genuinely about to be disrupted
        if depth >= 2:
            # The rung has to be both durable and aimed: #7's journal (a device
            # left disabled stays disabled through a reboot, so the intent is
            # committed before the verb and the cycle is refused when that write
            # fails) applied to #8's verified instance id (a COM number is not an
            # address, and ambiguity is a refusal). Either half alone is the bug.
            ok, line = self._disable_and_enable(target)
            did = "disabled and re-enabled the panel's USB device"
        else:
            ok, line = self._pnputil("/restart-device", target)
            did = f"restarted the panel's USB {'device' if aimed_parent else 'port'}"
        if ok:
            self.usb_restarts += 1
            self._retry_at = time.monotonic() + 5.0     # the device is re-enumerating
            self.log(f"[panel] {did} ({target}) — waiting for it to come back")
            return True
        self._restart_depth = 0
        self.usb_restart_error = line
        self.log(f"[panel] device reset did not work ({self.usb_restart_error}) — this "
                 f"needs Administrator; the loop keeps retrying the port")
        return False

    def _pnputil(self, verb: str, dev: str) -> tuple[bool, str]:
        """One pnputil device verb, believed only as far as the device store confirms it.

        The exit code lies (`Access is denied.` comes back with a 0) and the wording is
        localized, so the old `fail`/`denied`/`error` sniffing was never a verdict - it
        was an English accent, and `Zugriff verweigert` read as success. Each verb
        promises a state, so `_verb_delivered` asks the device node for that state and
        that answer is the verdict; the command's own last line is kept for the log.
        """
        try:
            r = subprocess.run(["pnputil", verb, dev], capture_output=True, text=True,
                               timeout=60,
                               creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW: pythonw has no console, so an un-flagged verb flashes one
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        line = (out.splitlines() or [f"exit {r.returncode}"])[-1][:140]
        if r.returncode != 0:
            return False, f"{line} (exit {r.returncode})"
        delivered, why = self._verb_delivered(verb, dev)
        return delivered, (why or line)

    def _device_state(self, dev: str) -> str:
        """What Windows says about this one device instance: on | off | degraded |
        absent | unknown.

        The answer comes off the device node rather than off what a command printed:
        pnputil exits 0 even when it refuses, and its wording is localized, so text
        cannot be the reason a recovery record gets deleted. The query matches the
        exact instance id and not a name pattern, because the question is about this
        device and not about a second one that happens to be named similarly.

        It asks through `_node`, the same single device-store seam every other
        question uses. It used to run its own PowerShell here, which meant the
        process had two ways of reading one store: the identity gate could be
        faked out in a test while the journal's verdict silently came from the
        real machine, and in production the two could disagree about a device
        between one query and the next.

        `degraded` is a node that is there, is not disabled, and is reporting some
        other problem: the debt is paid even though the panel may still be unusable,
        because this record's only promise is that we did not leave the device
        switched off. `unknown` keeps the record alive - an unanswered question is not
        a confirmation of one.
        """
        if not _is_instance_id(dev):
            return "unknown"
        node, why = self._node(dev)
        if node is None:
            if "not in the device store" in why:
                return "absent"
            if "device nodes answer" in why:
                self.recovery_error = f"{dev}: several nodes answer to one instance id"
            else:
                self.recovery_error = f"the device-state query for {dev}: {why}"
            return "unknown"
        if node.disabled:
            return "off"
        if node.code == _CM_PROB_NONE:
            return "on" if node.present else "degraded"
        return "degraded"

    # ---------------------------------------------------- the recovery journal
    def _journal_path(self) -> Path:
        return state_dir() / _STATE_FILE_NAME

    def _journal_name(self) -> str:
        """Where the journal lives, for a log line - even when that path is the fault."""
        try:
            return str(self._journal_path())
        except OSError:
            return _STATE_FILE_NAME

    def _stamp(self) -> str:
        """UTC at second resolution: the journal is read by a later process, possibly
        after a reboot, so the clock it happened to be written on must not matter."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _new_record(self, dev: str, phase: str) -> dict:
        return {"version": _JOURNAL_VERSION, "phase": phase, "device_id": dev,
                "port": self.port or "", "pid": os.getpid(),
                "written_at": self._stamp()}

    def _write_record(self, record: dict) -> str:
        """Put the journal on disk; "" means it is there, anything else is why not.

        Temp file, fsync, then one atomic replace: a crash anywhere in here leaves
        either the previous record or this one, never a half-written file the next
        process cannot read. Windows gives no way to force a directory entry, so the
        replace is the last step that can be durable - everything before it has
        already been pushed out of the cache.
        """
        try:
            d = state_dir()
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / (_STATE_FILE_NAME + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, d / _STATE_FILE_NAME)
        except OSError as e:  # noqa: BLE001 - the caller decides how loud to be
            self.recovery_error = f"write: {type(e).__name__}: {e}"
            return self.recovery_error
        self.recovery_error = ""
        return ""

    def _read_record(self) -> tuple[dict | None, str]:
        """The pending recovery, and why there is no readable one.

        The two halves are different answers: (None, "") means nothing is owed, while
        (None, reason) means a record is sitting there that cannot be trusted - which
        is never treated as "nothing owed", because an unreadable journal is exactly
        the case where a device may have been left disabled.
        """
        try:
            raw = self._journal_path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, ""
        except OSError as e:  # noqa: BLE001
            return None, f"read: {type(e).__name__}: {e}"
        try:
            rec = json.loads(raw)
        except ValueError as e:  # noqa: BLE001
            return None, f"not JSON: {type(e).__name__}"
        if not isinstance(rec, dict):
            return None, "not an object"
        if rec.get("version") != _JOURNAL_VERSION:
            return None, f"version {rec.get('version')!r} is not {_JOURNAL_VERSION}"
        dev = rec.get("device_id")
        if not isinstance(dev, str) or not _is_instance_id(dev):
            return None, "no usable device instance id"
        if rec.get("phase") not in (RecoveryPhase.DISABLING, RecoveryPhase.DISABLED,
                                    RecoveryPhase.ENABLING):
            return None, f"unknown phase {rec.get('phase')!r}"
        return rec, ""

    def _pending_recovery(self) -> str:
        """The device a recovery still owes an enable, or "" when nothing is owed."""
        rec, _ = self._read_record()
        return rec["device_id"] if rec else ""

    def _record_phase(self, dev: str, phase: str) -> str:
        """Move the journal to the next phase, keeping the time the cycle started.

        A failed write is reported and never swallowed: past this point this process
        may be the only thing that knows the device is off the bus.
        """
        rec, _ = self._read_record()
        if not rec or rec.get("device_id") != dev:
            rec = self._new_record(dev, phase)
        else:
            rec["phase"] = phase
            rec["port"] = self.port or rec.get("port", "")
        rec["written_at"] = self._stamp()
        rec["pid"] = os.getpid()
        reason = self._write_record(rec)
        if reason:
            self.log(f"[panel] the recovery journal for {dev} could not be updated "
                     f"({reason}) - the device may be left disabled with nothing on "
                     f"disk saying so")
        return reason

    def _clear_journal(self, dev: str) -> None:
        """The debt is paid: the device node itself says it is no longer disabled.

        Only `_device_state` gets to decide that. pnputil's wording never does - it
        exits 0 when it refuses, and on a localized system the words are not English.
        """
        try:
            self._journal_path().unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001
            self.recovery_error = f"clear: {type(e).__name__}: {e}"
            self.log(f"[panel] {dev} is back but the recovery record could not be "
                     f"removed ({self.recovery_error}); it is re-checked and cleared "
                     f"again on the next bring-up")
            return
        self.recovery_pending = ""
        self.log(f"[panel] {dev} confirmed enabled again - recovery record cleared")

    def _quarantine(self, path: Path, why: str) -> None:
        """Move a record that cannot be read aside, instead of deleting it.

        An unreadable journal is the exact case where a device may have been left
        disabled, so its bytes stay on disk for a person; renaming it is what stops the
        same unreadable file being re-read on every bring-up, and it says in the name
        that something here was not finished.
        """
        aside = path.with_name(path.name + ".unreadable")
        self.recovery_error = f"{path.name}: {why}"
        try:
            os.replace(path, aside)
        except OSError as e:  # noqa: BLE001
            self.recovery_error = f"quarantine: {type(e).__name__}: {e}"
            return
        self.log(f"[panel] the recovery record at {path} could not be read ({why}); it "
                 f"is kept as {aside.name} rather than deleted. If the panel's USB "
                 f"device is disabled in Device Manager, enable it - a disabled device "
                 f"stays disabled through a reboot.")

    def _adopt_legacy_mark(self) -> None:
        """Carry in the journal from before it had a stable home.

        `.panel_reset_pending` sat in the working directory, so a debt recorded by a
        run started from elsewhere was invisible to the run that had to finish it. An
        old file is adopted rather than dropped: it is the only memory that a device is
        sitting disabled, and it holds a bare id with no phase, which is read as the
        cautious one - confirmed disabled.
        """
        for legacy in _legacy_mark_paths():
            try:
                dev = legacy.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not dev:
                try:
                    legacy.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if not _is_instance_id(dev):
                self._quarantine(legacy, f"{dev!r} is not a device instance id")
                continue
            owed = self._pending_recovery()
            if owed and owed != dev:
                self._quarantine(legacy, f"{owed} is already owed an enable")
                continue
            if not owed:
                # Migration is a transaction, and the legacy file is the *only* memory
                # that a device may be sitting disabled — so it is removed only once the
                # new journal is durably on disk. `_write_record` returns "" on success
                # and the reason on failure; discarding that answer used to mean a failed
                # atomic replace (a permission problem, a full disk, an antivirus lock)
                # logged "adopted" and then deleted the legacy marker, leaving a disabled
                # device with no record anywhere that it needs enabling. On failure the
                # legacy file is kept for the next bring-up, which retries the migration
                # by doing the same thing again.
                reason = self._write_record(
                    self._new_record(dev, RecoveryPhase.DISABLED))
                if reason:
                    self.log(f"[panel] could not adopt the recovery record for {dev} "
                             f"from {legacy} ({reason}) - leaving that file in place so "
                             f"the next start can finish the migration")
                    continue
                self.log(f"[panel] adopted the recovery record for {dev} from {legacy} "
                         f"into {self._journal_name()}")
            try:
                legacy.unlink(missing_ok=True)
            except OSError:
                pass

    def _enable_and_confirm(self, dev: str, tries: int) -> tuple[bool, str]:
        """Ask for the enable, and let the device node - not pnputil - say whether it took.

        (True, ...) means Windows says the device is off the disabled list. False means
        the record stays: the enable either failed or its effect could not be read, and
        both leave a device that might still be off the bus.
        """
        last = "not attempted"
        for attempt in range(max(1, tries)):
            self._record_phase(dev, RecoveryPhase.ENABLING)
            ok, last = self._pnputil("/enable-device", dev)
            if not ok:
                self.recovery_error = f"enable: {last}"
            state = self._device_state(dev)
            if state in ("on", "degraded"):
                return True, last
            if attempt + 1 < tries:
                time.sleep(_BUS_SETTLE_S)     # re-enumeration is not instant
        return False, last

    def _disable_and_enable(self, dev: str) -> tuple[bool, str]:
        """Disable, then re-enable: take the device off the bus and put it back.

        `/restart-device` re-initialises the driver stack while the device stays
        claimed; disabling tears it down and re-enabling re-enumerates it. That is the
        strongest thing software can do to the port, and it is *not* an electrical power
        cycle - a self-powered hub keeps the panel's rail up while its port is disabled
        - so it is reported as what it is, and it is never counted as a fix on its own.

        The two words are not one action, and the gap between them is the whole risk: a
        device left disabled stays disabled **through a reboot**. So the intent is
        committed to the journal before the first pnputil call, and the cycle is refused
        outright when that write fails - a declined escalation is a worse evening than
        one we cannot finish. Nothing deletes the record except the device node saying
        it is no longer disabled, and the finally below makes sure a raise part-way
        through leaves the record behind instead of clearing it.
        """
        if not _is_instance_id(dev):
            return False, f"refusing to touch {dev!r}: not a usable device instance id"
        owed = self._pending_recovery()
        if owed:
            return False, (f"{owed} is still owed an enable from an earlier attempt; "
                           f"finishing that comes before another device cycle")
        reason = self._write_record(self._new_record(dev, RecoveryPhase.DISABLING))
        if reason:
            self.log("[panel] NOT disabling the device: the recovery journal could not "
                     f"be written ({reason}). A device left disabled stays disabled "
                     f"through a reboot, so this rung is skipped; the journal belongs "
                     f"at {self._journal_name()}")
            return False, f"journal: {reason}"
        self.recovery_pending = dev
        paid = False                    # the node says the device is no longer disabled
        last = ""
        try:
            ok, last = self._pnputil("/disable-device", dev)
            state = self._device_state(dev)
            if state == "on":
                # The node says it is still enabled: whatever pnputil printed, there is
                # no stopped device to bring back, so nothing is owed and nothing hurt.
                paid = True
                return False, f"disable did not take effect ({last})"
            # off / degraded / absent / unknown: assume the stop happened. Enabling a
            # device that was never stopped is a no-op; missing one that was is the bug.
            if not ok:
                # pnputil said no and the node has not contradicted it - the device may
                # already have been stopped by something else. The enable is owed to it
                # either way, so this is a note about the wording and not a reason to
                # stop here and leave the device down.
                self.recovery_error = f"disable: {last}"
            self._record_phase(dev, RecoveryPhase.DISABLED)
            self.log(f"[panel] {dev} disabled for a bus cycle - the recovery record at "
                     f"{self._journal_name()} stays until the device is confirmed back")
            time.sleep(_BUS_SETTLE_S)        # the hub needs a moment to drop it
            paid, last = self._enable_and_confirm(dev, _ENABLE_TRIES)
            return paid, last
        finally:
            if paid:
                self._clear_journal(dev)
            else:
                self._record_phase(dev, RecoveryPhase.DISABLED)
                self.recovery_pending = dev
                self.log(f"[panel] COULD NOT CONFIRM {dev} is enabled again ({last}) - "
                         f"the recovery record stays at {self._journal_name()} and the "
                         f"next bring-up retries. Device Manager -> USB -> enable it, "
                         f"or replug the screen. A disabled device stays disabled "
                         f"through a reboot.")

    def _reconcile_recovery(self) -> bool:
        """Finish a recovery that was started earlier - possibly by a process that died.

        Reached from every bring-up, which is what makes a crash survivable: the record
        is on disk before the disable, so the debt outlives whoever incurred it, and
        this is where it gets paid. One enable per call, because the caller's own retry
        cadence is the repetition - a device that needs a dozen tries is asked a dozen
        times over a dozen attempts instead of in one blocking burst.

        True means nothing is owed. False means the record is still there, and while it is
        the reason is logged again every `_RECONCILE_LOG_S` and `summary()` carries the
        device id: a half-finished recovery must not be a secret kept from whoever ends
        up reading the log.
        """
        self._adopt_legacy_mark()
        rec, why = self._read_record()
        if rec is None:
            if why:
                try:
                    self._quarantine(self._journal_path(), why)
                except OSError as e:  # noqa: BLE001 - nowhere to put it, so say so
                    self.recovery_error = f"state directory: {type(e).__name__}: {e}"
            self.recovery_pending = ""
            return not why
        dev = rec["device_id"]
        self.recovery_pending = dev
        state = self._device_state(dev)
        if state in ("on", "degraded"):
            self._clear_journal(dev)
            return True
        ok, last = self._enable_and_confirm(dev, 1)
        if ok:
            self._clear_journal(dev)
            return True
        now = time.monotonic()
        if now - self._last_reconcile_log > _RECONCILE_LOG_S:
            self._last_reconcile_log = now
            self.log(f"[panel] still owes {dev} an enable (record says {rec['phase']}, "
                     f"written {rec.get('written_at', '?')}, device reads {state}): "
                     f"{last} - keeping {_STATE_FILE_NAME} and asking again on every "
                     f"bring-up")
        return False

    def close(self) -> None:
        """Give the port back, and refuse to hand it to anybody again.

        `_closing` is the fence that makes this mean something: a builder that is
        mid-bring-up right now will reach `_publish`, find the fence up, and dispose of
        the driver it just opened instead of publishing a live connection into a process
        that is on its way out. Waiting for that builder here instead would be easier to
        read but is not available: a bring-up against a wedged endpoint takes tens of
        seconds, and shutdown must not be held up by a device that is not answering.
        """
        with self._lock:
            self._closing = True
        # Bounded for the same reason the build does it bounded: this runs on the exit
        # path of a process whose whole problem has been calls that do not return.
        if not _release(self._retire()):
            self.log("[panel] the port was still held at shutdown — the next process to "
                     "open it may have to wait for the driver to let go")

    def summary(self) -> str:
        state = "up" if self.ok else f"down({self.down_reason or 'never built'})"
        s = (f"panel={state} rev={self.rev} port={self.port or '-'} "
             f"bright={self._brightness} screen={self._screen_on} gen={self._gen}")
        if self.rebuilds:
            s += f" rebuilds={self.rebuilds}"
        if self.abandoned:
            # Each one is a worker we could not cancel; the count is how much of that
            # debt the process is carrying, and it is the reason a handle is disposed
            # before it is replaced.
            s += f" abandoned={self.abandoned}"
        if self.usb_restarts:
            s += f" usb_restarts={self.usb_restarts}"
        elif self.usb_restart_error:
            s += f" usb_restart={self.usb_restart_error}"
        if self.recovery_pending:
            s += f" recovery_pending={self.recovery_pending}"
        elif self.recovery_error:
            s += f" journal={self.recovery_error}"
        if self.usb_restart_refused:
            s += f" usb_refused={self.usb_restart_refused[:70]}"
        if self.errors or self.slow_writes:
            s += f" errors={self.errors} slow={self.slow_writes}"
        return s
