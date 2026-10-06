r"""One command for the whole offline gate: every selftest, one exit code.

    .venv\Scripts\python tools\run_offline_tests.py
    .venv\Scripts\python tools\run_offline_tests.py --list
    .venv\Scripts\python tools\run_offline_tests.py --only panel_link
    .venv\Scripts\python tools\run_offline_tests.py --strict

The selftests are the contract — "everything behavioural has an offline proof
that needs neither admin nor hardware" — but a command per selftest is a chance
to forget one, and a change cannot be judged by whoever remembers to run them.
This runner is what CI calls and what a contributor runs before pushing: each
selftest stays a separate process (its own `sys.path`, its own fault guard), and
this only aggregates their exit codes.

Five facts decide the shape:

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
* A selector is an identity, not a label. Two cases may not share one: the
  results dictionary is keyed by selector, so a duplicate would let a later
  pass silently overwrite an earlier failure. `--only` naming a selector that
  is not in `CASES` is a mistake, not an empty success, and both are refused
  up front (see `_validate`).
* A SKIP can only be a *description* of this machine, never of a release. In
  strict mode — what integration and release CI run — a skipped case that
  needs the vendored tree or theme fonts is a FAIL, because a release gate
  that goes green while `panel_link`, `lights` and `layout` were skipped has
  verified nothing about the panel it is shipping.

Exit status: 0 if no case FAILED, 1 otherwise. A SKIP alone cannot make this
fail unless `--strict` asks it to; neither does an ADVISORY-FAIL, which is
printed with its reason precisely so it is not mistaken for a pass.
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
#
# A selector must be unique across this list; `_validate` refuses a duplicate
# rather than letting dict assignment hide an earlier failure behind a later
# pass. The two integration collisions (issue #47 vs #54 on the recovery
# coordinator versus start-up containment, and issue #31 vs #32 on the preview's
# honest fps versus its transactional reload) are kept as *four* distinct
# selectors and four distinct files, because they prove four different things:
#
#   recovery, recovery_startup, liveview, liveview_reload
#
CASES = [
    # -------- the gate itself and the loop's own guards --------
    ("gate", ["tools/gate_selftest.py"],
     "the gate judges honestly: verdict contract, SKIP/advisory, hung-case kill",
     None, None),
    ("fault", ["tools/fault_selftest.py"],
     "per-tick guard contains faults; AST pass over the loop", None, None),

    # -------- host state and Windows events (#1, #2, #3, #4, #19) --------
    ("hoststate", ["tools/hoststate_selftest.py"],
     "sleep / displays-off / lock / frozen-loop fallback in the state machine",
     None, None),
    ("eventwindow", ["tools/eventwindow_selftest.py"],
     "event-window ABI: pointer-sized handles, exactly-once unregister, pump recovery",
     None, None),
    ("wake_gap", ["tools/wake_gap_selftest.py"],
     "the wake-gap rule on main.py's own dt: one recovery per freeze, none per slow rebuild",
     None, None),
    ("idle_clock", ["tools/idle_clock_selftest.py"],
     "idle arithmetic across the signed and 32-bit tick boundaries, on a scripted clock",
     None, None),

    # -------- recovery coordination and start-up containment (#5, #23) --------
    ("recovery", ["tools/recovery_selftest.py"],
     "recovery asks instead of blocking: one pass per burst, no lit frame mid-rebuild, "
     "the loop responsive while a slow handshake and a slow device reset run",
     None, None),
    ("recovery_startup", ["tools/recovery_startup_selftest.py"],
     "start-up containment, bootstrap log, heartbeat, restart budget, task settings",
     None, None),

    # -------- panel link: one owner, journal, verified reset, acked cache --------
    ("panel_link", ["tools/panel_link_selftest.py"],
     "link survives raise/hang, rebuilds, walks its device ladder", "vendor", None),
    ("panel_owner", ["tools/panel_owner_selftest.py"],
     "one owner for the port: late writes, competing rebuilds, close mid-build", None, None),
    ("open_serial", ["tools/open_serial_hardening_selftest.py"],
     "the vendor's openSerial gives up by raising, never os._exit: ending the "
     "process is the app's call, not the driver's", "vendor", None),
    ("panel_recovery", ["tools/panel_recovery_selftest.py"],
     "USB disable journal: written first, kept through crashes, cleared by device state",
     None, None),
    ("usb_reset", ["tools/panel_usb_reset_selftest.py"],
     "only the verified panel instance may be reset; ambiguity refuses", "vendor", None),
    ("diff_cache", ["tools/diff_cache_selftest.py"],
     "diff cache commits only acknowledged pushes on one generation", "vendor", None),

    # -------- appearance: night, lights, sweep --------
    ("nightlight", ["tools/nightlight_probe.py", "--selftest"],
     "CloudStore night-mode decode, pinned to captured blobs", None, None),
    ("gamma_ramp", ["tools/gamma_ramp_selftest.py"],
     "gamma-ramp read: right DLL, declared handles, reasons not fake neutrals", None, None),
    ("lights", ["tools/lights_selftest.py"],
     "the one light decision end to end, incl. a rendered night frame", "vendor", None),
    ("sweep", ["tools/sweep_selftest.py"],
     "burn-in sweep: one frame per tick, outranked by light/game/link", None, None),

    # -------- capture: parse, isolate, recover, shut down --------
    ("frames", ["tools/frames_selftest.py"],
     "present-stream parsing: fps, GPU-busy, held values, stream guards", None, None),
    ("frames_rows", ["tools/frames_rows_selftest.py"],
     "malformed-row isolation, bounded diagnostics, bounded child reap after reader death",
     None, None),
    ("frames_recovery", ["tools/frames_recovery_selftest.py"],
     "stalled-child recovery: deadlines, states, idle desktop, per-generation counters",
     None, None),
    ("frames_shutdown", ["tools/frames_shutdown_selftest.py"],
     "job adoption really happens (native), and close() cannot lose a child to a racing spawn", None, None),

    # -------- sensors, power, units, game identity --------
    ("sensors", ["tools/sensors_selftest.py"],
     "supervisor: bounded ticks, held grace, blanking, reopen with backoff, "
     "NVML re-acquire", None, None),
    ("sensor_counters", ["tools/sensor_counters_selftest.py"],
     "disk/net counters re-prime after gaps, resets, topology change, suspend",
     None, None),
    ("power_estimate", ["tools/power_estimate_selftest.py"],
     "power totals carry provenance; unknown telemetry is never a low measured total",
     None, None),
    ("units", ["tools/units_selftest.py"],
     "counter to pixels: network stays in bits, disk stays in bytes", "fonts", None),
    ("gamewatch", ["tools/gamewatch_selftest.py"],
     "game entry/exit, alt-tab keeps the locked target, video is not a game", None, None),
    ("steamid", ["tools/steamid_selftest.py"],
     "Steam identity read from psutil-shaped environments, incl. a live child", None, None),

    # -------- configuration, preview, layout --------
    ("config_schema", ["tools/config_schema_selftest.py"],
     "config knobs reach their consumers; bad values die at load naming the key",
     None, None),
    ("liveview", ["tools/liveview_selftest.py"],
     "preview shows missing fps as missing; invented fps is marked on the image",
     None, None),
    ("liveview_reload", ["tools/liveview_reload_selftest.py"],
     "preview hot reload is transactional: bad edits keep the working generation",
     None, None),
    ("layout", ["tools/layout_check.py"],
     "geometry at the value extremes: collisions and panel overflow", "fonts", None),

    # -------- ownership and installation (#24, #25, #26) --------
    ("instance", ["tools/instance_selftest.py"],
     "one main role: lock claim, crash-release, order before panel and capture", None, None),
    ("owned", ["tools/owned_process_selftest.py"],
     "install/remove stops this app and its collector, and only those", None, None),
    ("env_check", ["tools/env_check_selftest.py"],
     "the vendor pin verifies instead of agreeing with whatever it finds", None, None),
    ("control_plane", ["tools/control_plane_selftest.py"],
     "one control plane per role: a real --dump child leaves the app's owner record, "
     "stop request, heartbeat and deliberate-stop marker byte-identical (#67)", None, None),

    # -------- the combined control loop: every contract at once --------
    ("e2e_loop", ["tools/e2e_control_loop_selftest.py"],
     "the wired loop: 120 fault cycles, late completions, dark intent, staleness, "
     "the monitor-sleep wedge, and a clean shutdown - the combined contracts, "
     "not one module's", None, None),
]


def _validate(cases) -> None:
    """Refuse a gate that cannot report honestly.

    Two mistakes are silent today and both must be loud:

    * a repeated selector — `results` is keyed by selector, so the second case
      would overwrite the first, and a FAIL would be erased by a later PASS.
    * a repeated argv — two selectors running the same file is almost always a
      mis-registration, and it hides the case somebody meant to add.
    """
    seen: dict[str, str] = {}
    for selector, argv, _proves, _need, _advisory in cases:
        if selector in seen:
            raise SystemExit(
                f"gate registry error: selector {selector!r} is registered twice "
                f"({seen[selector]} and {' '.join(argv)}). Each case needs a distinct "
                f"selector, or a failure can be overwritten by a later pass.")
        seen[selector] = " ".join(argv)
    by_argv: dict[str, str] = {}
    for selector, argv, _proves, _need, _advisory in cases:
        key = " ".join(argv)
        if key in by_argv:
            raise SystemExit(
                f"gate registry error: {key!r} is registered under both "
                f"{by_argv[key]!r} and {selector!r}.")
        by_argv[key] = selector


def missing(need):
    """Why this case cannot run here, or None when it can."""
    if need == "vendor" and not (VENDOR / "library").is_dir():
        return "vendored library absent (tools/vendor_lock.ps1 -Fetch)"
    if need == "fonts" and not (VENDOR / "res" / "fonts").is_dir():
        return "theme fonts absent (tools/vendor_lock.ps1 -Fetch)"
    return None


def run(case, only, strict):
    selector, argv, proves, need, advisory = case
    if only and selector != only:
        return None
    reason = missing(need)
    if reason:
        # In strict mode a skipped hardware case is an unverified release, and
        # the whole point of running this in release CI is to refuse that.
        verdict = "fail" if strict else "skip"
        label = "FAIL(missing asset)" if strict else "SKIP"
        print(f"{label:<18} {selector:<21} {proves}  — {reason}")
        if strict:
            print(f"                  strict mode: {selector} is required for a release "
                  f"gate and cannot be skipped")
        sys.stdout.flush()
        return verdict
    print(f"----               {selector}: {' '.join(argv)}")
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
        print(f"FAIL               {selector:<21} {proves}  "
              f"(TIMEOUT after {CASE_TIMEOUT:g}s; child killed)")
        sys.stdout.flush()
        return "fail"
    out = (proc.stdout or "") + (proc.stderr or "")
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    sys.stdout.flush()
    if proc.returncode == 0 and "SELFTEST PASSED" in out:
        print(f"PASS               {selector:<21} {proves}  (exit 0)")
        sys.stdout.flush()
        return "pass"
    if proc.returncode == 0:
        # Exit 0 is not a verdict. A case that stops early - SystemExit from an
        # import, an empty main, a vendored module that calls sys.exit(0) when its
        # own optional dependency is missing - is otherwise indistinguishable from
        # one that ran every check it has. The house verdict line is the contract.
        print(f"FAIL               {selector:<21} {proves}  (exit 0 with no SELFTEST PASSED line)")
        sys.stdout.flush()
        return "fail"
    if advisory:
        print(f"ADVISORY-FAIL      {selector:<21} {proves}  (exit {proc.returncode})")
        print(f"                   not gating: {advisory}")
        sys.stdout.flush()
        return "advisory"
    print(f"FAIL               {selector:<21} {proves}  (exit {proc.returncode})")
    sys.stdout.flush()
    return "fail"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="run a single case by selector")
    ap.add_argument("--list", action="store_true", help="list the cases and exit")
    ap.add_argument("--strict", action="store_true",
                    help="a case that needs the vendored library or theme fonts and "
                         "cannot run is a FAIL, not a SKIP (integration/release CI)")
    args = ap.parse_args()

    _validate(CASES)
    selectors = [c[0] for c in CASES]

    if args.only and args.only not in selectors:
        # Succeeding with zero cases run is the worst possible answer to a
        # typo: it looks exactly like a pass.
        print(f"unknown --only selector {args.only!r}; known selectors are:\n  "
              + ", ".join(selectors))
        return 2

    if args.list:
        for selector, _, proves, need, advisory in CASES:
            tag = f"[needs {need}]" if need else ""
            tag += " [advisory]" if advisory else ""
            print(f"{selector:<21} {tag:<26} {proves}")
        return 0

    if args.strict and not args.only:
        absent = [need for need in ("vendor", "fonts") if missing(need)]
        if absent:
            print(f"strict gate: required assets absent ({', '.join(absent)}); "
                  f"the cases that need them cannot be verified here.\n")

    print(f"offline gate{' (strict)' if args.strict else ''} — {sys.executable}\ncwd: {ROOT}\n")
    results = {}
    advisories = {}
    for case in CASES:
        verdict = run(case, args.only, args.strict)
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
