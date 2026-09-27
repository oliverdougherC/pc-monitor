"""Replay the light decision — sleep, displays off, idle, night — without a desk.

    .venv\\Scripts\\python tools\\lights_selftest.py

`app/lights.py` is the single answer to "what should the panel look like", and every
one of those answers used to live in a different place (the idle timeout in
`burnin.py`, the brightness in `main.py`, the monitor and sleep cases nowhere). That
makes it the module most able to be subtly wrong, and the wrongness is the visible
kind: a panel that goes dark during a game, or one that stays bright at 3 a.m.

So each precedence line is exercised against the real planner with a stub host and a
stub night object, at the exact thresholds pinned below — including the two that are
easy to get backwards: `monitor_on is None` must mean *unknown*, never *off*, and the
dim has to come before the screen-off, not instead of it. The last case draws a real
frame twice, with and without the LUT, and measures the pixels: the plan can be right
about "warm" while the picture stays cold, and that is the case a reader cannot check
by eye in a log.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

from app import config as cfgmod          # noqa: E402
from app.lights import LightPlanner       # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


class Host:
    """The facts `lights` reads from `hoststate`, and nothing else."""

    def __init__(self, asleep=False, monitor_on=None, locked=False,
                 console_lost=False):
        self.asleep = asleep
        self.monitor_on = monitor_on
        self.locked = locked
        self.console_lost = console_lost


class Night:
    """A `NightLight`-shaped stub: the same attributes, with the file reads taken out."""

    def __init__(self, on=False, temp_k=2500, gains=None):
        self.on = on
        self.temp_k = temp_k
        self.gains = gains
        self.built = 0            # how many times a LUT was actually computed

    def lut(self, strength):
        self.built += 1
        return [min(255, int(i * 0.6)) for i in range(256)]      # warm stand-in


def cfg(**over) -> dict:
    """The real config with the numbers these assertions depend on pinned, so editing
    `config.yaml` cannot silently change what the test means. `cfg(power={...})`
    overrides one section at a time."""
    c = cfgmod.load(None)
    c["display"] = dict(c["display"], brightness_idle=45, brightness_game=70,
                        brightness_dim=12, dim_after_s=300, screen_off_after_min=45,
                        stay_lit_in_game=True)
    c["night"] = dict(c.get("night", {}), mode="auto", brightness_scale=0.5,
                      brightness_floor=8, strength=1.0, color_temp_k=2700)
    c["power"] = dict(c.get("power", {}), follow_sleep=True, follow_lock=True,
                      follow_display=True)
    for key, val in over.items():
        c[key] = dict(c.get(key, {}), **val) if isinstance(val, dict) else val
    return c


def planner(night=None, host=None, config=None) -> LightPlanner:
    return LightPlanner(config or cfg(), night=night, host=host)


def case_lit() -> None:
    print("case: a desk in use")
    p = planner(Night(on=False), Host())
    a = p.tick("idle", 5.0, 1.0)
    b = p.tick("game", 5.0, 1.0)
    check("idle brightness from config", (a.dark, a.reason, a.brightness),
          (False, "lit", 45))
    check("game brightness from config", (b.reason, b.brightness), ("lit", 70))
    check("no LUT while day-lit", a.lut, None)
    check("describe is readable", a.describe(), "lit 45% lit")


def case_dim_and_off() -> None:
    print("case: a quiet desk dims, then goes dark")
    p = planner(Night(on=False), Host())
    a = p.tick("idle", 299.0, 1.0)
    b = p.tick("idle", 301.0, 1.0)
    c = p.tick("idle", 45 * 60 + 1.0, 1.0)
    check("still full before dim_after_s", (a.reason, a.brightness), ("lit", 45))
    check("dim after dim_after_s", (b.reason, b.brightness), ("dim", 12))
    check("dark after screen_off", (c.dark, c.reason, c.brightness), (True, "idle", 0))
    check("dim comes before dark, not instead of it", b.dark, False)


def case_sleep() -> None:
    print("case: the PC sleeps")
    p = planner(Night(on=False), Host(asleep=True))
    a = p.tick("game", 5.0, 1.0)
    check("dark, named asleep", (a.dark, a.reason), (True, "asleep"))
    check("a running game does not keep it lit", a.reason, "asleep")
    back = p.tick("game", 5.0, 1.0)
    check("staying dark asks for no repaint", back.repaint, False)
    p.host.asleep = False
    up = p.tick("game", 5.0, 1.0)
    check("comes back lit", (up.dark, up.reason), (False, "lit"))
    check("and demands a whole frame", up.repaint, True)
    # and the switch that disables the behaviour must actually disable it
    q = planner(Night(on=False), Host(asleep=True),
                config=cfg(power={"follow_sleep": False}))
    r = q.tick("idle", 5.0, 1.0)
    check("follow_sleep: false ignores the sleep", (r.dark, r.reason), (False, "lit"))


def case_locked() -> None:
    print("case: the desk is locked")
    h = Host(locked=True)
    p = planner(Night(on=False), h)
    a = p.tick("game", 5.0, 1.0)
    check("dark, named locked", (a.dark, a.reason), (True, "locked"))
    h.locked = False
    up = p.tick("game", 5.0, 1.0)
    check("back to the game on unlock", (up.dark, up.reason, up.brightness),
          (False, "lit", 70))
    check("and a whole frame", up.repaint, True)
    # fast user switching: our session is still running, it just has no console
    b = planner(Night(on=False), Host(console_lost=True)).tick("idle", 5.0, 1.0)
    check("console taken by another session", (b.dark, b.reason), (True, "console-lost"))
    c = planner(Night(on=False), Host(locked=True),
                config=cfg(power={"follow_lock": False})).tick("idle", 5.0, 1.0)
    check("follow_lock: false ignores it", (c.dark, c.reason), (False, "lit"))


def case_playing() -> None:
    print("case: a game on a controller is not an empty desk")
    # The idle clock measures keyboard and mouse only, so at 301 s (past the dim) and
    # 99 min (past screen-off) a player mid-game looks exactly like nobody at all.
    p = planner(Night(on=False), Host())
    held = p.tick("game", 301.0, 1.0, playing=True)
    check("no dim while frames are live", (held.reason, held.brightness), ("lit", 70))
    check("and the plan says why", (held.idle_held, "+play" in held.describe()),
          (True, True))
    plain = p.tick("game", 301.0, 1.0, playing=False)
    check("the same idle without frames dims", (plain.reason, plain.brightness,
                                                plain.idle_held), ("dim", 12, False))
    deep = p.tick("game", 99 * 60.0, 1.0, playing=True)
    check("no screen-off during play", (deep.dark, deep.brightness), (False, 70))
    check("still marked as holding", deep.idle_held, True)
    # The beat is the line a human reads, so its wording is pinned too (it lives in
    # main.py as a function for exactly this reason).
    import main as app_main
    check("the beat says the timer is being held", app_main.light_text(held),
          "lit 70+play")
    check("and says nothing when it is not", app_main.light_text(plain), "lit 12")
    gone = p.tick("game", 99 * 60.0, 1.0, playing=False)
    check("alt-tabbed (stale frames) → dark at once", (gone.dark, gone.reason),
          (True, "idle"))

    # The hold covers the idle timers and nothing else: an asleep or locked machine is
    # not being played, whatever the last frame counter said.
    a = planner(Night(on=False), Host(asleep=True)).tick("game", 99 * 60.0, 1.0,
                                                         playing=True)
    check("asleep is never held", (a.dark, a.reason), (True, "asleep"))
    check("and a dark panel does not claim a hold", a.idle_held, False)
    check("the beat calls it dark", app_main.light_text(a), "dark(asleep)")
    b = planner(Night(on=False), Host(locked=True)).tick("game", 99 * 60.0, 1.0,
                                                         playing=True)
    check("locked is never held", (b.dark, b.reason), (True, "locked"))
    c = planner(Night(on=False), Host(),
                config=cfg(display={"stay_lit_in_game": False})
                ).tick("game", 99 * 60.0, 1.0, playing=True)
    check("stay_lit_in_game: false lets the timer win", (c.dark, c.reason),
          (True, "idle"))


def case_monitor() -> None:
    print("case: Windows turns the displays off")
    a = planner(Night(on=False), Host(monitor_on=False)).tick("idle", 5.0, 1.0)
    check("dark, named monitor-off", (a.dark, a.reason), (True, "monitor-off"))
    b = planner(Night(on=False), Host(monitor_on=None)).tick("idle", 5.0, 1.0)
    check("None means unknown, not off", (b.dark, b.reason), (False, "lit"))
    c = planner(Night(on=False), Host(monitor_on=True)).tick("idle", 5.0, 1.0)
    check("an awake monitor is lit", (c.dark, c.reason), (False, "lit"))
    d = planner(Night(on=False), Host(monitor_on=False),
                config=cfg(power={"follow_display": False})).tick("idle", 5.0, 1.0)
    check("follow_display: false ignores it", (d.dark, d.reason), (False, "lit"))


def case_precedence() -> None:
    print("case: the reasons in order")
    a = planner(Night(on=False), Host(asleep=True, monitor_on=False)).tick(
        "idle", 99 * 60.0, 1.0)
    check("asleep beats monitor-off and idle", a.reason, "asleep")
    b = planner(Night(on=False), Host(asleep=False, monitor_on=False)).tick(
        "idle", 99 * 60.0, 1.0)
    check("monitor-off beats idle", b.reason, "monitor-off")
    lb = planner(Night(on=False),
                 Host(locked=True, monitor_on=False)).tick("idle", 99 * 60.0, 1.0)
    check("locked beats monitor-off", lb.reason, "locked")
    la = planner(Night(on=False),
                 Host(asleep=True, locked=True)).tick("idle", 5.0, 1.0)
    check("asleep beats locked", la.reason, "asleep")
    li = planner(Night(on=False), Host(locked=True)).tick("idle", 99 * 60.0, 1.0)
    check("locked beats the idle guess", li.reason, "locked")
    c = planner(Night(on=False), Host()).tick("idle", 99 * 60.0, 1.0,
                                              forced_dark="manual")
    check("an explicit order beats everything", c.reason, "manual")


def case_night() -> None:
    print("case: night mode caps the light and warms it")
    n = Night(on=True, temp_k=2500)
    p = planner(n, Host())
    a = p.tick("idle", 5.0, 1.0)
    # 45 × 0.5 = 22.5 and Python's round() ties to even, so 22 — asserted at the
    # exact value because "roughly half" is not what the panel will show.
    check("capped by brightness_scale", a.brightness, 22)
    check("reason says so", a.reason, "lit+night")
    check("temperature is the OS's", a.temp_k, 2500)
    check("LUT built once", (a.lut is not None, n.built), (True, 1))
    check("first warm frame is a full repaint", a.repaint, True)
    b = p.tick("idle", 6.0, 1.0)
    check("a steady night does not rebuild the LUT", (n.built, b.repaint), (1, False))
    n.temp_k = 3400
    c = p.tick("idle", 7.0, 1.0)
    check("a new temperature rebuilds once", (n.built, c.repaint, c.temp_k),
          (2, True, 3400))
    n.on = False
    d = p.tick("idle", 8.0, 1.0)
    check("night off: full brightness again", (d.reason, d.brightness, d.lut),
          ("lit", 45, None))
    # the floor protects the panel from a scale that would make it unreadable
    e = planner(Night(on=True, temp_k=2500), Host(),
                config=cfg(night={"brightness_scale": 0.05, "brightness_floor": 10})
                ).tick("idle", 5.0, 1.0)
    check("floor holds the brightness up", e.brightness, 10)
    # dark outranks warmth: nothing is computed for a panel that is off
    f = planner(Night(on=True, temp_k=2500), Host(asleep=True)).tick("idle", 5.0, 1.0)
    check("dark needs no LUT", (f.lut, f.dark), (None, True))


def case_night_unknown() -> None:
    print("case: unknown night is not night off, and unknown warmth still falls back")
    # Startup with no prior state: the daytime look - the panel must not act
    # on a state it has never read. (Transient loss of an *established* state
    # is NightLight's held appearance, pinned in tools/nightlight_probe.py;
    # the planner never even sees that as a change.)
    u = planner(Night(on=None), Host()).tick("idle", 5.0, 1.0)
    check("unknown at startup looks day", (u.reason, u.brightness, u.lut),
          ("lit", 45, None))
    # `color_temp_k: 0` is the shipped default, meaning "whatever Windows
    # says". When Windows' warmth cannot be read either, the planner needs a
    # number anyway - and the failure shape from the issue is dimming the
    # panel with no LUT at all, so the fallback is a documented warmth.
    n = Night(on=True, temp_k=None)
    p = planner(n, Host(), config=cfg(night={"color_temp_k": 0}))
    a = p.tick("idle", 5.0, 1.0)
    check("falls back to a safe temperature", a.temp_k, 2700)
    check("still capped for night", a.reason, "lit+night")


def case_pixels_warm() -> None:
    print("case: the warmth actually reaches the pixels")
    from PIL import ImageStat

    from app import display as _d          # noqa: F401  (sets up the vendor path)
    from app.layout import Layout
    from app.nightlight import warm_lut
    from app.sensors import make_hub

    c = cfgmod.load(None)
    hub = make_hub(c, force="demo")        # real backend, no hardware, no admin
    hub.tick()
    time.sleep(0.3)
    snap = hub.tick()
    lay = Layout(c, rate_hz=1.0)

    lay.set_warm(None)
    plain = lay.render(snap, "idle")
    lut = warm_lut(2500, 1.0)
    check("a 2500 K LUT exists", lut is not None, True)
    check("it is a 768-entry per-channel table", len(lut), 768)
    lay.set_warm(lut)
    warm = lay.render(snap, "idle")

    p, w = ImageStat.Stat(plain).mean, ImageStat.Stat(warm).mean
    print(f"  means  plain={tuple(round(x, 1) for x in p)}  warm={tuple(round(x, 1) for x in w)}")
    check("blue is pushed down", w[2] < p[2] * 0.9, True)
    check("red is not", w[0] >= p[0] * 0.9, True)
    check("the frame gets warmer overall", w[0] - w[2] > p[0] - p[2], True)
    # `set_warm`'s docstring promises near-black stays near-black because a LUT maps 0
    # to 0. Tested where the promise is made (the table itself) and where it matters
    # (the drawn background): the layout's background is a dark grey, not pure black,
    # and a warmer table lifts a grey slightly — that is colour science, not a glow.
    check("the LUT maps 0 to 0 on every channel", (lut[0], lut[256], lut[512]),
          (0, 0, 0))
    pmin = min(x[0] for x in plain.getextrema())
    wmin = min(x[0] for x in warm.getextrema())
    print(f"  darkest pixel  plain={pmin}  warm={wmin}")
    check("the background stays near-black", wmin < 24, True)
    check("warming barely moves it", wmin - pmin <= 6, True)
    check("the wipe frame stays dark", max(max(x) for x in lay.blank().getextrema()) < 40,
          True)


def main() -> int:
    for fn in (case_lit, case_dim_and_off, case_sleep, case_locked, case_playing,
               case_monitor, case_precedence, case_night, case_night_unknown,
               case_pixels_warm):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
