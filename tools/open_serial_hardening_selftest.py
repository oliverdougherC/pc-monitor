"""The vendor's openSerial must not be able to end the process.

    .venv\\Scripts\\python tools\\open_serial_hardening_selftest.py

`library.lcd.lcd_comm.openSerial` gives up with

    try:
        sys.exit(0)
    except:
        os._exit(0)

and the bare `except` catches the SystemExit itself, so the give-up is always
`os._exit(0)`: uncatchable, and it runs on whatever thread happened to be inside
the fault path. The vendor's `WriteLine`/`ReadData` call `openSerial` on a *live*
driver the moment a write fails, and `app.display._abandon` only disarms drivers
the app has already retired — so between "the write failed" and "the link marked
itself down", a missing panel can terminate the whole app. That is the monitor
sleep / fast wake incident in log.log: the display-off command put the panel to
USB sleep, the wake pushed a frame into the dead port, and the exit landed inside
the vendor's retry loop.

This suite runs the real vendored class (it needs the vendor tree, and says so
honestly through the gate's `vendor` need):

  * the baseline: the unhardened `openSerial`, against a missing port, ends in
    `os._exit` — observed by standing in for `os._exit`, never by dying for real;
  * after `app.display.harden_vendor()`: the same call raises a catchable
    `RuntimeError` after a bounded number of attempts, and `os._exit` is never
    reached — from the fault path or from the constructor;
  * a retired driver (`_pcmonitor_abandoned`) bails at once, on the first
    re-check, without another port attempt: the port belongs to the newer
    connection even when it re-enumerates mid-loop;
  * the constructor itself is covered, because it calls `openSerial()` before
    any instance-level patch could land.

Runs in a temporary directory, like the other suites that touch the vendored
logger: the real log.log is the deployment's, and a self-test does not write
into it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.chdir(tempfile.mkdtemp(prefix="openserial-"))        # before the app imports
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import display as disp                # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}"
          + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


class _ExitSentinel(BaseException):
    """The patched `os._exit` raises this instead of dying: the question the
    suite answers is *would* the process have ended, not whether it does."""


def _need_vendor():
    """The suite needs the real vendor class; the gate's `vendor` need is what
    says SKIP where there is none. Getting here without it is a gate bug, and
    it is reported as one rather than skipped silently."""
    disp.ensure_vendor_path()
    from library.lcd import lcd_comm
    from library.lcd import lcd_comm_rev_c
    if not hasattr(lcd_comm, "LcdComm") or not hasattr(lcd_comm_rev_c, "LcdCommRevC"):
        print("vendor tree incomplete: LcdComm/LcdCommRevC missing - the gate's "
              "vendor need should have skipped this suite", flush=True)
        return 2
    return 0


def _bare(cls, com_port: str):
    """An instance without the constructor: `openSerial` only needs `com_port`
    and a place to put the handle."""
    inst = cls.__new__(cls)
    inst.com_port = com_port
    inst.lcd_serial = None
    return inst


def main() -> int:
    if rc := _need_vendor():
        return rc
    from library.lcd import lcd_comm
    from library.lcd.lcd_comm_rev_c import LcdCommRevC

    import serial

    original_open = lcd_comm.LcdComm.openSerial
    exits: list = []
    serial_attempts: list = []
    _real_serial = serial.Serial
    _real_exit = os._exit
    _real_sleep = time.sleep

    def _dead_port(*_a, **_k):
        serial_attempts.append(1)
        raise FileNotFoundError("no such port (the bench's missing panel)")

    def _recording_exit(code):
        exits.append(code)
        raise _ExitSentinel()

    serial.Serial = _dead_port
    os._exit = _recording_exit
    time.sleep = lambda *_a, **_k: None       # the bench runs at test scale
    try:
        # -------------------------------------------------- the baseline hazard
        print("case: the unhardened vendor openSerial ends in os._exit", flush=True)
        inst = _bare(LcdCommRevC, "COM999")
        raised = None
        try:
            original_open(inst)
        except _ExitSentinel:
            raised = "exit"
        except BaseException as e:          # noqa: BLE001
            raised = f"other {type(e).__name__}"
        check("a missing port takes the process down before the fix",
              raised, "exit")
        check("and it is the uncatchable kind", len(exits), 1)
        check("after running the whole vendor retry loop",
              len(serial_attempts), 10)

        # --------------------------------------------------- the fix, on the path
        print("case: the hardened openSerial raises instead of exiting", flush=True)
        disp._SAFE_OPEN_ATTEMPTS = 3
        disp.harden_vendor()
        check("hardening is idempotent", disp.harden_vendor(), None)
        check("and it wrapped the class, not one instance",
              getattr(lcd_comm.LcdComm.openSerial, "_pcmonitor", False), True)

        serial_attempts.clear()
        exits.clear()
        inst = _bare(LcdCommRevC, "COM999")
        raised = None
        try:
            inst.openSerial()
        except RuntimeError as e:
            raised = str(e)
        except BaseException as e:          # noqa: BLE001
            raised = f"other {type(e).__name__}"
        check("a missing port now raises a catchable RuntimeError",
              isinstance(raised, str) and raised.startswith("openSerial:"), True)
        check("os._exit is never reached after the fix", len(exits), 0)
        check("after exactly the bounded attempts, not the vendor's ten",
              len(serial_attempts), 3)

        # ------------------------------------------------------- the constructor
        print("case: the constructor's openSerial is covered too", flush=True)
        serial_attempts.clear()
        exits.clear()
        raised = None
        try:
            LcdCommRevC(com_port="COM999", display_width=480, display_height=800)
        except RuntimeError as e:
            raised = str(e)
        except BaseException as e:          # noqa: BLE001
            raised = f"other {type(e).__name__}"
        check("a constructor on a missing port raises, not exits",
              isinstance(raised, str) and raised.startswith("openSerial:"), True)
        check("and the process is still the one running this suite",
              len(exits), 0)

        # ------------------------------------------------------------ retirement
        print("case: a retired driver bails at the first re-check", flush=True)
        serial_attempts.clear()
        inst = _bare(LcdCommRevC, "COM999")
        inst._pcmonitor_abandoned = True    # what app.display._abandon stamps
        raised = None
        try:
            inst.openSerial()
        except RuntimeError as e:
            raised = str(e)
        check("it says whose port it is",
              "retired" in str(raised or ""), True)
        check("without a single attempt at the port", len(serial_attempts), 0)

        # ------------------------------------------------------- the AUTO branch
        print("case: the AUTO branch is bounded too", flush=True)
        serial_attempts.clear()
        exits.clear()
        inst = _bare(LcdCommRevC, "AUTO")
        inst.auto_detect_com_port = lambda: None     # no awake face, no sleeping face
        raised = None
        try:
            inst.openSerial()
        except RuntimeError as e:
            raised = str(e)
        except BaseException as e:          # noqa: BLE001
            raised = f"other {type(e).__name__}"
        check("no awake face raises after the bound, not os._exit",
              isinstance(raised, str) and raised.startswith("openSerial:"), True)
        check("and os._exit is never reached there either", len(exits), 0)
    finally:
        serial.Serial = _real_serial
        os._exit = _real_exit
        time.sleep = _real_sleep

    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
