"""One place decides what the panel looks like: dark, how bright, how warm.

Until now that decision was smeared across `burnin.tick_brightness()` (idle
timeout, a 5-minute dim, game-vs-idle) and `main.py`'s own brightness idea, and
the two could not express the things that actually matter: *the displays are off*,
*the PC is going to sleep*, *the user turned on night mode*. Centralised here as a
pure function of (host state, night state, telemetry state, idle), so the answer
is loggable, testable, and cannot disagree with itself.

Precedence, highest first — each line is a reason that ends up in log.log:

  dark  asleep            the PC is asleep or being told to sleep right now
  dark  locked            nobody is at the desk: the session is locked, or another
  dark  console-lost      session has taken the console (fast user switching)
  dark  monitor-off       Windows turned the displays off on their own timeout
  dark  idle              nobody touched the PC for `screen_off_after_min`, unless a live game holds it
  lit   game|idle|dim     the old behaviour, unchanged: game brightness while a
                          game is held, plain brightness otherwise, and the deep
                          dim once the desk has been quiet for `dim_after_s`
  warm  night             night mode caps the brightness (`brightness × scale`,
                          floored) and returns a per-channel LUT for the render

"Dark" means the backlight is off *and* the loop stops pushing: on revision C a
full frame is ~0.82 s of a 115200-baud link, so a dark panel is also the one
setting that costs the USB bus nothing. Nothing here guesses about the monitor —
`hoststate` reports `None` when no notification has arrived, and `None` means
"do not act on it", never "off".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.nightlight import warm_lut


@dataclass
class LightPlan:
    dark: bool = False
    reason: str = "lit"
    brightness: int = 45
    temp_k: int | None = None
    lut: list[int] | None = None
    night: bool = False
    # True when the panel must be repainted as a whole frame regardless of the
    # diff: after dark→lit, after a relink, after the LUT changed.
    repaint: bool = False
    # True when a live game is holding an idle timer that has already expired — the
    # reason the panel is still lit, and worth one line in the log (see `playing`).
    idle_held: bool = False

    def describe(self) -> str:
        if self.dark:
            return f"dark({self.reason})"
        s = f"lit {self.brightness}% {self.reason}"
        if self.idle_held:
            s += "+play"
        if self.night:
            s += f" warm={self.temp_k}K"
        return s


@dataclass
class LightPlanner:
    """Turns host/night/telemetry state into a `LightPlan`, once per tick."""

    cfg: dict
    night: object = None
    host: object = None
    brightness: int = -1
    dark: bool = False
    reason: str = "start"
    last_change: float = field(default=0.0, repr=False)
    transitions: int = 0

    def __post_init__(self) -> None:
        self.d = self.cfg["display"]
        self.n = self.cfg.get("night", {})
        self.mode = str(self.n.get("mode", "auto")).lower()
        self.scale = float(self.n.get("brightness_scale", 0.55))
        self.floor = int(self.n.get("brightness_floor", 8))
        self.strength = float(self.n.get("strength", 1.0))
        self.temp_default = int(self.n.get("color_temp_k", 2700))
        self.follow_display = bool(self.cfg.get("power", {}).get("follow_display", True))
        self.follow_sleep = bool(self.cfg.get("power", {}).get("follow_sleep", True))
        self.follow_lock = bool(self.cfg.get("power", {}).get("follow_lock", True))
        self.dim_after_s = float(self.d.get("dim_after_s", 300))
        self.off_after_s = float(self.d.get("screen_off_after_min", 45)) * 60.0
        self.idle_hold = bool(self.d.get("stay_lit_in_game", True))
        self._lut_key: tuple | None = None
        self._lut: list[int] | None = None

    # ------------------------------------------------------------------ tick
    def tick(self, state: str, idle_s: float, dt: float,
             asleep: bool | None = None, monitor_on: bool | None = None,
             forced_dark: str | None = None, locked: bool | None = None,
             console_lost: bool | None = None,
             playing: bool = False) -> LightPlan:
        """`idle_s` from the host clock; the host facts default to the HostState.

        `playing` means the locked game target is presenting *right now* (live frame
        numbers, not held ones). It holds the two idle timers, because
        `GetLastInputInfo` only counts keyboard and mouse: a player on a controller
        looks indistinguishable from an empty desk, and without this the panel would
        dim after five minutes and go dark after forty-five while someone was mid-game.
        """
        if asleep is None:
            asleep = bool(getattr(self.host, "asleep", False))
        if monitor_on is None:
            monitor_on = getattr(self.host, "monitor_on", None)
        if locked is None:
            locked = bool(getattr(self.host, "locked", False))
        if console_lost is None:
            console_lost = bool(getattr(self.host, "console_lost", False))
        # Only a *live* game holds the timer. Alt-tabbed, minimised or loading, the
        # numbers go `stale`, and then the desk really is unattended as far as anyone
        # can tell — so the timers resume immediately rather than after a grace period.
        hold_idle = self.idle_hold and playing

        dark_reason = ""
        if forced_dark:
            dark_reason = forced_dark
        elif self.follow_sleep and asleep:
            dark_reason = "asleep"
        elif self.follow_lock and (locked or console_lost):
            # The clearest "away" there is: the keyboard, the games and the screen all
            # belong to somebody else now, so whatever this panel would be showing, no
            # one is there to see it. It outranks monitor-off because a lock usually
            # happens with the displays still awake, and it outranks the idle timer
            # because it is a fact rather than a guess about how long someone has been
            # out of the room.
            dark_reason = "locked" if locked else "console-lost"
        elif self.follow_display and monitor_on is False:
            dark_reason = "monitor-off"
        elif idle_s > self.off_after_s and not hold_idle:
            dark_reason = "idle"

        night_on = bool(self.night is not None and getattr(self.night, "on", False))
        plan = LightPlan(dark=bool(dark_reason), reason=dark_reason or "lit",
                         night=night_on)

        if plan.dark:
            plan.brightness = 0
        else:
            level = int(self.d["brightness_game"] if state == "game"
                        else self.d["brightness_idle"])
            if idle_s > self.dim_after_s and not hold_idle:
                level = int(self.d["brightness_dim"])
                plan.reason = "dim"
            if night_on:
                cap = max(self.floor, round(level * self.scale))
                if cap < level:
                    level = cap
                    plan.reason += "+night"
            plan.brightness = max(0, min(100, level))
            if night_on:
                plan.temp_k = int(getattr(self.night, "temp_k", 0) or self.temp_default)
                # The night object owns the warmth decision: a colour temperature
                # when the evidence is Windows' setting, the measured gamma ramp when
                # the evidence is the ramp itself (f.lux-shaped). Keyed so the LUT is
                # rebuilt only when the look actually changes.
                gains = getattr(self.night, "gains", None)
                key = (plan.temp_k, self.strength, None if gains is None else
                       tuple(round(x, 3) for x in gains))
                if key != self._lut_key:
                    self._lut = self.night.lut(self.strength)
                    self._lut_key = key
                    plan.repaint = True     # the colours changed: full frame
                plan.lut = self._lut

        # Say what was *not* acted on: an expired idle timer that a live game is
        # holding is the difference between "the panel is lit" and "the panel is
        # ignoring the timer", and the log has to be able to tell them apart. Only when
        # the panel is actually lit — claiming a hold while some other reason has it
        # dark (asleep, locked, displays off) would be a sentence about a decision the
        # planner never got to make.
        plan.idle_held = bool(hold_idle and not plan.dark
                               and idle_s > self.dim_after_s)

        # repaint whenever the panel has to be re-told everything
        if plan.dark != self.dark:
            if not plan.dark:
                plan.repaint = True        # coming back from dark: whole frame
            self.dark = plan.dark
            self.transitions += 1
        if plan.reason != self.reason:
            self.transitions += 1
        self.reason = plan.reason
        self.brightness = plan.brightness if not plan.dark else self.brightness
        return plan

    def summary(self) -> str:
        return (f"lit={not self.dark} reason={self.reason} bright={self.brightness}% "
                f"night={self.mode} transitions={self.transitions}")
