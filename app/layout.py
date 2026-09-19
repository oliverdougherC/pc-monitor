"""Frame rendering: 800x480 dark layouts for idle and game states.

Design rules:
- dark near-black background (burn-in + power friendly)
- CPU+RAM blue, GPU green, storage white, network pink, frametime tan
- big thick values (JetBrains Mono ExtraBold: tabular digits, no jitter),
  small dim labels
- one panel slot language in both states, so the screen does not have to be
  relearned when game mode starts:
    label → big values (baseline-aligned) → dim captions → 60s trend band →
    detail row → power row (dim caption left, value right)
- the trend band is the same element everywhere: a TRACK rectangle, a muted
  60s polyline (muted by blending toward TRACK so it never competes with the
  numbers), a bright dot on "now", and optionally a 4px level bar along its
  bottom edge. Gaps (None) are drawn as gaps, never interpolated.
- type sizes are chosen for the WORST-CASE string per slot, so a value can
  never collide with its neighbour, and never changes size while it updates.
- total system power always in the bottom strip, green→red over 50W→1000W.
- geometry lives in the *_BOX tables; content is inset by INNER and every line
  is placed by baseline, so rows stay aligned when fonts change.
"""
from __future__ import annotations

import time

from PIL import Image, ImageDraw, ImageFont

from app.history import History
from app.power import power_color, power_watts_for_display
from app.snapshot import Snapshot

BG = (9, 11, 15)
PANEL = (19, 23, 32)
BORDER = (55, 64, 82)
TRACK = (37, 43, 56)
DIM = (138, 148, 165)
DIMMER = (120, 130, 148)   # legible at brightness_idle (45%) — checked at dim=0.45
BLUE = (96, 165, 250)
GREEN = (72, 222, 128)
WHITE = (240, 243, 246)
TAN = (224, 208, 178)      # fps/frametime panel, distinct from disk white
PINK = (255, 106, 182)

W, H = 800, 480
INNER = 12                 # content inset inside a panel

# panel boxes: (x0, y0, x1, y1), landscape pixels
IDLE_TOP = {"cpu": (12, 12, 394, 224), "gpu": (406, 12, 788, 224)}
GAME_TOP = {"cpu": (12, 12, 238, 224), "fps": (250, 12, 550, 224), "gpu": (562, 12, 788, 224)}
# in-game drops NETWORK and widens RAM + DISK into two panels
BOT_BOX = {
    "idle": {"ram": (12, 234, 294, 424), "disk": (306, 234, 540, 424),
             "net": (552, 234, 788, 424)},
    "game": {"ram": (12, 234, 394, 424), "disk": (406, 234, 788, 424)},
}
POWER_BOX = (12, 436, 788, 472)

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _off(box, sx: int, sy: int):
    """Shift a panel box by the burn-in pixel shift."""
    return (box[0] + sx, box[1] + sy, box[2] + sx, box[3] + sy)


class Layout:
    def __init__(self, cfg: dict, rate_hz: float | None = None):
        self.cfg = cfg
        self.w, self.h = W, H
        self.fval = cfg["layout"]["font_value"]
        self.flabel = cfg["layout"]["font_label"]
        self.fsmall = cfg["layout"]["font_small"]
        # history length is derived from the sample rate so the window is
        # `trend_window_s` seconds whether we tick at 1 Hz (panel) or 2 Hz (liveview)
        win = float(cfg["layout"].get("trend_window_s", 60))
        dt = (1.0 / rate_hz) if rate_hz else float(cfg["sensors"].get("interval_s", 1.0))
        self.history = History(int(round(win / max(dt, 0.05))))
        self.samples = self.history.samples

    # ---------- text helpers ---------------------------------------------------
    def _font(self, path: str, size: int) -> ImageFont.FreeTypeFont:
        key = (path, size)
        f = _font_cache.get(key)
        if f is None:
            f = ImageFont.truetype(f"{self.cfg['_fonts']}/{path}", size)
            _font_cache[key] = f
        return f

    def _txt(self, d, xy, text, font, fill, anchor="la"):
        d.text(xy, text, font=self._font(font[0], font[1]), fill=fill, anchor=anchor)

    def _w(self, text: str, path: str, size: int) -> float:
        return self._font(path, size).getlength(text)

    def _row(self, d, x0: int, x1: int, y: int, left=None, right=None):
        """Left/right pair sharing one baseline (y). right = (text, color, size[, font])."""
        if left:
            self._txt(d, (x0, y), left[0], (self.fsmall, left[2]), left[1], "ls")
        if right:
            fnt = right[3] if len(right) > 3 else self.fval
            self._txt(d, (x1, y), right[0], (fnt, right[2]), right[1], "rs")

    def _pair(self, d, x: int, y: int, label: str, value: str, lab_col, val_col,
              lab_size: int, val_size: int, right_edge: int | None = None,
              present: bool = True):
        """`LABEL  value` run sharing one baseline: left-anchored at x, or ending
        at right_edge when given. Missing values go dim and small, like `_big`."""
        if not present:
            value, val_col, val_size = "--", DIMMER, max(13, int(val_size * 0.62))
        lw = self._w(label, self.fsmall, lab_size)
        vfont = self.fval if present else self.fsmall
        vw = self._w(value, vfont, val_size)
        gap = 7
        if right_edge is not None:
            self._txt(d, (right_edge - vw - gap, y), label, (self.fsmall, lab_size),
                      lab_col, "rs")
            self._txt(d, (right_edge, y), value, (vfont, val_size), val_col, "rs")
        else:
            self._txt(d, (x, y), label, (self.fsmall, lab_size), lab_col, "ls")
            self._txt(d, (x + lw + gap, y), value, (vfont, val_size), val_col, "ls")

    def _panel(self, d, box):
        d.rectangle(box, fill=PANEL, outline=BORDER, width=1)

    # ---------- the shared trend element ----------------------------------------
    @property
    def trends(self) -> bool:
        """Trend bands on/off. Read per render (not cached) so tools/liveview can
        flip it live through the config dict, and so `layout.trend_bands: false`
        is a real serial-transport budget mode, not just a preview option."""
        return bool(self.cfg["layout"].get("trend_bands", True))

    def _series(self, name: str):
        return self.history.series(name) if self.trends else []

    @staticmethod
    def _blend(a: tuple, b: tuple, t: float) -> tuple:
        return tuple(int(round(x * (1 - t) + y * t)) for x, y in zip(a, b))

    def _graph(self, d, x0: int, y0: int, x1: int, y1: int, series, color,
               span_min: float = 1.0, level=None, target=None, bar_h: int = 4,
               plain: bool = False):
        """TRACK band + muted 60s polyline (newest at the right edge) + 'now' dot.
        level: optional 0..1 fraction drawn as a bar along the bottom edge.
        plain: trend bands switched off (serial-budget mode) — the band becomes a
        solid level bar, with no polyline and no 'waiting for data' axis."""
        if plain:
            if level is not None:
                f = max(0.0, min(1.0, float(level)))
                d.rectangle((x0, y0, x1, y1), fill=TRACK)
                if f > 0:
                    d.rectangle((x0, y0, x0 + max(2, int((x1 - x0) * f)), y1), fill=color)
            return
        d.rectangle((x0, y0, x1, y1), fill=TRACK)
        if level is None:
            bar_h = 0
        top, bot = y0 + 3, y1 - bar_h - 3
        s = list(series[-self.samples:])
        vals = [v for v in s if v is not None]
        if len(vals) >= 2:
            lo, hi = min(vals), max(vals)
            if hi - lo < span_min:
                mid = (hi + lo) / 2.0
                lo, hi = mid - span_min / 2.0, mid + span_min / 2.0
            step = (x1 - x0 - 2) / max(1, self.samples - 1)
            height = max(1.0, bot - top)

            def pt(i, v):
                return (x1 - 1 - (len(s) - 1 - i) * step,
                        bot - (v - lo) / (hi - lo) * height)

            if target is not None and lo <= target <= hi:
                ty = bot - (target - lo) / (hi - lo) * height
                for tx in range(x0 + 1, x1 - 1, 7):
                    d.line((tx, ty, min(tx + 3, x1 - 1), ty), fill=self._blend(TRACK, DIM, 0.75))

            line_col = self._blend(TRACK, color, 0.5)
            run: list[tuple[float, float]] = []
            last_pt = None
            for i, v in enumerate(s):
                if v is None:
                    if len(run) > 1:
                        d.line(run, fill=line_col, width=1)
                    run = []
                else:
                    run.append(pt(i, v))
                    last_pt = run[-1]
            if len(run) > 1:
                d.line(run, fill=line_col, width=1)
            if last_pt:
                x, y = last_pt
                d.rectangle((x - 1, y - 1, x + 1, y + 1), fill=color)

        if level is not None:
            f = max(0.0, min(1.0, float(level)))
            if f > 0:
                d.rectangle((x0, y1 - bar_h, x0 + max(2, int((x1 - x0) * f)), y1), fill=color)
        elif len(vals) < 2:
            # no sensor for this metric (e.g. CPU temp on the psutil fallback):
            # show an empty axis rather than a blank box that reads as a bug
            mid_y = (top + bot) // 2
            for tx in range(x0 + 1, x1 - 1, 7):
                d.line((tx, mid_y, min(tx + 3, x1 - 1), mid_y),
                       fill=self._blend(TRACK, DIMMER, 0.5))

    # ---------- formatters ------------------------------------------------------
    @staticmethod
    def rate(bps: float | None) -> str:
        if bps is None:
            return "--"
        for unit, div in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
            if bps >= div:
                return f"{bps / div:.1f} {unit}"
        return f"{bps:.0f} B/s"

    @staticmethod
    def ghz(mhz: float | None) -> str:
        return "--" if mhz is None else f"{mhz / 1000:.2f}"

    @staticmethod
    def num(v: float | None, fmt: str = "{:.0f}") -> str:
        return fmt.format(v) if v is not None else "--"

    def _big(self, d, x: int, y: int, text: str, size: int, color, anchor: str,
             present: bool = True) -> None:
        """Big value; when the sensor is missing, a small dim '--' on the same
        baseline instead of a giant bright dash that reads as a broken number."""
        if present:
            self._txt(d, (x, y), text, (self.fval, size), color, anchor)
        else:
            self._txt(d, (x, y), "--", (self.fsmall, max(15, int(size * 0.5))),
                      DIMMER, anchor)

    def _value(self, v: float | None, fmt: str, color, size: int):
        """Right-slot tuple for `_row`: dimmed small '--' when unavailable."""
        if v is None:
            return ("--", DIMMER, max(13, int(size * 0.62)), self.fsmall)
        return (self.num(v, fmt), color, size)

    def _rate_value(self, v: float | None, color, size: int):
        if v is None:
            return ("--", DIMMER, max(13, int(size * 0.62)), self.fsmall)
        return (self.rate(v), color, size)

    # ---------- telemetry → history (call once per tick, before render) ---------
    def observe(self, snap: Snapshot, state: str = "idle") -> None:
        h = self.history
        h.push("cpu.temp", snap.cpu.temp_c)
        h.push("gpu.temp", snap.gpu.temp_c)
        h.push("ram.used", snap.ram_used_mb)
        h.push("disk.read", snap.disk_read_bps)
        h.push("disk.write", snap.disk_write_bps)
        h.push("net.down", snap.net_down_bps)
        h.push("net.up", snap.net_up_bps)
        f = snap.frames
        ms = f.latency_ms
        if ms is None and f.fps:
            ms = 1000.0 / f.fps
        h.push("frames.ms", ms)

    # ---------- CPU / GPU panel (same slots in both states) ---------------------
    def _chip_panel(self, d, box, name: str, color, temp, load, sub: str,
                    power_w, power_label: str, series,
                    big: int = 58, captions: bool = True, name_size: int = 15,
                    pow_size: int = 24):
        x0, y0, x1, y1 = box
        cx0, cx1 = x0 + INNER, x1 - INNER
        self._panel(d, box)
        self._txt(d, (cx0, y0 + 24), name, (self.flabel, name_size), color, "ls")

        big_base = y0 + (88 if captions else 82)
        self._big(d, cx0, big_base, self.num(temp, "{:.0f}\u00b0"), big, color, "ls",
                  temp is not None)
        self._big(d, cx1, big_base, self.num(load, "{:.0f}%"), big, color, "rs",
                  load is not None)
        if captions:
            self._txt(d, (cx0, big_base + 22), "TEMP", (self.fsmall, 12), DIMMER, "ls")
            self._txt(d, (cx1, big_base + 22), "LOAD", (self.fsmall, 12), DIMMER, "rs")
            band_top = big_base + 30
        else:
            band_top = big_base + 16

        detail_y = y1 - 48
        self._graph(d, cx0, band_top, cx1, detail_y - 24, series, color,
                    span_min=4.0, level=None if load is None else load / 100.0,
                    plain=not self.trends)

        self._txt(d, (cx0, detail_y), sub, (self.fsmall, 18), color, "ls")
        self._row(d, cx0, cx1, y1 - 20, left=(power_label, DIMMER, 13),
                  right=self._value(power_w, "{:.0f} W", color, pow_size))

    # ---------- bottom row: RAM --------------------------------------------------
    def _ram_panel(self, d, box, snap: Snapshot) -> None:
        x0, y0, x1, y1 = box
        cx0, cx1 = x0 + INNER, x1 - INNER
        self._panel(d, box)
        self._txt(d, (cx0, y0 + 24), "RAM", (self.flabel, 15), BLUE, "ls")
        used_gb = snap.ram_used_mb / 1000 if snap.ram_used_mb else None
        tot_gb = snap.ram_total_mb / 1000 if snap.ram_total_mb else None
        self._big(d, cx0, y0 + 78, self.num(used_gb, "{:.1f}") + " GB", 40, BLUE, "ls",
                  used_gb is not None)
        pct = None if not (used_gb and tot_gb) else used_gb / tot_gb
        self._txt(d, (cx1, y0 + 78), "--" if pct is None else f"{pct * 100:.0f}%",
                  (self.fsmall, 17), DIM, "rs")
        self._txt(d, (cx0, y0 + 104), f"of {tot_gb:.0f} GB" if tot_gb else "--",
                  (self.fsmall, 15), DIM, "ls")
        self._graph(d, cx0, y0 + 114, cx1, y0 + 150, self._series("ram.used"),
                    BLUE, span_min=1.0, level=pct, plain=not self.trends)
        free = None if not (used_gb and tot_gb) else tot_gb - used_gb
        self._row(d, cx0, cx1, y0 + 170, left=("FREE", DIMMER, 12),
                  right=self._value(free, "{:.1f} GB", DIM, 16))

    # ---------- bottom row: paired-rate panel (DISK / NETWORK) -------------------
    def _rate_panel(self, d, box, title, color, lab_a, val_a, ser_a,
                    lab_b, val_b, ser_b, wide: bool) -> None:
        x0, y0, x1, y1 = box
        cx0, cx1 = x0 + INNER, x1 - INNER
        self._panel(d, box)
        self._txt(d, (cx0, y0 + 24), title, (self.flabel, 15), color, "ls")
        if not self.trends:
            # serial budget mode: no history bands at all — stacked rows spaced to
            # fill the panel. Same shape in both widths on purpose: two side-by-side
            # 10-char rate columns ("999.9 MB/s") overlap in a 358 px column.
            mid = (y0 + 34 + y1 - 12) // 2
            vsize = 26
            for off, lab, val in ((mid - 34, lab_a, val_a), (mid + 46, lab_b, val_b)):
                self._row(d, cx0, cx1, off, left=(lab, DIMMER, 14),
                          right=self._rate_value(val, color, vsize))
            d.line((cx0, mid + 4, cx1, mid + 4), fill=BORDER, width=1)
            return
        if wide:
            # two columns: caption / big value / graph under each
            colw = int((cx1 - cx0 - 30) / 2)
            for col, (lab, val, ser) in enumerate(((lab_a, val_a, ser_a), (lab_b, val_b, ser_b))):
                left = col == 0
                tx = cx0 if left else cx1
                gx0 = cx0 + col * (colw + 30)
                gx1 = gx0 + colw
                self._txt(d, (tx, y0 + 54), lab, (self.fsmall, 13), DIMMER,
                          "ls" if left else "rs")
                self._txt(d, (tx, y0 + 98), self.rate(val), (self.fval, 28),
                          color if val is not None else DIMMER,
                          "ls" if left else "rs")
                self._graph(d, gx0, y0 + 112, gx1, y0 + 162, ser, color, span_min=1.0,
                            plain=not self.trends)
        else:
            # stacked rows: caption + value on one baseline, graph under each
            for row, (lab, val, ser) in enumerate(((lab_a, val_a, ser_a), (lab_b, val_b, ser_b))):
                base = y0 + 58 + row * 80
                self._row(d, cx0, cx1, base, left=(lab, DIMMER, 14),
                          right=self._rate_value(val, color, 26))
                self._graph(d, cx0, base + 8, cx1, base + 44, ser, color, span_min=1.0,
                            plain=not self.trends)
            d.line((cx0, y0 + 100, cx1, y0 + 100), fill=BORDER, width=1)

    # ---------- shared bottom row dispatch ---------------------------------------
    def _bottom_row(self, img, d, snap: Snapshot, sx: int, sy: int, state: str) -> None:
        boxes = BOT_BOX[state]
        self._ram_panel(d, _off(boxes["ram"], sx, sy), snap)
        disk_wide = len(boxes) == 2
        self._rate_panel(d, _off(boxes["disk"], sx, sy), "DISK", WHITE,
                         "READ", snap.disk_read_bps, self._series("disk.read"),
                         "WRITE", snap.disk_write_bps, self._series("disk.write"),
                         wide=disk_wide)
        if "net" in boxes:
            self._rate_panel(d, _off(boxes["net"], sx, sy), "NETWORK", PINK,
                             "DOWN", snap.net_down_bps, self._series("net.down"),
                             "UP", snap.net_up_bps, self._series("net.up"),
                             wide=False)

    # ---------- power strip ------------------------------------------------------
    def _power_strip(self, img, d, snap: Snapshot, sx: int, sy: int) -> None:
        x0, y0, x1, y1 = box = _off(POWER_BOX, sx, sy)
        cx0, cx1 = x0 + INNER, x1 - INNER
        self._panel(d, box)
        self._txt(d, (cx0, y0 + 24), "TOTAL POWER", (self.flabel, 14), DIM, "ls")

        w = power_watts_for_display(snap, self.cfg)
        gx0, gx1 = x0 + 160, x0 + 544
        if w is None:
            self._txt(d, (gx0, y0 + 24), "-- W", (self.fsmall, 14), DIMMER, "ls")
        else:
            p = self.cfg["power"]
            lo, hi = float(p["gradient_min_w"]), float(p["gradient_max_w"])
            t = max(0.0, min(1.0, (w - lo) / (hi - lo)))
            col = power_color(w, self.cfg)
            self._graph(d, gx0, y0 + 12, gx1, y0 + 21, [], col, level=t, bar_h=9,
                        plain=not self.trends)
            for tick in (0.25, 0.5, 0.75):
                tx = gx0 + int((gx1 - gx0) * tick)
                d.line((tx, y0 + 11, tx, y0 + 22), fill=PANEL, width=1)
            self._txt(d, (gx0, y0 + 31), f"{lo:.0f}", (self.fsmall, 11), DIMMER, "ls")
            self._txt(d, (gx1, y0 + 31), f"{hi:.0f}", (self.fsmall, 11), DIMMER, "rs")
            self._txt(d, (x0 + 676, y0 + 30), f"{w:.0f}", (self.fval, 34), col, "rs")
            self._txt(d, (x0 + 684, y0 + 30), "W", (self.fsmall, 18), col, "ls")
        self._txt(d, (cx1, y0 + 26), time.strftime("%H:%M"), (self.fsmall, 17), DIM, "rs")

    # ---------- state layouts ----------------------------------------------------
    def render(self, snap: Snapshot, state: str, shift=(0, 0)) -> Image.Image:
        sx, sy = shift
        img = Image.new("RGB", (self.w, self.h), BG)
        d = ImageDraw.Draw(img)
        if state == "game":
            self._game(d, snap, sx, sy)
        else:
            self._idle(d, snap, sx, sy)
        self._bottom_row(img, d, snap, sx, sy, state)
        self._power_strip(img, d, snap, sx, sy)
        return img

    def _idle(self, d, snap: Snapshot, sx: int, sy: int) -> None:
        c, g = snap.cpu, snap.gpu
        self._chip_panel(d, _off(IDLE_TOP["cpu"], sx, sy), "CPU", BLUE,
                         c.temp_c, c.load_pct,
                         f"PEAK {self.ghz(c.clock_max_mhz)}  AVG {self.ghz(c.clock_avg_mhz)} GHz",
                         c.power_w, "PKG POWER", self._series("cpu.temp"),
                         big=58, captions=True)
        vram = (f"CORE {self.num(g.core_mhz, '{:.0f}')} MHz   "
                f"VRAM {self.num(g.vram_used_mb / 1000 if g.vram_used_mb else None, '{:.1f}')} GB")
        self._chip_panel(d, _off(IDLE_TOP["gpu"], sx, sy), "GPU", GREEN,
                         g.temp_c, g.load_pct, vram, g.power_w, "BOARD POWER",
                         self._series("gpu.temp"), big=58, captions=True)

    def _game(self, d, snap: Snapshot, sx: int, sy: int) -> None:
        c, g, f = snap.cpu, snap.gpu, snap.frames

        # ---- CPU / GPU compact (same slots as idle, tighter) ----
        self._chip_panel(d, _off(GAME_TOP["cpu"], sx, sy), "CPU", BLUE,
                         c.temp_c, c.load_pct,
                         f"{self.ghz(c.clock_max_mhz)} / {self.ghz(c.clock_avg_mhz)} GHz",
                         c.power_w, "PKG POWER", self._series("cpu.temp"),
                         big=42, captions=False, name_size=14, pow_size=22)
        vram_gb = g.vram_used_mb / 1000 if g.vram_used_mb else None
        self._chip_panel(d, _off(GAME_TOP["gpu"], sx, sy), "GPU", GREEN,
                         g.temp_c, g.load_pct, f"{self.num(g.core_mhz, '{:.0f}')} MHz",
                         g.power_w, f"VRAM {self.num(vram_gb, '{:.1f}')} GB",
                         self._series("gpu.temp"),
                         big=42, captions=False, name_size=14, pow_size=22)

        # ---- frame stats + frametime history ----
        box = _off(GAME_TOP["fps"], sx, sy)
        x0, y0, x1, y1 = box
        cx0, cx1 = x0 + INNER, x1 - INNER
        mid = (x0 + x1) // 2
        self._panel(d, box)
        self._txt(d, (cx0, y0 + 24), "FRAMES", (self.flabel, 14), DIM, "ls")
        self._big(d, mid, y0 + 92, self.num(f.fps, "{:.0f}"), 72, TAN, "ms",
                  f.fps is not None)
        self._txt(d, (mid, y0 + 114), "FPS", (self.flabel, 14), DIM, "ms")

        ms = self.history.last("frames.ms")
        self._row(d, cx0, cx1, y0 + 140, left=("FRAME TIME", DIMMER, 13),
                  right=self._value(ms, "{:.1f} ms", TAN, 20))
        target = float(self.cfg["game"].get("frametime_target_ms", 16.7))
        self._graph(d, cx0, y0 + 148, cx1, y0 + 176, self._series("frames.ms"),
                    TAN, span_min=2.0, target=target, plain=not self.trends)

        self._pair(d, cx0, y1 - 14, "1% LOW", self.num(f.low1_pct), DIMMER, TAN, 13, 24,
                   present=f.low1_pct is not None)
        self._pair(d, 0, y1 - 14, "0.1% LOW", self.num(f.low01_pct), DIMMER, TAN, 13, 24,
                   right_edge=cx1, present=f.low01_pct is not None)

    # ---------- burn-in exercise sweep --------------------------------------------
    def sweep(self, progress: float) -> Image.Image:
        """Full-screen moving rainbow gradient: cycles every color across every
        pixel over `exercise_s` seconds to exercise the whole panel."""
        import colorsys

        import numpy as np
        t = progress * 2.0  # two full sweeps
        xs = (np.arange(self.w) / self.w + t) % 1.0
        rgb = np.array([colorsys.hsv_to_rgb(x, 0.9, 0.9) for x in xs]) * 255
        row = rgb.astype(np.uint8)                       # (W,3)
        frame = np.tile(row[None, :, :], (self.h, 1, 1))
        return Image.fromarray(frame)
