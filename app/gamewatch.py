"""Which process is the game, and when does the panel switch to game mode.

The old rule was "the foreground process is presenting frames", with hysteresis.
Two of the complaints this repo exists to fix came straight out of that:

  * tab out of a game to a browser and the panel switched to the browser and
    reported *its* frame rate — the foreground process changed, so the answer
    changed, even though the game was the only thing still rendering hard;
  * switching took four seconds every time and was wrong often enough to notice,
    because one threshold was used for evidence of very different quality.

So detection is now scored by confidence and the frame target is *locked*:

  STRONG  foreground process, presenting, covering its monitor   — enters in ~1 s
          (or a process named in `game.processes`, on the same clock)
  MED     foreground+presenting without full coverage, a Steam-flagged presenter,
          a presenter the GPU is working for (>= `min_gpu_score` % of the frame),
          or a hardware-flip presenter that is not a known application
          enters on the configured `enter_after_s`
  LEGACY  the window heuristic (covers the monitor, borderless, GPU busy) — used
          when the present stream is unavailable or below its threshold
  NONE    a known non-game presenter: a browser, a video player, the shell, a
          chat app. This is what stops a scrolling page or a 4K trailer from
          looking like a game at 60 fps — a frame counter cannot tell them apart,
          a name and a GPU-busy figure can.

Once in game mode, the frame target is the *locked pid*, and nothing steals it:
other presenters are ignored while it lives, whether it is foreground or not, so a
browser, an overlay, or a second monitor's video cannot move the numbers. It is
released when the process dies (quickly — `dead_exit_s`) or when it has stopped
presenting entirely for `exit_after_s` (so an alt-tab keeps the last measurement,
dimmed, exactly as `app/frames.py` holds it). A new STRONG candidate that has been
alone for `switch_silence_s` takes the lock, which is how starting game B from
game A's desktop works.

SteamAppId/SteamGameId (app/steamid.py) is identity and enrichment — it names the
game and keys per-game profiles — and a tiebreaker here, never the core signal:
protected processes block the environment read and direct-launched exes bypass
Steam entirely.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

import psutil

WS_CAPTION = 0x00C00000
MONITOR_DEFAULTTONEAREST = 2

# Names that present frames all day and are never the game. Anything in here is
# demoted to NONE even when it is foreground and presenting at 144 Hz, so that
# `game.ignore` (a veto on *detection*) and this list (a veto on *being the game*)
# stay different knobs: a game running in an emulator you ignore should still be
# detectable another way, while chrome.exe should not matter how busy it gets.
NON_GAME = (
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "360chromex.exe", "qqbrowser.exe", "sogouexplorer.exe",
    "spotify.exe", "discord.exe", "telegram.exe", "wechat.exe", "qq.exe",
    "dingtalk.exe", "teams.exe", "zoom.exe", "skype.exe",
    "explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "widgets.exe", "widgetservice.exe", "shellexperiencehost.exe",
    "taskmgr.exe", "mmc.exe", "code.exe", "devenv.exe", "pycharm64.exe",
    "obs64.exe", "obs32.exe", "streamlabs.exe", "nvcontainer.exe",
    "nvidiabar.exe", "gameviewer.exe", "geforceexperience.exe",
    "powerpnt.exe", "winword.exe", "excel.exe", "msedgewebview.app.exe",
    "widgetboard.exe", "lockapp.exe", "logonui.exe", "systemsettings.exe",
    "applicationframehost.exe", "windowsinternal.composableshell.experiences",
    "video.ui.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
)
# A presenter whose name matches nothing else and whose GPU busyness is under this
# is a compositor or a widget, not a title doing work.
_GPU_STRONG = 55.0


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


def _foreground_info():
    """(pid, process_name, covers_monitor, borderless) of the foreground window.
    Covers is measured against the monitor the window is actually on, so a
    fullscreen game on any display counts, not just the primary."""
    if _user32 is None:
        return None, None, False, False
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None, None, False, False

    rect = wintypes.RECT()
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    covers = False
    if _user32.GetWindowRect(hwnd, ctypes.byref(rect)) and \
            _user32.GetMonitorInfoW(_user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST),
                                    ctypes.byref(mi)):
        m = mi.rcMonitor
        # tolerate up to 2 px of rounding/shadow; allow down to 0.9 coverage
        w, h = m.right - m.left, m.bottom - m.top
        rw, rh = rect.right - rect.left, rect.bottom - rect.top   # wintypes.RECT has no accessors
        covers = (rw * rh >= 0.9 * w * h
                  and rect.left <= m.left + 2 and rect.top <= m.top + 2
                  and rect.right >= m.right - 2 and rect.bottom >= m.bottom - 2)

    style = _user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
    borderless = not (style & WS_CAPTION)

    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name().lower()
    except psutil.Error:
        name = None
    return pid.value, name, covers, borderless


def _alive(pid: int, since: float | None = None) -> bool:
    """Is this pid still the process we locked onto? `since` is its create_time."""
    try:
        p = psutil.Process(pid)
        return since is None or abs(p.create_time() - since) < 1.0
    except psutil.Error:
        return False


STRONG, MED, LEGACY, NONE = 3, 2, 1, 0


class Candidate:
    def __init__(self, pid: int, name: str, tier: int, why: str, fps: float = 0.0,
                 gpu: float | None = None) -> None:
        self.pid, self.name, self.tier, self.why = pid, name, tier, why
        self.fps, self.gpu = fps, gpu

    def __repr__(self) -> str:
        g = "-" if self.gpu is None else f"{self.gpu:.0f}%"
        return f"{self.name or '?'}({self.pid}) {self.tier}:{self.why} {self.fps:.0f}fps gpu={g}"


class GameWatch:
    IDLE, GAME = "idle", "game"

    def __init__(self, cfg: dict):
        self.cfg = cfg["game"]
        det = cfg.get("detection", {})
        self.state = self.IDLE
        self.game_pid: int | None = None
        self.game_name: str | None = None
        self.steam_appid: str | None = None
        self.evidence = "-"                 # what the last decision was based on
        self.switches = 0                   # lock changes, for the log
        self.enter_after_s = float(self.cfg["enter_after_s"])
        self.exit_after_s = float(self.cfg["exit_after_s"])
        # A STRONG candidate needs only long enough to be sure it is not a flicker
        # of focus: one second, against the old flat four.
        self.enter_strong_s = float(det.get("enter_strong_s",
                                            max(1.0, self.enter_after_s / 3.0)))
        self.dead_exit_s = float(det.get("dead_exit_s", 3.0))
        self.switch_silence_s = float(det.get("switch_silence_s", 3.0))
        self.gpu_strong = float(det.get("min_gpu_score", _GPU_STRONG))
        self.non_game = tuple(str(x).lower() for x in (det.get("non_game") or NON_GAME))
        self._self_pid = os.getpid()
        self._in_s = 0.0
        self._out_s = 0.0
        self._quiet_s = 0.0
        self._dead_s = 0.0
        self._create_time: float | None = None
        self._pending: Candidate | None = None
        self._last_tier = NONE

    # ------------------------------------------------------------------ scoring
    def _is_non_game(self, name: str | None) -> bool:
        n = (name or "").lower()
        return any(n == x or n.startswith(x) for x in self.non_game)

    def _score(self, pid: int, pr, fg_pid: int, fg_name: str | None, covers: bool,
               steam, ignore: tuple) -> Candidate:
        """One presenting process → how much it looks like the game."""
        name = (pr.name or "").lower()
        if pid == self._self_pid:
            # With a simulated panel (`revision: SIMU`, `tools/liveview.py`) the app
            # presents through DWM itself, in the foreground, at 60 fps — which scores
            # as a game and would lock the panel onto its own preview window. Nothing
            # we render is ever the thing we are measuring.
            return Candidate(pid, pr.name, NONE, "ourselves", pr.fps, pr.gpu)
        if name in ignore or self._is_non_game(name):
            return Candidate(pid, pr.name, NONE, "non-game", pr.fps, pr.gpu)
        if name in [p.lower() for p in self.cfg["processes"]]:
            return Candidate(pid, pr.name, STRONG, "configured", pr.fps, pr.gpu)
        foreground = pid == fg_pid
        steam_game = bool(steam is not None and steam.is_steam_game(pid))
        if foreground and covers:
            return Candidate(pid, pr.name, STRONG, "foreground+presenting",
                             pr.fps, pr.gpu)
        why, tier = [], MED
        if foreground:
            why.append("foreground")
        if steam_game:
            why.append("steam")
        if pr.exclusive:
            why.append("exclusive-flip")
        if pr.gpu is not None and pr.gpu >= self.gpu_strong:
            why.append(f"gpu{pr.gpu:.0f}%")
        if not why:
            return Candidate(pid, pr.name, NONE, "background presenter", pr.fps, pr.gpu)
        if foreground and (fg_name or name) and self._is_non_game(fg_name):
            tier = NONE
        return Candidate(pid, pr.name, tier, "+".join(why), pr.fps, pr.gpu)

    def _best(self, presenters, fg_pid, fg_name, covers, steam, ignore) -> Candidate | None:
        best: Candidate | None = None
        also = []
        for pid, pr in presenters.items():
            c = self._score(pid, pr, fg_pid, fg_name, covers, steam, ignore)
            if c.tier == NONE:
                also.append(c)
                continue
            key = (c.tier, c.pid == fg_pid, c.gpu or 0.0, c.fps)
            if best is None or key > (best.tier, best.pid == fg_pid, best.gpu or 0.0,
                                      best.fps):
                best = c
        if best is None and also:
            self.evidence = "no candidate: " + ", ".join(repr(c) for c in also[:3])
        return best

    # --------------------------------------------------------------------- tick
    def _idle(self, dt: float, cand: Candidate | None) -> None:
        if cand is None:
            self._in_s = 0.0
            self._pending = None
            return
        need = self.enter_after_s if cand.tier < STRONG else self.enter_strong_s
        if self._pending is None or self._pending.pid != cand.pid:
            self._in_s = 0.0
        self._pending = cand
        self._in_s += dt
        if self._in_s >= need:
            self._lock(cand.pid, cand.name)
            self.state = self.GAME
            self.switches += 1
            self.evidence = f"enter {cand!r} after {self._in_s:.1f}s"
            self._in_s = 0.0
            self._quiet_s = self._dead_s = 0.0

    def _held(self, dt: float, presenters, ctx, steam, ignore) -> bool:
        """Game mode: the lock decides, and nothing else gets to move the numbers.

        `presenters` is None when the present stream is unavailable — which is a
        different fact from an empty dict ("nothing is presenting") and has to be
        treated as one, or a machine without the ETW capture would drop out of game
        mode every time the window heuristic could not see a present.
        """
        fg_pid, fg_name, covers, borderless, snap = ctx
        if presenters is None:
            busy = (snap.gpu.load_pct is not None
                    and snap.gpu.load_pct >= float(self.cfg["min_gpu_load"]))
            same = fg_pid == self.game_pid and covers and borderless and busy
            if same:
                self._quiet_s = 0.0
                self.evidence = f"held {self.game_name}({self.game_pid}) on window heuristic"
                return True
            self._quiet_s += dt
            self.evidence = (f"window heuristic lost {self.game_name}({self.game_pid}) "
                             f"({self._quiet_s:.0f}s)")
            if self._quiet_s >= self.exit_after_s:
                self._release("window no longer covers with the GPU busy")
                return False
            return True

        pr = presenters.get(self.game_pid)
        if pr is not None:
            self._quiet_s = 0.0
            self._dead_s = 0.0
            self.evidence = (f"held {self.game_name}({self.game_pid}) {pr.fps:.0f}fps "
                             f"gpu={'-'}" if pr.gpu is None else
                             f"held {self.game_name}({self.game_pid}) {pr.fps:.0f}fps "
                             f"gpu={pr.gpu:.0f}%")
            return True

        # Not presenting right now. Alive? Then this is an alt-tab, a loading screen
        # or a paused game: stay, and let app/frames.py hold the last measurement.
        if not _alive(self.game_pid, self._create_time):
            self._dead_s += dt
            self._quiet_s += dt
            self.evidence = f"target {self.game_name}({self.game_pid}) gone " \
                            f"{self._dead_s:.0f}s"
            if self._dead_s >= self.dead_exit_s:
                self._release("target process exited")
                return False
            return True

        self._quiet_s += dt
        self._dead_s = 0.0
        self.evidence = f"target {self.game_name}({self.game_pid}) silent {self._quiet_s:.0f}s"
        if self._quiet_s >= self.exit_after_s:
            self._release(f"target stopped presenting for {self._quiet_s:.0f}s")
            return False

        # Something else is confidently rendering while our target is quiet: a new
        # game started. Give it the lock rather than sitting on a dead one.
        cand = self._best(presenters, fg_pid or -1, fg_name, covers, steam, ignore)
        if (cand is not None and cand.tier >= MED and cand.pid != self.game_pid
                and self._quiet_s >= self.switch_silence_s):
            old = self.game_pid
            self._lock(cand.pid, cand.name)
            self.switches += 1
            self.evidence = f"switch {old}→{cand!r} after {self._quiet_s:.0f}s silence"
            self._quiet_s = 0.0
        return True

    def _lock(self, pid: int, name: str | None) -> None:
        self.game_pid = pid
        self.game_name = name
        try:
            self._create_time = psutil.Process(pid).create_time()
        except psutil.Error:
            self._create_time = None

    def _release(self, why: str) -> None:
        self.state = self.IDLE
        self.game_pid = None
        self.game_name = None
        self.steam_appid = None
        self._create_time = None
        self._in_s = self._quiet_s = self._dead_s = 0.0
        self.evidence = f"exit: {why}"

    def tick(self, snap, dt: float, presenters=None, steam=None) -> str:
        """One step. `presenters` is `frames.presenters()`, or None when the present
        stream is unavailable — see `_held`: the two are different facts."""
        dt = min(max(dt, 0.0), 5.0)      # a stalled loop must not fake a long timer
        ignore = tuple(str(p).lower() for p in self.cfg["ignore"])
        fg_pid, fg_name, covers, borderless = _foreground_info()
        ctx = (fg_pid, fg_name, covers, borderless, snap)

        if self.state == self.GAME:
            if self._held(dt, presenters, ctx, steam, ignore):
                if self.game_pid is not None and steam is not None:
                    self.steam_appid = steam.appid(self.game_pid)
                self._last_tier = STRONG
                return self.state
            return self.state

        cand = None
        if presenters and self.cfg.get("present_detection", True):
            cand = self._best(presenters, fg_pid or -1, fg_name, covers, steam, ignore)
        if cand is None and fg_pid is not None and fg_pid != self._self_pid \
                and fg_name not in ignore \
                and not self._is_non_game(fg_name) and covers and borderless:
            gpu = snap.gpu
            # legacy window heuristic: also the path when the game runs below the
            # present threshold (a light title, a 20 fps cap) or presentmon is out
            busy = gpu.load_pct is not None and gpu.load_pct >= float(self.cfg["min_gpu_load"])
            if busy:
                cand = Candidate(fg_pid, fg_name, LEGACY, "window heuristic", 0.0,
                                 gpu.load_pct)
        self._last_tier = cand.tier if cand else NONE
        self._idle(dt, cand)
        if self.state == self.GAME and self.game_pid is not None and steam is not None:
            self.steam_appid = steam.appid(self.game_pid)
        return self.state

    def reset(self, reason: str = "") -> None:
        """Forget the lock — after a resume, when every assumption is stale."""
        if self.game_pid is not None:
            self._release(f"reset {reason}".strip())
        self._pending = None
        self._in_s = 0.0

    def summary(self) -> str:
        t = {STRONG: "strong", MED: "med", LEGACY: "legacy", NONE: "none"}[self._last_tier]
        s = f"state={self.state} tier={t} evidence={self.evidence}"
        if self.game_pid:
            s += f" target={self.game_name}({self.game_pid}) quiet={self._quiet_s:.0f}s"
        if self.switches:
            s += f" switches={self.switches}"
        return s
