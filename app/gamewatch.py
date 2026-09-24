"""Game/load state machine.

Detection is layered, strongest signal first:

  1. PRESENT (needs frames.FrameMonitor, i.e. elevated): the foreground process
     is actively presenting frames at >= frames.min_present_fps ("watch for a
     graphics process" — ETW makes it truthful). A sticky game pid keeps the
     state while it keeps presenting (alt-tabs, launchers overlaying it), and a
     Steam-flagged foreground process adopts its presenting render-child.
  2. LEGACY WINDOW HEURISTIC (always available): foreground window covers its
     own monitor (multi-monitor aware: the monitor the window is ON, not the
     primary), has no title-bar style, GPU minimally busy, not ignored.
  3. Explicit `game.processes` always trigger; `game.ignore` always vetoes.

Hysteresis: must hold for enter_after_s to enter, and clear for exit_after_s to
leave (survives alt-tabs, loadings, brief desktop visits).

SteamAppId/SteamGameId (app/steamid.py) is identity/enrichment — it names the
game and keys per-game profiles — never the detection core, because protected
processes block the env read and direct-launched exes bypass Steam.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import psutil

WS_CAPTION = 0x00C00000
MONITOR_DEFAULTTONEAREST = 2


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


class GameWatch:
    IDLE, GAME = "idle", "game"

    def __init__(self, cfg: dict):
        self.cfg = cfg["game"]
        self.state = self.IDLE
        self.game_pid: int | None = None
        self.steam_appid: str | None = None
        self._in_s = 0.0
        self._out_s = 0.0

    # ------------------------------------------------------------------ signals
    def _pick(self, fg_pid, presenters, steam) -> int | None:
        """Choose which presenting process IS the game."""
        if fg_pid in presenters:
            return fg_pid
        if steam:
            steam_fps = [(pr.fps, pid) for pid, pr in presenters.items()
                         if steam.is_steam_game(pid)]
            if steam_fps:
                return max(steam_fps)[1]
        return None

    def _cond(self, snap, presenters, steam) -> bool:
        fg_pid, name, covers, borderless = _foreground_info()
        if fg_pid is None:
            return False
        ignore = [p.lower() for p in self.cfg["ignore"]]
        if name in [p.lower() for p in self.cfg["processes"]]:
            self.game_pid = fg_pid
            return True
        if name in ignore:
            return False

        if presenters is not None and self.cfg.get("present_detection", True):
            sticky = (self.game_pid in presenters
                      and presenters[self.game_pid].name.lower() not in ignore)
            if sticky:
                return True
            pick = self._pick(fg_pid, presenters, steam)
            if pick is not None:
                self.game_pid = pick
                return True
        # legacy path — also the fallback when the game is under the present
        # threshold (light title, 20 fps cap) or presentmon is unavailable
        gpu = snap.gpu
        busy = gpu.load_pct is not None and gpu.load_pct >= float(self.cfg["min_gpu_load"])
        if covers and borderless and busy:
            if self.game_pid is None or not presenters or self.game_pid not in presenters:
                self.game_pid = fg_pid
            return True
        return False

    # --------------------------------------------------------------------- tick
    def tick(self, snap, dt: float, presenters=None, steam=None) -> str:
        if self._cond(snap, presenters, steam):
            self._in_s += dt
            self._out_s = 0.0
        else:
            self._out_s += dt
            self._in_s = 0.0

        if self.state == self.IDLE and self._in_s >= float(self.cfg["enter_after_s"]):
            self.state = self.GAME
        elif self.state == self.GAME and self._out_s >= float(self.cfg["exit_after_s"]):
            self.state = self.IDLE
            self.game_pid = None

        if self.state == self.GAME and self.game_pid is not None and steam is not None:
            self.steam_appid = steam.appid(self.game_pid)
        return self.state
