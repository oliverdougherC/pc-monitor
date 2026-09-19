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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "turing-smart-screen-python"))

from app import config as cfgmod
from app.burnin import BurnIn
from app.gamewatch import GameWatch
from app.layout import Layout
from app.output import DiffPusher
from app.power import estimate
from app.sensors import make_hub


def make_lcd(cfg: dict):
    rev = str(cfg["display"]["revision"]).upper()
    w, h = int(cfg["display"]["portrait_width"]), int(cfg["display"]["portrait_height"])
    port = cfg["display"]["com_port"]

    from library.lcd.lcd_comm import Orientation  # noqa: F401 (kept for symmetry)
    if rev == "SIMU":
        from library.lcd.lcd_simulated import LcdSimulated
        lcd = LcdSimulated(display_width=w, display_height=h)
    else:
        cls_map = {
            "A": ("library.lcd.lcd_comm_rev_a", "LcdCommRevA"),
            "B": ("library.lcd.lcd_comm_rev_b", "LcdCommRevB"),
            "C": ("library.lcd.lcd_comm_rev_c", "LcdCommRevC"),
            "D": ("library.lcd.lcd_comm_rev_d", "LcdCommRevD"),
            "TUR_USB": ("library.lcd.lcd_comm_turing_usb", "LcdCommTuringUSB"),
            "WEACT_A": ("library.lcd.lcd_comm_weact_a", "LcdCommWeActA"),
            "WEACT_B": ("library.lcd.lcd_comm_weact_b", "LcdCommWeActB"),
        }
        if rev not in cls_map:
            raise SystemExit(f"unknown display revision: {rev}")
        import importlib
        mod = importlib.import_module(cls_map[rev][0])
        lcd = getattr(mod, cls_map[rev][1])(com_port=port, display_width=w, display_height=h)

    lcd.Reset()
    lcd.InitializeComm()
    if str(cfg["display"]["orientation"]).lower() == "landscape":
        from library.lcd.lcd_comm import Orientation
        lcd.SetOrientation(Orientation.LANDSCAPE)
    return lcd


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

    dump_mode = bool(args.dump)
    lcd = None if dump_mode else make_lcd(cfg)
    pusher = None if dump_mode else DiffPusher(lcd)
    last_shift = burn.shift()

    from app.sensors.demo import DemoBackend
    demo = hub.backend if isinstance(hub.backend, DemoBackend) else None

    # prime interval-based counters
    hub.tick()
    time.sleep(0.2)

    tick_dt = time.monotonic()
    while True:
        t0 = time.monotonic()
        snap = hub.tick()
        dt = t0 - tick_dt
        tick_dt = t0

        state = args.force_state or watch.tick(snap, dt)
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
