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
        # Reboot the panel on every start, the way the vendored examples do. Off by
        # default: a screen that answers HELLO gets a full repaint a second later
        # anyway, and that reboot's blocking write is what hangs when the panel's
        # USB endpoint has stopped draining (see app/display.py).
        "reset_on_start": False,
        # After two minutes of the panel not answering anything, ask Windows to
        # restart its USB device (pnputil /restart-device — the Restart-PnpDevice
        # cmdlet is gone from this build). Reopening the COM port cannot unstick an
        # endpoint that stopped draining — measured: the port opens, the first write
        # blocks, and the vendor's own RESTART command never lands. Needs
        # Administrator (the scheduled task has it); otherwise it logs the refusal
        # and keeps retrying the port.
        "usb_restart_on_fail": True,
        # One [beat] line per this many seconds: every other log line is conditional,
        # and a loop that died in the night otherwise looks identical to a quiet one.
        # 0 disables it. (First beat always ~60 s after start, to prove a start lived.)
        "heartbeat_s": 900,
        "brightness_idle": 45,
        "brightness_game": 70,
        "brightness_dim": 12,
        # no input for this long → backlight off (and the loop stops pushing frames)
        "screen_off_after_min": 45,
        # … and a shorter step before that: the deep dim, which is the setting that
        # used to be a hard-coded 300 s in app/burnin.py
        "dim_after_s": 300,
        # A controller does not reset GetLastInputInfo, so the two timers above would
        # fire in the middle of a game. Live frames from the locked target hold them;
        # set false to let the timers win even during play.
        "stay_lit_in_game": True,
    },
    "sensors": {"backend": "auto", "interval_s": 1.0},
    "power": {
        "base_w": 34, "rail_overhead_pct": 9, "cpu_tdp": 170, "gpu_tdp": 575,
        "gradient_min_w": 50, "gradient_max_w": 1000, "gradient_gamma": 0.75,
        # A whole-system number is only as whole as its inputs: with a CPU or
        # GPU reading completely missing, the strip shows "-- W partial" (or
        # unavailable) instead of a total that silently excludes it. Set false
        # to accept a known-components-only figure as a floor - the panel will
        # still label it "partial".
        "require_complete": True,
        # Older than this and the snapshot behind the number is labelled
        # "stale": a sensor tick that failed leaves the loop reusing the last
        # snapshot, and yesterday's watts must not pose as this second's.
        "max_age_s": 8.0,
        # Follow the machine, not just the idle timer: dark while it is asleep, while
        # the session is locked, and (by default) whenever Windows has turned the
        # displays off. Turn these off if you want the panel to stay lit through
        # either — the reasons are logged, so `[light] panel off (asleep)` tells you
        # which one you are fighting.
        "follow_sleep": True,
        "follow_lock": True,
        "follow_display": True,
        # At start-up the monitor state is unknown (Windows only reports *changes*), so
        # derive it once from the idle clock against the power scheme's own display
        # timeout. Turn this off to wait for Windows to say something instead — the
        # panel then stays lit after a reboot into a dark room until the idle timer.
        "seed_monitor": True,
        # A tick gap larger than this means we were frozen (asleep, hibernating, or
        # starved) and everything must be re-made: the panel link, the ETW capture,
        # the game lock. 5 s is well past any honest 1 Hz tick.
        "wake_gap_s": 5.0,
    },
    "night": {
        # "auto" follows Windows Night light (and any third-party warmer that writes
        # the gamma ramp); "on"/"off" override it; the schedule is only a fallback for
        # a build whose state store cannot be read. See app/nightlight.py.
        "mode": "auto",
        "refresh_s": 3.0,
        # 1.0 = the full colour temperature the user set. Below 1 blends it toward
        # neutral, which is how you stop 2500 K reading as "the panel is broken".
        "strength": 1.0,
        "color_temp_k": 0,            # 0 = use whatever Windows has set
        "schedule": "",               # e.g. "21:00-07:00"; fallback only, see mode
        # Night mode also dims: min(level, level × scale), never below the floor, so
        # the panel cannot be the brightest thing in a dark room.
        "brightness_scale": 0.55,
        "brightness_floor": 8,
        # Second opinion from the actual gamma ramp (f.lux, LightBulb, Twilight write
        # it; Windows Night light does not). Cheap, and it means "my night mode" is
        # followed whatever implements it.
        "check_gamma_ramp": True,
        "ramp_warm_margin": 0.12,     # R minus B at 50% tone before we call it warm
    },
    "game": {
        "processes": [], "ignore": ["explorer.exe", "dwm.exe"],
        "fullscreen_heuristic": True, "min_gpu_load": 10,
        "enter_after_s": 4, "exit_after_s": 25,
        "frametime_target_ms": 16.7,   # dashed reference line in the frametime graph
        "present_detection": True,     # foreground-PID-is-presenting detection
                                       # (needs frames.source working; else legacy heuristic)
        "detection": {
            # A foreground process presenting on its whole monitor needs only long
            # enough to prove it is not a focus flicker. The flat 4 s applied to every
            # kind of evidence is what made switching feel slow.
            "enter_strong_s": 1.2,
            "dead_exit_s": 3.0,        # target process gone → leave game mode
            "switch_silence_s": 3.0,   # target quiet + another strong candidate
            "min_gpu_score": 55.0,     # % of the frame the GPU was busy for it
            # "non_game" replaces the built-in browser/chat/shell list if set.
            "non_game": None,
        },
    },
    "frames": {
        "source": "auto",              # auto | off (off = no ETW child, panel shows --)
        "path": "vendor/presentmon/presentmon.exe",
        "min_present_fps": 24,         # presenting >= this ⇒ "is rendering a game"
        "window_s": 60,                # frametime ring length = lows/percentile window
        "exclude_dropped": True,       # count only frames that reached the screen
        "role": "",                    # ETW session role ("" = "main"; A/B runs)
        "extra_args": [],              # extra presentmon args (diagnostics / A-B)
        "output_file": "",             # diagnostic: raw CSV to file, frame stats off
        # How long a game that stopped presenting (alt-tab, loading screen) keeps its
        # last measurement on the panel, dimmed and marked held. Past this: `--`.
        "hold_s": 12,
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
        "transition": "wipe",           # "wipe" | "none" on idle↔game reflow
        "transition_hold_s": 0.12,      # dark-frame hold before the new layout
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
