"""Diagnostic: dump what the PresentMon stream *actually* contains, verbatim.

    .venv\\Scripts\\python tools\\frames_diag.py [seconds] [outfile]

Needs Administrator (ETW kernel session). Unlike tools/frames_probe.py this one
also keeps the **raw text** of the stream, so the three ways "the panel never
shows fps" can happen are told apart by evidence instead of guessing:

  1. no session / no rows at all          → presentmon or privilege problem
  2. rows, but the parser finds no time   → CSV column-name mismatch (header is
     column or every value is NA             printed verbatim for exactly this)
  3. rows parse fine, but the pid the
     panel asks about isn't in them       → detection/pid mismatch

Every second it prints the foreground window (pid, name, covers-its-monitor,
borderless — the inputs `gamewatch` uses), every presenter the stream reports,
the Steam AppID of each, what `GameWatch._pick` would choose, and `stats()` for
that pid.

Runs alongside the app: it claims its own ETW session role ("diag"), so it never
stops the production capture.
"""
import sys, time
sys.path.insert(0, ".")  # our tree first: vendor has its own main.py
sys.path.append("vendor/turing-smart-screen-python")
from app import config as cfgmod
from app.frames import FrameMonitor
from app.gamewatch import _foreground_info
from app.steamid import SteamIdentity

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 15
out_path = sys.argv[2] if len(sys.argv) > 2 else "tools/frames_diag.txt"
_out = open(out_path, "w", encoding="utf-8")


def echo(msg: str = "") -> None:
    print(msg, flush=True)
    _out.write(msg + "\n")
    _out.flush()


class DiagMonitor(FrameMonitor):
    """FrameMonitor + a verbatim copy of the stream, plus the parsed header."""

    def __init__(self, cfg, role="diag"):
        super().__init__(cfg, role=role)
        self.header: list[str] = []
        self.raw_rows: list[str] = []

    def _read_stream(self, proc, gen=None):   # gen: per-spawn guard, see app/frames.py
        import csv, io
        stream = io.TextIOWrapper(proc.stdout, encoding="utf-8-sig",
                                  errors="replace", newline="")
        reader = csv.reader(stream)
        idx = None
        for row in reader:
            if not row:
                continue
            line = ",".join(row)
            if idx is None:
                if "ProcessID" not in row:
                    echo(f"  [pre-header line] {line[:200]}")
                    continue
                idx = {name: i for i, name in enumerate(row)}
                self.header = row
                self.ok = True
                echo("CSV HEADER (verbatim):")
                echo("  " + ",".join(row))
                echo(f"  {len(row)} columns; ProcessID at {idx['ProcessID']}")
                continue
            # The generation is passed explicitly rather than defaulted away: `_ingest`
            # refuses to publish into a retired generation, and a diagnostic that
            # silently opted out of that fence would be the one caller able to
            # repopulate rings a `restart()` had just cleared — the very hole (#13)
            # the fence exists to close.
            self._ingest(row, idx, gen)
            if len(self.raw_rows) < 8:
                self.raw_rows.append(line)
                echo(f"  [row] {line[:260]}")


import ctypes  # noqa: E402
admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
echo(f"elevated={admin}  python={sys.executable}")

cfg = cfgmod.load()
cfg.setdefault("frames", {})["min_present_fps"] = 0.0   # list *every* presenter
mon = DiagMonitor(cfg, role="diag")
steam = SteamIdentity()
echo(f"exe={mon._exe} exists_session={mon.session_name}")
echo(f"waiting up to 12 s for the stream to come up…")

t_end = time.monotonic() + secs
while time.monotonic() < t_end:
    time.sleep(1.0)
    if not mon.ok:
        echo(f"STREAM NOT UP: ok={mon.ok} error={mon.error} "
             f"stray={mon._out[-3:]}")
        continue
    if not mon.header:
        continue
    hi = {n.lower(): i for i, n in enumerate(mon.header)}
    if not hasattr(mon, "_reported"):
        mon._reported = True
        want = ["cpustartqpctime", "cpustarttime", "msbetweenpresents",
                "msbetweendisplaychange", "swapchainaddress", "application",
                "processid", "timeinms", "presentmode"]
        echo("\nCOLUMN LOOKUP (what app/frames.py asks the header for):")
        for w in want:
            echo(f"  {w:<24} -> {'col ' + str(hi[w]) if w in hi else 'ABSENT'}")
        echo(f"  time column used by _ingest: "
             f"{'CPUStartQPCTime' if 'cpustartqpctime' in hi else ('CPUStartTime' if 'cpustarttime' in hi else 'NONE -> row[-1]')}\n")

    pres = mon.presenters()
    fg_pid, fg_name, covers, borderless = _foreground_info()
    fg_apps = {p.lower() for p in cfg["game"]["ignore"]}
    echo(f"[tick] presenters={len(pres)} foreground={fg_pid} ({fg_name}) "
         f"covers={covers} borderless={borderless} "
         f"ignored={fg_name in fg_apps if fg_name else '-'}")
    for p in sorted(pres.values(), key=lambda x: -x.fps)[:12]:
        try:
            real = __import__("psutil").Process(p.pid).name()
        except Exception:  # noqa: BLE001
            real = "?"
        tag = steam.appid(p.pid)
        echo(f"    pid={p.pid:<7} fps={p.fps:7.1f} age={p.age:5.2f}s "
             f"stream_name={p.name:<22} psutil={real:<22} "
             f"{'app=' + tag if tag else ''}")
    pick = fg_pid if fg_pid in pres else None
    echo(f"    gamewatch would pick: {pick}")
    if pick:
        st = mon.stats(pick)
        echo(f"    stats({pick}) = {st}")
    rows_for_fg = sum(1 for (p, _s) in mon._rings if p == fg_pid)
    echo(f"    rings for foreground pid: {rows_for_fg}  "
         f"(total rings={len(mon._rings)}, known pids={len(mon._pids)})")

echo("\nDONE")
if not mon.ok:
    echo(f"final: ok={mon.ok} error={mon.error}")
mon.close()
_out.close()
