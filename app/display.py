"""Build the panel object for `display.revision`, from the vendored library.

Kept out of main.py so tools (screen_bench, liveview) can create the same device
without importing the app entry point: `import main` used to resolve to the
*vendored* main.py, because the vendor tree was pushed in front of ours on
sys.path. Vendor paths are therefore appended (never inserted) and only here —
this is the one module allowed to import `library.*`.

Revision → transport, as found on real hardware (README "When the screen arrives"):
  SIMU     simulated panel, web preview on :5678, no hardware needed
  TUR_USB  newer TURZX models: raw USB (pyusb + libusb), VID 1CBE, no COM port
  A/B/C/D  classic Turing/XuanFang models: a CDC-ACM COM port at 115200 8N1.
           Revision C — the 2.1"/2.8"/5"/8" family — enumerates *two* serial
           faces through a hub built into the screen: 1A86:CA21 ("UsbMonitor",
           serial "CT21INCH") which only wakes the panel, and 1D6B:0106
           (serial "20080411") which is the live link. com_port: AUTO does the
           wake-then-find-the-awake-port dance for us; a fixed COM4 breaks it,
           because the awake port disappears while the panel resets.
"""
from __future__ import annotations

import importlib
import platform
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
VENDOR = _ROOT / "vendor" / "turing-smart-screen-python"

# Panel bring-up deadlines. The vendored driver has none of its own: Reset() writes
# a RESTART command with pyserial's write_timeout unset, so on a screen whose CDC
# endpoint stopped draining that write blocks in WriteFile forever, and
# InitializeComm() retries its HELLO handshake once a second without end. Both look
# identical from outside — dark panel, log that stops mid-sentence, nothing to act
# on — and both happened on this desk, recoverable only by a power cycle.
_HELLO_WAIT_S = 8.0
_RESET_WAIT_S = 30.0        # Reset() reboots the panel and waits up to 15 s + 15 s
_INIT_TRIES = 3             # a replug needs ~15 s to re-enumerate: give it a window
_INIT_RETRY_WAIT_S = 10.0
_WRITE_TIMEOUT_S = 2.0      # makes a deaf endpoint raise instead of hang

# Serial revisions speak over a COM port; the rest are USB-native or simulated.
SERIAL_REVISIONS = {"A", "B", "C", "D", "WEACT_A", "WEACT_B"}

_CLS = {
    "A": ("library.lcd.lcd_comm_rev_a", "LcdCommRevA"),
    "B": ("library.lcd.lcd_comm_rev_b", "LcdCommRevB"),
    "C": ("library.lcd.lcd_comm_rev_c", "LcdCommRevC"),
    "D": ("library.lcd.lcd_comm_rev_d", "LcdCommRevD"),
    "TUR_USB": ("library.lcd.lcd_comm_turing_usb", "LcdCommTuringUSB"),
    "WEACT_A": ("library.lcd.lcd_comm_weact_a", "LcdCommWeActA"),
    "WEACT_B": ("library.lcd.lcd_comm_weact_b", "LcdCommWeActB"),
}


def ensure_vendor_path() -> None:
    """Put the vendored library on sys.path (last, so our names always win).

    Public because main.py's status logging writes through the vendored logger,
    which owns log.log.
    """
    p = str(VENDOR)
    if p not in sys.path:
        sys.path.append(p)  # append: our tree must always win name collisions


def _harden_log(logger) -> None:
    """Stop a status line from ever raising inside the logger.

    log.log is opened by the vendored logger with the runtime default encoding —
    cp1252 on this machine — while our own lines carry arrows and box characters
    (`sunset→sunrise`, `old→new`). A handler that cannot encode those logs a
    "Logging error" traceback into the file we read to diagnose the app. Unmappable
    characters become `?` instead; the text is still readable, and the run never
    stops over punctuation.
    """
    for h in list(getattr(logger, "handlers", []) or []):
        stream = getattr(h, "stream", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except Exception:  # noqa: BLE001 - already tolerant, this is best-effort
                pass


def app_log(msg: str) -> None:
    """Write one line to log.log via the vendored logger; never raises.

    The autostarted app runs as pythonw.exe, where sys.stdout is None and every
    print() vanishes - this is the only way to see a scheduled run's start-up
    story (panel identity, backend chosen, why frame stats are off).
    """
    try:
        ensure_vendor_path()
        from library.log import logger
        _harden_log(logger)
        logger.info(msg)
    except Exception:  # noqa: BLE001 - logging must never break the loop
        pass


def _run_bounded(fn, wait_s: float, what: str) -> bool:
    """Run a vendored call on a daemon thread and refuse to wait longer than wait_s.

    True means it finished in time. False means it is *still running* somewhere, so
    the caller must treat the device handle as dirty — see _abandon.
    """
    done = threading.Event()
    gave_up = threading.Event()      # the waiter stopped caring; the thread's own
                                     # death afterwards is expected, not a fault
    outcome: list[bool] = []

    def go() -> None:
        try:
            fn()
            outcome.append(True)
        except Exception as e:  # noqa: BLE001 — a dead link is a normal answer here
            if not gave_up.is_set():
                app_log(f"[display] {what} raised {type(e).__name__}: {e}")
            outcome.append(False)
        finally:
            done.set()

    threading.Thread(target=go, daemon=True, name="panel-init").start()
    if not done.wait(wait_s):
        gave_up.set()
        app_log(f"[display] {what} did not finish within {wait_s:.0f} s")
        return False
    return bool(outcome and outcome[0])


def _abandon(lcd) -> None:
    """Take a driver we are walking away from out of the running, then close it.

    Closing alone is not enough, and this is the part that used to bite: the vendored
    `WriteLine`/`ReadData` reopen the port *themselves* when a write fails
    (`lcd_comm.py` calls `self.openSerial()` in its fault path), and `InitializeComm`
    keeps sending HELLO once a second while the panel id looks wrong. So a thread we
    stopped waiting for does not quietly die on a closed port — a second later it
    takes the port back and is writing frames down it while our *next* driver is
    trying to open the same one. Two owners, and neither is stoppable from Python.

    Hence: `openSerial` is refused first, so the retry loop's next attempt raises
    where it used to reclaim the port; then the port is closed, which makes an
    in-flight write raise and the thread die for real. A thread already blocked
    *inside* a native write is not reachable this way — that is the wedged-firmware
    case, and the caller has to say so out loud.
    """
    if lcd is None:           # an attempt that never got a port: nothing to retire
        return
    try:
        if not getattr(lcd, "_pcmonitor_abandoned", False):
            def _refuse(*_a, **_k):    # noqa: ANN001, ANN202 - vendor-shaped stub
                raise RuntimeError("this driver was retired by PC Monitor; the port "
                                   "belongs to a newer connection")

            lcd.openSerial = _refuse
            lcd._pcmonitor_abandoned = True
    except Exception:  # noqa: BLE001 - best effort; the close below is the real step
        pass
    try:
        lcd.closeSerial()
    except Exception:  # noqa: BLE001
        pass


def _reopen(remake, what: str):
    """A *fresh* driver object, or None. Never a second lease on a retired one.

    `remake` is the caller's constructor: opening a new object is what keeps the
    abandoned thread and the live connection from sharing state (and, on revision C,
    re-runs the awake-port dance, which is exactly what a panel that has just reset
    needs — its awake COM port can move).
    """
    if remake is None:
        return None
    try:
        return remake()
    except SystemExit:                    # vendor gives up on the port after 10 tries
        app_log(f"[display] {what}: the driver gave up looking for the port")
    except Exception as e:  # noqa: BLE001
        app_log(f"[display] {what}: reopen failed ({type(e).__name__}: {e})")
    return None


def _bring_up(lcd, force_reset: bool, remake=None) -> tuple[str, object | None]:
    """Handshake first, reboot the panel only if it did not answer:
    (ok | deaf | wedged | noport, the driver to keep owning).

    Reset() used to run on every start because that is the order the vendored
    examples use. It is not needed for a screen that answers: the loop's first push
    is a full frame (DiffPusher starts with no previous frame), so whatever was on
    the panel is painted over within a second. Skipping it removes the one blocking
    write from every normal start — and about 15 s from the boot.

    `remake` is the caller's factory for a fresh driver object. It matters because a
    deadline here only stops *us* waiting: the call we gave up on is still running on
    `lcd`. Retrying on that same object would put our reset traffic and that thread's
    HELLO loop on one handle, so after every `_abandon` the next attempt is a new
    object and the old one is gone for good. Without a factory we do not reopen at
    all — a retry we cannot make safe is a retry we should not make.

    The second value is the object this attempt ended on, which is not necessarily the
    one passed in: it is the caller's to dispose of, and returning anything else would
    leave a port open behind an attempt that failed.
    """
    answered = _run_bounded(lcd.InitializeComm, _HELLO_WAIT_S, "HELLO handshake")
    if answered and not force_reset:
        return "ok", lcd
    if not answered:
        _abandon(lcd)
        time.sleep(1.0)
        lcd = _reopen(remake, "after HELLO timeout")
        if lcd is None:
            return ("noport" if remake is not None else "deaf"), None
    try:
        # Where pyserial honours this, a wedged endpoint now raises after 2 s instead
        # of hanging; where it does not, the deadline above still catches it.
        lcd.lcd_serial.write_timeout = _WRITE_TIMEOUT_S
    except Exception:  # noqa: BLE001 — optional hardening, not a requirement
        pass
    if not _run_bounded(lcd.Reset, _RESET_WAIT_S, "panel reset"):
        # A write that stalls because the panel dropped off the bus mid-reboot looks
        # exactly like a firmware wedge from here, and the reboot may well have
        # worked — so try to talk to it again before declaring anything.
        _abandon(lcd)
        time.sleep(2.0)
        lcd = _reopen(remake, "after reset timeout")
        if lcd is None:
            return "wedged", None
        if not _run_bounded(lcd.InitializeComm, _HELLO_WAIT_S, "HELLO after failed reset"):
            return "wedged", lcd
        return "ok", lcd
    if answered and force_reset:
        return "ok", lcd   # reset was the point; the panel already identified itself
    if _run_bounded(lcd.InitializeComm, _HELLO_WAIT_S, "HELLO after reset"):
        return "ok", lcd
    return "deaf", lcd


# The vendor's openSerial gives up by killing the process:
# `try: sys.exit(0) except: os._exit(0)` — the bare `except` catches the SystemExit
# itself, so the exit is always `os._exit(0)`: uncatchable, from whatever thread
# happened to be inside the fault path. The vendor's `WriteLine`/`ReadData` call
# `openSerial` on a *live* driver the moment a write fails, and `_abandon` only
# disarms drivers the app has already retired — so between "the write failed" and
# "the link marked itself down", the fault path can take the whole app down with a
# dying panel. The trigger measured on this desk is the commonest event on it: a
# monitor that sleeps and is woken quickly. The display-off command puts the panel
# to USB sleep (its awake port disappears), the wake pushes a whole frame into the
# port that just stopped existing, and the exit lands inside the vendor's retry
# loop — the "it never came back" the panel gets blamed for.
_SAFE_OPEN_ATTEMPTS = 3
_SAFE_OPEN_RETRY_S = 1.0
_vendor_hardened = False


class _ShortWrite(NotImplementedError):
    """A command frame that left the host in part — the one failure a resend cannot fix.

    It has to be an exception at all (the vendor's `serial_write` returns None and
    throws away the count pyserial hands back), and it has to be recognisable as
    *this* failure, because the vendor's reconnect-and-resend-once exists for a
    different one. That retry replays the buffer **from byte zero**, which is correct
    only when nothing was delivered; after a partial write the same bytes are still
    sitting in the endpoint's buffer, so replaying prepends a second copy of the
    prefix. Measured against a fake endpoint that accepts 3 bytes of `b'abcdefgh'` and
    all 8 on the retry, the wire sees `b'abcabcdefgh'` — and every caller above, from
    `PanelLink._verdict` to `DiffPusher`, saw a method that returned normally.

    `NotImplementedError` is the middle of the ladder these writes are raised on:

      * it is a `RuntimeError` — an ordinary app failure — so `PanelLink._verdict`
        (which special-cases `BaseException` and catches `SystemExit` separately) treats
        it exactly like a raised `SerialException`, and `DiffPusher` discards the shadow
        frame: the partial delivery cannot be acknowledged;
      * it is deliberately **not** a `serial.SerialException`, which is the type the
        vendor's `WriteLine` catches in order to reconnect and resend. That arm of the
        vendored library is `except serial.SerialException` and nothing wider, so this
        type is what keeps a partial frame out of it even in a process where something
        else has replaced the write path — and it is why the replacement below has to
        handle this case *above* the vendor-shaped `SerialException` arm;
      * the stack trace is there to be read, because by the time it surfaces it has
        crossed a vendored class and a link class and the text is the only thing that
        still says which of the two write paths produced it.

    There is no protocol-level resynchronisation available here to make the retry safe
    instead: the pinned revision-C driver sends an image as separate SETUP / HEADER /
    PAYLOAD / STATUS commands (`_send_command` pads each one to its own 250-byte
    boundary), and reopening a host COM handle is neither a rollback of the prefix the
    endpoint already took nor a replay of the transaction. So this fails, loudly.
    """


def harden_vendor() -> None:
    """Wrap `LcdComm.openSerial` and the write path so a failed push cannot look like one.

    Idempotent; call it from every path that builds a vendor driver. The replacements
    keep the vendor's contract — open the port, or give up — and change only the things
    the app must own:

      * giving up means a catchable `RuntimeError`, not `os._exit(0)`: the retry
        policy belongs to the app's rebuild loop, which is the only one that has
        the wake flow (an awake-port search that pokes the panel's sleeping face);
      * every attempt re-checks the retirement flag, so a driver the link has
        already retired cannot take the port back when it re-enumerates mid-loop —
        the vendor's fault path calls `openSerial` from inside a failed write,
        which is exactly the moment the app is rebuilding.

    **A write that did not happen must not return normally** (#9). The pinned vendor's
    `WriteLine` catches `serial.SerialTimeoutException` and returns — the frame is not
    on the panel, but every caller above sees a method that returned, so `PanelLink`
    acks it and `DiffPusher` commits the shadow frame. The panel is then believed to be
    showing a picture it never received, and the diff transport stops sending the bands
    that are actually missing: the next tick compares against a frame the screen does
    not have. The same is true of a **short write**, which the vendor discards entirely
    (`serial_write` throws away the byte count `pyserial` returns).

    So `serial_write` is replaced with one that requires the whole buffer to leave, and
    `WriteLine` with one that lets a failure propagate instead of swallowing it. The
    vendor's own reconnect-and-retry-once stays — but **only for a failure where nothing
    was delivered**, which is the one case replaying the buffer from byte zero is correct
    in. A *short* write is the opposite case and it is why `_ShortWrite` exists: some of
    the frame is in the endpoint's buffer, so the reconnect-and-resend path would
    prepend a second copy of the prefix and corrupt the stream while reporting success.
    A short write therefore propagates from the first attempt: the exception reaches
    `PanelLink._verdict`, which reports the push as not acknowledged, which is what keeps
    the diff cache honest. Nothing else in this module — the rebuild loop, the full-frame
    recovery, `DiffPusher._discard` — has to change to recover from it.

    This touches nothing on disk. `vendor/` is a pinned, hash-verified tree and stays
    byte-identical: the patch is applied to the class in the running process, exactly as
    the `openSerial` replacement already was.

    The class, not the instance: the constructor itself calls `openSerial()`, so an
    instance-level patch would leave that call armed. Where the self-tests install a
    fake vendor in sys.modules there is no `LcdComm` to wrap — nothing to harden.
    """
    global _vendor_hardened
    if _vendor_hardened:
        return
    ensure_vendor_path()
    try:
        from library.lcd import lcd_comm
    except ImportError:
        _vendor_hardened = True
        return
    base = getattr(lcd_comm, "LcdComm", None)
    if base is None or getattr(base, "_pcmonitor_hardened", False):
        _vendor_hardened = True
        return

    import serial

    def serial_write(self, data: bytes):
        """Write the whole buffer, or raise. Never write part of a command frame.

        A CDC endpoint that has stopped draining accepts some bytes and then blocks or
        times out. pyserial reports how many it managed; the vendor discarded that
        number, so a half-delivered frame counted as delivered. Half a frame is worse
        than none: the panel's parser is left mid-command and the next write lands in
        the wrong place.

        `_ShortWrite` rather than a plain `SerialException`, so the replacement
        `WriteLine` can tell this failure from a port that delivered nothing at all
        (see the class docstring: only the second one may be resent).
        """
        if self.lcd_serial is None:
            raise serial.SerialException(
                "PC Monitor: the port is closed, so this command was not sent")
        sent = self.lcd_serial.write(data)
        if sent is not None and sent != len(data):
            raise _ShortWrite(
                f"PC Monitor: short write, {sent} of {len(data)} bytes left the host")

    def write_line(self, line: bytes):
        """The vendor's write path, with its silence about failure removed.

        The one retry the vendor performs (close, reopen, send again) is kept for the
        failure it can actually repair: a `SerialException` raised before any byte was
        delivered — a port that vanished, a handle that was closed, a device that
        refused the write outright. Nothing on the wire, so replaying the buffer is
        exactly a second first attempt, and if it succeeds the frame *did* arrive.

        A `_ShortWrite` is deliberately not in that group, and this is the whole point
        of the fix: the bytes the endpoint already accepted are still there, and the
        vendor's replay writes the buffer from byte zero, so the panel would receive
        `accepted-prefix + whole buffer` and `WriteLine` would return normally — a
        corrupted stream that every caller above reads as a completed push, after which
        `PanelLink` acks it and `DiffPusher` commits a shadow frame the panel does not
        hold. There is no way to repair it down this path: reopening the host handle
        neither rolls back the accepted prefix nor replays the image transaction (the
        pinned driver sends header and payload as separate commands), so the failure is
        raised on the first attempt and the rebuild/full-frame recovery above owns it.

        What is also not kept is the `SerialTimeoutException` branch returning normally:
        a timeout means the bytes did not go out, and saying nothing is how a failed push
        became an acknowledged one (#9).
        """
        try:
            self.serial_write(line)
            if platform.system() == "Darwin":
                self.lcd_serial.flush()
        except serial.SerialTimeoutException:
            # A print is not enough. This is the failure the diff cache must hear about.
            app_log("[display] write timed out - the endpoint is not draining; the "
                    "frame was NOT sent")
            raise
        except _ShortWrite as e:
            # The port is closed for the same reason the reconnect path would have
            # closed it — the packet boundary this endpoint is parsing is already lost
            # — but the frame is *not* re-sent: it would be prepended to the bytes that
            # did leave. The link is marked down by the caller; its rebuild opens a
            # fresh handle, and the diff cache's invalidation makes the next frame
            # whole, which is the resynchronisation this case does not get here.
            app_log(f"[display] {e} - a partial frame cannot be resent from byte zero; "
                    f"the frame was NOT sent and the port is closed")
            try:
                self.closeSerial()
            except Exception:  # noqa: BLE001 - closing is best effort, the raise is not
                pass
            raise
        except serial.SerialException as e:  # noqa: BLE001 - reconnect once, as before
            app_log(f"[display] serial write failed ({e}); closing and reopening the "
                    f"port, then sending this frame once more")
            self.closeSerial()
            time.sleep(1)
            self.openSerial()
            self.serial_write(line)

    def open_serial(self):
        for attempt in range(1, _SAFE_OPEN_ATTEMPTS + 1):
            if getattr(self, "_pcmonitor_abandoned", False):
                raise RuntimeError("this driver was retired by PC Monitor; the port "
                                   "belongs to a newer connection")
            com_port = self.com_port
            if com_port == "AUTO":
                com_port = self.auto_detect_com_port()
                if not com_port:
                    app_log(f"[display] Cannot find COM port automatically, retrying "
                            f"({attempt}/{_SAFE_OPEN_ATTEMPTS})")
                    time.sleep(_SAFE_OPEN_RETRY_S)
                    continue
                app_log(f"[display] Auto detected COM port: {com_port}")
            else:
                app_log(f"[display] Static COM port: {com_port}")
            try:
                self.lcd_serial = serial.Serial(com_port, 115200, timeout=1,
                                                rtscts=True)
                # The retirement is re-checked *after* acquisition, not only before it
                # (#6). `serial.Serial(...)` can take a long time against a device node
                # that is still enumerating, and the link may have retired this driver
                # while it was inside the constructor — at which point assigning the
                # handle installs a live port on a driver nothing owns any more, and the
                # next rebuild opens a second one. One owner is a property of the open,
                # not of the attempt that asked for it.
                if getattr(self, "_pcmonitor_abandoned", False):
                    try:
                        self.lcd_serial.close()
                    except Exception:  # noqa: BLE001 - closing a late handle must not raise
                        pass
                    self.lcd_serial = None
                    raise RuntimeError("this driver was retired while opening the port; "
                                       "the handle was closed instead of installed")
                return
            except Exception as e:  # noqa: BLE001
                app_log(f"[display] Cannot open COM port {com_port}: {e} - retrying "
                        f"({attempt}/{_SAFE_OPEN_ATTEMPTS})")
                time.sleep(_SAFE_OPEN_RETRY_S)
        raise RuntimeError(f"openSerial: no usable panel port after "
                           f"{_SAFE_OPEN_ATTEMPTS} attempts; the app's rebuild "
                           "owns the retry")

    open_serial._pcmonitor = True
    base.openSerial = open_serial
    base.serial_write = serial_write
    base.WriteLine = write_line
    base._pcmonitor_hardened = True
    _vendor_hardened = True
    app_log("[display] vendor write path hardened: openSerial is bounded, "
            "raise-not-exit and retirement-checked after acquisition; serial_write "
            "requires the whole buffer; WriteLine propagates a failed write so a push "
            "is never acknowledged on a frame that did not land, and a short write is "
            "never repaired by a resend from byte zero")


def make_lcd(cfg: dict):
    """Create the panel, bring it up, and set the configured orientation.

    HELLO, then — only if it did not answer — the reboot, retried over a window wide
    enough for a USB replug. If the screen still will not talk, this says what to do
    in log.log and exits with a distinct code rather than hanging in silence: the
    alternative is a dark panel and a log that stops at "Display reset", which is
    how a firmware wedge got mistaken for a broken app twice on this desk.
    """
    rev = str(cfg["display"]["revision"]).upper()
    w, h = int(cfg["display"]["portrait_width"]), int(cfg["display"]["portrait_height"])
    port = cfg["display"]["com_port"]
    force_reset = bool(cfg["display"].get("reset_on_start", False))
    ensure_vendor_path()
    harden_vendor()

    from library.lcd.lcd_comm import Orientation

    if rev == "SIMU":
        from library.lcd.lcd_simulated import LcdSimulated
        lcd = LcdSimulated(display_width=w, display_height=h)
        lcd.Reset()
        lcd.InitializeComm()
    else:
        if rev not in _CLS:
            raise SystemExit(f"unknown display revision: {rev}")

        def make():
            """A brand-new driver for this revision, port included.

            Every attempt — here and inside `_bring_up` — gets its own object, because
            an attempt we stopped waiting for still owns the one it was running on.
            """
            m = importlib.import_module(_CLS[rev][0])
            return getattr(m, _CLS[rev][1])(com_port=port, display_width=w,
                                            display_height=h)

        lcd = make()
        for attempt in range(1, _INIT_TRIES + 1):
            outcome, lcd = _bring_up(lcd, force_reset, remake=make)
            if outcome == "ok":
                break
            if outcome == "wedged":
                app_log("[display] screen stopped answering in the middle of its reset - its "
                        "firmware is wedged, not the app. Unplug its USB for 5 seconds and, "
                        "once it enumerates again: Start-ScheduledTask -TaskName PCMonitor")
                _abandon(lcd)
                raise SystemExit(2)
            app_log(f"[display] screen did not answer ({attempt}/{_INIT_TRIES}; "
                    f"{outcome}); waiting {_INIT_RETRY_WAIT_S:.0f} s for it to come back")
            _abandon(lcd)
            time.sleep(_INIT_RETRY_WAIT_S)
            try:
                lcd = make()
            except SystemExit:
                raise SystemExit(2)
        else:
            app_log("[display] no answer from the screen after several tries - check its cable, "
                    "then start it again: Start-ScheduledTask -TaskName PCMonitor")
            _abandon(lcd)
            raise SystemExit(2)

    if str(cfg["display"]["orientation"]).lower() == "landscape":
        lcd.SetOrientation(Orientation.LANDSCAPE)
    return lcd
