"""Prove the burn-in sweep is one frame per tick, and outranked by everything else.

    .venv\\Scripts\\python tools\\sweep_selftest.py

The exercise sweep used to run to completion inside a single tick: a nested `while`
with its own `time.sleep(0.12)`, ~100 pushes, twelve shipped seconds during which
the control loop read no host state, made no light decision and never asked the game
detector. A monitor going off, a lock, a suspend query, a wake, or a game starting
mid-animation was therefore ignored until the animation ended — and every push could
additionally wait on the transport deadline, so twelve seconds was a floor.

Half of this file is behavioural: it drives the real `main.sweep_step` with fakes for
the layout and the pusher and a real `BurnIn` on a virtual clock, injecting the state
changes the issue asks for *at several points during a sweep*, and asserting that no
bright frame leaves after the panel should be dark, that a game start abandons the
exercise instead of pausing it half-rainbow'd, that a dead link is never pushed into,
and that exactly one frame is produced per call.

The other half is structural, because behaviour alone cannot show what the loop does
with the answer: an AST pass proves the sweep is a single call inside the tick with no
nested loop and no `time.sleep` of its own, and that the frame it pushed is the only
`pusher.push` the tick performs. That is the difference between "the sweep is
interruptible" and "the sweep is subordinate": the second means the tick's freshest
decision is what reaches the panel, at the same transport budget as normal operation.

Nothing here touches the vendored library or the theme fonts, so this case gates on a
bare clone too — the shape of the loop is the part that must not regress.
"""
import ast
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

import fault_selftest                          # noqa: E402 - reuse the guard walker
import main                                    # noqa: E402
from app import config as cfgmod               # noqa: E402
from app.burnin import SHIFTS, BurnIn          # noqa: E402
from app.lights import LightPlan               # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


LIT = LightPlan(dark=False, reason="lit")


def dark(reason: str) -> LightPlan:
    return LightPlan(dark=True, reason=reason)


# ------------------------------------------------------------------- fakes
class FakeLayout:
    """`sweep()` returns a token and records the progress it was asked to draw."""

    def __init__(self, drawable: bool = True) -> None:
        self.drawable = drawable
        self.sweeps: list[float] = []

    def sweep(self, p):
        self.sweeps.append(p)
        return None if not self.drawable else f"frame@{p:.3f}"


class FakePusher:
    def __init__(self) -> None:
        self.pushed: list = []
        self.invalidated = 0

    def push(self, img) -> None:
        self.pushed.append(img)

    def invalidate(self) -> None:
        self.invalidated += 1


def _burn(*, exercise_s: float = 6.0, enabled: bool = True, due: bool = True) -> BurnIn:
    """A burn-in counter whose exercise is due now, on a clock the test drives.

    `now` is handed to `sweep_step` explicitly, so a twelve-second animation is
    stepped through in microseconds without sleeping for any of it. `BurnIn` counts
    on `time.monotonic()`, so the virtual clock has to start there and not at zero.
    """
    burn = BurnIn({"burnin": {"shift_every_min": 5, "exercise_every_h": 1,
                              "exercise_s": exercise_s, "exercise_enabled": enabled}})
    burn._last_exercise_end = time.monotonic() - (3605.0 if due else -3605.0)
    return burn


def _clock(step_s: float = 1.0):
    """One tick per call: the interval the control loop actually runs at."""
    base = time.monotonic()
    tick = [0]

    def next_now() -> float:
        tick[0] += 1
        return base + tick[0] * step_s
    return next_now


def _quiet_guard() -> "main.Guard":
    return main.Guard(log=lambda m: None)


# ------------------------------------------------------------ behavioural
def case_api() -> bool:
    print("case: the loop has a step to call")
    check("main.sweep_step exists (one frame per call, not a nested loop)",
          hasattr(main, "sweep_step"), True)
    bare = BurnIn({"burnin": {"shift_every_min": 5, "exercise_every_h": 1,
                              "exercise_s": 1}})
    check("BurnIn can be told the sweep is unwanted", hasattr(bare, "enabled"), True)
    return hasattr(main, "sweep_step")


def case_one_frame_per_tick() -> None:
    print("case: one frame per call, and the exercise still finishes")
    burn, layout, pusher, g = _burn(exercise_s=6.0), FakeLayout(), FakePusher(), _quiet_guard()
    clock = _clock(step_s=1.0)
    owned = [main.sweep_step(burn, layout, pusher, LIT, "idle", True, g, now=clock())
             for _ in range(10)]
    check("six 1 s steps of a 6 s exercise = six frames", len(pusher.pushed), 6)
    check("and the sweep owns exactly those six ticks", sum(owned), 6)
    check("progress only advances", layout.sweeps == sorted(layout.sweeps), True)
    check("every progress is inside the exercise",
          all(0.0 <= p < 1.0 for p in layout.sweeps), True)
    check("no frame after it finishes", len(pusher.pushed), 6)
    check("the finished sweep hands the panel back as a whole frame",
          pusher.invalidated >= 1, True)
    check("nothing is left running", burn.exercise_progress(clock()), None)


def case_dark_ends_it() -> None:
    print("case: dark mid-sweep sends nothing bright afterwards")
    # Every reason the light policy has for dark, injected two frames into a sweep:
    # the display timeout, a suspend, a lock, and the idle timer. They have to behave
    # the same as each other — the sweep may not outrank any of them.
    for reason in ("monitor-off", "asleep", "locked", "idle"):
        burn, layout, pusher, g = _burn(), FakeLayout(), FakePusher(), _quiet_guard()
        clock = _clock()
        main.sweep_step(burn, layout, pusher, LIT, "idle", True, g, now=clock())
        main.sweep_step(burn, layout, pusher, LIT, "idle", True, g, now=clock())
        frames = len(pusher.pushed)
        owned = [main.sweep_step(burn, layout, pusher, dark(reason), "idle", True, g,
                                 now=clock()) for _ in range(8)]
        check(f"{reason}: no further frame pushed", len(pusher.pushed), frames)
        check(f"{reason}: nothing further even drawn", len(layout.sweeps), frames)
        check(f"{reason}: the tick is not owned", any(owned), False)
        check(f"{reason}: abandoned, not paused", burn.exercise_progress(clock()), None)
        check(f"{reason}: counted as a postponement", burn.postponed, 1)


def case_game_ends_it() -> None:
    print("case: a game starting mid-sweep abandons it")
    burn, layout, pusher, g = _burn(), FakeLayout(), FakePusher(), _quiet_guard()
    clock = _clock()
    main.sweep_step(burn, layout, pusher, LIT, "idle", True, g, now=clock())
    frames = len(pusher.pushed)
    not_owned = True
    for _ in range(5):
        not_owned = not main.sweep_step(burn, layout, pusher, LIT, "game", True, g,
                                        now=clock())
    check("no frame after the game starts", len(pusher.pushed), frames)
    check("nothing further even drawn", len(layout.sweeps), frames)
    check("the tick is not owned", not_owned, True)
    check("the sweep is gone, not waiting for the game to end",
          burn.exercise_progress(clock()), None)


def case_dead_link_is_not_pushed_into() -> None:
    print("case: a link that is down is never pushed into")
    # The transport fault in the issue: pushing into a wedged endpoint pays the write
    # deadline for pixels that cannot arrive. Twelve seconds of that is exactly how a
    # sweep becomes an unresponsive recovery loop, so the link gets a veto before the
    # frame is even drawn. That `panel.ok` is the signal that really drops when a
    # write blocks is `panel_link_selftest`'s claim to hold, not this file's.
    burn, layout, pusher, g = _burn(), FakeLayout(), FakePusher(), _quiet_guard()
    clock = _clock()
    owned = [main.sweep_step(burn, layout, pusher, LIT, "idle", False, g, now=clock())
             for _ in range(6)]
    check("nothing drawn", layout.sweeps, [])
    check("nothing pushed", pusher.pushed, [])
    check("tick not owned", any(owned), False)
    check("exercise abandoned", burn.exercise_progress(clock()), None)

    # … and mid-sweep: the link wedges after two frames.
    burn2, layout2, pusher2 = _burn(), FakeLayout(), FakePusher()
    g2, clock2 = _quiet_guard(), _clock()
    main.sweep_step(burn2, layout2, pusher2, LIT, "idle", True, g2, now=clock2())
    main.sweep_step(burn2, layout2, pusher2, LIT, "idle", True, g2, now=clock2())
    frames = len(pusher2.pushed)
    for _ in range(5):
        main.sweep_step(burn2, layout2, pusher2, LIT, "idle", False, g2, now=clock2())
    check("no frame pushed once the link went down", len(pusher2.pushed), frames)


def case_undrawable_is_not_retried() -> None:
    print("case: a sweep that cannot draw is given up on, not retried per tick")
    burn, layout, pusher, g = _burn(), FakeLayout(drawable=False), FakePusher(), _quiet_guard()
    clock = _clock()
    owned = [main.sweep_step(burn, layout, pusher, LIT, "idle", True, g, now=clock())
             for _ in range(6)]
    check("asked to draw once", len(layout.sweeps), 1)
    check("nothing pushed", pusher.pushed, [])
    check("tick not owned", any(owned), False)
    check("given up on", burn.exercise_progress(clock()), None)


def case_sweep_is_opt_in() -> None:
    print("case: the visible sweep is off, the pixel shift is not")
    shipped = cfgmod.load()["burnin"]
    # `.get` so a missing switch reads as the FAIL it is instead of a traceback that
    # would hide every case after it.
    check("DEFAULTS and the shipped config.yaml both say off",
          shipped.get("exercise_enabled", "<key absent>"), False)
    check("so that a bare `BurnIn(cfg)` sees the switch", "exercise_enabled" in shipped,
          True)
    now = time.monotonic() + 10.0
    off = _burn(enabled=False)
    check("a disabled sweep never arms", off.exercise_due(now), False)
    check("so there is nothing to draw", off.exercise_progress(now), None)
    on = _burn(enabled=True)
    on.exercise_due(now)
    check("and the switch is what makes the difference, not the fixture",
          on.exercise_progress(now), 0.0)
    # Turning the visible animation off must not turn off the anti-aging offset,
    # which is the part that actually protects the panel and costs nothing to run.
    check("the pixel shift still moves", off.shift() in SHIFTS, True)


# ---------------------------------------------------------------- structural
def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no top-level {name}() in main.py")


def _calls(node, names: set[str]) -> list[ast.Call]:
    """Calls whose callee is a bare name, or `owner.name` on a plain object."""
    out: list[ast.Call] = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        key = f.id if isinstance(f, ast.Name) else (
            f"{f.value.id}.{f.attr}" if isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name) else "")
        if key in names:
            out.append(n)
    return out


def case_loop_shape() -> None:
    print("case: the sweep is a step inside the tick, not a loop of its own")
    loop = fault_selftest._loop()           # the first `while True:` in main.py
    nested = [n.lineno for n in ast.walk(loop) if n is not loop and isinstance(n, ast.While)]
    check("no nested loop that can run past one iteration", nested, [])
    sleeps = [n.lineno for n in _calls(loop, {"time.sleep"})]
    check("the tick never sleeps to pass the time (`host.wait` is the sanctioned wait)",
          sleeps, [])
    check("the loop steps the sweep exactly once", len(_calls(loop, {"sweep_step"})), 1)
    # One frame on the bus per tick is the transport budget the issue asks to keep:
    # the sweep's frame and the telemetry frame must be alternatives, not neighbours.
    check("one pusher.push in the whole tick", len(_calls(loop, {"pusher.push"})), 1)


def case_step_is_guarded() -> None:
    print("case: the step keeps its device calls inside the guard")
    fn = _fn("sweep_step")
    guarded = fault_selftest._guarded_ids(fn)
    bare: list[str] = []
    total = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in fault_selftest.RISKY:
            total += 1
            if id(node) not in guarded:
                bare.append(f"{f.value.id}.{f.attr}() @ main.py:{node.lineno}")
    check("the walk found the step's subsystem calls", total >= 2, True)
    check("every one of them is guarded", bare, [])
    if bare:
        print("      unguarded: " + ", ".join(bare))


def main_run() -> int:
    have = case_api()
    print()
    if have:
        for fn in (case_one_frame_per_tick, case_dark_ends_it, case_game_ends_it,
                   case_dead_link_is_not_pushed_into, case_undrawable_is_not_retried,
                   case_step_is_guarded):
            fn()
            print()
    else:
        # Named rather than silently skipped: the missing step *is* the bug, and a
        # case that could not run must never read as a case that passed.
        print("behavioural cases not run: main.sweep_step does not exist yet.\n")
    for fn in (case_sweep_is_opt_in, case_loop_shape):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())