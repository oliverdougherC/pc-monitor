"""Config loading with sensible defaults."""
import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "turing-smart-screen-python"

DEFAULTS = {
    "display": {
        "revision": "SIMU",
        "com_port": "AUTO",
        "portrait_width": 480,
        "portrait_height": 800,
        "orientation": "landscape",
        "brightness_idle": 45,
        "brightness_game": 70,
        "brightness_dim": 12,
        "screen_off_after_min": 45,
    },
    "sensors": {"backend": "auto", "interval_s": 1.0},
    "power": {
        "base_w": 34, "rail_overhead_pct": 9, "cpu_tdp": 170, "gpu_tdp": 575,
        "gradient_min_w": 50, "gradient_max_w": 1000, "gradient_gamma": 0.75,
    },
    "game": {
        "processes": [], "ignore": ["explorer.exe", "dwm.exe"],
        "fullscreen_heuristic": True, "min_gpu_load": 10,
        "enter_after_s": 4, "exit_after_s": 25,
        "frametime_target_ms": 16.7,   # dashed reference line in the frametime graph
    },
    "burnin": {"shift_every_min": 5, "exercise_every_h": 8, "exercise_s": 12},
    "layout": {
        "font_value": "jetbrains-mono/JetBrainsMono-ExtraBold.ttf",
        "font_label": "roboto/Roboto-Bold.ttf",
        "font_small": "roboto/Roboto-Medium.ttf",
        "trend_window_s": 60,          # seconds of history in the trend bands
        # false = serial-transport budget mode: bands become plain level bars and
        # nothing animates except the digits (see README bandwidth note)
        "trend_bands": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    user = {}
    if p.exists():
        user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, user)
    cfg["_root"] = str(ROOT)
    cfg["_vendor"] = str(VENDOR)
    cfg["_fonts"] = str(VENDOR / "res" / "fonts")
    return cfg
