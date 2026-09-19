"""Synthetic backend for UI development/preview without sensors.

Shapes the data to look like the real thing in a 60s trend window:
- loads/clocks/power ripple at a few seconds (so digits visibly tick)
- temps drift on the scale of minutes (thermal mass), with a small ripple
- disk/network are mostly quiet with periodic bursts — a smooth sine would make
  the trend bands look like static instead of activity
"""
from __future__ import annotations

import math
import time

from app.snapshot import Snapshot


def _burst(t: float, base: float, amp: float, period: float, width: float,
           seed: float = 0.0) -> float:
    """Slow wander + one gaussian burst per `period` seconds."""
    wander = base * (0.6 + 0.4 * math.sin(t * 0.11 + seed))
    ph = (t % period) - period / 2.0
    return wander + amp * math.exp(-(ph * ph) / (2.0 * width * width))


class DemoBackend:
    def __init__(self, cfg: dict):
        self._t0 = time.monotonic()
        self.game = False  # flipped by main when state=game preview
        self._last = time.monotonic()

    def sample(self, snap: Snapshot) -> None:
        t = time.monotonic() - self._t0
        w = math.sin(t * 0.35)        # seconds-scale ripple
        slow = math.sin(t * 0.055)    # minutes-scale thermal drift
        game = self.game
        c, g, f = snap.cpu, snap.gpu, snap.frames

        if game:
            c.load_pct = 58 + 12 * w
            c.temp_c = 70 + 3 * slow + 0.5 * math.sin(t * 1.7)
            c.clock_max_mhz = 5350 + 40 * w
            c.clock_avg_mhz = 5020 + 60 * w
            c.power_w = 118 + 18 * w
            g.load_pct = 97 + 2 * w
            g.temp_c = 65 + 2.5 * slow + 0.4 * math.sin(t * 1.3)
            g.core_mhz = 2520 + 25 * w
            g.power_w = 400 + 35 * math.sin(t * 0.6)
            g.vram_used_mb = 13_400 + 900 * slow
            g.vram_total_mb = 32_000
            # frame stats carry real-looking jitter and a periodic stutter, so
            # the frametime graph is judged against something honest; latency is
            # left None so the layout derives it from fps, as it will from the
            # PresentMon feed.
            jitter = 3.2 * math.sin(t * 6.1) + 2.1 * math.sin(t * 2.7) + 1.1 * math.sin(t * 13.3)
            stutter = 27.0 if (t % 11.0) < 0.8 else 0.0
            f.fps = max(24.0, 143 + 6 * w + jitter - stutter)
            f.low1_pct = 101 + 4 * w
            f.low01_pct = 58 + 6 * w
            f.latency_ms = None
            ram = 26_800 + 1_400 * slow
            snap.disk_read_bps = _burst(t, 4e6, 26e6, 9.0, 0.9, 1.0)
            snap.disk_write_bps = _burst(t, 1.6e6, 5e6, 14.0, 1.3, 2.0)
            snap.net_down_bps = _burst(t, 26e6, 80e6, 11.0, 1.4, 3.0)
            snap.net_up_bps = _burst(t, 1.2e6, 6e6, 17.0, 1.2, 4.0)
        else:
            c.load_pct = 6 + 5 * (w + 1) / 2
            c.temp_c = 41 + 3.5 * slow + 0.4 * math.sin(t * 1.1)
            c.clock_max_mhz = 5290 + 30 * w
            c.clock_avg_mhz = 2100 + 500 * w
            c.power_w = 28 + 9 * w
            g.load_pct = 2 + 3 * (w + 1) / 2
            g.temp_c = 37 + 2.5 * slow + 0.3 * math.sin(t * 0.9)
            g.core_mhz = 330 + 30 * w
            g.power_w = 24 + 5 * w
            g.vram_used_mb = 3_800 + 500 * slow
            g.vram_total_mb = 32_000
            ram = 18_400 + 1_100 * slow
            snap.disk_read_bps = _burst(t, 0.35e6, 2.2e6, 17.0, 1.1, 1.0)
            snap.disk_write_bps = _burst(t, 0.7e6, 1.4e6, 23.0, 1.6, 2.0)
            snap.net_down_bps = _burst(t, 1.8e6, 7e6, 13.0, 1.8, 3.0)
            snap.net_up_bps = _burst(t, 0.16e6, 1.1e6, 19.0, 1.4, 4.0)

        snap.ram_used_mb = ram
        snap.ram_total_mb = 64_000
