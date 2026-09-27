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
_RESTART_MAX_S = 60.0           # backoff ceiling for a capture that keeps dying
_HEALTHY_S = 120.0              # alive this long with rows → the streak of failures resets
_REAP_WAIT_S = 5.0              # each step of terminate → wait → kill → reap
_CLOSE_LOCK_S = 10.0            # shutdown's patience with a spawn mid-flight; past this
                                # the late child is still disposed by _spawn's own check
_JOIN_WAIT_S = 10.0             # shutdown's patience with the supervisor thread
# Present modes worth distinguishing, from the column of the same name. `Hardware:`
# without "Composed" is a true exclusive-flip path; everything else goes through the
# compositor like any other window. dwm.exe's own rows are the compositor presenting
# and are ignored outright (it is in `game.ignore`), which is what keeps a
# `Hardware: Legacy Flip` row from being read as "the desktop is a fullscreen game".
_MODE_EXCLUSIVE = ("hardware: legacy flip", "hardware committed", "hardware: flip")
# A present this old is not "live" any more, and a game that stopped presenting is
# held (dimmed, `stale`) for `hold_s` before the panel goes to `--`.
_FRESH_S = 1.2
_DEFAULT_HOLD_S = 12.0
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
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# AssignProcessToJobObject's documented access requirement on the process
# handle is PROCESS_SET_QUOTA *and* PROCESS_TERMINATE. The old mask asked for
# SYNCHRONIZE | TERMINATE | QUERY_LIMITED_INFORMATION under a comment naming
# "PROCESS_SET_QUERIES" - a right that does not exist - so on a machine with
# nothing wrong with nesting at all, every adoption failed with
# ERROR_ACCESS_DENIED (5, measured on this desk) and the kill-on-exit guard
# silently never existed. QUERY_LIMITED_INFORMATION stays because verifying
# the adoption (IsProcessInJob) asks the handle itself.
_PROCESS_ADOPT_RIGHTS = (_PROCESS_SET_QUOTA | _PROCESS_TERMINATE
                         | _PROCESS_QUERY_LIMITED_INFORMATION)
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
        k32.IsProcessInJob.restype = wt.BOOL
        k32.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
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
        """Put `pid` under the job, and verify it is actually there.

        Nested jobs are fine since Windows 8, so this still works when the
        app itself is already running inside a job (Task Scheduler, a
        sandbox). The verification is not paranoia: this class exists to
        make exactly one claim - the OS will take the child when we die -
        and a TRUE from AssignProcessToJobObject is the call answering, not
        the fact being true. Ask IsProcessInJob, and report failure when it
        disagrees.
        """
        if not self.handle:
            return False
        k32 = _kernel32()
        try:
            h = k32.OpenProcess(_PROCESS_ADOPT_RIGHTS, False, pid)
            if not h:
                self.last_error = ctypes.GetLastError()
                return False
            try:
                ok = bool(k32.AssignProcessToJobObject(self.handle, h))
                if not ok:
                    self.last_error = ctypes.GetLastError()
                    return False
                import ctypes.wintypes as wt
                inside = wt.BOOL(0)
                if not (k32.IsProcessInJob(h, self.handle, ctypes.byref(inside))
                        and inside.value):
                    ok = False      # last_error stays None: the call lied, it did not fail
                return ok
            finally:
                k32.CloseHandle(h)
        except Exception as e:  # noqa: BLE001
            self.last_error = getattr(e, "winerror", None)
            return False

    def close(self) -> None:
        """Release the job handle. With KILL_ON_JOB_CLOSE set, the last close
        is the OS's kill switch: anything still in the job dies here, even a
        child some bounded reap gave up on - which is the whole point of
        holding the handle in the first place, made deterministic at shutdown
        instead of only at process death."""
        if not self.handle:
            return
        h, self.handle = self.handle, None
        try:
            k32 = _kernel32()
            if k32 is not None:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


@dataclass
class Presenter:
    pid: int
    name: str
    fps: float
    last_s: float     # seconds since this pid's last present (0 = just now)
    # How hard the GPU is working for this process, % of the frame span. A real game
    # renders every frame it can: on this desk re9.exe measured ~100 % while a
    # scrolling browser sat near 19 %, on the same column. That makes it the single
    # best "this is a game, not a video" discriminator available without asking the
    # user, and it is free — PresentMon already measured it.
    gpu: float | None = None
    # True when the process presents on a hardware flip path (`Hardware: Legacy
    # Flip`, `Hardware Committed`), i.e. exclusive fullscreen. `Hardware Composed:
    # Independent Flip` and `Composed: Flip` are the desktop-compositor paths, which
    # every window uses, so they say nothing about exclusivity.
    exclusive: bool = False


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
        # How long a game that has stopped presenting keeps its last measurement
        # (dimmed, `stale`) instead of dropping to `--`. Alt-tab and loading screens.
        self.hold_s = float(fcfg.get("hold_s", _DEFAULT_HOLD_S))

        self.ok = False
        self.error: str | None = None
        self.job_error: int | None = None   # set if the child could not be put in a kill-on-exit job
        self.header: list[str] = []         # CSV columns as they actually arrived
        self.rows = 0                       # data rows seen after the header
        self.parsed = 0                     # … of those, the ones we could ingest
        self.silent_busy_s = 0.0            # seconds of "GPU busy, stream empty"
        self.last_args: list[str] = []      # spawn line, for the log/warning text
        self.starts = 0                     # successful spawns, including restarts
        self.restarts = 0                   # deliberate ones (after a resume)
        self._lock = threading.Lock()
        self._pids: dict[int, str] = {}                       # pid → app name
        self._rings: dict[tuple[int, str], deque] = {}        # (pid, swapchain) → samples
        self._pid_t: dict[int, float] = {}                    # pid → last event qpc-ms
        self._held: dict[int, tuple] = {}                     # pid → (last live stats, mono)
        self._last_t = 0.0                                    # newest qpc-ms seen
        self._last_row = 0.0      # monotonic time of the last data row (liveness)
        self._spawned = 0.0       # monotonic time of the successful spawn
        self._streak = 0          # consecutive early exits, drives the backoff
        self._restart_req = False
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._out: list[str] = []                             # stray output lines (for error text)
        # Lifecycle (issue #12): close() and _spawn must not interleave, or a
        # supervisor parked in session reclamation births a child after the
        # app has decided to stop. _life makes "decide to spawn, spawn,
        # publish" one decision; _closed is what a spawn already holding the
        # lock checks on both sides of the birth.
        self._life = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None

        if not os.path.exists(self._exe):
            self.error = (f"presentmon not found: {self._exe} — "
                          f"run: powershell -File tools\\fetch_presentmon.ps1")
            return
        self._thread = threading.Thread(target=self._supervise, daemon=True)
        self._thread.start()

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
        # stop-wait rather than sleep: close() holds the lifecycle lock while it
        # reaps, and a plain sleep here would make shutdown queue behind a
        # settle it no longer wants.
        self._stop.wait(_SESSION_SETTLE_S)

    def _spawn(self) -> subprocess.Popen | None:
        """Spawn one capture under the lifecycle lock, or return None if the
        app stopped while we were getting there.

        The lock spans reclaim → birth → publish so close() cannot interleave
        and leave a child nobody owns. A child that is born after close() has
        set the flag is still a child we made, so we dispose of it ourselves
        rather than hand a late orphan to a supervisor that is about to exit.
        """
        with self._life:
            if self._closed:
                return None
            # session name is per role (see __init__): a fresh run reclaims the
            # ETW session from any earlier run of the same role, not adds one
            self._reclaim_session()
            args = [self._exe, "--no_console_stats", "--qpc_time_ms",
                    "--session_name", self.session_name, "--stop_existing_session"]
            if self.output_file:
                args += ["--output_file", self.output_file]  # diagnostic: file, not pipe
            else:
                args.append("--output_stdout")
            if self.exclude_dropped:
                args.append("--exclude_dropped")
            args += self.extra_args
            self.last_args = args[1:]
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    startupinfo=None, creationflags=flags, bufsize=0)
            self._spawned = time.monotonic()
            # Re-check now that the child exists: reclaim's settle is where a
            # close() most often lands, and this is the last point inside the
            # lock at which we can still take the child back.
            if self._closed:
                self._dispose(proc)
                return None
            self._proc = proc
            # PresentMon owns a kernel trace session, so an orphan outlives
            # every guard except the OS's: die with us or run forever.
            if not self._job.adopt(proc.pid):
                self.job_error = self._job.last_error
            return proc

    def _dispose(self, proc: subprocess.Popen | None, streams: bool = True) -> None:
        """Bounded terminate → wait → kill → reap. Used for a late child and
        by close(); every wait is capped so one child that ignores
        TerminateProcess cannot hold shutdown open.

        `streams=False` defers the pipe close to `_close_streams`: closing a
        pipe handle another thread is blocked reading is undefined, so the
        shutdown path reaps first, joins the reader, and only then closes.
        """
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(_REAP_WAIT_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(_REAP_WAIT_S)
                except subprocess.TimeoutExpired:
                    pass        # the job handle close is the last backstop
        if streams:
            self._close_streams(proc)

    @staticmethod
    def _close_streams(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return          # close before the first birth, or a second close: normal
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _supervise(self) -> None:
        """Keep a capture running, forever, with a backoff that never gives up.

        The old behaviour was one retry and then a permanent `error`, which is the
        wrong shape for the real failure modes: the ETW session can be held by
        something that is about to exit, and the child can be killed by a sleep/resume
        cycle. In both cases the capture comes back on its own if asked again in a few
        seconds, and a panel that shows `--` for the rest of the afternoon because one
        spawn failed is a worse outcome than a child that retries quietly.
        """
        while not self._stop.is_set():
            t_start = time.monotonic()
            try:
                proc = self._spawn()
            except OSError as e:
                self._fail(f"could not start presentmon: {e}")
                if self._stop.wait(_RESTART_MAX_S):
                    return
                continue
            if proc is None:
                return            # close() won the race; this thread is done
            self.starts += 1
            try:
                self._read_stream(proc)
            except Exception:  # noqa: BLE001 - never let a parse bug kill the stream silently
                pass
            rc = proc.wait()
            if self._stop.is_set():
                return
            alive = time.monotonic() - t_start
            if self._restart_req:
                self._restart_req = False
                self.restarts += 1
                self._streak = 0
                self.error = None
                continue
            if alive > _HEALTHY_S:
                self._streak = 0          # it worked; whatever broke now starts fresh
            self._streak += 1
            delay = min(_RESTART_MAX_S, 2.0 ** min(self._streak, 6))
            self.ok = False
            tail = " | ".join(self._out[-3:]) or f"exit code {rc}"
            self.error = (f"presentmon exited after {alive:.0f}s: {tail} — retrying in "
                          f"{delay:.0f}s (attempt {self._streak})")
            if self._stop.wait(delay):
                return

    def restart(self, reason: str = "") -> None:
        """Throw the capture away and open a clean one. Called after a resume.

        An ETW session that survived suspend is the sort of thing that keeps its
        registration and delivers nothing, and the rings are full of timestamps from
        before the machine stopped — so both go. The panel keeps showing the held
        `stale` numbers until the new stream produces real ones.
        """
        self._restart_req = True
        with self._lock:
            self._rings.clear()
            self._pids.clear()
            self._pid_t.clear()
        self.rows = self.parsed = 0
        self._held.clear()
        self._last_t = 0.0
        self.ok = False
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if reason:
            self.error = f"restarting capture after {reason}"

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
        # GPU busyness for this frame, as a percentage of the frame's own span.
        busy = self._f(row, idx, "MsGPUBusy")
        span = between or self._f(row, idx, "MsGPUTime")
        gpu = max(0.0, min(100.0, 100.0 * busy / span)) if busy and span else None
        mode = 0
        if "PresentMode" in idx:
            m = (row[idx["PresentMode"]] or "").strip().lower()
            mode = 1 if m in _MODE_EXCLUSIVE else 0
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
            ring.append((t, between, display, now, gpu, mode))

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
        """Stop monitoring and take everything with us, deterministically.

        The old close() set a flag and terminated whatever child happened to
        be published at that instant. A supervisor parked in `_reclaim_session`
        - which sleeps for seconds inside a `subprocess.run` that close() never
        waited on - would wake after close() returned and spawn a fresh child
        that nobody owned, defeating the whole kill-on-exit promise while the
        app still ran. The lifecycle lock makes spawn and close mutually
        exclusive; _closed is checked on both sides of the birth inside that
        lock, so a child can only exist if close() will see it. Every wait is
        bounded, and the job handle is closed last so its kill-on-close fires
        even for a child that shrugged off terminate.
        """
        self._stop.set()
        # Set before touching the lock, and set even if the lock is held past
        # _CLOSE_LOCK_S: a spawn already holding the lock checks this flag on
        # both sides of the birth, so a child published after this point
        # disposes of itself, and one published before it is visible below.
        self._closed = True
        if self._life.acquire(timeout=_CLOSE_LOCK_S):
            try:
                proc, self._proc = self._proc, None
            finally:
                self._life.release()
        else:
            # A spawn is mid-flight and close() is done waiting for it.
            # Whatever it publishes next sees _closed and self-disposes;
            # whatever it already published is ours to reap here.
            proc = self._proc
        # Reap without the pipe handles yet: the reader may still be blocked
        # on them, and they are not safe to close until that thread is back.
        self._dispose(proc, streams=False)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_WAIT_S)      # the child is dead, so the read returns
        self._close_streams(proc)
        # Last backstop: releasing the job handle is the OS's kill switch for
        # any member our bounded terminate/kill failed to take down.
        self._job.close()

    # ------------------------------------------------------------------ queries
    def _best(self, pid: int, span_s: float) -> list:
        """The busiest of a process's swapchains over `span_s` of stream time.

        Two things in this method are deliberate. Summing swapchains is the tempting
        thing to do and it is wrong: a process with two of them (a benchmark overlay,
        a second window) would report double the frame rate it has, so the *busiest*
        one is the frame rate. And the window is stream time — the QPC column inside
        the rows — not arrival time, because arrival time is what a replayed CSV has
        no notion of: windowing by it reported a replay of a steady 60 fps as 180.
        """
        best: list = []
        for (p, _swap), ring in self._rings.items():
            if p != pid or not ring:
                continue
            newest = ring[-1][0]
            fresh = [r for r in reversed(ring) if r[0] >= newest - span_s * 1000.0]
            if len(fresh) > len(best):
                best = fresh
        return best

    @staticmethod
    def _summarise(rows: list) -> tuple:
        """(fps, median frametime, gpu %, exclusive?) for one swapchain's rows."""
        if not rows:
            return 0.0, None, None, False
        newest = rows[0][0]
        last1 = [r for r in rows if r[0] >= newest - 1000.0]
        allb = [r[1] for r in rows if r[1] is not None]
        b1 = [r[1] for r in last1 if r[1] is not None]
        # Frames per second from the *interval* between the newest and oldest frame in
        # the window, not from how many rows fell inside it: N timestamps span N-1
        # frames, and counting rows inflates every reading by one (a true 60 fps read
        # as 61, 48 as 49 — small, but it is a number the user compares against an
        # on-screen counter). Falls back to the median frametime when the window holds
        # a single frame.
        span = (last1[0][0] - last1[-1][0]) if len(last1) >= 2 else 0.0
        fps = ((len(last1) - 1) * 1000.0 / span if span > 0 else
               (1000.0 / float(np.median(allb)) if allb else 0.0)) if last1 else (
               1000.0 / float(np.median(allb)) if allb else 0.0)
        ms = float(np.median(b1)) if b1 else (float(np.median(allb)) if allb else None)
        g = [r[4] for r in last1 if r[4] is not None]
        gpu = float(np.median(g)) if g else None
        excl = bool(rows[0][5]) or sum(r[5] for r in rows[:40]) > 20
        return fps, ms, gpu, excl

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

    def presenters(self, min_fps: float | None = None) -> dict[int, Presenter]:
        """Processes actively rendering right now, and how hard.

        This is the detection signal: age < 2 s AND fps >= min_fps. The extras exist
        because "something is presenting" is not the same question as "a game is
        running" — a browser presenting 60 fps of scrolling, a video at 24, and a
        game at 120 all look identical to a frame counter, and differ completely in
        how busy the GPU is and how the frames reach the screen.
        """
        if not self.ok:
            return {}
        # Nobody is presenting anything at all if the stream itself has been quiet:
        # on a desktop that has gone still, DWM stops too, and without this the last
        # known frame rates would keep looking live forever.
        if time.monotonic() - self._last_row > 3.0:
            return {}
        floor = self.min_fps if min_fps is None else float(min_fps)
        with self._lock:
            self._sweep()
            out: dict[int, Presenter] = {}
            for pid, name in self._pids.items():
                t0 = self._pid_t.get(pid, 0.0)
                age = max(0.0, (self._last_t - t0) / 1000.0)
                if age > 2.0:
                    continue
                fps, _ms, gpu, excl = self._summarise(self._best(pid, 1.0))
                if fps >= floor:
                    out[pid] = Presenter(pid, name, fps, age, gpu, excl)
            return out

    def names(self) -> dict[int, str]:
        with self._lock:
            return dict(self._pids)

    def stats(self, pid: int) -> FrameStats | None:
        """Panel numbers for one process.

        Live numbers while it presents. Once it stops — minimised, alt-tabbed, a
        loading screen that renders nothing — the last measurement is held, dimmed
        and flagged `stale`, for `hold_s`; after that it is gone, and the panel shows
        `--`. Holding is the honest middle: a game you tabbed out of has not stopped
        having a frame rate, and inventing one, or dropping to `--` the instant the
        window loses focus, are the two wrong answers here.
        """
        if not self.ok or pid is None:
            return None
        hold = float(self.hold_s)
        with self._lock:
            rows = self._best(pid, self.window_s)
        if not rows:
            held = self._held.get(pid)
            if held and (time.monotonic() - held[1]) <= hold:
                out = held[0]
                return FrameStats(fps=out.fps, low1_pct=out.low1_pct,
                                  low01_pct=out.low01_pct, latency_ms=out.latency_ms,
                                  stale=True, age_s=time.monotonic() - held[1],
                                  gpu_pct=out.gpu_pct)
            self._held.pop(pid, None)
            return None
        newest_t = rows[0][0]
        newest_mono = rows[0][3]
        # Two gaps, both needed, and neither alone is enough: how far this process's
        # newest frame is behind the newest frame *in the stream* (a game that went
        # quiet while everything else keeps presenting), and how long it has been
        # since anything at all arrived (the whole stream stopped). Both are in the
        # stream's own clock or the child's arrival time, so a replayed CSV ages the
        # same way a live one does.
        age = max(0.0, (self._last_t - newest_t) / 1000.0) \
            + max(0.0, time.monotonic() - self._last_row)
        if age > hold:
            # The ring still remembers this process for `window_s`, but a number that
            # old is not a held measurement, it is a memory — and the panel has no way
            # to say "this was true 20 seconds ago" in the space it has. `--` is the
            # honest answer, and `hold_s` is the knob that decides where that line is.
            self._held.pop(pid, None)
            return None
        fps, ms, gpu, _excl = self._summarise(rows)
        between = [r[1] for r in rows if r[1] is not None]
        low1 = 1000.0 / float(np.percentile(between, 99)) if len(between) >= 30 else None
        low01 = 1000.0 / float(np.percentile(between, 99.9)) if len(between) >= 100 else None
        out = FrameStats(fps=fps if fps > 0 else None, low1_pct=low1, low01_pct=low01,
                         latency_ms=ms, stale=age > _FRESH_S, age_s=age, gpu_pct=gpu)
        if age <= _FRESH_S:
            self._held[pid] = (out, newest_mono)   # the last thing that was live
        return out
