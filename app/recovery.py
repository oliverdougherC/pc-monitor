"""Recovery as a request, so the render loop never waits for a screen.

Three different things have been called "a resume" here, and the app treated all three
as the first of them:

  * **the machine stopped and came back** - the COM port is gone or the panel rebooted
    into portrait, the ETW session is a husk, and the game we locked onto was frozen
    mid-frame. This one really does need everything downstream re-made.
  * **the display or session changed shape** - a monitor came back on, somebody
    unlocked the session, a resolution changed. `WM_DISPLAYCHANGE` in particular
    arrives whenever a second monitor is plugged in or a driver re-modes a display,
    which says nothing at all about the panel.
  * **the panel itself came back** - replugged, re-enumerated, awake after the USB bus
    finished whatever it was doing.

The expensive work was also done *inside* the loop. `PanelLink.relink()` holds the
link's lock through a whole bring-up (auto-detect, a HELLO bounded at 8 s, sometimes a
reset and a second HELLO), and `PanelLink.tick()` spends up to minutes in `pnputil`
once the device ladder starts walking - 60-second subprocess deadlines, several rungs.
So one spurious `WM_DISPLAYCHANGE` could stop the render loop for tens of seconds, and
the loop's own watchdog reads a long tick gap as a suspend: a display event used to
manufacture the very resume it was being recovered from.

Here, recovery is something the loop *asks for*:

    d = recovery.tick(lit=not plan.dark)

  * `d.wake` - the machine was stopped; restart the capture and drop the locked game
    target. Fires once per burst, whatever the burst contained.
  * `d.repaint` - the next frame has to be whole. The only thing a display event costs.
  * `d.holds_light` - do not light the panel or push a frame this tick: whatever this
    tick would send was composed before the event that started the rebuild.

The work that can block runs on one worker thread, and a burst of events collapses into
one pass: `request()` only bumps a generation, and the pass carries whatever the burst
had become by the time it was made. The light state is handed in every tick rather than
remembered from the moment of the event, so a rebuild that finishes late applies what
is wanted *now* - including "dark", which is the point of an unattended 3 a.m. wake: a
bring-up is a visible event on this hardware (the panel's MCU comes up at its own
default brightness), so a wake that arrives while the panel is meant to be dark defers
the rebuild until there is something to show.

The link is only re-made when the evidence says it is not working. A healthy link is
kept and merely repaints: `PanelLink` marks itself down on the first write that fails,
and the next pass rebuilds it, which costs one tick and does not reboot a screen that
was fine.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# What the event is evidence *of*. `WAKE` is the only kind that reaches for the
# expensive work; `REFRESH` costs a whole frame and nothing else.
WAKE = "wake"
REFRESH = "refresh"
ARRIVAL = "arrival"         # the device came back on the bus: do not wait for the clock

# How long the loop may be told "not yet" while a rebuild is outstanding. A panel that
# is not answering will not answer in 20 seconds either, and the render loop must not
# inherit the patience of the recovery ladder.
_HOLD_MAX_S = 20.0
# How often the worker looks at the link. `PanelLink.tick()` decides for itself whether
# a rebuild is due; this is only how often somebody asks it.
_MAINTENANCE_S = 1.0


@dataclass
class Directive:
    """What the loop is being asked to do this tick, decided by the coordinator."""
    wake: str | None = None         # the machine stopped: re-make capture and target
    repaint: bool = False           # the next frame must be whole
    holds_light: bool = False       # no brightness command and no push this tick
    deferred: bool = False          # a rebuild is waiting for the panel to be wanted


def live_ports() -> set[str] | None:
    """The serial ports Windows can see right now, or None where that is unanswerable.

    Only used to notice that a device *arrived*. None - pyserial missing, or the setup
    call refusing - means "cannot tell", and the link's own retry clock decides.
    """
    try:
        from serial.tools import list_ports
        return {p.device for p in list_ports.comports()}
    except Exception:  # noqa: BLE001 - an unreadable bus is not a fault to report hourly
        return None


class Recovery:
    """One worker thread, one coalesced generation, and nothing blocking in the loop."""

    def __init__(self, panel, log=print, list_ports=live_ports,
                 cadence_s: float = _MAINTENANCE_S, hold_max_s: float = _HOLD_MAX_S,
                 threaded: bool = True) -> None:
        self.panel = panel
        self.log = log
        self._list_ports = list_ports
        self._cadence = cadence_s
        self._hold_max = hold_max_s
        self._threaded = threaded
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Generations, not counters of events: `asked` is what has happened, `done` how
        # far a pass has caught up with it. A burst moves `asked` five times and one
        # pass moves `done` to wherever it stood when that pass was made.
        self._asked = 0
        self._done = 0
        self._refresh_asked = 0
        self._refresh_done = 0
        self._reasons: list[str] = []      # the tail of the burst, for the log line
        self._need_build = False           # a wake/arrival wants the link re-made
        self._urgent = False               # ... and must not wait for the light
        self._building: int | None = None  # `panel.rebuilds` we are waiting to move
        self._asked_at = 0.0
        self._stood_down = False               # gave up; the next event re-arms it
        self._lit = True
        self._deferred = False
        self._seen_ports: set[str] | None = None
        self._next_port_poll = 0.0
        self._faults: dict[str, int] = {}
        self._fault = ""
        self.passes = 0                    # rebuilds this coordinator asked for
        self.repaints = 0
        self.coalesced = 0                 # events that folded into another one's pass
        self.holds_light = False

    # ------------------------------------------------------------------ loop side
    def request(self, kind: str, reason: str) -> None:
        """Note what happened. Called from the loop: it does no work and cannot block.

        A refresh that lands while a wake is still outstanding folds into that wake's
        pass, and an arrival folds into whatever is pending: the expensive work is
        already going to happen, and doing it twice in a row is how a burst of
        notifications used to become thirty seconds of frozen panel.
        """
        with self._cond:
            self._reasons.append(reason)
            del self._reasons[:-6]            # the log quotes the tail of the burst
            # A new event is what makes this coordinator urgent again after it decided
            # to stop waiting for a panel that would not answer.
            self._stood_down = False
            if kind == REFRESH:
                self._refresh_asked += 1
                # Not evidence that the link died - but if it is already down, this
                # event wants a frame on it and the link's own retry clock may be a
                # minute away. A working link is left alone either way.
                if not self.panel.ok:
                    self._need_build = True
            else:
                self._asked += 1
                self._need_build = True
                if kind == ARRIVAL:
                    self._urgent = True       # a hand just went on the cable
            self._cond.notify_all()

    def tick(self, lit: bool) -> Directive:
        """Consume the coalesced events and ask for whatever they add up to.

        Cheap by construction: the only call into the panel is `start_build()`, which
        returns at once (and which the link coalesces against itself), and the pass
        landing is noticed from the link's own rebuild counter instead of by waiting on
        anything.
        """
        self._start_once()
        start: str | None = None
        with self._cond:
            self._lit = lit
            self._landed()
            self._expired()
            if self._stood_down and self.panel.ok:
                # Maintenance got there first, which is the normal ending: the ladder
                # kept working while the loop went back to drawing.
                self._stood_down = False
                self.log("[recovery] the panel came back on maintenance - the loop "
                         "never had to wait for it")
            wake = None
            repaint = False
            # One pass per burst: whatever is pending is taken together, so five
            # notifications arriving inside a second are one directive and one rebuild
            # rather than five of each. The wake half dominates, because it is the one
            # that says the capture and the game target went stale along with the link.
            pending_wake = self._asked - self._done
            pending_refresh = self._refresh_asked - self._refresh_done
            n = pending_wake + pending_refresh
            if pending_wake:
                self._done = self._asked
                self._refresh_done = self._refresh_asked
                self.coalesced += max(0, n - 1)
                wake = self._burst(n)
                repaint = True               # it may have rebooted: paint all of it
            elif pending_refresh:
                self._refresh_done = self._refresh_asked
                self.coalesced += max(0, n - 1)
                repaint = True
                self.repaints += 1
            if self._need_build and self.panel.ok:
                self._need_build = False     # it answers: there is nothing to rebuild
            start = self._claim_locked(self._reasons[-1] if self._reasons else "recovery")
            self.holds_light = self._building is not None
            out = Directive(wake=wake, repaint=repaint, holds_light=self.holds_light,
                            deferred=self._deferred)
        if start is not None:
            self._begin(start)
        return out

    def summary(self) -> str:
        """For the beat line: only says something once recovery has actually happened."""
        if not (self.passes or self.coalesced or self.holds_light or self._deferred
                or self._faults):
            return ""
        s = f"recovery={self.passes} pass{'es' if self.passes != 1 else ''}"
        if self.coalesced:
            s += f" coalesced={self.coalesced}"
        if self.holds_light:
            s += " holding"
        if self._deferred:
            s += " deferred(dark)"
        if self._faults:
            extra = f"+{len(self._faults) - 1}" if len(self._faults) > 1 else ""
            s += f" fault={self._fault[:36]}{extra}"
        return s

    def close(self) -> None:
        """Stop the worker. Does not wait for a `pnputil` call that is still running."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=0.2)
        self._thread = None

    # ---------------------------------------------------------------- worker side
    def maintenance(self) -> None:
        """The side that is allowed to block. Runs on the worker, not in the loop.

        `PanelLink.tick()` is where a rebuild attempt happens and, after two minutes of
        silence, where a device verb gets handed to `pnputil` - up to several 60-second
        subprocess deadlines in a row. It used to sit in the render loop once a tick,
        and that is the other half of why recovery could stop the panel for minutes.
        Nothing in the loop waits for this, and this waits for nothing in the loop.

        Which pass runs is the loop's decision, not this thread's. `tick(lit=...)`
        publishes the current intent on `_lit`, and a dark pass is
        `panel.tick(reconnect_only=True)`: the retry clock and the device ladder keep
        working - a device left disabled through a night is a worse morning than a
        panel that reconnects a minute late - but no bring-up starts, because on this
        hardware a bring-up raises brightness and wakes the screen whatever the app
        wants. The original #47 called the plain `tick()` here, and that was the
        defect: the loop had deferred the rebuild through `_claim_locked`, and this
        thread did it anyway a second later, with no consultation of the host state,
        the lock, or the night plan. One authority - the loop decides when the panel
        may illuminate, and what maintenance adds is only that the deferral has to
        survive being handed to another thread.
        """
        with self._cond:
            lit = self._lit
        try:
            if lit:
                self.panel.tick()
                if getattr(self.panel, "pending_setup", False):
                    # Deferred while dark, and the desk is lit now: the flag is the
                    # coordinator's to clear, and `authorize_light()` is the only
                    # door back through it.
                    self.panel.authorize_light()
            else:
                self.panel.tick(reconnect_only=True)
        except BaseException as e:  # noqa: BLE001 - SystemExit is a vendor habit
            self._note_fault(f"panel maintenance {type(e).__name__}: {e}")
        try:
            self._watch_arrival()
        except BaseException as e:  # noqa: BLE001
            self._note_fault(f"arrival watch {type(e).__name__}: {e}")

    def _watch_arrival(self) -> None:
        """Retry the moment the device comes back, instead of on the retry clock.

        A replug is the one recovery event that is not delivered to us: while the link
        is down the port is simply absent, and the only way to learn it is back is to
        look. The link's own cadence is ten seconds, or a minute once the device ladder
        has been walked - right for a panel that stays missing, wrong for the second
        somebody has just put the cable back in.
        """
        if self.panel.ok:
            self._seen_ports = None          # next time it is down, baseline again
            return
        now = time.monotonic()
        if now < self._next_port_poll:
            return
        self._next_port_poll = now + self._cadence
        present = self._list_ports()
        if present is None:
            return
        seen, self._seen_ports = self._seen_ports, set(present)
        if seen is None:
            return                           # a baseline is not an arrival
        arrived = sorted(set(present) - seen)
        if not arrived:
            return
        port = str(getattr(self.panel, "port", "") or "")
        # A COM number is not an address - `app/panel.py` says so at length, and the
        # device store lesson behind it is that Windows hands those numbers out again
        # whenever it likes. So "our number came back" is not the test for "our screen
        # came back": a replug that re-enumerates as a different number is the
        # ordinary case, and refusing to notice it is how a cable put back at midnight
        # waits until the slow retry clock. The honest predicate is about where we
        # left the panel: if the port we last failed on is still sitting there
        # untouched, whatever just appeared is somebody else's phone, and the
        # wedged-endpoint case belongs to the device ladder, not to arrivals. If that
        # port is gone and a number appeared, the new one may be ours - and a rebuild
        # attempt that turns out to be for nothing costs one coalesced bring-up.
        if port.startswith("COM") and port not in arrived and port in present:
            return                           # our port never left: not our screen
        names = ", ".join(arrived)
        lost = "" if (not port.startswith("COM") or port in arrived) else \
            f" ({port} is gone, so this may be it under a new number)"
        self.log(f"[recovery] {names} is back on the bus{lost} - retrying now instead of "
                 f"waiting for the retry clock")
        # Ask here rather than waiting for the loop's next tick: the whole point of
        # watching is that the person who just replugged the cable is not waiting on a
        # ten- or sixty-second retry cadence. `_claim_locked` still refuses if a pass
        # is already in flight, so this cannot start a second rebuild.
        self.request(ARRIVAL, f"panel arrived ({names})")
        self._try_start(f"panel arrived ({names})")

    # ------------------------------------------------------------------ internals
    def _claim_locked(self, reason: str) -> str | None:
        """Is now the moment to ask for a rebuild? Called with the lock held.

        Three things have to be true: something asked, nothing is already in flight,
        and the panel is not answering. The fourth is the one that preserves intent -
        a rebuild is a visible event on this hardware, so unless something is waiting
        to be shown (or a hand just went on the cable) it waits.
        """
        if self._building is not None or self._stood_down or self.panel.ok:
            return None
        if not self._need_build:
            return None
        if not (self._lit or self._urgent):
            if not self._deferred:
                self._deferred = True
                self.log("[recovery] rebuild waiting - the panel is meant to be dark, "
                         "and a bring-up lights this hardware up whatever the app "
                         "wants; it runs as soon as there is something to show")
            return None
        self._urgent = False
        self._deferred = False
        self._need_build = False
        self._building = self.panel.rebuilds
        self._asked_at = time.monotonic()
        self.passes += 1
        return reason

    def _try_start(self, reason: str) -> None:
        """The same decision from a thread that is not the loop's (the arrival watch).

        The landing and the expiry are observed here as well, not left for the loop's
        next tick. `_building` holds the rebuild counter the pass started from, so a
        pass that has already landed still refuses the claim until somebody calls
        `_landed()` - and if the only caller that notices landings is the loop, an
        arrival on the worker gets turned away behind a flag whose news nobody has
        picked up yet. That is the retry clock hiding inside the one event that is
        supposed to escape it. These observations are idempotent, so both threads
        making them is not a second authority - the intent still comes only from
        `tick(lit=...)`.
        """
        with self._cond:
            self._landed()
            self._expired()
            start = self._claim_locked(reason)
        if start is not None:
            self._begin(start)

    def _begin(self, reason: str) -> None:
        """Ask the link to rebuild itself. Non-blocking, and the link coalesces it."""
        try:
            self.panel.start_build(reason)
        except BaseException as e:  # noqa: BLE001
            with self._cond:
                self._building = None
                self._need_build = True
            self._note_fault(f"start_build {type(e).__name__}: {e}")

    def _landed(self) -> None:
        """Did the rebuild we asked for finish? Called with the lock held."""
        if self._building is None:
            return
        if self.panel.rebuilds == self._building and not self.panel.ok:
            return                            # still trying
        self._building = None
        self._deferred = False
        n = len(self._reasons)
        self.log(f"[recovery] panel answered again - one pass covered {n} event"
                 f"{'s' if n != 1 else ''} ({self._reasons[-1] if n else '-'})")

    def _expired(self) -> None:
        """Stop holding the light for a panel that is not going to answer. Locked.

        Standing down, not retrying: re-asking the moment the hold expires would put
        the loop back on hold every `_hold_max` seconds forever, which is the same
        stall with a new cadence. From here the link's own ladder - maintenance, and
        the device rungs behind it - is what brings the screen back, and the next
        *event* is what makes this coordinator urgent again.
        """
        if self._building is None or time.monotonic() - self._asked_at < self._hold_max:
            return
        self._building = None
        self._stood_down = True
        self.log("[recovery] the panel has not answered the rebuild - the loop is not "
                 "waiting for it; maintenance keeps retrying the port")

    def _burst(self, n: int) -> str:
        """One sentence on what just arrived, for the `[resume]` line."""
        why = self._reasons[-1] if self._reasons else "wake"
        return f"{why} ({n} event{'s' if n != 1 else ''} coalesced)" if n > 1 else why

    def _note_fault(self, what: str) -> None:
        """Say each distinct fault once: the worker runs on a one-second cadence.

        Counted rather than remembered, because two different faults can alternate
        (the port refusing and the maintenance pass raising) and a "last one" string
        would re-announce both of them every second forever.
        """
        n = self._faults.get(what, 0) + 1
        self._faults[what] = n
        self._fault = what
        if n == 1:
            self.log(f"[recovery] {what} - the loop keeps running without it")

    def _start_once(self) -> None:
        """The worker thread, started on the first tick so it cannot outlive start-up."""
        if not self._threaded or self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="panel-recovery")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._cadence):
            self.maintenance()
