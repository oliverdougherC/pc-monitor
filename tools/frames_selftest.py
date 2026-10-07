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

Later cases pin the two ways a capture can look alive while measuring nothing
(issues #10 and #13): a header with no *parsed* row must not report `ok`
(main.py reads that as "session live" / `capture=live`), a pipe busy with rows
nothing can be parsed from must not keep a process looking freshly measured
(freshness is `_last_parse`, not `_last_row`), and a reader that was retired by
`restart()` must not be able to publish into the generation it left behind —
not even through a direct `_ingest`.

The 2.x header below is copied verbatim from a live capture (see V2_HEADER), so
the test is pinned to the real stream shape. To check the real thing again, run
the app once with `frames.output_file` set and point this at that file:

    .venv\\Scripts\\python tools\\frames_selftest.py vendor\\presentmon\\capture.csv
"""
import io
import sys
import threading
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
          mode: str = "Composed: Flip", swap: str = "0x1ABC0000") -> bytes:
    """n presents at `fps`, one frame taking `hitch_ms`, as presentmon would print.

    `gpu_busy_pct` fills MsGPUBusy against MsGPUTime — the column pair the detector
    scores a process on — and `mode` chooses the PresentMode string, which is how the
    exclusive-flip flag gets exercised. `swap` is the SwapChainAddress, so one replay
    can hold several swapchains apart — which is exactly what a display-mode change
    does to a game process.
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
                vals.append(swap)
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


def junk_only(header: str, time_col: str, n: int = 400, **kw) -> bytes:
    """`n` rows in the real header's shape whose clock cannot be read.

    The stream shape is right, the pipe is busy, and not one row carries a
    number: a renamed column, a truncated writer, a driver emitting junk. This
    is the input that separates "rows are arriving" from "we are measuring" —
    `_last_row` stays fresh on it while `_last_parse` does not move at all.
    """
    blob = synth(header, time_col, n=n, **kw).decode("utf-8-sig")
    lines = blob.splitlines()
    i = header.split(",").index(time_col)
    out = [lines[0]]
    for line in lines[1:]:
        field = line.split(",")
        field[i] = "not-a-number"
        out.append(",".join(field))
    return ("\r\n".join(out) + "\r\n").encode()


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
    # A missing binary keeps the supervisor from ever spawning: this is a parse
    # test, and the real presentmon needs elevation it must not depend on.
    cfg["frames"]["path"] = "does-not-exist.exe"
    m = FrameMonitor(cfg, role="selftest")
    m.error = None
    m._phase = "watching"             # state() is about the stream, not the absent exe
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
    check("ok (after a parsed row)", m.ok, True)
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
    check("header seen", bool(legacy.header), True)
    # A header without the millisecond clock can never produce a number, so it
    # is not a live capture: `ok` used to latch True on ProcessID alone and the
    # heartbeat called this `live` forever (issue #10).
    check("ok (must be False: no CPUStartQPCTimeInMs)", legacy.ok, False)
    check("state", legacy.state, "bad-schema")
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
    # A header on its own is not a live capture: main.py reports "presentmon
    # session live" / `capture=live` straight off `ok`, and the old latch on
    # header receipt made a capture that parsed nothing report as live for the
    # whole schema/silence deadline (issue #10). Health is a parsed row.
    check("a header with no parsed row is not ok", q.ok, False)
    check("…and nothing was measured", q.parsed, 0)
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

    print("\ncase 6: swapchain recreation — a retired 240 fps chain must not shadow a live 60 fps one")
    # The counterexample from the issue: a game recreates its swapchain after a
    # display-mode change. The old chain keeps its dense 240 Hz history and the
    # process stays alive (so a per-pid sweep never reaches it); if selection
    # windows each chain against its own newest timestamp and takes the largest
    # sample count, the retired chain outvotes the live one forever and the
    # panel shows 240 fps for a game that is presenting 60.
    m6 = replay(synth(V2_HEADER, V2_TIME, n=240, fps=240.0, hitch_at=-1,
                      base_ms=1_000_000.0, swap="0xAAAA0000"))
    check("chain A fps before", round(float(m6.stats(4242).fps)), 240)
    m6._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=60, fps=60.0, hitch_at=-1,
                                base_ms=1_001_000.0, swap="0xBBBB0000")))
    st6 = m6.stats(4242)
    check("stats follows the new chain, promptly", round(float(st6.fps)), 60)
    check("new chain is live", st6.stale, False)
    check("presenters follows too", round(float(m6.presenters()[4242].fps)), 60)
    # And it stays followed: seconds later, with the old chain minutes stale by
    # the stream clock, the live chain must still own the numbers.
    m6._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1,
                                base_ms=1_002_000.0, swap="0xBBBB0000")))
    check("still the new chain seconds later", round(float(m6.stats(4242).fps)), 60)

    print("\ncase 7: several live chains — the frame rate is the busiest one, never the sum")
    m7 = replay(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1,
                      base_ms=1_000_000.0, swap="0xCCCC0000"))
    m7._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=288, fps=144.0, hitch_at=-1,
                                base_ms=1_000_003.5, swap="0xDDDD0000")))
    p7 = m7.presenters()[4242]
    check("busiest live chain is the fps", round(float(p7.fps)), 144)
    check("never the sum of chains", float(p7.fps) < 150.0, True)
    # Equal-rate chains: two windows presenting at the same rate report that
    # rate, not double it, whichever one the selection picks.
    m7b = replay(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1,
                       base_ms=1_000_000.0, swap="0x11110000"))
    m7b._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1,
                                 base_ms=1_000_008.3, swap="0x22220000")))
    check("two equal chains report 60, never 120", round(float(m7b.stats(4242).fps)), 60)

    print("\ncase 8: hundreds of recreations — rings and query cost must plateau")
    # A process that recreates its swapchain over and over (resize-heavy tooling,
    # some engines on mode churn) while one long-lived chain keeps presenting.
    # Retired chains are not the process's frame rate and must not be kept: a
    # sweep that only expires whole pids never reaches them, and memory plus the
    # per-query scan grow with session length.
    m8 = replay(synth(V2_HEADER, V2_TIME, n=8400, fps=60.0, hitch_at=-1,
                      base_ms=2_000_000.0, swap="0x11111111"))
    for i in range(300):
        m8._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=10, fps=240.0, hitch_at=-1,
                                    base_ms=2_000_000.0 + i * 100.0,
                                    swap=f"0x{i:08X}")))
    check("rings held during the storm", len([k for k in m8._rings if k[0] == 4242]), 301)
    # 110 s of stream later (the live chain carried the clock forward), every
    # retired chain is past the expire line while the process itself is fresh.
    m8._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=6600, fps=60.0, hitch_at=-1,
                                base_ms=2_140_000.0, swap="0x11111111")))
    pres8 = m8.presenters()          # this is where the sweep runs
    check("live chain survives its own process", round(float(pres8[4242].fps)), 60)
    check("obsolete chains expire independently of the pid",
          len([k for k in m8._rings if k[0] == 4242]), 1)
    check("stats still follows the live chain", round(float(m8.stats(4242).fps)), 60)

    print("\ncase 9: pid reuse — a new process must not inherit the old one's chains or name")
    # Windows reuses pids. The CSV carries no process creation time, so the exe
    # name changing under a live pid is the identity signal: the old process is
    # gone and its chains must never be candidates for the new one's fps.
    m9 = replay(synth(V2_HEADER, V2_TIME, n=240, fps=240.0, hitch_at=-1,
                      base_ms=1_000_000.0, swap="0xAAAA0000"))
    m9._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=60, fps=30.0, hitch_at=-1,
                                base_ms=1_002_000.0, name="notepad.exe",
                                swap="0xEEEE0000")))
    check("name follows the live process", m9.names().get(4242), "notepad.exe")
    check("stats is the new process's", round(float(m9.stats(4242).fps)), 30)
    pres9 = m9.presenters().get(4242)
    check("old game does not haunt detection",
          (pres9.name, round(float(pres9.fps))) if pres9 else None, ("notepad.exe", 30))

    print("\ncase 10: capture restart — rows from the dead child must not repopulate the new one")
    m10 = replay(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1))
    check("live before restart", round(float(m10.stats(4242).fps)), 60)
    m10.restart("test")              # what main.py does after a resume
    check("restart drops the old capture state", (len(m10._rings), m10._last_t), (0, 0.0))
    # The old child's last buffered rows arrive *after* the restart was requested.
    # They belong to the previous generation and must not touch the new one —
    # rings, counters, or the ok flag a stale header would set.
    m10._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1)),
                     gen=m10._gen - 1)
    check("stale-generation rows ignored", (m10.rows, m10.parsed, len(m10._rings)), (0, 0, 0))
    check("stale header did not claim the stream", m10.ok, False)
    m10._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1,
                                 base_ms=1_005_000.0)), gen=m10._gen)
    check("new-generation rows land", round(float(m10.stats(4242).fps)), 60)

    print("\ncase 11: bounded diagnostics and held state")
    # A child that crash-loops printing lines before its header used to grow the
    # stray-output list for the life of the process.
    junk = replay(("\r\n".join(f"noise {i}" for i in range(200)) + "\r\n").encode())
    check("stray output stays bounded", len(junk._out) <= 64, True)
    m11 = replay(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1,
                       base_ms=1_000_000.0))
    m11.stats(4242)                  # populates the held cache while live
    check("held populated while live", 4242 in m11._held, True)
    # The stream moves on for 40 s with the game gone: its held value is past
    # hold_s and can never be returned again, so the sweep must not keep it.
    m11._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=2400, fps=60.0, hitch_at=-1,
                                 pid=7, name="chrome.exe", base_ms=1_040_000.0)))
    # A held value older than hold_s is one stats() refuses to return, so the
    # sweep must not keep it. In a live system the wall clock ages it by itself;
    # in a replay we age the timestamp by hand, which is the same predicate.
    s4242, t4242 = m11._held[4242]
    m11._held[4242] = (s4242, t4242 - m11.hold_s - 1.0)
    m11.presenters()
    check("expired held entries are swept", 4242 in m11._held, False)
    # A pid that keeps presenting but is never queried again: its held entry
    # ages on the wall clock and must be swept too, or the cache grows per pid.
    m11.stats(7)
    check("held populated for the queried pid", 7 in m11._held, True)
    s7, t7 = m11._held[7]
    m11._held[7] = (s7, t7 - m11.hold_s - 1.0)   # as if nobody queried it for hold_s+1
    m11.presenters()
    check("never-queried held entries are swept", 7 in m11._held, False)

    print("\ncase 12: malformed-only rows — a busy pipe is not a measurement")
    # The issue's second clause: a valid 60 fps stream, then rows that keep
    # arriving and never parse (a renamed clock column, a half-written row, a
    # driver emitting junk). `_last_row` stays fresh because rows *are* arriving,
    # and stats()/presenters() used to age by exactly that — so the panel kept
    # reporting fps=60.0, stale=False, age=0.00s and the process stayed listed as
    # a presenter while nothing had been measured for minutes. Freshness has to
    # come from `_last_parse`: the pipe's activity is a fact about the pipe.
    m12 = replay(synth(V2_HEADER, V2_TIME, n=120, fps=60.0, hitch_at=-1))
    check("valid stream parses", m12.parsed, 120)
    m12._read_stream(_Stub(junk_only(V2_HEADER, V2_TIME, n=400,
                                     base_ms=1_010_000.0)))
    check("the malformed rows were all counted as rows", m12.rows, 520)
    check("…and none of them parsed", m12.parsed, 120)
    check("rows are arriving right now (the old freshness clock)",
          time.monotonic() - m12._last_row < 1.0, True)
    # In a live system the wall clock ages `_last_parse` by itself; a replay runs
    # in microseconds, so age it by hand. Same predicate, same field either way.
    m12._last_parse -= 5.0
    st12 = m12.stats(4242)
    if st12 is None:
        fails.append("stats() returned None inside hold_s")
        print("  FAIL stats() returned None inside hold_s")
    else:
        check("aged, not fresh", st12.stale, True)
        ok = 4.5 <= st12.age_s <= 5.5
        print(f"  {'ok  ' if ok else 'FAIL'} age follows the parse clock: "
              f"{st12.age_s:.2f}s (5 s of rows nothing could be parsed from)")
        if not ok:
            fails.append("aged by _last_parse")
    check("presenters drops the process nothing is measuring",
          sorted(m12.presenters()), [])

    print("\ncase 13: a retired generation cannot be published into — not even by _ingest")
    # Issue #13's own proof: restart() bumps the generation and clears the
    # capture, and a reader that had already passed its outer generation check
    # handed its row to _ingest, which committed under the lock without
    # re-checking. The cleared capture came back — rings=1, pids=1,
    # _last_t=1000000.0 — and the panel answered from a capture that had been
    # thrown away. The check belongs where the mutation is.
    m13 = replay(synth(V2_HEADER, V2_TIME, n=1, fps=60.0, hitch_at=-1,
                       base_ms=1_000_000.0))
    stale_gen = m13._gen
    stale_row = synth(V2_HEADER, V2_TIME, n=1, fps=60.0, hitch_at=-1,
                      base_ms=1_000_000.0).decode("utf-8-sig").splitlines()[1].split(",")
    stale_idx = {name: i for i, name in enumerate(V2_HEADER.split(","))}
    m13.restart("verify")
    check("restart cleared the capture",
          (len(m13._rings), dict(m13._pids), m13._last_t), (0, {}, 0.0))
    check("a retired generation's row is refused",
          m13._ingest(stale_row, stale_idx, stale_gen), False)
    check("…and it published nothing",
          (m13.parsed, len(m13._rings), dict(m13._pids), m13._last_t), (0, 0, {}, 0.0))
    # The positive control: the same row carrying the *current* generation lands.
    # A fence that refused everything would satisfy the check above for the wrong
    # reason.
    check("the current generation still ingests",
          m13._ingest(stale_row, stale_idx, m13._gen), True)
    check("…and that one landed",
          (m13.parsed, len(m13._rings), sorted(m13._pids), m13._last_t),
          (1, 1, [4242], 1_000_000.0))
    # The other half of atomicity: the *retirement* has to be taken under the
    # same lock the publications fence against. Bumping `_gen` outside it left a
    # window in which a query passed the fence, lost the race to restart()'s
    # clear, and then wrote its held value into the generation that replaced it.
    # Deterministic here: this thread holds the lock, so the restart thread is
    # parked at the bump until it is released.
    m13._read_stream(_Stub(synth(V2_HEADER, V2_TIME, n=60, fps=60.0, hitch_at=-1)))
    check("a capture to retire", len(m13._rings), 1)
    top = m13._gen
    started, done = threading.Event(), threading.Event()

    def retire() -> None:
        started.set()
        m13.restart("test")
        done.set()

    with m13._lock:
        worker = threading.Thread(target=retire, daemon=True)
        worker.start()
        started.wait(2.0)
        # Long enough for the worker to get as far as it can while this thread
        # holds the lock: it cannot finish (the clear needs the lock), but with
        # the bump outside the lock it would be visible here at once, and that is
        # exactly the window a late query used to slip a held value through.
        check("retirement is still in flight", done.wait(0.5), False)
        check("retirement waits for the lock", m13._gen, top)
        check("…and clears nothing behind its back", len(m13._rings), 1)
    check("retired once the lock is free", done.wait(2.0), True)
    worker.join(2.0)
    check("and the generation moved on", m13._gen, top + 1)
    check("the retired capture is gone",
          (len(m13._rings), dict(m13._pids), dict(m13._held)), (0, {}, {}))

    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        print(f"\ncase 14: recorded stream {p}")
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
