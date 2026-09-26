"""Replay a recorded present stream through app/frames.py — no admin needed.

    .venv\\Scripts\\python tools\\frames_selftest.py

Two jobs:

  1. Prove the numbers. 180 presents at exactly 60 Hz with one 30 ms hitch go
     through the real `_read_stream`/`_ingest`/`presenters`/`stats`, and the
     panel values must come out as fps=60, frametime≈16.7 ms, lows present.
  2. Prove the guard. The same rows in the *1.x* column shape (no
     CPUStartQPCTimeInMs column) must land as "rows arrived, none parsed" in
     `stream_warning()` — that is the failure mode that otherwise shows up as a
     panel stuck at `--` with a healthy-looking log. It is not hypothetical: the
     parser used to look for a column name the pinned binary never prints, and
     this test passed anyway because the test's header was invented too.

The 2.x header below is copied verbatim from a live capture (see V2_HEADER), so
the test is pinned to the real stream shape. To check the real thing again, run
the app once with `frames.output_file` set and point this at that file:

    .venv\\Scripts\\python tools\\frames_selftest.py vendor\\presentmon\\capture.csv
"""
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")  # our tree first: vendor has its own main.py
sys.path.append("vendor/turing-smart-screen-python")
from app import config as cfgmod          # noqa: E402
from app.frames import FrameMonitor       # noqa: E402

# Reported column names and stream text are not always cp1252-safe when this is
# redirected into a file or a PowerShell pipe.
sys.stdout.reconfigure(errors="replace")

# PresentMon 2.x with --qpc_time_ms, copied verbatim from a live capture on this
# desk (vendor/presentmon/cap-live.csv, 2.5.1, 2026-09-24). This used to be
# composed from the documented metric names, and that is exactly how the parser
# came to read a column — CPUStartQPCTime — that this binary never emits: the
# panel showed `--` while 1500 frames a second went past. Keep it a fingerprint.
V2_HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,"
             "PresentFlags,AllowsTearing,PresentMode,TimeInSeconds,"
             "MsBetweenSimulationStart,MsBetweenPresents,MsBetweenDisplayChange,"
             "MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,"
             "CPUStartQPCTimeInMs,MsBetweenAppStart,MsCPUBusy,MsCPUWait,"
             "MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,MsAnimationError,"
             "AnimationTime,MsFlipDelay,MsAllInputToPhotonLatency,"
             "MsClickToPhotonLatency")
V2_TIME = "CPUStartQPCTimeInMs"
# PresentMon's 1.x shape (--v1_metrics): time lives in QPCTime, lowercase ms*.
# Deliberately unsupported — those ticks are not milliseconds, and silently
# scaling them wrong is worse than the honest `--` the guard reports.
V1_HEADER = ("Application,ProcessID,SwapChainAddress,Runtime,SyncInterval,PresentFlags,"
             "Dropped,TimeInSeconds,msInPresentAPI,msBetweenPresents,AllowsTearing,"
             "PresentMode,msUntilRenderComplete,msUntilDisplayed,msBetweenDisplayChange,"
             "msFlipDelay,QPCTime")


def synth(header: str, time_col: str, n: int = 180, fps: float = 60.0,
          hitch_at: int = 90, hitch_ms: float = 30.0, base_ms: float = 1_000_000.0,
          pid: int = 4242, name: str = "game.exe", gpu_busy_pct: float | None = None,
          mode: str = "Composed: Flip") -> bytes:
    """n presents at `fps`, one frame taking `hitch_ms`, as presentmon would print.

    `gpu_busy_pct` fills MsGPUBusy against MsGPUTime — the column pair the detector
    scores a process on — and `mode` chooses the PresentMode string, which is how the
    exclusive-flip flag gets exercised.
    """
    cols = header.split(",")
    step = 1000.0 / fps
    rows = []
    for i in range(n):
        ms = hitch_ms if i == hitch_at else step
        vals = []
        for c in cols:
            if c == "Application":
                vals.append(name)
            elif c == "ProcessID":
                vals.append(str(pid))
            elif c in ("SwapChainAddress", "SwapChainAddress "):
                vals.append("0x1ABC0000")
            elif c in ("PresentRuntime", "Runtime"):
                vals.append("DX12")
            elif c in ("SyncInterval", "PresentFlags", "AllowsTearing", "Dropped"):
                vals.append("0")
            elif c == "PresentMode":
                vals.append(mode)
            elif c == time_col:
                vals.append(f"{base_ms + i * step:.4f}")
            elif c in ("MsBetweenPresents", "msBetweenPresents"):
                vals.append(f"{ms:.4f}")
            elif c in ("MsBetweenDisplayChange", "msBetweenDisplayChange"):
                vals.append(f"{ms:.4f}")
            elif c == "MsGPUTime":
                vals.append(f"{step:.4f}")
            elif c == "MsGPUBusy":
                vals.append("NA" if gpu_busy_pct is None
                            else f"{gpu_busy_pct / 100.0 * step:.4f}")
            elif c in ("TimeInSeconds", "QPCTime"):
                vals.append(f"{(base_ms + i * step) / 1000.0:.6f}")
            else:
                vals.append("NA")
        rows.append(",".join(vals))
    # utf-8-sig supplies the BOM, exactly like presentmon's `w,ccs=UTF-8` file.
    return (header + "\r\n" + "\r\n".join(rows) + "\r\n").encode("utf-8-sig")


class _Stub:
    """Just enough Popen for _read_stream."""

    def __init__(self, blob: bytes):
        self.stdout = io.BytesIO(blob)
        self.pid = -1

    def wait(self) -> int:
        return 0

    def poll(self) -> int | None:
        return 0


def replay(blob: bytes) -> FrameMonitor:
    cfg = cfgmod.load()
    # A missing binary keeps __init__ from spawning anything: this is a parse
    # test, and the real presentmon needs elevation it must not depend on.
    cfg["frames"]["path"] = "does-not-exist.exe"
    m = FrameMonitor(cfg, role="selftest")
    m.error = None
    # Pretend the child came up 30 s ago: the liveness guards stay quiet until a
    # stream has had a chance to say something, which is the point of them.
    m._spawned = time.monotonic() - 30.0
    m._read_stream(_Stub(blob))       # same code path the supervisor thread uses
    return m


def main() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        ok = (abs(got - want) <= abs(want) * 0.02) if isinstance(got, float) else got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")
        if not ok:
            fails.append(name)

    print("case 1: 2.x stream, 180 presents at 60 Hz with one 30 ms hitch")
    m = replay(synth(V2_HEADER, V2_TIME))
    check("ok (header seen)", m.ok, True)
    check("rows", m.rows, 180)
    check("parsed", m.parsed, 180)
    pres = m.presenters()
    check("presenters", sorted(pres), [4242])
    check("presenter fps", pres[4242].fps if pres else -1.0, 60.0)
    st = m.stats(4242)
    if st is None:
        fails.append("stats() returned None")
        print("  FAIL stats() returned None")
    else:
        check("stats.fps", float(st.fps), 60.0)
        check("stats.latency_ms", float(st.latency_ms), 1000.0 / 60.0)
        check("stats.low1_pct present", st.low1_pct is not None, True)
        check("stats.low01_pct present", st.low01_pct is not None, True)
        if st.low1_pct:
            slow = 1000.0 / float(st.low1_pct)
            ok = 16.0 <= slow <= 31.0
            print(f"  {'ok  ' if ok else 'FAIL'} low1 is the slow-frame end: "
                  f"p99 frametime {slow:.1f} ms")
            if not ok:
                fails.append("low1 range")
    m.observe(busy=False, dt=1.0)
    check("no warning on a good stream", m.stream_warning(), None)

    print("\ncase 2: 1.x column shape — time is not where the parser looks")
    legacy = replay(synth(V1_HEADER, "QPCTime"))
    check("header seen", legacy.ok, True)
    check("rows arrived", legacy.rows, 180)
    check("parsed (must be 0: no CPUStartQPCTimeInMs column)", legacy.parsed, 0)
    legacy.silent_busy_s = 99.0
    w = legacy.stream_warning()
    caught = bool(w) and "none parsed" in w
    print(f"  {'ok  ' if caught else 'FAIL'} guard fires: {w}")
    if not caught:
        fails.append("guard text")

    print("\ncase 3: silent stream while the machine renders")
    q = replay(synth(V2_HEADER, V2_TIME, n=0))
    q.observe(busy=True, dt=25.0)
    w2 = q.stream_warning()
    caught2 = bool(w2) and "no rows from presentmon" in w2
    print(f"  {'ok  ' if caught2 else 'FAIL'} guard fires: {w2}")
    if not caught2:
        fails.append("silent guard")

    print("\ncase 4: GPU busyness and present mode — the detector's evidence")
    g = replay(synth(V2_HEADER, V2_TIME, n=120, fps=48.0, hitch_at=-1,
                     gpu_busy_pct=100.0, mode="Hardware Composed: Independent Flip"))
    gp = g.presenters().get(4242)
    if gp is None:
        fails.append("gpu presenter")
        print("  FAIL no presenter")
    else:
        check("fps", round(float(gp.fps)), 48)
        check("gpu busy from MsGPUBusy/MsGPUTime", round(float(gp.gpu)), 100)
        # Measured on this desk: a real game at 48 fps presents as Independent Flip
        # with the GPU ~100% busy. That path is not exclusive flip, so classifying
        # exclusivity must never be what gates the game evidence.
        check("independent flip is not exclusive", bool(gp.exclusive), False)
        check("stats carries gpu", round(float(g.stats(4242).gpu_pct)), 100)
    ex = replay(synth(V2_HEADER, V2_TIME, n=120, fps=48.0, hitch_at=-1,
                      gpu_busy_pct=100.0, mode="Hardware: Legacy Flip"))
    check("legacy flip counts as exclusive",
          bool(ex.presenters().get(4242).exclusive), True)
    na = replay(synth(V2_HEADER, V2_TIME, n=120, fps=48.0, hitch_at=-1))
    check("gpu None when the columns are NA", na.presenters().get(4242).gpu, None)

    print("\ncase 5: the game stops presenting while the stream moves on")
    m5 = replay(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1))
    live = m5.stats(4242)
    check("live before", live.stale, False)
    # 2 s of someone else's frames starting 3 s after the game's last: the game's
    # numbers must stay on the panel, marked no-longer-live, aged by the *stream*
    # clock (its newest frame is ~5 s behind the stream's newest).
    m5._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1,
                                pid=7, name="chrome.exe", base_ms=1_000_000.0 + 3_000.0)))
    held5 = m5.stats(4242)
    if held5 is None:
        fails.append("held within hold_s")
        print("  FAIL stats() dropped the game inside the hold window")
    else:
        check("value is still the game's", round(float(held5.fps)), 60)
        check("marked stale", held5.stale, True)
        ok = 2.5 <= held5.age_s <= 3.5
        print(f"  {'ok  ' if ok else 'FAIL'} age follows the stream clock: "
              f"{held5.age_s:.1f}s (game's newest frame is 3 s behind the stream's)")
        if not ok:
            fails.append("held age")
    # Past hold_s the number is a memory, not a measurement: the panel goes to `--`.
    m5._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=1200, fps=60.0, hitch_at=-1,
                                pid=7, name="chrome.exe", base_ms=1_000_000.0 + 20_000.0)))
    check("held value expires after hold_s", m5.stats(4242), None)
    check("the new presenter is untouched", round(float(m5.stats(7).fps)), 60)

    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        print(f"\ncase 6: recorded stream {p}")
        blob = p.read_bytes()
        r = replay(blob)
        print(f"  header : {','.join(r.header[:14])}…")
        print(f"  rows   : {r.rows}  parsed: {r.parsed}  presenters: "
              f"{ {p.pid: round(p.fps, 1) for p in r.presenters().values()} }")
        if r.parsed:
            busiest = max(r.presenters().values(), key=lambda x: x.fps, default=None)
            if busiest:
                print(f"  stats  : {r.stats(busiest.pid)}")

    print("\n" + ("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
