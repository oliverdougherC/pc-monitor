"""Prove liveview's hot reload is transactional — with a real Engine and real files.

    .venv\\Scripts\\python tools\\liveview_selftest.py

Issue #32: `tools/liveview.py` reloads its module chain in place while a
preview is running, and every way that can go wrong used to damage the running
preview instead of being refused:

  * a *saved* (even unedited) demo.py produced instances of the reloaded
    `DemoBackend` class, which an `isinstance` against the once-imported old
    class rejected — after both hubs were replaced but before `game = True`
    was set, so the game pane silently became idle data;
  * file stamps advanced before the reload, so a failed reload was never
    retried and its error vanished on the next tick;
  * `importlib.reload` executes in place, so a broken edit left the working
    preview running a half-new, half-old module generation, with no rollback;
  * an invalid config edit raised mid-batch, and `--config` paths other than
    the repo's `config.yaml` were loaded once but never watched.

This drives the real `Engine` (demo backend, no server, no threads) through
exactly the issue's acceptance sequence — touch demo.py, break it, repair it,
break the config, repair it — and pins that a failed reload keeps the previous
working generation (module dicts, hubs, layouts, config contents) untouched,
keeps the failed file pending so the error stays visible and the repair is
picked up without a restart. demo.py is written back byte-identical in a
finally block; the only file it edits is one it restores.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

import liveview as lv                                   # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def touch(p: Path) -> None:
    """Pretend an editor saved p: mtime moves, content does not."""
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns + 10 ** 9, st.st_mtime_ns + 10 ** 9))


def attempt(engine) -> str | None:
    """Run one reload, returning the exception name instead of escaping it —
    a failed reload must be a reported outcome, never a crash of the preview."""
    try:
        engine._maybe_reload()
        return None
    except Exception as exc:                              # noqa: BLE001
        return exc.__class__.__name__


def build_engine(custom: Path):
    """Engine with the custom config; falls back to the old signature so the
    pre-fix code still runs far enough to fail behaviourally, not at import."""
    cfg = lv.cfgmod.load(str(custom))
    try:
        return lv.Engine(cfg, "demo", 1.0, False, "off", cfg_path=str(custom))
    except TypeError:
        return lv.Engine(cfg, "demo", 1.0, False, "off")


def case_custom_config(engine, custom: Path) -> None:
    print("case: the config actually requested by --config is the one watched")
    check("the custom config was loaded to begin with",
          engine.cfg["power"]["base_w"], 99)
    check("the engine watches that path, not the repo default",
          engine._cfg_path, custom.resolve())
    custom.write_text("power:\n  base_w: 42\n", encoding="utf-8")
    err = attempt(engine)
    check("editing it hot-reloads", err, None)
    check("and the new value is live", engine.cfg["power"]["base_w"], 42)


def case_invalid_config(engine, custom: Path) -> None:
    print("case: invalid YAML is refused without touching the working config")
    custom.write_text("power:\n  base_w: [1, 2\n", encoding="utf-8")   # unterminated flow
    err = attempt(engine)
    check("the broken config is refused with an error", err is not None, True)
    check("the working config generation survives",
          engine.cfg["power"]["base_w"], 42)
    check("the failed file stays pending, so the error stays visible",
          engine._stamps.get(custom.resolve()) == lv.Engine._stamp(custom.resolve()), False)
    custom.write_text("power:\n  base_w: 7\n", encoding="utf-8")
    err = attempt(engine)
    check("a repair recovers without restarting the preview", err, None)
    check("and the repaired value is live", engine.cfg["power"]["base_w"], 7)


def case_unsafe_values_are_refused_not_applied(engine, custom: Path) -> None:
    """#20's reload clause: a *parseable* config with an unsafe value must be refused.

    The case above covers YAML that will not parse. This one covers the failure that
    is easier to miss and more dangerous: a config that parses perfectly and carries a
    value the loop cannot run — a zeroed interval, a brightness out of range, a
    geometry that is not this panel. Such an edit used to reach `Layout`/`BurnIn` and
    surface as a divide-by-zero or a nonsense render; `cfgmod.load_or_keep` now keeps
    the last-known-good generation and hands back the reasons, and the preview keeps
    rendering with values it knows are safe.
    """
    print("case: a parseable but unsafe config edit is refused, and stays refused")
    # The baseline is whatever the previous case left live, captured rather than
    # assumed: this case is about the *refusal* keeping it, not about its value.
    baseline = engine.cfg["display"]["brightness_idle"]
    check("a safe value is live first", 0 <= baseline <= 100, True)
    # 9999 is in range for YAML and out of range for a backlight.
    custom.write_text("display:\n  brightness_idle: 9999\n", encoding="utf-8")
    err = attempt(engine)
    # The refusal is not an exception: the page stays up and the reason is reported,
    # because a preview that dies on a typo cannot tell you what the typo was.
    check("the unsafe edit did not stop the preview", err, None)
    check("and the unsafe value did not reach the config",
          engine.cfg["display"]["brightness_idle"], baseline)
    check("the reason is reported", bool(engine.reload_problems), True)
    check("and it names the offending key",
          any("brightness_idle" in p for p in engine.reload_problems), True)
    seen = list(engine.reload_problems)
    # A second tick must not lose it: `step()` clears `error` on every successful
    # tick, and a validity complaint that survives one tick is one nobody reads.
    engine._maybe_reload()
    check("the complaint is still there on the next tick", engine.reload_problems, seen)
    check("the layouts kept the safe value too",
          engine.layouts["idle"].cfg["display"]["brightness_idle"], baseline)
    # And a valid edit clears it, so the page is not stuck complaining.
    custom.write_text("display:\n  brightness_idle: 55\n", encoding="utf-8")
    err = attempt(engine)
    check("a valid edit is accepted", err, None)
    check("it is live", engine.cfg["display"]["brightness_idle"], 55)
    check("and the complaint is gone", engine.reload_problems, [])


def case_demo_touch(engine) -> None:
    print("case: saving demo.py rebuilds the streams against the current class")
    cls = lv.demo_mod.DemoBackend
    hub = engine.hub_game
    touch(Path(lv.demo_mod.__file__))
    err = attempt(engine)
    check("a plain save reloads cleanly", err, None)
    check("the module really got a new class", lv.demo_mod.DemoBackend is not cls, True)
    check("the rebuilt game hub is of that current class",
          isinstance(engine.hub_game.backend, lv.demo_mod.DemoBackend), True)
    check("the game hub object was replaced", engine.hub_game is not hub, True)
    check("and the game pane is still game-shaped", engine.hub_game.backend.game, True)
    check("while the idle pane is still idle-shaped", engine.hub_idle.backend.game, False)


def case_broken_edit(engine) -> None:
    print("case: a broken module edit is refused and rolled back, then repairs itself")
    p = Path(lv.demo_mod.__file__)
    original = p.read_bytes()
    cls = lv.demo_mod.DemoBackend
    hub, lay = engine.hub_game, engine.layouts["idle"]
    # insert after the __future__ import so the module still *compiles*: the
    # reload must die mid-execution, which is what leaves a half-new module
    # dict behind when there is no rollback
    needle = b"from __future__ import annotations\n"
    cut = original.index(needle) + len(needle)
    broken = (original[:cut]
              + b"\nLIVEVIEW_SELFTEST_MARKER = 1\n"
                b"raise RuntimeError('selftest: deliberate broken edit')\n"
              + original[cut:])
    try:
        p.write_bytes(broken)
        err = attempt(engine)
        check("the broken edit is refused", err is not None, True)
        # importlib executes in place: without a rollback the module dict keeps
        # every name bound before the raise, so the preview runs a generation
        # that never fully existed.
        check("the half-executed module is rolled back",
              hasattr(lv.demo_mod, "LIVEVIEW_SELFTEST_MARKER"), False)
        check("the working class survives the failed reload",
              lv.demo_mod.DemoBackend is cls, True)
        check("the running hubs are untouched", engine.hub_game is hub, True)
        check("and still carry the game flag", engine.hub_game.backend.game, True)
        check("the layouts are not swapped mid-failure",
              engine.layouts["idle"] is lay, True)
        check("the failed file stays pending for the next tick",
              engine._stamps.get(p) == lv.Engine._stamp(p), False)
    finally:
        p.write_bytes(original)
    check("demo.py is byte-identical to before the test", p.read_bytes(), original)
    err = attempt(engine)
    check("the repair reloads without restarting the preview", err, None)
    check("the class is finally the new one", lv.demo_mod.DemoBackend is not cls, True)
    check("and the rebuilt game hub is still game-shaped",
          engine.hub_game.backend.game, True)


def case_layout_edit(engine) -> None:
    print("case: a layout edit rebuilds the layouts and carries the history across")
    lay, hist = engine.layouts["idle"], engine.layouts["idle"].history
    touch(Path(lv.layout_mod.__file__))
    err = attempt(engine)
    check("the layout edit reloads cleanly", err, None)
    check("the layouts are new objects", engine.layouts["idle"] is not lay, True)
    check("the trend history survives the reload",
          engine.layouts["idle"].history is hist, True)
    check("and the game hub kept its shape through a non-demo edit",
          engine.hub_game.backend.game, True)


def main_run() -> int:
    with tempfile.TemporaryDirectory(prefix="liveview-selftest-") as tds:
        custom = Path(tds) / "custom.yaml"
        custom.write_text("power:\n  base_w: 99\n", encoding="utf-8")
        engine = build_engine(custom)
        check("the preview is up and ticking", engine.tick_n, 0)  # nothing ticked yet
        case_custom_config(engine, custom.resolve())
        print()
        case_invalid_config(engine, custom.resolve())
        print()
        case_unsafe_values_are_refused_not_applied(engine, custom.resolve())
        print()
        case_demo_touch(engine)
        print()
        case_broken_edit(engine)
        print()
        case_layout_edit(engine)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())