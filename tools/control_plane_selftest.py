"""One control plane, one owner: a --dump runs beside the app without touching it.

    .venv\\Scripts\\python tools\\control_plane_selftest.py

Issue #67's defect is cross-role interference in the *shared* files, and the only
honest way to test that is with two roles: a production control plane on disk, and a
real `main.py --dump` child running against it. Nothing in this file starts the app,
opens the panel or touches the ETW session — the dump is the headless preview role
(there is no panel in it by construction), and `--backend demo` needs no hardware.

The tools the child is given:

  `.owner`       the record the installer reads to decide which pid is the app
  `.stop`        the installer's polite shutdown request
  `.heartbeat`   what the outside observer reads to decide the app is stuck
  `.stopped`     the marker that says a shutdown was deliberate, so recovery holds

`PCMON_CONTROL_DIR` points the child at a scratch directory (see `app/owned.py` and
`app/liveness.py`), which is what makes the assertions below possible at all: the
test seeds what the *app* had put there, runs a real dump, and then compares byte for
byte. A dump that wrote the owner record, consumed the stop request, refreshed the
heartbeat, deleted the record or marked the app stopped on purpose shows up here as a
changed file, and there is no way for it to show up as anything else.

Three shapes are covered, which is what the issue asked for: the dump completing
normally, the dump being interrupted (Ctrl-C), and the dump dying before it ever
reached a tick.
"""
import ast
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
fails: list[str] = []

# Everything the production role owns. The child may add its own `-dump` suffixed
# files beside these; what it may not do is move, rewrite or delete any of them.
PRODUCTION_FILES = (".owner", ".stop", ".heartbeat", ".stopped", ".restarts")


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}"
          + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def child_env(control: Path) -> dict:
    env = dict(os.environ)
    env["PCMON_CONTROL_DIR"] = str(control)
    env["PYTHONPATH"] = str(ROOT)
    return env


def seed_production(control: Path) -> dict[str, bytes]:
    """The state a running app leaves behind, as bytes we can compare afterwards.

    `os.getpid()` is the *test's* pid, and that is deliberate: the observer's own
    liveness probe is read-only, so reusing this process's number makes the seeded
    beat a live one. A dump must leave it exactly as it found it regardless.
    """
    seed = {
        # A record that names a pid, its creation time and an instance token: the
        # file the installer's whole stop path reads.
        ".owner": ('{"pid": %d, "started": %f, "main": "%s", "root": "%s", '
                   '"session": "PCMonitor-main", "role": "main", '
                   '"instance": "the-running-apps-token"}'
                   % (os.getpid(), time.time(),
                      str(ROOT / "main.py").replace("\\", "\\\\"),
                      str(ROOT).replace("\\", "\\\\"))).encode(),
        # A stop request addressed to the running app and not yet consumed.
        ".stop": f"{time.time():.3f} requested\n".encode(),
        # A beat a little stale but inside the observer's stall window.
        ".heartbeat": f"{time.time() - 5.0:.3f} {os.getpid()} 4242 idle\n".encode(),
        ".restarts": b"",
    }
    control.mkdir(parents=True, exist_ok=True)
    for name, blob in seed.items():
        (control / name).write_bytes(blob)
    return {name: blob for name, blob in seed.items()
            if name in PRODUCTION_FILES}


def snapshot(control: Path) -> dict[str, bytes]:
    out = {}
    if control.exists():
        for p in sorted(control.iterdir()):
            if p.is_file():
                out[p.name] = p.read_bytes()
    return out


def untouched(control: Path, before: dict[str, bytes], label: str) -> None:
    """Every production file is byte-identical, and no production file appeared."""
    after = snapshot(control)
    for name, blob in before.items():
        check(f"{label}: {name} is byte-identical", after.get(name), blob)
    for name in PRODUCTION_FILES:
        if name not in before:
            check(f"{label}: {name} was not created", name in after, False)


def run_dump(control: Path, out: Path, frames: int = 2, timeout: float = 180.0,
             interrupt_after: float | None = None) -> subprocess.CompletedProcess:
    """One real headless dump. `interrupt_after` interrupts it instead of waiting.

    The interrupt has to be a real Ctrl-C, because the thing under test is what the
    child does *in its KeyboardInterrupt handler*. On Windows that means CTRL_BREAK
    delivered to a process group of the child's own (`CREATE_NEW_PROCESS_GROUP`):
    `Popen.send_signal(SIGINT)` raises `ValueError: Unsupported signal: 2`, and
    `terminate()` would kill the child before the handler ever ran, which is the one
    thing this case must not do. If the signal cannot be delivered at all the case
    falls back to `kill()` and says so, rather than reporting a pass it did not earn.
    """
    cmd = [sys.executable, str(MAIN), "--dump", str(out),
           "--backend", "demo", "--frames", str(frames)]
    if interrupt_after is None:
        return subprocess.run(cmd, cwd=str(ROOT), env=child_env(control),
                              capture_output=True, text=True, timeout=timeout)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen(cmd, cwd=str(ROOT), env=child_env(control),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         creationflags=flags)
    delivered = False
    try:
        time.sleep(interrupt_after)
        try:
            os.kill(p.pid, signal.CTRL_BREAK_EVENT)
            delivered = True
        except (AttributeError, OSError, ValueError):
            p.kill()
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=30)
    if not delivered:
        print("    note: CTRL_BREAK could not be delivered; the child was killed "
              "instead (the handler did not run)")
    return subprocess.CompletedProcess(cmd, p.returncode, "", "")


def case_normal_completion(tmp: Path) -> None:
    print("case: a dump that runs to completion leaves production alone")
    control = tmp / "normal"
    before = seed_production(control)
    out = tmp / "normal.png"
    r = run_dump(control, out)
    check("the dump actually rendered a frame", out.exists() and out.stat().st_size > 0,
          True)
    check("and exited cleanly", r.returncode, 0)
    untouched(control, before, "completed dump")
    # The other half of the contract: isolation must not mean the role has no state
    # at all. Its own beat is where it belongs, under its own name.
    check("the dump beat under its own name",
          (control / ".heartbeat-dump").exists(), True)
    check("and the app's beat is still the app's",
          (control / ".heartbeat").read_bytes(), before[".heartbeat"])


def case_interrupted(tmp: Path) -> None:
    print("case: an interrupted dump cannot mark the app deliberately stopped")
    control = tmp / "interrupted"
    before = seed_production(control)
    out = tmp / "interrupted.png"
    r = run_dump(control, out, frames=600, interrupt_after=12.0)
    check("the dump did not finish", r.returncode != 0, True)
    # This is the sharp end of #67: `.stopped` is what makes the observer *hold* and
    # refuse to restart the app. A Ctrl-C on a preview used to write it.
    check("no deliberate-stop marker was written",
          (control / ".stopped").exists(), False)
    untouched(control, before, "interrupted dump")


def case_dies_before_the_first_tick(tmp: Path) -> None:
    print("case: a dump that dies at start-up still leaves production alone")
    control = tmp / "died"
    before = seed_production(control)
    out = tmp / "died.png"
    # A config it cannot run: `load()` refuses it and the process dies before the
    # loop, which is the third shape the issue names. The dump must not have touched
    # the record on its way out — that is what the `atexit` clear used to do.
    bad = tmp / "bad-config.yaml"
    bad.write_text("display:\n  brightness_idle: 9999\n", encoding="utf-8")
    cmd = [sys.executable, str(MAIN), "--config", str(bad), "--dump", str(out),
           "--backend", "demo", "--frames", "1"]
    r = subprocess.run(cmd, cwd=str(ROOT), env=child_env(control),
                       capture_output=True, text=True, timeout=180.0)
    check("the dump failed on the bad config", r.returncode != 0, True)
    untouched(control, before, "dead-on-arrival dump")


def case_main_gates_every_shared_write() -> None:
    print("case: main.py writes shared state only as the owner (static)")
    # The behavioural cases above run one role. This one reads main.py and fails if a
    # *new* shared-state call is ever added outside the role gates — which is how #67
    # got here, and is the kind of thing a test that only runs a dump cannot catch.
    #
    # The search covers the whole module, not just `main()`: two of the calls live
    # outside it by design — `atexit.register(clear_record, token)` is inside `main()`,
    # but the Ctrl-C `mark_stopped` is in the module-level `__main__` handler, which is
    # exactly where a role gate is easiest to forget.
    src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if fn is None:
        fails.append("main() found")
        print("  FAIL main() found")
        return
    body = list(ast.walk(tree))          # every call site in main.py

    def calls_named(*names):
        return [n for n in body
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in names]

    # `clear_record` is never *called* — it is handed to `atexit` — so looking for a
    # call would never find it. What matters is that it is registered with a token and
    # that the registration happens inside the owner gate.
    for call in [n for n in body if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "register"
                 and any(isinstance(a, ast.Name) and a.id == "clear_record"
                         for a in n.args)]:
        check("clear_record is registered with a token", len(call.args) == 2, True)
        gate = nearest_role_gate(tree, call)
        check("and the registration sits inside a role gate", gate is not None, True)
        check("...and that gate is the owner check", owner_gate(gate), True)
    check("clear_record is not called anywhere",
          len(calls_named("clear_record")), 0)

    for name in ("write_record", "stop_requested", "mark_stopped"):
        found = calls_named(name)
        check(f"main.py calls {name}", len(found) >= 1, True)
        for call in found:
            gate = nearest_role_gate(tree, call)
            check(f"{name} at line {call.lineno} sits inside a role gate",
                  gate is not None, True)
            if gate is not None:
                # Two spellings of the same gate are legitimate: `owns_installation`
                # (the startup block, the loop) and `not dump_role_requested()` (the
                # `__main__` handler, which cannot see a name `main()` binds).
                check(f"...and that gate is a role check ({name}:{call.lineno})",
                      owner_gate(gate) or dump_gate(gate), True)
    for call in calls_named("beat_liveness"):
        kw = {k.arg for k in call.keywords}
        check("every beat names its role", "role" in kw, True)
    # And the variable itself must be derived from the role, not hardwired true.
    check("owns_installation is derived from the role",
          "owns_installation = not dump_mode" in src, True)
    check("and each beat role reaches beat_liveness",
          src.count('beat_role = "dump" if dump_mode else "main"'), 1)
    # The `__main__` handler cannot see a name bound inside `main()`. This is the bug
    # that reading caught and running did not: `mark_stopped` there used
    # `owns_installation`, which does not exist at module scope, so the handler raised
    # NameError and wrote no marker for *any* role — production included, which is #23's
    # contract. Anything the handler reads must be reachable without `main()`'s frame.
    handler = next((n for n in ast.walk(tree) if isinstance(n, ast.Try)
                    and any(isinstance(h, ast.ExceptHandler)
                            and h.type is not None
                            and getattr(h.type, "id", "") == "KeyboardInterrupt"
                            for h in n.handlers)), None)
    if handler is None:
        fails.append("the Ctrl-C handler exists")
        print("  FAIL the Ctrl-C handler exists")
        return
    handler_names = {n.id for n in ast.walk(handler) if isinstance(n, ast.Name)}
    handler_calls = {n.func.id for n in ast.walk(handler)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("the Ctrl-C handler asks the module which role this is",
          "dump_role_requested" in handler_calls, True)
    check("and never reads a name main() binds locally",
          "owns_installation" in handler_names, False)
    check("the deliberate-stop marker is behind the role check",
          _mark_stopped_is_role_gated(handler), True)


def _mark_stopped_is_role_gated(handler: ast.Try) -> bool:
    """Is the handler's `mark_stopped` inside `if not dump_role_requested():`?"""
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        calls = [n.func.id for n in ast.walk(node)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if "mark_stopped" in calls and "dump_role_requested" in names:
            return True
    return False


def nearest_role_gate(tree: ast.AST, call: ast.Call) -> ast.AST | None:
    """The innermost test that guards `call`, or None when it sits unguarded.

    Found by AST containment, not by line ordering: a call is gated only if some test's
    *guarded body* contains it, which an ancestor walk can say and a line comparison
    cannot (a call after an unrelated `if` is not gated by it).
    """
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.While, ast.For)):
            continue
        if call_in_body(node, call):
            best = node.test
    return best


def call_in_body(node: ast.AST, call: ast.Call) -> bool:
    """Is `call` inside this node's guarded body (not its test or nested deeper first)?"""
    for field in ("body", "orelse", "finalbody"):
        for stmt in getattr(node, field, []) or []:
            if any(sub is call for sub in ast.walk(stmt)):
                return True
    for h in getattr(node, "handlers", []) or []:
        if any(sub is call for sub in ast.walk(h)):
            return True
    return False


def owner_gate(test: ast.AST) -> bool:
    """Is this test `owns_installation` (possibly aliased to a local first)?"""
    if isinstance(test, ast.BoolOp):
        return any(owner_gate(v) for v in test.values)
    if isinstance(test, ast.UnaryOp):
        return owner_gate(test.operand)
    if isinstance(test, ast.Name):
        return test.id in ("owns_installation",)
    return False


def dump_gate(test: ast.AST) -> bool:
    """Is this test `not dump_role_requested()` — the handler's spelling of the role?"""
    if isinstance(test, ast.BoolOp):
        return any(dump_gate(v) for v in test.values)
    if isinstance(test, ast.UnaryOp):
        return dump_gate(test.operand)
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name):
        return test.func.id == "dump_role_requested"
    return False


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pcmon-control-plane-") as d:
        tmp = Path(d)
        case_normal_completion(tmp)
        print()
        case_interrupted(tmp)
        print()
        case_dies_before_the_first_tick(tmp)
        print()
        case_main_gates_every_shared_write()
    print("\n" + ("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
