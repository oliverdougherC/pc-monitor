"""Prove a stalled presentmon recovers on its own — fake children, no admin.

    .venv\\Scripts\\python tools\\frames_recovery_selftest.py

Issue #10's failures all share one shape: the child stays alive and the
capture stops being useful, and the old supervisor could not see it because
it *was* the reader — a blocked read blocked the watchdog with it, and the
silence detector went blind after the first row it had ever seen. The
workaround people reached for was rebooting the machine, which is the wrong
first response to a condition the code had never even observed.

Each case drives the real `_supervise` loop against a scripted fake child
(no ETW, no elevation) reproducing one acceptance shape from the issue:

  A  a child that never prints a header            → restarted, not awaited
  B  a child that prints the header and nothing else, while rendering is
     expected                                     → restarted
  C  one valid row, then silence                   → restarted, and the new
                                                    generation starts at zero
  D  a legitimately quiet desktop                  → NOT restarted (no loop)
  E  an incompatible schema                        → restarted, ok stays False
  F  a child that exits repeatedly                 → retried with bounded backoff
  G  the executable missing at startup, appearing later → picked up without
     restarting the app
  U  the state words and stall decisions, synchronously

Fake children, not real ones, because the condition to reproduce is "alive
and saying nothing while its pipe stays open" — a real presentmon under ETW
does that on demand from a wedged driver and never on demand from a test.
"""
import io
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, ".")   # our tree first: vendor has its own main.py
sys.path.insert(0, "tools")
sys.path.append("vendor/turing-smart-screen-python")
from app import config as cfgmod          # noqa: E402
from app import frames as frames_mod      # noqa: E402
from app.frames import FrameMonitor       # noqa: E402
from frames_selftest import V1_HEADER, V2_HEADER, V2_TIME, synth  # noqa: E402

sys.stdout.reconfigure(errors="replace")

FAILS: list[str] = []

# Deadlines scaled down so a case runs in seconds, not in the minutes the
# production values imply; the logic under test is the comparison, not the
# magnitude. Silence stays at the real 20 s because the cases feed that
# through `observe(dt)` directly — that is the knob, not the clock.
PATCH = {"_HEADER_TIMEOUT_S": 0.5, "_SCHEMA_TIMEOUT_S": 0.5, "_WATCH_POLL_S": 0.05,
         "_READER_JOIN_S": 1.0, "_KILL_WAIT_S": 1.0, "_RESTART_MAX_S": 1.0,
         "_MISSING_EXE_RETRY_S": 0.3}


class _Pipe(io.RawIOBase):
    """A stdout pipe the test controls: bytes appear only when fed, EOF only
    when the child is killed or exits — which is exactly the shape of a child
    that wedges before its header."""

    def __init__(self):
        self._buf = bytearray()
        self._eof = False
        self._cv = threading.Condition()

    def feed(self, data: bytes) -> None:
        with self._cv:
            self._buf += data
            self._cv.notify_all()

    def eof(self) -> None:
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        with self._cv:
            while not self._buf and not self._eof:
                self._cv.wait()
            if not self._buf:
                return 0
            n = min(len(b), len(self._buf))
            b[:n] = bytes(self._buf[:n])
            del self._buf[:n]
            return n


class FakeChild:
    """Popen-shaped child the test controls completely. `terminate()` is what
    TerminateProcess looks like from the supervisor's side: the pipe closes
    and an exit code appears."""

    def __init__(self, label: str):
        self.label = label
        self.pid = 424200 + len(label)     # never used to open anything
        self.stdout = io.BufferedReader(_Pipe())
        self.terminated = False
        self._exit: int | None = None

    def feed(self, text: str) -> None:
        self.stdout.raw.feed(text.encode("utf-8"))

    def exit(self, code: int = 3) -> None:      # died on its own
        self.stdout.raw.eof()
        if self._exit is None:
            self._exit = code

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True
        self.stdout.raw.eof()
        if self._exit is None:
            self._exit = -15

    def wait(self, timeout: float | None = None) -> int:
        return self._exit if self._exit is not None else 0


class Harness:
    """Patches the supervisor's clocks and replaces spawning with fakes for
    the duration of one case. `_spawn` and `_reclaim_session` are patched on
    the class so the supervisor thread the constructor starts is already
    talking to fakes — there is no window where it could touch the real
    presentmon or a real ETW session."""

    def __init__(self):
        self.children: list[FakeChild] = []
        self._saved: dict = {}
        self._saved_m: dict = {}

    def __enter__(self):
        for name, val in PATCH.items():
            self._saved[name] = getattr(frames_mod, name)
            setattr(frames_mod, name, val)
        for name in ("_spawn", "_reclaim_session"):
            self._saved_m[name] = getattr(FrameMonitor, name)
        FrameMonitor._reclaim_session = lambda self: None

        def fake_spawn(monitor):
            child = FakeChild(f"gen{len(self.children) + 1}")
            self.children.append(child)
            monitor._spawned = time.monotonic()
            monitor.last_args = ["--fake"]
            return child
        FrameMonitor._spawn = fake_spawn
        return self

    def __exit__(self, *exc):
        for name, val in self._saved.items():
            setattr(frames_mod, name, val)
        for name, val in self._saved_m.items():
            setattr(FrameMonitor, name, val)
        return False

    def monitor(self, exe: str | None = None) -> FrameMonitor:
        cfg = cfgmod.load()
        cfg["frames"] = dict(cfg["frames"])
        # sys.executable exists, so the supervisor thread starts and enters its
        # watch loop — but every spawn is a FakeChild.
        cfg["frames"]["path"] = exe or sys.executable
        return FrameMonitor(cfg, role="selftest-recovery")

    def child(self, i: int, timeout: float = 6.0) -> FakeChild | None:
        if not wait_for(lambda: len(self.children) > i, timeout):
            return None
        return self.children[i]


def wait_for(pred, timeout: float = 6.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def header_line(header: str) -> str:
    return header + "\r\n"


def one_row(header: str, time_col: str, **kw) -> str:
    blob = synth(header, time_col, n=1, **kw).decode("utf-8-sig")
    return blob.splitlines()[1] + "\r\n"


def case(name: str, fn) -> None:
    print(f"\ncase {name}: {fn.__doc__.strip().splitlines()[0]}")
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - a raising case is a failing case
        FAILS.append(f"{name} raised {e!r}")
        print(f"  FAIL {name} raised {e!r}")


# --------------------------------------------------------------------------- cases

def case_a_header_never():
    """never prints a header → the child is replaced, not awaited forever"""
    with Harness() as h:
        m = h.monitor()
        try:
            c1 = h.child(0)
            check_true("first child spawned", c1 is not None)
            check_true("stalled child terminated within the deadline",
                       wait_for(lambda: c1 and c1.terminated, 5.0),
                       f"state={getattr(m, 'state', None)}")
            check_true("a second child was spawned", h.child(1, 5.0) is not None)
            check_true("capture is not reported live", not m.ok)
            check_true("state names supervision progress",
                       getattr(m, "state", None) in ("starting", "retrying"),
                       f"state={getattr(m, 'state', None)}")
        finally:
            m.close()


def case_b_header_only_busy():
    """header only, rendering expected → stale, then replaced"""
    with Harness() as h:
        m = h.monitor()
        try:
            c1 = h.child(0)
            c1.feed(header_line(V2_HEADER))
            check_true("header parsed", wait_for(lambda: bool(m.header), 3.0))
            # A header is not health. `ok` used to latch the moment ProcessID was
            # seen, and main.py reads it as "presentmon session live" /
            # `capture=live` — so a capture that went on to parse nothing at all
            # reported as a live session for the whole schema/silence deadline
            # (issue #10). It is the first *parsed* row that proves measurement.
            check_true("a header with no parsed row is not ok", not m.ok)
            check("no rows parsed yet", m.parsed, 0)
            for _ in range(40):                     # feed rendering evidence
                m.observe(busy=True, dt=5.0)
                if c1.terminated:
                    break
                time.sleep(0.05)
            check_true("silent-while-rendering child terminated", c1.terminated)
            check_true("a second child was spawned", h.child(1, 5.0) is not None)
            check_true("capture is not reported live", not m.ok)
        finally:
            m.close()


def case_c_one_row_then_hang():
    """one valid row then silence → recovered, and the new generation counts from zero"""
    with Harness() as h:
        m = h.monitor()
        try:
            c1 = h.child(0)
            c1.feed(header_line(V2_HEADER))
            c1.feed(one_row(V2_HEADER, V2_TIME))
            check_true("one row parsed", wait_for(lambda: m.parsed >= 1, 3.0))
            check("rows in generation 1", m.rows, 1)
            for _ in range(40):
                m.observe(busy=True, dt=5.0)
                if c1.terminated:
                    break
                time.sleep(0.05)
            check_true("one-row-then-hang child terminated", c1.terminated)
            check_true("a second child was spawned", h.child(1, 5.0) is not None)
            # The old detector kept `rows == 1` forever and could never arm
            # again; a fresh generation must measure silence from zero.
            check("rows reset for generation 2", m.rows, 0)
            check("parsed reset for generation 2", m.parsed, 0)
        finally:
            m.close()


def case_d_idle_desktop():
    """quiet on an idle desktop → stays up; idle is not a fault"""
    with Harness() as h:
        m = h.monitor()
        try:
            c1 = h.child(0)
            c1.feed(header_line(V2_HEADER))
            check_true("header parsed", wait_for(lambda: bool(m.header), 3.0))
            # One valid row, because health is now evidence of *measurement*, not
            # of a header (issue #10). A desktop that has rendered nothing at all
            # is covered by A and B — its deadlines own that case. This case is
            # the other quiet desktop: one that measured something and then went
            # still, which must be left alone and must still read as live.
            c1.feed(one_row(V2_HEADER, V2_TIME))
            check_true("one row parsed", wait_for(lambda: m.parsed >= 1, 3.0))
            check("a measured capture is live", m.ok, True)
            # The quiet stretch below is the wall clock's in a live system and
            # microseconds in a replay, so age the parse clock by hand past the
            # silence window: same predicate, same field, and `idle` vs `stale`
            # is exactly the decision that must not change.
            m._last_parse -= frames_mod._STREAM_SILENT_S + 1.0
            t0 = time.monotonic()
            while time.monotonic() - t0 < 2.0:      # ≫ every patched deadline
                m.observe(busy=False, dt=0.2)
                time.sleep(0.05)
            check_true("idle child untouched", not c1.terminated)
            check("no restart loop on an idle desktop", len(h.children), 1)
            check("state", getattr(m, "state", None), "idle")
            check("a quiet desktop is still a live capture", m.ok, True)
        finally:
            m.close()


def case_e_bad_schema():
    """incompatible rows → ok stays False and the child is replaced"""
    with Harness() as h:
        m = h.monitor()
        try:
            c1 = h.child(0)
            c1.feed(header_line(V1_HEADER))
            blob = synth(V1_HEADER, "QPCTime", n=2).decode("utf-8-sig")
            c1.feed("\r\n".join(blob.splitlines()[1:]) + "\r\n")
            check_true("rows were counted", wait_for(lambda: m.rows >= 2, 3.0))
            check_true("a header without the ms clock is not ok", not m.ok)
            check_true("bad-schema child terminated",
                       wait_for(lambda: c1.terminated, 5.0))
            check_true("error names the missing column",
                       wait_for(lambda: "CPUStartQPCTimeInMs" in (m.error or ""), 2.0),
                       f"error={m.error!r}")
            check_true("state is a recovery word",
                       getattr(m, "state", None) in ("bad-schema", "retrying", "starting"),
                       f"state={getattr(m, 'state', None)}")
        finally:
            m.close()


def case_f_repeated_exits():
    """exits repeatedly → retried forever with bounded backoff"""
    with Harness() as h:
        m = h.monitor()
        try:
            c1 = h.child(0)
            c1.exit()
            check_true("third spawn within bounded backoff",
                       h.child(2, timeout=10.0) is not None)
            for c in h.children:
                if c.poll() is None:
                    c.exit()
            check_true("capture is not reported live", not m.ok)
            check_true("state is between attempts",
                       getattr(m, "state", None) in ("starting", "retrying"),
                       f"state={getattr(m, 'state', None)}")
        finally:
            m.close()


def case_g_missing_exe_appears():
    """executable missing at startup, fetched later → picked up without an app restart"""
    tmp = tempfile.mkdtemp(prefix="pm-absent-")
    exe = os.path.join(tmp, "absent.exe")
    with Harness() as h:
        m = h.monitor(exe=exe)
        try:
            check("state with no executable", getattr(m, "state", None), "missing-exe")
            check_true("error tells the user how to fetch it",
                       "not found" in (m.error or ""), f"error={m.error!r}")
            check("nothing spawned while absent", len(h.children), 0)
            Path(exe).write_bytes(b"MZ")            # the fetch script lands
            check_true("supervisor notices the new executable at its low rate",
                       h.child(0, timeout=4.0) is not None)
            check_true("state moved on from missing-exe",
                       getattr(m, "state", None) != "missing-exe",
                       f"state={getattr(m, 'state', None)}")
        finally:
            m.close()
            shutil.rmtree(tmp, ignore_errors=True)


def case_u_states_and_deadlines():
    """the state words and stall decisions, checked synchronously"""
    m = FrameMonitor(_cfg_missing(), role="selftest-recovery")
    m.error = None
    m._phase = "watching"
    m._spawned = time.monotonic()
    check("no header yet → starting", m.state, "starting")
    check("within the startup deadline → no stall", m._stall_reason(), None)
    m._spawned -= 100.0                             # the deadline has passed
    check_true("startup deadline fires", "no CSV header" in (m._stall_reason() or ""))
    m._spawned = time.monotonic()

    blob = synth(V2_HEADER, V2_TIME, n=1).decode("utf-8-sig").encode("utf-8")
    m._read_stream(_Stub(blob))
    check("usable rows → healthy", m.state, "healthy")
    check("a parsed row is ok", m.ok, True)
    m._last_parse -= 100.0                          # and then the stream went quiet
    m.observe(busy=False, dt=1.0)
    check("quiet desktop → idle, not stale", m.state, "idle")
    check("idle desktop must never be restarted", m._stall_reason(), None)
    m.observe(busy=True, dt=25.0)                   # something is rendering again
    check("rendering with no rows → stale", m.state, "stale")
    check_true("stale is a stall reason", "rendering" in (m._stall_reason() or ""))
    m.observe(busy=False, dt=1.0)                   # silence window resets with the render

    m._begin_generation(1)                          # a restart measures from zero
    check("generation reset rows", m.rows, 0)
    check("generation reset header", m.header, [])
    check("generation reset silence", m.silent_busy_s, 0.0)
    check("generation reset ok", m.ok, False)

    legacy = synth(V1_HEADER, "QPCTime", n=2)       # synth returns bytes, BOM included
    m._read_stream(_Stub(legacy))
    check("incompatible header → bad-schema", m.state, "bad-schema")
    check("incompatible header is not ok", m.ok, False)
    check_true("stall reason names the column",
               "CPUStartQPCTimeInMs" in (m._stall_reason() or ""))

    m._begin_generation(2)
    rows = synth(V2_HEADER, V2_TIME, n=2).decode("utf-8-sig").splitlines()
    broken = rows[1].split(",")
    broken[V2_HEADER.split(",").index(V2_TIME)] = "not-a-number"
    m._read_stream(_Stub((rows[0] + "\r\n" + ",".join(broken) + "\r\n").encode()))
    check("rows with dead values still count", m.rows, 1)
    check("…and none parse", m.parsed, 0)
    m._first_row -= 100.0                           # the schema deadline has passed
    check_true("schema deadline fires", "none parsed" in (m._stall_reason() or ""))
    check("state", m.state, "bad-schema")


class _Stub:
    """Just enough Popen for a direct _read_stream call."""

    def __init__(self, blob: bytes):
        self.stdout = io.BytesIO(blob)
        self.pid = -1

    def wait(self) -> int:
        return 0

    def poll(self) -> int | None:
        return 0


def _cfg_missing() -> dict:
    cfg = cfgmod.load()
    cfg["frames"] = dict(cfg["frames"])
    cfg["frames"]["path"] = "does-not-exist.exe"    # no thread ever spawns
    return cfg


def main() -> int:
    case("A", case_a_header_never)
    case("B", case_b_header_only_busy)
    case("C", case_c_one_row_then_hang)
    case("D", case_d_idle_desktop)
    case("E", case_e_bad_schema)
    case("F", case_f_repeated_exits)
    case("G", case_g_missing_exe_appears)
    case("U", case_u_states_and_deadlines)
    print("\n" + ("SELFTEST PASSED" if not FAILS else f"SELFTEST FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
