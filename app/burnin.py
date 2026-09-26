"""Burn-in mitigation: global pixel-shift and the exercise sweep.

A static panel image ages unevenly, and this one shows the same four panels every
second of every day. Two counters, both config-driven: every `shift_every_min` the
layout is offset by 3 px around a 4-step cycle; for `exercise_s` every
`exercise_every_h` a full-screen colour gradient sweeps the panel.

Brightness, screen-off and night-mode warmth deliberately do not live here any
more. The old `tick_brightness()` owned part of that decision and `main.py` owned
the rest, which is how "the PC is going to sleep" and "Windows turned the displays
off" had nowhere to go — there were two half-policies and no place to put a third
input. One decision, one owner: `app/lights.py`. What stays here is the anti-aging
movement, which no other module has any business knowing about.

The idle clock moved too (`app/hoststate.idle_seconds`), because sleep, lock and
display state are read from the same place and the screen-off rule needs the same
number the power policy needs.
"""
from __future__ import annotations

import time

SHIFTS = [(0, 0), (3, 0), (3, 3), (0, 3)]  # px, cycles slowly


class BurnIn:
    def __init__(self, cfg: dict):
        self.cfg = cfg["burnin"]
        self._last_exercise_end = time.monotonic()
        self._exercise_start: float | None = None
        self.postponed = 0

    # ---- global layout shift ------------------------------------------------
    def shift(self) -> tuple[int, int]:
        minutes = time.time() / 60.0
        idx = int(minutes / float(self.cfg["shift_every_min"])) % len(SHIFTS)
        return SHIFTS[idx]

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

    def defer(self, now: float) -> None:
        """Push the exercise back: it only makes sense on a lit, unattended panel.

        A colour sweep on a dark panel at 3 a.m. — because the PC happens to be
        asleep-with-monitor-off or the user is in night mode and stepped away — is
        the one way this feature becomes a disturbance, so the clock restarts
        instead of running while nobody can see it.
        """
        if self._exercise_start is not None or now > self._last_exercise_end:
            self._last_exercise_end = now
            self._exercise_start = None
            self.postponed += 1
