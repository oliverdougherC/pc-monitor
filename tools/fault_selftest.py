"""Prove the loop survives a subsystem raising — without breaking the app to find out.

    .venv\\Scripts\\python tools\\fault_selftest.py

The app runs under `pythonw.exe`: no console, no traceback on screen. So the one
failure mode that used to be invisible was a raise inside the tick — the panel simply
stopped updating and `log.log` said nothing wrong. `main.Guard` is the containment,
and this file pins two separate things about it:

  behaviour   a raise yields the fallback and one log line, repeats are counted
              rather than re-reported, a *different* subsystem still gets said,
              `SystemExit` is contained (the vendored driver raises it for a port
              that will not open) while Ctrl-C still gets through;

  coverage    an AST pass over the render loop: every call into a subsystem this
              process does not own (the panel link, the ETW child, the event window,
              Pillow, the sensor hub, the diff pusher) must sit inside a `g.run(...)`
              argument or inside a `try` with a handler. Without this half, the
              guard is a claim: a later edit that adds `layout.render(...)` bare
              would restore the silent death and nothing would notice until a
              morning with a frozen panel.
"""
import ast
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

import main                                     # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


class Boom(RuntimeError):
    pass


def case_basic() -> None:
    print("case: a raise is a fallback and one log line")
    log: list[str] = []
    g = main.Guard(log=log.append)
    check("good call returns its value", g.run("x", lambda: 7, -1), 7)
    check("raising call returns the fallback", g.run("x", _raise, -1), -1)
    check("counted", g.count, 1)
    check("logged once", len(log), 1)
    check("names the subsystem", "x raised" in log[0], True)
    check("carries the exception text", "Boom" in log[0], True)
    check("says the tick continues", "tick continues" in log[0], True)


def _raise():
    raise Boom("the panel lied")


def case_repeats() -> None:
    print("case: the same fault is counted, not re-reported")
    log: list[str] = []
    g = main.Guard(log=log.append)
    for _ in range(300):
        g.run("sensors", _raise, None)
    check("all 300 counted", g.count, 300)
    check("reported once", len(log), 1)
    check("summary tells the count", g.summary(), " faults=300(sensors)")
    # … and a quiet window still ends, so a fault that outlives the loop is visible.
    g._at = time.monotonic() - 400.0
    g.run("sensors", _raise, None)
    check("re-reported after the quiet window", len(log), 2)


def case_second_subsystem() -> None:
    print("case: a second, different fault is new information")
    log: list[str] = []
    g = main.Guard(log=log.append)
    g.run("panel", _raise, None)
    g.run("render", _raise, None)
    check("both logged", len(log), 2)
    check("the second names itself", "render raised" in log[1], True)
    check("the count is total", g.count, 2)


def case_exit_and_ctrl_c() -> None:
    print("case: SystemExit is contained, KeyboardInterrupt is not")
    log: list[str] = []
    g = main.Guard(log=log.append)

    def _sys_exit():
        raise SystemExit(0)      # exactly what the vendored driver does for a bad port

    check("SystemExit contained", g.run("panel", _sys_exit, "fallback"), "fallback")
    check("and logged", len(log), 1)
    try:
        g.run("wait", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        check("Ctrl-C still escapes", False, True)
    except KeyboardInterrupt:
        check("Ctrl-C still escapes", True, True)


def case_thread_net() -> None:
    print("case: a thread that dies says so")
    logged: list[str] = []
    real, main.app_log = main.app_log, lambda *a, **k: logged.append(str(a))
    try:
        main._thread_safety_net()
        hook = threading.excepthook
        check("hook installed", hook is not threading.__excepthook__, True)

        def _raise_in_thread():
            raise Boom("builder died")

        t = threading.Thread(target=_raise_in_thread, name="panel-bring-up")
        t.start()
        t.join()
        # The default hook would have printed to stderr and been forgotten.
        joined = " ".join(logged)
        check("the death was logged", "panel-bring-up" in joined, True)
        check("with the exception", "Boom" in joined, True)
    finally:
        main.app_log = real
        threading.excepthook = threading.__excepthook__


# ------------------------------------------------------------------ coverage
# Names whose implementation reaches outside this process: a device, a child, the
# window manager, the OS. `burn` is deliberately absent — burn-in is arithmetic.
RISKY = {"host", "panel", "pusher", "layout", "watch", "frames_mon", "lights",
         "night", "hub"}
RISKY_FUNCS = {"estimate"}          # module-level helpers that read other processes


def _loop() -> ast.While:
    """The render loop: the first `while True:` in main.py."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            return node
    raise AssertionError("no while loop found in main.py — did the loop move?")


def _guarded_ids(loop: ast.While) -> set[int]:
    ids: set[int] = set()
    named: set[str] = set()          # `g.run("beat", _beat)` — guard by name too
    for node in ast.walk(loop):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "g" \
                and node.func.attr in ("run", "text"):
            for arg in node.args[1:]:      # everything after the name is inside the guard
                if isinstance(arg, ast.Name):
                    named.add(arg.id)
                for n in ast.walk(arg):
                    ids.add(id(n))
                    if isinstance(n, ast.Call):
                        for kw in n.keywords:
                            ids.add(id(kw))
                            ids |= {id(x) for x in ast.walk(kw.value)}
        elif isinstance(node, ast.Try) and node.handlers:
            ids |= {id(n) for n in ast.walk(node)}
    # A helper built in the loop and handed to the guard is guarded — that is how the
    # beat line keeps its readability without becoming five separately-wrapped calls.
    for node in ast.walk(loop):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in named:
            ids |= {id(n) for n in ast.walk(node)}
    return ids


def case_coverage() -> None:
    print("case: nothing outside the guard is left inside the loop")
    loop = _loop()
    guarded = _guarded_ids(loop)
    bare = []
    total = 0
    for node in ast.walk(loop):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in RISKY:
            total += 1
            if id(node) not in guarded:
                bare.append(f"{f.value.id}.{f.attr}() @ main.py:{node.lineno}")
        elif isinstance(f, ast.Name) and f.id in RISKY_FUNCS:
            total += 1
            if id(node) not in guarded:
                bare.append(f"{f.id}() @ main.py:{node.lineno}")
    check("the walk found the subsystem calls", total >= 20, True)
    check("every one of them is guarded", bare, [])
    if bare:
        print("      unguarded: " + ", ".join(bare))


def main_run() -> int:
    for fn in (case_basic, case_repeats, case_second_subsystem, case_exit_and_ctrl_c,
               case_thread_net, case_coverage):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())
