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


def case_vendor_boundary_does_not_swallow_a_timeout() -> None:
    """The ack has to survive the *pinned vendor's* write path, not just a fake.

    Issue #9's residual defect lived below this module: the vendored `LcdComm.WriteLine`
    catches `serial.SerialTimeoutException` and returns normally, and `serial_write`
    throws away the byte count. So a frame that never left the host still looked like a
    method that returned, `PanelLink` acked it, and `DiffPusher` committed the shadow
    frame — after which the diff transport stops re-sending the bands that are actually
    missing. The other cases here drive scripted transports, which is why none of them
    caught it. This one drives the real `LcdComm` class with the hardening `app/display`
    installs, and a fake pyserial whose `write()` times out.
    """
    print("case: a swallowed vendor write timeout is not an acknowledgement (#9)")
    from app import display as disp

    try:
        from library.lcd.lcd_comm import LcdComm
    except ImportError as e:
        print(f"  SKIP the vendored library is not present ({e})")
        return

    import serial as real_serial

    class FakeSerial:
        """A CDC endpoint that has stopped draining: every write times out."""

        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, data):
            self.writes.append(bytes(data))
            raise real_serial.SerialTimeoutException("the endpoint is not draining")

        def close(self):
            pass

        def flush(self):
            pass

    # The real class gets the app's hardening, exactly as `app/panel.py` does at
    # bring-up. `_vendor_hardened` is reset so the patch is applied to this process's
    # copy of the class even if an earlier import already hardened it.
    disp._vendor_hardened = False
    disp.harden_vendor()
    check("the vendor write path was patched", getattr(LcdComm, "_pcmonitor_hardened", False),
          True)
    check("serial_write is no longer the vendor's", LcdComm.serial_write.__name__,
          "serial_write")
    check("and WriteLine is not either", LcdComm.WriteLine.__name__, "write_line")

    class Dev(LcdComm):
        def InitializeComm(self): pass
        def Reset(self): pass
        def Clear(self): pass
        def ScreenOff(self): pass
        def ScreenOn(self): pass
        def SetBrightness(self, level): pass
        def SetOrientation(self, orientation): pass
        def DisplayPILImage(self, *a, **k): pass

    dev = Dev(com_port="COM_TEST")
    dev.lcd_serial = FakeSerial()
    raised = None
    try:
        dev.WriteLine(b"\\x00HELLO\\x00")
    except BaseException as e:  # noqa: BLE001 - the raise *is* the finding
        raised = e
    check("a timed-out vendor write raises instead of returning",
          raised is not None, True)
    check("and it is the serial timeout, not something invented",
          type(raised).__name__, "SerialTimeoutException")

    # A short write is the same lie in a different shape: pyserial reports how much
    # left the host and the vendor dropped that number on the floor.
    class ShortSerial(FakeSerial):
        def write(self, data):
            self.writes.append(bytes(data))
            return len(data) - 1

    short_serial = ShortSerial()
    dev.lcd_serial = short_serial
    raised = None
    try:
        dev.serial_write(b"0123456789")
    except BaseException as e:  # noqa: BLE001
        raised = e
    check("a short write raises too", raised is not None, True)
    check("and names what happened", "short write" in str(raised), True)
    # ...and it is *not* the exception whose shape invites the reconnect-and-resend
    # path: writing `line` again after `len(line) - 1` bytes were accepted would put
    # two copies of the same bytes on the wire. `type()` is how `WriteLine` tells the
    # two failure arms apart, so this is the check that keeps them apart.
    check("and it is not a SerialException (that arm would resend the buffer)",
          isinstance(raised, real_serial.SerialException), False)
    check("the short buffer was written exactly once (nothing was replayed)",
          len(short_serial.writes), 1)

    # And the whole chain: a real DiffPusher over a PanelLink whose device times out
    # must not commit, and the retry must be a whole frame rather than a band.
    class TimingOutDevice:
        def __init__(self):
            self.attempts = 0

        def DisplayPILImage(self, *a, **k):
            self.attempts += 1
            raise real_serial.SerialTimeoutException("the endpoint is not draining")

        SetBrightness = ScreenOn = ScreenOff = closeSerial = DisplayPILImage
        SetOrientation = DisplayPILImage

    link = PanelLink(simu_cfg(), log=lambda m: None)
    check("open()", link.open(), True)
    device = TimingOutDevice()
    link.lcd = device
    pusher = DiffPusher(link)
    img = Image.new("RGB", (800, 480), (3, 3, 3))
    check("the push over a timing-out device fails", pusher.push(img), False)
    check("so the shadow frame was not committed", pusher.prev, None)
    check("and the device really was asked", device.attempts >= 1, True)
    link.close()


class PartialDevice:
    """A device whose first write is short and whose next one is complete.

    This is the endpoint shape the reconnect-and-resend-once path was *made* for and
    could not survive: 3 bytes of the first buffer are accepted, the rest are not, and
    the following write goes out whole. See the case below for why that makes the two
    failure arms of `WriteLine` have to stay apart.
    """

    def __init__(self) -> None:
        self.calls: list[bytes] = []      # every buffer written, in order

    def write(self, data):
        self.calls.append(bytes(data))
        return 3 if len(self.calls) == 1 else len(data)

    def close(self):
        pass

    def flush(self):
        pass


def case_vendor_boundary_does_not_resent_a_short_write() -> None:
    """A short write must fail the push, not be repaired by replaying the buffer (#9).

    The previous fix made a *short* write raise, which was right, but it raised inside
    the arm of the vendored `WriteLine` that closes the port, reopens it and writes the
    buffer **from byte zero**. Against this device the first write accepts 3 bytes of a
    250-byte padded command and the retry delivers all 250, so the wire receives
    `b'abcabcdefgh'` — the accepted prefix followed by a second copy of the whole buffer.
    `WriteLine` returned normally, so `PanelLink` acknowledged the push, so `DiffPusher`
    committed a shadow frame the panel does not hold: from then on the diff transport
    stops re-sending exactly the bands that are missing. That is the state this case
    pins, and the fix is that a *partial* delivery is never resent — only a
    `SerialException` raised with nothing on the wire buys the reconnect-and-resend-once,
    because only then is the buffer still unsent.

    Driven the way the finding requires: through `WriteLine` *and* the acknowledgement
    chain (real vendored `LcdComm` under `harden_vendor`, a real `PanelLink`, a real
    `DiffPusher`), not by calling `serial_write` directly. The assertion is about bytes,
    not about the exception, so a fix that changed the exception type without changing
    the write behaviour would still fail here.
    """
    print("case: a short write is not resent from byte zero (#9)")
    from app import display as disp

    try:
        from library.lcd.lcd_comm import LcdComm
    except ImportError as e:
        print(f"  SKIP the vendored library is not present ({e})")
        return

    # `harden_vendor` is idempotent and `_pcmonitor_hardened` is sticky per process, so
    # the flag is reset the same way the case above does it: the patch must be the one
    # this process runs, not whichever revision an earlier import installed.
    disp._vendor_hardened = False
    disp.harden_vendor()

    class Dev(LcdComm):
        def InitializeComm(self): pass
        def Reset(self): pass
        def Clear(self): pass
        def ScreenOff(self): pass
        def ScreenOn(self): pass
        def SetBrightness(self, level): pass
        def SetOrientation(self, orientation): pass
        def DisplayPILImage(self, *a, **k): pass

    class RealDriver(Dev):
        """A driver whose image call really goes out through the vendored `WriteLine`.

        That is the point of driving the class instead of a hand-written fake: the
        failure under test lives in the write path, and this call reaches it exactly the
        way the vendored revision-C full-frame push does — `WriteData` → `WriteLine` →
        `serial_write`, one padded 250-byte command per call. The payload here is a
        stand-in rather than `_generate_full_image`'s bytes (there is no panel and no ROM
        version to generate for), but the number, order and framing of the writes is the
        real shape, which is what the byte assertions below are about.
        """

        def __init__(self, serial_port):
            super().__init__(com_port="COM_TEST")
            self.lcd_serial = serial_port

        def DisplayPILImage(self, image, x: int = 0, y: int = 0,   # noqa: N802
                            image_width: int = 0, image_height: int = 0) -> None:
            self.WriteData(bytearray(b"PC-BITMAP-COMMAND".ljust(250, b" ")))
            if image.width * image.height:
                self.WriteData(bytearray(b"PC-BITMAP-PAYLOAD".ljust(250, b" ")))

    device = PartialDevice()
    dev = RealDriver(device)

    # The link the diff cache holds, with the real hardened driver installed as its
    # device — the same hand-over `PanelLink.lcd` does at bring-up. The command type is
    # `bytearray`, which is why `PartialDevice.write` reads `len(data)` and not the
    # length of some `bytes` conversion.
    link = PanelLink(simu_cfg(), log=lambda m: None)
    check("open()", link.open(), True)
    link.lcd = dev
    pusher = DiffPusher(link)
    img = Image.new("RGB", (800, 480), (7, 7, 7))
    check("the push whose first write is short reports failure", pusher.push(img), False)
    check("so the shadow frame was not committed", pusher.prev, None)
    check("the device was asked exactly once (no blind resend of the buffer)",
          len(device.calls), 1)

    # And the next push is a whole frame on a fresh handle that writes cleanly — the
    # recovery path the propagation hands the failure to. `_discard` left the cache
    # dirty, so this one is the whole 800x480 image rather than a band, and every byte
    # of it arrives exactly once.
    link.lcd = dev
    check("the retry of the same image is acknowledged", pusher.push(img), True)
    check("and each of its commands went out whole, exactly once",
          [len(c) for c in device.calls[1:]], [250, 250])
    # The push above proves the *chain* refused to commit; the same `WriteLine` is then
    # called once more, directly, so the `_ShortWrite` arm itself is pinned: one call for
    # one buffer. The old arm wrote twice (and a buffer written twice is the corruption),
    # so this is the count that distinguishes the fix from it.
    dev.lcd_serial = PartialDevice()
    direct = None
    try:
        dev.WriteLine(b"PC-ONE-COMMAND")
    except BaseException as e:  # noqa: BLE001 - the raise is the point of the case
        direct = e
    check("an unacknowledgeable short-write command raises from WriteLine",
          type(direct).__name__, "_ShortWrite")
    check("and WriteLine wrote that buffer exactly once (no resend from byte zero)",
          len(dev.lcd_serial.calls), 1)
    check("the bytes it wrote are the buffer it was given, in order",
          dev.lcd_serial.calls[0], b"PC-ONE-COMMAND")


def main() -> int:
    for fn in (case_healthy_diff, case_failed_full_frame, case_failed_mid_band,
               case_relink_during_push, case_invalidate_during_push,
               case_late_old_generation_completion, case_link_acks_and_counts,
               case_vendor_boundary_does_not_swallow_a_timeout,
               case_vendor_boundary_does_not_resent_a_short_write):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
