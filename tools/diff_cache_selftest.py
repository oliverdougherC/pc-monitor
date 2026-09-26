"""The diff cache commits only what the panel actually acknowledged.

    .venv\\Scripts\\python tools\\diff_cache_selftest.py

`DiffPusher` keeps a shadow framebuffer (`prev`) so an unchanged next frame can
be skipped. That is only a promise about the panel's memory if the bytes it
describes actually arrived: a push that raised, timed out, died halfway through
a multi-band update, or was overtaken by a relink must leave a full refresh
pending, or the panel is left showing a frame the diff cache believes is gone -
and the identical frame, or the missing band, is never sent again.

The cases here drive a fake transport with PanelLink's acknowledgement shape
(every push answers True or False, and the connection has a generation that
each bring-up increments), plus the vendored simulated panel end to end. The
expected frame sizes are written out literally on purpose: a test that reuses
the implementation's constants cannot catch the implementation changing them.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="diffcache-"))     # before vendor imports
sys.path.insert(0, ROOT)                            # absolute: the cwd has moved
from app.output import DiffPusher         # noqa: E402
from app.panel import PanelLink           # noqa: E402

sys.stdout.reconfigure(errors="replace")
import numpy as np                      # noqa: E402
from PIL import Image                   # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def frame(strips: list[tuple[int, int, tuple[int, int, int]]]) -> Image.Image:
    """A 100x100 frame: a uniform base with (y0, y1, colour) row strips painted."""
    a = np.full((100, 100, 3), 20, dtype=np.uint8)
    for y0, y1, colour in strips:
        a[y0:y1, :, :] = colour
    return Image.fromarray(a, "RGB")


BLANK = frame([])
# Two changed strips, 45 rows apart: far more than the merge gap, so the diff
# must send exactly two bands - the multi-band shape the mid-frame failure
# case needs.
TWO_BANDS = frame([(10, 15, (255, 0, 0)), (60, 65, (0, 0, 255))])
FULL = (100, 100, 0, 0)
BANDS = [(100, 5, 0, 10), (100, 5, 0, 60)]


class FakeTransport:
    """A link with PanelLink's acknowledgement shape: every push answers True
    or False, and `generation` counts connection bring-ups."""

    def __init__(self) -> None:
        self.generation = 1
        self.sent: list[tuple[int, int, int, int]] = []
        self.answers: list[bool] = []    # scripted per-push, exhausted = True
        self.mid_push = None             # fires inside each accepted push

    def DisplayPILImage(self, image, x: int = 0, y: int = 0, **kw) -> bool:   # noqa: N802
        self.sent.append((image.width, image.height, x, y))
        if self.mid_push is not None:
            self.mid_push()
        return self.answers.pop(0) if self.answers else True

    def full_pushes(self) -> int:
        return sum(1 for s in self.sent if s == FULL)


def case_healthy_diff() -> None:
    print("case: a healthy link still diffs (the fix must not just always send full)")
    t = FakeTransport()
    p = DiffPusher(t)
    check("first frame acknowledged", p.push(BLANK), True)
    check("sent whole", t.sent, [FULL])
    check("identical frame acknowledged", p.push(BLANK), True)
    check("identical frame skipped", t.sent, [FULL])
    check("changed frame acknowledged", p.push(TWO_BANDS), True)
    check("changed rows sent as two bands", t.sent[1:], BANDS)


def case_failed_full_frame() -> None:
    print("case: the first full frame fails - retrying the same image must send it again")
    t = FakeTransport()
    p = DiffPusher(t)
    t.answers = [False]
    check("failed push reports failure", p.push(BLANK), False)
    check("retry of the same image acknowledged", p.push(BLANK), True)
    check("retry sent a FULL frame, not nothing", t.full_pushes(), 2)


def case_failed_mid_band() -> None:
    print("case: a multi-band update dies on the second band - partial is not displayed")
    t = FakeTransport()
    p = DiffPusher(t)
    check("baseline frame acknowledged", p.push(BLANK), True)
    t.answers = [True, False]           # band 1 lands, band 2 does not
    check("partial update reports failure", p.push(TWO_BANDS), False)
    check("retry of the same image acknowledged", p.push(TWO_BANDS), True)
    # Not "the missing band": the panel holds a frame that is neither the old
    # one nor the new one, so only a whole frame can re-establish the truth.
    check("retry sent a FULL frame", t.full_pushes(), 2)


def case_relink_during_push() -> None:
    print("case: the link is rebuilt mid-push - a late completion must not commit")
    t = FakeTransport()
    p = DiffPusher(t)
    check("baseline frame acknowledged", p.push(BLANK), True)
    # The relink lands between bands: every band answered True on the *old*
    # generation, and those acknowledgements are worthless now.
    t.mid_push = lambda: setattr(t, "generation", t.generation + 1)
    check("push overtaken by relink reports failure", p.push(TWO_BANDS), False)
    t.mid_push = None
    check("retry of the same image acknowledged", p.push(TWO_BANDS), True)
    check("retry sent a FULL frame", t.full_pushes(), 2)


def case_invalidate_during_push() -> None:
    print("case: invalidate() runs while a push is in flight - the commit must not clobber it")
    t = FakeTransport()
    p = DiffPusher(t)
    check("baseline frame acknowledged", p.push(BLANK), True)
    # Exactly the background `invalidate()` (the on_relink callback) racing an
    # in-flight push: whichever order the writes land in, "invalidated" wins.
    t.mid_push = p.invalidate
    check("push racing invalidate reports failure", p.push(TWO_BANDS), False)
    check("cache left dirty", p.prev is None, True)
    t.mid_push = None
    check("retry of the same image acknowledged", p.push(TWO_BANDS), True)
    check("retry sent a FULL frame", t.full_pushes(), 2)


def case_late_old_generation_completion() -> None:
    print("case: a real thread pushes while the link relinks underneath it")
    t = FakeTransport()
    p = DiffPusher(t)
    check("baseline frame acknowledged", p.push(BLANK), True)
    t.mid_push = lambda: time.sleep(0.3)     # a slow full-frame write, ~0.8 s for real
    result: list = []
    th = threading.Thread(target=lambda: result.append(p.push(TWO_BANDS)))
    th.start()
    time.sleep(0.1)                           # the write is mid-band here
    t.generation += 1                         # the relink completes...
    p.invalidate()                            # ...and demands a full repaint
    th.join(5.0)
    check("late old-generation completion reports failure", result, [False])
    check("new generation's dirty state survives", p.prev is None, True)
    t.mid_push = None
    check("retry of the same image acknowledged", p.push(TWO_BANDS), True)
    check("retry sent a FULL frame", t.full_pushes(), 2)


class Raising:
    """A device whose every call raises - the write that reached no screen."""

    def DisplayPILImage(self, *a, **k) -> None:  # noqa: N802
        raise RuntimeError("endpoint gone")

    SetBrightness = ScreenOn = ScreenOff = closeSerial = DisplayPILImage
    SetOrientation = DisplayPILImage


class Logging:
    """Records the geometry of every image that reaches the device beneath."""

    def __init__(self, lcd, seen: list) -> None:
        self.lcd = lcd
        self.seen = seen

    def DisplayPILImage(self, image, x: int = 0, y: int = 0, **kw):   # noqa: N802
        self.seen.append((image.width, image.height, x, y))
        return self.lcd.DisplayPILImage(image, x, y)

    def __getattr__(self, name):
        return getattr(self.lcd, name)


def simu_cfg() -> dict:
    from app import config as cfgmod
    c = cfgmod.load(None)
    c["display"] = dict(c["display"])
    c["display"]["revision"] = "SIMU"
    c["display"]["orientation"] = "landscape"
    return c


def case_link_acks_and_counts() -> None:
    print("case: PanelLink acknowledges writes and counts connection generations")
    link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
    check("open()", link.open(), True)
    gen0 = getattr(link, "generation", None)
    check("bring-up counted a generation", isinstance(gen0, int) and gen0 >= 1, True)
    img = Image.new("RGB", (800, 480), (5, 6, 7))
    check("healthy write acknowledged", link.DisplayPILImage(img, 0, 0), True)
    link.lcd = Raising()
    check("failed write not acknowledged", link.DisplayPILImage(img, 0, 0), False)
    pusher = DiffPusher(link)
    img2 = Image.new("RGB", (800, 480), (9, 9, 9))
    check("push over the dead link reports failure", pusher.push(img2), False)
    check("cache left dirty", pusher.prev is None, True)
    check("relink ok", link.relink("test-relink"), True)
    gen1 = getattr(link, "generation", None)
    check("relink advanced the generation",
          isinstance(gen1, int) and isinstance(gen0, int) and gen1 > gen0, True)
    seen: list = []
    link.lcd = Logging(link.lcd, seen)
    check("push on the new link acknowledged", pusher.push(img2), True)
    check("it was a FULL frame (old link was never acknowledged)", seen, [(800, 480, 0, 0)])
    check("identical push acknowledged", pusher.push(img2), True)
    check("identical push skipped", seen, [(800, 480, 0, 0)])
    link.close()


def main() -> int:
    for fn in (case_healthy_diff, case_failed_full_frame, case_failed_mid_band,
               case_relink_during_push, case_invalidate_during_push,
               case_late_old_generation_completion, case_link_acks_and_counts):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())