"""Offline proof of the single-instance lock: app/instance.py and its wiring.

    .venv\\Scripts\\python tools\\instance_selftest.py

Issue #24's acceptance is racing two real starts — a desk step. The primitive's
contract and the startup ordering are both provable offline, no admin, no
hardware, no panel:

  claim     the first acquire owns the lock; a second claim is refused with a
            sentence worth printing, and the refusal is the same one a second
            process gets (Windows reports ERROR_ALREADY_EXISTS to a duplicate
            claim in any process, ours included);
  crash     a process that dies holding the lock releases it — the OS closes
            handles for dead processes, which is the whole reason this is a
            mutex and not a pid file. The child here is this test's own child,
            killed with kill() to skip every cleanup: the closest offline
            shape of Stop-Process or a crash, and the acceptance line "a
            subsequent legitimate start must recover without a stale lock";
  order     main.py must acquire before it can touch the panel or reclaim the
            capture session. The issue asks for static inspection of startup
            ownership, so that is what this checks: the call site precedes the
            FrameMonitor and PanelLink constructions in main().

The lock name is a per-run test name, so a real PC Monitor run on this machine
cannot collide with the gate, and the gate cannot disturb a real run.
"""
import ast
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from app.instance import acquire_main_role, release      # noqa: E402

sys.stdout.reconfigure(errors="replace")

NAME = f"PCMonitor-selftest-{os.getpid()}"
fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}"
          + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def case_claim() -> None:
    print("case: the first claim owns, the second is refused")
    ok1, why1 = acquire_main_role(NAME)
    check("first claim owns", ok1, True)
    print(f"  detail: {why1}")
    ok2, why2 = acquire_main_role(NAME)
    check("second claim refused", ok2, False)
    print(f"  detail: {why2}")


def case_crash_release() -> None:
    print("case: a dead owner releases it — no stale lock is possible")
    release(NAME)                      # hand the lock to the child
    # The child claims the lock and is killed: no atexit, no cleanup, no
    # release() — the shape of a crash or a Stop-Process. A pid file would be
    # pointing at a corpse here; the mutex cannot.
    child = ("import sys, time; sys.path.insert(0, '.');"
             "from app.instance import acquire_main_role;"
             f"ok, why = acquire_main_role({NAME!r});"
             "print('HELD' if ok else 'DENIED ' + why, flush=True);"
             "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", child],
                         stdout=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline().strip()
        check("child acquired it", line, "HELD")
        ok2, _ = acquire_main_role(NAME)
        check("the live owner blocks us", ok2, False)
    finally:
        p.kill()                       # our own child, killed to skip cleanup
        p.wait(timeout=30)
    ok3, why3 = False, ""
    deadline = time.monotonic() + 5.0  # teardown closes handles promptly; the
    while time.monotonic() < deadline:  # loop is insurance, not a protocol
        ok3, why3 = acquire_main_role(NAME)
        if ok3:
            break
        time.sleep(0.05)
    check("re-acquired right after the child died", ok3, True)
    print(f"  detail: {why3}")


def case_startup_order() -> None:
    print("case: main.py takes the lock before the hardware")
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main_fn is None:
        fails.append("main() found")
        print("  FAIL main() found")
        return

    def first_call(pred):
        lines = [n.lineno for n in ast.walk(main_fn)
                 if isinstance(n, ast.Call) and pred(n.func)]
        return min(lines) if lines else None

    def named(*names):
        return lambda f: isinstance(f, ast.Name) and f.id in names

    acq = first_call(named("acquire_main_role"))
    frames = first_call(named("FrameMonitor"))
    panel = first_call(named("PanelLink"))
    check("main() acquires the lock", acq is not None, True)
    check("before the capture child can reclaim the session",
          acq is not None and frames is not None and acq < frames, True)
    check("before the panel link exists",
          acq is not None and panel is not None and acq < panel, True)
    print(f"  lines: acquire={acq} FrameMonitor={frames} PanelLink={panel}")


def main() -> int:
    case_claim()
    print()
    case_crash_release()
    print()
    case_startup_order()
    release(NAME)                      # leave nothing behind for the next run
    print("\n" + ("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
