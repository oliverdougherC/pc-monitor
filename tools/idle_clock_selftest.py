"""Replay 25 and 50 days of uptime at the idle clock - no 25-day wait.

    .venv\\Scripts\\python tools\\idle_clock_selftest.py

`idle_seconds` is a subtraction between two reads of the boot clock that are not the
same width: `LASTINPUTINFO.dwTime`, a 32-bit DWORD, and the boot clock itself. ctypes
returns an unannotated call as a signed C int, so a DWORD tick count reads negative
from 24.9 days of uptime on and stays negative for the next 24.8 - half of every
boot - and the last-input tick wraps on its own at 49.7 days. Subtracting the two as
they come, and clamping the result at zero, turns both boundaries into "somebody
just typed": the answer the panel acts on by *not* going dark, and the answer a
pending screen-off reads as proof of a wake.

The two reads are scripted at the call itself, so the cases land on the exact ticks
instead of on whatever uptime whoever runs the gate happens to have, and the same
cases run against any implementation of `idle_seconds`. Expected values are written
out in seconds from the tick pairs above them, not computed from the code.
"""
import ctypes
import sys

sys.path.insert(0, ".")
from app import hoststate as hs          # noqa: E402
from app.hoststate import HostState      # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []
DWORD = 0xFFFFFFFF


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


class FakeBootClock:
    """`GetLastInputInfo` and the tick count, scripted to whatever uptime a case wants.

    Scripted at the ctypes call rather than at a helper, because the shape of the two
    reads is half of what is under test: `GetTickCount` is replaced with what ctypes'
    default return type makes of a DWORD (negative past 0x7fffffff), which is the
    mistake, and `GetTickCount64` with the honest 64-bit value.
    """

    def __init__(self) -> None:
        self.tick: int | None = 0        # LASTINPUTINFO.dwTime; None = the read failed
        self.now: int = 0                # the boot clock, 64-bit
        self._patch: tuple = ()

    def at(self, tick: int | None, now: int) -> None:
        self.tick, self.now = tick, now

    def install(self) -> None:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        # Reading the names first is what caches the real exported functions, so
        # `remove()` can hand them back to anything else in the process.
        self._patch = (u, "GetLastInputInfo", getattr(u, "GetLastInputInfo", None)), \
                      (k, "GetTickCount", getattr(k, "GetTickCount", None)), \
                      (k, "GetTickCount64", getattr(k, "GetTickCount64", None))
        u.GetLastInputInfo = self._last_input
        k.GetTickCount = lambda: as_int32(self.now)
        k.GetTickCount64 = lambda: self.now

    def remove(self) -> None:
        for owner, name, real in self._patch:
            try:
                del owner.__dict__[name]          # back to the real exported function
            except KeyError:
                pass
            if real is not None:
                setattr(owner, name, real)

    def _last_input(self, p_info) -> int:
        """Fill the structure the caller passed, the way user32 would."""
        if self.tick is None:
            return 0                  # the call failed; nothing was written
        info = _LastInputInfoOut(p_info)
        info.dwTime = self.tick
        return 1


class _Info(ctypes.Structure):
    """The same 8 bytes `LASTINPUTINFO` is, seen from the test side."""
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


def _LastInputInfoOut(p_info) -> _Info:
    return ctypes.cast(p_info, ctypes.POINTER(_Info)).contents


def as_int32(v: int) -> int:
    """What ctypes' default restype makes of a DWORD: the same bits, signed."""
    return v - (1 << 32) if v > 0x7FFFFFFF else v


CLOCK = FakeBootClock()


def idle() -> float | None:
    """`idle_seconds()`, to milliseconds: these answers are exact tick differences."""
    seen = hs.idle_seconds()
    return None if seen is None else round(seen, 3)


def case_signed_boundary() -> None:
    print("case: the signed boundary, 24.9 days after boot")
    # The desk does not change as the tick count crosses 0x7fffffff; only the width
    # it is read at does. A clamped subtraction reads that as a fresh keystroke.
    CLOCK.at(0x7ffff000, 0x7ffffff0)
    check("just before it, seconds ago", idle(), 4.08)           # 0xff0 ms
    CLOCK.at(0x7ffff000, 0x80000000)
    check("one tick past it, same input", idle(), 4.096)         # 0x1000 ms
    CLOCK.at(0x7ffff000, 0x80000fa0)
    check("eight seconds past it", idle(), 8.096)
    CLOCK.at(0x7ffff000, 0x80000000 + 3_600_000)
    check("an hour later, nobody home", idle(), 3604.096)
    check("which is what the screen-off rule needs",
          hs.idle_seconds() > 45 * 60.0, True)
    CLOCK.at(0x80000000, 0x80000fa0)
    check("input on the far side of it reads as just now", idle(), 4.0)


def case_dword_wrap() -> None:
    print("case: the 32-bit wrap, 49.7 days after boot")
    # One input instant, read either side of the wrap: the age has to keep being the
    # difference between the two instead of jumping by 2^32.
    CLOCK.at(0xffffe000, 0xfffffff0)
    check("just before it, seconds ago", idle(), 8.176)          # 0x1ff0 ms
    CLOCK.at(0xffffe000, 0x100001000)
    check("just past it, same input", idle(), 12.288)            # 0x3000 ms
    CLOCK.at(0xffffe000, 0x100000000 + 3_600_000)
    check("an hour past it, nobody home", idle(), 3608.192)
    CLOCK.at(0x00000000, 0x100000000 + 3_600_000)
    check("a tick that wrapped with the clock", idle(), 3600.0)


def case_long_uptime() -> None:
    print("case: two months of uptime, which window the tick came from")
    day = 86_400_000
    now = 60 * day                          # past both boundaries
    CLOCK.at((now - 10_000) & DWORD, now)
    check("input ten seconds ago", idle(), 10.0)
    CLOCK.at((now - 3 * 3_600_000) & DWORD, now)
    check("input three hours ago", idle(), 10800.0)
    check("which is what the idle timer needs", hs.idle_seconds() > 45 * 60.0, True)
    CLOCK.at((now - day) & DWORD, now)
    check("input a day ago", idle(), 86400.0)


def case_unknown_is_not_recent() -> None:
    print("case: an unreadable clock is not a desk that was just typed on")
    CLOCK.at(None, 0x80000000)
    check("no last-input read", idle(), None)
    CLOCK.at(0x7ffff000, 0x7ffff000 - 1)     # the input tick ahead of the clock
    check("two reads that disagree", idle(), None)
    h = HostState(gap_s=1.0, poll_s=3600.0, events=False)
    h.tick(1.0)
    check("the state knows it does not know", h.idle_known, False)
    check("and does not claim a recent input", h.idle_s, 0.0)
    check("summary says so", "idle=-" in h.summary(), True)
    CLOCK.at(0x7ffff000, 0x80000fa0)
    h.tick(1.0)
    check("a read that comes back is believed again",
          (h.idle_known, round(h.idle_s, 3)), (True, 8.096))
    h.close()


def case_reads_are_unsigned() -> None:
    print("case: the two reads, on this machine, right now")
    # The arithmetic above is scripted; this is the part that is only true if the
    # calls are declared. A DWORD read as a signed int is negative for half of every
    # boot, and an idle clock that cannot be read apart from the subtraction built on
    # it is an idle clock no test can put at 25 days of uptime.
    uptime, tick = getattr(hs, "uptime_ms", None), getattr(hs, "last_input_tick", None)
    if uptime is None or tick is None:
        check("the idle clock exposes its two reads to be checked", False, True)
        return
    now = uptime()
    check("the boot clock is a plain non-negative count",
          isinstance(now, int) and now >= 0, True)
    seen = tick()
    check("the last-input tick fits in a DWORD",
          seen is None or 0 <= seen <= DWORD, True)
    age = hs.idle_seconds()
    check("and the desk reads as a real age", age is None or age >= 0.0, True)
    print(f"    this desk: uptime={now / 86_400_000:.2f} days idle={age}")


def main() -> int:
    CLOCK.install()                          # no case below reads this desk's clock
    print("boot clock: scripted\n")
    try:
        for fn in (case_signed_boundary, case_dword_wrap, case_long_uptime,
                   case_unknown_is_not_recent):
            fn()
            print()
    finally:
        CLOCK.remove()
    case_reads_are_unsigned()
    print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())