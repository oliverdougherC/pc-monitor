"""Prove the app can survive a bad morning — start-up, death, and hang — offline.

    .venv\\Scripts\\python tools\\recovery_selftest.py

Issue #23 is a stack of failures that all ended the same way: the panel stopped and
nothing said so. Each one had a different shape, so each gets its own proof here:

  bootstrap log   `log.log` is written by the *vendored* logger, and `app_log` swallows
                  every failure of it. A clean clone has no vendored checkout at all, so
                  the app's only diagnostic path was a no-op on exactly the machine where
                  a start-up failure needed explaining. boot.log is the stdlib-only
                  fallback, and it is bounded, because a log that fills the disk is its
                  own outage.

  start-up        the guard and the thread hook used to be installed *after* the config
                  read, the sensor hub, the ETW child, the panel link and the first
                  sensor sample. A transient "not ready yet" at logon therefore ended the
                  process before the machinery that was meant to contain it existed.

  the first sample the initial `hub.tick()` was the one unguarded call between "started"
                  and "looping", and it is where a machine still coming up says so.

  progress        the `[beat]` line is written by the loop whose life it proves, so a
                  hung loop is invisible from inside. `.heartbeat` is written for a
                  reader that is not this process, and `app/liveness.py` turns it into a
                  decision: ok / hold / backoff / permanent / restart / start.

  the budget      bounded restarts with doubling backoff, and a verdict that *stops*
                  trying. A crash-looping app does not need a faster relaunch.

  deliberate stop recovery that undoes a shutdown the user asked for is not recovery.

The Task Scheduler half is checked the way the issue says to check it — by reading what
the installer registers, not by running Task Scheduler — and one of those checks is that
the watchdog must not kill anything: it stops and starts *the task*, by name.
"""
import ast
import inspect
import os
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

import fault_selftest                          # noqa: E402 - reuse the guard walker
import main                                    # noqa: E402
from app import bootlog, liveness              # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def _main_fn() -> ast.FunctionDef:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("no main() in main.py")


def _fn(mod, name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(mod))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no {name}() in {mod.__name__}")


# ------------------------------------------------------------- bootstrap log
def case_bootlog_is_the_path_that_works_without_the_vendor() -> None:
    print("case: a start-up line survives a machine with no vendored logger")
    if not (hasattr(main, "vendor_logger_ok") and hasattr(main, "_boot_phase")):
        # Named rather than crashed: a missing bootstrap path is the bug, and the
        # cases after it still deserve to run.
        check("main has a bootstrap fallback beside app_log()", False, True)
        return
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "boot.log"
        real_path, real_log, real_phase = bootlog.PATH, main._log_path_ok, main._boot_phase
        bootlog.PATH = p
        try:
            main._log_path_ok = False          # the vendored logger cannot write here
            main._boot_phase = False
            main.status("[start] pid=1 backend=Fallback")
            check("the line landed somewhere readable", bootlog.PATH == p, True)
            check("and it is the start-up story", "backend=Fallback" in bootlog.text(p), True)
            check("with a timestamp", bootlog.text(p)[:4] == str(time.localtime().tm_year), True)
            check("and it reports success", bootlog.note("second", p), True)
            check("lines are appended, not replaced", len(bootlog.tail(99, p)), 2)

            # Once log.log works and start-up is over, the mirror must stop: boot.log is
            # a fallback, not a second copy of everything.
            main._log_path_ok = True
            main.status("[beat] up=1m00s idle")
            check("a working log.log is not duplicated into the mirror",
                  "up=1m00s" in bootlog.text(p), False)
            main._boot_phase = True
            main.status("[start] still starting")
            check("but the start-up phase mirrors even then", "still starting" in bootlog.text(p), True)

            # Bounded: a bootstrap log that eats the disk is a second outage.
            p.write_text(("x" * 4096 + "\n") * (bootlog.MAX_BYTES // 4097 + 2),
                         encoding="utf-8")
            bootlog.note("after rotation", p)
            check("the file stays bounded", p.stat().st_size <= bootlog.MAX_BYTES + 4096, True)
            check("and the newest line survived", "after rotation" in bootlog.text(p), True)

            # Never raises, and says whether it landed.
            check("an unwritable target is a False, not an exception",
                  bootlog.note("x", Path(td) / "no-such-dir" / "boot.log"), False)
        finally:
            bootlog.PATH, main._log_path_ok, main._boot_phase = real_path, real_log, real_phase


# ------------------------------------------------------------------- liveness
def case_beat_file(tmp: Path) -> None:
    print("case: the heartbeat is readable, atomic-shaped, and unshockable")
    b = tmp / ".heartbeat"
    check("nothing has beaten yet", liveness.read_beat(b), None)
    check("age of no beat is None (not 0, not a crash)", liveness.age(None, b), None)
    check("a beat lands", liveness.beat(now=1000.0, tick=42, state="game",
                                        path=b), True)
    got = liveness.read_beat(b)
    check("it says who and what", (got["pid"] == os.getpid(), got["tick"], got["state"]),
          (True, 42, "game"))
    check("and how old it is", liveness.age(1060.0, b), 60.0)
    check("no half-written file is left behind", (tmp / ".heartbeat.tmp").exists(), False)
    # A beat caught mid-write, a file full of junk, an empty file: all must read as
    # "no beat", because the alternative is an observer that throws at 4 a.m.
    for junk in ("1234 56", "not a beat at all", "", "nan x 1 -"):
        b.write_text(junk, encoding="utf-8")
        check(f"unreadable beat {junk!r} is None", liveness.read_beat(b), None)


def case_decide_matrix(tmp: Path) -> None:
    print("case: the observer's decision, every branch")
    paths = {"beat": tmp / "d.beat", "stopped": tmp / "d.stopped",
             "restarts": tmp / "d.restarts"}
    now = 1_000_000.0

    def decide(n: float = now, record: bool = True) -> str:
        return liveness.decide(now=n, record=record, paths=paths)[0]

    liveness.beat(now=now - 30.0, tick=7, state="idle", path=paths["beat"])
    check("a fresh beat is nothing to do", decide(), "ok")

    liveness.mark_stopped("keyboard-interrupt", path=paths["stopped"])
    check("a deliberate shutdown is not undone", decide(), "hold")
    paths["stopped"].unlink()

    liveness.beat(now=now - liveness.STALL_S - 5, tick=8, state="idle",
                  path=paths["beat"])
    check("stale + this live process is a hang", decide(), "restart")
    check("and the attempt was recorded", len(liveness._read_restarts(paths["restarts"])), 1)

    # A dead pid is a different sentence: nothing is wedged, it simply is not running.
    liveness.beat(now=now - liveness.STALL_S - 5, tick=9, state="idle", path=paths["beat"])
    b = liveness.read_beat(paths["beat"])
    paths["beat"].write_text(f"{now - liveness.STALL_S - 5:.3f} 2147483646 9 idle\n",
                             encoding="utf-8")
    paths["restarts"].unlink()          # a fresh budget: this branch is about the verdict
    check("a pid that is gone is a start, not a restart", decide(), "start")
    check("dead pid 2147483646 is reported dead", liveness.pid_alive(2147483646), False)
    check("and this one alive", liveness.pid_alive(b["pid"]), True)
    check("pid 0 is not alive", liveness.pid_alive(0), False)

    # Backoff: the second attempt has to wait longer than the first.
    verdict = decide(n=now + 10.0)
    check("the next attempt backs off instead of hammering", verdict, "backoff")
    check("backoff is not yet the end", liveness.decide(now=now + 10.0, record=False,
                                                        paths=paths)[1].startswith("beat"),
          True)
    later = now + liveness.BACKOFF_S * (2 ** 1) + 1
    check("after the backoff it tries again", decide(n=later), "start")

    # Budget spent: the verdict that refuses is the one that stops a relaunch loop.
    for i in range(liveness.BUDGET):
        liveness._record_restart(now + i, paths["restarts"])
    check("budget full", len(liveness._read_restarts(paths["restarts"])), liveness.BUDGET)
    check("and the observer refuses to keep trying", decide(n=now + 900.0), "permanent")
    check("the refusal says what to do about it",
          "boot.log" in liveness.decide(now=now + 900.0, record=False, paths=paths)[1], True)
    # The window rolls over, so a machine that was merely unlucky gets tried again.
    much_later = now + liveness.WINDOW_S + 10
    check("the budget renews with the window", decide(n=much_later), "start")

    # A stop marker older than the last beat means the app came back after the stop:
    # holding then would silence the observer forever.
    liveness.mark_stopped("keyboard-interrupt", path=paths["stopped"])
    paths["stopped"].write_text(f"{much_later - 5000:.3f} keyboard-interrupt\n",
                                encoding="utf-8")
    liveness.beat(now=much_later - 10.0, tick=1, state="idle", path=paths["beat"])
    check("a beat newer than the stop marker wins", decide(n=much_later), "ok")


def case_pid_check_asks_only_to_look() -> None:
    print("case: the liveness check cannot kill what it is checking")
    src = _fn(liveness, "pid_alive")
    kills = [n for n in ast.walk(src) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "os" and n.func.attr == "kill"]
    in_handler = []
    for node in ast.walk(src):
        if isinstance(node, ast.Try):
            for h in node.handlers:
                in_handler += [n for n in ast.walk(h) if isinstance(n, ast.Call)]
    check("os.kill is not used on the Windows path",
          all(k in in_handler for k in kills), True)
    body = ast.get_source_segment((ROOT / "app" / "liveness.py").read_text(encoding="utf-8"),
                                  src) or ""
    check("it asks for query rights", "_QUERY_LIMITED" in body, True)
    # The prose says "terminate" (it has to, to explain itself); the constant must not
    # appear, because that is the right that would make a liveness check a kill.
    check("and never asks for terminate rights", "PROCESS_TERMINATE" in body, False)


# ------------------------------------------------------------------ main.py
def case_containment_is_installed_first() -> None:
    print("case: the guard exists before the things that can fail")
    fn = _main_fn()
    guarded = fault_selftest._guarded_ids(fn)

    def first(name: str):
        """The first node that uses `name`: a call, or the bare name where it is handed
        to the guard as a callable (`g.run("steam-init", SteamIdentity, None)`)."""
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Name) and f.id == name) \
                        or getattr(f, "attr", "") == name:
                    return node
            if isinstance(node, ast.Name) and node.id == name:
                return node
        return None

    net, guard = first("_thread_safety_net"), first("Guard")
    check("the thread hook is installed in main()", net is not None, True)
    check("the guard is constructed in main()", guard is not None, True)
    for risky in ("make_hub", "PanelLink", "FrameMonitor", "SteamIdentity", "_prime"):
        call = first(risky)
        check(f"{risky}() is reached through the guard",
              call is not None and id(call) in guarded, True)
    check("containment is installed before the first outside-world call",
          net is not None and first("make_hub") is not None
          and net.lineno < first("make_hub").lineno, True)
    check("... and before the panel link",
          net is not None and first("PanelLink") is not None
          and net.lineno < first("PanelLink").lineno, True)
    check("... and before the first sensor sample",
          net is not None and first("_prime") is not None
          and net.lineno < first("_prime").lineno, True)

    # A bad config is not a transient fault: it is reported and the process stops,
    # instead of being retried into a relaunch loop.
    src = ast.get_source_segment((ROOT / "main.py").read_text(encoding="utf-8"), fn) or ""
    load = first("load")
    check("the config read is deliberately outside the guard", id(load) in guarded, False)


def case_degraded_tick_still_serves_the_panel() -> None:
    print("case: with no sensors, the panel still obeys the dark rules")
    loop = fault_selftest._loop()
    branch = None
    for node in loop.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "snap":
            branch = node
    check("the tick has a no-snapshot path", branch is not None, True)
    if branch is None:
        return
    names = set()
    for n in ast.walk(branch):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
            names.add(f"{n.func.value.id}.{n.func.attr}")
        elif isinstance(n.func, ast.Name):
            names.add(n.func.id)
    check("it still makes the one light decision", "lights.tick" in names or
          any("lights" in x for x in names), True)
    check("it still turns the panel off when it should be off",
          any("screen" in x for x in names), True)
    check("and it still tells the outside observer it is alive",
          any("beat" in x for x in names), True)
    check("it also retries the link it could not build",
          any("panel" in x for x in names), True)
    check("and the tick does not fall through into the telemetry path",
          any(isinstance(n, ast.Continue) for n in ast.walk(branch)), True)


def case_deliberate_shutdown_is_marked() -> None:
    print("case: a shutdown the user asked for is recorded as such")
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    tried = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    ki = [h for t in tried for h in t.handlers
          if isinstance(h.type, ast.Name) and h.type.id == "KeyboardInterrupt"]
    check("there is a Ctrl-C handler", len(ki) >= 1, True)
    marked = any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "mark_stopped"
                 for h in ki for n in ast.walk(h))
    check("it marks the stop as deliberate", marked, True)
    check("... and atexit does not (a crash unwinds through atexit too)",
          any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "mark_stopped"
              for n in ast.walk(_main_fn())), False)
    fatal = [h for t in tried for h in t.handlers
             if h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "BaseException")]
    check("a fatal start-up failure is written to boot.log as well as log.log",
          any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "note"
              for h in fatal for n in ast.walk(h)), True)


# ------------------------------------------------------------- task settings
def case_the_installer_registers_bounded_recovery() -> None:
    print("case: what Task Scheduler is actually told to do")
    ps = (ROOT / "tools" / "install_autostart.ps1").read_text(encoding="utf-8")
    wd = (ROOT / "tools" / "watchdog_autostart.ps1").read_text(encoding="utf-8")
    check("restart-on-failure is configured", "-RestartCount" in ps, True)
    check("bounded, not unlimited", "$RestartCount = 0" in ps, False)
    count = [ln.strip() for ln in ps.splitlines() if ln.strip().startswith("$RestartCount =")]
    check("and small enough to count", len(count) == 1 and 0 < int(count[0].split("=")[1]) <= 10,
          True)
    check("with an interval between attempts", "-RestartInterval" in ps, True)
    check("the unlimited execution time is kept (this task runs for weeks)",
          "-ExecutionTimeLimit 0" in ps, True)
    check("one instance is still enforced (one app, one ETW collector)",
          "-MultipleInstances IgnoreNew" in ps, True)
    check("the observer task is registered", "PCMonitorWatchdog" in ps, True)
    wd_unreg = "Unregister-ScheduledTask -TaskName $WatchdogName"
    app_unreg = "Unregister-ScheduledTask -TaskName $TaskName"
    check("removal unregisters the observer too", wd_unreg in ps, True)
    # `.index` on a string that is not there raises, and a raise here would hide every
    # check after it — so the ordering is only asked when both halves exist.
    check("and does so *before* the app, or removal restarts itself",
          wd_unreg in ps and app_unreg in ps and ps.index(wd_unreg) < ps.index(app_unreg),
          True)
    check("the effective settings are read back, not echoed",
          "(Get-ScheduledTask -TaskName $TaskName).Settings" in ps, True)
    # The observer must not become a second offender against unrelated processes.
    for banned in ("Stop-Process", "Get-CimInstance", "Win32_Process", "taskkill"):
        check(f"the watchdog never uses {banned}", banned.lower() in wd.lower(), False)
    check("it stops and starts the task by name instead",
          "Stop-ScheduledTask -TaskName $TaskName" in wd
          and "Start-ScheduledTask -TaskName $TaskName" in wd, True)
    check("it asks the Python policy rather than deciding in PowerShell",
          "-m app.liveness decide" in wd, True)
    check("and it writes down the verdicts it acts on", "Add-Content -Path $Log" in wd, True)


def main_run() -> int:
    case_bootlog_is_the_path_that_works_without_the_vendor()
    print()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        case_beat_file(tmp)
        print()
        case_decide_matrix(tmp)
        print()
    case_pid_check_asks_only_to_look()
    print()
    case_containment_is_installed_first()
    print()
    case_degraded_tick_still_serves_the_panel()
    print()
    case_deliberate_shutdown_is_marked()
    print()
    case_the_installer_registers_bounded_recovery()
    print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())
