"""The panel as a link that can fail — because it does, on this desk, for real.

Everything the app draws goes through one 115200-baud CDC-ACM port on a board
that is power-cycled by the PC's USB bus. Three failure shapes have actually
been seen in log.log:

  * the port vanishes across a sleep/resume, and the vendor driver's own recovery
    (`WriteLine` → close → `openSerial`) gives up after 10 tries by calling
    `sys.exit(0)` — and `os._exit(0)` if that raises. A missing screen has
    therefore been able to terminate the whole app, which is the "it never came
    back" the panel gets blamed for;
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
path did not have to learn about any of this.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from app.display import _HELLO_WAIT_S, _RESET_WAIT_S, _abandon, ensure_vendor_path

# Written before a `/disable-device` and removed after the matching `/enable-device`.
# A device left disabled stays disabled through a reboot, so the intent has to outlive
# this process: whoever starts next reads this and finishes the job. It sits beside
# log.log because the app's working directory is the project (as it is for the task).
_MARK_FILE = Path(".panel_reset_pending")

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
        self._brightness: int | None = None
        self._screen_on: bool | None = None
        self.needs_full = True       # panel memory is not trustworthy yet
        self._on_relink = None

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
        """Synchronous rebuild — used for a resume, where the caller wants the answer."""
        return self._build_now(reason=reason)

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
        """
        with self._lock:
            if self._closing or self._state in (_READY, _BUILDING):
                return
            self._set_state(_BUILDING)

        def work() -> None:
            try:
                self._build_now(reason=reason)
            finally:
                # `_build_now` clears the claim on every path where it ran. This catches
                # the one where it did not: it could not get the gate, so the attempt now
                # inside it belongs to somebody else and the flag would otherwise stay up
                # forever with nothing left to drop it. An idle `_gate` is the proof that
                # nobody is building, and holding it across the reset means no build can
                # start between the check and the write.
                if self._gate.acquire(blocking=False):
                    try:
                        with self._lock:
                            if self._state == _BUILDING:
                                self._set_state(_CLOSED)
                    finally:
                        self._gate.release()

        threading.Thread(target=work, daemon=True, name="panel-bring-up").start()

    def _build_now(self, first: bool = False, reason: str = "") -> bool:
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
            # Leave the state alone here: `_BUILDING` is true information while another
            # attempt is still inside the gate, and clearing it would invite a third.
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
        if _MARK_FILE.exists():
            # Somebody (probably an earlier us) stopped this device to reset it and did
            # not finish the job. Fix that before complaining that it will not answer.
            self._heal_disabled()
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
                        image_width: int = 0, image_height: int = 0) -> None:
        self.push(image, x, y)

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

    def tick(self) -> bool:
        """Keep the link honest: rebuild it when it is down and due."""
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
        if self._state == _BUILDING:
            return False                      # an attempt is already in flight
        self.start_build()
        if self.ok:
            return True
        # Reopening the port has been failing for a while: the device itself needs a
        # word. pnputil needs Administrator — which the scheduled task has — and when
        # it does not, the answer is logged, not swallowed.
        #
        # `self._state != _BUILDING` is not a nicety: `start_build` above has only
        # *started* its thread, so without this the same tick that began a bring-up
        # would reset the USB device underneath it, yanking the port out from under the
        # attempt that was ten seconds from asking it for HELLO. The ladder waits for
        # the next tick, which is a second away; a build that just failed has already
        # moved the state out of `_BUILDING`, so escalation is delayed by one attempt,
        # never suppressed.
        if (self.cfg["display"].get("usb_restart_on_fail", True)
                and self._state != _BUILDING
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
        """
        if (self._restart_attempts < 3 or self.ok
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

    def _device_id(self, port: str, parent: bool = False) -> str:
        """The PnP instance id of the device that owns `port`, via WMI.

        Only single quotes inside the PowerShell command and no double quotes at all:
        this string is passed as one argv element and every extra quoting layer has to
        survive both Python's join and PowerShell's parser.

        `parent` walks one level up — from the CDC interface (`…&MI_00`) to the
        composite device inside the screen. Restarting the interface re-initialises the
        serial pipe; restarting the parent is the full unplug/replug, which is what the
        panel's MCU needs when the pipe came back but the firmware did not.
        """
        q = (f"$d = Get-CimInstance Win32_PnPEntity -Filter 'Name LIKE \"%({port})%\"' "
             "| Select-Object -First 1")
        if parent:
            # Win32_PnPEntity.Parent is empty for this device's CDC interface, so the
            # parent is derived: strip the `&MI_xx` suffix, escape the backslashes for
            # WQL (a string literal needs them doubled), and LIKE-match what is left.
            # The quote character is built with [char]39 rather than nested, because
            # this string is passed as one argv element through two parsers already.
            # Raw strings throughout: every backslash below is the character PowerShell
            # and WQL are meant to see. Checked against `tools/screen_wake_probe.py
            # --restart --query`, which prints both ids.
            q += (r" ; if ($d) { $b = $d.DeviceID -replace '&MI_[0-9A-F]+\\.*$', '';"
                  r" $e = $b -replace '\\', '\\'; $q = [char]39;"
                  r' $f = "DeviceID LIKE " + $q + $e + "\\%" + $q;'
                  r" $p = Get-CimInstance Win32_PnPEntity -Filter $f | Select-Object -First 1;"
                  r" if ($p) { $d = $p } }")
        ps = q + "; if ($d) { $d.DeviceID }"
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                "-Command", ps], capture_output=True, text=True,
                               timeout=60)
        except Exception as e:  # noqa: BLE001
            self.usb_restart_error = f"query: {type(e).__name__}: {e}"
            return ""
        out = (r.stdout or "").strip()
        return out.splitlines()[-1].strip() if out else ""

    def _usb_restart(self) -> bool:
        """Restart the panel's USB device. The last resort that is not a hand on the cable.

        When the CDC endpoint has stopped draining, nothing can be delivered — not
        even the vendor's own RESTART — so retrying the port forever cannot help;
        measured here with `tools/screen_wake_probe.py`: the port opens, the first
        write blocks. It needs Administrator — the scheduled task has it — and pnputil
        **exits 0 even when it refuses**, so every verdict below is read from its text.
        (`Restart-PnpDevice`, the cmdlet that would do the same, has been removed from
        this build's PnpDevice module.)

        Three rungs, each tried once before stepping up:

        1. restart the CDC interface (`…&MI_00`) — re-initialises the serial pipe;
        2. restart the composite device above it — the software unplug/replug;
        3. disable and re-enable that device — the strongest thing software can do to
           the port, and the only rung that can leave the device *disabled*, so it is
           bracketed by a marker file and a start-up heal (see `_power_cycle`).

        Measured on this desk: the interface restart brought the pipe back (a read
        returned where the write had blocked) but HELLO came back empty — pipe alive,
        MCU not — and the parent restart did not clear it either. So the ladder exists
        to take the fault as far down as it can go and to say plainly, when it runs
        out, that the answer is the cable.
        """
        port = self._port_device()
        if not port:
            self.usb_restart_error = "no COM port known for the panel"
            return False
        parent = self._restart_depth > 0
        dev = self._device_id(port, parent=parent)
        if (not dev or "\\" not in dev) and parent:
            parent, dev = False, self._device_id(port)      # no parent: try the port
        if not dev or "\\" not in dev:
            self.usb_restart_error = f"no device found for {port}"
            self.log(f"[panel] cannot restart the device: {self.usb_restart_error} "
                     f"(is {port} still there?)")
            return False
        self._device = dev              # remembered: the heal path needs an id to fix
        depth = self._restart_depth
        self._restart_attempts += 1     # how long to wait for the next rung
        self._restart_depth += 1        # next time, aim one rung higher
        if depth >= 2:
            ok, line = self._power_cycle(dev)
            did = "power-cycled the panel's USB device"
        else:
            ok, line = self._pnputil("/restart-device", dev)
            did = f"restarted the panel's USB {'device' if parent else 'port'}"
        if ok:
            self.usb_restarts += 1
            self._retry_at = time.monotonic() + 5.0     # the device is re-enumerating
            self.log(f"[panel] {did} ({dev}) — waiting for it to come back")
            return True
        self._restart_depth = 0
        self.usb_restart_error = line
        self.log(f"[panel] device reset did not work ({self.usb_restart_error}) — this "
                 f"needs Administrator; the loop keeps retrying the port")
        return False

    def _pnputil(self, verb: str, dev: str) -> tuple[bool, str]:
        """One pnputil device verb, judged by its output: the exit code lies."""
        try:
            r = subprocess.run(["pnputil", verb, dev], capture_output=True, text=True,
                               timeout=60)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        low = out.lower()
        bad = "fail" in low or "denied" in low or "error" in low
        line = (out.splitlines() or [f"exit {r.returncode}"])[-1][:140]
        return (r.returncode == 0 and not bad), line

    def _power_cycle(self, dev: str) -> tuple[bool, str]:
        """Disable, then re-enable: take the device off the bus and put it back.

        `/restart-device` re-initialises the driver stack while the device stays
        claimed; disabling tears it down and re-enabling re-enumerates it, which is as
        close to pulling the plug as an OS will let a program get.

        The risk is the gap between the two words: a device left disabled stays
        disabled **through a reboot**, which would turn a dark panel into an absent one.
        So the intent is written down before the disable (`_MARK_FILE`) and the enable
        is retried; if even that fails, the log says what to click, because the app has
        then genuinely made the desk worse and must not pretend otherwise.
        """
        ok, line = self._pnputil("/disable-device", dev)
        if not ok:
            return False, f"disable: {line}"      # never re-enable what we did not stop
        try:
            _MARK_FILE.write_text(dev + "\n", encoding="utf-8")
        except OSError:
            pass   # the marker is the safety net; the enable below is the real step
        time.sleep(2.0)                # the hub needs a moment to drop it
        last = line
        for _ in range(3):
            ok, last = self._pnputil("/enable-device", dev)
            if ok:
                try:
                    _MARK_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                return True, last
            time.sleep(2.0)
        self.log(f"[panel] COULD NOT RE-ENABLE the panel's USB device ({dev}): {last} — "
                 f"Device Manager → USB → enable it, or replug the screen. "
                 f"A disabled device stays disabled through a reboot.")
        return False, f"enable: {last}"

    def _heal_disabled(self) -> None:
        """If a previous run left the device disabled (crash between the two words),
        enable it before anything else. The marker file is the whole memory: this may
        be a different process, or the first tick after a boot."""
        try:
            dev = _MARK_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not dev:
            try:
                _MARK_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.log(f"[panel] a previous run left {dev} disabled — enabling it")
        ok, line = self._pnputil("/enable-device", dev)
        if ok:
            try:
                _MARK_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            self.log("[panel] device re-enabled")
        else:
            self.log(f"[panel] still could not enable {dev}: {line}")

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
        if self.errors or self.slow_writes:
            s += f" errors={self.errors} slow={self.slow_writes}"
        return s
