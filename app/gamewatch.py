"""Game/load state machine.

GAME state heuristic (no game-process database required):
  foreground window covers (nearly) the whole primary monitor AND has no
  title-bar style (exclusive/fullscreen or borderless fullscreen) AND GPU is
  at least minimally busy AND the process isn't on the ignore list.
Hysteresis: must hold for enter_after_s to enter, and clear for
exit_after_s to leave (survives alt-tabs, loadings, brief desktop visits).

Explicit process names from config always trigger GAME.

Frame stats (fps/1%-low/latency) will come from PresentMon in a later phase;
for now FrameStats stays empty unless a backend fills it (demo).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import psutil

WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


def _foreground_info():
    """Return (process_name_lower, is_fullscreen) of the foreground window."""
    if _user32 is None:
        return None, False
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None, False

    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    sw = _user32.GetSystemMetrics(0)
    sh = _user32.GetSystemMetrics(1)
    covers = (rect.left <= 2 and rect.top <= 2
              and rect.right >= sw - 2 and rect.bottom >= sh - 2)

    style = _user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
    borderless = not (style & WS_CAPTION)

    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name().lower()
    except psutil.Error:
        name = None
    return name, (covers and borderless)


class GameWatch:
    IDLE, GAME = "idle", "game"

    def __init__(self, cfg: dict):
        self.cfg = cfg["game"]
        self.state = self.IDLE
        self._in_s = 0.0
        self._out_s = 0.0

    def _cond(self, snap) -> bool:
        name, fullscreen = _foreground_info()
        if name is None:
            return False
        if name in [p.lower() for p in self.cfg["processes"]]:
            return True
        if name in [p.lower() for p in self.cfg["ignore"]]:
            return False
        if not self.cfg["fullscreen_heuristic"] or not fullscreen:
            return False
        gpu = snap.gpu
        busy = gpu.load_pct is not None and gpu.load_pct >= float(self.cfg["min_gpu_load"])
        return busy

    def tick(self, snap, dt: float) -> str:
        if self._cond(snap):
            self._in_s += dt
            self._out_s = 0.0
        else:
            self._out_s += dt
            self._in_s = 0.0

        if self.state == self.IDLE and self._in_s >= float(self.cfg["enter_after_s"]):
            self.state = self.GAME
        elif self.state == self.GAME and self._out_s >= float(self.cfg["exit_after_s"]):
            self.state = self.IDLE
        return self.state
