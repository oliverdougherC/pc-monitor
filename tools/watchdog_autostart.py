#!/usr/bin/env pythonw
"""Restart the panel app when its own heartbeat stops: the observer it cannot be for itself.

The policy is unchanged and lives in app/liveness.py: `decide()` answers
(ok / hold / backoff / permanent / restart / start) plus a reason, and this script
only acts on that word. What it replaces is the *carrier*: the task action used to
be powershell.exe, and a console program run by Task Scheduler in the user session
gets a visible console for its whole run - every five minutes, a flash of a window
short enough to start an hour-long investigation. pythonw carries no console at
all, and the policy was always Python, so the observer now answers in its own
interpreter: no child process, no window, no fast path needed (the fast path only
ever existed to dodge a ~2 s venv startup *inside* that visible console).

Two rules this script keeps to, same as the PowerShell observer before it:

  * It touches the task, never a process. No image-name match, no command-line
    match: it stops and starts the PCMonitor task by name through schtasks.
  * `hold` and `permanent` both mean "do nothing", and both are still written
    down. A watchdog that quietly stops trying is indistinguishable from one
    that is not needed, and the difference is the whole reason to read the file.

Register it with tools/install_autostart.ps1, which also unregisters it again.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_NAME = sys.argv[1] if len(sys.argv) > 1 else "PCMonitor"
LOG = ROOT / "watchdog.log"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW, for schtasks


def log(line: str) -> None:
    # Only incidents land in the file, so it stays short enough to read in one go.
    # Bounded the blunt way: one generation of history is enough to answer "when
    # did this start happening", and a log that grows forever is its own outage.
    try:
        try:
            if LOG.exists() and LOG.stat().st_size > 262144:
                os.replace(LOG, Path(str(LOG) + ".old"))
        except OSError:
            pass
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except Exception:
        # Nowhere left to report a logging failure to. Deliberately silent, and
        # the reason is on the line above: this is not allowed to be the thing
        # that fails.
        pass


def schtasks(*verbs: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *verbs, "/TN", TASK_NAME],
                          capture_output=True, text=True, timeout=20,
                          creationflags=NO_WINDOW)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    try:
        from app.liveness import decide
    except Exception as e:  # noqa: BLE001 - a broken venv is reported, not acted on
        log(f"could not import app.liveness ({type(e).__name__}: {e}) - doing nothing")
        return 0

    try:
        q = schtasks("/Query")
    except Exception as e:  # noqa: BLE001
        log(f"could not query task {TASK_NAME} ({type(e).__name__}: {e}) - doing nothing")
        return 0
    if q.returncode != 0:
        # The app is not installed (or was removed). Its heartbeat going stale is
        # the expected answer, and restarting it would be the watchdog undoing an
        # uninstall.
        return 0

    try:
        verdict, reason = decide()
    except Exception as e:  # noqa: BLE001
        log(f"could not ask app.liveness ({type(e).__name__}: {e}) - doing nothing")
        return 0

    verdict = (verdict or "").strip().lower()
    if verdict == "ok":
        return 0                              # the common case: no line, no action
    if verdict in ("hold", "backoff"):
        log(f"{verdict}: {reason}")
        return 0
    if verdict == "permanent":
        log(f"PERMANENT: {reason}")
        return 0
    if verdict in ("restart", "start"):
        log(f"{verdict} : {reason}")
        # Stop first: 'restart' means something is wedged behind a stale beat, and
        # starting a task Windows still believes is running would do nothing at all
        # (the task settings say IgnoreNew). Best-effort, like the -SilentlyContinue
        # the PowerShell observer used.
        try:
            schtasks("/End")
        except Exception:  # noqa: BLE001
            pass
        try:
            r = schtasks("/Run")
            if r.returncode != 0:
                log(f"could not start task {TASK_NAME} (exit {r.returncode}): "
                    f"{(r.stderr or r.stdout or '').strip()[:200]}")
                return 0
            log(f"{verdict} issued for task {TASK_NAME}")
        except Exception as e:  # noqa: BLE001
            log(f"could not start task {TASK_NAME}: {type(e).__name__}: {e}")
        return 0
    log(f"unknown verdict '{verdict}' - doing nothing")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - pythonw has no console: die into the log, never silently
        log(f"watchdog crashed: {type(e).__name__}: {e}")
        sys.exit(0)