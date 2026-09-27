"""Drive recovery events at app/recovery.py and time what the render loop pays.

    .venv\\Scripts\\python tools\\recovery_selftest.py

The complaint this is pinned against is not that recovery did the wrong thing but that
it happened *in* the loop: `PanelLink.relink()` holds the link's lock through a whole
bring-up (auto-detect, a HELLO bounded at 8 s, sometimes a reset and a second HELLO),
and `PanelLink.tick()` hands device verbs to `pnputil` with 60-second deadlines once
the escalation ladder starts walking. Both used to be called from the render loop for
every resume-shaped event, and resume-shaped events included an unlock, a monitor
coming back on, and any `WM_DISPLAYCHANGE`. A panel that is slow to answer therefore
stopped the app for tens of seconds - and the loop's own watchdog reads a long tick as
a suspend, so the recovery manufactured the next resume.

The panel here is a fake with two dials: how long a bring-up takes, and how long the
maintenance pass (the `pnputil` half) takes. The first case measures the old inline
path and the new one against the same dials, which is the whole point of the change;
the rest hold the decisions still: a burst rebuilds once, a healthy link is kept, a
display event costs a frame and nothing else, no frame is lit or pushed while a rebuild
is in flight, a replug is retried the moment the port returns, and a wake that arrives
while the panel is meant to be dark does not light it up.
"""
import sys
import threading
import time

sys.path.insert(0, ".")
from app.recovery import REFRESH, WAKE, Recovery   # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def said(log: list[str], fragment: str) -> int:
    """How many times the coordinator said something containing `fragment`."""
    return sum(fragment in line for line in log)


class FakePanel:
    """The slice of `PanelLink` the coordinator touches, with the slow parts scripted.

    Only the shapes that matter are kept: `start_build()` is the non-blocking bring-up
    the link already runs on its own thread (and coalesces against itself), `tick()` is
    the maintenance pass that *used to run in the loop* and that may spend minutes in
    `pnputil`, and `relink()` is the synchronous teardown main.py used to call - kept
    here so the before/after case can measure the thing that was removed.
    """

    def __init__(self, ok: bool = False, build_s: float = 0.0, tick_s: float = 0.0,
                 answers: bool = True, port: str = "COM7") -> None:
        self.ok = ok
        self.port = port
        self.rebuilds = 0
        self.needs_full = False
        self.down_reason = "" if ok else "bring-up said deaf"
        self.build_s, self.tick_s, self.answers = build_s, tick_s, answers
        self.build_calls: list[str] = []
        self.ticks = 0
        self.dark_ticks = 0
        self.full_ticks = 0
        self.pending_setup = False
        self.dark_deferred = 0
        self.invalidations = 0
        self.screens: list[bool] = []
        self._building = False
        self._thread: threading.Thread | None = None

    # - what the link really does
    def relink(self, reason: str) -> bool:
        """The old call: tear the port down and bring it back, here, now, under lock."""
        time.sleep(self.build_s)
        self.ok = self.answers
        if self.ok:
            self.rebuilds += 1
            self.needs_full = True
        return self.ok

    def start_build(self, reason: str = "background retry") -> None:
        self.build_calls.append(reason)
        if self.ok or self._building:
            return                      # coalesced by the link itself, as in the real one
        self._building = True
        self._thread = threading.Thread(target=self._build, daemon=True)
        self._thread.start()

    def _build(self) -> None:
        time.sleep(self.build_s)
        self._building = False
        self.ok = self.answers
        if self.ok:
            self.rebuilds += 1
            self.needs_full = True
            self.down_reason = ""

    def tick(self, reconnect_only: bool = False) -> bool:
        # The coordinator's contract (app/panel.py): a dark pass keeps the retry
        # clock and the device ladder - this fake's `tick_s` - but brings nothing
        # up, because a bring-up is what lights this hardware. The counters are
        # what the dark/lit cases read.
        self.ticks += 1
        if reconnect_only:
            self.dark_ticks += 1
            self.pending_setup = True
            self.dark_deferred += 1
            return self.ok
        self.full_ticks += 1
        time.sleep(self.tick_s)         # this is where the device ladder lives
        return self.ok

    def authorize_light(self) -> bool:
        """The deferred bring-up, done on the coordinator's word rather than this link's."""
        if not self.pending_setup:
            return self.ok
        self.pending_setup = False
        if not self.ok:
            self._build()
        return self.ok

    def invalidate(self) -> None:
        self.invalidations += 1
        self.needs_full = True

    def screen(self, on: bool) -> bool:
        self.screens.append(on)
        return True


def new_recovery(panel, **kw):
    """A coordinator for the deterministic cases: no worker thread unless asked for.

    The cases that drive `maintenance()` by hand do not want a second thread calling it
    at the same time; the one case that measures the loop does, because the thread is
    what the measurement is about. Returns the coordinator and the log it is writing.
    """
    kw.setdefault("cadence_s", 0.0)
    kw.setdefault("hold_max_s", 5.0)
    kw.setdefault("threaded", False)
    log: list[str] = []
    return Recovery(panel, log=log.append, **kw), log


def settle(r: Recovery, panel: FakePanel, want: bool = True, tries: int = 200) -> bool:
    """Tick like the loop until the scripted bring-up has landed."""
    for _ in range(tries):
        d = r.tick(lit=True)
        if panel.ok == want and not d.holds_light:
            return True
        time.sleep(0.01)
    return False


def apply_light(d, dark: bool, cmds: list[str]) -> None:
    """What main.py does with a directive: the commands the panel would actually get."""
    if d.repaint:
        cmds.append("invalidate")       # a flag on the diff transport, cheap either way
    if dark:
        cmds.append("screen-off")
        return
    if d.holds_light:
        return                          # no light command and no frame this tick
    cmds += ["brightness", "screen-on", "push"]


def case_the_loop_used_to_wait() -> None:
    print("case: what the same events cost the loop, before and after")
    build, maintenance = 0.35, 0.45     # a slow handshake and a slow pnputil
    old = FakePanel(ok=False, build_s=build, tick_s=maintenance)
    t0 = time.monotonic()
    old.relink("resume:0x12")           # exactly what main.py used to do ...
    old.tick()                          # ... and then this, in the same tick
    inline = time.monotonic() - t0
    check("the inline path cost both waits", inline > build + maintenance - 0.05, True)

    panel = FakePanel(ok=False, build_s=build, tick_s=maintenance)
    r, _ = new_recovery(panel, threaded=True, cadence_s=0.02)
    try:
        r.request(WAKE, "resume:0x12")
        worst = 0.0
        for _ in range(60):             # a two-second stretch of a one-second loop
            t1 = time.monotonic()
            r.tick(lit=True)
            worst = max(worst, time.monotonic() - t1)
            time.sleep(0.03)
            if panel.ok:
                break
        check("the loop never waited for the handshake", worst < 0.05, True)
        check("the work still got done", panel.ok, True)
        check("and it was the non-blocking kind", len(panel.build_calls), 1)
        check("maintenance ran, off the loop", panel.ticks >= 1, True)
        print(f"    the loop paid {inline * 1000:.0f} ms per event before; worst tick "
              f"now {worst * 1000:.1f} ms, with a {build * 1000:.0f} ms handshake and a "
              f"{maintenance * 1000:.0f} ms device pass in flight")
    finally:
        r.close()


def case_burst_rebuilds_once() -> None:
    print("case: a resume burst is one pass, not one per notification")
    panel = FakePanel(ok=False, build_s=0.02, tick_s=0.0)
    r, _ = new_recovery(panel)
    try:
        # The order a real wake delivers: RESUMEAUTOMATIC, the unlock, a display-on,
        # a WM_DISPLAYCHANGE from the driver re-moding, and another display-on when
        # the second monitor finishes waking up.
        r.request(WAKE, "resume:0x12")
        for reason in ("session-unlock", "console-display-on", "display-change",
                       "monitor-display-on"):
            r.request(REFRESH, reason)
        r.request(WAKE, "resume:0x7")
        d = r.tick(lit=True)
        check("one wake directive for the burst", d.wake is not None, True)
        check("it says how much it folded up", "6 events coalesced" in (d.wake or ""), True)
        check("one rebuild asked for", len(panel.build_calls), 1)
        check("coalesced counter", r.coalesced, 5)
        check("and the whole frame is demanded", d.repaint, True)
        settle(r, panel)
        # The same burst again, once the panel is up: nothing to rebuild, still a frame.
        before = len(panel.build_calls)
        r.request(REFRESH, "display-change")
        r.request(REFRESH, "session-unlock")
        d = r.tick(lit=True)
        check("a display burst asks for no rebuild", len(panel.build_calls), before)
        check("it asks for a frame instead", (d.wake, d.repaint), (None, True))
    finally:
        r.close()


def case_healthy_link_is_kept() -> None:
    print("case: a screen that answers is not torn down to prove it answers")
    panel = FakePanel(ok=True, build_s=0.02)
    r, _ = new_recovery(panel)
    try:
        r.request(WAKE, "resume:0x12")
        d = r.tick(lit=True)
        check("the wake still reaches the loop", d.wake, "resume:0x12")
        check("the link is not rebuilt", len(panel.build_calls), 0)
        check("the frame is repainted instead", d.repaint, True)
        check("and the loop is not held", d.holds_light, False)
        # The first write decides: once the link reports itself down, the next pass
        # rebuilds it, which is one tick of delay and no wasted bring-ups.
        panel.ok = False
        r.request(REFRESH, "display-change")
        r.tick(lit=True)
        check("a link that stopped answering is rebuilt", len(panel.build_calls), 1)
    finally:
        r.close()


def case_no_bright_or_stale_frame() -> None:
    print("case: nothing is lit or pushed while the panel is being re-made")
    panel = FakePanel(ok=False, build_s=0.25)
    r, _ = new_recovery(panel)
    cmds: list[str] = []
    try:
        r.request(WAKE, "resume:0x12")
        apply_light(r.tick(lit=True), dark=False, cmds=cmds)
        check("the loop is told to hold", r.holds_light, True)
        check("no light command went out", "screen-on" in cmds, False)
        check("no frame went out", "push" in cmds, False)
        check("the whole-frame demand was made first", cmds, ["invalidate"])
        for _ in range(40):                      # keep ticking while it builds
            if panel.ok and not r.tick(lit=True).holds_light:
                break
            time.sleep(0.02)
        d = r.tick(lit=True)
        check("the hold ends when the panel answers", d.holds_light, False)
        apply_light(d, dark=False, cmds=cmds)
        check("the first frame after it is a whole one",
              cmds[-1] == "push" and "invalidate" in cmds, True)
        check("and it is the panel that says so", panel.needs_full, True)
    finally:
        r.close()


def case_dark_intent_survives_a_wake() -> None:
    print("case: an unattended wake does not light a panel that is meant to be dark")
    # PBT_APMRESUMEAUTOMATIC arrives for wakes nobody asked for - a scheduled task, a
    # wake-on-LAN packet, the network adapter's own magic - and at that moment the
    # display state is unknown. A bring-up is not a neutral act on this hardware: the
    # panel's MCU comes up at its own default brightness, so rebuilding on that event
    # is how a 3 a.m. packet became a lit panel in a dark room.
    panel = FakePanel(ok=False, build_s=0.02)
    r, log = new_recovery(panel)
    try:
        r.request(WAKE, "resume:0x12")
        d = r.tick(lit=False)
        check("the loop is told about the wake", d.wake is not None, True)
        check("the rebuild waits for something to show", panel.build_calls, [])
        check("and that is said out loud", said(log, "meant to be dark"), 1)
        check("the directive says it was deferred", d.deferred, True)
        d = r.tick(lit=False)
        check("and it is not repeated every tick", said(log, "meant to be dark"), 1)
        r.tick(lit=True)                        # somebody sits down
        check("now the rebuild runs", len(panel.build_calls), 1)
        settle(r, panel)
        check("and the panel comes up", panel.ok, True)
    finally:
        r.close()


def case_arrival_is_noticed() -> None:
    print("case: the cable going back in is an event, not a wait for the retry clock")
    ports = {"COM7": False}
    panel = FakePanel(ok=False, build_s=0.02, port="COM7")
    r, _ = new_recovery(panel, list_ports=lambda: {p for p, here in ports.items() if here})
    try:
        ports["COM9"] = True                   # somebody else's device
        r.maintenance()                        # the first look is a baseline
        r.maintenance()
        check("a baseline is not an arrival", panel.build_calls, [])
        check("a foreign port is not our screen", panel.build_calls, [])
        ports["COM7"] = True
        r.maintenance()
        check("our port arriving starts a rebuild", len(panel.build_calls), 1)
        check("and it names the arrival",
              "panel arrived (COM7)" in panel.build_calls[0], True)
        check("on the worker, not in the loop", panel.ticks >= 1, True)
        settle(r, panel)
        check("the link is back", panel.ok, True)
        # And it is a watch, not a one-shot: the cable comes out and goes back in
        # again, the first look after the link is down is the baseline, the second
        # sees the arrival.
        panel.ok = False
        r.maintenance()
        ports["COM7"] = False
        r.maintenance()
        ports["COM7"] = True
        r.maintenance()
        check("and it happens again next time", len(panel.build_calls), 2)
        # And the number is not the address: Windows reissues COM numbers freely,
        # so the replug that comes back as COM3 while COM7 is gone is our screen
        # coming back, and waiting for the retry clock to notice is the defect.
        settle(r, panel)                       # that pass lands, as it will in use
        panel.ok = False
        r.maintenance()                        # link down: baseline again
        ports["COM7"] = False
        ports["COM3"] = True                   # same screen, new number
        r.maintenance()
        check("a renumbered replug is still our arrival", len(panel.build_calls), 3)
        check("and it says so", "COM3" in panel.build_calls[-1], True)
    finally:
        r.close()


def case_unreadable_bus_is_silent() -> None:
    print("case: when the bus cannot be read, the link's own clock decides")
    panel = FakePanel(ok=False, build_s=0.0)
    r, _ = new_recovery(panel, list_ports=lambda: None)
    try:
        r.maintenance()
        r.maintenance()
        check("no arrival is invented", panel.build_calls, [])
        check("maintenance still ran", panel.ticks >= 2, True)
    finally:
        r.close()


def case_a_deaf_panel_does_not_hold_the_loop() -> None:
    print("case: a panel that never answers does not put the loop on hold")
    panel = FakePanel(ok=False, build_s=1.0, answers=False)
    r, log = new_recovery(panel, hold_max_s=0.15)
    try:
        r.request(WAKE, "resume:0x12")
        check("held while the rebuild is outstanding", r.tick(lit=True).holds_light, True)
        time.sleep(0.2)
        check("and not held forever", r.tick(lit=True).holds_light, False)
        check("that is said once, not silently", said(log, "not waiting for it"), 1)
        check("and it does not simply start holding again", len(panel.build_calls), 1)
        check("the beat can say it too", "holding" not in r.summary(), True)
    finally:
        r.close()


def case_worker_faults_are_contained() -> None:
    print("case: a worker that raises takes nothing down with it")

    class Exploding(FakePanel):
        def tick(self, reconnect_only: bool = False) -> bool:
            raise SystemExit(0)         # the vendor library's idea of an error

        def start_build(self, reason: str = "") -> None:
            raise RuntimeError("the port refused")

    panel = Exploding(ok=False)
    r, log = new_recovery(panel)
    try:
        r.request(WAKE, "resume:0x12")
        r.tick(lit=True)                # start_build raises inside this
        r.maintenance()                 # and the maintenance pass raises here
        r.maintenance()
        check("the loop survived it", r.tick(lit=True).wake, None)
        check("the refused build is said once", said(log, "port refused"), 1)
        check("the raising worker is said once",
              said(log, "panel maintenance SystemExit"), 1)
        check("and the beat can say it is happening", "fault=" in r.summary(), True)
        check("the panel stays down, not absent", panel.ok, False)
    finally:
        r.close()


def main() -> int:
    for fn in (case_the_loop_used_to_wait, case_burst_rebuilds_once,
               case_healthy_link_is_kept, case_no_bright_or_stale_frame,
               case_dark_intent_survives_a_wake, case_arrival_is_noticed,
               case_unreadable_bus_is_silent, case_a_deaf_panel_does_not_hold_the_loop,
               case_worker_faults_are_contained):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
