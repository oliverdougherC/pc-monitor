"""Is the panel deaf, and what actually wakes it? Raw serial, no vendored loop.

    .venv\\Scripts\\python tools\\screen_wake_probe.py            # COM4
    .venv\\Scripts\\python tools\\screen_wake_probe.py COM7

Written because the app logged `HELLO handshake did not finish within 8 s` twice —
including after the vendor's own RESTART — on a machine that had been idle for over
an hour with the previous build's idle timeout already having sent `ScreenOff`. Two
explanations matter for the "screen follows the monitor" feature, and they need
opposite fixes:

  * the panel MCU stops answering while its backlight is off, in which case a link
    re-establishment must send TURNON *before* it expects a handshake, or an
    off-then-on cycle loses the panel for good;
  * the CDC-ACM port is fine and something else (USB selective suspend, a wedged
    endpoint) ate the bytes, in which case retrying HELLO is the whole answer.

The commands are the revision-C frames from
`library/lcd/lcd_comm_rev_c.py::Command` — HELLO, TURNON (`…00 00 00 00`), RESTART —
sent and read directly, because the vendored `_hello()` retries forever and cannot
tell you *why* nothing came back. Nothing here writes an image or changes
brightness; the only state it can change is backlight off→on.

Stop the app first: the COM port is exclusive.

    Stop-ScheduledTask -TaskName PCMonitor
    … run this …
    Start-ScheduledTask -TaskName PCMonitor
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")
try:
    # Verdicts here are printed with arrows and box characters; a piped console is
    # cp1252 and would raise on the first one.
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass
import serial  # noqa: E402

HELLO = bytes((0x01, 0xEF, 0x69, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0xC5, 0xD3))
RESTART = bytes((0x84, 0xEF, 0x69, 0x00, 0x00, 0x00, 0x01))
TURNOFF = bytes((0x83, 0xEF, 0x69, 0x00, 0x00, 0x00, 0x01))
TURNON = bytes((0x83, 0xEF, 0x69, 0x00, 0x00, 0x00, 0x00))
BRIGHT_50 = bytes((0x7B, 0xEF, 0x69, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00)) + bytes([128])


def ask(s: serial.Serial, what: bytes, label: str, read: int = 23,
         wait_s: float = 3.0, quiet_s: float = 0.6) -> bytes:
    """One command, then whatever comes back within `wait_s` (idle out at `quiet_s`)."""
    s.reset_input_buffer()
    try:
        s.write(what)
        s.flush()
    except Exception as e:  # noqa: BLE001
        # This is the interesting one, not an error to paper over: a write to a wedged
        # CDC endpoint blocks forever, which is why the app never issues an unbounded
        # write to the panel. A blocking pyserial call is what hung the old build.
        print(f"  → {label}: WRITE FAILED {type(e).__name__}: {e}")
        print("    the endpoint is not draining — the port opens but bytes go nowhere")
        raise
    print(f"  → {label}  ({what.hex(' ')})", flush=True)
    buf = b""
    deadline = time.time() + wait_s
    s.timeout = 0.5
    while time.time() < deadline:
        chunk = s.read(read - len(buf) if read else 1)
        if chunk:
            buf += chunk
            if read and len(buf) >= read:
                break
            deadline = time.time() + quiet_s      # more may be on the way
        elif buf:
            break
    if buf:
        printable = "".join(chr(c) for c in buf if 32 <= c < 127)
        print(f"  ← {len(buf)} bytes  {buf.hex(' ')}   ascii={printable!r}")
    else:
        print("  ← (nothing)")
    return buf


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM4"
    if "--restart" in sys.argv:
        # The software recovery the app itself uses, run by hand: find the device
        # behind the port and ask Windows to restart it. Unelevated this reports the
        # refusal — which is the point of printing it, since pnputil exits 0 either
        # way and a status code cannot be trusted here.
        sys.path.insert(0, ".")
        from app import config as cfgmod
        from app.panel import PanelLink
        link = PanelLink(cfgmod.load(None), log=print)
        link.port = port
        dev = link._device_id(port)
        par = link._device_id(port, parent=True)
        print(f"{port} → interface {dev or '(nothing found)'}")
        print(f"{port} → parent      {par or '(nothing found)'}")
        if "--query" in sys.argv:
            return 0
        print(f"restart: {link._usb_restart()}  error={link.usb_restart_error or '-'}")
        return 0
    print(f"opening {port} at 115200 (rtscts like the vendored driver)", flush=True)
    try:
        # Same construction as lcd_comm_rev_c.openSerial, so a "deaf" verdict here
        # means the same thing it would mean to the app.
        s = serial.Serial(port, 115200, timeout=1, rtscts=True)
        # Bounded writes, which the vendored driver does not set: a wedged endpoint
        # must produce a SerialException here, not hang the probe like it hung the
        # old build.
        s.write_timeout = 3.0
    except Exception as e:  # noqa: BLE001
        print(f"  cannot open: {type(e).__name__}: {e}")
        print("  that is the app's `[panel] no screen` case: the port is gone or owned.")
        return 1
    print(f"  opened: dtr={s.dtr} rts={s.rts} baud={s.baudrate}\n")

    print("1. HELLO as-is")
    try:
        r = ask(s, HELLO, "HELLO")
    except Exception:  # noqa: BLE001 - the reason is already on stdout
        s.close()
        print("\ndone — port released (the write blocked: device not reachable)")
        return 2
    if r.startswith(b"chs_"):
        print("  ⇒ answers now: the link is healthy; a retry loop is sufficient\n")
    else:
        print("  ⇒ no ID: trying the backlight and then the MCU reboot\n")
        print("2. TURNON (backlight on), then HELLO")
        ask(s, TURNON, "TURNON", read=0, wait_s=0.5)
        time.sleep(1.5)
        r = ask(s, HELLO, "HELLO")
        if r.startswith(b"chs_"):
            print("  ⇒ TURNON revived it: a re-link MUST send TURNON before HELLO\n")
        else:
            print("  ⇒ still deaf after TURNON\n")
            print("3. RESTART (MCU reboot), wait, HELLO a few times")
            ask(s, RESTART, "RESTART", read=0, wait_s=0.5)
            for i in range(6):
                time.sleep(3.0)
                print(f"  attempt {i + 1}:")
                if ask(s, HELLO, "HELLO").startswith(b"chs_"):
                    print("  ⇒ came back after RESTART (took "
                          f"{(i + 1) * 3} s): retry-with-reset is the answer\n")
                    break
            else:
                print("  ⇒ never answered, even after its own reboot command was "
                      "accepted by the port.\n"
                      "    The bytes are going nowhere: this is the USB-side wedge "
                      "(selective\n"
                      "    suspend or a stalled endpoint) — the device has to be "
                      "power-cycled, and\n"
                      "    the app's job is to keep retrying until it does.\n")

    # Leave the panel in a state a human can see: backlight on, mid brightness.
    ask(s, TURNON, "TURNON (leave it on)", read=0, wait_s=0.4)
    ask(s, BRIGHT_50, "SET_BRIGHTNESS 50%", read=0, wait_s=0.4)
    s.close()
    print("\ndone — port released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
