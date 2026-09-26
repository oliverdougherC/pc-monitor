"""The gamma-ramp read against declared native contracts — no monitor needed.

    .venv\\Scripts\\python tools\\gamma_ramp_selftest.py

`app/nightlight.py` used to ask **gdi32** for `EnumDisplaySettingsW`, which
User32 exports and Gdi32 does not. ctypes answered with an AttributeError, the
broad `except Exception` turned that into `None`, and the advertised gamma-ramp
second opinion had never measured a single pixel — while the log line stayed
silent, because "neutral" and "never asked" were the same return value.

So the fakes below are built from the SDK's own export lists: a module that
answers only what its DLL really exports and refuses everything else. If the
production code asks the wrong DLL, the case fails here rather than in a log
nobody reads. Where a real display is present the same claims are checked
against it, and where one is not (a service session, a CI runner) that case says
SKIP out loud instead of pretending to pass.
"""
import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.nightlight import gamma_gains        # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []

# What each DLL really exports, transcribed from the SDK. Not imported from the
# implementation - that is the mistake this file exists to catch.
USER32_EXPORTS = {"EnumDisplayDevicesW", "EnumDisplaySettingsW"}
GDI32_EXPORTS = {"CreateDCW", "GetDeviceGammaRamp", "SetDeviceGammaRamp", "DeleteDC"}

WARM_MIDS = (65535, 52000, 38000)          # a plausible f.lux-ish mid-tone
IDENTITY = tuple([i * 257 for i in range(256)] * 3)


def ramp_array(mids):
    """A 768-entry GAMMA_RAMP whose 50% tone is `mids` (what the reader samples)."""
    out = [0] * 768
    for c, v in enumerate(mids):
        for i in range(256):
            out[c * 256 + i] = int(round(v * i / 128.0)) if i <= 128 else v
    return out


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


class FakeFunc:
    def __init__(self, dll, name, impl, called):
        self.dll, self.name, self.impl = dll, name, impl
        self.called = called              # the DLL's set of *invoked* exports
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.called.add(self.name)        # declaring a function is not calling it
        return self.impl(self, args)


class FakeDll:
    """Answers only the exports that DLL actually has."""

    def __init__(self, dll_name, exports, impls):
        self.dll_name, self.exports, self.impls = dll_name, exports, impls
        self.funcs: dict[str, FakeFunc] = {}
        self.called: set[str] = set()

    def __getattr__(self, name):
        if name not in self.exports:
            raise AttributeError(f"[fake {self.dll_name}] does not export {name}")
        if name not in self.funcs:
            self.funcs[name] = FakeFunc(self.dll_name, name,
                                        self.impls.get(name, lambda f, a: 1), self.called)
        return self.funcs[name]


DISPLAY1 = "\\\\.\\DISPLAY1"
DISPLAY2 = "\\\\.\\DISPLAY2"


class FakeWindll:
    """A windll whose user32/gdi32 only know what the SDK says they know."""

    def __init__(self, devices=((DISPLAY1, 0x5),), hdc=0x7FF715900000,
                 ramp=None, create_ok=True, ramp_ok=True):
        self.devices, self.hdc = list(devices), hdc
        self.ramp = ramp_array(WARM_MIDS) if ramp is None else list(ramp)
        self.create_ok, self.ramp_ok = create_ok, ramp_ok
        self.released: list[int] = []
        self.seen_hdc: list[object] = []
        self.user32 = FakeDll("user32", USER32_EXPORTS, {
            "EnumDisplayDevicesW": self._enum,
            "EnumDisplaySettingsW": lambda f, a: 1,
        })
        self.gdi32 = FakeDll("gdi32", GDI32_EXPORTS, {
            "CreateDCW": self._create,
            "GetDeviceGammaRamp": self._ramp,
            "SetDeviceGammaRamp": lambda f, a: 1,
            "DeleteDC": self._delete,
        })

    def _enum(self, _f, args):
        _dev, num, ppdev, _flags = args
        if num >= len(self.devices):
            return 0
        dd = ppdev._obj
        name, flags = self.devices[num]
        dd.DeviceName = name
        dd.StateFlags = flags
        return 1

    def _create(self, _f, args):
        return self.hdc if self.create_ok else 0

    def _ramp(self, _f, args):
        hdc, pp = args
        self.seen_hdc.append(hdc.value)
        if not self.ramp_ok:
            return 0
        arr = pp._obj
        for i, v in enumerate(self.ramp):
            arr[i] = v
        return 1

    def _delete(self, _f, args):
        self.released.append(args[0].value)
        return 1


def case_right_dll_and_declarations() -> None:
    print("case: every call goes to the DLL that exports it")
    fake = FakeWindll()
    got = gamma_gains(_windll=fake)
    check("measured the primary device", got.device, DISPLAY1)
    check("no failure reason", got.reason, None)
    check("user32 answered the enumeration", sorted(fake.user32.called),
          ["EnumDisplayDevicesW"])
    check("gdi32 answered the device context and the ramp", sorted(fake.gdi32.called),
          ["CreateDCW", "DeleteDC", "GetDeviceGammaRamp"])
    check("nothing was asked of gdi32 that it does not export",
          "EnumDisplaySettingsW" in fake.gdi32.called, False)
    # The declarations, against the SDK requirement rather than the code's opinion.
    check("CreateDCW returns a pointer-sized HDC",
          fake.gdi32.funcs["CreateDCW"].restype is ctypes.c_void_p, True)
    check("GetDeviceGammaRamp returns BOOL",
          fake.gdi32.funcs["GetDeviceGammaRamp"].restype is ctypes.c_long, True)
    check("DeleteDC takes the HDC",
          fake.gdi32.funcs["DeleteDC"].argtypes, [ctypes.c_void_p])


def case_handle_is_not_truncated() -> None:
    print("case: a handle with the upper 32 bits set survives intact")
    hdc = 0x28C96EF55C0                      # the shape real handles arrive with
    fake = FakeWindll(hdc=hdc)
    gamma_gains(_windll=fake)
    check("GetDeviceGammaRamp saw all 64 bits", fake.seen_hdc, [hdc])
    check("DeleteDC saw all 64 bits", fake.released, [hdc])
    truncated = ctypes.c_int(hdc).value
    check("and that is not the truncated value the old default restype gave",
          fake.seen_hdc == [truncated], False)


def case_dc_is_always_released() -> None:
    print("case: the device context is released even when the ramp read fails")
    fake = FakeWindll(ramp_ok=False)
    got = gamma_gains(_windll=fake)
    check("released exactly once", fake.released, [fake.hdc])
    check("and the failure is reported, not called neutral", got.gains, None)
    check("with a reason", "GetDeviceGammaRamp" in (got.reason or ""), True)
    check("naming the device it tried", got.device, DISPLAY1)


def case_failures_are_reasons_not_neutral() -> None:
    print("case: no display is a reason, never a measurement")
    fake = FakeWindll(create_ok=False)
    got = gamma_gains(_windll=fake)
    check("no gains invented", got.gains, None)
    check("CreateDCW failure named", "CreateDCW" in (got.reason or ""), True)

    empty = FakeWindll(devices=[])
    got = gamma_gains(_windll=empty)
    check("no active device says so", got.reason, "no active display device")
    check("and no DC was opened", sorted(empty.gdi32.called), [])

    idle = FakeWindll(devices=((DISPLAY1, 0x1), (DISPLAY2, 0x0)))
    got = gamma_gains(_windll=idle)
    check("an inactive display is not chosen", got.device, DISPLAY1)

    second = FakeWindll(devices=((DISPLAY1, 0x5), (DISPLAY2, 0x1)))
    got = gamma_gains(device=DISPLAY2, _windll=second)
    check("a named active display is followed", got.device, DISPLAY2)
    got = gamma_gains(device="\\\\.\\DISPLAY9", _windll=second)
    check("a stale name is refused, not silently retargeted",
          got.gains is None and "DISPLAY9" in (got.reason or ""), True)


def case_warm_ramp_is_measured() -> None:
    print("case: a warm ramp reads as warm")
    fake = FakeWindll(ramp=ramp_array(WARM_MIDS))
    r, g, b = gamma_gains(_windll=fake).gains
    check("red is the reference channel", round(r, 3), 1.0)
    check("green is reduced", round(g, 3), round(WARM_MIDS[1] / WARM_MIDS[0], 3))
    check("blue is reduced further", round(b, 3), round(WARM_MIDS[2] / WARM_MIDS[0], 3))
    check("so the caller's warm test would fire", (r - b) >= 0.12, True)

    flat = FakeWindll(ramp=IDENTITY)
    r, _g, b = gamma_gains(_windll=flat).gains
    check("an identity ramp reads neutral", (round(r, 3), round(b, 3)), (1.0, 1.0))
    check("and would not be called warm", (r - b) >= 0.12, False)


def case_real_display() -> None:
    print("case: this desk's display, through the real declarations")
    if os.name != "nt":
        print("  SKIP not Windows (not counted as a pass)")
        return
    got = gamma_gains()
    if got.gains is None:
        print(f"  SKIP no measurable display here: {got.reason} "
              f"(service session / CI runner - not counted as a pass)")
        return
    print(f"  ok   real read from {got.device}: gains {tuple(round(v, 3) for v in got.gains)}")
    check("the device is named like a display", got.device.startswith("\\\\.\\DISPLAY"), True)
    check("gains are normalised into 0..1", all(0.0 <= v <= 1.0 for v in got.gains), True)
    check("the brightest channel is the reference", round(max(got.gains), 3), 1.0)
    check("the read reported no reason", got.reason, None)
    # What the declarations buy is asserted deterministically above (restype is
    # c_void_p, and a 64-bit handle survives a call intact). Measuring it here
    # would mean opening a second device context per run and leaking the
    # truncated one, which is exactly the kind of handle this code must not
    # hand to DeleteDC - so the real case stays a read-only smoke test.


def main() -> int:
    case_right_dll_and_declarations()
    case_handle_is_not_truncated()
    case_dc_is_always_released()
    case_failures_are_reasons_not_neutral()
    case_warm_ramp_is_measured()
    case_real_display()
    print("\nSELFTEST " + ("PASSED" if not fails else f"FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())