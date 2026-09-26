"""Config loading: sensible defaults, one explicit schema, and a last-known-good.

Config used to be merge-only: any YAML deep-merged happily, so `shift_every_min: 0`
reached the render loop and divided by zero minutes into the night, `exercise_s: 0`
did the same inside a sweep, and equal gradient endpoints broke the color ramp.
The schema below is the one place that says what every documented key means -
type, range, enum - so a bad value stops the app at load with the key named,
before any thread, port or ETW child exists to be poisoned by it.

Two entry points, because the two moments are different. `load()` is start-up:
it raises rather than starting a broken run. `load_or_keep()` is live reload:
an edited file that does not validate leaves the last-known-good config running
and hands the problems back for the log - a typo mid-edit should not stop the
panel or put it into a permanent error loop.

Keys the schema does not mention pass through untouched on purpose: sibling
work lands keys before this file catches up, and a validator that rejects the
future conflicts with every PR that adds a setting. What it does reject is a
documented key holding an undocumentable value.
"""
import copy
import math
import re
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


class ConfigError(ValueError):
    """The config says something the app cannot act on.

    `problems` is one line per offending key - the whole list, not just the
    first - because someone editing config.yaml over SSH should see every
    mistake in one go, not reload-fail-reload once per typo.
    """

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems) or "invalid configuration")
        self.problems = list(problems)


# The schema. One spec per documented key, mirroring DEFAULTS' shape; a nested
# dict means a section and recurses. Specs are tuples (kind, *args):
#   ("bool",)                 true/false only (a YAML "yes" string is not a bool)
#   ("str",)                  a non-empty string
#   ("str_or_none",)          a string (possibly "") or null
#   ("strlist",)              a list of strings (possibly empty)
#   ("strlist_or_none",)      a list of strings or null
#   ("enum", {..})            one of these, matched case-insensitively
#   ("int", lo, hi)           a whole number in [lo, hi] (None = open end)
#   ("num", lo, hi)           a finite number in [lo, hi] (None = open end)
#   ("pos",)                  a finite number strictly > 0 (every interval that
#                             something divides by or sleeps for; 0 is the value
#                             that once divided by zero inside the render loop)
#   ("nonneg",)               a finite number >= 0 (0 means "off": heartbeat,
#                             screen-off; still rejects negative and non-finite)
# A key absent from SCHEMA is not validated (see the module docstring).
SCHEMA = {
    "display": {
        "revision": ("enum", {"simu", "tur_usb", "a", "b", "c", "d", "weact_a", "weact_b"}),
        "com_port": ("str",),
        "portrait_width": ("int", 1, 8192),
        "portrait_height": ("int", 1, 8192),
        "orientation": ("enum", {"landscape", "portrait"}),
        "reset_on_start": ("bool",),
        "usb_restart_on_fail": ("bool",),
        "heartbeat_s": ("nonneg",),
        "brightness_idle": ("int", 0, 100),
        "brightness_game": ("int", 0, 100),
        "brightness_dim": ("int", 0, 100),
        "screen_off_after_min": ("nonneg",),
        "dim_after_s": ("nonneg",),
        "stay_lit_in_game": ("bool",),
    },
    "sensors": {
        "backend": ("enum", {"auto", "lhm", "fallback", "demo"}),
        "interval_s": ("pos",),
    },
    "power": {
        "base_w": ("nonneg",),
        "rail_overhead_pct": ("num", 0, 100),
        "cpu_tdp": ("pos",),
        "gpu_tdp": ("pos",),
        "gradient_min_w": ("num", None, None),
        "gradient_max_w": ("num", None, None),
        "gradient_gamma": ("pos",),
        "follow_sleep": ("bool",),
        "follow_lock": ("bool",),
        "follow_display": ("bool",),
        "seed_monitor": ("bool",),
        "wake_gap_s": ("pos",),
    },
    "night": {
        "mode": ("enum", {"auto", "on", "off"}),
        "refresh_s": ("pos",),
        "strength": ("num", 0, 1),
        "color_temp_k": ("nonneg",),
        "schedule": ("str_or_none",),
        "brightness_scale": ("num", 0, 1),
        "brightness_floor": ("int", 0, 100),
        "check_gamma_ramp": ("bool",),
        "ramp_warm_margin": ("num", 0, 1),
    },
    "game": {
        "processes": ("strlist",),
        "ignore": ("strlist",),
        "fullscreen_heuristic": ("bool",),
        "min_gpu_load": ("num", 0, 100),
        "enter_after_s": ("pos",),
        "exit_after_s": ("pos",),
        "frametime_target_ms": ("pos",),
        "present_detection": ("bool",),
        "detection": {
            "enter_strong_s": ("pos",),
            "dead_exit_s": ("pos",),
            "switch_silence_s": ("pos",),
            "min_gpu_score": ("num", 0, 100),
            "non_game": ("strlist_or_none",),
        },
    },
    "frames": {
        "source": ("enum", {"auto", "off"}),
        "path": ("str",),
        "min_present_fps": ("pos",),
        "window_s": ("pos",),
        "exclude_dropped": ("bool",),
        "role": ("str_or_none",),
        "extra_args": ("strlist",),
        "output_file": ("str_or_none",),
        "hold_s": ("pos",),
    },
    "burnin": {
        "shift_every_min": ("pos",),
        "exercise_every_h": ("pos",),
        "exercise_s": ("pos",),
    },
    "layout": {
        "font_value": ("str",),
        "font_label": ("str",),
        "font_small": ("str",),
        "trend_window_s": ("pos",),
        "trend_bands": ("bool",),
        "transition": ("enum", {"wipe", "none"}),
        "transition_hold_s": ("nonneg",),
    },
}

# The frame the fixed 800x480 layout draws is what gets pushed to the panel, so
# that is the only geometry the app can render. Kept as a literal rather than
# imported from app/layout.py (which pulls in PIL) so config stays import-light;
# the comment names the owner so the two cannot drift unnoticed.
RENDERABLE_WH = (800, 480)   # app/layout.py: W, H


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_leaf(path: str, spec: tuple, v) -> str | None:
    kind = spec[0]
    if kind == "bool":
        return None if isinstance(v, bool) else f"{path}: must be true or false, got {v!r}"
    if kind == "str":
        return None if isinstance(v, str) and v else f"{path}: must be a non-empty string, got {v!r}"
    if kind == "str_or_none":
        return None if v is None or isinstance(v, str) else f"{path}: must be a string, got {v!r}"
    if kind == "strlist":
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return None
        return f"{path}: must be a list of strings, got {v!r}"
    if kind == "strlist_or_none":
        if v is None or (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            return None
        return f"{path}: must be a list of strings (or empty), got {v!r}"
    if kind == "enum":
        allowed = spec[1]
        if isinstance(v, str) and v.lower() in allowed:
            return None
        return f"{path}: must be one of {{{', '.join(sorted(allowed))}}}, got {v!r}"
    if kind == "int":
        if not _is_num(v) or int(v) != v:
            return f"{path}: must be a whole number, got {v!r}"
        lo, hi = spec[1], spec[2]
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return f"{path}: must be a whole number in [{lo}, {hi}], got {v!r}"
        return None
    if kind == "num":
        if not _is_num(v) or not math.isfinite(v):
            return f"{path}: must be a finite number, got {v!r}"
        lo, hi = spec[1], spec[2]
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return f"{path}: must be between {lo} and {hi}, got {v!r}"
        return None
    if kind == "pos":
        if not _is_num(v) or not math.isfinite(v) or v <= 0:
            return f"{path}: must be a positive number (zero and negative are not allowed), got {v!r}"
        return None
    if kind == "nonneg":
        if not _is_num(v) or not math.isfinite(v) or v < 0:
            return f"{path}: must be a number 0 or greater, got {v!r}"
        return None
    raise AssertionError(f"unhandled schema kind {kind!r}")   # a schema bug, not user error


def _walk(schema: dict, node, path: str, problems: list[str]) -> None:
    if not isinstance(node, dict):
        problems.append(f"{path or 'config'}: must be a mapping of options, got {type(node).__name__}")
        return
    for key, spec in schema.items():
        sub = f"{path}.{key}" if path else key
        if key not in node:
            continue    # DEFAULTS filled every documented key before we got here
        v = node[key]
        if isinstance(spec, dict):
            _walk(spec, v, sub, problems)
        else:
            err = _check_leaf(sub, spec, v)
            if err:
                problems.append(err)


def _cross_field(cfg: dict, problems: list[str]) -> None:
    """Rules that are about two keys together, checked only once each is a
    usable number on its own (so a bad type does not also trigger these)."""
    p = cfg.get("power", {})
    lo, hi = p.get("gradient_min_w"), p.get("gradient_max_w")
    if _is_num(lo) and math.isfinite(lo) and _is_num(hi) and math.isfinite(hi) and hi <= lo:
        problems.append(
            f"power.gradient_max_w: must be greater than gradient_min_w ({lo}), got {hi!r} "
            f"- the green->red ramp divides by their difference")

    d = cfg.get("display", {})
    pw, ph, ori = d.get("portrait_width"), d.get("portrait_height"), d.get("orientation")
    if (_is_num(pw) and _is_num(ph) and isinstance(ori, str)
            and ori.lower() in ("landscape", "portrait")):
        # Resolve exactly as PanelLink.get_width/get_height does, then compare
        # to the single frame the hand-placed layout can draw.
        land = ori.lower() == "landscape"
        w, h = (ph, pw) if land else (pw, ph)
        if (int(w), int(h)) != RENDERABLE_WH:
            problems.append(
                f"display: a {ori} panel resolves to {int(w)}x{int(h)}, but the fixed layout "
                f"only renders {RENDERABLE_WH[0]}x{RENDERABLE_WH[1]} (portrait_width/"
                f"portrait_height/orientation)")


def validate(cfg: dict) -> list[str]:
    """Every problem with this config, [] when it is good. Leaves the caller
    deciding whether that means raise (start-up) or keep-last-good (reload)."""
    problems: list[str] = []
    _walk(SCHEMA, cfg, "", problems)
    _cross_field(cfg, problems)
    return problems


_last_good: dict | None = None


def load(path: str | None = None) -> dict:
    """Read, merge over defaults, and validate. Raises ConfigError naming every
    bad key before anything can start on a config that cannot run."""
    global _last_good
    p = Path(path) if path else ROOT / "config.yaml"
    user = {}
    if p.exists():
        user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, user)
    problems = validate(cfg)
    if problems:
        raise ConfigError(problems)
    cfg["_root"] = str(ROOT)
    cfg["_vendor"] = str(VENDOR)
    cfg["_fonts"] = str(VENDOR / "res" / "fonts")
    _last_good = copy.deepcopy(cfg)
    return cfg


def load_or_keep(path: str | None = None) -> tuple[dict, list[str]]:
    """Live reload entry point: the new config if it validates, otherwise the
    last-known-good one still worth running, with the problems to log.

    A reload is a human editing a file while the app runs; a mistake there must
    not stop the panel or spin the loop on a value that divides by zero. We keep
    what was working and say exactly why the edit was refused. Raises
    ConfigError only if there is no last-known-good to fall back to (nothing has
    loaded successfully yet), which is a start-up path, not a reload.
    """
    try:
        return load(path), []
    except ConfigError as e:
        if _last_good is None:
            raise
        return copy.deepcopy(_last_good), list(e.problems)
