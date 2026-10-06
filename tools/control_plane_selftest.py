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
import json
import os
import re
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
             ) -> subprocess.CompletedProcess:
    """One real headless dump, in its own scratch control plane.

    A whole `main.py` per case, not a mocked entry point: the point of these cases is
    what a *second process* does to the first one's files, and anything short of running
    the real entry point would be testing the test.
    """
    cmd = [sys.executable, str(MAIN), "--dump", str(out),
           "--backend", "demo", "--frames", str(frames)]
    return subprocess.run(cmd, cwd=str(ROOT), env=child_env(control),
                          capture_output=True, text=True, timeout=timeout)


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
    # This is the sharp end of #67, and it is deliberately *not* driven by a signal.
    # Delivering a real Ctrl-C to a console-less child on this platform proved
    # unreliable enough that a case built on it would be asserting on a process nobody
    # interrupted: `send_signal(SIGINT)` raises, CTRL_BREAK reaches a bare `python -c`
    # loop but not this one through `runpy`, and `PyThreadState_SetAsyncExc` reports
    # success while the exception is never observed (the loop is inside a pipe write).
    #
    # So the decision itself is called, in a child process, with the same argv a real
    # `main.py --dump` gets. `note_deliberate_stop` is the *app's own* function now —
    # the `__main__` handler is a two-line call to it — so this exercises the production
    # code path rather than a re-implementation of it.
    control = tmp / "interrupted"
    before = seed_production(control)
    probe = (
        "import sys; sys.path.insert(0, %r);"
        "import main as M;"
        "print('WROTE', M.note_deliberate_stop('keyboard-interrupt'));"
        % str(ROOT)
    )
    for role, argv in (("dump", ["main.py", "--dump", "x.png"]),
                       ("main", ["main.py"])):
        r = subprocess.run([sys.executable, "-c", probe, *argv], cwd=str(ROOT),
                           env=child_env(control), capture_output=True, text=True,
                           timeout=120)
        wrote = r.stdout.strip().endswith("WROTE True")
        marker = (control / (".stopped" if role == "main" else ".stopped-dump")).exists()
        if role == "dump":
            check("a preview's Ctrl-C does not write the app's marker", wrote, False)
            check("and writes no deliberate-stop marker at all", marker, False)
        else:
            check("the owner's Ctrl-C does write it", wrote, True)
            check("in the production marker file", marker, True)
    # The production marker the owner just wrote is *not* part of the seeded baseline, so
    # it is checked and cleared before the byte-for-byte comparison.
    (control / ".stopped").unlink(missing_ok=True)
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
            # Three spellings of the same gate are legitimate: `owns_installation` (the
            # startup block and the loop), `not dump_role_requested()` (the Ctrl-C
            # decision), and the early return a function uses when the role check comes
            # first and the marker after it — `nearest_role_gate` cannot see that one,
            # because `mark_stopped` is not lexically inside the `if`.
            covered = (gate is not None
                       and (owner_gate(gate) or dump_gate(gate))) \
                or _mark_stopped_is_role_gated(_enclosing(tree, call))
            check(f"{name} at line {call.lineno} sits inside a role gate", covered, True)
    for call in calls_named("beat_liveness"):
        kw = {k.arg for k in call.keywords}
        check("every beat names its role", "role" in kw, True)
    # And the variable itself must be derived from the role, not hardwired true.
    check("owns_installation is derived from the role",
          "owns_installation = not dump_mode" in src, True)
    check("and each beat role reaches beat_liveness",
          src.count('beat_role = "dump" if dump_mode else "main"'), 1)
    # The Ctrl-C decision lives in an app function (`main.note_deliberate_stop`), not in
    # the `__main__` block, and that is a correctness requirement rather than a style
    # one: the block is module scope, so it cannot see anything `main()` binds. Reading
    # `owns_installation` there was a NameError inside the one handler that must never
    # fail — no marker would have been written for *any* role, production included, which
    # is #23's contract. Keeping the decision in a function makes it reachable, testable,
    # and impossible to get wrong by scope.
    decision = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "note_deliberate_stop"), None)
    if decision is None:
        fails.append("note_deliberate_stop exists")
        print("  FAIL note_deliberate_stop exists")
        return
    decision_names = {n.id for n in ast.walk(decision) if isinstance(n, ast.Name)}
    check("the decision asks the module which role this is",
          "dump_role_requested" in decision_names, True)
    check("it is the role check that gates the deliberate-stop marker",
          _mark_stopped_is_role_gated(decision), True)

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
    check("the Ctrl-C handler calls that decision",
          "note_deliberate_stop" in handler_calls, True)
    check("and never reads a name main() binds locally",
          "owns_installation" in handler_names, False)


def case_abbreviated_dump_options(tmp: Path) -> None:
    """Every spelling of the preview option resolves to one role, and one role only (#67).

    `main()` built its parser with argparse's default `allow_abbrev=True`, so `--du
    preview.png` and `--d=preview.png` parsed as `--dump` — while the hand-written
    argv scanner in `dump_role_requested()` recognised neither and returned False.
    Measured: `args.dump == 'preview.png'` with `dump_role_requested(...) == False`. A
    preview started that way therefore took the *production* branch: it acquired the
    main-role lock, used the production control files and ETW session, opened the
    physical panel, and never reached the dump-save/exit branch. Two readers of one
    argv is the defect; both halves of this case exist to keep them one reader.

    Chosen resolution, asserted here rather than described: `allow_abbrev=False`
    **cleanly rejects** `--du`, `--d=` and `--dum` — argparse exits 2 with "unrecognized
    arguments" before any control-plane or hardware work — and both readers agree on
    that. The other option, treating every prefix as a preview, was rejected because it
    would make `--d` mean the diagnostic role and turn an unrecognised option into a
    silently headless run instead of an error a person can read.

    Two halves, because "the readers agree" and "the role is isolated" are different
    claims:

      * a child process imports `main`, parses each spelling with the module's own parser
        (`build_parser`, the same one `main()` uses) and prints what it parsed beside what
        `dump_role_requested()` says. The two must match for every spelling, rejections
        included: a command line that cannot be parsed is not a role, and neither reader
        may call it one;
      * the real entry point is then run for each spelling against a seeded production
        control plane, and whatever it decided, the production files must be
        byte-identical afterwards. For the accepted spellings that means full isolation:
        the preview's own beat file exists, the app's beat was not refreshed, the stop
        request was not consumed, and no owner record was written.
    """
    print("case: abbreviated --dump spellings agree with the role classifier, and stay isolated")
    control = tmp / "abbrev"
    before = seed_production(control)

    # `(label, the whole option as argv words, is this the preview?, what `--dump` the
    # parser reports for it)`. `--dump=` is one word that carries its value; the others
    # pass the value as the next word. The three abbreviations are what the old default
    # accepted as `--dump`; the bare `--dump` is the malformed control, which argparse
    # refuses outright (`rejected(2)`), while the abbreviations parse cleanly and simply
    # leave `--dump` unset — both "not a role", by two different routes.
    spellings = [
        ("--dump", lambda value: ["--dump", value], True, "'x.png'"),
        ("--dump=", lambda value: [f"--dump={value}"], True, "'x.png'"),
        ("--du", lambda value: ["--du", value], False, "None"),
        ("--d=", lambda value: [f"--d={value}"], False, "None"),
        ("--dum", lambda value: ["--dum", value], False, "None"),
    ]
    malformed = ("bare --dump", lambda value: ["--dump"], False, "rejected(2)")

    # The parser, the classifier and the shutdown decision, seen through the real module in
    # a child process, with the same argv shape a start gets:
    #
    #   * one parser — `build_parser()`, the module's only factory — so a spelling cannot
    #     be a preview for `main()` and something else for the shutdown handler;
    #   * `note_deliberate_stop` is the `__main__` Ctrl-C path, called here instead of
    #     delivered as a signal (a console-less child on this platform does not receive one
    #     reliably — see `case_interrupted`);
    #   * every spelling gets its **own** scratch control plane, because the decision under
    #     test writes a marker file and a shared directory would let one spelling's marker
    #     be read as the next spelling's.
    #
    # The probe is a file, not `python -c` one-liner text: a `try:` cannot follow a `;` on
    # one logical line, so the inline form was a `SyntaxError` that printed nothing at all
    # — and a probe with nothing to say is a probe that would silently pass.
    probe = tmp / "dump_role_probe.py"
    probe.write_text(
        "# Written by tools/control_plane_selftest.py: ask the app's own parser and its\n"
        "# own role classifier the same question, then ask the shutdown decision.\n"
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import main as M\n"
        "import app.liveness as L\n"
        "\n"
        "argv = sys.argv[1:]\n"
        "try:\n"
        "    accepted = M.build_parser().parse_known_args(argv)\n"
        "    dump = repr(accepted[0].dump)\n"
        "    unknown = repr(accepted[1])\n"
        "except SystemExit as exc:\n"
        "    dump = unknown = 'rejected(%s)' % exc.code\n"
        "role = M.dump_role_requested(argv)\n"
        "wrote = M.note_deliberate_stop('probe')\n"
        "print('PARSED %s UNKNOWN %s ROLE %s WROTE %s MARKER %s'\n"
        "      % (dump, unknown, role, wrote, L.stopped_path().exists()))\n",
        encoding="utf-8")
    for label, value_argv, want_role, want_dump in [*spellings, malformed]:
        plane = tmp / ("role-" + label.replace(" ", "-").strip("-="))
        r = subprocess.run([sys.executable, str(probe), *value_argv("x.png")],
                           cwd=str(ROOT), env=child_env(plane), capture_output=True,
                           text=True, timeout=120)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("PARSED")), "")
        m = re.match(r"PARSED (.+) UNKNOWN (.+) ROLE (True|False) WROTE (True|False) "
                     r"MARKER (True|False)$", line)
        if m is None:
            # Silence fails the case rather than skipping it: a probe that cannot speak has
            # asserted nothing.
            check(f"{label}: the probe reported a parsed value and a role",
                  f"got {line!r} (rc={r.returncode}) {(r.stderr or '')[-200:]!r}",
                  "PARSED <value> UNKNOWN <list> ROLE <bool> WROTE <bool> MARKER <bool>")
            continue
        parsed, role = m.group(1), m.group(3) == "True"
        wrote, marker = m.group(4) == "True", m.group(5) == "True"
        # The finding, as a comparison: the value the parser resolved and the role the
        # shutdown path derives have to be the same reading of one command line. `--du`
        # used to be `'x.png'` here (argparse's abbreviation) against `False` (the raw
        # scanner), which is a process that runs the production branch and never saves a
        # frame.
        check(f"{label}: the parse and dump_role_requested agree", role, want_role)
        check(f"{label}: the parse resolved exactly the value it was given, or refused it",
              parsed, want_dump)
        # The role answer is meaningful in both directions: an accepted preview never
        # writes the production marker, and a spelling that is not the preview is the
        # owner's role — which is the branch that does. Asserting that the *owner* branch
        # is really taken for a rejected spelling is what pins the conservative direction
        # (both readings say "not the preview") instead of merely asserting a marker is
        # absent for reasons the test cannot see.
        if want_role:
            check(f"{label}: a preview writes no deliberate-stop marker", marker, False)
            check(f"{label}: and its Ctrl-C decision is not the owner's", wrote, False)
        else:
            check(f"{label}: not-the-preview follows the owner's shutdown branch",
                  wrote, True)
            check(f"{label}: and the marker landed in this spelling's own plane",
                  marker, True)

    # The real entry point, one process per spelling, and each one saves into this scratch
    # directory: an absolute `--dump` value, so the assertions below are about the file
    # that process wrote and a preview can never drop a frame into the working tree. The
    # accepted spellings need `--frames 2` to reach the save-and-exit branch; the rejected
    # ones exit at argv parsing.
    for label, value_argv, want_role, _want_dump in spellings:
        label_clean = label.strip("-=") or "dump"
        out = (tmp / f"abbrev-{label_clean}.png").resolve()
        r = subprocess.run([sys.executable, str(MAIN), *value_argv(str(out)),
                            "--backend", "demo", "--frames", "2"], cwd=str(ROOT),
                           env=child_env(control), capture_output=True, text=True,
                           timeout=180.0)
        check(f"{label}: the entry point agrees with the classifier", r.returncode == 0,
              want_role)
        check(f"{label}: a preview renders its frame", out.exists(), want_role)
        if not want_role:
            continue
        untouched(control, before, f"{label} preview")
        check(f"{label}: the preview beats under its own name",
              (control / ".heartbeat-dump").exists(), True)
        (control / ".heartbeat-dump").unlink(missing_ok=True)   # the next spelling's turn

    # And the same for the malformed spelling, which the entry point refuses before it
    # reaches the control plane: nothing is created, and nothing is touched.
    out = (tmp / "abbrev-bare.png").resolve()
    r = subprocess.run([sys.executable, str(MAIN), *malformed[1](str(out)),
                        "--backend", "demo", "--frames", "2"], cwd=str(ROOT),
                       env=child_env(control), capture_output=True, text=True, timeout=180.0)
    check(f"{malformed[0]}: the entry point refuses it", r.returncode != 0, True)
    check(f"{malformed[0]}: and saves nothing", out.exists(), False)
    untouched(control, before, f"{malformed[0]} run")


def _enclosing(tree: ast.AST, call: ast.Call) -> ast.AST:
    """The innermost function definition containing `call` (or the module itself).

    Needed because two of the gates are *early returns* at the top of a function rather
    than an `if` around the call, and "is this call gated?" then becomes a question about
    the function it sits in, not about its lexical neighbours.
    """
    best: ast.AST = tree
    best_line = -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= call.lineno and node.lineno > best_line:
                best, best_line = node, node.lineno
    return best


def _mark_stopped_is_role_gated(node: ast.AST) -> bool:
    """Is `mark_stopped` reachable only under an early return for the diagnostic role?

    Two shapes are legitimate and both appear in the app: the handler form
    (`if not dump_role_requested(): mark_stopped(...)`) and the function form, where the
    function returns early for the diagnostic role and calls `mark_stopped` after it.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.If):
            continue
        calls = [n.func.id for n in ast.walk(sub)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        names = {n.id for n in ast.walk(sub) if isinstance(n, ast.Name)}
        if "mark_stopped" in calls and "dump_role_requested" in names:
            return True
    # The early-return form: `if dump_role_requested(): return False` then a bare
    # `return bool(mark_stopped(...))`, so `mark_stopped` is not lexically inside an `If`.
    for sub in ast.walk(node):
        if isinstance(sub, ast.If) and _is_early_return_for_dump(sub):
            after = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "mark_stopped" and n.lineno > sub.lineno]
            if after:
                return True
    return False


def _is_early_return_for_dump(node: ast.If) -> bool:
    """`if dump_role_requested(): return …` — the diagnostic role leaves first."""
    names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
    if "dump_role_requested" not in names:
        return False
    return any(isinstance(s, ast.Return) for s in node.body)


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
        case_abbreviated_dump_options(tmp)
        print()
        case_main_gates_every_shared_write()
    print("\n" + ("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
