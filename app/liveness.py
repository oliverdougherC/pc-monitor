"""Progress an outside observer can check, and a restart budget that can say no.

The app's own heartbeat is written by the loop it is meant to prove alive, so a hung
loop is the one failure the log cannot report: the last line stays the last line
forever, and `log.log` says nothing wrong. `tools/watchdog_autostart.ps1` is the
observer that lives outside that failure domain, and this module is the contract
between them — the beat file, the deliberate-shutdown marker, and the decision.

Three things are deliberate about the shape:

* **Everything here is stdlib.** The point of a bootstrap path is that it still works
  when the normal dependencies do not, so nothing may be imported that a broken venv
  or a missing `vendor/` can take with it. `psutil` is exactly such a dependency, and
  `pid_alive` says what it does instead and why.
* **The decision is a pure function of what is on disk.** A watchdog that decides in
  PowerShell string logic cannot be tested without Task Scheduler; this one is tested
  by writing three files and calling `decide()`, which is the whole of its behaviour.
* **The budget can refuse.** Bounded restarts with backoff, and a `permanent` verdict
  when the budget is spent: an app that is crash-looping does not need a faster
  relaunch, it needs a human, and recovery that never gives up is how a two-minute
  outage becomes a machine that relaunches forever.

Files, all next to `main.py`, all rewritten atomically (a reader must never see a
half-written beat and call it fresh):

  `.heartbeat`  `<epoch> <pid> <tick> <state>`   one line, once per control-loop tick
  `.stopped`    `<epoch> <reason>`               written only on a deliberate shutdown
  `.restarts`   `<epoch>` per line, capped       the budget's memory

**A diagnostic role beats under its own name** (#67). `main.py --dump` runs a real
control loop beside the running app on purpose, and when it wrote the shared
`.heartbeat` it refreshed the one file the outside observer reads: a preview could
make a stalled app look healthy, and its exit could write `.stopped` and talk the
watchdog out of recovering the app it had nothing to do with. A non-production role
therefore writes `<file>-<role>` instead. The observer is unchanged and still reads
the unsuffixed names, so the only thing a preview can now move is its own file.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def control_dir() -> Path:
    """Where the beat, the stop marker and the budget live.

    The tree itself by default. The environment override lets a test hand a child
    process a scratch control plane so it can prove, from outside, that the child
    touched nothing in the real one — the #67 contract, which is otherwise only
    assertable by racing two real processes on somebody's desk.
    """
    override = os.environ.get("PCMON_CONTROL_DIR", "").strip()
    return Path(override) if override else ROOT


def _scoped(name: str, role: str) -> Path:
    """`name` for the production role, `name-<role>` for any other.

    Production is the unsuffixed name because that is the contract the observer
    (`tools/watchdog_autostart.ps1`) already implements, and a watchdog has to keep
    working against an app installed before this distinction existed.
    """
    clean = "".join(c for c in role if c.isalnum() or c in "-_") or "main"
    return control_dir() / (name if clean == "main" else f"{name}-{clean}")


def beat_path(role: str = "main") -> Path:
    return _scoped(".heartbeat", role)


def stopped_path(role: str = "main") -> Path:
    return _scoped(".stopped", role)


def restarts_path(role: str = "main") -> Path:
    return _scoped(".restarts", role)

# A tick is 1 s; six hundred seconds of silence is not a slow tick, it is a loop that
# is not running. It has to be comfortably longer than the app's own start-up (a
# revision-C link wakes, resets and re-detects the panel, and the installer waits 20 s
# for that) and longer than the observer's interval, so one stale read alone cannot
# cause a restart.
STALL_S = 600.0
BUDGET = 3                # restarts per WINDOW_S, then `permanent`
WINDOW_S = 3600.0
BACKOFF_S = 300.0         # the nth restart also waits BACKOFF_S × 2**(n-1)
STILL_ACTIVE = 259        # GetExitCodeProcess: the process has not exited
_QUERY_LIMITED = 0x1000   # PROCESS_QUERY_LIMITED_INFORMATION


# ------------------------------------------------------------------ the beat
def beat(now: float | None = None, tick: int = 0, state: str = "",
         path: Path | str | None = None, role: str = "main") -> bool:
    """Say "the loop finished a tick". True if the sentence landed.

    Written through a temporary file and `os.replace`, which is atomic on the same
    volume: the observer reads this file while the loop is writing it, and a truncated
    read would look exactly like a hang. `role` picks the file (see `_scoped`): a
    `--dump` preview beats into `.heartbeat-dump`, so the observer's view of the app
    is not something a diagnostic can refresh or silence (#67).
    """
    p = beat_path(role) if path is None else Path(path)
    tmp = p.with_name(p.name + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{time.time() if now is None else now:.3f} {os.getpid()} "
                       f"{tick} {state}\n", encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def read_beat(path: Path | str | None = None) -> dict | None:
    """The last beat, or None when there is no readable one. Never raises."""
    try:
        parts = Path(beat_path() if path is None else path).read_text(
            encoding="utf-8", errors="replace").split()
        return {"epoch": float(parts[0]), "pid": int(parts[1]),
                "tick": int(parts[2]), "state": parts[3] if len(parts) > 3 else ""}
    except (OSError, ValueError, IndexError):
        return None


def age(now: float | None = None, path: Path | str | None = None) -> float | None:
    """Seconds since the last beat, or None if the app has never beaten."""
    b = read_beat(path)
    return None if b is None else max(0.0, (time.time() if now is None else now)
                                      - b["epoch"])


# ------------------------------------------------- deliberate shutdown marker
def mark_stopped(reason: str = "shutdown", path: Path | str | None = None,
                 role: str = "main") -> bool:
    """Record that this instance was stopped on purpose.

    Recovery that undoes a deliberate shutdown is not recovery. The marker is only
    written by the paths that mean it — Ctrl-C, and the installer's own stop — and not
    by `atexit`, because an unhandled exception also unwinds to `atexit` and would
    otherwise silence the observer on the one occasion it is needed. `role` scopes the
    file for the same reason `beat` does (#67): a Ctrl-C on a headless preview is not
    a statement that the app was stopped on purpose, and writing the shared marker
    would hold the observer back from recovering the app the preview never owned.
    """
    p = stopped_path(role) if path is None else Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{time.time():.3f} {reason}\n", encoding="utf-8")
        return True
    except OSError:
        return False


def stopped_epoch(path: Path | str | None = None) -> float | None:
    try:
        return float(Path(stopped_path() if path is None else path).read_text(
            encoding="utf-8", errors="replace").split()[0])
    except (OSError, ValueError, IndexError):
        return None


# --------------------------------------------------------------- liveness
def pid_alive(pid: int) -> bool:
    """Is this pid still running? Read-only, and provably without termination rights.

    `os.kill(pid, 0)` is the obvious spelling and it is *wrong here*: on Windows any
    signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT terminates the process, so the
    idiomatic liveness check on POSIX is a way to kill an arbitrary pid on this
    platform. Asking for `PROCESS_QUERY_LIMITED_INFORMATION` alone cannot: if the
    handle opened, the rights to terminate were never granted to us.
    """
    if pid <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
    except (AttributeError, OSError):         # pragma: no cover - not Windows
        try:
            os.kill(pid, 0)                   # the POSIX spelling is safe there
            return True
        except OSError:
            return False
    h = k32.OpenProcess(_QUERY_LIMITED, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ budget
def _read_restarts(path: Path | str | None = None) -> list[float]:
    try:
        return [float(ln) for ln in Path(restarts_path() if path is None else path)
                .read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()]
    except (OSError, ValueError):
        return []


def _record_restart(now: float, path: Path | str | None = None) -> None:
    """Remember this restart, keeping only what the window can still see."""
    p = restarts_path() if path is None else Path(path)
    kept = [t for t in _read_restarts(p) + [now] if now - t <= WINDOW_S][-BUDGET:]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"{t:.3f}\n" for t in kept), encoding="utf-8")
    except OSError:
        pass


def _policy(now: float, beat: dict | None, stuck: str,
            paths: dict, record: bool) -> tuple[str, str]:
    """The shared tail of the policy: deliberate-stop, budget, backoff, verdict.

    Both the "the app is running but its beat is stale" case and the "no beat has
    ever been written" case end here, and that is the fix for the bypass: a
    failure *before* the first heartbeat is a failure of the same app and must
    face the same accounting. Returning `start` the moment no beat exists meant a
    deliberate stop was ignored (the observer relaunched an app the user had
    closed) and so was the restart budget (a start-up that fails every time was
    relaunched forever, with no backoff and nothing left in the log but a
    crash-loop). The only case that legitimately returns `start` with no history
    is a genuinely first run: no beat, no stop marker, no restarts on record.
    """
    epoch = None if beat is None else beat["epoch"]
    stop = stopped_epoch(paths.get("stopped"))
    if stop is not None and (epoch is None or stop >= epoch):
        # With no beat at all, any stop marker means the app was stopped on
        # purpose before it ever got to write one - a manual launch that was
        # then closed, or an installer stop right after install.
        when = ("with no heartbeat ever written" if epoch is None
                else f"{stop - epoch:.0f}s after the last beat")
        return ("hold", f"stopped on purpose ({when}); recovery must not undo that")

    recent = [t for t in _read_restarts(paths.get("restarts")) if now - t <= WINDOW_S]
    if len(recent) >= BUDGET:
        return ("permanent", f"{stuck}; {len(recent)} restarts in the last "
                             f"{WINDOW_S / 60:.0f} min — budget spent, leaving it alone "
                             f"for a human to read boot.log and log.log")
    if recent:
        wait = _backoff_for(len(recent))
        since = now - max(recent)
        if since < wait:
            return ("backoff", f"{stuck}; next try in {wait - since:.0f}s "
                               f"({len(recent)} restart(s) this window)")
    if record:
        _record_restart(now, paths.get("restarts"))
    alive = beat is not None and pid_alive(beat["pid"])
    return (("restart" if alive else "start"),
            f"{stuck}; attempt {len(recent) + 1} of {BUDGET} this window")


def _backoff_for(n: int) -> float:
    """How long to wait after `n` restarts already inside the window.

    One definition, used by both the verdict and the reason text, so the number a
    person reads in the log is the number the observer is actually waiting.
    """
    return BACKOFF_S * (2 ** n)


def decide(now: float | None = None, record: bool = True,
           paths: dict | None = None) -> tuple[str, str]:
    """What the observer should do: `(verdict, reason)`.

    `ok`         the loop is beating; nothing to do;
    `hold`       it stopped on purpose, and recovery must not undo that;
    `backoff`    it is stuck, but the last restart is too recent to try again yet;
    `permanent`  the budget for this window is spent — say so and leave it alone;
    `restart`    it is stuck with something wedged behind it (stale beat, live pid);
    `start`      it is simply not running (stale beat, dead pid) — *or* it has
                 never run at all and nothing on disk argues otherwise.

    Every branch reads the same three files, so the whole policy is testable without
    Task Scheduler, a panel, or a second process.

    Every branch also applies the *same* policy. The no-heartbeat case used to
    short-circuit to `start` before the stop marker and the restart budget were
    consulted at all, which is how `decide()` could return "start" for a
    deliberate stop and for the sixth consecutive crash of a start-up that never
    reached its first beat.
    """
    paths = paths or {}
    now = time.time() if now is None else now
    b = read_beat(paths.get("beat"))
    a = age(now, paths.get("beat"))

    if b is None:
        # No beat. Either it has never run, or it has been failing since before it
        # could write one — and only the files on disk can tell those apart.
        return _policy(now, None,
                       "no heartbeat has ever been written (the app has not "
                       "reached its first tick)",
                       paths, record)
    if a is not None and a <= STALL_S:
        # A recent beat outranks a stop marker: a healthy app that is beating now
        # is running, whatever a stale marker from an earlier run says.
        stop = stopped_epoch(paths.get("stopped"))
        if stop is None or stop < b["epoch"]:
            return ("ok", f"beat {a:.0f}s old, tick {b['tick']}, state {b['state'] or '-'}")
    alive = pid_alive(b["pid"])
    stuck = f"beat {a:.0f}s old (past {STALL_S:.0f}s), pid {b['pid']} " \
            f"{'is alive' if alive else 'is gone'}"
    return _policy(now, b, stuck, paths, record)


def main(argv: list[str] | None = None) -> int:
    """`python -m app.liveness decide` — one verdict word, then the reason.

    The observer is PowerShell, and the policy is here: the script asks, reads the
    first line, and acts. Keeping the decision in Python is what makes it testable
    offline, and keeping the *words* stable is what keeps the two in step.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    record = (argv or ["decide"])[0] == "decide"
    verdict, reason = decide(record=record)
    print(verdict)
    print(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
