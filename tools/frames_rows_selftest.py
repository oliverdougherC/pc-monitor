"""Malformed rows are one row's problem, never the stream's - and a reader
that dies never waits on the child that outlived it.

    .venv\\Scripts\\python tools\\frames_rows_selftest.py

Two wedges, both real at this baseline, both from app/frames.py:

  1. `_ingest` position-checked pid and the time column, then indexed
     SwapChainAddress, PresentMode and Application straight through. The
     issue's minimal shape - header ProcessID=0, CPUStartQPCTimeInMs=1,
     SwapChainAddress=2, row ['123', '1000'] - raised IndexError out of the
     reader, which the supervisor caught and silently discarded...
  2. ...and then called proc.wait() on the still-running child. Nobody was
     draining its stdout any more, so it filled the pipe buffer, blocked
     mid-write, and the wait became the wedge: supervisor and child both
     parked forever, capture still reported live.

Cases 1-5 replay row shapes through the real `_read_stream`/`_ingest`: the
truncated row (must not escape), a flood of them (rejected individually,
diagnostics bounded), NaN/±Inf clocks and +Inf frametimes (dropped, never
allowed into _last_t or the rings), reordered/missing columns, and a BOM
with stray pre-header chatter. Case 6 drives the real supervisor with a
fake child whose pipe faults mid-stream while the child stubbornly ignores
terminate: every wait must carry a bound, the escalation must reach kill,
the child must end up reaped (never an orphan), and a fresh capture must
appear within a deadline.
"""
import io
import math
import subprocess
import sys
import threading
import time

sys.path.insert(0, ".")   # our tree first: vendor has its own main.py
sys.path.insert(0, "tools")
sys.path.append("vendor/turing-smart-screen-python")
from app import frames as frames_mod      # noqa: E402
from app.frames import FrameMonitor       # noqa: E402
from frames_selftest import V2_HEADER, V2_TIME, replay, synth  # noqa: E402

sys.stdout.reconfigure(errors="replace")

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = (abs(got - want) <= abs(want) * 0.02) if isinstance(got, float) else got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def blob(*lines: str) -> bytes:
    """The lines as presentmon would print them: utf-8-sig BOM, CRLF."""
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def case(name: str, fn) -> None:
    print(f"\ncase {name}: {fn.__doc__.strip().splitlines()[0]}")
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - a raising case is a failing case
        FAILS.append(f"{name} raised {e!r}")
        print(f"  FAIL {name} raised {e!r}")


# ------------------------------------------------------------------ row shapes

def case_1_truncated_row():
    """the issue's minimal shape: valid pid+time, row ends before SwapChainAddress"""
    hdr = "ProcessID,CPUStartQPCTimeInMs,SwapChainAddress"
    m = replay(blob(hdr, "4242,1000.0,0xAB", "123,1000", "4242,1033.3333,0xAB"))
    check("rows counted", m.rows, 3)
    check("valid rows around the bad one parsed", m.parsed, 2)
    check("the bad row rejected individually", m.bad_rows, 1)
    check("one diagnostic kept", len(m._bad), 1)
    st = m.stats(4242)
    check_true("valid rows stay usable (stats)", st is not None, f"stats={st}")
    if st is not None:
        check("fps from the surviving rows", float(st.fps), 30.0)


def case_2_flood_is_bounded():
    """a flood of malformed rows: every one rejected, diagnostics capped"""
    hdr = "ProcessID,CPUStartQPCTimeInMs,SwapChainAddress"
    lines = [hdr, "4242,1000.0,0xAB"] + ["123,1000"] * 400 + ["4242,1033.3333,0xAB"]
    m = replay(blob(*lines))
    check("rows", m.rows, 402)
    check("parsed (the stream survived)", m.parsed, 2)
    check("counted", m.bad_rows, 400)
    check("stored samples bounded", len(m._bad), 3)
    q = replay(blob(hdr, *["123,1000"] * 5))
    w = q.stream_warning()
    check_true("none-parsed warning quotes a malformed row",
               bool(w) and "rejected as malformed" in w and "123,1000" in w,
               f"warning={w!r}")


def case_3_non_finite():
    """NaN/±Inf clocks and +Inf frametimes never reach the numbers"""
    hdr = "ProcessID,CPUStartQPCTimeInMs,MsBetweenPresents"
    m = replay(blob(hdr,
                    "7,1000.0,16.6",
                    "7,NaN,16.6",
                    "7,inf,16.6",
                    "7,-inf,16.6",
                    "7,1e999,16.6",
                    "7,1033.3333,inf"))
    check("finite rows parsed", m.parsed, 2)
    check("non-finite clocks rejected", m.bad_rows, 4)
    check_true("the stream clock stayed finite",
               math.isfinite(m._last_t) and m._last_t == 1033.3333,
               f"_last_t={m._last_t!r}")
    poison = [v for ring in m._rings.values() for s in ring
              for v in s[:4] if isinstance(v, float) and not math.isfinite(v)]
    check("no non-finite value in any ring", poison, [])
    st = m.stats(7)
    check_true("stats survive the +Inf frametime row", st is not None, f"stats={st}")
    check_true("+Inf frametime never rode the median",
               st is not None and st.latency_ms is not None
               and float(st.latency_ms) == 16.6,
               f"latency={st.latency_ms if st else None!r}")


def case_4_reordered_columns():
    """a different but complete schema still works; absent columns default"""
    hdr = "CPUStartQPCTimeInMs,ProcessID"       # swapped, no SwapChainAddress
    m = replay(blob(hdr, "1000.0,4242"))
    check("parsed", m.parsed, 1)
    check("missing swapchain column defaults", list(m._rings), [(4242, "-")])
    check("missing application column defaults", m.names()[4242], "?")


def case_5_bom_and_stray():
    """BOM plus pre-header chatter: the header is still found"""
    raw = (b"presentmon: starting session\r\n"
           + V2_HEADER.encode("utf-8") + b"\r\n"
           + synth(V2_HEADER, V2_TIME, n=3).decode("utf-8-sig")
           .encode("utf-8"))
    m = replay(raw)
    check("stray line kept for the error text", m._out, ["presentmon: starting session"])
    check("rows parsed after the header", m.parsed, 3)


# ------------------------------------------------------- reader death + reaping

class _FaultyPipe(io.RawIOBase):
    """Reads once (the header), then faults - the reader's last act."""

    def __init__(self, first: bytes):
        self._first = first

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self._first:
            n = min(len(b), len(self._first))
            b[:n] = self._first[:n]
            self._first = self._first[n:]
            return n
        raise RuntimeError("simulated broken pipe")


class StubbornChild:
    """Alive, streaming-dead, and it ignores terminate: the shape a process
    stuck mid-write on an undrained pipe actually presents. Every wait call is
    recorded - an unbounded one is the wedge this test hunts."""

    def __init__(self):
        self.pid = 424299
        self.stdout = io.BufferedReader(_FaultyPipe(
            b"ProcessID,CPUStartQPCTimeInMs\r\n"))
        self.terminated = False
        self.killed = False
        self.wait_calls: list = []
        self._exit: int | None = None

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True          # and ignores it

    def kill(self) -> None:
        self.killed = True
        self._exit = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self._exit is not None:
            return self._exit
        if timeout is None:
            # A real wait() on this child never returns: the pipe wedge is
            # exactly why. Block the way the real thing would, so the test
            # watches the restart deadline pass instead of pretending.
            threading.Event().wait()
        raise subprocess.TimeoutExpired("fake", timeout)


def case_6_reader_death_reaps():
    """reader faults while the child lives → bounded terminate→wait→kill→reap, then a new capture"""
    children: list = []
    saved = {}
    patch = {"_RESTART_MAX_S": 1.0, "_EXIT_REAP_S": 0.5}
    missing = object()
    for name, val in patch.items():
        saved[name] = getattr(frames_mod, name, missing)
        setattr(frames_mod, name, val)
    saved_m = {n: getattr(FrameMonitor, n) for n in ("_spawn", "_reclaim_session")}
    FrameMonitor._reclaim_session = lambda self: None

    def fake_spawn(monitor):
        child = StubbornChild() if not children else _QuietChild()
        children.append(child)
        return child
    FrameMonitor._spawn = fake_spawn
    cfg_mod = __import__("app.config", fromlist=["load"])
    cfg = cfg_mod.load()
    cfg["frames"] = dict(cfg["frames"])
    cfg["frames"]["path"] = sys.executable      # exists, so the supervisor runs
    m = None
    try:
        m = FrameMonitor(cfg, role="selftest-rows")
        t0 = time.monotonic()
        restarted = False
        while time.monotonic() - t0 < 8.0:
            if len(children) >= 2:
                restarted = True
                break
            time.sleep(0.02)
        c1 = children[0]
        check_true("the faulting child was terminated", c1.terminated)
        check_true("escalated to kill when terminate was ignored", c1.killed)
        check_true("bounded waits only (never an unbounded wait on the child)",
                   c1.wait_calls and all(t is not None for t in c1.wait_calls),
                   f"wait_calls={c1.wait_calls!r}")
        check_true("reaped, not orphaned", c1.poll() is not None,
                   f"exit={c1.poll()!r}")
        check_true("a fresh capture appeared within the deadline", restarted,
                   f"children={len(children)}")
        check_true("the fault is reported, not swallowed",
                   "reader failed" in (m.error or "") or "reader failed" in " ".join(
                       m.error or "" for _ in [0]),
                   f"error={m.error!r}")
        check_true("capture not reported live", not m.ok)
    finally:
        if m is not None:
            m.close()
        for name, val in saved.items():
            if val is missing:
                delattr(frames_mod, name)
            else:
                setattr(frames_mod, name, val)
        for name, val in saved_m.items():
            setattr(FrameMonitor, name, val)


class _QuietChild:
    """The replacement capture: alive and printing nothing yet, exactly the
    way a real presentmon spends its first moments reclaiming the ETW
    session. The pipe never yields and never EOFs, so the supervisor settles
    into the new stream and the fault it just reported stays readable."""

    def __init__(self):
        self.pid = 424298
        self.stdout = io.BufferedReader(_SilentPipe())
        self._exit: int | None = None

    def poll(self):
        return self._exit

    def terminate(self):
        pass

    def kill(self):
        self._exit = -9

    def wait(self, timeout=None):
        return 0


class _SilentPipe(io.RawIOBase):
    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        threading.Event().wait()        # the child is alive and has said nothing


def main() -> int:
    case("1 truncated row", case_1_truncated_row)
    case("2 flood bounded", case_2_flood_is_bounded)
    case("3 non-finite", case_3_non_finite)
    case("4 reordered columns", case_4_reordered_columns)
    case("5 BOM and stray", case_5_bom_and_stray)
    case("6 reader death reaps", case_6_reader_death_reaps)
    print("\n" + ("SELFTEST PASSED" if not FAILS else f"SELFTEST FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())