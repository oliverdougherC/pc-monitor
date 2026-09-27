"""The event window's native ABI and its lifecycle, tested without a suspend.

    .venv\\Scripts\\python tools\\eventwindow_selftest.py

`tools/hoststate_selftest.py` drives the *state machine* by calling `_dispatch`
the way the WndProc does. This file is about the layer underneath it, which is
where the handles come from, and none of it can be reached that way: a suspend
cannot be scheduled, a window station cannot be conjured in a test, and the
question "did the 64-bit handle survive the call" is not visible from a Python
message at all. So the native layer is swapped out for one that hands out its own
handles, and the questions get asked directly:

  ABI        every foreign call is declared, and a handle's restype is pointer
             sized - ctypes hands an undeclared return back as a C `long`, which
             is the low half of the value on x64;
  width      a handle that arrives with its upper 32 bits set is still there when
             it is used again, in the WNDCLASSEX it registered, in the handle it
             unregisters, and in the window it destroys. The upper 32 bits are
             set on purpose: real HPOWERNOTIFY handles on this desk are heap
             pointers, so that is the shape they actually arrive in;
  once       every retained notification handle goes back exactly once, and the
             window handle is never passed where a notification handle belongs;
  readiness  `start()` says ready only once the registrations are in, because a
             caller that believes the answer acts on it;
  recovery   a pump that stops flips the published health and is put back by the
             loop's tick, with one window and one set of registrations;
  smoke      the real window, on the real machine, when this process has a window
             station to make one in.

The fake never pretends to be Windows: it records what it was called with, and
every assertion is about what our code did with the values it was handed.
"""
import contextlib
import ctypes
import sys
import threading
import time
from collections import Counter
from ctypes import wintypes

sys.path.insert(0, ".")          # our tree first: vendor has its own main.py
from app import gamewatch as gw   # noqa: E402
from app import hoststate as hs   # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def wait_until(what, timeout: float = 3.0) -> bool:
    """Poll a condition the way the loop polls its own thread: bounded, no sleep of faith."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if what():
            return True
        time.sleep(0.02)
    return bool(what())


def deref(ptr, struct):
    """The struct behind a `byref`, read the way the callee reads it.

    Reading it back out of memory is the point: it says what our code actually put
    in the struct, not what it meant to put there.
    """
    return ctypes.cast(ptr, ctypes.POINTER(struct)).contents


@contextlib.contextmanager
def fake_native(fake):
    """Put a fake where `app.hoststate.native()` lives, and put the real one back."""
    real = getattr(hs, "native", None)
    had = real is not None
    hs.native = lambda: fake
    try:
        yield fake
    finally:
        if had:
            hs.native = real
        else:
            del hs.native


class FakeNative:
    """Stand-in for `app.hoststate.Native`: handles nobody has to earn.

    Every handle it issues has a nonzero upper 32 bits, because that is the only
    shape which tells a pointer-sized declaration apart from one ctypes read back
    as a C `long`.
    """

    def __init__(self, register_delay: float = 0.0) -> None:
        self.register_delay = register_delay
        self.created: list[int] = []          # CreateWindowExW's returns
        self.destroyed: list[int] = []        # DestroyWindow's arguments
        self.classes: list[tuple] = []        # (name, hInstance, lpfnWndProc)
        self.registrations: list[tuple] = []  # (hwnd, guid bytes)
        self.handles: list[int] = []          # HPOWERNOTIFY returns, in order
        self.unregistered: list[int] = []     # UnregisterPowerSettingNotification args
        self.session: list[int] = []          # WTSRegisterSessionNotification args
        self.session_unregistered: list[int] = []
        self.fail_peeks = 0                   # PeekMessageW returns -1 like the API does
        self.block_peeks = False              # the pump stops coming back at all
        self.inside = threading.Event()       # set while a blocked peek is in progress
        self.released = threading.Event()
        self._next = 0x0000_7FF6_0000_0000

    def _handle(self) -> int:
        # Both halves move, so neither a truncation nor a zero-extension is a
        # value this fake would ever hand out by accident.
        self._next += 0x0000_0001_0000_0001
        return self._next

    # -- the pump -------------------------------------------------------------
    def PeekMessageW(self, msg, hwnd, f_min, f_max, remove) -> int:
        if self.block_peeks:
            self.inside.set()
            self.released.wait(5.0)
            return 0
        return -1 if self.fail_peeks else 0

    def TranslateMessage(self, msg) -> int:
        return 1

    def DispatchMessageW(self, msg) -> int:
        return 0

    def PostMessageW(self, hwnd, msg, wparam, lparam) -> int:
        return 1

    def DefWindowProcW(self, hwnd, msg, wparam, lparam) -> int:
        return 0

    def last_error(self) -> int:
        return 0

    # -- the window -----------------------------------------------------------
    def GetModuleHandleW(self, name) -> int:
        return self._handle()

    def RegisterClassExW(self, wc) -> int:
        c = deref(wc, hs._WndClass)
        self.classes.append((c.lpszClassName, c.hInstance, c.lpfnWndProc))
        return 0xC0FF

    def CreateWindowExW(self, exstyle, cls, title, style, x, y, w, h, parent,
                        menu, instance, param) -> int:
        hwnd = self._handle()
        self.created.append(hwnd)
        return hwnd

    def DestroyWindow(self, hwnd) -> int:
        self.destroyed.append(hwnd)
        return 1

    def UnregisterClassW(self, name, instance) -> int:
        return 1

    # -- the notifications ----------------------------------------------------
    def RegisterPowerSettingNotification(self, hwnd, guid, flags) -> int:
        if self.register_delay:
            time.sleep(self.register_delay)
        self.registrations.append((hwnd, bytes(deref(guid, ctypes.c_ubyte * 16))))
        handle = self._handle()
        self.handles.append(handle)
        return handle

    def UnregisterPowerSettingNotification(self, handle) -> int:
        self.unregistered.append(handle)
        return 1

    def register_session(self, hwnd) -> bool:
        self.session.append(hwnd)
        return True

    def unregister_session(self, hwnd) -> None:
        self.session_unregistered.append(hwnd)


# ------------------------------------------------------------------- the ABI
def case_declared_abi() -> None:
    print("case: every native call this module makes is declared")
    if not hs.NT:
        print("  SKIP not Windows: there is no user32 here to declare")
        return
    a = hs.native()
    vp = ctypes.c_void_p
    # A handle's return type has to be pointer sized, and "undeclared" is not:
    # ctypes then hands back a C `long`, i.e. the low half of the handle.
    # getattr, not subscripting - `dll[name]` mints a fresh, undeclared function
    # object, which would answer the question about ctypes' defaults instead of
    # about what this module declared.
    for who, name in ((a.kernel32, "GetModuleHandleW"), (a.user32, "CreateWindowExW"),
                      (a.user32, "RegisterPowerSettingNotification")):
        check(f"{name}.restype is declared", getattr(who, name).restype, vp)
    # PeekMessageW answers 0, nonzero, or -1. Typed as a bool, -1 arrives as True
    # and the failure branch can never be taken.
    check("PeekMessageW keeps -1", a.user32.PeekMessageW.restype, ctypes.c_long)
    # Which parameter of each call *is* a handle, by index.
    handle_params = {
        (a.user32, "DestroyWindow"): 0,
        (a.user32, "PostMessageW"): 0,
        (a.user32, "DefWindowProcW"): 0,
        (a.user32, "PeekMessageW"): 1,                       # the window filter
        (a.user32, "UnregisterPowerSettingNotification"): 0,  # the HPOWERNOTIFY
        (a.user32, "CreateWindowExW"): 10,                    # hInstance
    }
    if a.wtsapi32 is not None:
        handle_params[(a.wtsapi32, "WTSRegisterSessionNotification")] = 0
        handle_params[(a.wtsapi32, "WTSUnRegisterSessionNotification")] = 0
    for (dll, name), at in handle_params.items():
        types = getattr(dll, name).argtypes
        check(f"{name}.argtypes is declared", types is not None, True)
        if types:
            check(f"{name} declares parameter {at} as a pointer", types[at] is vp, True)
    check("RegisterClassExW takes the class struct by pointer",
          a.user32.RegisterClassExW.argtypes[0], ctypes.POINTER(hs._WndClass))
    check("RegisterPowerSettingNotification takes the GUID by pointer",
          a.user32.RegisterPowerSettingNotification.argtypes[1],
          ctypes.POINTER(ctypes.c_ubyte * 16))
    # The structures the calls are handed. A field typed as a C `int` where the
    # header has a pointer does not only truncate the value, it moves every field
    # after it, so the size says whether the layout is the one Windows will read.
    wide = ctypes.sizeof(vp) == 8
    check("WNDCLASSEXW is the size the header says",
          ctypes.sizeof(hs._WndClass), 80 if wide else 48)
    check("MSG is the size the header says", ctypes.sizeof(hs._Msg), 48 if wide else 32)
    check("MONITORINFO is the size the header says", ctypes.sizeof(gw._MONITORINFO), 40)
    # The same class of bug, in the foreground query that game detection leans on.
    g = gw._user32
    for name in ("GetForegroundWindow", "MonitorFromWindow"):
        check(f"gamewatch {name}.restype is declared", getattr(g, name).restype, vp)
    for name in ("GetWindowRect", "GetMonitorInfoW", "GetWindowLongW",
                 "GetWindowThreadProcessId"):
        types = getattr(g, name).argtypes
        check(f"gamewatch {name}.argtypes is declared", types is not None, True)
        if types:
            check(f"gamewatch {name} declares its HWND as a pointer", types[0] is vp, True)
    check("gamewatch GetWindowThreadProcessId takes the pid by pointer",
          g.GetWindowThreadProcessId.argtypes[1], ctypes.POINTER(wintypes.DWORD))


def case_truncation_is_real() -> None:
    print("case: on this machine, an undeclared return really is the low half")
    if not hs.NT:
        print("  SKIP not Windows")
        return
    declared = hs.native().kernel32.GetModuleHandleW(None)
    undeclared = ctypes.WinDLL("kernel32").GetModuleHandleW(None)   # nothing typed
    buf = ctypes.create_unicode_buffer(260)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetModuleFileNameW.restype = ctypes.c_uint
    k.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    full_ok = bool(k.GetModuleFileNameW(declared, buf, 260))
    print(f"    declared:   {declared:#x} (upper 32 bits "
          f"{'set' if declared >> 32 else 'clear'}) -> names a module: {full_ok}")
    print(f"    undeclared: {undeclared:#x} -> "
          f"names a module: {bool(k.GetModuleFileNameW(undeclared, buf, 260))}")
    check("the declared call returns a handle Windows recognises", full_ok, True)
    if declared >> 32:
        check("an undeclared restype loses the top of it", undeclared == declared, False)
        check("and what is left is the low half",
              undeclared & 0xFFFF_FFFF, declared & 0xFFFF_FFFF)
    else:
        print("    this desk has a low module base, so truncation is not visible here")


# ---------------------------------------------------------------- the lifecycle
def case_full_handles() -> None:
    print("case: a full-width handle stays full-width, and goes back exactly once")
    fake = FakeNative()
    with fake_native(fake):
        w = hs.EventWindow(lambda m, wp, lp: None)
        check("the window came up", w.start(), True)
        hwnd = w.hwnd
        check("it is the handle that was issued", hwnd in fake.created, True)
        check("our code still holds all 64 bits of it", bool(hwnd or 0) and
              (hwnd >> 32) != 0, True)
        name, hinstance, proc = fake.classes[0]
        check("the class is the shared one, not a fresh name", name, hs.CLASS_NAME)
        check("hInstance reached the struct intact", (hinstance or 0) >> 32 != 0, True)
        check("the callback pointer reached it too", bool(proc), True)
        # The registered set is stated as the module's own source list rather than a
        # fixed pair: the claim is "every display source we know asks for a
        # registration, in order, none invented" - and when #35 added the ranked
        # session-display source, a hard-coded pair would have been the thing that
        # went stale.
        check("every display source was registered", [g for _, g in fake.registrations],
              [bytes(g) for _, g in hs.DISPLAY_SOURCES])
        check("against the real window", {h for h, _ in fake.registrations}, {hwnd})
        issued = list(fake.handles)
        check("every notification handle is retained", list(w._notify), issued)
        w.close()
        check("each notification handle back exactly once", sorted(fake.unregistered),
              sorted(issued))
        check("none of them twice", max(Counter(fake.unregistered).values(), default=0), 1)
        check("the window handle was never used instead",
              any(h == hwnd for h in fake.unregistered), False)
        check("the session registration is undone once", fake.session_unregistered,
              [hwnd])
        check("the window is destroyed once", fake.destroyed, [hwnd])
        check("nothing is retained afterwards", (w.hwnd, w._notify), (None, []))
        check("close() joined the pump", w._thread.is_alive(), False)


def case_readiness_is_registration() -> None:
    print("case: ready means registered, not merely built")
    fake = FakeNative(register_delay=0.30)
    with fake_native(fake):
        h = hs.HostState(gap_s=1.0, poll_s=3600.0, events=True)
        try:
            check("events are live", h.events_live, True)
            check("live implies every registration happened",
                  len(fake.registrations), len(hs.DISPLAY_SOURCES))
            check("and the session one", h.events["session"], True)
            check("summary names every source",
                  "events=" + ",".join(n for n, _ in hs.DISPLAY_SOURCES) + ",session"
                  in h.summary(), True)
        finally:
            h.close()


def case_pump_failure_recovers() -> None:
    print("case: a dead pump is a dead source, and the loop puts it back")
    fake = FakeNative()
    real_idle = hs.idle_seconds
    with fake_native(fake):
        h = hs.HostState(gap_s=3600.0, poll_s=3600.0, events=True)
        try:
            first = list(fake.handles)
            check("live to start with", h.events_live, True)
            fake.fail_peeks = 1                   # one failed PeekMessageW is enough
            check("the pump stops", wait_until(lambda: h._ev.dead), True)
            check("health stops claiming it", h.events_live, False)
            check("health says why", "PeekMessageW" in h.event_error, True)
            check("summary names no source", "events=none" in h.summary(), True)
            check("the dead window was destroyed", fake.destroyed, [fake.created[0]])
            check("its handles went back once", sorted(fake.unregistered), sorted(first))
            # The fault was a bad minute, not a broken machine: what is being tested
            # here is that the loop notices at all, and puts the pump back.
            fake.fail_peeks = 0
            hs.idle_seconds = lambda: 60.0        # the desk's input clock is not the subject
            h._poll_left = 3600.0
            h.tick(1.0)
            check("the next tick rebuilds it", h.events_live, True)
            check("with the registrations back", sorted(h.events.values()),
                  [True] * len(h.events))
            check("and the error gone", h.event_error, "")
            check("counted as a repair", h._ev.repairs, 1)
            check("a second window, not a third", len(fake.created), 2)
            check("one window destroyed, one still up", len(fake.destroyed), 1)
            check("health names the sources again",
                  "events=" + ",".join(n for n, _ in hs.DISPLAY_SOURCES) + ",session"
                  in h.summary(), True)
            second = [x for x in fake.handles if x not in first]
            check("the rebuilt pump holds a fresh handle per source",
                  (len(second), sorted(h._ev._notify)),
                  (len(hs.DISPLAY_SOURCES), sorted(second)))
        finally:
            hs.idle_seconds = real_idle
            h.close()
    check("every handle ever issued went back exactly once",
          sorted(fake.unregistered), sorted(fake.handles))
    check("and no handle twice", max(Counter(fake.unregistered).values(), default=0), 1)


def case_wedged_pump_is_not_replaced() -> None:
    print("case: a pump that is stuck gets no second window")
    fake = FakeNative()
    with fake_native(fake):
        w = hs.EventWindow(lambda m, wp, lp: None)
        check("up", w.start(), True)
        fake.block_peeks = True
        # Only stuck once it is actually inside the call: a pump that is between
        # peeks would just notice `_stop` and leave, which is the good case.
        check("the pump is inside a call that will not return",
              wait_until(fake.inside.is_set), True)
        check("restart refuses rather than duplicating", w.restart(wait_s=0.2), False)
        check("still exactly one window ever created", len(fake.created), 1)
        check("and the refusal is said", "wedged" in (w.error or ""), True)
        fake.released.set()                     # the stuck call finally returns
        check("it then retires itself", wait_until(lambda: fake.destroyed == fake.created),
              True)
        check("and is supervised as dead, not live", w.dead, True)
        w.close()


def case_native_smoke() -> None:
    print("case: the real window on this machine")
    if not hs.NT:
        print("  SKIP not Windows: nothing here to build a window on")
        return
    probe = ctypes.WinDLL("user32", use_last_error=True)
    probe.IsWindow.restype = ctypes.c_long
    probe.IsWindow.argtypes = [ctypes.c_void_p]
    w = hs.EventWindow(lambda m, wp, lp: None)
    if not w.start():
        # A service session and a headless run have no window station to make a
        # top-level window in. That is the documented degradation, not a failure.
        print(f"  SKIP no window station here: {w.error}")
        return
    first = w.hwnd
    try:
        check("Windows recognises the handle we were given", probe.IsWindow(w.hwnd), 1)
        check("a power-setting registration took", w.reg_console or w.reg_monitor, True)
        # Printed, not asserted: how wide Windows happens to make a notification
        # handle on a given build is Windows' business, and the gate must not turn
        # red over it. The fake above is what pins our behaviour down.
        print(f"    this desk: hwnd={w.hwnd:#x} hinstance={w._hinstance:#x} "
              f"notify={[hex(h) for h in w._notify]}")
    finally:
        w.close()
    check("close() leaves no window", probe.IsWindow(first or 0), 0)
    check("and retains nothing", w._notify, [])
    check("close() joined the pump", w._thread.is_alive(), False)
    check("restart comes back up", (w.restart(), w.live), (True, True))
    check("as a different window, not a second one", w.hwnd != first, True)
    check("with its own registrations retained",
          len(w._notify), len(hs.DISPLAY_SOURCES))
    w.close()


def main() -> int:
    for fn in (case_declared_abi, case_truncation_is_real, case_full_handles,
               case_readiness_is_registration, case_pump_failure_recovers,
               case_wedged_pump_is_not_replaced, case_native_smoke):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a case that raises is a failed case,
            print(f"  FAIL {fn.__name__} raised {type(e).__name__}: {e}")
            fails.append(fn.__name__)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
