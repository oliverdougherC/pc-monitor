#!/usr/bin/env python
"""PC desk-monitor app: telemetry → 5" USB LCD (Turing family) or simulator.

Run:
  python main.py                     # live loop (SIMU in config → http://localhost:5678)
  python main.py --backend demo      # synthetic data, no sensors needed
  python main.py --dump p.png        # render one frame headless and exit
  python main.py --force-state game  # preview the game layout

The loop is deliberately boring: read sensors, ask what the machine is doing, ask
what the panel should look like, draw, push. Every decision that used to be made
here in an ad-hoc way now belongs to a module that can be tested without a panel:

  app/hoststate  asleep / monitor off / locked        (events + a gap watchdog)
  app/nightlight is the user's night mode on, how warm
  app/lights     dark? how bright? which LUT?         (the only light decision)
  app/gamewatch  which process is the game            (locked target)
  app/frames     its frame rate, or its last one      (held, marked stale)
  app/panel      the USB link, including its failures
  app/recovery   what an event asks for, and where the waiting happens
  app/instance   one main role per machine            (OS-held claim, at start)

Sleep is the reason for that shape. The loop freezes when the machine sleeps, so
"turn the panel off when the PC sleeps" cannot be a rule evaluated after the fact —
it has to be an event that wakes us. Hence `host.wait()` below instead of
`time.sleep()`, and a resume path that re-makes the panel link and the ETW capture
rather than assuming the world is as we left it - asking for that work instead of
doing it here, because a bring-up is tens of seconds and the loop cannot spend them.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import bootlog                                # noqa: E402
from app import config as cfgmod                       # noqa: E402
from app.burnin import BurnIn                          # noqa: E402
from app.display import app_log                        # noqa: E402
from app.frames import FrameMonitor                    # noqa: E402
from app.gamewatch import GameWatch                    # noqa: E402
from app.hoststate import HostState                    # noqa: E402
from app.instance import acquire_main_role             # noqa: E402
from app.layout import Layout                          # noqa: E402
from app.lights import LightPlanner                    # noqa: E402
from app.liveness import beat as beat_liveness         # noqa: E402
from app.liveness import mark_stopped                  # noqa: E402
from app.nightlight import NightLight                  # noqa: E402
from app.owned import clear_record, stop_requested, write_record   # noqa: E402
from app.output import DiffPusher, wipe, wipe_supported   # noqa: E402
from app.panel import PanelLink                        # noqa: E402
from app.power import estimate                         # noqa: E402
from app.recovery import REFRESH, WAKE, Recovery       # noqa: E402
from app.sensors import make_hub                       # noqa: E402
from app.snapshot import Snapshot                      # noqa: E402
from app.steamid import SteamIdentity                  # noqa: E402

# How long to leave a sensor backend that failed to start alone before asking it again.
# Longer than a tick, shorter than a logon: the COM port and LibreHardwareMonitor's
# ring0 handle are both usually ready within a few seconds of this app giving up.
SENSOR_RETRY_S = 20.0
PRIME_TRIES = 5
PRIME_WAIT_S = 2.0

# The start-up story is mirrored into boot.log until the loop is running. After that
# only when log.log cannot be written, so the mirror is a fallback and not a second log.
_boot_phase = True
_log_path_ok: bool | None = None


def vendor_logger_ok() -> bool:
    """Can `log.log` actually be written? Asked once, then remembered.

    `app_log` swallows every failure of the vendored logger, which is exactly right in
    the middle of a tick and useless as an answer to "can I rely on log.log on this
    machine". A clean clone has no vendored checkout at all — `vendor/` is a pin plus a
    provenance note — and the answer there is no, which is how a start-up that died
    before the library loaded used to leave no trace anywhere.
    """
    global _log_path_ok
    if _log_path_ok is None:
        try:
            from app.display import ensure_vendor_path
            ensure_vendor_path()
            import library.log                          # noqa: F401
            _log_path_ok = True
        except BaseException:   # noqa: BLE001 - absence is a real answer, not a fault
            _log_path_ok = False
    return _log_path_ok


def status(msg: str) -> None:
    """Report something worth reading: print() for a console run, and log.log,
    because the scheduled task runs pythonw.exe, where sys.stdout is None and
    every print() silently vanishes.

    And when `log.log` is not there to be written to — no vendored logger, a clean
    install, a permissions problem — the line goes to boot.log instead, which needs
    nothing but the standard library. The start-up lines are mirrored into both while
    the app is still starting up, because that is the window where the app is most
    likely to die and least likely to have a working logger yet.
    """
    try:
        print(msg)
    except (UnicodeEncodeError, ValueError):    # pragma: no cover - pythonw: no stdout
        pass
    app_log(msg)
    if _boot_phase or not vendor_logger_ok():
        bootlog.note(msg)


def _console_safe() -> None:
    """Never let a log line be the reason the app stopped.

    Status text carries arrows and box-drawing characters (`sunset→sunrise`, the
    state transitions). A console defaults to the OEM codepage — cp1252 here — and
    `print()` on a character outside it raises, which once killed the loop on its
    very first night-mode line. Replacing unknown characters with `?` on the console
    is the right trade: log.log is UTF-8 and keeps the text intact.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):     # pythonw: stdout is None
            pass


class Guard:
    """One subsystem raising is a logged fault, never a dead loop.

    Every tick touches things this app does not control: ctypes calls into user32, a
    vendored driver that raises *and* calls `sys.exit()` when a COM port will not
    open, a CSV child process, the window manager, Pillow, the serial link. Only the
    sensor read used to be guarded, so a raise anywhere else ended the process — and
    under `pythonw.exe` there is no console, so the ending was silent: the panel
    stopped updating and `log.log` said nothing wrong. That is the failure this whole
    pass is meant to remove, so it is not allowed to survive in the loop itself.

    This is containment, not recovery: the tick in progress is abandoned, the next
    one tries again, and each subsystem has its own retry (the link rebuilds, the
    capture restarts, the held frame value expires on its own). Repeating the same
    fault is counted rather than re-reported — a call that raises every second must
    not fill the log or bury everything else in it.

    `BaseException` on purpose: `KeyboardInterrupt` is re-raised so Ctrl-C still
    works, but `SystemExit` is not — the vendor driver's "cannot open COM port" path
    raises it, and a missing screen must not be able to stop the sensor loop.
    """

    def __init__(self, log=status):
        self.log = log
        self.count = 0
        self.where = ""
        self._at = 0.0
        self.repeat_s = 300.0

    def run(self, what, fn, fallback=None):
        try:
            return fn()
        except KeyboardInterrupt:
            raise
        except BaseException:  # noqa: BLE001 - see the docstring
            self.count += 1
            new = what != self.where
            self.where = what
            now = time.monotonic()
            # Repeat noise is counted, not re-reported; a *different* subsystem
            # failing is new information even inside the quiet window, and swallowing
            # it would hide the second fault behind the first one's message.
            if new or now - self._at > self.repeat_s:
                self._at = now
                trace = traceback.format_exc(limit=2).strip().replace("\n", " | ")
                self.log(f"[fault] {what} raised ({self.count} total): {trace}"
                         f" — the tick continues without it")
            return fallback

    def text(self, what, fn) -> str:
        """One field of a status line. `?` is a better outcome than a dead tick: a
        log line that cannot be formatted has already been the reason this app
        stopped once, and the panel's numbers matter more than the sentence."""
        return self.run(what, fn, "?")

    def summary(self) -> str:
        return "" if not self.count else f" faults={self.count}({self.where})"


def _thread_safety_net() -> None:
    """Log what a thread dies of. Panel rebuilds and the event window run in threads
    whose exceptions nobody sees: without this, a crashed builder thread just means
    the panel quietly stops being retried, which looks exactly like a dead panel."""
    def hook(args) -> None:
        try:
            app_log(f"[fault] thread {args.thread.name if args.thread else '?'} raised: "
                    + "".join(traceback.format_exception(
                        type(args.exc_value), args.exc_value, args.exc_traceback))
                    .strip().replace("\n", " | "))
        except Exception:  # noqa: BLE001 - the net must not be the thing that fails
            pass

    try:
        threading.excepthook = hook
    except (AttributeError, TypeError):     # pragma: no cover - pre-3.8
        pass


def sweep_step(burn, layout, pusher, plan, state: str, panel_ok: bool,
               holds_light: bool, g: "Guard",
               now: float | None = None) -> bool:
    """Advance the burn-in exercise by at most one frame. True if one was pushed.

    The sweep used to run to completion *inside* a single tick, in a nested `while`
    with its own `time.sleep(0.12)`. Twelve shipped seconds, then, in which the loop
    read no host state, made no light decision and asked the game detector nothing:
    a monitor turning off, a lock, a suspend query or a game starting mid-animation
    was ignored until the animation ended. And because every one of its ~100 pushes
    could additionally wait on the transport deadline, twelve seconds was a floor and
    not a ceiling — the worst case was a sweep that outlived the fault it was
    oblivious to.

    One frame per iteration makes every sweep frame carry this tick's decisions, and
    it costs a coarser animation on a screen whose exercise nobody watches. Whether
    the sweep owned the tick is the caller's cue to skip the normal frame, so a sweep
    tick pays exactly what a normal tick pays — one frame on the bus either way.

    Anything meaning "nobody can see this" abandons the exercise instead of pausing
    it with the screen half-rainbow'd: `BurnIn.defer` restarts the interval, which is
    the same treatment the dark branch of the light policy already gives it. A link
    that is down is in that list too — pushing into it would pay the write deadline
    for pixels that cannot arrive, which is how a sweep becomes a recovery loop. A
    recovery pass in flight (`holds_light`) is in it as well: a sweep is a minute of
    deliberate full-frame pushes, and half of it would land on a panel being re-made.
    """
    now = time.monotonic() if now is None else now
    if not (burn.exercise_due(now) or burn.exercise_progress(now) is not None):
        return False
    if plan.dark or state == "game" or not panel_ok or holds_light:
        burn.defer(now)
        g.run("invalidate", pusher.invalidate)      # next lit frame must be whole
        return False
    p = burn.exercise_progress(now)
    if p is None:                                   # the exercise just finished
        g.run("invalidate", pusher.invalidate)
        return False
    img = g.run("sweep-frame", lambda: layout.sweep(p), None)
    if img is None:
        # The sweep cannot draw: abandon it rather than spend the rest of the
        # exercise discovering that again, one guarded fault per tick.
        burn.defer(now)
        g.run("invalidate", pusher.invalidate)
        return False
    g.run("sweep-push", lambda: pusher.push(img))
    return True


def light_text(plan) -> str:
    """The beat's `light=` field: `dark(idle)`, `lit 45`, or `lit 70+play`.

    The `+play` suffix is the difference between "the panel is lit" and "the panel is
    lit although the idle timer expired two hours ago" — the first reads as a healthy
    desk at 7 a.m., the second is the one you want to know about, and it is only
    visible if the beat says it. Kept out of the beat closure so the tests can pin it.
    """
    if plan.dark:
        return f"dark({plan.reason})"
    return f"lit {plan.brightness}{'+play' if plan.idle_held else ''}"


DT_CAP_S = 60.0


def elapsed_dt(prev: float, now: float) -> float:
    """Seconds the loop has been away since its previous tick, clamped at both ends.

    The lower clamp stops a clock that stepped backwards from handing every interval
    counter a negative `dt`. The upper one is the reason this is a function and not an
    expression: a loop that wakes from a three-hour sleep must not spend three hours
    of it inside the idle timers, the frame-hold window or the night-mode poll.

    It is deliberately *not* the wake evidence. A number that has just been capped at
    60 s cannot say how long the machine was really asleep, and until now the same
    number was also being used as the threshold of the gap watchdog inside
    `app.hoststate` — which asked a 30-second suspend to outweigh 90 seconds and so
    never fired for any ordinary sleep. The watchdog measures its own gap and compares
    it against the polling cadence it is given; this one only feeds the counters.
    """
    return min(max(now - prev, 0.0), DT_CAP_S)


def _prime(hub, tries: int = PRIME_TRIES, wait_s: float = PRIME_WAIT_S,
           log=status, sleep=time.sleep):
    """Take the first sensor sample, with a short bounded retry. None if it never came.

    Two samples are needed before an interval counter can say anything, so the loop's
    first tick is blank either way — what matters is that a *failure* here is not fatal.
    A machine that is still coming up usually needs seconds, so this waits a little;
    past that it gives up and lets the loop carry on, because the loop already keeps the
    last snapshot when a read fails and reports the fault once per quiet window.
    """
    if hub is None:
        return None
    last: Exception | None = None
    for n in range(tries):
        try:
            return hub.tick()
        except Exception as e:  # noqa: BLE001 - refusing to die is the whole point
            last = e
            if n + 1 < tries:
                sleep(wait_s)
    log(f"[sensors] first sample failed ({type(last).__name__}: {last}) — starting "
        f"without a snapshot; the loop keeps retrying every tick")
    return None
def main() -> None:
    global _boot_phase        # set False once the loop is running; see `status()`
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--backend", default=None, help="auto|lhm|fallback|demo")
    ap.add_argument("--force-state", choices=["idle", "game"], default=None)
    ap.add_argument("--dump", default=None, help="render N frames headless, save PNG, exit")
    ap.add_argument("--frames", type=int, default=3, help="ticks before --dump saves")
    args = ap.parse_args()
    _console_safe()

# Containment and a diagnostic path come first, before anything that can fail.
    # Both of these used to be installed further down, after the config read, the
    # sensor hub, the ETW child, the panel link and the first sensor sample. A
    # transient failure in any of those ended the process before the machinery meant to
    # contain it existed, and (with no vendored logger to write to) ended it without
    # leaving a sentence anywhere.
    _thread_safety_net()
    g = Guard()
    bootlog.note(f"[boot] start argv={' '.join(sys.argv[1:]) or '-'}")

    # Ownership next, and before any hardware. A second main-role start must leave
    # without opening the COM port and without letting the capture child reclaim the
    # running owner's ETW session — both of which happen inside the objects below —
    # so the claim is taken here, before any of them exist. It is an OS-held lock
    # released by process death (app/instance.py), so a killed or crashed owner
    # cannot leave a stale lock behind, and the machine-wide name means two
    # checkouts in two working directories still compete for one lock. `--dump` is
    # the headless preview role: like liveview and the diag tools it runs alongside
    # the app on purpose, so it takes no lock — and it captures under its own
    # session role below, so rendering a preview never reclaims the live app's
    # capture either.
    dump_mode = bool(args.dump)
    if not dump_mode:
        owned, why = acquire_main_role()
        if not owned:
            status(f"[start] {why}")
            return

    # The config is read outside the guard on purpose. A bad config is not a transient
    # hardware fault: retrying it is how a permanent mistake turns into a restart loop,
    # so it is reported once, loudly, and the process stops (see `__main__`).
    cfg = cfgmod.load(args.config)
    interval = float(cfg["sensors"]["interval_s"])
    layout = Layout(cfg, rate_hz=1.0 / interval)
    watch = GameWatch(cfg)
    burn = BurnIn(cfg)
    # `cadence_s` is what the loop is *supposed* to take between ticks. The gap
    # watchdog needs that expectation: judging a freeze against the elapsed time it is
    # measuring made every ordinary suspend look like a normal tick (see
    # `main.elapsed_dt` and `app.hoststate.tick`).
    host = HostState(gap_s=float(cfg["power"].get("wake_gap_s", 5.0)),
                     cadence_s=interval)
    night = NightLight(cfg, refresh_s=float(cfg["night"].get("refresh_s", 3.0)))
    lights = LightPlanner(cfg, night=night, host=host)

# Everything below here reaches outside the process, which is where "not yet" is a
    # normal answer at logon. Each one is allowed to fail into a degraded run that the
    # loop retries, rather than into a dead process that waits for the next logon.
    hub = g.run("sensors-init", lambda: make_hub(cfg, force=args.backend), None)
    if hub is None:
        status("[sensors] backend did not start — running degraded: the panel link, the "
               "sleep/lock rules and the light policy all keep working, and the backend "
               f"is retried every {SENSOR_RETRY_S:.0f} s")

    # real per-process present telemetry (ETW, needs admin); degrades to None/{}.
    # The headless preview runs alongside the app on purpose, so it captures under
    # its own session role instead of reclaiming the main role's (config's
    # `frames.role` still pins, and pins every role the same way it always did).
    frames_mon = (g.run("frames-init",
                        lambda: FrameMonitor(cfg, role="dump" if dump_mode else "main"),
                        None)
                  if str(cfg["frames"].get("source", "auto")) != "off" else None)
    steam = g.run("steam-init", SteamIdentity, None)
    if frames_mon is None and str(cfg["frames"].get("source", "auto")) != "off":
        status("[frames] the present capture could not start — frame stats off, legacy "
               "detection only; needs a restart to try the ETW session again")
    if frames_mon is not None:
        import atexit
        atexit.register(frames_mon.close)  # don't leave an orphaned ETW session behind
        if frames_mon.error:
            status(f"[frames] {frames_mon.error} — frame stats off, legacy detection only")

    # Say who this process is, in the one place the installer is allowed to look.
    # `tools/install_autostart.ps1` recognised "the app" by a substring of a command
    # line and force-killed everything that matched — which on any dev machine includes
    # every other Python program that has a file called main.py. A record written by the
    # app about itself (this pid, this instance's creation time, canonical paths, and the
    # ETW session its own collector was told to own) is what turns "stop the app" into a
    # statement about one specific process instead of one specific filename.
    session = (frames_mon.session_name if frames_mon is not None
               else f"PCMonitor-{cfg['frames'].get('role', 'main')}")
    write_record(session=session,
                 collector=str(ROOT / str(cfg["frames"].get("presentmon_path", ""))))
    import atexit
    atexit.register(clear_record)
    # A request left behind by a run that died between "stop" and "exited" belonged to
    # that run. Consumed here, once, or every later start would quit immediately.
    if stop_requested():
        status("[stop] cleared a stop request left by a previous run")

    panel = None
    pusher = None
    recovery = None

    def _shutdown() -> None:
        """Give the port back and stop the event window on any exit path."""
        try:
            host.close()
        except Exception:  # noqa: BLE001
            pass
        if recovery is not None:
            # Before the link: the worker calls into it, and a thread that is still
            # calling into a closed device is a fault line in the log on the way out.
            try:
                recovery.close()
            except Exception:  # noqa: BLE001
                pass
        if panel is not None:
            try:
                panel.close()
            except Exception:  # noqa: BLE001
                pass

    import atexit
    atexit.register(_shutdown)

    if not dump_mode:
        # The link owns the device: it survives a vanished COM port, a wedged
        # endpoint, and the panel rebooting into its own default orientation. Building
        # it is the part that can fail on a machine still enumerating USB, and a screen
        # that is not there yet is not a reason to stop the sensor loop.
        panel = g.run("panel-init", lambda: PanelLink(cfg, log=status), None)
        if panel is None:
            pusher = None
            panel_desc = "link not built"
            status("[panel] the link could not be built — the loop retries it on its "
                   "own clock; everything except the pixels keeps working")
        else:
            pusher = DiffPusher(panel)
            if not panel.open(on_relink=pusher.invalidate):
                status(f"[panel] no screen on start ({panel.down_reason}) — continuing anyway, "
                       f"the loop keeps retrying every 10 s; plug it in and it will come up")
            panel_desc = f"{panel.get_width()}x{panel.get_height()}"
            # Who decides what an event costs. The link is already built by now, so the
            # coordinator starts from a known state; its worker thread is what runs the
            # link's maintenance and the device ladder, which is the work that used to
            # stop this loop for tens of seconds (see app/recovery.py).
            recovery = Recovery(panel, log=status)
    else:
        panel_desc = "headless dump"
    if frames_mon is None:
        frames_desc = "off"
    elif frames_mon.output_file:
        frames_desc = "file-capture"
        status(f"[frames] raw capture → {frames_mon.output_file}; frame stats off by design")
    else:
        frames_desc = "starting"
    status(f"[start] pid={os.getpid()} ppid={os.getppid()} backend={type(hub.backend).__name__} "
           f"revision={cfg['display']['revision']} port={cfg['display']['com_port']} "
           f"panel={panel_desc} frames={frames_desc} "
           f"interval={cfg['sensors']['interval_s']}s")
    status(f"[host] {host.summary()}")
    last_shift = burn.shift()
    # idle→game reflow is hidden by a dark frame; serial boards can't afford it
    can_wipe = not dump_mode and wipe_supported(cfg["display"]["revision"], cfg)
    hold_s = float(cfg["layout"].get("transition_hold_s", 0.12))
    prev_state = None

    from app.sensors.demo import DemoBackend
    demo = (None if hub is None else
            hub.backend if isinstance(hub.backend, DemoBackend) else None)

    # Prime the interval-based counters. The first sample is where a machine that is
    # still coming up says so — LibreHardwareMonitor's ring0 handle, NVML, a COM port
    # that has not enumerated yet — and this was the one unguarded call standing between
    # "the app started" and "the loop is running": a transient failure here ended the
    # process while the guard was still two lines further down.
    prev_snap = g.run("sensors-prime", lambda: _prime(hub), None)
    time.sleep(0.2)

    # A first light decision so a fault in the planner later on has something to fall
    # back to instead of leaving `plan` unbound in front of the panel.
    # A process that starts while the displays are already asleep is never told so:
    # `GUID_CONSOLE_DISPLAY_STATE` reports changes. Seed the answer from the power
    # scheme, or a reboot into a dark room leaves the desk panel lit until the idle
    # timer catches up 45 minutes later. `hoststate.seed_monitor` explains itself.
    if cfg["power"].get("seed_monitor", True) and host.monitor_on is None:
        seed = g.run("monitor-seed", host.seed_monitor, "")
        if seed:
            status(f"[monitor] {seed}")
    plan = lights.tick("idle", host.idle_s, 0.0)

    tick_dt = time.monotonic()
    # How long the previous tick took to run, reported to the gap watchdog below. The
    # first tick has no previous tick, and it is exempt anyway.
    work_s = 0.0
    frames_reported = frames_mon is None
    frames_warned: str | None = None
    min_gpu = float(cfg["game"]["min_gpu_load"])
    errors = 0
    last_err = ""
    last_err_at = 0.0
    last_light = ""
    dark_logged = ""
    held_logged = ""                 # "waiting" while recovery is in flight
    idle_held = False
    # A heartbeat, because every other line here is conditional and the failure mode
    # of a loop that died at 4 a.m. is a panel frozen on last night's numbers with a
    # log that says nothing wrong. One line every 15 min (first one after a minute,
    # so a start is provably alive without waiting a quarter of an hour).
    beat_every_s = float(cfg["display"].get("heartbeat_s", 900))
    next_beat = time.monotonic() + 60.0
    started = time.monotonic()
    ticks = 0
    hub_retry_at = 0.0
    # The start-up story is over: from the first tick on, boot.log only mirrors what
    # log.log could not hold, so it stays a bootstrap record instead of a second copy.
    _boot_phase = False
    while True:
        t0 = time.monotonic()
        ticks += 1
        # A shutdown the installer asked for, taken the normal way. The app closes its
        # own COM port and stops its own collector on the way out — which a forced stop
        # never lets it do, and that is exactly where an orphaned ETW session comes from.
        # Checked at the top of a tick, so the worst-case answer is one interval.
        if g.run("stop-request", stop_requested, False):
            status("[stop] shutdown requested — closing the link and exiting cleanly")
            return

        if hub is None:
            # No backend yet. Ask again on the loop's clock: a COM port that had not
            # enumerated at logon is usually there a minute later, and the app that
            # gave up during start-up is the one that never finds that out.
            if t0 - hub_retry_at >= SENSOR_RETRY_S:
                hub_retry_at = t0
                hub = g.run("sensors-init",
                            lambda: make_hub(cfg, force=args.backend), None)
                if hub is not None:
                    status("[sensors] backend is back — resuming telemetry")
        if hub is not None:
            # The hub is the supervisor (app/sensors/__init__.py): the backend call
            # is bounded, a failed tick holds the last good sample for a documented
            # grace and then blanks to honest "--". Reaching this except would be a
            # supervisor bug - contain it, but fall back to a *blank* snapshot:
            # re-presenting the previous one as live used to be the answer here,
            # and it is the very fault this replaced (flat lines drawn into the
            # trend bands for as long as the driver stayed dead).
            try:
                snap = hub.tick()
            except Exception as e:  # noqa: BLE001 - a sensor that throws once must not stop the panel
                errors += 1
                if f"{type(e).__name__}" != last_err or t0 - last_err_at > 300.0:
                    last_err, last_err_at = f"{type(e).__name__}", t0
                    status(f"[sensors] supervisor fault ({type(e).__name__}: {e}) — "
                           f"blank sample; {errors} total")
                snap = Snapshot(ts=time.time())
            line = g.run("sensors-report", hub.changed_to_log, "")
            if line:
                status(line)
        else:
            snap = None

        dt = elapsed_dt(tick_dt, t0)       # a frozen loop must not fake a long dt
        tick_dt = t0
# `work_s` is the part of that elapsed time this loop spent executing. The
        # gap watchdog subtracts it from the gap it measures and treats what is left
        # as time the process did not run at all, which is the only evidence a gap can
        # give of a suspend — and the reason a half-minute relink of a deaf panel is
        # not mistaken for a wake it just performed (see app/panel.py `start_build`).
        g.run("host", lambda: host.tick(dt, work_s=work_s))

        # Two kinds of evidence arrive from the event window, and they cost different
        # things: a resume means the machine stopped, so everything downstream is
        # suspect; a display or session event means the panel has something new to show
        # and nothing else. `app/recovery.py` folds a burst of either into one request
        # and runs the slow half on its own thread, so a notification can no longer stop
        # this loop - which matters because the loop's own watchdog reads a long tick
        # as a suspend, and a rebuild used to manufacture the next resume.
        # The resume edge is *held*, not consumed, while a suspend request is still
        # outstanding: a wake costs the milliseconds the panel has left before the bus
        # loses power, and the click on Start -> Sleep is exactly that moment. The edge
        # stays armed until the tick after the request resolves - by a resume message,
        # or by input that moved the clock forward from the request (app/hoststate.py).
        # The light state is passed in as the last decision made, which is the newest
        # one this tick has: a rebuild that lands late applies what is wanted then.
        wake = (None if host.suspend_pending
                else g.run("take-resume", host.take_resume, None))
        display = g.run("take-refresh", host.take_refresh, None)
        d = None
        holds = False

        if not dump_mode and panel is None:
            # The link itself could not be built at start-up. Rebuild it here rather
            # than give up on the screen for the rest of the run: by now the device may
            # well exist, and this is the only other place that knows to ask.
            panel = g.run("panel-init", lambda: PanelLink(cfg, log=status), None)
            if panel is not None:
                pusher = DiffPusher(panel)
                g.run("panel-open", lambda: panel.open(on_relink=pusher.invalidate))
                recovery = Recovery(panel, log=status)
                status("[panel] link built on the retry — the screen is back in play")

        if not dump_mode and recovery is not None:
            if wake:
                g.run("recovery-wake", lambda: recovery.request(WAKE, wake))
            if display:
                g.run("recovery-refresh", lambda: recovery.request(REFRESH, display))
            d = g.run("recovery", lambda: recovery.tick(lit=not plan.dark), None)
            if d is not None:
                holds = d.holds_light
                if d.wake:
                    status(f"[resume] {d.wake} - {g.text('host.summary', host.summary)}")
                    # Only a real wake throws the capture and the locked target away.
                    # An ETW session that survived a suspend keeps its registration and
                    # delivers nothing, and the game we locked onto was frozen mid-frame;
                    # a monitor timing itself out is neither of those things, and used to
                    # cost both of them plus the link.
                    if frames_mon is not None:
                        g.run("capture-restart", lambda: frames_mon.restart(d.wake))
                        frames_reported = False      # say again whether the session came back
                    g.run("watch-reset", lambda: watch.reset(d.wake))
                    # Drivers do not survive sleep either: NVML handles and the LHM
                    # Computer belong to the world that existed before the suspend, so
                    # the hub re-acquires them instead of trusting the ones it has.
                    if hub is not None:
                        g.run("sensors-recover", lambda: hub.recover(d.wake))
                elif d.repaint:
                    status(f"[display] {display or 'repaint'} - whole frame, link kept")
                if d.repaint:
                    g.run("invalidate", pusher.invalidate)

        if snap is None:
            # Degraded: there are no numbers, so there is nothing to draw and nothing
            # to detect. This is deliberately not a frozen tick. The two things that
            # need no telemetry still happen — the link retry above, and the one light
            # decision below — so a machine that sleeps, locks or idles while the
            # sensors are down still gets the dark panel it asked for. Losing that is
            # the one output mistake a degraded start-up must not be allowed to make.
            plan = g.run("lights", lambda: lights.tick("idle", host.idle_s, dt), plan)
            if not dump_mode and panel is not None and pusher is not None:
                if plan.dark:
                    g.run("screen-off", lambda: panel.screen(False))
                    burn.defer(t0)
                else:
                    g.run("warm-lut", lambda: layout.set_warm(plan.lut))
                    g.run("brightness", lambda: panel.set_brightness(plan.brightness))
                    g.run("screen-on", lambda: panel.screen(True))
            # Say it to the outside world too: an observer that cannot see this tick
            # will restart an app that is degraded but perfectly alive.
            beat_liveness(tick=ticks, state="degraded")
            sleep_left = interval - (time.monotonic() - t0)
            if sleep_left > 0:
                g.run("wait", lambda: host.wait(sleep_left))
            continue

        if dump_mode:
            presenters = g.run("presents", lambda: frames_mon.presenters()
                               if frames_mon is not None else None, None)
        else:
            # None, not {}, when there is no capture: "cannot see presents" and
            # "nothing is presenting" lead to different decisions in GameWatch.
            presenters = (g.run("presents", frames_mon.presenters, None)
                          if frames_mon is not None and frames_mon.ok else None)
        if frames_mon is not None and not frames_reported:
            if frames_mon.ok:
                status("[frames] presentmon session live — present-based detection on"
                       + ("" if frames_mon.job_error is None else
                          # the child can outlive a hard kill of this process; harmless
                          # across restarts (the session is taken over), worth knowing
                          f" (child not in a kill-on-exit job, winerr={frames_mon.job_error})"))
                frames_reported = True
            elif frames_mon.error:
                status(f"[frames] {frames_mon.error} — frame stats off, legacy detection only")
                frames_reported = True
        if frames_mon is not None:
            # A dead stream is only news while something is drawing: a still
            # desktop presents no frames, and that is not a fault.
            busy = bool(presenters) or (snap.gpu.load_pct or 0.0) >= min_gpu
            g.run("capture-observe", lambda: frames_mon.observe(busy, dt))
            warn = g.run("capture-warning", frames_mon.stream_warning, None)
            if warn and warn != frames_warned:
                frames_warned = warn
                status(warn)

        # A detector that raises falls back to the previous state rather than to
        # "idle": the panel flipping to the idle layout on a parser bug would be the
        # exact complaint this pass is fixing.
        state = args.force_state or g.run(
            "watch", lambda: watch.tick(snap, dt, presenters=presenters, steam=steam),
            prev_state or watch.state)
        if state == watch.GAME and frames_mon is not None and watch.game_pid is not None:
            fs = g.run("frame-stats", lambda: frames_mon.stats(watch.game_pid), None)
            if fs is not None:
                snap.frames = fs
        state_changed = prev_state is not None and state != prev_state
        prev_state = state
        if state_changed:
            # which detector fired, and on what — the one line to read when the
            # panel is in the wrong mode (see README: frame stats & game detection).
            # `frames=no` here means the panel will read `--`: `snap.frames` is
            # always the dataclass, so only its fps field can say whether the
            # present stream actually filled it in.
            status(f"[state] {watch.state} pid={watch.game_pid} steam={watch.steam_appid} "
                   f"frames={'no' if snap.frames.fps is None else f'{snap.frames.fps:.0f}'}"
                   f"{' held' if snap.frames.stale else ''} | {g.text('watch.summary', watch.summary)}")
        if demo is not None:
            demo.game = state == "game"

        # The estimate can legitimately carry no total (an unreadable component is
        # not zero watts); storing that None is the point: the snapshot keeps
        # saying unavailable/partial instead of holding an old watt figure.
        est = g.run("power-model", lambda: estimate(snap, cfg), None)
        if est is not None:
            snap.power_total_w = est.total_w

        # ---- the one light decision: dark? bright? warm? ----------------------
        # The night read touches files owned by another process (CloudStore rewrites
        # them as Windows saves the setting), so a half-written blob is a torn read:
        # keep the last known state instead of failing the tick.
        if g.run("night", night.refresh, False):
            line = g.run("night-report", night.changed_to_log, "")
            if line:
                status(line)
        # Falling back to the previous plan is the point: the panel keeps the light it
        # had rather than being driven to some default by a config read that failed.
        # `playing` is the evidence that somebody is at the desk even though the idle
        # clock says otherwise: `GetLastInputInfo` counts keyboard and mouse only, so a
        # controller looks exactly like an empty room, and without this the panel would
        # dim at five minutes and go dark at forty-five mid-game. `stale` — the game
        # alive but not presenting — deliberately does not count.
        playing = (state == watch.GAME and snap.frames.fps is not None
                   and not snap.frames.stale)
        plan = g.run("lights",
                     lambda: lights.tick(state, host.idle_s, dt, playing=playing), plan)
        if plan.idle_held != idle_held:
            idle_held = plan.idle_held
            status(f"[light] idle timer {'held — the game is presenting frames' if idle_held else 'released'}"
                   f" — idle={host.idle_s:.0f}s")
        if plan.reason != last_light:
            last_light = plan.reason
            if plan.reason != "lit":
                status(f"[light] {plan.describe()}")
        if not dump_mode and panel is not None and pusher is not None:
            if plan.dark:
                if dark_logged != plan.reason:
                    dark_logged = plan.reason
                    status(f"[light] panel off ({plan.reason}) — "
                           f"{g.text('host.summary', host.summary)}")
                g.run("screen-off", lambda: panel.screen(False))
                burn.defer(t0)          # an exercise nobody can see is just a wakeup
                if plan.lut:
                    g.run("warm-lut", lambda: layout.set_warm(plan.lut))
                # Nothing is pushed while dark: on a 115200-baud link a full frame is
                # ~0.8 s of bus, and the pixels are not visible anyway. The diff is
                # invalidated so the first frame after waking is whole.
                g.run("invalidate", pusher.invalidate)
            else:
                if dark_logged:
                    status(f"[light] panel on again ({plan.describe()})")
                    dark_logged = ""
                if holds:
                    # Recovery is in flight. Whatever this tick would send was composed
                    # before the event that started the rebuild, and lighting the panel
                    # in the middle of a bring-up is the visible half of the old
                    # behaviour. The commands resume on the next tick, with whatever is
                    # wanted then - which is the point of handing the light state in
                    # every tick rather than remembering it from the event.
                    if held_logged != "waiting":
                        held_logged = "waiting"
                        status(f"[light] waiting on recovery ({plan.describe()}) - not "
                               f"lighting the panel while it is being re-made")
                else:
                    held_logged = ""
                    g.run("warm-lut", lambda: layout.set_warm(plan.lut))
                    g.run("brightness", lambda: panel.set_brightness(plan.brightness))
                    g.run("screen-on", lambda: panel.screen(True))
                if plan.repaint:
                    g.run("invalidate", pusher.invalidate)

        # ---- burn-in: exercise sweep (idle, and only on a lit panel) ----------
# A call, not a loop: `sweep_step` advances one frame and lets this tick's
        # light plan, game state, link health and the recovery hold veto it. `panel.ok`
        # is a plain attribute, so asking it costs the tick nothing and cannot be a
        # second way for the device to fail.
        sweep_owned = False
        if pusher is not None:
            sweep_owned = g.run("sweep", lambda: sweep_step(
                burn, layout, pusher, plan, state,
                panel is not None and panel.ok, holds, g),
                False)

        # shift changed → next push is automatically full via diff (large change)
        shift = burn.shift()
        # History takes measured samples only: a held one (the hub's last-good
        # re-publication while the backend is blind) would draw the blind
        # window as a flat measured line, and the bands' contract is that
        # missing data is a gap. The panel still renders held numbers until
        # the grace expires; after it the Nones draw gaps and "--".
        if not snap.held:
            g.run("history", lambda: layout.observe(snap, state))   # feeds the 60s bands
        # Still observed while dark, so the trend bands are continuous across a
        # wake — but not rendered: the pixels cannot be seen and the draw is not
        # free (~40 ms of a 1 Hz tick).
        frame = None if (plan.dark and not dump_mode) else g.run(
            "render", lambda: layout.render(snap, state, shift), None)

        if dump_mode:
            if frame is not None:      # a render that raised is not a rendered frame
                args.frames -= 1
                if args.frames <= 0:
                    out = Path(args.dump)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    frame.save(out)
                    print(f"saved {out}")
                    return
        else:
# `pusher` is None only when the link could not be built at all. The frame
            # is still rendered and the history still fed, so the moment the retry lands
            # there is something current to show; an `assert` here used to be a way for
            # a screen that is not plugged in to end the process two lines after the
            # guard promised it never could.
            # `holds` is the same rule as the light commands above, on the same tick:
            # a frame composed before the event that started the rebuild is exactly the
            # stale intermediate frame that used to appear while the panel rebooted.
            # `sweep_owned` is the one frame this tick already paid for: pushing the
            # telemetry frame underneath it would double the bus and undo the sweep.
            if (pusher is not None and frame is not None and not plan.dark
                    and not holds and not sweep_owned):
                if state_changed and can_wipe:
                    g.run("wipe", lambda: wipe(pusher, layout.blank(), frame, hold_s))
                else:
                    g.run("push", lambda: pusher.push(frame))
                if shift != last_shift:
                    last_shift = shift
            if errors and t0 - last_err_at > 300.0:
                errors = 0        # a long clean stretch resets the counter

        # Park on the event flag, not on the clock: a suspend query or a display-off
        # has seconds, not a whole tick, to be acted on.
        # Progress, written for the one reader that is not this loop. The `[beat]` line
        # proves the loop is alive to whoever reads log.log; this proves it to something
        # that is neither in this process nor able to read that file, which is the only
        # way a *hung* loop is observable at all — its own log cannot say anything,
        # because the line that would say it is the line that never gets written.
        # Cheap by construction: one small file, replaced atomically, never raises.
        beat_liveness(tick=ticks, state=state)
        if t0 >= next_beat:
            next_beat = t0 + beat_every_s
            up = int(t0 - started)

            def _beat(up=up, state=state, plan=plan, snap=snap) -> None:
                # The line that proves the loop is alive is built out of every
                # subsystem at once, so it is inside the guard as a whole: the one
                # thing it must never become is the reason the loop stops.
                # `lit 70+play` says the idle timer has expired and a live game is
                # holding it — "lit" and "lit despite the timer" read very differently
                # in the morning.
                light = light_text(plan)
                # `panel` is None in --dump mode, and this line must never be the thing
                # that faults: it was, in a headless run, for a whole round before the
                # `[fault]` line that proves the guard caught it gave it away.
                if panel is None:
                    pl = "headless"
                elif panel.ok:
                    pl = "up"
                else:
                    pl = f"down({(panel.down_reason or '?')[:40]})"
                # Recovery only has a line when it has done something, so the beat
                # stays quiet on an ordinary night and says `recovery=1 pass
                # coalesced=4 holding` on the morning when it did not.
                rec = recovery.summary() if recovery is not None else ""
                status(f"[beat] up={up // 60}m{up % 60:02d}s {state} "
                       f"light={light}"
                       # `capture=live` is the only positive proof the ETW child is
                       # still feeding us: on an idle desk nothing presents, so
                       # `frames=--` is correct and would otherwise read as a fault.
                       f" capture={'none' if frames_mon is None else ('live' if frames_mon.ok else 'down')}"
                       f" panel={pl}"
                       f" frames={'--' if snap.frames.fps is None else f'{snap.frames.fps:.0f}'}"
                       f"{'+held' if snap.frames.stale else ''}"
                       f"{(' ' + rec) if rec else ''}"
                       f"{g.summary()}{f' +sensors{errors}' if errors else ''}"
                       f" | {host.summary()}")

            g.run("beat", _beat)
        work_s = time.monotonic() - t0        # what this tick cost: see `host.tick` above
        sleep_left = interval - work_s
        if sleep_left > 0:
            # The event pump is ctypes calling into user32 like everything else here:
            # a raise inside it is as fatal as a raise anywhere else in the tick.
            g.run("wait", lambda: host.wait(sleep_left))


if __name__ == "__main__":
    _console_safe()
    try:
        main()
    except KeyboardInterrupt:
        # Stopped on purpose, so say so where the outside observer looks: recovery
        # that relaunches the app the user just stopped is not recovery. Not written
        # from `atexit`, because an unhandled exception unwinds through `atexit` too,
        # and that is precisely the case the observer must still act on.
        mark_stopped("keyboard-interrupt")
        bootlog.note("[boot] stopped on request (Ctrl-C)")
    except BaseException:  # noqa: BLE001 - start-up is the one place a death is final
        # Setup failures (config unreadable, sensors library missing, port denied)
        # still end the process — but under pythonw.exe nothing else would ever say
        # so. A crash must leave a sentence in the log, never silence. It has to reach
        # boot.log as well, because on the machine where the vendored logger is the
        # thing that went wrong, log.log is exactly the file that will not be written.
        # Exiting non-zero is the point: that is what the task's bounded
        # restart-on-failure policy reacts to, and `app/liveness.py` is what keeps it
        # from becoming a relaunch loop.
        detail = traceback.format_exc(limit=6).strip().replace("\n", " | ")
        app_log("[fatal] " + detail)
        bootlog.note("[fatal] " + detail
                     + " — exiting non-zero: the task retries a bounded number of times "
                     "and then leaves it alone. If the cause is the config or the "
                     "install, restarting will not help.")
        raise
