"""Power estimation + green→red gradient coloring.

There is no true "system watt" without an inline meter, so we estimate:
  total = base (mobo/ram/fans/ssd + psu losses) + cpu + gpu
Where package-power sensors exist they are used directly; otherwise a
load-curved TDP model gives an educated guess (aim: +/- 10-20 W on desktops
with modern APUs which report well).
"""
from __future__ import annotations

import math

from app.snapshot import Snapshot


def _model(load01: float, tdp: float, idle_w: float) -> float:
    # idle floor + superlinear rise toward TDP
    return idle_w + (tdp - idle_w) * (max(0.0, min(1.0, load01)) ** 1.25)


def estimate(snap: Snapshot, cfg: dict) -> tuple[float, dict]:
    """Returns (total_w, breakdown dict for debugging)."""
    p = cfg["power"]
    base = float(p["base_w"])  # non-CPU/GPU rails: board, RAM, SSD, fans, pump, misc

    if snap.cpu.power_w is not None:
        cpu_w = float(snap.cpu.power_w)
    elif snap.cpu.load_pct is not None:
        cpu_w = _model(snap.cpu.load_pct / 100.0, float(p["cpu_tdp"]), 18.0)
    else:
        cpu_w = 0.0

    if snap.gpu.power_w is not None:
        gpu_w = float(snap.gpu.power_w)
    elif snap.gpu.load_pct is not None:
        # GPUs idle ~20-35 W even at 0% reported load
        gpu_w = _model(snap.gpu.load_pct / 100.0, float(p["gpu_tdp"]), 28.0)
    else:
        gpu_w = 0.0

    # CPU/GPU sensor values are measured at the socket / card connector;
    # VRM conversion (~5%) and PSU losses (~4%) happen upstream of them.
    rail_overhead = 1.0 + float(p.get("rail_overhead_pct", 0)) / 100.0
    total = base + (cpu_w + gpu_w) * rail_overhead
    return total, {"base": base, "cpu": cpu_w, "gpu": gpu_w, "overhead_pct": rail_overhead - 1.0}


def power_color(watts: float, cfg: dict) -> tuple[int, int, int]:
    """Green at gradient_min_w → red at gradient_max_w (HSV hue sweep)."""
    p = cfg["power"]
    lo, hi = float(p["gradient_min_w"]), float(p["gradient_max_w"])
    t = max(0.0, min(1.0, (watts - lo) / (hi - lo)))
    t = t ** float(p.get("gradient_gamma", 0.75))
    hue = (1.0 - t) * (130.0 / 360.0)  # 130deg green → 0deg red
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
    return (round(r * 255), round(g * 255), round(b * 255))


def power_watts_for_display(snap: Snapshot, cfg: dict) -> float | None:
    if snap.power_total_w is not None:
        return snap.power_total_w
    if snap.cpu.load_pct is None and snap.cpu.power_w is None and snap.gpu.power_w is None and snap.gpu.load_pct is None:
        return None
    total, _ = estimate(snap, cfg)
    return total
