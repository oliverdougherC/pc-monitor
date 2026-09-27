"""Nobody outlives the app: the kill-on-exit job actually adopted, and a
shutdown that cannot lose a child to a racing spawn.

    .venv\\Scripts\\python tools\\frames_shutdown_selftest.py

Two wedges in the "monitoring dies with the app" promise, both verified at
this baseline:

  1. the process handle was opened with SYNCHRONIZE | TERMINATE |
     QUERY_LIMITED_INFORMATION - AssignProcessToJobObject's documented
     requirement is PROCESS_SET_QUOTA *and* PROCESS_TERMINATE - so every
     adoption failed with ERROR_ACCESS_DENIED (5, reproduced live in case 1)
     and the child was never in the job at all;
  2. close() set a flag and terminated whatever child was published at that
     instant, while a supervisor parked in _reclaim_session (a 20 s
     subprocess.run plus a settle sleep) woke up after close() returned and
     spawned a child nobody owned. No join, no reap, no stream close, no job
     handle close.

Case 1 is native and real: a real child process, a real job object, the real
AssignProcessToJobObject - membership verified with IsProcessInJob (the call
answering is not the fact being true), and kill-on-close proven by closing
the handle and watching the child die. Cases 2-4 drive the real supervisor
and close() against scripted children, including one that ignores terminate
(the escalation must reach kill) and one born behind a barrier while close()
is already running (it must not survive the barrier). Recovery stays scoped
to this app's own role-named ETW session; nothing here kills a collector the
app does not own.
"""
import ctypes
import io
import subprocess
import sys
import threading
import time

sys.path.insert(0, ".")   # our tree first: vendor has its own main.py
sys.path.append("vendor/turing-smart-screen-python")
from app import config as cfgmod          # noqa: E402
from app import frames as frames_mod      # noqa: E402
from app.frames import FrameMonitor       # noqa: E402

sys.stdout.reconfigure(errors="replace")

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def wait_for(pred, timeout: float = 6.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def case(name: str, fn) -> None:
    print(f"\ncase {name}: {fn.__doc__.strip().splitlines()[0]}")
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - a raising case is a failing case
        FAILS.append(f"{name} raised {e!r}")
        print(f"  FAIL {name} raised {e!r}")


# ------------------------------------------------------------------- the fakes

class _Pipe(io.RawIOBase):
    """stdout the test controls: quiet until the child dies, then EOF."""

    def __init__(self):
        self._eof = False
        self._cv = threading.Condition()

    def eof(self) -> None:
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        with self._cv:
            while not self._eof:
                self._cv.wait()
            return 0


class FakeChild:
    """Popen-shaped child; `stubborn` ignores terminate like a process stuck
    in a kernel call does, and must be killed instead."""

    _next_pid = [424200]

    def __init__(self, stubborn: bool = False):
        self.pid = FakeChild._next_pid[0]
        FakeChild._next_pid[0] += 1
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False
        self._exit: int | None = None
        self.stdout = io.BufferedReader(_Pipe())
        self.stderr = None            # Popen's contract: merged into stdout
        self.stdin = None             # Popen's contract: DEVNULL

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True
        if not self.stubborn:
            self._die(-15)

    def kill(self) -> None:
        self.killed = True
        self._die(-9)

    def _die(self, code: int) -> None:
        self._exit = code
        self.stdout.raw.eof()

    def wait(self, timeout: float | None = None) -> int:
        if self._exit is not None:
            return self._exit
        if timeout is None:
            threading.Event().wait()      # the wedge the old code walked into
        raise subprocess.TimeoutExpired("fake", timeout)


class Scenario:
    """Real supervisor, scripted births: Popen, session reclaim and the
    reap bounds are swapped for the duration of one case."""

    def __init__(self, stubborn: bool = False, reclaim=None):
        self.children: list[FakeChild] = []
        self.stubborn = stubborn
        self.reclaim = reclaim
        self._saved: dict = {}

    def __enter__(self):
        # getattr-or-set: before the fix these constants do not exist, and
        # the cases must still run far enough to show the race, not die on
        # the patch itself.
        for name, val in {"_REAP_WAIT_S": 0.5, "_JOIN_WAIT_S": 5.0,
                          "_CLOSE_LOCK_S": 5.0}.items():
            self._saved[name] = getattr(frames_mod, name, None)
            setattr(frames_mod, name, val)
        self._saved["Popen"] = subprocess.Popen
        subprocess.Popen = self._popen
        self._saved["reclaim"] = FrameMonitor._reclaim_session
        FrameMonitor._reclaim_session = (self.reclaim or (lambda self: None))
        return self

    def __exit__(self, *exc):
        subprocess.Popen = self._saved["Popen"]
        FrameMonitor._reclaim_session = self._saved["reclaim"]
        for name, val in self._saved.items():
            if name in ("Popen", "reclaim"):
                continue
            if val is None:
                delattr(frames_mod, name)
            else:
                setattr(frames_mod, name, val)
        return False

    def _popen(self, *args, **kwargs):
        child = FakeChild(stubborn=self.stubborn)
        self.children.append(child)
        return child

    def monitor(self) -> FrameMonitor:
        cfg = cfgmod.load()
        cfg["frames"] = dict(cfg["frames"])
        cfg["frames"]["path"] = sys.executable     # exists; every birth is faked
        return FrameMonitor(cfg, role="selftest-shutdown")


# ----------------------------------------------------------------------- cases

def case_1_rights_and_native_adoption():
    """the documented rights, proven by a real assignment and a real kill-on-close"""
    rights = getattr(frames_mod, "_PROCESS_ADOPT_RIGHTS", 0)
    check_true("adoption asks for PROCESS_SET_QUOTA (the documented requirement)",
               bool(rights & getattr(frames_mod, "_PROCESS_SET_QUOTA", 0)),
               f"mask={rights:#x}")
    check_true("adoption asks for PROCESS_TERMINATE (the job must be able to kill)",
               bool(rights & getattr(frames_mod, "_PROCESS_TERMINATE", 0)),
               f"mask={rights:#x}")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             creationflags=0x08000000)
    try:
        job = frames_mod._KillJob()
        check_true("job object created", job.handle is not None,
                   f"winerr={job.last_error}")
        check_true("real child adopted into the job", job.adopt(child.pid),
                   f"winerr={job.last_error}")
        k32 = frames_mod._kernel32()
        import ctypes.wintypes as wt
        h = k32.OpenProcess(frames_mod._PROCESS_QUERY_LIMITED_INFORMATION, False,
                            child.pid)
        inside = wt.BOOL(0)
        asked = k32.IsProcessInJob(h, job.handle, ctypes.byref(inside))
        k32.CloseHandle(h)
        check_true("IsProcessInJob confirms membership (verified, not assumed)",
                   bool(asked) and bool(inside.value))
        job.close()                 # what the OS does when this process dies
        t0 = time.monotonic()
        died = wait_for(lambda: child.poll() is not None, 10.0)
        check_true("kill-on-close took the child when the handle closed", died,
                   f"after {time.monotonic() - t0:.1f}s")
    finally:
        if child.poll() is None:
            child.kill()


def case_2_close_during_blocked_spawn():
    """close() while session reclaim is blocked, then release: the late child must not survive"""
    entered, release = threading.Event(), threading.Event()

    def blocked_reclaim(self):
        entered.set()
        release.wait(10)

    with Scenario(reclaim=blocked_reclaim) as s:
        m = s.monitor()
        try:
            check_true("supervisor is inside the spawn", entered.wait(5.0))
            closer = threading.Thread(target=m.close)
            closer.start()
            time.sleep(0.3)
            check_true("close waits behind the in-flight spawn (lifecycle lock)",
                       closer.is_alive())
            release.set()
            closer.join(10.0)
            check_true("close returns once the barrier releases", not closer.is_alive())
            check("exactly one child was born", len(s.children), 1)
            c = s.children[0]
            check_true("the child did not survive close()",
                       c.terminated and c.poll() is not None,
                       f"terminated={c.terminated} exit={c.poll()!r}")
            check_true("supervisor thread finished", wait_for(lambda: not m._thread.is_alive()))
            check("job handle released", m._job.handle, None)
        finally:
            release.set()
            m.close()


def case_3_stubborn_child_escalates():
    """a child that ignores terminate is killed and reaped; close() stays bounded"""
    with Scenario(stubborn=True) as s:
        m = s.monitor()
        try:
            check_true("child published", wait_for(lambda: m._proc is not None))
            t0 = time.monotonic()
            m.close()
            elapsed = time.monotonic() - t0
            c = s.children[0]
            check_true("escalated to kill when terminate was ignored", c.killed)
            check_true("reaped, not orphaned", c.poll() is not None,
                       f"exit={c.poll()!r}")
            check_true("close() stayed bounded", elapsed < 10.0, f"{elapsed:.1f}s")
            check_true("supervisor thread finished", not m._thread.is_alive())
        finally:
            m.close()


def case_4_start_stop_cycles():
    """repeated start/stop: no thread, handle or child accumulates"""
    base_threads = threading.active_count()
    n_children = 0
    with Scenario() as s:
        for _ in range(3):
            m = s.monitor()
            check_true("child published", wait_for(lambda: m._proc is not None))
            m.close()
            n_children += len(s.children) - n_children
            check_true("this cycle's child is dead",
                       all(c.poll() is not None for c in s.children))
            check_true("this cycle's supervisor is gone", not m._thread.is_alive())
            check("this cycle's job handle is released", m._job.handle, None)
            check("no child left published", m._proc, None)
    check_true("no threads accumulated", threading.active_count() <= base_threads,
               f"{base_threads} -> {threading.active_count()}")


def main() -> int:
    case("1 rights + native adoption", case_1_rights_and_native_adoption)
    case("2 close during blocked spawn", case_2_close_during_blocked_spawn)
    case("3 stubborn child escalates", case_3_stubborn_child_escalates)
    case("4 start/stop cycles", case_4_start_stop_cycles)
    print("\n" + ("SELFTEST PASSED" if not FAILS else f"SELFTEST FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
