#!/usr/bin/env python
"""What is actually on USB right now — raw libusb view + Windows view.

Run:
  python tools/usb_probe.py                 # one snapshot
  python tools/usb_probe.py --watch 180     # print every appear/disappear for 180 s
  python tools/usb_probe.py --verbose       # + descriptors, interfaces, endpoints

The libusb enumeration is the authoritative test for `revision: TUR_USB`, which
finds the panel by VID/PID (no COM port, no driver of ours). Windows' PnP view is
printed next to it because a device that enumerates for Windows but not for
libusb is a driver-binding problem (WinUSB/usbccgp), and one that appears in
neither never got to the host at all (power/charge-only cable, unpowered hub).

Requires pyusb; libusb-1.0.dll is taken from the vendored tree
(vendor/turing-smart-screen-python/external/libusb-1.0) if it is not on PATH.
"""
from __future__ import annotations

import argparse
import ctypes.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_LIBUSB = ROOT / "vendor" / "turing-smart-screen-python" / "external" / "libusb-1.0"

# VID:PID -> what the vendored driver expects for it (library/lcd/lcd_comm_turing_usb.py)
TURING_VENDOR_ID = 0x1CBE
TURING_PRODUCT_IDS = {
    0x0028: "Turing 2.8 round (USB)",
    0x0046: "Turing 4.6 (320x960)",
    0x0050: "Turing 5.2 (720x1280)",
    0x0080: "Turing 8.0 (800x1280)",
    0x0088: "Turing 8.8 (480x1920)",
    0x0092: "Turing 9.2 (462x1920)",
    0x0123: "Turing 12.3 (720x1920)",
}


def load_libusb() -> None:
    """Make pyusb's libusb1 backend find the vendored DLL (Windows only)."""
    if os.name == "nt" and VENDOR_LIBUSB.is_dir():
        try:
            os.add_dll_directory(str(VENDOR_LIBUSB))
        except (AttributeError, OSError):
            os.environ["PATH"] = str(VENDOR_LIBUSB) + os.pathsep + os.environ.get("PATH", "")


def open_backend():
    import usb.backend.libusb1 as libusb1

    load_libusb()
    be = libusb1.get_backend(find_library=lambda _n: None)
    if be is not None:
        return be
    path = VENDOR_LIBUSB / "libusb-1.0.dll"
    if path.exists():
        return libusb1.get_backend(find_library=lambda _n: str(path))
    found = ctypes.util.find_library("libusb-1.0")
    return libusb1.get_backend(find_library=lambda _n: found)


def scan(backend):
    import usb.core

    return usb.core.find(find_all=True, backend=backend) or []


def dev_key(dev) -> tuple:
    return (dev.idVendor, dev.idProduct, dev.bus, dev.address)


def describe(dev, verbose: bool) -> str:
    vid, pid = dev.idVendor, dev.idProduct
    tag = ""
    if vid == TURING_VENDOR_ID:
        tag = TURING_PRODUCT_IDS.get(pid, "1CBE product the vendored map does not know")
    try:
        man = usb_str(dev, dev.iManufacturer)
    except Exception:
        man = ""
    try:
        prod = usb_str(dev, dev.iProduct)
    except Exception:
        prod = ""

    line = f"  {vid:04x}:{pid:04x}  bus {dev.bus:03d} addr {dev.address:03d}" \
           f"  cls={dev.bDeviceClass:02x} cfgs={dev.bNumConfigurations}" \
           f"  {man} {prod}".rstrip()
    if tag:
        line += f"   <== {tag}"
    if verbose:
        try:
            for cfg in dev:
                for intf in cfg:
                    eps = ", ".join(
                        f"{e.bEndpointAddress:02x}{'IN' if e.bEndpointAddress & 0x80 else 'OUT'}/"
                        f"{['ctrl','iso','bulk','intr'][e.bmAttributes & 3]}/{e.wMaxPacketSize}B"
                        for e in intf)
                    line += (f"\n      if{intf.bInterfaceNumber} cls={intf.bInterfaceClass:02x}"
                             f" sub={intf.bInterfaceSubClass:02x} proto={intf.bInterfaceProtocol:02x}"
                             f" eps=[{eps}]")
        except Exception as e:
            line += f"\n      (descriptors unreadable: {e})"
    return line


def usb_str(dev, index: int) -> str:
    if not index:
        return ""
    try:
        return (dev.get_string(index) or "").strip()
    except Exception:
        return ""


def windows_view():
    """Registry view: every USB instance Windows has ever bound, with its desc.

    Presence is not in the registry, so this only explains *what* a VID:PID was
    and shows devices that failed to enumerate (VID_0000 ones).
    """
    if os.name != "nt":
        return []
    import winreg

    rows = []
    base = r"SYSTEM\CurrentControlSet\Enum\USB"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                vidkey = winreg.EnumKey(k, i)
                if not vidkey.startswith("VID_"):
                    continue
                try:
                    with winreg.OpenKey(k, vidkey) as vk:
                        for j in range(winreg.QueryInfoKey(vk)[0]):
                            inst = winreg.EnumKey(vk, j)
                            with winreg.OpenKey(vk, inst) as ik:
                                desc = winreg.QueryValueEx(ik, "DeviceDesc")[0]
                            rows.append((vidkey.upper(), desc.split("%")[-1]))
                except OSError:
                    continue
    except OSError as e:
        print(f"[windows] registry unreadable: {e}")
    return sorted(set(rows))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=float, default=0, metavar="SECONDS",
                    help="keep polling and report devices appearing/disappearing")
    ap.add_argument("--verbose", action="store_true", help="print interfaces/endpoints")
    ap.add_argument("--windows", action="store_true", help="also dump the registry view")
    args = ap.parse_args()

    try:
        backend = open_backend()
    except Exception as e:
        print(f"libusb backend unavailable: {e}")
        print("libusb-1.0.dll was not found; expected at vendor/turing-smart-screen-python/external/libusb-1.0/")
        return 2
    if backend is None:
        print("libusb1 backend not found. Copy libusb-1.0.dll somewhere on PATH.")
        return 2

    if args.windows:
        print("Windows registry view (ever-seen instances):")
        for vidkey, desc in windows_view():
            mark = " <== TURING" if vidkey.lower().startswith("vid_1cbe") else ""
            print(f"  {vidkey:28s} {desc}{mark}")
        print()

    def snapshot():
        devs = scan(backend)
        return {dev_key(d): d for d in devs}

    seen = {}
    deadline = time.monotonic() + args.watch if args.watch else None
    while True:
        now = snapshot()
        if deadline is None or not seen:
            print(f"libusb sees {len(now)} device(s):")
            for k in sorted(now, key=lambda k: (k[0], k[1])):
                print(describe(now[k], args.verbose))
            if not now:
                print("  (none — libusb only lists devices Windows has a driver for)")
        else:
            for k, d in now.items():
                if k not in seen:
                    print(f"[+] appeared: {describe(d, args.verbose)}")
            for k in seen:
                if k not in now:
                    print(f"[-] gone:     {k[0]:04x}:{k[1]:04x} bus {k[2]:03d} addr {k[3]:03d}")
        seen = now

        if deadline is None:
            return 0
        if time.monotonic() >= deadline:
            print("watch over")
            return 0
        time.sleep(0.7)


if __name__ == "__main__":
    sys.exit(main())
