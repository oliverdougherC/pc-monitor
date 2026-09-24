#!/usr/bin/env python
"""PC desk-monitor app: telemetry → 5" USB LCD (Turing family) or simulator.

Run:
  python main.py                     # live loop (SIMU in config → http://localhost:5678)
  python main.py --backend demo      # synthetic data, no sensors needed
  python main.py --dump p.png        # render one frame headless and exit
  python main.py --force-state game  # preview the game layout
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import config as cfgmod
from app.burnin import BurnIn
from app.display import app_log, make_lcd
from app.frames import FrameMonitor
from app.gamewatch import GameWatch
from app.layout import Layout
from app.output import DiffPusher, wipe, wipe_supported
from app.power import estimate
from app.sensors import make_hub
from app.steamid import SteamIdentity


def status(msg: str) -> None:
    """Report something worth reading: print() for a console run, and log.log,
    because the scheduled task runs pythonw.exe, where sys.stdout is None and
    every print() silently vanishes."""
    print(msg)
    app_log(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--backend", default=None, help="auto|lhm|fallback|demo")
    ap.add_argument("--force-state", choices=["idle", "game"], default=None)
    ap.add_argument("--dump", default=None, help="render N frames headless, save PNG, exit")
    ap.add_argument("--frames", type=int, default=3, help="ticks before --dump saves")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    hub = make_hub(cfg, force=args.backend)
    interval = float(cfg["sensors"]["interval_s"])
    layout = Layout(cfg, rate_hz=1.0 / interval)
    watch = GameWatch(cfg)
    burn = BurnIn(cfg)

    # real per-process present telemetry (ETW, needs admin); degrades to None/{}
    frames_mon = FrameMonitor(cfg) if str(cfg["frames"].get("source", "auto")) != "off" else None
    steam = SteamIdentity()
    if frames_mon is not None:
        import atexit
        atexit.register(frames_mon.close)  # don't leave an orphaned ETW session behind
        if frames_mon.error:
            status(f"[frames] {frames_mon.error} — frame stats off, legacy detection only")

    dump_mode = bool(args.dump)
    lcd = None if dump_mode else make_lcd(cfg)
    pusher = None if dump_mode else DiffPusher(lcd)
    panel = "headless dump" if dump_mode else f"{lcd.get_width()}x{lcd.get_height()}"
    if frames_mon is None:
        frames_desc = "off"
    elif frames_mon.output_file:
        frames_desc = "file-capture"
        status(f"[frames] raw capture → {frames_mon.output_file}; frame stats off by design")
    else:
        frames_desc = "starting"
    status(f"[start] pid={os.getpid()} ppid={os.getppid()} backend={type(hub.backend).__name__} "
           f"revision={cfg['display']['revision']} port={cfg['display']['com_port']} panel={panel} "
           f"frames={frames_desc} interval={cfg['sensors']['interval_s']}s")
    last_shift = burn.shift()
    # idle→game reflow is hidden by a dark frame; serial boards can't afford it
    can_wipe = not dump_mode and wipe_supported(cfg["display"]["revision"], cfg)
    hold_s = float(cfg["layout"].get("transition_hold_s", 0.12))
    prev_state = None

    from app.sensors.demo import DemoBackend
    demo = hub.backend if isinstance(hub.backend, DemoBackend) else None

    # prime interval-based counters
    hub.tick()
    time.sleep(0.2)

    tick_dt = time.monotonic()
    frames_reported = frames_mon is None
    frames_warned: str | None = None
    min_gpu = float(cfg["game"]["min_gpu_load"])
    while True:
        t0 = time.monotonic()
        snap = hub.tick()
        dt = t0 - tick_dt
        tick_dt = t0

        presenters = frames_mon.presenters() if frames_mon is not None else None
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
            frames_mon.observe(busy, dt)
            warn = frames_mon.stream_warning()
            if warn and warn != frames_warned:
                frames_warned = warn
                status(warn)

        state = args.force_state or watch.tick(snap, dt, presenters=presenters, steam=steam)
        if state == watch.GAME and frames_mon is not None and watch.game_pid is not None:
            fs = frames_mon.stats(watch.game_pid)
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
                   f"frames={'no' if snap.frames.fps is None else f'{snap.frames.fps:.0f}'}")
        if demo is not None:
            demo.game = state == "game"

        total, _ = estimate(snap, cfg)
        snap.power_total_w = total

        # burn-in: brightness + screen off
        if lcd is not None:
            burn.tick_brightness(state, lcd)

        # burn-in: exercise sweep (only when idle)
        if burn.exercise_due(t0) or burn.exercise_progress(t0) is not None:
            while True:
                now = time.monotonic()
                p = burn.exercise_progress(now)
                if p is None or state == "game":
                    break
                frame = layout.sweep(p)
                if dump_mode:
                    break
                assert pusher is not None
                pusher.push(frame)
                time.sleep(0.12)
            if pusher is not None:
                pusher.invalidate()  # restore full frame after sweep
            if dump_mode:
                pass  # fall through to normal render below

        # shift changed → next push is automatically full via diff (large change)
        shift = burn.shift()
        layout.observe(snap, state)          # feeds the 60s trend bands
        frame = layout.render(snap, state, shift)

        if dump_mode:
            args.frames -= 1
            if args.frames <= 0:
                out = Path(args.dump)
                out.parent.mkdir(parents=True, exist_ok=True)
                frame.save(out)
                print(f"saved {out}")
                return
        else:
            assert pusher is not None
            if state_changed and can_wipe:
                wipe(pusher, layout.blank(), frame, hold_s)
            else:
                pusher.push(frame)
            if shift != last_shift:
                last_shift = shift

        sleep_left = interval - (time.monotonic() - t0)
        if sleep_left > 0:
            time.sleep(sleep_left)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
