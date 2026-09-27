"""One owner for the panel port, proved without the vendored library.

    .venv\\Scripts\\python tools\\panel_owner_selftest.py

`app/panel.py` owns a COM port that only one handle may hold. The failure it exists to
guard against is not the write that raises — that one is easy — but the work that
outlives its own deadline: a timeout in Python stops *us* waiting, and the thread we
were waiting on is still running, still holding the driver it was given, and still able
to call `openSerial()` and take the port back. Two owners of one port is what turns a
recoverable replug into a screen that never comes back, and none of it is visible from
outside the process.

Claims like that cannot be settled by reading the code, and cannot be provoked on the
panel that is plugged in — so everything here runs against a fake driver bolted to a
fake port that keeps the only two ledgers that matter:

  * how many drivers are open at the same moment, and the high-water mark of that;
  * how many handshakes are in flight at the same moment, and its high-water mark.

If either goes above one, the link has had two owners, whatever else it managed to do.
The fake driver also has the vendor's worst habit — `WriteLine`'s fault path calls
`openSerial()`, so a closed port is only half of a retired driver — because that is the
half a plain `close()` does not fix.

Needs neither the vendored library nor the theme fonts: the two names the link touches
(`library.lcd.lcd_comm.Orientation`, and the class named in `app.display._CLS`) are
supplied as fake modules, so this gates on CI instead of skipping there. The frame is a
sentinel object, so it needs no PIL either.

Runs in a temporary directory so the reset-marker file has somewhere to go.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types

os.chdir(tempfile.mkdtemp(prefix="panelowner-"))     # before the app imports
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                             # absolute: the cwd has moved

from app import config as cfgmod          # noqa: E402
from app import display as disp           # noqa: E402
from app import panel as panel_mod        # noqa: E402
from app.panel import PanelLink           # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []

# What a "frame" is here: `push` only ever hands it to the driver, so nothing below has
# to decode a pixel, and neither PIL nor the vendored image path is dragged in.
THE_FRAME = object()


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def raises(exc, fn, *a) -> bool:
    """Did calling `fn(*a)` raise exactly this kind of thing?"""
    try:
        fn(*a)
    except exc:
        return True
    except BaseException:  # noqa: BLE001 - the answer to this question is yes or no
        return False
    return False


def wait_for(pred, timeout: float = 15.0) -> bool:
    """Poll `pred` until it holds. Says whether it ever did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def say(msg: str) -> None:
    print(f"    | {msg}")


# ------------------------------------------------------------------ the fake device
class FakeSerial:
    """Stand-in for the pyserial handle `_bring_up` and `_apply_setup` harden."""

    write_timeout = None


class FakePort:
    """The pretend CDC-ACM port. Two ledgers, and they are the whole test.

    A driver counts as open from its constructor, because that is when the real one
    calls `openSerial()`. `peak_open` and `peak_hello` are high-water marks rather than
    current values: an instantary second owner is still a second owner, and it is
    exactly the kind that only exists for the length of a race.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.all: list = []          # every driver ever built here
        self.held: list = []         # the ones that currently believe they have the port
        self.peak_open = 0
        self.hello = 0               # handshakes in flight right now
        self.peak_hello = 0
        self.next_behaviour: dict = {}   # applied to the next driver built

    def reset(self) -> None:
        """Forget everything: each case judges one link, not the sum of the run."""
        self.__init__()

    def _born(self, d) -> None:
        """A driver exists, so it has the port: the real one opens it in its constructor."""
        with self.lock:
            self.all.append(d)
            self._note_open(d)

    def _note_open(self, d) -> None:
        if d not in self.held:
            self.held.append(d)
        self.peak_open = max(self.peak_open, len(self.held))

    def _note_closed(self, d) -> None:
        if d in self.held:
            self.held.remove(d)

    def open_count(self) -> int:
        with self.lock:
            return len(self.held)

    def hello_enter(self) -> None:
        with self.lock:
            self.hello += 1
            self.peak_hello = max(self.peak_hello, self.hello)

    def hello_exit(self) -> None:
        with self.lock:
            self.hello -= 1

    def __str__(self) -> str:
        return (f"drivers={len(self.all)} open={self.open_count()} "
                f"peak_open={self.peak_open} peak_hello={self.peak_hello}")


class FakeLcd:
    """A driver for the pretend port, with the vendor's two worst habits.

    Only the surface `PanelLink` and `app.display._bring_up` actually touch is here.
    The habits are the point: `openSerial()` re-claims the port whenever anything calls
    it (which is what `lcd_comm.py`'s write-failure path does), and every method runs on
    whichever thread the link happened to hand it — possibly long after that link
    stopped caring about the answer.
    """

    def __init__(self, com_port, display_width, display_height) -> None:
        self.port = PORT                     # the one pretend port this module drives
        self.com_port = com_port
        self.display_width = display_width
        self.display_height = display_height
        self.orientation = None
        self.lcd_serial = FakeSerial()
        self.n = len(PORT.all) + 1
        self.closed = False
        self.writes = 0
        self.woke = 0                # a gated write got past its gate
        self.orient_sets = 0
        self.brightness = None
        self.screen_on = None
        self.write_fails = False     # the endpoint has stopped taking frames
        self.write_gate = None       # an Event: set it to let a blocked write finish
        self.hello_gate = None       # an Event: set it to let a blocked handshake finish
        self.orient_gate = None
        self.orient_fails = False    # answers HELLO, then refuses every command
        self.reset_gate = None
        # A case configures the driver it is about to cause to be built, because a
        # rebuild is precisely the thing a case cannot reach in from the outside.
        self.__dict__.update(PORT.next_behaviour)
        PORT.next_behaviour = {}
        PORT._born(self)

    def __repr__(self) -> str:       # for the FAIL lines
        return f"<FakeLcd #{self.n}{' closed' if self.closed else ''}>"

    # ---------------------------------------------------------------- handshake
    def InitializeComm(self) -> None:                    # noqa: N802
        self.port.hello_enter()
        try:
            if self.hello_gate is not None:
                self.hello_gate.wait(30)
        finally:
            self.port.hello_exit()

    def Reset(self) -> None:                             # noqa: N802
        if self.reset_gate is not None:
            self.reset_gate.wait(30)

    # ----------------------------------------------------------------- commands
    def SetOrientation(self, orient) -> None:            # noqa: N802
        self.orient_sets += 1
        if self.orient_gate is not None:
            self.orient_gate.wait(30)
        if self.orient_fails:
            raise RuntimeError("panel refused the orientation command")
        self.orientation = orient

    def SetBrightness(self, pct: int) -> None:           # noqa: N802
        self.brightness = pct

    def ScreenOn(self) -> None:                          # noqa: N802
        self.screen_on = True

    def ScreenOff(self) -> None:                         # noqa: N802
        self.screen_on = False

    def DisplayPILImage(self, image, x: int, y: int) -> None:   # noqa: N802
        if self.write_gate is not None:
            self.write_gate.wait(30)
        self.woke += 1
        if self.write_fails or self.closed:
            # `lcd_comm.WriteLine` does not simply give up on a failed write: it calls
            # `openSerial()` and tries again. So a driver that was closed under it can
            # take the port back a moment later, while the *next* driver is opening it.
            self.openSerial()
            raise RuntimeError("endpoint gone; the driver tried to reopen the port")
        self.writes += 1

    def get_width(self) -> int:                          # noqa: N802
        return self.display_height

    def get_height(self) -> int:                         # noqa: N802
        return self.display_width

    # ----------------------------------------------------------- the port itself
    def closeSerial(self) -> None:                       # noqa: N802
        self.port._note_closed(self)
        self.closed = True

    def openSerial(self) -> None:                        # noqa: N802
        self.port._note_open(self)
        self.closed = False


PORT = FakePort()


def install_fake_vendor() -> None:
    """Teach the link to build our fake driver, using only names it already uses.

    `_apply_setup` names `library.lcd.lcd_comm.Orientation`, and `_build_owned` reaches
    the driver class through `app.display._CLS`. Those two are the entire surface, so
    faking them in `sys.modules` — which wins over any sys.path entry — is enough to run
    the real `PanelLink` against a pretend panel. Nothing inside the link is faked:
    every rule under test here is the production one.
    """
    lib = types.ModuleType("library")
    lib.__path__ = []                                # package-ish, for `from … import`
    sub = types.ModuleType("library.lcd")
    sub.__path__ = []
    comm = types.ModuleType("library.lcd.lcd_comm")

    class Orientation:
        LANDSCAPE = "landscape"
        PORTRAIT = "portrait"

    comm.Orientation = Orientation
    lib.lcd = sub
    sub.lcd_comm = comm
    drv = types.ModuleType("fake_panel_driver")
    drv.FakeLcd = FakeLcd
    sys.modules.update({"library": lib, "library.lcd": sub,
                        "library.lcd.lcd_comm": comm, "fake_panel_driver": drv})
    disp._CLS["FAKE"] = ("fake_panel_driver", "FakeLcd")     # display.revision: FAKE


install_fake_vendor()


def cfg() -> dict:
    c = cfgmod.load(None)
    c["display"] = dict(c["display"])
    c["display"].update({
        "revision": "FAKE",          # resolved through the fake entry in `disp._CLS`
        "com_port": "COM99",         # fixed: auto-detect is the vendor's, not ours
        "orientation": "landscape",
        "reset_on_start": False,
        # The device ladder shells out to pnputil. Nothing in this file may touch a
        # real device, so the strongest rung of that ladder is switched off here.
        "usb_restart_on_fail": False,
    })
    return c


# --------------------------------------------------------------------------- cases
def case_a_rebuild_leaves_one_owner() -> None:
    print("case: an ordinary rebuild hands the port over without a gap")
    PORT.reset()
    link = PanelLink(cfg(), log=say)
    check("open()", link.open(), True)
    check("frame accepted", link.push(THE_FRAME), True)
    old = PORT.all[-1]
    check("relink()", link.relink("replug"), True)
    new = PORT.all[-1]
    check("the rebuild made a new driver", new is not old, True)
    check("the old driver was closed", old.closed, True)
    check("never more than one driver open", PORT.peak_open, 1)
    check("frame accepted on the new link", link.push(THE_FRAME), True)
    check("and it landed on the new driver", new.writes, 1)
    # The part a `closeSerial()` alone does not do: the vendor's own recovery takes the
    # port back from a driver that was merely closed, and races the new one for it.
    check("the retired driver cannot take the port back",
          raises(RuntimeError, old.openSerial), True)
    check("and so the port is still singly owned", PORT.open_count(), 1)
    link.close()
    check("shutdown released the port", PORT.open_count(), 0)
    print(f"    {PORT}")


def case_late_write_cannot_reclaim_the_port() -> None:
    print("case: a write that finishes after its deadline cannot become a second owner")
    PORT.reset()
    link = PanelLink(cfg(), log=say)
    check("open()", link.open(), True)
    old = PORT.all[-1]
    gate = threading.Event()
    old.write_gate = gate              # the endpoint has stopped draining
    answers: list[bool] = []
    t0 = time.monotonic()
    th = threading.Thread(target=lambda: answers.append(link.push(THE_FRAME)),
                          daemon=True, name="late-push")
    th.start()
    time.sleep(0.5)                    # the worker is now inside the native write
    check("the link is still up until the deadline passes", link.ok, True)
    check("push has not answered yet", answers, [])
    # The real `_WRITE_GIVEUP_S`, not a shortened one: this is the deadline the shipped
    # loop actually runs with, and the case is worth nothing if it tests another one.
    th.join(panel_mod._WRITE_GIVEUP_S + 10.0)
    waited = time.monotonic() - t0
    check("push answered False on the deadline", answers, [False])
    check("it waited for its deadline, not for the write",
          panel_mod._WRITE_GIVEUP_S - 0.5 < waited < panel_mod._WRITE_GIVEUP_S + 5.0,
          True)
    check("link marked down", link.ok, False)
    check("the wedged worker is still inside the write", old.woke, 0)
    # Rebuild under it, which is what a resume does. The old handle has to be retired
    # and refused *before* the new one is opened, or the two of them fight for the port.
    check("relink while the old write is still running", link.relink("resume"), True)
    new = PORT.all[-1]
    check("the rebuild is up", link.ok, True)
    check("nothing has been drawn on it yet", new.writes, 0)
    gate.set()                         # the endpoint finally drains; the worker wakes
    check("the late write ran", wait_for(lambda: old.woke > 0), True)
    time.sleep(0.4)                    # give a port reclaim a chance to show up
    check("the late write stayed on its own connection", new.writes, 0)
    check("and did not reopen its retired driver", old.closed, True)
    check("still exactly one owner right now", PORT.open_count(), 1)
    check("and never more than one at any moment", PORT.peak_open, 1)
    check("the new link is still up", link.ok, True)
    print(f"    {link.summary()}")
    print(f"    {PORT}")
    link.close()


def case_deaf_driver_is_replaced_not_reused() -> None:
    print("case: a handshake we stopped waiting for does not get to keep the port")
    PORT.reset()
    # Never set: this HELLO is the vendor's once-a-second loop with no panel behind it,
    # and the deadline — not an answer — is what ends the attempt.
    PORT.next_behaviour = {"hello_gate": threading.Event()}
    link = PanelLink(cfg(), log=say)
    t0 = time.monotonic()
    ok = link.open()                   # first HELLO runs out its whole deadline
    took = time.monotonic() - t0
    check("open() recovered on a second driver", ok, True)
    check("it took the bounded path, not the vendor's retry loop",
          disp._HELLO_WAIT_S < took < disp._HELLO_WAIT_S + 12.0, True)
    deaf, live = PORT.all[0], PORT.all[-1]
    check("the deaf driver was replaced, not reopened", live is not deaf, True)
    check("the deaf driver is closed", deaf.closed, True)
    check("and cannot be reopened by the attempt that is still running on it",
          raises(RuntimeError, deaf.openSerial), True)
    check("the link owns exactly one driver", PORT.open_count(), 1)
    check("and never owned two at once", PORT.peak_open, 1)
    check("frame accepted on the link that survived", link.push(THE_FRAME), True)
    print(f"    {PORT}")
    link.close()


def case_setup_failure_is_not_success() -> None:
    print("case: a panel that answers HELLO and then refuses commands is not 'up'")
    PORT.reset()
    PORT.next_behaviour = {"orient_fails": True}
    link = PanelLink(cfg(), log=say)
    check("open() refuses to report success", link.open(), False)
    check("the link is down", link.ok, False)
    check("the reason is the setup, not the handshake",
          "orientation" in link.down_reason, True)
    check("the connection it could not use was released", PORT.all[-1].closed, True)
    check("no rebuild was counted", link.rebuilds, 0)
    link.close()

    # The counters matter independently of the return value: a HELLO-only "success"
    # zeroed the escalation ladder every time it happened, so an outage that never
    # actually ended also never escalated, and the app spent the night reopening a port
    # that could answer HELLO and nothing else.
    PORT.reset()
    link2 = PanelLink(cfg(), log=say)
    check("second link comes up", link2.open(), True)
    link2._restart_attempts = 2
    PORT.next_behaviour = {"orient_fails": True}
    check("relink says no", link2.relink("resume"), False)
    check("and takes the link down with it", link2.ok, False)
    check("escalation counters were not reset by it", link2._restart_attempts, 2)
    check("no rebuild counted", link2.rebuilds, 0)
    check("the refused connection left its port go", PORT.open_count(), 0)
    print(f"    {PORT}")
    link2.close()


def case_close_during_build() -> None:
    print("case: shutdown while a bring-up is in flight ends the argument")
    PORT.reset()
    gate = threading.Event()
    PORT.next_behaviour = {"hello_gate": gate}
    link = PanelLink(cfg(), log=say)
    link.start_build()                 # background bring-up, now blocked inside HELLO
    check("the bring-up reached the device", wait_for(lambda: PORT.hello == 1), True)
    t0 = time.monotonic()
    link.close()                       # shutdown, with the bring-up still running
    check("close() did not wait for a wedged bring-up", time.monotonic() - t0 < 1.0, True)
    check("the link is down", link.ok, False)
    gate.set()                         # and now the bring-up finishes, after the fact
    # Orientation is the last thing said before a connection becomes usable on either
    # shape of this class, so seeing it proves the late attempt ran all the way to the
    # point where it could have published.
    check("the mid-shutdown bring-up ran to its end",
          wait_for(lambda: PORT.all[-1].orient_sets > 0), True)
    time.sleep(0.3)
    check("nothing it did published a connection", link.ok, False)
    check("and no driver survived shutdown", PORT.open_count(), 0)
    check("every driver it opened was released", all(d.closed for d in PORT.all), True)
    n = len(PORT.all)
    check("relink after close builds nothing", link.relink("late"), False)
    check("and opens nothing", len(PORT.all), n)
    print(f"    {PORT}")


def case_one_build_at_a_time() -> None:
    print("case: a resume that lands mid-bring-up queues instead of competing")
    PORT.reset()
    gate = threading.Event()
    PORT.next_behaviour = {"hello_gate": gate}
    link = PanelLink(cfg(), log=say)
    link.start_build()                 # a background retry is already talking to it
    check("the first bring-up reached the device", wait_for(lambda: PORT.hello == 1), True)
    answers: list[bool] = []
    threading.Thread(target=lambda: answers.append(link.relink("resume")),
                     daemon=True, name="resume-relink").start()
    time.sleep(0.5)                    # long enough for a competing build to show up
    check("the resume did not start a second handshake", PORT.peak_hello, 1)
    check("and did not open a second driver", PORT.open_count(), 1)
    check("the resume is still waiting for its answer", answers, [])
    gate.set()
    deadline = time.monotonic() + 30
    while not answers and time.monotonic() < deadline:
        time.sleep(0.05)
    check("the resume eventually got one", answers, [True])
    check("still never two handshakes at once", PORT.peak_hello, 1)
    check("still never two drivers open at once", PORT.peak_open, 1)
    check("no driver was orphaned by it", PORT.open_count(), 1)
    check("the link is up", link.ok, True)
    print(f"    {PORT}")
    link.close()


def case_hundred_outages() -> None:
    print("case: 100 outages, retried the way the loop retries them, leak nothing")
    PORT.reset()
    log: list[str] = []
    link = PanelLink(cfg(), log=log.append)     # silent: 100 of each line is noise
    check("open()", link.open(), True)
    stuck: list[str] = []
    for i in range(100):
        live = PORT.all[-1]
        live.write_fails = True                 # the endpoint stops taking frames
        if link.push(THE_FRAME):
            stuck.append(f"outage {i}: push reported success")
            break
        if link.ok:
            stuck.append(f"outage {i}: link still up after a refused frame")
            break
        # What a tick one retry-interval later does: a background rebuild, not a
        # synchronous one, because the loop cannot afford to wait for a bring-up.
        link._retry_at = 0.0
        link.tick()
        if not wait_for(lambda: link.ok, 15.0):
            stuck.append(f"outage {i}: the background rebuild never brought it back")
            break
        if PORT.open_count() != 1:
            stuck.append(f"outage {i}: {PORT.open_count()} drivers open at once")
            break
    check("every outage recovered", stuck, [])
    check("100 rebuilds counted", link.rebuilds, 100)
    check("one driver open at the end", PORT.open_count(), 1)
    check("never two open at any moment", PORT.peak_open, 1)
    check("one driver per bring-up, no extra owners", len(PORT.all), 101)
    check("every retired driver is closed", sum(1 for d in PORT.all if not d.closed), 1)
    time.sleep(0.5)                             # let the release threads finish dying
    live_threads = sorted(t.name for t in threading.enumerate() if t.name != "MainThread")
    check("no worker threads piled up", len(live_threads) <= 8, True)
    if live_threads:
        print(f"    threads still alive: {live_threads}")
    print(f"    {link.summary()}")
    print(f"    {PORT}")
    link.close()
    check("shutdown released the last one", PORT.open_count(), 0)


def main() -> int:
    for fn in (case_a_rebuild_leaves_one_owner,
               case_late_write_cannot_reclaim_the_port,
               case_deaf_driver_is_replaced_not_reused,
               case_setup_failure_is_not_success,
               case_close_during_build,
               case_one_build_at_a_time,
               case_hundred_outages):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())