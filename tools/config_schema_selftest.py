"""The config schema: bad values die at load, good values reach their consumer.

    .venv\\Scripts\\python tools\\config_schema_selftest.py

Issue #20 was three complaints in one costume. Documented knobs the consumer
never read (`game.detection` was looked up at the config root, where nothing
lives; `game.fullscreen_heuristic` was grepped for nowhere at all), values that
are fine to YAML but fatal to run (a zero `shift_every_min` or `exercise_s`
divides by zero inside the loop, minutes into the night; equal gradient
endpoints break the color ramp the same way), and no answer at all when a live
reload arrives with one of those in it.

So every case here drives the REAL consumers - GameWatch, BurnIn, Layout,
`output.wipe_supported`, `power.power_color` - through the REAL merged config
loaded from YAML text. A schema tested against its own mirror proves nothing;
these prove the number survives the merge, passes the schema, and changes what
the component does. The rejection cases then prove the opposite direction: the
kind of value that used to reach the loop now stops at `load()` naming its key,
before any thread, port or ETW child exists to be poisoned by it - `load()` is
main.py's first act, and this test process starts nothing in order to catch it.

Run against the pre-fix source and the nested-detection, heuristic, rejection
and last-known-good cases fail; the consumer cases pass (they document the
acceptance contract, not just the fix).
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import time
from types import SimpleNamespace

import yaml

sys.path.insert(0, ".")          # our tree first: vendor has its own main.py
from app import burnin as burnin_mod       # noqa: E402
from app import config as cfgmod           # noqa: E402
from app import gamewatch, layout, output, power       # noqa: E402
from app.burnin import BurnIn              # noqa: E402
from app.gamewatch import GameWatch        # noqa: E402

sys.stdout.reconfigure(errors="replace")

BASE = cfgmod.load(None)         # the shipped YAML, merged and (now) validated
ALIVE = os.getppid()             # a pid that exists and is not us
fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = (abs(got - want) <= max(0.02 * abs(want), 0.02)) if isinstance(got, float) \
        else got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}"
          + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


# ---- config builders -------------------------------------------------------
def gcfg(detection: dict | None = None, **game) -> dict:
    """Shipped config with `game:` overrides - through the real dict shape."""
    c = copy.deepcopy(BASE)
    for k, v in game.items():
        c["game"][k] = v
    for k, v in (detection or {}).items():
        c["game"]["detection"][k] = v
    return c


def write_cfg(overrides: dict) -> str:
    """A real YAML file on disk (not a dict) - the schema guards the file path."""
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="cfg20_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(overrides, f)
    return path


# ---- fake world for GameWatch ----------------------------------------------
# _foreground_info is the only call in the detector that reaches outside the
# process, so it is the only thing replaced (same seam gamewatch_selftest uses).
FG = {"pid": None, "name": None, "covers": False, "borderless": False}
gamewatch._foreground_info = lambda: (FG["pid"], FG["name"], FG["covers"],
                                      FG["borderless"])


def fg(pid, name, covers: bool = True, borderless: bool = True) -> None:
    FG.update(pid=pid, name=name, covers=covers, borderless=borderless)


class Pr:
    """A presenter from the present stream, as _score consumes it."""

    def __init__(self, name: str, fps: float = 60.0, gpu: float | None = None,
                 exclusive: bool = False) -> None:
        self.name, self.fps, self.gpu, self.exclusive = name, fps, gpu, exclusive


def snap(gpu_load: float) -> SimpleNamespace:
    return SimpleNamespace(gpu=SimpleNamespace(load_pct=gpu_load))


# ---- cases -----------------------------------------------------------------
def case_shipped_loads() -> None:
    print("case: the shipped config is valid under the schema")
    problems = cfgmod.validate(copy.deepcopy(BASE))
    check("shipped config.yaml problems", problems, [])
    check("load() still returns the merged config", BASE["display"]["revision"], "C")


def case_detection_reaches_detector() -> None:
    print("case: game.detection values reach GameWatch (was read at the root)")
    c = gcfg(enter_after_s=2.0, detection={
        "enter_strong_s": 4.5, "dead_exit_s": 7.5, "switch_silence_s": 6.25,
        "min_gpu_score": 88.0, "non_game": ["onlynotgame.exe"]})
    w = GameWatch(c)
    check("enter_strong_s", w.enter_strong_s, 4.5)
    check("dead_exit_s", w.dead_exit_s, 7.5)
    check("switch_silence_s", w.switch_silence_s, 6.25)
    check("min_gpu_score -> gpu_strong", w.gpu_strong, 88.0)
    check("non_game override replaces the built-in list (chrome no longer excluded)",
          w._is_non_game("chrome.exe"), False)
    check("non_game override adds its own name", w._is_non_game("onlynotgame.exe"), True)


def case_enter_time_changes_behaviour() -> None:
    print("case: game.detection.enter_strong_s changes when the lock happens")
    fg(ALIVE, "re9.exe", covers=True, borderless=True)
    pres = {ALIVE: Pr("re9.exe", 60.0, 30.0)}        # STRONG: foreground + covering

    fast = GameWatch(gcfg(detection={"enter_strong_s": 0.5}))
    fast.tick(snap(30.0), 0.5, pres, None)
    check("0.5 s: entered on the first tick", fast.state, "game")

    slow = GameWatch(gcfg(detection={"enter_strong_s": 4.0}))
    for _ in range(4):
        slow.tick(snap(30.0), 0.5, pres, None)       # 2.0 s of the same evidence
    check("4.0 s: still not in game mode after 2.0 s", slow.state, "idle")


def case_gpu_score_changes_behaviour() -> None:
    print("case: game.detection.min_gpu_score decides who is a candidate")
    fg(None, None, covers=False, borderless=False)   # a background presenter
    pres = {ALIVE: Pr("bggame.exe", 120.0, 70.0)}

    lo = GameWatch(gcfg(enter_after_s=1.0, detection={"min_gpu_score": 55.0}))
    lo.tick(snap(70.0), 1.0, pres, None)
    check("threshold 55: a 70 % GPU presenter enters", lo.state, "game")

    hi = GameWatch(gcfg(enter_after_s=1.0, detection={"min_gpu_score": 88.0}))
    for _ in range(5):
        hi.tick(snap(70.0), 1.0, pres, None)
    check("threshold 88: the same presenter never enters", hi.state, "idle")


def case_non_game_override_changes_behaviour() -> None:
    print("case: game.detection.non_game controls what a presenter may be")
    # chrome, not explorer: `game.ignore` (a separate knob) names explorer.exe,
    # and an ignored name is vetoed before the non-game list is ever consulted.
    fg(ALIVE, "chrome.exe", covers=True, borderless=True)
    pres = {ALIVE: Pr("chrome.exe", 60.0, 30.0)}

    default = GameWatch(gcfg(enter_after_s=1.0, detection={"enter_strong_s": 0.5}))
    for _ in range(5):
        default.tick(snap(30.0), 1.0, pres, None)
    check("built-in list keeps presenting chrome out of game mode",
          default.state, "idle")

    replaced = GameWatch(gcfg(enter_after_s=1.0, detection={
        "enter_strong_s": 0.5, "non_game": ["onlynotgame.exe"]}))
    replaced.tick(snap(30.0), 1.0, pres, None)
    check("a replaced list lets it score like any foreground game",
          replaced.state, "game")


def case_heuristic_off() -> None:
    print("case: game.fullscreen_heuristic: false is honoured (was never read)")
    fg(ALIVE, "gameish.exe", covers=True, borderless=True)   # perfect window shape
    off = GameWatch(gcfg(fullscreen_heuristic=False, enter_after_s=1.0))
    for _ in range(6):
        off.tick(snap(42.0), 1.0, None, None)                # no present stream
    check("off: a covering borderless window never enters game mode", off.state, "idle")

    on = GameWatch(gcfg(fullscreen_heuristic=True, enter_after_s=1.0,
                        exit_after_s=2.0))
    on.tick(snap(42.0), 1.0, None, None)
    check("on (shipped): the same window enters - the switch gates, not disables",
          on.state, "game")

    # A lock held on the window heuristic must also let go once the switch is off
    # (what a live reload of that key means mid-game).
    on.fullscreen_heuristic = False
    for _ in range(3):
        on.tick(snap(42.0), 1.0, None, None)
    check("off mid-hold: the lock releases instead of riding the heuristic",
          on.state, "idle")
    check("and says why", "heuristic" in on.evidence.lower(), True)


def case_burnin_consumers() -> None:
    print("case: burnin timers drive BurnIn (the divide-by-zero pair)")

    class Clock:
        t = 0.0

        def time(self) -> float:
            return self.t

        def monotonic(self) -> float:
            return self.t

    clock = Clock()
    real = burnin_mod.time
    burnin_mod.time = clock
    try:
        clock.t = 5460.0                                  # minute 91: 7 and 13 differ
        b7 = BurnIn({"burnin": {"shift_every_min": 7, "exercise_every_h": 8,
                                "exercise_s": 12}})
        b13 = BurnIn({"burnin": {"shift_every_min": 13, "exercise_every_h": 8,
                                 "exercise_s": 12}})
        check("shift_every_min 7 at minute 91", b7.shift(), (3, 0))
        check("shift_every_min 13 at minute 91", b13.shift(), (0, 3))

        for dur, want in ((4.0, 0.5), (8.0, 0.25)):
            b = BurnIn({"burnin": {"shift_every_min": 5, "exercise_every_h": 1,
                                   "exercise_s": dur}})
            b._last_exercise_end = 1000.0
            check(f"exercise_every_h 1: not due at +3000 s",
                  b.exercise_due(4000.0), False)
            check(f"exercise_every_h 1: due at +4000 s", b.exercise_due(5000.0), False)
            check(f"exercise_s {dur}: sweep started", b._exercise_start is not None, True)
            check(f"exercise_s {dur}: progress at +2 s", b.exercise_progress(5002.0), want)
    finally:
        burnin_mod.time = real


def case_render_consumers() -> None:
    print("case: layout/power knobs reach the render decisions")
    c = copy.deepcopy(BASE)
    check("transition wipe on USB is supported",
          output.wipe_supported("TUR_USB", c), True)
    c["layout"]["transition"] = "none"
    check("transition none turns the wipe off",
          output.wipe_supported("TUR_USB", c), False)
    c["layout"]["transition"] = "wipe"
    check("serial revisions never wipe anyway",
          output.wipe_supported("C", c), False)

    p = copy.deepcopy(BASE)
    p["power"].update({"gradient_min_w": 100, "gradient_max_w": 1000})
    lo = power.power_color(100.0, p)
    hi = power.power_color(1000.0, p)
    check("gradient floor is the green end", lo[1] > lo[0], True)
    check("gradient ceiling is the red end", hi[0] > hi[1], True)

    t = copy.deepcopy(BASE)
    t["layout"]["trend_window_s"] = 31
    check("trend_window_s sizes the history", layout.Layout(t, rate_hz=1.0).samples, 31)
    t["layout"]["trend_bands"] = False
    check("trend_bands: false reaches the layout", layout.Layout(t, rate_hz=1.0).trends,
          False)


def case_rejects_bad_values() -> None:
    print("case: values that used to reach the loop now stop at load(), naming keys")
    bad = getattr(cfgmod, "ConfigError", None)
    if bad is None:
        check("config.ConfigError exists", "missing", "present")
        return
    nan, inf = float("nan"), float("inf")
    # (override, the key the error must name) - every one of these merged
    # silently and broke something downstream before the schema existed.
    table = [
        ({"burnin": {"shift_every_min": 0}}, "burnin.shift_every_min"),
        ({"burnin": {"shift_every_min": -5}}, "burnin.shift_every_min"),
        ({"burnin": {"shift_every_min": nan}}, "burnin.shift_every_min"),
        ({"burnin": {"exercise_s": 0}}, "burnin.exercise_s"),
        ({"burnin": {"exercise_every_h": 0}}, "burnin.exercise_every_h"),
        ({"sensors": {"interval_s": 0}}, "sensors.interval_s"),
        ({"display": {"heartbeat_s": -1}}, "display.heartbeat_s"),
        ({"display": {"brightness_game": 140}}, "display.brightness_game"),
        ({"display": {"brightness_dim": -3}}, "display.brightness_dim"),
        ({"display": {"brightness_idle": "high"}}, "display.brightness_idle"),
        ({"display": {"reset_on_start": "true"}}, "display.reset_on_start"),
        ({"display": {"orientation": "diagonal"}}, "display.orientation"),
        ({"display": {"revision": "Z"}}, "display.revision"),
        ({"display": {"portrait_width": 480, "portrait_height": 480}}, "800x480"),
        ({"power": {"gradient_max_w": 50}}, "power.gradient_max_w"),
        ({"power": {"gradient_min_w": 900, "gradient_max_w": 100}}, "power.gradient_max_w"),
        ({"power": {"base_w": inf}}, "power.base_w"),
        ({"night": {"mode": "sometimes"}}, "night.mode"),
        ({"night": {"strength": 1.5}}, "night.strength"),
        ({"game": "yes"}, "game:"),
        ({"game": {"detection": "yes"}}, "game.detection"),
        ({"game": {"processes": "game.exe"}}, "game.processes"),
        ({"game": {"enter_after_s": 0}}, "game.enter_after_s"),
        ({"frames": {"source": "sometimes"}}, "frames.source"),
        ({"frames": {"hold_s": -2}}, "frames.hold_s"),
        ({"layout": {"transition": "slide"}}, "layout.transition"),
        ({"layout": {"trend_window_s": 0}}, "layout.trend_window_s"),
    ]
    for overrides, key in table:
        path = write_cfg(overrides)
        try:
            try:
                cfgmod.load(path)
                check(f"reject {key}", "load() accepted it", f"ConfigError naming {key}")
            except bad as e:
                named = any(key in p for p in e.problems)
                check(f"rejected, naming {key}", named, True)
        finally:
            os.unlink(path)


def case_last_known_good() -> None:
    print("case: a bad live reload keeps the last-known-good config running")
    keep = getattr(cfgmod, "load_or_keep", None)
    if keep is None:
        check("config.load_or_keep exists", "missing", "present")
        return
    path = write_cfg({"display": {"brightness_idle": 44}})
    try:
        cfg, problems = keep(path)
        check("good reload applies", cfg["display"]["brightness_idle"], 44)
        check("good reload reports nothing", problems, [])

        with open(path, "w", encoding="utf-8") as f:      # mid-edit mistake
            yaml.safe_dump({"display": {"brightness_idle": 44},
                            "burnin": {"shift_every_min": 0}}, f)
        cfg2, problems2 = keep(path)
        check("bad reload keeps the running brightness",
              cfg2["display"]["brightness_idle"], 44)
        check("bad reload hands the problems back for the log",
              any("burnin.shift_every_min" in p for p in problems2), True)
        raised = False
        try:
            cfgmod.load(path)                             # start-up stays strict
        except cfgmod.ConfigError:
            raised = True
        check("load() itself still refuses to start on it", raised, True)

        with open(path, "w", encoding="utf-8") as f:      # the edit completed
            yaml.safe_dump({"display": {"brightness_idle": 46}}, f)
        cfg3, problems3 = keep(path)
        check("fixed file applies", cfg3["display"]["brightness_idle"], 46)
        check("fixed file reports nothing", problems3, [])
    finally:
        os.unlink(path)


def main() -> int:
    for fn in (case_shipped_loads, case_detection_reaches_detector,
               case_enter_time_changes_behaviour, case_gpu_score_changes_behaviour,
               case_non_game_override_changes_behaviour, case_heuristic_off,
               case_burnin_consumers, case_render_consumers,
               case_rejects_bad_values, case_last_known_good):
        try:
            fn()
        except Exception as e:               # pre-fix source: a missing API is a FAIL
            print(f"  FAIL {fn.__name__} raised {type(e).__name__}: {e}")
            fails.append(fn.__name__)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())