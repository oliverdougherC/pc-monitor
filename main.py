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

Sleep is the reason for that shape. The loop freezes when the machine sleeps, so
"turn the panel off when the PC sleeps" cannot be a rule evaluated after the fact —
it has to be an event that wakes us. Hence `host.wait()` below instead of
`time.sleep()`, and a resume path that rebuilds the panel link and the ETW capture
rather than assuming the world is as we left it.
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

from app import config as cfgmod                       # noqa: E402
from app.burnin import BurnIn                          # noqa: E402
from app.display import app_log                        # noqa: E402
from app.frames import FrameMonitor                    # noqa: E402
from app.gamewatch import GameWatch                    # noqa: E402
from app.hoststate import HostState                    # noqa: E402
from app.layout import Layout                          # noqa: E402
from app.lights import LightPlanner                    # noqa: E402
from app.nightlight import NightLight                  # noqa: E402
from app.output import DiffPusher, wipe, wipe_supported   # noqa: E402
from app.panel import PanelLink                        # noqa: E402
from app.power import estimate                         # noqa: E402
from app.sensors import make_hub                       # noqa: E402
from app.steamid import SteamIdentity                  # noqa: E402


def status(msg: str) -> None:
    """Report something worth reading: print() for a console run, and log.log,
    because the scheduled task runs pythonw.exe, where sys.stdout is None and
    every print() silently vanishes."""
    print(msg)
    app_log(msg)


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
               g: "Guard", now: float | None = None) -> bool:
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
    for pixels that cannot arrive, which is how a sweep becomes a recovery loop.
    """
    now = time.monotonic() if now is None else now
    if not (burn.exercise_due(now) or burn.exercise_progress(now) is not None):
        return False
    if plan.dark or state == "game" or not panel_ok:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--backend", default=None, help="auto|lhm|fallback|demo")
    ap.add_argument("--force-state", choices=["idle", "game"], default=None)
    ap.add_argument("--dump", default=None, help="render N frames headless, save PNG, exit")
    ap.add_argument("--frames", type=int, default=3, help="ticks before --dump saves")
    args = ap.parse_args()
    _console_safe()

    cfg = cfgmod.load(args.config)
    hub = make_hub(cfg, force=args.backend)
    interval = float(cfg["sensors"]["interval_s"])
    layout = Layout(cfg, rate_hz=1.0 / interval)
    watch = GameWatch(cfg)
    burn = BurnIn(cfg)
    host = HostState(gap_s=float(cfg["power"].get("wake_gap_s", 5.0)))
    night = NightLight(cfg, refresh_s=float(cfg["night"].get("refresh_s", 3.0)))
    lights = LightPlanner(cfg, night=night, host=host)

    # real per-process present telemetry (ETW, needs admin); degrades to None/{}
    frames_mon = FrameMonitor(cfg) if str(cfg["frames"].get("source", "auto")) != "off" else None
    steam = SteamIdentity()
    if frames_mon is not None:
        import atexit
        atexit.register(frames_mon.close)  # don't leave an orphaned ETW session behind
        if frames_mon.error:
            status(f"[frames] {frames_mon.error} — frame stats off, legacy detection only")

    panel = None
    pusher = None
    dump_mode = bool(args.dump)

    def _shutdown() -> None:
        """Give the port back and stop the event window on any exit path."""
        try:
            host.close()
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
        # endpoint, and the panel rebooting into its own default orientation.
        panel = PanelLink(cfg, log=status)
        pusher = DiffPusher(panel)
        if not panel.open(on_relink=pusher.invalidate):
            status(f"[panel] no screen on start ({panel.down_reason}) — continuing anyway, "
                   f"the loop keeps retrying every 10 s; plug it in and it will come up")
        panel_desc = f"{panel.get_width()}x{panel.get_height()}"
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
    demo = hub.backend if isinstance(hub.backend, DemoBackend) else None

    # prime interval-based counters
    prev_snap = hub.tick()
    time.sleep(0.2)

    # Containment for everything the loop cannot control, and a first light decision
    # so a fault in the planner later on has something to fall back to instead of
    # leaving `plan` unbound in front of the panel.
    _thread_safety_net()
    g = Guard()
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
    frames_reported = frames_mon is None
    frames_warned: str | None = None
    min_gpu = float(cfg["game"]["min_gpu_load"])
    errors = 0
    last_err = ""
    last_err_at = 0.0
    last_light = ""
    dark_logged = ""
    idle_held = False
    # A heartbeat, because every other line here is conditional and the failure mode
    # of a loop that died at 4 a.m. is a panel frozen on last night's numbers with a
    # log that says nothing wrong. One line every 15 min (first one after a minute,
    # so a start is provably alive without waiting a quarter of an hour).
    beat_every_s = float(cfg["display"].get("heartbeat_s", 900))
    next_beat = time.monotonic() + 60.0
    started = time.monotonic()
    while True:
        t0 = time.monotonic()
        try:
            snap = hub.tick()
        except Exception as e:  # noqa: BLE001 - a sensor that throws once must not stop the panel
            errors += 1
            if f"{type(e).__name__}" != last_err or t0 - last_err_at > 300.0:
                last_err, last_err_at = f"{type(e).__name__}", t0
                status(f"[sensors] tick failed ({type(e).__name__}: {e}) — using the last "
                       f"snapshot; {errors} total")
            snap = prev_snap
        else:
            prev_snap = snap

        dt = min(max(t0 - tick_dt, 0.0), 60.0)   # a frozen loop must not fake a long dt
        tick_dt = t0
        g.run("host", lambda: host.tick(dt))

        # A resume is the only moment everything downstream has to be re-made: the
        # COM port came back (or has not yet), the panel rebooted into portrait, the
        # ETW session is a husk, and the game we locked onto was frozen mid-frame.
        reason = g.run("take-resume", host.take_resume, None)
        if reason and not dump_mode:
            status(f"[resume] {reason} — {g.text('host.summary', host.summary)}")
            # Each recovery on its own: a relink that raises must not also leave the
            # capture dead, or one fault becomes two.
            if not g.run("relink", lambda: panel.relink(reason), False):
                status("[resume] panel not answering yet; retrying in the background")
            if frames_mon is not None:
                g.run("capture-restart", lambda: frames_mon.restart(reason))
                frames_reported = False      # say again whether the session came back
            g.run("watch-reset", lambda: watch.reset(reason))

        # Background link maintenance: when the screen is down this retries the port
        # on its own clock (and, past a couple of minutes, asks Windows to restart the
        # USB device). Cheap and immediate when it is up.
        if not dump_mode:
            g.run("panel", lambda: panel.tick())

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

        total, _ = g.run("power-model", lambda: estimate(snap, cfg), (None, None))
        if total is not None:
            snap.power_total_w = total

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
        if not dump_mode:
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
                g.run("warm-lut", lambda: layout.set_warm(plan.lut))
                g.run("brightness", lambda: panel.set_brightness(plan.brightness))
                g.run("screen-on", lambda: panel.screen(True))
                if plan.repaint:
                    g.run("invalidate", pusher.invalidate)

        # ---- burn-in: exercise sweep (idle, and only on a lit panel) ----------
        # A call, not a loop: `sweep_step` advances one frame and lets this tick's
        # light plan, game state and link health veto it. `panel.ok` is a plain
        # attribute, so asking it costs the tick nothing and cannot be a second way
        # for the device to fail.
        sweep_owned = g.run("sweep", lambda: sweep_step(
            burn, layout, pusher, plan, state, panel is not None and panel.ok, g),
            False)

        # shift changed → next push is automatically full via diff (large change)
        shift = burn.shift()
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
            assert pusher is not None
            # `sweep_owned` is the one frame this tick already paid for: pushing the
            # telemetry frame underneath it would double the bus and undo the sweep.
            if frame is not None and not plan.dark and not sweep_owned:
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
                status(f"[beat] up={up // 60}m{up % 60:02d}s {state} "
                       f"light={light}"
                       # `capture=live` is the only positive proof the ETW child is
                       # still feeding us: on an idle desk nothing presents, so
                       # `frames=--` is correct and would otherwise read as a fault.
                       f" capture={'none' if frames_mon is None else ('live' if frames_mon.ok else 'down')}"
                       f" panel={pl}"
                       f" frames={'--' if snap.frames.fps is None else f'{snap.frames.fps:.0f}'}"
                       f"{'+held' if snap.frames.stale else ''}"
                       f"{g.summary()}{f' +sensors{errors}' if errors else ''}"
                       f" | {host.summary()}")

            g.run("beat", _beat)
        sleep_left = interval - (time.monotonic() - t0)
        if sleep_left > 0:
            # The event pump is ctypes calling into user32 like everything else here:
            # a raise inside it is as fatal as a raise anywhere else in the tick.
            g.run("wait", lambda: host.wait(sleep_left))


if __name__ == "__main__":
    _console_safe()
    try:
        main()
    except KeyboardInterrupt:
        pass
    except BaseException:  # noqa: BLE001 - start-up is the one place a death is final
        # Setup failures (config unreadable, sensors library missing, port denied)
        # still end the process — but under pythonw.exe nothing else would ever say
        # so. A crash must leave a sentence in the log, never silence.
        app_log("[fatal] " + traceback.format_exc(limit=6).strip().replace("\n", " | "))
        raise
