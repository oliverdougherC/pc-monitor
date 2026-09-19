"""Burn-in mitigation: brightness schedule, global pixel-shift, exercise sweep."""
from __future__ import annotations

import ctypes
import time

SHIFTS = [(0, 0), (3, 0), (3, 3), (0, 3)]  # px, cycles slowly


def _system_idle_seconds() -> float:
    """Seconds since last keyboard/mouse input (Windows)."""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
            return max(0.0, millis / 1000.0)
    except Exception:
        pass
    return 0.0


class BurnIn:
    def __init__(self, cfg: dict):
        self.cfg = cfg["burnin"]
        self.dcfg = cfg["display"]
        self._brightness: int | None = None
        self._screen_on = True
        self._last_exercise_end = time.monotonic()
        self._exercise_start: float | None = None

    # ---- global layout shift ------------------------------------------------
    def shift(self) -> tuple[int, int]:
        minutes = time.time() / 60.0
        idx = int(minutes / float(self.cfg["shift_every_min"])) % len(SHIFTS)
        return SHIFTS[idx]

    # ---- brightness / screen-off --------------------------------------------
    def tick_brightness(self, state: str, lcd) -> None:
        idle_s = _system_idle_seconds()
        if idle_s > self.dcfg["screen_off_after_min"] * 60:
            if self._screen_on:
                lcd.ScreenOff()
                self._screen_on = False
            return
        if not self._screen_on:
            lcd.ScreenOn()
            self._screen_on = True

        if state == "game":
            level = int(self.dcfg["brightness_game"])
        elif idle_s > 300:
            level = int(self.dcfg["brightness_dim"])
        else:
            level = int(self.dcfg["brightness_idle"])

        if level != self._brightness:
            lcd.SetBrightness(level)
            self._brightness = level

    # ---- periodic exercise animation -----------------------------------------
    def exercise_due(self, now: float) -> bool:
        every = float(self.cfg["exercise_every_h"]) * 3600
        if self._exercise_start is None:
            if now - self._last_exercise_end >= every:
                self._exercise_start = now
            return False
        return True

    def exercise_progress(self, now: float) -> float | None:
        """0..1 while the sweep runs, None when not running."""
        if self._exercise_start is None:
            return None
        dur = float(self.cfg["exercise_s"])
        p = (now - self._exercise_start) / dur
        if p >= 1.0:
            self._exercise_start = None
            self._last_exercise_end = now
            return None
        return p
