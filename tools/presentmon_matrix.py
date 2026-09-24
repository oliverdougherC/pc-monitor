"""Flag-matrix probe: which PresentMon invocation actually yields frames?

    .venv\\Scripts\\python tools\\presentmon_matrix.py [seconds_per_config]

Needs Administrator. Runs several presentmon command lines back to back against
the live system and reports, for each, whether a CSV header arrived, how many
data rows came through, which processes they came from, and the first row
verbatim. The CSV is counted raw — nothing from `app/frames.py` is involved — so
this is the tool that separates

  * "our flags are wrong"            (one config yields rows, ours doesn't)
  * "our parser is wrong"            (every config yields rows, panel still `--`)
  * "the session never gets events"  (every config is starved: an ETW/graphics
                                     provider problem on this machine, or the
                                     session name being reused after a stop)

Session names are per config and every spawn is `--timed` +
`--terminate_after_timed`, so a crashed probe cannot leave an ETW session behind.
The last config deliberately re-uses the first config's name *after* that session
has been stopped, because "take over a name that was already used" is exactly
what the app does on every restart (`--stop_existing_session`).
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = str(ROOT / "vendor" / "presentmon" / "presentmon.exe")
BASE = ["--output_stdout", "--no_console_stats"]
PRODUCTION = BASE + ["--qpc_time_ms", "--exclude_dropped"]

CONFIGS = [
    ("production   (as app/frames.py spawns it)", PRODUCTION, "PCMonitor-mtx1"),
    ("no-exclude-dropped",                        BASE + ["--qpc_time_ms"], "PCMonitor-mtx2"),
    ("minimal      (stdout only)",                list(BASE), "PCMonitor-mtx3"),
    ("v1_metrics   (1.x CSV shape)",              PRODUCTION + ["--v1_metrics"], "PCMonitor-mtx4"),
    ("production   (name re-used after stop)",    PRODUCTION, "PCMonitor-mtx1"),
]


def echo(msg: str = "") -> None:
    print(msg, flush=True)


def run(label: str, args: list[str], secs: float, session: str = "PCMonitor-matrix") -> dict:
    cmd = [EXE] + args + ["--session_name", session, "--stop_existing_session",
                          "--timed", str(int(secs) + 1), "--terminate_after_timed"]
    lines: list[str] = []
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=0x08000000, bufsize=0)
    except OSError as e:
        return {"label": label, "session": session, "error": f"spawn failed: {e}", "rows": 0}

    def pump():
        try:
            for raw in iter(p.stdout.readline, b""):
                lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except Exception as e:  # noqa: BLE001
            lines.append(f"# read error: {e!r}")

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    deadline = time.monotonic() + secs + 4   # presentmon self-exits at --timed + 1
    while time.monotonic() < deadline:
        if p.poll() is not None and not t.is_alive():
            break
        time.sleep(0.2)
    t.join(1.0)
    rc = p.poll()
    if rc is None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            pass

    header = next((l for l in lines if l.startswith("Application,")), None)
    cols = {n: i for i, n in enumerate(header.split(","))} if header else {}
    data = [l for l in lines
            if l != header and l.count(",") >= 5
            and not l.startswith(("Application", "warning", "error", " ", "#"))]
    apps: dict[str, int] = {}
    if cols:
        for l in data:
            parts = l.split(",")
            i = cols.get("Application", 0)
            if i < len(parts):
                apps[parts[i]] = apps.get(parts[i], 0) + 1
    return {"label": label, "session": session,
            "args": " ".join(a for a in args if a != "--output_stdout"),
            "exit": rc, "lines": len(lines), "rows": len(data),
            "fps": round(len(data) / max(secs, 0.1), 1),
            "header": header, "apps": apps,
            "sample": data[0] if data else None,
            "noise": [l for l in lines if l[:1] in "weS#"][:3]}


def report(r: dict, echo=print) -> None:
    echo(f"  [{r['label']}]  session={r.get('session')}")
    echo(f"      args   : {r.get('args', r.get('error', ''))}")
    echo(f"      exit   : {r.get('exit')}   lines={r.get('lines')}   "
         f"FRAMES={r.get('rows')} ({r.get('fps')} rows/s)")
    if r.get("error"):
        echo(f"      ERROR  : {r['error']}")
    if r.get("header"):
        echo(f"      header : {r['header']}")
    if r.get("apps"):
        echo(f"      sources: " + ", ".join(f"{k}×{v}" for k, v in
                                            sorted(r["apps"].items(), key=lambda x: -x[1])[:6]))
    if r.get("sample"):
        echo(f"      first  : {r['sample'][:300]}")
    for n in (r.get("noise") or []):
        echo(f"      note   : {n[:190]}")
    if not r.get("rows"):
        echo("      >>> NO FRAMES")
    echo()


def main(seconds: float | None = None) -> list[dict]:
    import ctypes
    secs = float(seconds or (sys.argv[1] if len(sys.argv) > 1 else 5.0))
    echo(f"elevated={bool(ctypes.windll.shell32.IsUserAnAdmin())}  exe={EXE}  "
         f"{secs:.0f}s per config")
    if not Path(EXE).exists():
        echo("presentmon.exe missing — run tools/fetch_presentmon.ps1")
        return []
    echo()
    results = []
    for label, args, session in CONFIGS:
        r = run(label, args, secs, session)
        results.append(r)
        report(r, echo)
        time.sleep(0.5)
    echo("=" * 72)
    for r in results:
        echo(f"{r['label']:<42} FRAMES={r.get('rows', 0)}")
    if all(not r.get("rows") for r in results):
        echo("\nEvery invocation is starved — the CSV never even starts. That is an ETW")
        echo("problem (no graphics events reach a new session on this machine), not a")
        echo("flag or parsing problem.")
    return results


if __name__ == "__main__":
    main()
