"""Real frame telemetry per process, via GameTechDev PresentMon (ETW).

A child `presentmon.exe` streams every present on the system as CSV on stdout
(`--output_stdout --qpc_time_ms`); we parse it into per-(pid, swapchain) ring
buffers and answer two kinds of questions:

  presenters(min_fps)  → who is actively rendering, at what fps   (detection)
  stats(pid)           → fps / frametime / 1% / 0.1% low          (the panel)

Why ETW instead of a game-side counter: zero injection (anti-cheat-safe, the
same mechanism CapFrameX/G-Helper use), works for DX9/11/12, Vulkan, GL and UWP
without knowing the game, and it measures *displayed* frames (`--exclude_dropped`),
which is what "fps" means on screen.

Requires Administrator for the ETW kernel session; without it the child exits
with an error and this object degrades to `ok=False` — the app then falls back
to the window-style game heuristic and `--` frame values. Never synthetic.

Definitions (deliberately simple, documented, single-source):
  fps        displayed presents in the last 1.0 s window
  frametime  MsBetweenPresents per frame; the panel's FRAME TIME is the median
             of the last second (displayed-frame interval, not click-to-photon)
  1%/0.1%    1000 / 99th / 99.9th percentile of frametimes over the window
"""
from __future__ import annotations

import csv
import ctypes
import io
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.snapshot import FrameStats

_HEADER_TIMEOUT_S = 10.0        # no CSV header by then → presentmon failed to start
_MAX_SAMPLES = 12000            # 60 s at 200 Hz
_PID_EXPIRE_S = 30.0
_STREAM_SILENT_S = 20.0         # header up but no rows for this long → say so in the log
_SESSION_SETTLE_S = 2.5         # grace after stopping a previous run's session, see _reclaim_session
# Per-present clock. Exactly one name, verified against a live 2.5.1 capture on
# this desk (2026-09-24): `--qpc_time_ms`, which we always pass, produces
# CPUStartQPCTimeInMs in milliseconds, and its deltas match MsBetweenPresents.
# Aliases are deliberately not accepted — the same column family without the flag
# is raw QPC ticks, and guessing its unit would scale every number by 10,000.
# A rename shows up as the "rows arrived, none parsed" warning with the real
# column list in it, which is a one-line fix instead of quietly wrong fps.
_TIME_COL = "CPUStartQPCTimeInMs"


def _abs(p: str, cfg: dict) -> str:
    """Project-relative path → absolute; config paths are relative to the repo."""
    q = Path(p)
    return str(q if q.is_absolute() else Path(cfg["_root"]) / q)


# Windows job-object plumbing, so the ETW child cannot outlive us. See _KillJob.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
# AssignProcessToJobObject's documented requirement: PROCESS_SET_QUERIES, i.e.
# SYNCHRONIZE | TERMINATE | QUERY_LIMITED_INFORMATION. TERMINATE is not optional —
# the job has to be able to kill the member.
_PROCESS_SET_QUERIES = _SYNCHRONIZE | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION
_k32_configured = False


def _kernel32():
    """kernel32 with the handle prototypes set, or None where it is unavailable.

    restype/argtypes are not decoration: without `restype = HANDLE` a 64-bit
    handle comes back truncated and signed, and the assignment then fails in a
    way that looks like a permissions problem.
    """
    global _k32_configured
    if os.name != "nt":
        return None
    import ctypes.wintypes as wt

    k32 = ctypes.windll.kernel32
    if not _k32_configured:
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.restype = wt.BOOL
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k32.AssignProcessToJobObject.restype = wt.BOOL
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.CloseHandle.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        _k32_configured = True
    return k32


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("ScheduledClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _KillJob:
    """A Windows job object that kills its members when the last handle closes.

    presentmon is an ETW *session*, not a pipe consumer: when the app is killed
    hard (Stop-Process, Stop-ScheduledTask, a crash, a logout) `atexit` never
    runs, the child keeps its kernel session open and burns CPU forever. If the
    child belongs to a job whose handle we hold, the OS closes that job when this
    process dies and takes the child with it.

    Two honest caveats, both measured on real machines rather than assumed:
      * If *we* are already inside a job that disallows nesting — a sandbox, or
        some Task Scheduler configurations — `AssignProcessToJobObject` fails with
        ERROR_ACCESS_DENIED (5) and this whole class degrades to a no-op.
        `adopt()` returns False and `last_error` says why; nobody crashes.
      * So the real bound on orphans is the role-scoped session name plus
        `--stop_existing_session`: the next run of the same role takes the ETW
        session away, and the previous presentmon exits when its session ends.
        This job is what stops an orphan even when nothing restarts.
    """

    def __init__(self) -> None:
        self.handle = None
        self.last_error: int | None = None
        k32 = _kernel32()
        if k32 is None:
            return
        try:
            h = k32.CreateJobObjectW(None, None)
            if not h:
                self.last_error = ctypes.GetLastError()
                return
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(h, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                               ctypes.byref(info), ctypes.sizeof(info)):
                self.last_error = ctypes.GetLastError()
                k32.CloseHandle(h)
                return
            self.handle = h
        except Exception as e:  # noqa: BLE001 - the guard is optional, the stream is not
            self.last_error = getattr(e, "winerror", None)
            self.handle = None

    def adopt(self, pid: int) -> bool:
        """Put `pid` under the job. Nested jobs are fine since Windows 8, so this
        still works when the app itself is already running inside a job (Task
        Scheduler, a sandbox)."""
        if not self.handle:
            return False
        k32 = _kernel32()
        try:
            h = k32.OpenProcess(_PROCESS_SET_QUERIES, False, pid)
            if not h:
                self.last_error = ctypes.GetLastError()
                return False
            try:
                ok = bool(k32.AssignProcessToJobObject(self.handle, h))
                if not ok:
                    self.last_error = ctypes.GetLastError()
                return ok
            finally:
                k32.CloseHandle(h)
        except Exception as e:  # noqa: BLE001
            self.last_error = getattr(e, "winerror", None)
            return False


@dataclass
class Presenter:
    pid: int
    name: str
    fps: float
    last_s: float     # seconds since this pid's last present (0 = just now)


class FrameMonitor:
    """Owns the presentmon child process; safe to call from the tick thread."""

    def __init__(self, cfg: dict, role: str = "main"):
        fcfg = cfg.get("frames", {})
        # The role names the ETW session: the same role takes the session over
        # from a previous run of itself, a different role captures alongside it.
        # Config can pin it, which is how a run gets a brand-new session when the
        # takeover itself is the suspect (tools/ab_capture.ps1).
        role = str(fcfg.get("role") or role)
        self.window_s = float(fcfg.get("window_s", 60))
        self.min_fps = float(fcfg.get("min_present_fps", 24))
        self._exe = _abs(fcfg.get("path", "vendor/presentmon/presentmon.exe"), cfg)
        # Only frames that actually reached the screen are the honest definition
        # of "fps", but that attribution is the OS/driver's call: on a flip path
        # the installed collector cannot attribute, the flag filters everything
        # away. Kept as a knob because it is the first thing to rule out.
        self.exclude_dropped = bool(fcfg.get("exclude_dropped", True))
        # Extra presentmon arguments, appended verbatim. Diagnostic and A/B
        # surface: which frames the collector tracks (`--no_track_display`,
        # `--no_track_gpu`, `--v1_metrics`) changes what a stubborn machine will
        # actually emit, and having to edit code to try a flag makes that a
        # guessing game.
        self.extra_args = [str(a) for a in (fcfg.get("extra_args") or [])]
        # Diagnostic: write the raw capture to a file instead of streaming it.
        # Frame stats are then off *by design* — the file is the artifact to read.
        self.output_file = _abs(fcfg.get("output_file") or "", cfg) \
            if fcfg.get("output_file") else ""
        # One ETW session per *role*, not per pid: production main.py and the
        # liveview dev server coexist, while a new run of the same role takes the
        # session over from a previous (or orphaned) run — see --stop_existing_session.
        self.session_name = f"PCMonitor-{role}"
        self._job = _KillJob()

        self.ok = False
        self.error: str | None = None
        self.job_error: int | None = None   # set if the child could not be put in a kill-on-exit job
        self.header: list[str] = []         # CSV columns as they actually arrived
        self.rows = 0                       # data rows seen after the header
        self.parsed = 0                     # … of those, the ones we could ingest
        self.silent_busy_s = 0.0            # seconds of "GPU busy, stream empty"
        self.last_args: list[str] = []      # spawn line, for the log/warning text
        self._lock = threading.Lock()
        self._pids: dict[int, str] = {}                       # pid → app name
        self._rings: dict[tuple[int, str], deque] = {}        # (pid, swapchain) → (t_ms, between_ms, display_ms)
        self._pid_t: dict[int, float] = {}                    # pid → last event qpc-ms
        self._last_t = 0.0                                    # newest qpc-ms seen
        self._last_row = 0.0      # monotonic time of the last data row (liveness)
        self._spawned = 0.0       # monotonic time of the successful spawn
        self._retries = 1
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._out: list[str] = []                             # stray output lines (for error text)

        if not os.path.exists(self._exe):
            self.error = (f"presentmon not found: {self._exe} — "
                          f"run: powershell -File tools\\fetch_presentmon.ps1")
            return
        threading.Thread(target=self._supervise, daemon=True).start()

    # ---------------------------------------------------------------- lifecycle
    def _reclaim_session(self) -> None:
        """Take the ETW session back from a previous run, then let the kernel settle.

        Doing it in one invocation (`--stop_existing_session` on the capture
        itself) stops the old session and re-creates the same name within
        milliseconds. On at least one Windows 11/driver combination that leaves
        the *new* session registered for the graphics providers but never
        receiving a single event: the child sits there alive, printing nothing,
        and the panel shows `--` forever. Stopping in a process of its own, then
        waiting for the teardown to finish, then starting the real capture is the
        difference between a dead session and a live one — PresentMon's own 2.5.0
        notes ("provider lifecycle ... after a previous instance was abruptly
        stopped") describe the same class of problem.
        """
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            subprocess.run([self._exe, "--no_console_stats", "--no_csv",
                            "--session_name", self.session_name,
                            "--terminate_existing_session"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, creationflags=flags, timeout=20)
        except (subprocess.TimeoutExpired, OSError):
            pass          # nothing to stop, or it refused: the capture below decides
        time.sleep(_SESSION_SETTLE_S)

    def _spawn(self) -> subprocess.Popen:
        # session name is per role (see __init__): a fresh run reclaims the ETW
        # session from any earlier run of the same role instead of adding one
        self._reclaim_session()
        args = [self._exe, "--no_console_stats", "--qpc_time_ms",
                "--session_name", self.session_name, "--stop_existing_session"]
        if self.output_file:
            args += ["--output_file", self.output_file]   # diagnostic: file, not pipe
        else:
            args.append("--output_stdout")
        if self.exclude_dropped:
            args.append("--exclude_dropped")
        args += self.extra_args
        self.last_args = args[1:]
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, startupinfo=None,
                                creationflags=flags, bufsize=0)
        self._spawned = time.monotonic()
        # PresentMon owns a kernel trace session, so an orphan outlives every
        # guard except the OS's: die with us or run forever.
        if not self._job.adopt(proc.pid):
            self.job_error = self._job.last_error
        return proc

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                proc = self._spawn()
            except OSError as e:
                self._fail(f"could not start presentmon: {e}")
                return
            self._proc = proc
            try:
                self._read_stream(proc)
            except Exception:  # noqa: BLE001 — never let a parse bug kill the stream silently
                pass
            rc = proc.wait()
            if self._stop.is_set():
                return
            if self._retries > 0:
                self._retries -= 1
                time.sleep(1.0)
                continue
            tail = " | ".join(self._out[-3:]) or f"exit code {rc}"
            self._fail(f"presentmon stopped: {tail}")
            return

    def _read_stream(self, proc: subprocess.Popen) -> None:
        stream = io.TextIOWrapper(proc.stdout, encoding="utf-8-sig",
                                  errors="replace", newline="")
        reader = csv.reader(stream)
        idx: dict[str, int] | None = None
        for row in reader:
            if not row:
                continue
            if idx is None:
                if "ProcessID" not in row:
                    self._out.append(",".join(row)[:200])
                    continue
                # The header is the only thing that proves the stream is ours:
                # keep it, because "rows arrive but the panel is --" is a
                # column-name question and this is the evidence that answers it.
                idx = {name: i for i, name in enumerate(row)}
                self.header = row
                self.ok = True
                self.error = None
                continue
            self.rows += 1
            self._last_row = time.monotonic()
            self._ingest(row, idx)

    def _ingest(self, row: list[str], idx: dict[str, int]) -> None:
        # Time by name, never by position: the pre-name-lookup version read
        # row[-1], which on this header is MsClickToPhotonLatency — a mostly-empty
        # unrelated column, which is how "fps never appears" survived as long as
        # it did. _TIME_COL says which column and why; TimeInSeconds is a trap
        # (measured: consecutive re9 rows at 48 fps advanced it by +20.8
        # *seconds* per frame, and its origin differs per process).
        i = idx.get(_TIME_COL)
        try:
            pid = int(row[idx["ProcessID"]])
            t = float(row[i]) if i is not None and i < len(row) else None
        except (ValueError, IndexError):
            return
        if t is None:
            return
        between = self._f(row, idx, "MsBetweenPresents")
        display = self._f(row, idx, "MsBetweenDisplayChange")
        swap = row[idx["SwapChainAddress"]] if "SwapChainAddress" in idx else "-"
        now = time.monotonic()
        with self._lock:
            self.parsed += 1
            self._last_t = max(self._last_t, t)
            self._pid_t[pid] = t
            if pid not in self._pids:
                self._pids[pid] = (row[idx["Application"]] if "Application" in idx
                                   else "?")
            key = (pid, swap)
            ring = self._rings.get(key)
            if ring is None:
                ring = self._rings[key] = deque(maxlen=_MAX_SAMPLES)
            ring.append((t, between, display, now))

    @staticmethod
    def _f(row: list[str], idx: dict[str, int], col: str) -> float | None:
        i = idx.get(col)
        if i is None or i >= len(row):
            return None
        v = row[i]
        if not v or v == "NA":
            return None
        try:
            f = float(v)
            return f if f > 0 else None
        except ValueError:
            return None

    def _fail(self, msg: str) -> None:
        self.ok = False
        self.error = msg

    # ----------------------------------------------------------------- liveness
    def observe(self, busy: bool, dt: float) -> None:
        """Accumulate "the machine is drawing and we are seeing nothing".

        A present stream with nothing in it is normal on a still desktop — DWM
        presents no frames when nothing changes — so the only silence worth
        reporting is silence while something is visibly rendering.
        """
        self.silent_busy_s = self.silent_busy_s + dt if (busy and self.rows == 0) else 0.0

    def stream_warning(self) -> str | None:
        """One honest sentence when the capture is up but useless; None if healthy.

        The failure that matters here is silent: presentmon starts, `ok` goes True
        when the header lands, and then no frame ever arrives because the flip
        path on this OS/driver is not attributed by the installed build. Without
        this the panel shows `--` forever and the log says nothing at all, which is
        how "fps never appears" ends up blamed on the game.
        `tools/presentmon_matrix.py` is the follow-up: it A/Bs the invocations.
        """
        if self.error or self.output_file or self._spawned == 0.0:
            return None
        if self.rows and not self.parsed:
            # Print every column: the first real capture put the time column at
            # index 15, so a truncated list hid exactly the name that was wrong.
            cols = ",".join(self.header)
            return (f"[frames] {self.rows} rows arrived, none parsed — this CSV has no "
                    f"{_TIME_COL} column, so there is no millisecond clock to bucket "
                    f"by (we pass --qpc_time_ms to get it). Columns: {cols}")
        if self.silent_busy_s > _STREAM_SILENT_S:
            # No live counter in here: main.py logs each *distinct* warning once,
            # and a number that ticks would rewrite the line every second. Quote
            # the child's *first* line, not its last: that is where a refused
            # provider or a missing privilege shows up, and it stays stable, so
            # the warning is logged once instead of scrolling the log.
            said = self._out[0] if self._out else "printed nothing at all"
            return (f"[frames] no rows from presentmon for {_STREAM_SILENT_S:.0f}+ s of "
                    f"rendering (header={'yes' if self.ok else 'no'}, "
                    f"args: {' '.join(self.last_args)}); child said: {said[:200]} — "
                    f"frame stats stay -- and present-based detection is off; this "
                    f"clears on a reboot when the OS graphics-telemetry path is "
                    f"wedged, else run tools/presentmon_matrix.py")
        return None

    def close(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    # ------------------------------------------------------------------ queries
    def _rows(self, pid: int, max_age_s: float) -> list[tuple[float, float, float]]:
        """(qpc_ms, between_ms, display_ms) for one pid, newest-first, fresh."""
        now_mono = time.monotonic()
        out = []
        for (p, _swap), ring in self._rings.items():
            if p != pid:
                continue
            for t, between, display, mono in reversed(ring):
                if now_mono - mono > max_age_s:
                    break
                out.append((t, between, display))
        return out

    def _sweep(self) -> None:
        """Drop state for processes that stopped presenting a while ago.
        Timing is stream-clock (qpc ms): a pid absent from the stream for
        _PID_EXPIRE_S is gone — a process sweep, not a wall-clock race."""
        dead = [p for p, t in self._pid_t.items() if t < self._last_t - _PID_EXPIRE_S * 1000]
        for p in dead:
            self._pid_t.pop(p, None)
            self._pids.pop(p, None)
            for key in [k for k in self._rings if k[0] == p]:
                self._rings.pop(key, None)

    def presenters(self) -> dict[int, Presenter]:
        """Processes whose displayed present rate beats `min_fps` right now.
        This is the 'it is rendering a game' signal: age < 2 s AND fps >= min_fps."""
        if not self.ok:
            return {}
        with self._lock:
            self._sweep()
            out: dict[int, Presenter] = {}
            for pid, name in self._pids.items():
                t0 = self._pid_t.get(pid, 0.0)
                age = max(0.0, (self._last_t - t0) / 1000.0)
                if age > 2.0:
                    continue
                n = 0
                for (p, _swap), ring in self._rings.items():
                    if p != pid:
                        continue
                    for t, *_ in reversed(ring):
                        if t < self._last_t - 1000.0:
                            break
                        n += 1
                fps = n / 1.0
                if fps >= self.min_fps:
                    out[pid] = Presenter(pid, name, fps, age)
            return out

    def names(self) -> dict[int, str]:
        with self._lock:
            return dict(self._pids)

    def stats(self, pid: int) -> FrameStats | None:
        """Panel numbers for one process; None when it has no fresh data."""
        if not self.ok:
            return None
        with self._lock:
            rows = self._rows(pid, self.window_s)
        if not rows:
            return None
        newest = rows[0][0]
        between = [b for t, b, d in rows if b is not None and t >= newest - self.window_s * 1000]
        last1 = [b for t, b, d in rows if b is not None and t >= newest - 1000]
        disp1 = [d for t, b, d in rows if d is not None and t >= newest - 1000]
        if not last1 and not between:
            return None
        n1 = sum(1 for t, *_ in rows if t >= newest - 1000)
        fps = float(n1) if n1 else (1000.0 / float(np.median(between)) if between else 0.0)
        low1 = 1000.0 / float(np.percentile(between, 99)) if len(between) >= 30 else None
        low01 = 1000.0 / float(np.percentile(between, 99.9)) if len(between) >= 100 else None
        ms = float(np.median(disp1)) if disp1 else (float(np.median(last1)) if last1 else None)
        return FrameStats(fps=fps, low1_pct=low1, low01_pct=low01, latency_ms=ms)
