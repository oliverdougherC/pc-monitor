"""Debug helper: show what the PresentMon present stream actually reports.

    .venv\\Scripts\\python tools\\frames_probe.py [seconds] [min_fps]

Needs Administrator for the ETW session; without it it prints the honest error
and exits non-zero. Prints every presenter above `min_fps` each second, plus
full stats for the busiest one (preferring a Steam-identified process).

Safe to run while main.py is up: the probe claims its own ETW session role
("PCMonitor-probe"), so it never stops the production app's capture — sessions
are named per role and each one takes over only its own.
"""
import sys, time
sys.path.insert(0, ".")  # our tree first: vendor has its own main.py
sys.path.append("vendor/turing-smart-screen-python")
from app import config as cfgmod
from app.frames import FrameMonitor
from app.steamid import SteamIdentity

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 8
cfg = cfgmod.load()
cfg.setdefault("frames", {})["min_present_fps"] = float(sys.argv[2]) if len(sys.argv) > 2 else 0
mon = FrameMonitor(cfg, role="probe")
steam = SteamIdentity()
for i in range(secs):
    time.sleep(1.0)
    if not mon.ok and mon.error:
        print(f"FRAMES UNAVAILABLE: {mon.error}")
        sys.exit(1)
    if not mon.ok:
        print(f"[{i}] waiting for presentmon header…")
        continue
    pres = mon.presenters()
    rows = sorted(pres.values(), key=lambda p: -p.fps)
    print(f"[{i}] ok={mon.ok} presenters={len(rows)}")
    for p in rows[:12]:
        tag = steam.appid(p.pid)
        print(f"    pid={p.pid:<7} fps={p.fps:6.1f} age={p.age:4.2f}s "
              f"{'app=' + tag + ' ' if tag else ''}{p.name}")
    if rows:
        best = max(rows, key=lambda p: (bool(steam.appid(p.pid)), p.fps))
        st = mon.stats(best.pid)
        if st:
            print(f"    stats({best.pid}): fps={st.fps:.0f} "
                  f"frametime={st.latency_ms:.2f}ms low1={st.low1_pct} low01={st.low01_pct}")
print("done")
