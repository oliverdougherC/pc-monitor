"""Which processes this installation actually owns — and which it must leave alone.

`tools/install_autostart.ps1` used to stop the app with

    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'main\\.py' } | Stop-Process -Force

That is an elevated script force-killing every Python program on the machine whose
command line happens to contain `main.py`: another project's server, a colleague's
notebook, an editor's language server. `main.py` is the most common entry-point filename
in Python. The collector cleanup was the same shape — any `presentmon.exe` whose parent
pid was absent — and a parent pid is not ownership evidence, because pids are recycled
and a reused one points at whatever process got there second.

Everything here is decided from identity rather than from a substring, in this order of
trust:

  1. **the record the app wrote about itself** (`.owner`): the app is the only writer, so
     a pid plus that process's creation time is a fact about a specific process
     instance, not a guess about a name. The creation time is what defeats pid reuse.
  2. **canonical paths**: the interpreter must live under this install's own tree, and
     the script argument must resolve to *this* `main.py` — compared component-wise, so
     `..\\PC Monitor2\\main.py` and `..\\PC Monitor\\tools\\main.py` are not it. Windows
     paths are canonicalised through `GetFullPathNameW` first, so an 8.3 segment
     (`PROGRA~1`), a `..`, a stray separator or a different case cannot hide the match or
     fake one.
  3. **the session the collector was told to own**: a `presentmon.exe` is ours only if it
     is *this* project's pinned executable *and* it carries this project's role-scoped
     ETW session name (or its parent is our recorded app instance).

Anything that cannot be resolved — an unreadable command line, an empty
`ExecutablePath`, a collector with a name but no session — is **reported and left
running**. The issue's rule, and the right one: an installer that guesses stops
somebody's work, and an installer that is unsure can simply say so.

Nothing in this module stops a process at import time or by accident: stopping happens
only inside `apply()`, with a pid list that came from `plan()`, a graceful request
(`.stop`, which the app itself watches so it can close the COM port and its own child
properly), a bounded wait, and force only for a pid still proven to be ours.

**The record belongs to the instance that wrote it** (#67). The record used to be a
plain shared file with an unconditional `unlink()` on the way out, so any second
writer — a `main.py --dump` headless preview is the one that actually happens —
replaced the running app's identity while it ran and then deleted it on exit. The
installer's whole stop path reads that file, so a preview could make "stop the app"
name a process that had already gone. A record now carries an `instance` token and
`clear_record` removes it only when the token on disk is still this process's; the
other writers register nothing at all.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def control_dir() -> Path:
    """Where the owner record and the stop request live.

    `main.py` runs from this tree, and so does the installer, so the default is the
    tree itself. The environment override exists so a test can give a child process a
    scratch control plane and then assert, from outside, that the child wrote nothing
    into the real one — which is the only way to prove #67 without stopping the app on
    somebody's desk. It is read on every call rather than cached at import, because a
    child that is handed the variable after this module is imported must still see it.
    """
    override = os.environ.get("PCMON_CONTROL_DIR", "").strip()
    return Path(override) if override else ROOT


def owner_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else control_dir() / ".owner"


def stop_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else control_dir() / ".stop"

APP_NAMES = {"python.exe", "pythonw.exe"}
COLLECTOR = "presentmon.exe"
GRACEFUL_S = 12.0           # how long a requested shutdown may take before it is forced
COLLECTOR_GRACE_S = 3.0     # ... and how long its collector may take to follow it
FORCE_WAIT_S = 3.0          # ... and how long to wait for the force to land
_QUERY_LIMITED = 0x1000     # PROCESS_QUERY_LIMITED_INFORMATION
_STILL_ACTIVE = 259

# A `.py` argument on a command line. Quoted or not, forward or back slashes.
_PY_ARG = re.compile(r'"([^"]+\.py)"|(\S+\.py)')


# ------------------------------------------------------------------ canonical
def canon(path: str | Path) -> str:
    """A comparable form of a Windows path: long form, folded case, one separator.

    Two calls, because they do opposite halves of the job. `GetFullPathNameW` makes the
    path absolute and collapses `.`/`..`; `GetLongPathNameW` then expands any 8.3 segment,
    because a process can be described by its short name (`PCMONI~1\\main.py`) and that is
    the same file the installer is holding. The second call needs the path to exist, so a
    path that does not (a config value pointing at a missing binary) keeps the first
    form — which is still comparable against another non-existent path.

    Neither call follows symlinks or junctions, and that is deliberate: a vendored tree
    that is junctioned elsewhere is still *this* install's file, and resolving it away
    would reassign it to whoever owns the target.
    """
    s = str(path)
    try:
        k32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        buf = ctypes.create_unicode_buffer(32768)
        if k32.GetFullPathNameW(s, 32768, buf, None):
            s = buf.value
            if os.path.exists(s):
                buf2 = ctypes.create_unicode_buffer(32768)
                if k32.GetLongPathNameW(s, buf2, 32768):
                    s = buf2.value
    except (AttributeError, OSError):         # pragma: no cover - not Windows
        s = os.path.abspath(s)
    return os.path.normcase(s).replace("/", "\\").rstrip("\\")


def under(child: str | Path, parent: str | Path) -> bool:
    """Is `child` inside `parent`? Component-wise, never as a string prefix.

    The prefix test is the bug this exists to avoid: `C:\\x\\PC Monitor2\\main.py` starts
    with `C:\\x\\PC Monitor` and is a different project.
    """
    try:
        return Path(canon(child)).relative_to(canon(parent)) != Path()
    except ValueError:
        return False


def script_of(command_line: str | None) -> str | None:
    """The first `.py` argument on a command line, or None."""
    if not command_line:
        return None
    for m in _PY_ARG.finditer(command_line):
        return m.group(1) or m.group(2)
    return None


# ------------------------------------------------------------------- identity
def process_start_time(pid: int) -> float | None:
    """When this process instance began, as a unix timestamp. None if unaskable.

    Paired with the pid, this is the difference between "pid 40268 is our app" and "pid
    40268 was our app and is now something else that inherited the number".
    """
    try:
        k32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
    except (AttributeError, OSError):         # pragma: no cover - not Windows
        return None
    h = k32.OpenProcess(_QUERY_LIMITED, False, int(pid))
    if not h:
        return None
    try:
        # All four FILETIMEs get a real buffer. The documented NULL-able ones are not
        # treated as NULL-able on every build, and passing None here was caught writing to
        # address zero — an access violation in the path the app uses to describe itself.
        creation, exit_t, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        # FILETIME: 100 ns ticks since 1601-01-01.
        return creation.value / 1e7 - 11644473600.0
    finally:
        k32.CloseHandle(h)


def pid_alive(pid: int) -> bool:
    """Still running? Query rights only — never the right to terminate.

    `os.kill(pid, 0)` is the POSIX idiom and on Windows it *terminates* the process, so
    it has no place in a module whose job is to decide about other people's processes.
    """
    if pid <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
    except (AttributeError, OSError):         # pragma: no cover - not Windows
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = k32.OpenProcess(_QUERY_LIMITED, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and \
            code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def new_token() -> str:
    """A token that belongs to one process *start*, not to one pid and not to a name.

    `os.getpid()` alone is not an identity — a diagnostic run and the app it runs
    beside can both be alive and a pid can be recycled — so the token is random and
    is only ever compared against the file it was written into. It is not a secret
    and it is not a security boundary: it is the difference between "the record on
    disk is the one I wrote" and "the record on disk is somebody else's", which is
    exactly what an unconditional `unlink()` could not tell apart (#67).
    """
    return secrets.token_hex(16)


def write_record(main_py: Path | None = None, interpreter: str | None = None,
                 session: str = "", collector: str = "", role: str = "main",
                 token: str = "", path: Path | str | None = None) -> str:
    """Say who this process is, for whoever has to stop it later.

    Written by the app about itself, which is what makes it evidence rather than a
    guess: an unrelated `main.py` has no reason to claim this install's root. Returns
    the instance token it wrote (the caller's, or a fresh one), which is what
    `clear_record` and `owns_record` require before they will believe the file is ours.

    `role` is recorded but never used to *match*: a record names one process, and a
    second writer replacing it is the failure this token exists to catch, whatever
    role it claims.
    """
    rec = {"pid": os.getpid(), "ppid": os.getppid(),
           "started": process_start_time(os.getpid()),
           "main": canon(main_py or (ROOT / "main.py")),
           "root": canon(ROOT),
           "interpreter": canon(interpreter or sys.executable),
           "session": session, "collector": canon(collector or ""),
           "role": role, "instance": token or new_token()}
    p = owner_path(path)
    tmp = p.with_name(p.name + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        return ""
    return str(rec["instance"])


def read_record(path: Path | str | None = None) -> dict | None:
    p = owner_path(path)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def owns_record(token: str, path: Path | str | None = None) -> bool:
    """Is the record on disk still the one this token wrote?

    False is the safe answer for every uncertainty — no token, no file, an unreadable
    file, a file another instance has since replaced — because the caller uses this to
    decide whether it may delete shared state and whether a stop request on disk is
    addressed to it. Deleting somebody else's record is the bug (#67); declining to
    delete our own costs one stale file that the next start overwrites anyway.
    """
    if not token:
        return False
    rec = read_record(path)
    return bool(rec) and rec.get("instance") == token


def clear_record(token: str = "", path: Path | str | None = None) -> bool:
    """Remove the owner record, but only if we are still the instance that wrote it.

    Returns whether the record was removed. With no token this refuses rather than
    clearing the shared file: every caller that legitimately owns a record has a token
    from `write_record`, so the tokenless form is the old unconditional unlink, and
    that is precisely what a concurrent `main.py --dump` used to call on its way out.
    """
    if not owns_record(token, path):
        return False
    try:
        owner_path(path).unlink()
        return True
    except OSError:
        return False


# ------------------------------------------------------------------- the plan
@dataclass
class Proc:
    """One row of `Win32_Process`, or a test's stand-in for one."""
    pid: int
    name: str = ""
    exe: str | None = None            # ExecutablePath; None means "could not read"
    cmd: str | None = None            # CommandLine; None means "could not read"
    ppid: int = 0
    created: float | None = None      # process creation time, unix seconds


@dataclass
class Decision:
    stop: list[Proc] = field(default_factory=list)
    graceful: list[Proc] = field(default_factory=list)
    collector: list[Proc] = field(default_factory=list)
    report: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return self.report


def _record_matches(rec: dict | None, p: Proc) -> bool:
    if not rec or p.pid != rec.get("pid"):
        return False
    started, created = rec.get("started"), p.created
    if started is None or created is None:
        return True             # nothing to contradict it; the record named this pid
    return abs(float(started) - float(created)) <= 2.0


def _app_verdict(p: Proc, rec: dict | None, root: str, main_py: str,
                 owners: set[int]) -> tuple[bool, str]:
    """Is this python/pythonw row ours? Returns (ours, why).

    Ordered by strength of evidence, and every "no" says which test it failed, because
    an installer that refuses should be legible about it.
    """
    if _record_matches(rec, p):
        return True, "the record this app wrote about itself names this pid and creation time"
    if not p.exe:
        return False, ("interpreter path unreadable - not touching it (this is what an "
                       "elevated process looks like to a run that is not elevated the "
                       "same way)")
    script = script_of(p.cmd)
    if script is None:
        return False, "no script on its command line - it is not this app's entry point"
    if canon(script) != main_py:
        return False, f"runs {script}, not {main_py}"
    if under(p.exe, root):
        return True, f"interpreter {p.exe} belongs to this install and the script is its main.py"
    if p.ppid in owners:
        # The venv launcher and the interpreter it hands off to are one launch, and the
        # child's ExecutablePath is the base Python, not this tree's. Following the
        # parent is still a canonical-path statement; it is not a name match.
        return True, "child of a process already proven to be this install's launcher"
    # Somebody ran this install's main.py with a different interpreter. It is still this
    # install's app, and it is still holding the panel's one COM port, so the install
    # cannot proceed past it - but it is said out loud, because the launch is not the one
    # this install would have made.
    return True, (f"runs this install's main.py under an interpreter from elsewhere "
                  f"({p.exe}) - it holds the panel, so it is stopped, and said so")


def classify(procs: list[Proc], rec: dict | None, root: Path | str | None = None,
             main_py: Path | str | None = None, collector: Path | str | None = None,
             session: str = "") -> Decision:
    """Sort a process list into ours, and everything we must not touch (with a reason)."""
    root = canon(root or ROOT)
    main_py = canon(main_py or (Path(root) / "main.py"))
    want_collector = canon(collector or (Path(root) / "vendor" / "presentmon" / COLLECTOR))
    d = Decision()
    apps = [p for p in procs if p.name.lower() in APP_NAMES]

    # Two passes: a launcher hand-off is only recognisable once the launcher itself has
    # been identified, and the order rows come back from CIM is not that order.
    owners = {p.pid for p in apps if _app_verdict(p, rec, root, main_py, set())[0]}
    for p in apps:
        ours, why = _app_verdict(p, rec, root, main_py, owners)
        if ours:
            d.graceful.append(p)
        d.report.append(f"pid {p.pid}: " + (f"ours - {why}" if ours
                                            else f"left alone - {why}"))
    app_pids = {p.pid for p in d.graceful}

    for p in procs:
        if p.name.lower() != COLLECTOR:
            continue
        if not p.exe or canon(p.exe) != want_collector:
            d.report.append(f"pid {p.pid}: {COLLECTOR} is not this project's collector "
                            f"binary ({p.exe or 'path unreadable'}) - left alone")
            continue
        # Only a parent identified in this same scan counts. A pid recorded by a
        # previous run is exactly what a recycled pid can imitate, which is the failure
        # this rule exists to close; with the app already gone, the collector has to be
        # recognised by the session it was told to own.
        parent_is_ours = p.ppid in app_pids
        carries_session = bool(session) and session in (p.cmd or "")
        if not parent_is_ours and not carries_session:
            d.report.append(f"pid {p.pid}: our collector binary, but it carries no "
                            f"session {session or '(none given)'} and its parent "
                            f"{p.ppid} is not an app we own - left alone")
            continue
        d.collector.append(p)
        d.report.append(f"pid {p.pid}: ours - this project's collector "
                        + ("carrying " + session if carries_session
                           else "with our app as its parent"))

    return d


def plan(procs: list[Proc], rec: dict | None, **kw) -> Decision:
    return classify(procs, rec, **kw)


# --------------------------------------------------------------- the real world
_QUERY = ("-NoProfile", "-Command",
          "Get-CimInstance -ClassName Win32_Process -Filter "
          "\"Name='python.exe' OR Name='pythonw.exe' OR Name='presentmon.exe'\" | "
          "ForEach-Object { ($_.ProcessId, $_.Name, $_.ExecutablePath, "
          "$_.ParentProcessId, $_.CreationDate, "
          "($_.CommandLine -replace '[\\r\\n]+', ' ')) -join \"`t\" }")


def enumerate_processes(timeout: float = 45.0) -> tuple[list[Proc], str]:
    """Ask Windows for the rows `classify` needs. Read-only; returns (rows, error).

    One CIM query, not one per process: the command line is fetched in the same pass,
    with its newlines folded, because a row per line is how this is parsed. CIM rather
    than `psutil.process_iter` because the installer already runs elevated, and CIM is
    where another elevated process's `ExecutablePath` and `CommandLine` are actually
    visible — and because this has to keep working when the venv cannot import
    third-party modules.

    The error half matters as much as the rows. An empty list is a *claim* — "nothing of
    ours is running" — and only a query that actually ran is entitled to make it. A query
    that failed means we do not know, so the caller must neither stop anything nor report
    success.
    """
    try:
        out = subprocess.run(["powershell.exe", *_QUERY], capture_output=True,
                             text=True, timeout=timeout, check=False,
                             creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW: pythonw has no console, so an un-flagged query flashes one
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"could not ask Windows for its process list: {type(e).__name__}: {e}"
    if out.returncode != 0:
        return [], (f"the process query failed (exit {out.returncode}): "
                    f"{(out.stderr or '').strip()[:200]}")
    rows: list[Proc] = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[3])
        except ValueError:
            continue
        rows.append(Proc(pid=pid, name=parts[1], exe=parts[2] or None, ppid=ppid,
                         cmd=parts[5] or None, created=_to_unix(parts[4])))
    return rows, ""


def _to_unix(created: str) -> float | None:
    """CIM hands back `20260926184337.123456+120`; that is local wall time."""
    m = re.match(r"(\d{14})(?:\.(\d+))?([+-]\d+)?", created.strip())
    if not m:
        return None
    try:
        t = time.strptime(m.group(1), "%Y%m%d%H%M%S")
        base = time.mktime(t)
        if m.group(3) is not None:
            off = int(m.group(3))
            base -= off * 60 - (time.timezone if time.daylight == 0 else time.altzone)
        return base
    except (ValueError, OverflowError):
        return None


def request_stop(path: Path | str | None = None) -> bool:
    """Ask the app to shut itself down. It watches for this file so its own `atexit`
    can close the COM port and stop its collector — which a `Stop-Process -Force` never
    lets it do, and which is why orphaned ETW sessions existed at all."""
    p = stop_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{time.time():.3f} requested\n", encoding="utf-8")
        return True
    except OSError:
        return False


def stop_requested(path: Path | str | None = None) -> bool:
    """Consume a stop request: True once, then the file is gone.

    Consumption is destructive, so only the production instance may call this (#67):
    a stop request is addressed to the process that owns the installation, and a
    headless preview that consumed one would leave the installer waiting for an app
    that is still running.
    """
    p = stop_path(path)
    try:
        p.unlink()
        return True
    except OSError:
        return False


def apply(decision: Decision, alive=pid_alive, kill=None, sleep=time.sleep,
          graceful_s: float = GRACEFUL_S, collector_grace_s: float = COLLECTOR_GRACE_S,
          stop_path: Path | str | None = None,
          report: list[str] | None = None) -> list[str]:
    """Stop what was planned, gently first. Returns the log of what happened.

    `alive`, `kill` and `sleep` are parameters so the whole sequence can be proven
    against a scripted process list instead of by stopping something real.
    """
    kill = kill or _terminate
    log = report if report is not None else []
    for p in decision.graceful:
        request_stop(stop_path)
        waited = 0.0
        while waited < graceful_s and alive(p.pid):
            sleep(0.5)
            waited += 0.5
        if not alive(p.pid):
            log.append(f"pid {p.pid}: stopped itself cleanly after {waited:.1f}s")
            continue
        log.append(f"pid {p.pid}: still running after {waited:.1f}s - forcing (owned "
                   f"process, identity proven)")
        kill(p.pid)
        sleep(FORCE_WAIT_S)
    if decision.collector and decision.graceful:
        # Stopping its own collector is part of how the app leaves. Forcing the child the
        # instant its parent dies throws away the one shutdown that closes the ETW session
        # properly, so the collector gets a short window to go with it.
        waited = 0.0
        while waited < collector_grace_s and any(alive(c.pid) for c in decision.collector):
            sleep(0.5)
            waited += 0.5
    for p in decision.collector:
        if not alive(p.pid):
            log.append(f"pid {p.pid}: collector already gone with its parent")
            continue
        log.append(f"pid {p.pid}: our collector, still owning an ETW session - stopping")
        kill(p.pid)
        sleep(FORCE_WAIT_S)
    return log


def _terminate(pid: int) -> None:
    """TerminateProcess on a pid that `classify` proved is ours. Nothing else calls it."""
    k32 = ctypes.windll.kernel32              # type: ignore[attr-defined]
    h = k32.OpenProcess(0x0001 | 0x0040, False, int(pid))    # TERMINATE | QUERY_LIMITED
    if h:
        k32.TerminateProcess(h, 0)
        k32.CloseHandle(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.owned",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("verb", nargs="?", default="report",
                    choices=["report", "stop", "live"])
    ap.add_argument("--session", default="", help="the role-scoped ETW session name")
    ap.add_argument("--collector", default="", help="this project's presentmon.exe")
    args = ap.parse_args(argv)

    rec = read_record()
    procs, err = enumerate_processes()
    if err:
        # Loud, and non-zero: the installer's `throw` turns this into a stopped install
        # instead of an install that quietly stopped nothing and kept going.
        print(f"  cannot check which processes are ours: {err}")
        print("  not touching any process")
        return 1
    d = classify(procs, rec, session=args.session,
                 collector=args.collector or None)
    if args.verb == "stop":
        for line in apply(d):
            print("  " + line)
    for line in d.report:
        print("  " + line)
    if args.verb == "live":
        pids = [str(p.pid) for p in d.graceful]
        print("pids " + (",".join(pids) if pids else "-"))
        print("collectors " + (",".join(str(p.pid) for p in d.collector) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
