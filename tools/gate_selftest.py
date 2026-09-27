"""Verify the offline gate itself, without taking the gate's word for anything.

    .venv\\Scripts\\python tools\\gate_selftest.py

`run_offline_tests.py` decides what green means, and until now nothing
verified that decision. That is the same failure mode issue #30 calls out for
the behavioural tests — a check that only reproduces the implementation's
assumptions — promoted one level: if an edit quietly restored "exit 0 means
PASS", or counted a SKIP as coverage, the gate would keep reporting confidence
while covering nothing. So this file drives the real runner module with
*golden* synthetic cases — scripts whose verdict is dictated by the contract
stated in the runner's own docstring, not read back from its code — and pins:

  a PASS needs exit 0 *and* the `SELFTEST PASSED` line; a silent exit 0 is
  FAIL, and the case's output must be echoed so the verdict is auditable;

  a non-zero exit is FAIL unless the case is advisory, which prints
  ADVISORY-FAIL with its reason and keeps the exit code at 0;

  a case whose prerequisite is absent is SKIP, is counted as skipped, is
  named as "not covered" in the job summary, and can never flip the exit;

  a hung case does not hang the gate: the runner kills it at the case
  timeout, names the FAIL TIMEOUT, and leaves no live child behind;

  repeated case batches do not grow this process's threads or handles — the
  runner's spawn/capture/reap discipline must plateau, not drift;

  the x64 native plumbing every ctypes contract in this repo stands on: a
  pointer-sized handle through a *declared* prototype into kernel32, on a
  real x64 Windows interpreter. Not mocked — this case fails off x64
  Windows, because a mock that reports "ABI covered" is exactly what #30
  says must not happen.

The synthetic cases drive `main()` in-process with the runner's CASES list
swapped out; the children are real `python` subprocesses, so spawn, capture,
classify, echo, timeout-kill and reap all run as CI runs them.
"""
import contextlib
import ctypes
import gc
import importlib.util
import io
import os
import platform
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

import psutil                                    # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


# The runner module under test, loaded from its real path. Importing it does
# not run it (it guards on __main__), so this is the same file CI executes.
_spec = importlib.util.spec_from_file_location(
    "run_offline_tests_under_test", ROOT / "tools" / "run_offline_tests.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

# The golden scripts. Their expected verdicts are decided from the contract
# ("exit 0 is not a verdict", "SKIP is never coverage", "advisory is visible"),
# never from what the runner currently does with them.
GOOD = """
print("golden check ran")
print("SELFTEST PASSED")
"""

SILENT = """
# Says nothing, exits 0: the shape the verdict-line contract must reject.
# A runner that calls this PASS is indistinguishable from one that never
# ran the case at all.
"""

FAILING = """
import sys
print("SELFTEST FAILED: 1")
sys.exit(1)
"""

HUNG = """
import os, sys, time
with open(sys.argv[1], "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(60)
print("SELFTEST PASSED")
"""


def script(dir: Path, name: str, body: str) -> str:
    path = dir / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def drive(cases, *, vendor=None, only=None, summary=None, timeout=None):
    """Run the real runner.main() over `cases` with the runner's view of the
    world swapped out. Returns (exit code, everything it printed, elapsed s)."""
    saved_cases, saved_vendor = runner.CASES, runner.VENDOR
    saved_timeout = getattr(runner, "CASE_TIMEOUT", None)
    saved_env = os.environ.get("GITHUB_STEP_SUMMARY")
    saved_argv = list(sys.argv)
    buf = io.StringIO()
    t0 = time.monotonic()
    try:
        runner.CASES = cases
        if vendor is not None:
            runner.VENDOR = vendor
        if timeout is not None:
            runner.CASE_TIMEOUT = timeout
        if summary is not None:
            os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        else:
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
        sys.argv = ["run_offline_tests.py"] + (["--only", only] if only else [])
        with contextlib.redirect_stdout(buf):
            code = runner.main()
    finally:
        runner.CASES, runner.VENDOR = saved_cases, saved_vendor
        if saved_timeout is None:
            try:
                del runner.CASE_TIMEOUT
            except AttributeError:
                pass
        else:
            runner.CASE_TIMEOUT = saved_timeout
        if saved_env is None:
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
        else:
            os.environ["GITHUB_STEP_SUMMARY"] = saved_env
        sys.argv = saved_argv
    return code, buf.getvalue(), time.monotonic() - t0


def case_native() -> None:
    print("case: the x64 native plumbing this repo's ctypes contracts rest on")
    check("x64 interpreter (pointer-sized handles)", ctypes.sizeof(ctypes.c_void_p), 8)
    check("Windows interpreter", sys.platform, "win32")
    check("x64 machine", platform.machine().upper() in ("AMD64", "X86_64"), True)
    if sys.platform != "win32":
        return                        # the checks above already failed; windll is not there
    try:
        k32 = ctypes.windll.kernel32
        # Declared, not guessed: a prototype whose handle is pointer-sized,
        # called for real. This is the ABI shape #2/#36's contracts ride on,
        # exercised here on the x64 runner instead of mocked off it.
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        k32.GetProcessHandleCount.restype = ctypes.c_int
        n = ctypes.c_uint(0)
        ok = k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n))
        check("declared prototype answers through a pointer-sized handle", bool(ok), True)
        check("handle count comes back sane", n.value > 0, True)
    except Exception as exc:            # noqa: BLE001 — the smoke itself must not crash the gate
        check("native smoke ran", repr(exc), "no raise")


def case_classification(good, silent, failing, no_vendor, summary) -> None:
    print("case: the gate judges every verdict shape honestly")
    cases = [
        ("golden-pass", [good], "proves PASS needs the verdict line", None, None),
        ("golden-silent", [silent], "proves silent exit 0 is not a pass", None, None),
        ("golden-fail", [failing], "proves a failed case fails the gate", None, None),
        ("golden-advisory", [failing], "proves advisory stays out of the exit",
         None, "golden advisory reason"),
        ("golden-skip", [good], "proves an absent prerequisite is SKIP", "vendor", None),
    ]
    code, out, _ = drive(cases, vendor=no_vendor, summary=summary)
    check("exit code follows only the gating failures", code, 1)
    check("the passing golden case is PASS",
          re.search(r"^PASS\s+golden-pass", out, re.M) is not None, True)
    check("the case's own output is echoed", "golden check ran" in out, True)
    check("silent exit 0 is FAIL",
          re.search(r"^FAIL\s+golden-silent.*no SELFTEST PASSED", out, re.M) is not None, True)
    check("nonzero exit is FAIL",
          re.search(r"^FAIL\s+golden-fail.*\(exit 1\)", out, re.M) is not None, True)
    check("advisory fails without gating",
          re.search(r"^ADVISORY-FAIL\s+golden-advisory", out, re.M) is not None, True)
    check("advisory names its reason", "not gating: golden advisory reason" in out, True)
    check("absent prerequisite is SKIP",
          re.search(r"^SKIP\s+golden-skip.*vendored library absent", out, re.M) is not None, True)
    check("the tally counts each verdict exactly once",
          re.search(r"passed 1 +failed 2 +advisory-fail 1 +skipped 1", out) is not None, True)
    text = summary.read_text(encoding="utf-8")
    check("the job summary states the same numbers",
          "passed **1**, failed **2**, advisory-fail **1**, skipped **1**" in text, True)
    check("the job summary calls the skip not-covered",
          "not* covered by this run" in text and "golden-skip" in text, True)


def case_hung_child(hung, pidfile) -> None:
    print("case: a hung case is killed and named, not waited on forever")
    cases = [("golden-hung", [hung, str(pidfile)],
              "proves a hung case gets killed, not waited on", None, None)]
    code, out, elapsed = drive(cases, timeout=2.5)
    check("the hung case fails the gate", code, 1)
    check("the hung case is named as a TIMEOUT", "TIMEOUT" in out, True)
    # 2.5s cap + a generous margin for interpreter start: an unbounded wait
    # shows up here as ~60s, which is the whole point of having a cap.
    check("the gate stopped waiting within seconds of the cap", elapsed < 20.0, True)
    pid = int(pidfile.read_text()) if pidfile.exists() else None
    check("the hung child actually ran", pid is not None, True)
    check("the killed child is gone", bool(pid) and not psutil.pid_exists(pid), True)
    check("no live children were left behind", psutil.Process().children(), [])


def case_plateau(good, no_vendor) -> None:
    print("case: repeated batches neither leak children nor drift upward")

    def counts():
        gc.collect()
        p = psutil.Process()
        return p.num_threads(), p.num_handles()

    cases = [
        ("golden-pass", [good], "a full batch", None, None),
        ("golden-skip", [good], "and a skipped one", "vendor", None),
    ]
    before = counts()
    for _ in range(3):
        code, _, _ = drive(cases, vendor=no_vendor)
        check("a batch of pass+skip exits 0", code, 0)
        check("and leaves no child behind", psutil.Process().children(), [])
    after = counts()
    check("threads did not grow across batches", after[0] <= before[0], True)
    check("handles did not grow across batches", after[1] <= before[1] + 16, True)


def case_only(good, failing) -> None:
    print("case: --only runs exactly the case named")
    cases = [
        ("golden-pass", [good], "the one asked for", None, None),
        ("golden-fail", [failing], "the one that must stay in its lane", None, None),
    ]
    code, out, _ = drive(cases, only="golden-pass")
    check("--only exits 0 when the one case passes", code, 0)
    check("--only did not run the other case", "golden-fail" not in out, True)


def main_run() -> int:
    case_native()
    print()
    with tempfile.TemporaryDirectory(prefix="gate-selftest-") as tds:
        td = Path(tds)
        good = script(td, "good.py", GOOD)
        silent = script(td, "silent.py", SILENT)
        failing = script(td, "failing.py", FAILING)
        hung = script(td, "hung.py", HUNG)
        pidfile = td / "hung.pid"
        no_vendor = td / "no-vendor"       # exists, but has no library/ or res/fonts/
        no_vendor.mkdir()
        summary = td / "step_summary.md"

        case_classification(good, silent, failing, no_vendor, summary)
        print()
        case_hung_child(hung, pidfile)
        print()
        case_plateau(good, no_vendor)
        print()
        case_only(good, failing)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())