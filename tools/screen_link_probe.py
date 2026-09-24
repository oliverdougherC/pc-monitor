#!/usr/bin/env python
"""Look at the desk screen's serial side: enumerate COM ports, say which USB
device each one came from, and dump whatever the device sends unasked.

Run:
  python tools/screen_link_probe.py                 # map ports -> USB parents
  python tools/screen_link_probe.py --listen 3      # 3 s of unsolicited bytes per port
  python tools/screen_link_probe.py --listen 3 --com COM4
  python tools/usb_probe.py --verbose               # the USB (non-serial) side

A "Turing-family" panel either speaks its binary protocol straight over USB
(TUR_USB: VID 1CBE, no COM port) or over a USB-CDC serial port (revisions A-D,
which normally enumerate as QinHeng 1A86:7523). Anything else — a CDC device
calling itself "UsbMonitor", a Linux-USB-gadget "Android" COM port behind a
built-in hub — is a different family and needs its protocol identified before
the app can drive it. This script is how you get the evidence.

Never writes to a port unless --send is given; reading is safe on any of them.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def port_parents() -> dict[str, dict]:
    """COM name -> {desc, hwid, service, instance} from the Windows registry.

    Registry only, no WMI: the friendly name is what Windows derived, the
    hardware IDs are what the device actually reported.
    """
    if os.name != "nt":
        return {}
    import winreg

    out: dict[str, dict] = {}
    base = r"SYSTEM\CurrentControlSet\Enum\USB"
    def read_props(key):
        props = {}
        for name in ("DeviceDesc", "FriendlyName", "HardwareID", "Service"):
            try:
                props[name] = winreg.QueryValueEx(key, name)[0]
            except OSError:
                props[name] = None
        return props

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                vidkey = winreg.EnumKey(k, i)
                try:
                    with winreg.OpenKey(k, vidkey) as vk:
                        for j in range(winreg.QueryInfoKey(vk)[0]):
                            inst = winreg.EnumKey(vk, j)
                            try:
                                with winreg.OpenKey(vk, inst) as ik:
                                    props = read_props(ik)
                            except OSError:
                                continue
                            fn = props.get("FriendlyName") or ""
                            if "COM" not in fn:
                                continue
                            com = fn[fn.rindex("COM"):].rstrip(")")
                            out[com] = {
                                "instance": f"{vidkey}\\{inst}",
                                "desc": (props.get("DeviceDesc") or "").split("%")[-1],
                                "hwid": ", ".join(props.get("HardwareID") or []),
                                "service": props.get("Service"),
                            }
                except OSError:
                    continue
    except OSError as e:
        print(f"[probe] registry unreadable: {e}")
    return out


def usb_strings(vid: int, pid: int) -> dict:
    """Manufacturer/product/serial strings straight off the device descriptor."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usb_probe as up

    out = {}
    try:
        be = up.open_backend()
        import usb.core

        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=be)
        if dev is None:
            return out
        for label, idx in (("manufacturer", dev.iManufacturer), ("product", dev.iProduct),
                           ("serial", dev.iSerialNumber)):
            out[label] = up.usb_str(dev, idx)
        out["config"] = f"bcdDevice={dev.bcdDevice:04x} maxPower={dev.bMaxPower_MA}mA" \
                        f" selfPowered={bool(dev.bmAttributes & 0x40)}"
    except Exception as e:
        out["error"] = str(e)
    return out


def listen(com: str, seconds: float) -> None:
    import serial

    try:
        sp = serial.Serial(com, 115200, timeout=0.2, write_timeout=0.5)
    except Exception as e:
        print(f"  {com}: could not open ({e})")
        return
    got = bytearray()
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            n = sp.in_waiting
            if n:
                got += sp.read(n)
            else:
                time.sleep(0.02)
    finally:
        sp.close()
    if got:
        printable = bytes(b if 32 <= b < 127 else 0x2E for b in got)
        print(f"  {com}: {len(got)} bytes unsolicited")
        print(f"        hex: {got[:96].hex(' ')}")
        print(f"        txt: {printable[:160].decode('ascii')}")
    else:
        print(f"  {com}: open, silent (device says nothing until asked)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=float, default=0, metavar="SECONDS",
                    help="read this many seconds of unsolicited output per port")
    ap.add_argument("--com", default=None, help="only this port (default: every COM port)")
    args = ap.parse_args()

    parents = port_parents()
    names = sorted(parents) or ["COM1"]
    if args.com:
        names = [args.com]

    print("COM ports and the USB device behind each:")
    for c in names:
        info = parents.get(c)
        if info:
            print(f"  {c:6s} {info['instance']}")
            print(f"         {info['desc']}  service={info['service']}")
            print(f"         {info['hwid']}")
        else:
            print(f"  {c:6s} (not a USB-serial device in the registry)")

    print("\nUSB descriptors for the devices behind them:")
    seen: set[tuple[int, int]] = set()
    for c in names:
        info = parents.get(c)
        if not info:
            continue
        hw = info["hwid"]
        if "VID_" not in hw:
            continue
        vid = int(hw.split("VID_")[1][:4], 16)
        pid = int(hw.split("PID_")[1][:4], 16)
        if (vid, pid) in seen:
            continue
        seen.add((vid, pid))
        s = usb_strings(vid, pid)
        print(f"  {vid:04x}:{pid:04x}  {s}")

    if args.listen:
        print(f"\n{args.listen:g}s of unsolicited traffic per port:")
        for c in names:
            listen(c, args.listen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
