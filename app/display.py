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


def app_log(msg: str) -> None:
    """Write one line to log.log via the vendored logger; never raises.

    The autostarted app runs as pythonw.exe, where sys.stdout is None and every
    print() vanishes - this is the only way to see a scheduled run's start-up
    story (panel identity, backend chosen, why frame stats are off).
    """
    try:
        ensure_vendor_path()
        from library.log import logger
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
    """Close the port so an abandoned vendored retry loop stops writing.

    InitializeComm() keeps sending HELLO once a second while the ID looks wrong. If
    we stopped waiting for it, that thread is still writing frames down a port we
    are about to use for real traffic; closing makes its next write raise and the
    thread die. A thread blocked *inside* a write is not reachable this way — that
    is the wedged-firmware case, and the caller has to say so out loud.
    """
    try:
        lcd.closeSerial()
    except Exception:  # noqa: BLE001
        pass


def _bring_up(lcd, force_reset: bool) -> str:
    """Handshake first, reboot the panel only if it did not answer: ok | deaf | wedged.

    Reset() used to run on every start because that is the order the vendored
    examples use. It is not needed for a screen that answers: the loop's first push
    is a full frame (DiffPusher starts with no previous frame), so whatever was on
    the panel is painted over within a second. Skipping it removes the one blocking
    write from every normal start — and about 15 s from the boot.
    """
    answered = _run_bounded(lcd.InitializeComm, _HELLO_WAIT_S, "HELLO handshake")
    if answered and not force_reset:
        return "ok"
    if not answered:
        _abandon(lcd)
        time.sleep(1.0)
        lcd.openSerial()              # bounded and logged; AUTO re-detects the port
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
        try:
            lcd.openSerial()
        except SystemExit:            # vendor gives up on the port after 10 tries
            return "wedged"
        if not _run_bounded(lcd.InitializeComm, _HELLO_WAIT_S, "HELLO after failed reset"):
            return "wedged"
        return "ok"
    if answered and force_reset:
        return "ok"   # reset was the point; the panel already identified itself
    return "ok" if _run_bounded(lcd.InitializeComm, _HELLO_WAIT_S, "HELLO after reset") else "deaf"


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

    from library.lcd.lcd_comm import Orientation

    if rev == "SIMU":
        from library.lcd.lcd_simulated import LcdSimulated
        lcd = LcdSimulated(display_width=w, display_height=h)
        lcd.Reset()
        lcd.InitializeComm()
    else:
        if rev not in _CLS:
            raise SystemExit(f"unknown display revision: {rev}")
        mod = importlib.import_module(_CLS[rev][0])
        lcd = getattr(mod, _CLS[rev][1])(com_port=port, display_width=w, display_height=h)
        for attempt in range(1, _INIT_TRIES + 1):
            outcome = _bring_up(lcd, force_reset)
            if outcome == "ok":
                break
            if outcome == "wedged":
                app_log("[display] screen stopped answering in the middle of its reset - its "
                        "firmware is wedged, not the app. Unplug its USB for 5 seconds and, "
                        "once it enumerates again: Start-ScheduledTask -TaskName PCMonitor")
                raise SystemExit(2)
            app_log(f"[display] screen did not answer ({attempt}/{_INIT_TRIES}); "
                    f"waiting {_INIT_RETRY_WAIT_S:.0f} s for it to come back")
            _abandon(lcd)
            time.sleep(_INIT_RETRY_WAIT_S)
            try:
                lcd.openSerial()
            except SystemExit:
                raise SystemExit(2)
        else:
            app_log("[display] no answer from the screen after several tries - check its cable, "
                    "then start it again: Start-ScheduledTask -TaskName PCMonitor")
            raise SystemExit(2)

    if str(cfg["display"]["orientation"]).lower() == "landscape":
        lcd.SetOrientation(Orientation.LANDSCAPE)
    return lcd
