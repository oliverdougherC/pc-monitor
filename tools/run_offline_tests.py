r"""One command for the whole offline gate: every selftest, one exit code.

    .venv\Scripts\python tools\run_offline_tests.py
    .venv\Scripts\python tools\run_offline_tests.py --list
    .venv\Scripts\python tools\run_offline_tests.py --only panel_link

The selftests are the contract — "everything behavioural has an offline proof
that needs neither admin nor hardware" — but nine separate commands are nine
chances to forget one, and a change cannot be judged by whoever remembers to
run them. This runner is what CI calls and what a contributor runs before
pushing: each selftest stays a separate process (its own `sys.path`, its own
fault guard), and this only aggregates their exit codes.

Three facts decide the shape:

* Some selftests reach into the vendored panel library (`library.lcd.*`) or the
  theme's fonts. A fresh clone has neither — `vendor/` is a pin plus a
  provenance note, not a checkout (see `vendor/README.md` and
  `tools/vendor_lock.ps1`) — so those cases report SKIP *with the reason*
  rather than failing a machine that has never held the library. SKIP is never
  counted as coverage: the summary prints how many ran and how many skipped,
  and CI prints the same numbers in the job summary.
* Nothing here needs admin, hardware, or a network. A selftest that starts to
  need one is a bug in the selftest, not a reason to mark it manual.
* A wedged case must not wedge the gate. Each case runs under a wall-clock
  cap; blowing it is a FAIL that names the case and the timeout, and its
  child is killed and reaped. Without the cap a hung selftest strands the
  whole run — locally forever, in CI on a 20-minute job timeout that names
  nothing — which is not a repeatable gate.

Exit status: 0 if no case FAILED, 1 otherwise. A SKIP alone cannot make this
fail, and must never be described as a pass; neither does an ADVISORY-FAIL,
which is printed with its reason precisely so it is not mistaken for a pass.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "turing-smart-screen-python"

# Wall-clock cap per case, in seconds (override: PCMON_GATE_CASE_TIMEOUT).
# Generous on purpose — the slowest real case is seconds, so blowing this cap
# means wedged, not slow. `gate_selftest` keeps its own tiny cap to stay fast.
CASE_TIMEOUT = float(os.environ.get("PCMON_GATE_CASE_TIMEOUT", "600"))

# (selector, argv, what it proves, what it needs beyond this repo, advisory reason)
#
# `advisory` is for a case that cannot yet be trusted as a gate, and it is never
# silent: an advisory failure prints `ADVISORY-FAIL` with its reason, is counted
# separately in the summary, and stays out of the exit code. The point is a
# green bar that means "no case we trust has failed" — not a green bar that
# quietly ignored one. Every advisory entry names the issue that removes it.
CASES = [
    ("gate", ["tools/gate_selftest.py"],
     "the gate judges honestly: verdict contract, SKIP/advisory, hung-case kill",
     None, None),
    ("frames", ["tools/frames_selftest.py"],
     "present-stream parsing: fps, GPU-busy, held values, stream guards", None, None),
    ("gamewatch", ["tools/gamewatch_selftest.py"],
     "game entry/exit, alt-tab keeps the locked target, video is not a game", None, None),
    ("hoststate", ["tools/hoststate_selftest.py"],
     "sleep / displays-off / lock / frozen-loop fallback in the state machine", None,
     "reads the live GetLastInputInfo clock, so it fails whenever a human is at the "
     "keyboard and passes when nobody is: it gates the desk, not the code. "
     "Pinning that clock is issue #4, which should drop this advisory flag."),
    ("nightlight", ["tools/nightlight_probe.py", "--selftest"],
     "CloudStore night-mode decode, pinned to captured blobs", None, None),
    ("panel_link", ["tools/panel_link_selftest.py"],
     "link survives raise/hang, rebuilds, walks its device ladder", "vendor", None),
    ("lights", ["tools/lights_selftest.py"],
     "the one light decision end to end, incl. a rendered night frame", "vendor", None),
    ("layout", ["tools/layout_check.py"],
     "geometry at the value extremes: collisions and panel overflow", "fonts", None),
    ("fault", ["tools/fault_selftest.py"],
     "per-tick guard contains faults; AST pass over the loop", None, None),
]


def missing(need):
    """Why this case cannot run here, or None when it can."""
    if need == "vendor" and not (VENDOR / "library").is_dir():
        return "vendored library absent (tools/vendor_lock.ps1 -Fetch)"
    if need == "fonts" and not (VENDOR / "res" / "fonts").is_dir():
        return "theme fonts absent (tools/vendor_lock.ps1 -Fetch)"
    return None


def run(case, only):
    selector, argv, proves, need, advisory = case
    if only and selector != only:
        return None
    reason = missing(need)
    if reason:
        print(f"SKIP           {selector:<11} {proves}  — {reason}")
        return "skip"
    print(f"----           {selector}: {' '.join(argv)}")
    sys.stdout.flush()
    try:
        proc = subprocess.run([sys.executable, *argv], cwd=str(ROOT),
                              capture_output=True, text=True, errors="replace",
                              timeout=CASE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child and waited on it; what
        # it cannot do is say *which* case died. This line is that, and it is
        # a FAIL — a hung case is a broken case, not a skipped one. (Only the
        # direct child is ours to kill; process-tree cleanup is #12's job
        # object, not this runner's.)
        print(f"FAIL           {selector:<11} {proves}  "
              f"(TIMEOUT after {CASE_TIMEOUT:g}s; child killed)")
        sys.stdout.flush()
        return "fail"
    out = (proc.stdout or "") + (proc.stderr or "")
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    sys.stdout.flush()
    if proc.returncode == 0 and "SELFTEST PASSED" in out:
        print(f"PASS           {selector:<11} {proves}  (exit 0)")
        sys.stdout.flush()
        return "pass"
    if proc.returncode == 0:
        # Exit 0 is not a verdict. A case that stops early - SystemExit from an
        # import, an empty main, a vendored module that calls sys.exit(0) when its
        # own optional dependency is missing - is otherwise indistinguishable from
        # one that ran every check it has. The house verdict line is the contract.
        print(f"FAIL           {selector:<11} {proves}  (exit 0 with no SELFTEST PASSED line)")
        sys.stdout.flush()
        return "fail"
    if advisory:
        print(f"ADVISORY-FAIL  {selector:<11} {proves}  (exit {proc.returncode})")
        print(f"               not gating: {advisory}")
        sys.stdout.flush()
        return "advisory"
    print(f"FAIL           {selector:<11} {proves}  (exit {proc.returncode})")
    sys.stdout.flush()
    return "fail"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="run a single case by selector")
    ap.add_argument("--list", action="store_true", help="list the cases and exit")
    args = ap.parse_args()

    if args.list:
        for selector, _, proves, need, advisory in CASES:
            tag = f"[needs {need}]" if need else ""
            tag += " [advisory]" if advisory else ""
            print(f"{selector:<11} {tag:<26} {proves}")
        return 0

    print(f"offline gate — {sys.executable}\ncwd: {ROOT}\n")
    results = {}
    advisories = {}
    for case in CASES:
        verdict = run(case, args.only)
        if verdict:
            results[case[0]] = verdict
            if verdict == "advisory":
                advisories[case[0]] = case[4]

    ran = sum(1 for v in results.values() if v == "pass")
    failed = [k for k, v in results.items() if v == "fail"]
    skipped = [k for k, v in results.items() if v == "skip"]
    warned = [k for k, v in results.items() if v == "advisory"]
    print("\n" + "=" * 62)
    print(f"passed {ran}   failed {len(failed)}   advisory-fail {len(warned)}   "
          f"skipped {len(skipped)}")
    if failed:
        print("FAILED: " + ", ".join(failed))
    if warned:
        print("ADVISORY (failing, deliberately not gating — see the reasons above): "
              + ", ".join(warned))
    if skipped:
        # A skipped case is an unverified claim, not a passing one. Say so in
        # the CI job summary too, where a reviewer reads it.
        print("SKIPPED (not covered here): " + ", ".join(skipped))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Offline gate\n\n"
                     f"* passed **{ran}**, failed **{len(failed)}**, "
                     f"advisory-fail **{len(warned)}**, skipped **{len(skipped)}**\n")
            if failed:
                fh.write(f"* failed: {', '.join(failed)}\n")
            for name in warned:
                fh.write(f"* **{name}** fails but is not gating: {advisories[name]}\n")
            if skipped:
                fh.write("  * skipped (vendored library / theme fonts absent, so "
                         "*not* covered by this run): " + ", ".join(skipped) + "\n")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())