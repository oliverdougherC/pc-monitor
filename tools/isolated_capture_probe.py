"""One elevated run that answers: can PresentMon get frames on this machine at all?

    .venv\\Scripts\\python tools\\isolated_capture_probe.py

Runs the flag matrix (tools/presentmon_matrix.py) twice —

  PHASE A  with the production app and its capture child alive (concurrent),
  PHASE B  with the app stopped and no capture session left over —

then restarts the PCMonitor task and writes everything to tools/_capture_probe.txt
so a non-elevated shell can read it back.

Why both phases: the app logs "presentmon session live" and the panel never shows
a frame. The capture child's CPU sits dead while a game renders, i.e. the session
exists and receives no graphics events. Either something else owns the graphics
ETW provider (then no code change in this repo can fix it, and the app needs a
different source), or the way the session is taken over/reused on restart breaks
it (then the fix is here, in app/frames.py). Phase A vs phase B says which.

Keep a game actually rendering while this runs — no frames presented, no rows.
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import presentmon_matrix as M  # noqa: E402
from app.gamewatch import _foreground_info  # noqa: E402

LOG = ROOT / "tools" / "_capture_probe.txt"
SECS = 5.0


def echo(msg: str = "") -> None:
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def matrix(tag: str) -> list[dict]:
    echo(f"\n===== {tag} @ {time.strftime('%H:%M:%S')} =====")
    fg = _foreground_info()
    echo(f"foreground window: pid={fg[0]} name={fg[1]} covers={fg[2]} borderless={fg[3]}")
    echo("")
    out = []
    for label, args, session in M.CONFIGS:
        r = M.run(label, args, SECS, session)
        out.append(r)
        M.report(r, echo)
        time.sleep(0.5)
    total = sum(r.get("rows", 0) for r in out)
    echo(f"  --> {tag}: total FRAMES across configs = {total}")
    return out


def sh(cmd: str) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    return ((p.stdout or "") + (p.stderr or "")).strip()


def main() -> None:
    LOG.write_text("")
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    echo(f"elevated={admin}  python={sys.executable}  @ {time.strftime('%F %T')}")
    if not admin:
        echo("MUST be elevated — nothing past this line works. Run from an admin shell.")
        return

    echo("\nbefore: " + sh('tasklist /fi "IMAGENAME eq presentmon.exe" /fo csv /nh'))
    a = matrix("PHASE A: production app alive (its session concurrent)")

    echo("\nstopping the PCMonitor task and its capture child...")
    echo("  schtasks /end: " + (sh("schtasks /end /tn PCMonitor") or "ok"))
    import psutil
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "PC Monitor" in " ".join(p.info["cmdline"] or []) and "main.py" in \
                    " ".join(p.info["cmdline"] or []):
                echo(f"  kill {p.pid} {p.info['name']}")
                p.terminate()
        except psutil.Error:
            pass
    time.sleep(2)
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() == "presentmon.exe":
                echo(f"  kill leftover capture {p.pid}")
                p.terminate()
        except psutil.Error:
            pass
    time.sleep(3)
    echo("  after: " + sh('tasklist /fi "IMAGENAME eq presentmon.exe" /fo csv /nh'))

    b = matrix("PHASE B: no other present capture alive")

    echo("\nrestarting the app...")
    echo("  schtasks /run: " + (sh("schtasks /run /tn PCMonitor") or "ok"))

    echo("\n" + "#" * 72)
    echo("VERDICT")
    ok_a = [r["label"] for r in a if r.get("rows")]
    ok_b = [r["label"] for r in b if r.get("rows")]
    echo(f"  configs that produced frames with the app running : {ok_a or 'NONE'}")
    echo(f"  configs that produced frames with nothing else    : {ok_b or 'NONE'}")
    if not ok_a and ok_b:
        echo("  → Two captures cannot coexist here: the app's long-lived session is fine")
        echo("    on its own, so look at what the app's session collides with (liveview,")
        echo("    a leftover child, another overlay).")
    elif not ok_a and not ok_b:
        echo("  → No new session on this machine receives graphics events while the app's")
        echo("    capture is alive OR dead: something else owns the provider, or this")
        echo("    PresentMon build is incompatible with this OS/driver setup.")
    elif ok_b and not any("production" in x for x in ok_b):
        echo("  → Frames are available but NOT with the production flags: the fix is in")
        echo("    the command line app/frames.py passes to presentmon.")
    echo("DONE (app restarting; the panel needs ~15 s to wake)")


if __name__ == "__main__":
    main()
