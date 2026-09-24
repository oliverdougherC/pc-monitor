#!/usr/bin/env python
"""Drive the real panel once, from our own wiring, and measure the link.

Run (the screen must be plugged in):
  python tools/screen_bench.py                 # hello + 1 full frame + band pushes
  python tools/screen_bench.py --frames 3 --bands 20
  python tools/screen_bench.py --state game    # push the in-game layout instead
  python tools/screen_bench.py --revision TUR_USB --backend demo

Why this exists: README's bandwidth table was written from the protocol spec, not
from the panel. Serial revisions pad every 249 payload bytes with a 0x00 and send
BGRA, so what a full frame and a trend band actually cost here is a measured
number, and it decides whether `layout.trend_bands` can stay on. This script
prints the display's own ID string, sub-revision and ROM version too — the
fastest way to tell a 5" (480x800) unit from a 2.1"/2.8" one when the vendor
label lies.

Everything goes through app.display.make_lcd() and app.layout.Layout, i.e. the
same path `python main.py` uses, so a pass here means the app will drive the
panel.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as cfgmod  # noqa: E402
from app.display import make_lcd  # noqa: E402
from app.layout import Layout  # noqa: E402
from app.sensors import make_hub  # noqa: E402


class Meter:
    """Count bytes and seconds inside the driver's serial calls."""

    def __init__(self, lcd):
        self.lcd = lcd
        self.wbytes = 0
        self.rbytes = 0
        self.wseconds = 0.0
        self._orig_write = lcd.serial_write
        self._orig_read = lcd.serial_read
        meter = self

        def write(data):
            t = time.perf_counter()
            r = meter._orig_write(data)
            meter.wseconds += time.perf_counter() - t
            meter.wbytes += len(data)
            return r

        def read(size):
            data = meter._orig_read(size)
            meter.rbytes += len(data or b"")
            return data

        lcd.serial_write = write
        lcd.serial_read = read

    def reset(self) -> None:
        self.wbytes = self.rbytes = 0
        self.wseconds = 0.0

    def report(self, label: str, seconds: float, pixels: int) -> None:
        rate = self.wbytes / seconds / 1024 if seconds > 0 else 0
        px = pixels / seconds if seconds > 0 else 0
        print(f"  {label:22s} {seconds:7.2f} s  {self.wbytes / 1024:8.1f} KiB out"
              f"  {self.rbytes / 1024:6.1f} KiB in  {rate:7.1f} KiB/s"
              + (f"  {px / 1000:6.1f} kpx/s" if pixels else ""))


def say_id(lcd) -> None:
    """Ask revision-C-class panels who they are (raw HELLO reply, unfiltered)."""
    try:
        from library.lcd.lcd_comm_rev_c import Command
    except ImportError:
        print("  (not a serial revision: no HELLO handshake)")
        return
    try:
        lcd.serial_flush_input()
        lcd._send_command(Command.HELLO, bypass_queue=True)
        raw = lcd.serial_read(23)
        lcd.serial_flush_input()
        print(f"  raw HELLO   : {raw!r}")
        print(f"  sub-revision: {getattr(lcd, 'sub_revision', '?')}   ROM: {getattr(lcd, 'rom_version', '?')}")
    except Exception as e:  # never let the identification step kill the bench
        print(f"  (HELLO failed: {e})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--revision", default=None, help="override display.revision")
    ap.add_argument("--com-port", default=None, help="override display.com_port")
    ap.add_argument("--backend", default="demo", help="sensor backend for the test frame")
    ap.add_argument("--state", default="idle", choices=["idle", "game"])
    ap.add_argument("--frames", type=int, default=1)
    ap.add_argument("--bands", type=int, default=6)
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    if args.revision:
        cfg["display"]["revision"] = args.revision
    if args.com_port:
        cfg["display"]["com_port"] = args.com_port
    rev = str(cfg["display"]["revision"]).upper()
    w, h = int(cfg["display"]["portrait_width"]), int(cfg["display"]["portrait_height"])
    print(f"revision={rev} port={cfg['display']['com_port']} panel={w}x{h} portrait "
          f"orientation={cfg['display']['orientation']}")

    print("\nConnecting (a revision-C Reset reboots the panel and may take ~30 s)...")
    t0 = time.perf_counter()
    lcd = make_lcd(cfg)
    print(f"  up in {time.perf_counter() - t0:.1f} s  "
          f"draw size {lcd.get_width()}x{lcd.get_height()}")

    m = Meter(lcd)
    print("\nIdentity:")
    say_id(lcd)

    hub = make_hub(cfg, force=args.backend)
    layout = Layout(cfg, rate_hz=1.0)
    snap = hub.tick()
    time.sleep(0.2)
    frame = layout.render(snap, args.state, (0, 0))
    print(f"\nTest frame: {frame.size[0]}x{frame.size[1]} '{args.state}' layout via the {args.backend} backend")

    print("\nFull-frame pushes:")
    m.reset()
    t = time.perf_counter()
    for _ in range(max(1, args.frames)):
        lcd.DisplayPILImage(frame, 0, 0)
    dt = (time.perf_counter() - t) / max(1, args.frames)
    m.report("one full frame", dt, frame.size[0] * frame.size[1])
    print(f"  => a 1 Hz full refresh would need {dt:.1f} s/frame"
          f" ({100 * min(1.0, dt / 1.0):.0f}% of the tick)" if dt > 1 else
          f"  => a 1 Hz full refresh is affordable ({dt * 1000:.0f} ms/frame)")

    print("\nBand pushes (what the diff transport actually sends per tick):")
    for (x0, y0, x1, y1) in ((0, 440, 800, 480), (18, 232, 400, 272), (418, 232, 800, 272)):
        band = frame.crop((x0, y0, x1, y1))
        m.reset()
        t = time.perf_counter()
        for _ in range(max(1, args.bands)):
            lcd.DisplayPILImage(band, x0, y0)
        dt = (time.perf_counter() - t) / max(1, args.bands)
        m.report(f"{x1 - x0}x{y1 - y0} band x{args.bands}", dt, (x1 - x0) * (y1 - y0))

    print("\nDone — look at the panel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
