#!/usr/bin/env python
"""The developer preview must not pass invented frame numbers off as measurements.

    .venv\\Scripts\\python tools\\liveview_selftest.py

`tools/liveview.py` renders the same two layouts the panel gets. It used to default
to `--synth-fps auto`, which meant "invent frame stats whenever the backend is
real": whenever the present stream was off, denied, quiet, or had no usable target,
`synth_frames()` filled the game pane with a sine-wave ~118 fps. The pane looked
alive, the status line described the *capture* rather than the numbers on screen,
and nothing on the image said the fps was made up — which is the opposite of what
the tool is for (issue #31).

Every case drives the real `Engine` (real snapshot → real `_fill_frames` → real
`Layout.render` → real PNG encoder) against a fake sensor hub and a fake
`FrameMonitor`, so the shapes the issue lists are exercised without admin, without
ETW, without a running game, and without going near the panel:

    frames off        no capture object exists at all (--frames-source off)
    denied capture    ok=False, with presentmon's "needs admin" error
    quiet desktop     a healthy stream with nothing presenting
    failed capture    a healthy stream whose presenter has no stats this tick
    real numbers      a presenter with measured stats — the control

For each of the first four: with the default (synthesis off) the game pane shows no
number at all and the status says so; with `--synth-fps on` the invented number is
carried by a SIMULATED marking painted into the frame — badges, not just a dimmer
hue — and the status line says the same thing the pixels say. Status and image are
asserted against *each other*, because the bug was exactly that they could disagree.

The theme fonts are swapped for PIL's built-in font (see `_use_builtin_font`) so
this case gates on a runner that has never fetched `vendor/`: it asserts what is
drawn and where, not how the theme kerns. Where the badge sits against the real
fonts is `tools/layout_check.py`'s job, and it now renders the simulated case too.
"""
from __future__ import annotations

import io
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, ".")          # our tree first: vendor has its own main.py
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - non-reconfigurable pipes are fine
        pass

import numpy as np                            # noqa: E402
from PIL import Image, ImageFont              # noqa: E402

from app import config as cfgmod              # noqa: E402
from app import layout as layout_mod          # noqa: E402
from app.snapshot import FrameStats, Snapshot  # noqa: E402

import liveview                               # noqa: E402  (tools/ is sys.path[0]: this file lives there)

fails: list[str] = []

# The pane under test, widened by the burn-in shift the engine also renders.
_BOX = layout_mod.BOT_BOX["game"]["frames"]
PANE = (_BOX[0] - 8, _BOX[1] - 8, _BOX[2] + 8, _BOX[3] + 8)


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


# ---------- the fakes: one capture shape per scenario ------------------------------

class FakeHub:
    """Stand-in for `SensorHub`: one fresh, empty Snapshot per tick, exactly as the
    real hub does it. That is the point — a measurement that did not happen has to
    be *absent* from the snapshot, not merely unseen."""

    def tick(self) -> Snapshot:
        return Snapshot(ts=time.time())


class FakePresenter:
    def __init__(self, pid: int, name: str, fps: float):
        self.pid, self.name, self.fps = pid, name, fps


class FakeMon:
    """The four attributes `_fill_frames` reads off a `FrameMonitor`, pinned to one
    real capture shape. A real monitor would need Administrator and an ETW session;
    the shapes it can be in do not."""

    def __init__(self, ok: bool = True, error: str | None = None,
                 presenters: dict | None = None, stats: FrameStats | None = None):
        self.ok, self.error = ok, error
        self._presenters = presenters or {}
        self._stats = stats

    def presenters(self, min_fps: float = 0.0) -> dict:
        return dict(self._presenters)

    def stats(self, pid: int):
        return self._stats


MEASURED = FrameStats(fps=61.2, low1_pct=52.0, low01_pct=41.0, latency_ms=16.3)
GAME_PRESENTER = {4242: FakePresenter(4242, "game.exe", 61.0)}

# (label, the capture, --frames-source) for the four shapes that have no numbers.
NO_NUMBERS = [
    ("frames off", None, "off"),
    ("denied capture", FakeMon(ok=False, error="presentmon exited: requires admin"), "auto"),
    ("quiet desktop", FakeMon(ok=True, presenters={}), "auto"),
    ("failed capture", FakeMon(ok=True, presenters=GAME_PRESENTER, stats=None), "auto"),
]
REAL = ("real numbers", FakeMon(ok=True, presenters=GAME_PRESENTER, stats=MEASURED), "auto")


def engine(cfg, mon, frames_source: str, synth: bool):
    """A liveview `Engine` wired to fakes instead of hardware.

    It is constructed as the demo backend because that path opens no COM port,
    spawns no ETW session and asks for no admin, then the demo stream is replaced
    with the fake hub and monitor. Everything after that is the real tick:
    `_snapshots` → `_fill_frames` → `Layout.render` → `diff_report`.
    """
    eng = liveview.Engine(cfg, "demo", 1.0, synth, "off")
    eng.demo = False
    eng.backend = "fallback"
    eng.hub_idle = eng.hub_game = FakeHub()
    eng.frames_mon = mon
    eng.frames_info = {"source": frames_source}
    eng._maybe_reload = lambda: None      # the test owns the clock; no file watching
    return eng


# ---------- what the renderer actually drew ----------------------------------------

class _FrozenClock:
    """The wall clock as the layout sees it (it prints `%H:%M` in the power strip).

    Without this, two renders that happen to straddle a minute differ in the strip,
    and "nothing outside the frames pane moved" would fail for a reason that has
    nothing to do with the marking being tested."""

    @staticmethod
    def strftime(fmt: str) -> str:
        return "12:00"


def _use_builtin_font() -> None:
    """Replace the theme fonts with PIL's built-in one.

    The vendored `res/fonts` folder is a pin, not a checkout, so a case that needed
    it would SKIP on CI and gate nothing. Font choice does not change *whether* a
    marking is drawn, only its metrics — and the metrics are layout_check's job."""
    layout_mod.Layout._font = lambda self, path, size: ImageFont.load_default()


def render_runs(eng) -> list[tuple[str, tuple]]:
    """`step()` the engine and record every text run it drew, with its box — the
    same spy `tools/layout_check.py` uses, because the claim is about what reaches
    the frame, not about what some dict claims."""
    runs: list[tuple[str, tuple]] = []
    orig = layout_mod.Layout._txt

    def spy(self, d, xy, text, font, fill, anchor="la"):
        orig(self, d, xy, text, font, fill, anchor)
        try:
            box = d.textbbox(xy, text, font=self._font(font[0], font[1]), anchor=anchor)
        except Exception:  # noqa: BLE001 - a run we cannot box is not a number
            return
        runs.append((str(text), box))

    layout_mod.Layout._txt = spy
    try:
        eng.step()
    finally:
        layout_mod.Layout._txt = orig
    return runs


def in_pane(box) -> bool:
    return not (box[2] < PANE[0] or box[0] > PANE[2] or box[3] < PANE[1] or box[1] > PANE[3])


def numbers_in_pane(runs) -> list[str]:
    """Bare numeric runs inside the frames pane: an fps, a low, a frametime. When
    nothing was measured the layout draws `--` there instead, so an empty list is
    the honest picture and anything else is a number somebody invented."""
    return sorted({t for t, b in runs if in_pane(b) and t.replace(".", "", 1).isdigit()})


def badges_in_pane(runs) -> list[str]:
    return sorted({t for t, b in runs if in_pane(b) and "SIMULATED" in t.upper()})


def diff_pixels(a, b, box=None) -> bool:
    """Do these two frames differ — anywhere, or only inside `box`?

    `box` is a PIL rectangle, so its far edge is drawn and read inclusive."""
    A, B = np.asarray(a.convert("RGB")), np.asarray(b.convert("RGB"))
    if box is not None:
        x0, y0, x1, y1 = box
        A, B = A[y0:y1 + 1, x0:x1 + 1], B[y0:y1 + 1, x0:x1 + 1]
    return bool((A != B).any())


def diff_outside(a, b, box) -> bool:
    """Same, with `box` blanked on both sides: did anything *else* move?"""
    A, B = np.asarray(a.convert("RGB")).copy(), np.asarray(b.convert("RGB")).copy()
    x0, y0, x1, y1 = box
    A[y0:y1 + 1, x0:x1 + 1] = 0
    B[y0:y1 + 1, x0:x1 + 1] = 0
    return bool((A != B).any())


# ---------- cases -----------------------------------------------------------------

def case_cli_default() -> None:
    """The regression itself: what the command line decides, before any rendering."""
    print("case: the command line no longer opts into fabrication by itself")
    check("default with a real backend", cli(["--backend", "auto"])["synth"], False)
    check("default with the demo backend", cli([])["synth"], False)
    check("--synth-fps off", cli(["--backend", "auto", "--synth-fps", "off"])["synth"], False)
    check("--synth-fps on is still available",
          cli(["--backend", "auto", "--synth-fps", "on"])["synth"], True)
    out = cli(["--backend", "auto", "--synth-fps", "auto"])
    check("--synth-fps auto no longer invents", out["synth"], False)
    check("--synth-fps auto says what it now means",
          ("auto" in out["out"] and "off" in out["out"]), True)


def cli(argv) -> dict:
    """Run liveview's real `main()` with `run()` captured, so the argument decision
    is read where it is made — no server, no port, no device."""
    got: dict = {"out": ""}
    real_run, real_argv = liveview.run, sys.argv

    def capture(cfg, backend, hz, port, synth, frames_source="auto"):
        got.update(backend=backend, synth=synth, frames_source=frames_source)

    liveview.run = capture
    sys.argv = ["liveview.py", *argv]
    try:
        buf = io.StringIO()
        stdout, sys.stdout = sys.stdout, buf
        try:
            liveview.main()
        finally:
            sys.stdout = stdout
            got["out"] = buf.getvalue()
    finally:
        liveview.run, sys.argv = real_run, real_argv
    return got


def case_missing_stays_missing(cfg) -> None:
    """The four shapes with no measurement, with synthesis off — the new default."""
    print("case: no capture → no number (synthesis off, the default)")
    for label, mon, src in NO_NUMBERS:
        eng = engine(cfg, mon, src, synth=False)
        runs = render_runs(eng)
        st = eng.status()["frames"]
        check(f"{label}: nothing numeric in the game pane", numbers_in_pane(runs), [])
        check(f"{label}: nothing labelled simulated", badges_in_pane(runs), [])
        check(f"{label}: status: displayed", st.get("displayed"), "none")
        check(f"{label}: status: simulated", bool(st.get("simulated")), False)
        check(f"{label}: image and status agree",
              bool(st.get("simulated")), bool(badges_in_pane(runs)))
    eng = engine(cfg, FakeMon(ok=False, error="presentmon exited: requires admin"),
                 "auto", synth=False)
    eng.step()
    check("capture health is still reported next to it",
          "admin" in str(eng.status()["frames"].get("error")), True)


def case_real_numbers_untouched(cfg) -> None:
    """The control: a measurement must never be overwritten, marked, or lost."""
    print("case: a measured frame rate is shown as measured")
    label, mon, src = REAL
    for synth in (False, True):
        eng = engine(cfg, mon, src, synth=synth)
        runs = render_runs(eng)
        st = eng.status()["frames"]
        check(f"fps/low/low01 on screen (synth={'on' if synth else 'off'})",
              numbers_in_pane(runs), ["41", "52", "61"])
        check(f"no simulation marking (synth={'on' if synth else 'off'})",
              badges_in_pane(runs), [])
        check(f"status: displayed (synth={'on' if synth else 'off'})",
              st.get("displayed"), "real")
        check(f"status: name of the presenting process", st.get("name"), "game.exe")


def case_synthesis_is_marked(cfg) -> None:
    """`--synth-fps on` keeps the UI-demo purpose, and pays for it in honesty."""
    print("case: --synth-fps on invents, and says so on the image")
    for label, mon, src in NO_NUMBERS:
        eng = engine(cfg, mon, src, synth=True)
        runs = render_runs(eng)
        st = eng.status()["frames"]
        check(f"{label}: a number is on screen", numbers_in_pane(runs) != [], True)
        check(f"{label}: the pane carries a SIMULATED marking", badges_in_pane(runs) != [], True)
        check(f"{label}: status: displayed", st.get("displayed"), "simulated")
        check(f"{label}: image and status agree",
              bool(st.get("simulated")), bool(badges_in_pane(runs)))


def case_marking_is_in_the_pixels(cfg) -> None:
    """The marking has to be part of the rendered frame.

    The browser page is not the deliverable: people crop, screenshot and save the
    PNG (`/frame.png?…&raw=1`), and a marking that lived in the status line or in
    the DOM would not survive any of that. So the comparison is the *same invented
    numbers*, once marked and once not: whatever differs between the two panes is
    the marking, and it has to still differ after a PNG round trip."""
    print("case: the marking is painted into the frame, and survives the PNG export")
    lay = layout_mod.Layout(cfg, rate_hz=1.0)
    snap = Snapshot(ts=time.time())
    liveview.synth_frames(snap, 3.0)
    check("the fallback marks what it invents", bool(getattr(snap.frames, "simulated", False)),
          True)
    check("and it did invent a number", snap.frames.fps is not None, True)

    # Same invented numbers, once marked and once not: whatever differs is the
    # marking itself, and nothing else can be blamed for the difference.
    f = snap.frames
    plain_frames = FrameStats(fps=f.fps, low1_pct=f.low1_pct, low01_pct=f.low01_pct,
                              latency_ms=f.latency_ms, stale=f.stale, age_s=f.age_s,
                              gpu_pct=f.gpu_pct)
    clock, layout_mod.time = layout_mod.time, _FrozenClock()
    try:
        marked = lay.render(snap, "game")
        plain = lay.render(replace(snap, frames=plain_frames), "game")
    finally:
        layout_mod.time = clock
    check("the marked pane looks different", diff_pixels(marked, plain, _BOX), True)
    check("nothing outside the frames pane moved", diff_outside(marked, plain, _BOX), False)

    eng = engine(cfg, FakeMon(ok=True, presenters={}), "auto", synth=True)
    eng.step()
    eng.base[("game", 0)] = marked          # export the frame we just built
    png = Image.open(io.BytesIO(eng.frame("game", 0, 1.0, False)))
    check("the export is a full-size panel", png.size, (800, 480))
    check("the marking survived the PNG", diff_pixels(png, plain, _BOX), True)
    check("the PNG is otherwise lossless", diff_pixels(png, marked), False)


def case_ui_demo_is_labelled(cfg) -> None:
    """The demo backend invents every number by design — the frame stats included,
    so the game pane says what it is instead of letting the invented fps stand as
    measured. (The rest of the demo stream is labelled in the status line, which is
    where a wholly synthetic backend is honest about itself.)"""
    print("case: the UI demo stream is labelled for what it is")
    eng = liveview.Engine(cfg, "demo", 1.0, False, "off")
    eng._maybe_reload = lambda: None
    runs = render_runs(eng)
    st = eng.status()["frames"]
    check("demo: game pane carries a SIMULATED marking", badges_in_pane(runs) != [], True)
    check("demo: status: simulated", bool(st.get("simulated")), True)
    check("demo: image and status agree", bool(st.get("simulated")),
          bool(badges_in_pane(runs)))


def main() -> int:
    _use_builtin_font()
    cfg = cfgmod.load(None)
    case_cli_default()
    print()
    for fn in (case_missing_stays_missing, case_real_numbers_untouched,
               case_synthesis_is_marked, case_marking_is_in_the_pixels,
               case_ui_demo_is_labelled):
        fn(cfg)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())