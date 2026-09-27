"""Prove the installer stops *this app* and nothing else — offline, with fakes.

    .venv\\Scripts\\python tools\\owned_process_selftest.py

`tools/install_autostart.ps1` used to stop the app like this:

    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'main\\.py' } | Stop-Process -Force

Run elevated, that is a force-kill of every Python program on the machine whose command
line contains `main.py` — the most common entry-point filename in Python. Another
project's dev server, a notebook kernel, an editor's language server. The install script
then checked its own work with the same substring, so an unrelated `main.py` could also
make a failed install report success. The collector pass was the same shape: any
`presentmon.exe` whose parent pid was no longer in the live set, and a parent pid is not
ownership — pids are recycled, so that query eventually points at a stranger.

The issue's validation is "no processes stopped during review", so every case here runs
against a **scripted process list**: `classify()` is a pure function over `Proc` rows, and
`apply()` takes its `alive`/`kill`/`sleep` as arguments. Nothing in this file can stop a
process, and one case asserts that the killer receives exactly the pids that were proven
owned — no more, not even one.

The rows below are the acceptance list, one per case: an unrelated project's `main.py`, a
similarly named path, this tree's *other* scripts, a dev-role collector, another
application's PresentMon, a recycled parent pid, an unreadable process, and the genuine
instance with its own child.
"""
import ast
import ctypes
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

from app import owned                                    # noqa: E402
from app.owned import Proc, classify, canon, script_of, under   # noqa: E402

fails: list[str] = []

# A root that is not this checkout, so a mistake in the rules cannot be masked by the
# real tree — and so the "similarly named path" case is a real sibling of it.
R = "C:\\Test\\PC Monitor"
MAIN = R + "\\main.py"
VENV = R + "\\.venv\\Scripts"
COLLECTOR = R + "\\vendor\\presentmon\\presentmon.exe"
SESSION = "PCMonitor-main"


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def plan(procs, rec=None):
    d = classify(procs, rec, root=R, main_py=MAIN, collector=COLLECTOR, session=SESSION)
    return d


def stopped_pids(d) -> list[int]:
    """Every pid this plan would stop, app and collector together."""
    return sorted({p.pid for p in d.graceful} | {p.pid for p in d.collector})


# ------------------------------------------------------------------- canonical
def case_paths_are_compared_not_matched() -> None:
    print("case: what counts as the same path")
    check("a sibling directory whose name starts ours is not inside it",
          under("C:\\Test\\PC Monitor2\\main.py", R), False)
    check("a file whose name starts with main.py is not main.py",
          canon("C:\\Test\\PC Monitor\\main.pyw") == canon(MAIN), False)
    check("the venv interpreter is inside the install",
          under(VENV + "\\pythonw.exe", R), True)
    check("the install is not inside itself", under(R, R), False)
    check("case and separators do not hide a match", canon("c:/test/pc monitor/"),
          canon(MAIN)[:len(canon(R))])
    check("and `..` does not fake one",
          canon(R + "\\tools\\..\\main.py"), canon(MAIN))
    check("a quoted path with a space survives", script_of('"C:\\a b\\main.py" --dump x'),
          "C:\\a b\\main.py")
    check("an unquoted one does too", script_of("pythonw.exe C:\\x\\main.py"),
          "C:\\x\\main.py")
    check("and `-c` has no script at all", script_of("python.exe -c import os"), None)
    # 8.3: a process can be described by its short name. Asked of the OS rather than
    # invented, because short-name generation can be off on a volume — in which case the
    # short name comes back long and the check still means what it says.
    with tempfile.TemporaryDirectory() as td:
        long_p = os.path.join(td, "PC Monitor Long Name")
        os.mkdir(long_p)
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.kernel32.GetShortPathNameW(long_p, buf, 260)
        check("a short name canonicalises to the long one", canon(buf.value), canon(long_p))


# ------------------------------------------------------------- the acceptance list
def case_the_genuine_instance_is_ours() -> None:
    print("case: the real thing, and its collector")
    procs = [
        Proc(pid=100, name="pythonw.exe", exe=VENV + "\\pythonw.exe",
             cmd=f'"{VENV}\\pythonw.exe" "{MAIN}"', ppid=4, created=1000.0),
        Proc(pid=101, name="presentmon.exe", exe=COLLECTOR,
             cmd=f'"{COLLECTOR}" -session_name {SESSION} -etw_csv_path x.csv',
             ppid=100, created=1001.0),
    ]
    d = plan(procs)
    check("the app is stopped", stopped_pids(d), [100, 101])
    check("its collector goes with it", sorted(p.pid for p in d.collector), [101])
    # The venv launcher hands off to an interpreter whose ExecutablePath is the base
    # Python, outside this tree. Two pids for one app is normal here.
    launch = procs + [Proc(pid=102, name="pythonw.exe", exe="C:\\Python312\\pythonw.exe",
                           cmd=f'"{VENV}\\pythonw.exe" "{MAIN}"', ppid=100, created=1000.5)]
    check("the hand-off child is the same app, not a stranger",
          stopped_pids(plan(launch)), [100, 101, 102])
    # The record the app wrote about itself outranks everything, and survives a
    # command line we cannot read.
    rec = {"pid": 100, "started": 1000.0, "root": canon(R), "main": canon(MAIN)}
    d2 = plan([Proc(pid=100, name="pythonw.exe", exe=None, cmd=None, ppid=4,
                    created=1000.0)], rec)
    check("the recorded pid is ours even unreadable", stopped_pids(d2), [100])


def case_strangers_are_left_running() -> None:
    print("case: everything that must survive an install")
    procs = [
        Proc(pid=201, name="python.exe", exe="C:\\other-project\\.venv\\Scripts\\python.exe",
             cmd='"C:\\other-project\\main.py" --serve', ppid=4, created=1.0),
        Proc(pid=202, name="pythonw.exe", exe=R + "2\\.venv\\Scripts\\pythonw.exe",
             cmd=f'"{R}2\\main.py"', ppid=4, created=1.0),
        Proc(pid=203, name="pythonw.exe", exe=VENV + "\\pythonw.exe",
             cmd=f'"{VENV}\\pythonw.exe" "{R}\\tools\\main.py"', ppid=4, created=1.0),
        Proc(pid=204, name="pythonw.exe", exe=VENV + "\\pythonw.exe",
             cmd=f'"{VENV}\\pythonw.exe" "{R}\\main.pyw"', ppid=4, created=1.0),
        Proc(pid=205, name="python.exe", exe=VENV + "\\python.exe",
             cmd=f'"{VENV}\\python.exe" "{R}\\tools\\liveview.py" --port 8712',
             ppid=4, created=1.0),
        Proc(pid=206, name="pythonw.exe", exe=VENV + "\\pythonw.exe",
             cmd=f'"{VENV}\\pythonw.exe" -m http.server', ppid=4, created=1.0),
        Proc(pid=207, name="pythonw.exe", exe=None, cmd=None, ppid=4, created=1.0),
        Proc(pid=208, name="pythonw.exe", exe="C:\\Python312\\pythonw.exe",
             cmd='"C:\\other-project\\main.py"', ppid=4, created=1.0),
    ]
    d = plan(procs)
    check("not one of them is stopped", stopped_pids(d), [])
    for pid, why in ((201, "another project"), (202, "the similarly named sibling"),
                     (203, "this tree's other main.py"), (204, "main.pyw"),
                     (205, "the dev server"), (206, "no script"),
                     (207, "unreadable"), (208, "an unrelated script elsewhere")):
        line = [x for x in d.report if x.startswith(f"pid {pid}:")]
        check(f"{why} is reported as left alone",
              len(line) == 1 and "left alone" in line[0], True)
    # The old rule's headline failure, said in its own words.
    check("the old substring would have killed the notebook kernel too",
          any("201" in x and "left alone" in x for x in d.report), True)


def case_the_recycled_pid_is_not_ownership() -> None:
    print("case: a pid that came back as somebody else")
    rec = {"pid": 40268, "started": 1000.0, "root": canon(R), "main": canon(MAIN)}
    # Same number, older process: the app died long ago and this is whatever inherited
    # the pid. Matching on the pid alone is how a stop lands on a stranger.
    stranger = Proc(pid=40268, name="pythonw.exe",
                    exe="C:\\Python312\\pythonw.exe", cmd='"C:\\work\\main.py"',
                    ppid=4, created=40.0)
    check("the recorded pid with a different creation time is not ours",
          stopped_pids(plan([stranger], rec)), [])
    check("and it says the creation time is what disagreed",
          any("left alone" in x for x in plan([stranger], rec).report), True)
    same = Proc(pid=40268, name="pythonw.exe", exe=VENV + "\\pythonw.exe",
                cmd=f'"{VENV}\\pythonw.exe" "{MAIN}"', ppid=4, created=1000.4)
    check("the same pid within the clock's tolerance is ours",
          stopped_pids(plan([same], rec)), [40268])

    # The collector's parent pid, recycled: our pinned binary, launched by a process that
    # is not our app any more, and not carrying our session.
    orphan = Proc(pid=700, name="presentmon.exe", exe=COLLECTOR,
                  cmd=f'"{COLLECTOR}" -session_name OtherTool -etw_csv_path o.csv',
                  ppid=40268, created=50.0)
    d = plan([stranger, orphan], rec)
    check("a reused parent pid does not make a collector ours", stopped_pids(d), [])
    check("and that refusal is reported", any("recycled" in x or "not an app we own" in x
                                              for x in d.report), True)


def case_collectors_are_told_apart() -> None:
    print("case: three presentmon.exe processes, one of them ours")
    procs = [Proc(pid=100, name="pythonw.exe", exe=VENV + "\\pythonw.exe",
                  cmd=f'"{VENV}\\pythonw.exe" "{MAIN}"', ppid=4, created=1.0)]
    ours = Proc(pid=301, name="presentmon.exe", exe=COLLECTOR,
                cmd=f'"{COLLECTOR}" -session_name {SESSION}', ppid=100, created=1.0)
    dev = Proc(pid=302, name="presentmon.exe", exe=COLLECTOR,
               cmd=f'"{COLLECTOR}" -session_name PCMonitor-liveview', ppid=999,
               created=1.0)
    other = Proc(pid=303, name="presentmon.exe", exe="C:\\Tools\\OBS\\presentmon.exe",
                 cmd=f'"C:\\Tools\\OBS\\presentmon.exe" -session_name {SESSION}',
                 ppid=998, created=1.0)
    blind = Proc(pid=304, name="presentmon.exe", exe=COLLECTOR, cmd=None, ppid=997,
                 created=1.0)
    d = plan(procs + [ours, dev, other, blind])
    check("only ours is stopped", stopped_pids(d), [100, 301])
    check("the dev role's capture is left running (it is a different session)",
          any("302" in x and "left alone" in x for x in d.report), True)
    check("another application's collector is left alone even carrying our session name",
          any("303" in x and "not this project's collector binary" in x for x in d.report),
          True)
    check("a collector whose command line cannot be read is left alone",
          any("304" in x and "left alone" in x for x in d.report), True)


# ---------------------------------------------------------------------- the stop
class Script:
    """A scripted world: which pids are alive, and everything the killer was asked to do."""

    def __init__(self, alive: dict[int, bool], exits_after: int = 0) -> None:
        self.alive = dict(alive)
        self.polls: dict[int, int] = {}
        self.exits_after = exits_after
        self.killed: list[int] = []
        self.slept = 0.0

    def is_alive(self, pid: int) -> bool:
        self.polls[pid] = self.polls.get(pid, 0) + 1
        if self.exits_after and pid in self.alive and self.alive[pid] \
                and self.polls[pid] > self.exits_after:
            self.alive[pid] = False          # it took the request and shut itself down
        return self.alive.get(pid, False)

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        self.alive[pid] = False

    def sleep(self, s: float) -> None:
        self.slept += s


def case_stop_is_graceful_then_bounded(tmp: Path) -> None:
    print("case: ask first, force only what was proven, never anything else")
    stop_file = tmp / ".stop"
    procs = [Proc(pid=100, name="pythonw.exe", exe=VENV + "\\pythonw.exe",
                  cmd=f'"{VENV}\\pythonw.exe" "{MAIN}"', ppid=4, created=1.0),
             Proc(pid=201, name="python.exe", exe="C:\\other\\python.exe",
                  cmd='"C:\\other\\main.py"', ppid=4, created=1.0),
             Proc(pid=301, name="presentmon.exe", exe=COLLECTOR,
                  cmd=f'"{COLLECTOR}" -session_name {SESSION}', ppid=100, created=1.0)]
    d = plan(procs)

    s = Script({100: True, 301: True}, exits_after=2)
    log = owned.apply(d, alive=s.is_alive, kill=s.kill, sleep=s.sleep, graceful_s=4.0,
                      collector_grace_s=2.0, stop_path=stop_file)
    check("an app that answers the request is not killed", s.killed, [])
    # The request file outlives the run that was asked: it is consumed by the app, and a
    # scripted world has none. main.py clears a leftover one at start-up, which is the
    # case tested in case_the_install_script_no_longer_matches_names.
    check("it was asked, in the way that lets it clean up", stop_file.exists(), True)
    check("the wait was bounded", s.slept <= 4.0 + 2.0 + 0.01, True)
    check("and the log says it stopped itself", any("stopped itself cleanly" in x for x in log),
          True)
    check("the collector went with its parent", any("already gone with its parent" in x
                                                    for x in log), True)

    s2 = Script({100: True, 301: True})          # ignores the request entirely
    log2 = owned.apply(d, alive=s2.is_alive, kill=s2.kill, sleep=s2.sleep, graceful_s=2.0,
                       collector_grace_s=1.0, stop_path=stop_file)
    check("an app that ignores it is forced, once", s2.killed, [100, 301])
    check("forcing is said out loud", any("forcing" in x for x in log2), True)
    check("the stranger was never even offered to the killer",
          201 not in s2.killed and 201 not in stopped_pids(d), True)

    # The request/consume pair, and its once-only behaviour.
    check("a request can be written", owned.request_stop(stop_file), True)
    check("and consumed exactly once", (owned.stop_requested(stop_file),
                                       owned.stop_requested(stop_file)), (True, False))


# ---------------------------------------------------------------------- the record
def case_the_record_is_honest(tmp: Path) -> None:
    print("case: what the app says about itself")
    p = tmp / ".owner"
    check("nothing recorded yet", owned.read_record(p), None)
    check("a real record is written", owned.write_record(main_py=Path(MAIN),
                                                         interpreter=VENV + "\\pythonw.exe",
                                                         session=SESSION,
                                                         collector=COLLECTOR, path=p), True)
    rec = owned.read_record(p)
    check("it names this process", rec["pid"] == os.getpid(), True)
    check("with the time it started", isinstance(rec["started"], float), True)
    # The root is the install the writing process belongs to — the real one, not the
    # fake root the classification cases use. That is the whole point of the record: it
    # is written by the app, so it cannot be forged by a stranger's main.py.
    check("and paths in canonical form", (rec["main"], rec["root"], rec["session"]),
          (canon(MAIN), canon(owned.ROOT), SESSION))
    check("no half-written file is left", (tmp / ".owner.tmp").exists(), False)
    p.write_text("{ not json", encoding="utf-8")
    check("a corrupt record reads as no record", owned.read_record(p), None)
    owned.clear_record(p)
    check("and clearing it never raises", owned.read_record(p), None)
    check("creation time of this process is real",
          owned.process_start_time(os.getpid()) is not None, True)
    check("and of a pid nobody has, it is not", owned.process_start_time(2147483646), None)
    check("pid 0 is not alive", owned.pid_alive(0), False)
    check("this process is", owned.pid_alive(os.getpid()), True)
    src = (ROOT / "app" / "owned.py").read_text(encoding="utf-8")
    # `os.kill(pid, 0)` terminates on Windows, so it may only appear in the branch that
    # runs where it is correct. A comment mentioning it is not a call, and neither is the
    # docstring — so this is an AST question, not a grep.
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "pid_alive")
    handlers = [h for t in ast.walk(fn) if isinstance(t, ast.Try) for h in t.handlers]
    guarded = [n for h in handlers for n in ast.walk(h) if isinstance(n, ast.Call)]
    kills = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "os" and n.func.attr == "kill"]
    check("os.kill is not used on the Windows path",
          all(k in guarded for k in kills) and len(kills) >= 1, True)
    body = ast.get_source_segment(src, fn) or ""
    check("liveness asks for query rights only", "_QUERY_LIMITED" in body, True)
    # The prose has to say the word to explain itself, so the question is about the right
    # itself: the constant name or its value, neither of which belongs in a check.
    check("and never for the right to terminate",
          "0x0001" in body or "PROCESS_TERMINATE" in body, False)


# ----------------------------------------------------------------- the real query
class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def case_the_process_query_is_the_one_that_runs() -> None:
    print("case: the CIM query itself, pinned without running it")
    # Everything above feeds `classify` rows it is handed. This is the only case that
    # looks at where those rows come from — and it earned its place: the first version of
    # the query passed the filter to CIM positionally, Windows answered with a parameter
    # error, and the module quietly reported "nothing of ours is running" while a real
    # instance held the COM port. A stop that stops nothing looks identical to a clean
    # install, so the shape of the query is a correctness claim, not a detail.
    calls: list[list[str]] = []
    row = (f"100\tpythonw.exe\t{VENV}\\pythonw.exe\t4\t20260926184337.123456+120\t"
           f'"{VENV}\\pythonw.exe" "{MAIN}"')
    real = owned.subprocess.run
    try:
        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            return _Done(0, row + "\n")

        owned.subprocess.run = fake_run
        rows, err = owned.enumerate_processes()
        joined = " ".join(calls[0])
        check("the query is asked once, not once per process", len(calls), 1)
        check("the class is named, not positional", "-ClassName Win32_Process" in joined, True)
        check("and the filter is a -Filter, not a bare WHERE",
              "-Filter" in joined and "Win32_Process WHERE" not in joined, True)
        check("command lines come back in the same pass", joined.count("CommandLine"), 1)
        check("nothing is stopped to ask the question", "Stop-Process" in joined, False)
        check("a row parses", (len(rows), rows[0].pid, rows[0].name, rows[0].ppid),
              (1, 100, "pythonw.exe", 4))
        check("its creation time is a real timestamp", isinstance(rows[0].created, float), True)
        check("and its command line survived the tab join", script_of(rows[0].cmd), MAIN)
        check("so a real row reaches the same verdict as a scripted one",
              stopped_pids(classify(rows, None, root=R, main_py=MAIN, collector=COLLECTOR,
                                    session=SESSION)), [100])

        owned.subprocess.run = lambda *a, **k: _Done(1, "", "Get-CimInstance : boom")
        rows2, err2 = owned.enumerate_processes()
        check("a failed query is not an empty process list", (rows2, "boom" in err2),
              ([], True))
        check("and the module refuses to decide on it", owned.main(["report"]), 1)

        def boom(*a, **k):
            raise OSError("no powershell here")

        owned.subprocess.run = boom
        rows3, err3 = owned.enumerate_processes()
        check("a missing interpreter of queries is said, not swallowed",
              (rows3, "OSError" in err3), ([], True))
        check("and it is non-zero too", owned.main(["stop"]), 1)
    finally:
        owned.subprocess.run = real


# ------------------------------------------------------------------ the callers
def case_the_install_script_no_longer_matches_names() -> None:
    print("case: the installer asks about ownership, not about names")
    ps = (ROOT / "tools" / "install_autostart.ps1").read_text(encoding="utf-8")
    check("no command-line substring match survives", "main\\.py" in ps, False)
    check("no -match on a command line at all", "CommandLine" in ps, False)
    check("the script contains no Stop-Process anywhere", "Stop-Process" in ps, False)
    check("nor a hard-kill helper", "Stop-AppInstances" in ps or "Stop-OrphanPresentMon" in ps,
          False)
    check("ownership is asked for once, before registering", "Stop-OwnedProcesses" in ps, True)
    check("by the module that decides it", "-m app.owned" in ps, True)
    check("with the collector's identity named", "--collector $Collector" in ps, True)
    check("and the session name it owns", "--session $Session" in ps, True)
    # Both verbs go through the one helper, so "does the health check use ownership" is a
    # question about the call sites, not about how many times the module is named.
    check("the stop asks it", "Invoke-Owned 'stop'" in ps, True)
    check("and so does the health check", "Invoke-Owned 'live'" in ps, True)
    # `.index` on a string that is not there raises, and a raise here would hide every check
    # after it — so the ordering is only asked when the helper exists at all.
    check("removal uses it too",
          "Stop-OwnedProcesses" in ps and "if (-not (Test-Path $Pyw))" in ps
          and ps.index("Stop-OwnedProcesses") < ps.index("if (-not (Test-Path $Pyw))"), True)

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    loop = next(n for n in ast.walk(main_fn) if isinstance(n, ast.While))
    # A helper handed to `atexit.register` or to the guard is a Name, not a Call, so both
    # forms count as "this code is wired up".
    used = {getattr(n.func, "id", "") for n in ast.walk(main_fn) if isinstance(n, ast.Call)}
    used |= {n.id for n in ast.walk(main_fn) if isinstance(n, ast.Name)}
    check("the app records itself before the loop", "write_record" in used, True)
    check("and registers the cleanup", "clear_record" in used, True)
    check("the loop watches for a requested shutdown",
          any(isinstance(n, ast.Name) and n.id == "stop_requested" for n in ast.walk(loop)),
          True)
    check("a request leaves by the front door, not a crash",
          any(isinstance(n, ast.Return) for n in ast.walk(loop)), True)
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check("the record and the request are runtime files, not sources",
          ".owner" in gi and ".stop" in gi, True)


def main_run() -> int:
    case_paths_are_compared_not_matched()
    print()
    case_the_genuine_instance_is_ours()
    print()
    case_strangers_are_left_running()
    print()
    case_the_recycled_pid_is_not_ownership()
    print()
    case_collectors_are_told_apart()
    print()
    with tempfile.TemporaryDirectory() as td:
        case_stop_is_graceful_then_bounded(Path(td))
        print()
        case_the_record_is_honest(Path(td))
        print()
    case_the_process_query_is_the_one_that_runs()
    print()
    case_the_install_script_no_longer_matches_names()
    print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main_run())