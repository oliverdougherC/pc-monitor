"""What the machine is doing: asleep, monitor off, session locked.

The panel's job is to be readable when it is worth reading and dark when it is
not, and until now its only signal was "nobody has moved the mouse for 45
minutes". That misses the three things that actually happen at a desk:

  * the PC goes to sleep and the panel stays lit on its last frame;
  * Windows turns the displays off on their own timeout — same;
  * the machine comes back and the panel does not: the COM port is gone, the
    panel rebooted itself back to portrait, and the diff transport still believes
    the pixels are current. Half of "it doesn't come back when the PC does".

Two deliberately redundant sources:

  1. EVENTS — a hidden **top-level** window (not a message-only one: those are
     excluded from broadcasts, which is the only thing we are here to receive)
     gets `WM_POWERBROADCAST` for suspend/resume, the power-setting
     notifications for the display (the session's own, then the console's, then
     the legacy monitor-power one), and `WM_WTSSESSION_CHANGE` for the session
     lock. This is the only way to see a suspend *before* it happens — the only
     moment at which the panel can still be switched off by us.

  2. THE LOOP ITSELF — `tick()` measures the gap between ticks. A gap far larger
     than the tick interval means this process was frozen: asleep, hibernating,
     or starved. It cannot tell you which, and it does not need to: the recovery
     is the same for all three, so it raises the same resume edge. It is also the
     belt to the window's braces — a broadcast is best effort, and a process
     frozen across a suspend never sees the pre-suspend query — but it cannot lie
     about time. What it can do is be *read* wrongly, so the caller also says how much
     of the gap it spent executing (`work_s`), and only the rest counts as a freeze:
     the loop grinding away at a half-minute panel rebuild is this process being busy,
     which is the opposite of the machine having been asleep, and the difference is
     what stops a recovery from being caused by the recovery.

Everything degrades on purpose. No window station (a service, a headless run, a
non-NT platform) and the event layer reports "off", the gap watchdog and the
idle timeout keep working, and `summary()` prints which sources are live, so
log.log always says which of the three the panel is currently trusting. The same
is true of an event layer that *had* a window and lost one: `summary()` says what
is live now rather than what came up at start-up, and `tick()` puts the pump back.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
import time
import uuid

NT = os.name == "nt"

# --- messages and notification codes -----------------------------------------
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WM_DISPLAYCHANGE = 0x007E
WM_QUIT = 0x0012
WM_DESTROY = 0x0002
PBT_POWERSETTINGCHANGE = 0x8013
PBT_APMQUERYSUSPEND = 0x0000
PBT_APMQUERYUSERSUSPEND = 0x000B
PBT_APMSUSPEND = 0x0004
PBT_APMUSERSUSPEND = 0x0006          # WinUser.h also spells 0x6 "PBT_APMRESUMECRITICAL"
PBT_APMRESUMECRITICAL = 0x0006          # WinUser.h also spells 0x6 "PBT_APMUSERSUSPEND"
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMUSERRESUME = 0x0008
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_CONSOLE_CONNECT, WTS_CONSOLE_DISCONNECT = 0x1, 0x2
WTS_REMOTE_CONNECT, WTS_REMOTE_DISCONNECT = 0x3, 0x4
WTS_SESSION_LOGON, WTS_SESSION_LOGOFF = 0x5, 0x6
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
WTS_SESSION_CREATE, WTS_SESSION_TERMINATE = 0xA, 0xB        # reserved, not logon/logoff
NOTIFY_FOR_THIS_SESSION = 0x0
DEVICE_NOTIFY_WINDOW_HANDLE = 0x0


def _guid(text: str) -> bytes:
    """The 16 bytes a GUID occupies in memory, from the string it is written as.

    The first three fields are little-endian and the last two are not, which is what
    `uuid.UUID(...).bytes_le` produces and the order `RegisterPowerSettingNotification`
    wants. Deriving the bytes from the canonical string is the point: the hand-copied
    hex that stood here was wrong for both display settings, and registering a GUID
    that does not exist *succeeds*, so the only symptom was that the notifications
    simply never arrived.
    """
    return uuid.UUID(text).bytes_le


# The settings that can tell us the display went away, best first. Microsoft says an
# application running in an interactive user session should use
# `GUID_SESSION_DISPLAY_STATUS`, and that `GUID_MONITOR_POWER_ON` is superseded by the
# console one, so the three are registered in this order and do not count equally: see
# `DISPLAY_RANK` and `_on_setting`. The older two stay registered because on an older
# build they are the only answers, and because a failed registration is normal enough
# that trusting only one source is trusting nothing.
GUID_SESSION_DISPLAY_STATUS = _guid("2B84C20E-AD23-4DDF-93DB-05FFBD7EFCA5")
GUID_CONSOLE_DISPLAY_STATE = _guid("6FE69556-704A-47A0-8F24-C28D936FDA47")
GUID_MONITOR_POWER_ON = _guid("02731015-4510-4526-99E6-E5A17EBD1AEA")

DISPLAY_SOURCES = (("session-display", GUID_SESSION_DISPLAY_STATUS),
                   ("console-display", GUID_CONSOLE_DISPLAY_STATE),
                   ("monitor-power", GUID_MONITOR_POWER_ON))
DISPLAY_RANK = {guid: i for i, (_, guid) in enumerate(DISPLAY_SOURCES)}


def _guid_bytes(raw: bytes) -> ctypes.Array[ctypes.c_ubyte]:
    return (ctypes.c_ubyte * 16)(*raw)


class _WndClass(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", ctypes.c_void_p)]


class _Msg(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint), ("ptx", ctypes.c_long), ("pty", ctypes.c_long)]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                              ctypes.c_size_t, ctypes.c_ssize_t) if NT else None


WS_POPUP = 0x80000000
ERROR_CLASS_ALREADY_EXISTS = 1410       # ERROR_CLASS_ALREADY_EXISTS: re-registering is fine
CLASS_NAME = "PCMonitorHostState"


class Native:
    """Every foreign call this module makes, declared once, from the SDK signatures.

    The declarations are not decoration, and the ones that used to be missing were
    not harmless. ctypes hands an undeclared foreign return back as a C `long`, and
    on x64 a handle is pointer-sized: `GetModuleHandleW` returns the executable's
    image base and `CreateWindowExW` returns the new HWND, so left undeclared both
    come back as the *low half* of the value, sign extended. Every later use of
    that value - registering for notifications, posting the WM_QUIT that ends the
    pump, destroying the window - then acts on a window that does not exist, and
    the calls fail in a thread nobody is watching, so the panel reports event
    sources it no longer has. The same is why `lParam` is a *signed* pointer-sized
    value (messages arrive with -1 in it): without `argtypes` ctypes refuses to
    hand it to a `c_void_p` parameter and DefWindowProc raises OverflowError on
    every routed message.

    The DLLs are ours rather than `ctypes.windll` so that `use_last_error` applies:
    ctypes then saves the error for *this* thread after each foreign call, and
    `ctypes.get_last_error()` reads it back without whatever Python did in between
    having run another API call over the top of it.
    """

    def __init__(self) -> None:
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        k = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        vp, up, sp = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ssize_t
        i32, u32, b32 = ctypes.c_int, ctypes.c_uint32, ctypes.c_long

        # the pump
        u.DefWindowProcW.restype = sp
        u.DefWindowProcW.argtypes = [vp, u32, up, sp]
        u.PeekMessageW.restype = b32          # BOOL: -1 is a real answer, not True
        u.PeekMessageW.argtypes = [ctypes.POINTER(_Msg), vp, u32, u32, u32]
        u.PostMessageW.restype = b32
        u.PostMessageW.argtypes = [vp, u32, up, sp]
        u.TranslateMessage.restype = b32
        u.TranslateMessage.argtypes = [ctypes.POINTER(_Msg)]
        u.DispatchMessageW.restype = sp
        u.DispatchMessageW.argtypes = [ctypes.POINTER(_Msg)]

        # the window itself
        k.GetModuleHandleW.restype = vp                    # HMODULE
        k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        u.RegisterClassExW.restype = ctypes.c_uint16       # ATOM
        u.RegisterClassExW.argtypes = [ctypes.POINTER(_WndClass)]
        u.CreateWindowExW.restype = vp                     # HWND
        u.CreateWindowExW.argtypes = [u32, ctypes.c_wchar_p, ctypes.c_wchar_p, u32,
                                      i32, i32, i32, i32, vp, vp, vp, vp]
        u.DestroyWindow.restype = b32
        u.DestroyWindow.argtypes = [vp]
        u.UnregisterClassW.restype = b32
        u.UnregisterClassW.argtypes = [ctypes.c_wchar_p, vp]

        # the notifications
        u.RegisterPowerSettingNotification.restype = vp    # HPOWERNOTIFY
        u.RegisterPowerSettingNotification.argtypes = [vp,
                                                       ctypes.POINTER(ctypes.c_ubyte * 16),
                                                       u32]     # LPCGUID: the 16 bytes themselves
        u.UnregisterPowerSettingNotification.restype = b32
        u.UnregisterPowerSettingNotification.argtypes = [vp]   # the handle, not the HWND

        # The surface this module actually calls, bound to the declared objects
        # above: one place to read the whole ABI, and the seam a test stands in
        # for when it needs handles that nobody has to earn.
        for name in ("DefWindowProcW", "DispatchMessageW", "PeekMessageW",
                     "PostMessageW", "TranslateMessage", "CreateWindowExW",
                     "DestroyWindow", "RegisterClassExW", "UnregisterClassW",
                     "RegisterPowerSettingNotification",
                     "UnregisterPowerSettingNotification"):
            setattr(self, name, getattr(u, name))
        self.GetModuleHandleW = k.GetModuleHandleW

        # wtsapi32 is not on every SKU, and losing it costs lock events and nothing
        # else, so it is allowed to be absent here rather than taking the window down.
        self.WTSRegisterSessionNotification = None
        self.WTSUnRegisterSessionNotification = None
        try:
            w = self.wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
            w.WTSRegisterSessionNotification.restype = b32
            w.WTSRegisterSessionNotification.argtypes = [vp, u32]
            w.WTSUnRegisterSessionNotification.restype = b32
            w.WTSUnRegisterSessionNotification.argtypes = [vp]
            self.WTSRegisterSessionNotification = w.WTSRegisterSessionNotification
            self.WTSUnRegisterSessionNotification = w.WTSUnRegisterSessionNotification
        except Exception:  # noqa: BLE001 - no session api costs lock events, nothing else
            self.wtsapi32 = None

    def last_error(self) -> int:
        return ctypes.get_last_error()

    def register_session(self, hwnd: int) -> bool:
        if self.WTSRegisterSessionNotification is None:
            return False
        try:
            return bool(self.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION))
        except Exception:  # noqa: BLE001
            return False

    def unregister_session(self, hwnd: int) -> None:
        if self.WTSUnRegisterSessionNotification is not None:
            self.WTSUnRegisterSessionNotification(hwnd)


_native: Native | None = None


def native() -> Native:
    """The declared native calls, built once per process.

    A function rather than a module-level object because it has to be swappable:
    tools/eventwindow_selftest.py puts a fake in here, and that is the only honest
    way to test a lifecycle whose real handles arrive from a window station and a
    suspend nobody can schedule.
    """
    global _native
    if _native is None:
        _native = Native()
    return _native


# ------------------------------------------------------------- cheap host polls
class _LastInputInfo(ctypes.Structure):
    """`LASTINPUTINFO`: a size and a tick, both DWORDs of the boot clock.

    Named at module scope because the read and its types belong together, and the
    types are the point: ctypes hands an unannotated call back as a C `int`, so a
    `DWORD` read through the default is the same bit pattern read two different ways.
    """
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


if NT:
    # Declared once, for the same reason. `GetLastInputInfo` takes a pointer to the
    # structure above and returns a BOOL; `GetTickCount64` returns a ULONGLONG, and
    # the default signed `int` truncates it - which is how the idle clock came to
    # report a desk that had been up for a month as one somebody had just typed on.
    ctypes.windll.user32.GetLastInputInfo.restype = ctypes.c_bool
    ctypes.windll.user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    ctypes.windll.kernel32.GetTickCount64.argtypes = []


def last_input_tick() -> int | None:
    """The boot tick the last keyboard/mouse input arrived on, or None if unreadable.

    The raw 32-bit `dwTime`, deliberately: it is half of a subtraction, and the other
    half has to be read on the same clock for the difference to mean anything, so the
    two are kept apart until `idle_seconds` puts them together.
    """
    if not NT:
        return None
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
    except Exception:  # noqa: BLE001 - a missing idle clock must not break the loop
        pass
    return None


def uptime_ms() -> int:
    """Milliseconds the boot clock has been running: `GetTickCount64`.

    The 64-bit form, because `dwTime` is a 32-bit window onto this same counter, and
    only a clock long enough to say *which* window that tick came from can put an age
    on it. (The 32-bit `GetTickCount` is also the value ctypes' default return type
    signs: it turns negative at 24.9 days of uptime and stays there for the next 24.8
    - half of every boot, in other words.)
    """
    return int(ctypes.windll.kernel32.GetTickCount64())


def input_age_ms(last: int, now: int) -> int | None:
    """Milliseconds between a 32-bit boot tick and the 64-bit boot clock `now`.

    `last` is the low half of the counter `now` was read from, so the age is the
    distance to the newest value with that low half which is not in the future: put
    back as many whole 2^32 windows as fit between the two. Subtracting them as they
    come is wrong twice over - the signed reading of the tick count goes negative at
    24.9 days and the DWORD itself wraps at 49.7 - and clamping the answer at zero, as
    this used to, converts both boundaries into "somebody just typed". That is the one
    answer the panel acts on by *not* going dark, and the one that reads as proof of a
    wake to whatever is waiting out a suspend.

    None when the reads disagree about which way the clock runs: an input tick ahead
    of the clock that just read it is an unanswerable question, not an input that
    happened this instant.
    """
    if now < last:
        return None
    return now - (last + (((now - last) >> 32) << 32))


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input, or None where unanswerable.

    None, not 0.0, when the call fails: "nobody has touched the keyboard for 0
    seconds" and "I cannot tell" look identical to the screen-off rule and opposite
    things to the wake rule below, where *proof* of input is what says the machine is
    awake again.
    """
    last = last_input_tick()
    if last is None:
        return None
    try:
        age = input_age_ms(last, uptime_ms())
    except Exception:  # noqa: BLE001 - a missing idle clock must not break the loop
        return None
    return None if age is None else age / 1000.0


def on_ac() -> bool:
    """Mains power? A desktop with no battery reports "unknown", and is on mains.

    Only used to pick which half of a power scheme's settings applies.
    """
    if not NT:
        return True
    try:
        class PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte),
                        ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_uint),
                        ("BatteryFullLifeTime", ctypes.c_uint)]

        ps = PowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(ps)):
            return ps.ACLineStatus != 0        # 0 = battery, 1 = mains, 255 = unknown
    except Exception:  # noqa: BLE001
        pass
    return True


def console_session() -> int | None:
    """The session id currently attached to the physical console, if knowable."""
    if not NT:
        return None
    try:
        k = ctypes.windll.kernel32
        k.WTSGetActiveConsoleSessionId.restype = ctypes.c_uint
        sid = k.WTSGetActiveConsoleSessionId()
        return None if sid in (0xFFFFFFFF, 0) else int(sid)
    except Exception:  # noqa: BLE001
        return None


def logon_ui_running() -> bool | None:
    """True/False while the lock screen is up, None when unanswerable.

    `LogonUI.exe` exists only on the secure desktop, so it is a usable cross-check
    for the session-lock event, and unlike `OpenInputDesktop` it asks for no
    window-station rights. Used to *confirm* a lock, never to clear one: for the
    unlock edge the event is the only trusted source.
    """
    if not NT:
        return None
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() == "logonui.exe":
                return True
        return False
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- event window
class EventWindow:
    """Hidden top-level window that forwards messages to a HostState. Own thread.

    PeekMessage on a 50 ms poll rather than GetMessage: a blocking GetMessage
    needs a posted WM_QUIT to exit, and a thread that cannot exit keeps the
    interpreter alive at shutdown. Polling makes `close()` deterministic.

    The thread is a *service* now, not a one-shot: `supervise()` brings it back
    when it stops, which is the only way a pump that died at 3 a.m. does not leave
    the panel trusting a source it no longer has until the next reboot.
    """

    def __init__(self, sink, api: Native | None = None, repair_s: float = 30.0) -> None:
        self.sink = sink
        self.api = api or native()
        self.hwnd: int | None = None
        self.error: str | None = None
# Which display settings this window is actually subscribed to, by the name
        # `_on_setting` reports them under: `HostState` needs it to know when a
        # lower-ranked setting is allowed to answer at all. The dict is the single
        # source; `reg_console` / `reg_monitor` below stay as the old attribute
        # names, derived rather than duplicated, because callers and the ABI
        # selftest read the state through them.
        self.reg: dict[str, bool] = {name: False for name, _ in DISPLAY_SOURCES}
        self.reg_session = False
        self._keep: list = []
        self.repairs = 0
        self.repair_s = float(repair_s)
        # The HPOWERNOTIFY handles Windows gave back, in the order they arrived.
        # These are the only things `UnregisterPowerSettingNotification` accepts,
        # so they are kept rather than reduced to a bool.
        self._notify: list[int] = []
        # One callback for the life of the object: the window class holds the raw
        # function pointer, so a proc that is garbage collected while its class is
        # still registered takes the process down on the next message routed to any
        # window of that class - including a window made by a later restart.
        self._proc = None
        self._hinstance: int | None = None
        self._stop = threading.Event()
        self._up = threading.Event()         # built and registered, or failed trying
        self._down = threading.Event()       # the pump has exited and torn down
        self._thread: threading.Thread | None = None
        self._last_repair = 0.0

    # -- health ----------------------------------------------------------------
    @property
    def live(self) -> bool:
        """Is there a window right now that Windows can send to?"""
        return bool(self.hwnd)

    @property
    def dead(self) -> bool:
        """Started once and no longer pumping: the state the loop has to react to."""
        return self._thread is not None and self._down.is_set()

    @property
    def reg_console(self) -> bool:
        """The console-display registration, under the name predating the third source."""
        return self.reg["console-display"]

    @property
    def reg_monitor(self) -> bool:
        """The monitor-power registration, same derivation: one truth, two names."""
        return self.reg["monitor-power"]

    # -- lifecycle -------------------------------------------------------------
    def start(self, wait_s: float = 3.0) -> bool:
        if not NT:
            self.error = "not Windows"
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="host-events")
        self._thread.start()
        # Waiting on the build's own event, not on `hwnd`: the handle is published
        # as soon as the window exists so that teardown can find it, and readiness
        # means the notifications are registered too. Acting on the first is the
        # same as acting on neither.
        if not self._up.wait(wait_s):
            self.error = self.error or "event thread never came up"
        return bool(self.hwnd) and not self.error

    def close(self, join_s: float = 2.0) -> None:
        """Stop the pump and wait for it: teardown runs on *its* thread, and a
        caller that does not join cannot say the handles have been released."""
        self._stop.set()
        if self.hwnd:
            try:
                self.api.PostMessageW(self.hwnd, WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(join_s)

    def supervise(self, wait_s: float = 1.0) -> bool:
        """Rebuild the pump if it stopped. True when events are running now.

        Called from the loop's tick because nothing else is watching this thread.
        `repair_s` keeps a machine that will not take a window at all (no window
        station, a service session) from rebuilding on every tick: the answer is
        still said, just not sixty times a minute.
        """
        if not self.dead:
            return bool(self.hwnd)
        now = time.monotonic()
        if now - self._last_repair < self.repair_s:
            return False
        self._last_repair = now
        return self.restart(wait_s)

    def restart(self, wait_s: float = 3.0) -> bool:
        """Run the whole lifecycle again, one window and one set of registrations.

        `close()` joins first, so a pump that is wedged rather than dead is found
        out here: two live pumps would mean two windows of the same class and two
        of every notification, i.e. every event delivered twice to one sink.
        """
        self.close()
        if self._thread is not None and self._thread.is_alive():
            # Stuck rather than dead, and a stuck thread cannot be killed: building
            # now would put a second window of this class on the same broadcasts.
            # It retires itself as soon as the call it is in returns, and the next
            # `supervise()` finds it properly dead.
            self.error = "event thread is wedged, not rebuilding"
            return False
        self.repairs += 1
        self.error = None
        self._up.clear()
        self._down.clear()
        self._stop.clear()
        return self.start(wait_s)

    # -- thread body ---------------------------------------------------------
    def _run(self) -> None:
        try:
            self._build()
            self._up.set()                # ready: window *and* registrations
            self._pump()
        except Exception as e:  # noqa: BLE001 - the event layer is optional by design
            self.error = f"{type(e).__name__}: {e}"
        finally:
            # Teardown here, not after the loop: a pump that raises on its third
            # PeekMessageW used to leave its window, its registrations and its
            # callback alive with nobody left to close them, and every restart then
            # added another set.
            self._up.set()                # a failed build must still release start()
            self._teardown()
            self._down.set()

    def _pump(self) -> None:
        a = self.api
        msg = _Msg()
        while not self._stop.is_set():
            got = a.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1)   # PM_REMOVE
            if got == 0:
                time.sleep(0.05)
                continue
            if got < 0:
                # A failed PeekMessageW is not a reason to go quiet: say so, and
                # let `supervise()` put the pump back.
                raise OSError(f"PeekMessageW: {a.last_error()}")
            if msg.message == WM_QUIT:
                break
            a.TranslateMessage(ctypes.byref(msg))
            a.DispatchMessageW(ctypes.byref(msg))

    def _build(self) -> None:
        a = self.api
        if self._proc is None:
            def wnd_proc(hwnd, msg, wparam, lparam):        # noqa: ANN001
                try:
                    self.sink(int(msg), int(wparam), int(lparam))
                except Exception:  # noqa: BLE001 - a handler bug must not kill the pump
                    pass
                return a.DefWindowProcW(hwnd, msg, wparam, lparam)
            self._proc = _WNDPROC(wnd_proc)
        self._hinstance = a.GetModuleHandleW(None)
        wc = _WndClass()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = self._hinstance
        wc.lpszClassName = CLASS_NAME
        # Re-registering an existing class is the normal case after a restart, and
        # the class is deliberately the same one: a second name would mean a second
        # window receiving the same broadcasts.
        if not a.RegisterClassExW(ctypes.byref(wc)) \
                and a.last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise OSError(f"RegisterClassExW: {a.last_error()}")
        hwnd = a.CreateWindowExW(0, CLASS_NAME, "PC Monitor host state", WS_POPUP,
                                 0, 0, 0, 0, 0, 0, self._hinstance, None)
        if not hwnd:
            raise OSError(f"CreateWindowExW: {a.last_error()}")
        self.hwnd = hwnd

# RegisterPowerSettingNotification lives in user32 (kernel32 only on Windows CE)
        # and returns the registration handle, 0 on failure. The handle is not a
        # success flag to be thrown away: it is the only thing the unregister call
        # takes, so keeping it is what makes the lifecycle reversible instead of a
        # slow leak - one per restart of the pump. The GUID is pinned in `_keep`
        # rather than left as a temporary because the API takes a *pointer* to it
        # and `byref` does not keep what it points at alive: an array can be freed
        # while the call is still reading it.
        reg = a.RegisterPowerSettingNotification
        for name, guid in DISPLAY_SOURCES:
            blob = _guid_bytes(guid)
            self._keep.append(blob)
            handle = reg(hwnd, ctypes.byref(blob), DEVICE_NOTIFY_WINDOW_HANDLE)
            self.reg[name] = bool(handle)
            if handle:
                self._notify.append(int(handle))
        try:
            self.reg_session = a.register_session(hwnd)
        except Exception:  # noqa: BLE001 - a session source that refuses is a fact, not a crash
            self.reg_session = False

    def _teardown(self) -> None:
        if not self.hwnd:
            return
        a = self.api
        hwnd, handles, session = self.hwnd, self._notify, self.reg_session
        self.hwnd, self._notify = None, []
        self.reg = {name: False for name, _ in DISPLAY_SOURCES}
        self.reg_session = False
        try:
            # Each handle back exactly once, and it is the *notification* handle:
            # passing the HWND back is a call that fails while the registration
            # stays alive, so the next build quietly holds a second one.
            for h in handles:
                a.UnregisterPowerSettingNotification(h)
            if session:
                a.unregister_session(hwnd)
        except Exception:  # noqa: BLE001 - a half-released source still must not hold the window
            pass
        finally:
            try:
                a.DestroyWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------- the state
# How far the reported age of the last input has to beat what time alone would have
# made of it before it counts as *new* input. The idle clock and the loop's own
# accounting of the tick are separate sources reconciled once per read, so a fraction
# of a second of disagreement between them is measurement noise; half a second of
# real movement is not.
INPUT_PROGRESS_S = 0.5

# How many expected ticks may pass before the loop's own gap counts as a freeze. The
# multiple belongs to the *cadence the loop was asked to keep*, never to the elapsed
# time being judged: a healthy loop parks for about one cadence, so anything under a
# few cadences is the app being slow rather than the machine being asleep — and
# `gap_s` stays the floor, so a 1 Hz loop still declares a freeze after five seconds.
GAP_TOLERANCE = 3.0


class HostState:
    """One refreshed view of the machine, per tick.

    Two edges come out of this, and which one an event raises is the whole
    difference between a cheap repaint and a rebuild:

      * `take_resume()` - the machine stopped and came back (a resume message, or a
        loop gap long enough that it might have). Everything downstream is suspect:
        the COM port, the panel's orientation, the ETW session, the locked game.
      * `take_refresh()` - the *view* changed shape: a monitor came back on, the
        session was unlocked, `WM_DISPLAYCHANGE` fired because a display arrived or a
        driver re-moded one. The machine kept running; the panel needs a whole frame
        and nothing more.

    Both are edges, not levels: each returns its reason once and then clears itself,
    so the loop acts once per event burst instead of every tick while the panel
    enumerates. Raising the second kind as the first is what froze the loop - a
    resolution change used to tear down a working link, restart the capture and drop
    the game target, and the bring-up it waited for took tens of seconds.

    A suspend is tracked twice, on purpose: `asleep` is the belief the light decision
    acts on, and `suspend_pending` is the *request* Windows made that nothing has
    answered yet. The rule used to be "any input younger than two seconds proves a
    wake", and the click on Start -> Sleep is exactly that, so the click cancelled the
    request it had just made. Answering one now takes an answer - a resume message,
    the loop having been frozen across the suspend, or input that moved the clock
    forward *from the request* - and while a request is outstanding the loop holds a
    resume edge rather than spending the milliseconds before the bus loses power on
    rebuilding the link.
    """

    def __init__(self, gap_s: float = 5.0, poll_s: float = 5.0,
                 cadence_s: float = 1.0, events: bool = True) -> None:
        self.gap_s = float(gap_s)
        # What the caller says a tick is *supposed* to cost. The gap watchdog needs an
        # expectation to judge against; without one it has nothing but the number it is
        # measuring, which is how a 30-second suspend came to be compared with 90.
        self.cadence_s = float(cadence_s)
        self.poll_s = float(poll_s)
        self.asleep = False
        self.asleep_reason = ""
        # A suspend is tracked as a request as well as a belief: `asleep` is what the
        # light decision acts on, `suspend_pending` that Windows has not answered the
        # request yet, and `_input_age` the last read of the input clock an answer is
        # judged against. See `_request_suspend`.
        self.suspend_pending = False
        self._input_age: float | None = None
        self.monitor_on: bool | None = None      # None = nothing has said yet
        self.monitor_seeded = False              # the current value is a derivation
        self.monitor_seed = ""                   # …and how it was arrived at
        self.locked = False
        self.console_lost = False
        self.idle_s = 0.0
        self.idle_known = False
        self.resume: str | None = None       # the machine stopped and came back
        self.refresh: str | None = None      # the view changed shape: repaint only
        self.last_event = "-"
        self.suspends = 0
        self.resumes = 0
        # Signalled by the event thread on every message that changes the answer.
        # The loop waits on this instead of sleeping blindly, so "the PC is going
        # to sleep" switches the panel off in milliseconds rather than at the top
        # of the next second — which is the only window in which it still works.
        self.event = threading.Event()
        self._tick_now = time.monotonic()
        self._primed = False
        self._asleep_since = 0.0
        self._since_resume = time.monotonic()
        self._poll_left = 0.0
        self._event_lock: bool | None = None
        # Started last, and only last: the window's thread calls `_on_event` the
        # moment it has a window, and a handler that met a half-built object would
        # be raising inside that thread, where nothing prints it.
        self._events_wanted = bool(events)
        self._closed = False
        self._ev = EventWindow(self._on_event)
        if events:
            self._ev.start()

    # -- health as seen by the loop ---------------------------------------------
    # These are views of the window, never start-up copies. A copy is the reason a
    # pump that died an hour in kept being reported as live: `summary()` printed the
    # registrations of a window that had stopped existing, and the panel's "is the
    # event layer there" question was answered from memory.
    @property
    def events_live(self) -> bool:
        return self._ev.live

    @property
    def events(self) -> dict[str, bool]:
        # Every display source the window subscribed to, by name - not a fixed pair.
        # Which ones answered is what `_on_setting` needs to rank (see
        # DISPLAY_SOURCES), and a summary that names only two of three is how a
        # silent registration failure on the newest source stays invisible.
        return {**self._ev.reg, "session": self._ev.reg_session}

    @property
    def event_error(self) -> str:
        if not self._events_wanted:
            return "events off (selftest)"
        return self._ev.error or ""

    # -- events --------------------------------------------------------------
    def _on_event(self, msg: int, w: int, l: int) -> None:      # noqa: E741
        try:
            self._dispatch(msg, w, l)
        finally:
            # Set *after* the state changes: tick() clears the flag once it has
            # read the state, and that only holds if a message's effects are
            # visible before the wake-up that announces them.
            self.event.set()

    def _dispatch(self, msg: int, w: int, l: int) -> None:      # noqa: E741
        if msg == WM_POWERBROADCAST:
            if w in (PBT_APMQUERYSUSPEND, PBT_APMQUERYUSERSUSPEND):
                # The last instant at which we can still switch the panel off.
                self._request_suspend("query-suspend")
                self.last_event = "suspend-query"
            elif w == PBT_APMSUSPEND:
                # The moment the request becomes real: the machine may lose the bus
                # before anything else runs. `PBT_APMUSERSUSPEND` is deliberately NOT
                # here - in this build's headers it is 0x6, the same number
                # WinUser.h gives to PBT_APMRESUMECRITICAL, and 0x6 is dispatched as
                # the resume below. Reading it as a suspend is the one mistake that
                # can leave the panel dark on a live desktop with nothing left to
                # wake it.
                self._request_suspend("suspend")
                self.last_event = "suspend"
            elif w in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND, PBT_APMUSERRESUME,
                       PBT_APMRESUMECRITICAL):
                # 0x6 belongs here, not with the suspends: WinUser.h gives that number
                # to PBT_APMUSERSUSPEND as well, but what it documents is "the system
                # has resumed operation" after a critical suspension, support for it
                # ended with Windows XP, and treating it as a suspend is the one
                # mistake that can leave the panel dark on a live desktop with nothing
                # left to wake it.
                self._exit_asleep(f"resume:{w:#x}")
                self.last_event = f"resume:{w:#x}"
            elif w == PBT_POWERSETTINGCHANGE:
                self._on_setting(l)
            else:
                self.last_event = f"power:{w:#x}"
            return
        if msg == WM_WTSSESSION_CHANGE:
            if w in (WTS_SESSION_LOCK, WTS_REMOTE_DISCONNECT):
                self.locked = True
                self._event_lock = True
                self.last_event = "session-lock"
            elif w in (WTS_SESSION_UNLOCK, WTS_REMOTE_CONNECT, WTS_SESSION_LOGON):
                self.locked = False
                self._event_lock = False
                self.last_event = f"session:{w:#x}"
# Unlocking, logging on, or a remote session attaching all mean
                # somebody is sitting at the machine and typing: the display is on,
                # whatever the last notification said. It is worth a repaint — the
                # panel has been showing a locked desktop's worth of nothing — but
                # not a rebuild: the machine never stopped, so the COM port and the
                # present capture are exactly as they were a moment ago.
                self.monitor_on = True
                self.monitor_seeded = False   # an answer, not the start-up guess
                self.refresh = self.refresh or "session-unlock"
            elif w in (WTS_CONSOLE_DISCONNECT, WTS_SESSION_LOGOFF):
                self.console_lost = True
                self.last_event = f"session:{w:#x}"
            elif w == WTS_CONSOLE_CONNECT:
                self.console_lost = False
                self.last_event = "console-connect"
                self.refresh = self.refresh or "console-connect"
            else:
                # 0x9 (remote-control status changed), 0xF (desktop ready) and the two
                # reserved codes 0xA/0xB are not evidence of anything here. They used
                # to be read as logon and logoff, so a session-create cleared the lock
                # and claimed the display was on, and a session-terminate said the
                # console had been handed over. Named, because an unrecognised code
                # that leaves no trace is what a later guess gets wrong again.
                self.last_event = f"session:{w:#x}"
            return
        if msg == WM_DISPLAYCHANGE:
            # A monitor arriving or leaving changes which modes and which geometry the
            # desktop ends up with, so the panel is redrawn from scratch - but it says
            # nothing about the link or the capture, and this message also arrives for
            # reasons that have no bearing on either (a second display being plugged
            # in, a driver re-moding one, a game toggling full screen). Treating it as
            # a resume used to rebuild the serial link and throw away a live present
            # capture every time one went past.
            self.last_event = "display-change"
            if self.monitor_on is False:
                self.monitor_on = True
                self.monitor_seeded = False
            self.refresh = self.refresh or "display-change"

    def _on_setting(self, lparam: int) -> None:      # noqa: E741
        """POWERBROADCAST_SETTING{GUID(16), DWORD len, DWORD value} at lparam."""
        if not lparam:
            return          # a null payload pointer is not readable, only avoidable
        try:
            raw = (ctypes.c_ubyte * 24).from_address(lparam & (2 ** 64 - 1))
            guid = bytes(raw[:16])
            data = int.from_bytes(bytes(raw[20:24]), "little")
        except (ValueError, OSError):
            return
        rank = DISPLAY_RANK.get(guid)
        if rank is None:
            self.last_event = f"setting:{guid[0:4].hex()}={data}"
            return
        name = DISPLAY_SOURCES[rank][0]
        on = data != 0                    # 2 (dimmed) is still a lit display
        better = self._display_outranked(rank)
        if better:
            # Redundant, and the better source either has just said the same thing or
            # is about to. Ignored rather than applied: which of the three messages
            # happened to arrive last is not a reason for the panel to change its mind
            # about the screen.
            self.last_event = f"{name}-{'on' if on else 'off'} behind {better}"
            return
        self.monitor_on = on
        # This is the notification the whole monitor rule is built on: it outranks the
        # start-up derivation, so the derivation's label has to go — otherwise the
        # "input beats a guess" rule in `tick()` would keep overwriting a fact.
        self.monitor_seeded = False
        self.last_event = f"{name}-{'on' if on else 'off'}"
        if on:
            # The displays came back: worth a whole frame, and nothing beyond that.
            # A monitor timing itself out and coming back is the commonest event of
            # this kind on a real desk, and it never once meant the panel's link or
            # the present capture had to be re-made.
            self.refresh = self.refresh or f"{name}-on"

    def _display_outranked(self, rank: int) -> str | None:
        """A live display setting that outranks `rank`, if there is one.

        Liveness here means *registered*, not "recently heard from": the setting we are
        subscribed to is the one we answer for, and one we are not subscribed to (an
        older Windows, or a registration that failed) is what gets to answer instead.
        The cost is a setting that registers and then stays quiet, which leaves
        `monitor_on` unknown rather than wrong, and the panel falls back to the
        start-up seed and the idle timer, which is all it had before events existed.
        """
        for name, _ in DISPLAY_SOURCES[:rank]:
            if self._ev.reg.get(name):
                return name
        return None

    def _request_suspend(self, why: str) -> None:
        """Windows has asked to suspend (or has done it): nothing may light the panel.

        This is a *request*, and it stays outstanding until something answers it. The
        rule used to be "any reading of the input clock under two seconds old proves a
        wake", and the click on Start -> Sleep is exactly that: the click cancelled the
        very request it made, the loop got a resume edge, and the panel was relit and
        relinked on its way into a suspend.

        So the request arms `_note_input` with a first reading of the clock, and only a
        reading that moved forward from there answers it. The read may fail; a missing
        clock is not an answer either, and the anchor simply waits for one it can use.
        """
        if not self.asleep:
            self.suspends += 1
            self._asleep_since = time.monotonic()
        self.asleep = True
        self.asleep_reason = why
        self.suspend_pending = True
        self._input_age = idle_seconds()

    def _note_input(self, seen: float | None, interval: float) -> bool:
        """Has the input clock moved forward since the last time it was read?

        `interval` is the gap measured *here*, between this reading and the previous
        one - never the caller's `dt`. Those two are different numbers: `dt` is the
        loop's own elapsed time, clamped and accounted, while the readings are taken
        at the top of each tick by this function. Comparing a sample against an
        interval that is not the one between the samples is what made a *stale*
        reading answer: the reading at t=.90 said .20 and the one at t=.91 said .21
        - .01 apart, aged .01, nobody touched anything - but handed in with the
        loop's dt of .91 it looked like .90 seconds of input and answered a suspend
        nobody had woken the machine from.

        A reading taken `interval` after the previous one is `interval` older if
        nothing happened, so a reading *younger* than that - by more than the slack
        between the idle clock and this loop's accounting - is a keystroke that came
        after the request, which is the only input that can answer a sleep that was
        aborted after the query (lid closed then reopened, a suspend that failed to
        take) when no resume message is ever going to arrive.

        The first reading after a request only anchors. Whatever it reports belongs
        to a desk that had not been asked to sleep yet, and the click that asked for
        it keeps reporting "just now" for two seconds afterwards.
        """
        if seen is None:
            self._input_age = None      # unreadable: nothing to judge the next by
            return False
        prev = self._input_age
        self._input_age = seen
        return prev is not None and seen + INPUT_PROGRESS_S < prev + interval

    def _exit_asleep(self, why: str) -> None:
        was = self.asleep
        if was:
            self.resumes += 1
        self.asleep = False
        self.asleep_reason = ""
        self.suspend_pending = False
        self._input_age = None
        # A wake arrives before anyone knows the display state, and
        # PBT_APMRESUMEAUTOMATIC lands while the screen is still dark: after a
        # real sleep, drop the monitor flag to unknown rather than guessing on,
        # so the next dark/lit decision waits for Windows to say so (the display
        # notification, or the session unlock). A mere loop stall keeps whatever
        # was last true — nothing about the display changed.
        if was:
            self.monitor_on = None
            self.monitor_seeded = False
        self.resume = why
        self._since_resume = time.monotonic()

    # -- start-up seed ---------------------------------------------------------
    def _display_timeout_s(self) -> float | None:
        """The active scheme's "turn off the display after" value, in seconds.

        `powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE` is the only dependable
        read: the canonical registry key for this setting exists on this machine but
        carries no `ACSettingIndex` at all (a user's change goes under the scheme's own
        GUID, which the app would then have to resolve), and the powrprof scheme APIs
        need a scheme handle plus a sub-group GUID for what is one query here.

        The value is parsed by *shape*, not by label: `powercfg` output is localised,
        so matching the English words "AC Power Setting Index" is a portability bug
        waiting to happen. GUIDs in that output are never written with a `0x` prefix,
        so the hex tokens in the tail of the output are the AC and then the DC value —
        with the word match as a preference and the positional order as a fallback.
        """
        try:
            r = subprocess.run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_VIDEO",
                                "VIDEOIDLE"], capture_output=True, text=True,
                               timeout=5.0)
        except Exception as e:  # noqa: BLE001 — a missing powercfg is not an error
            self.monitor_seed = f"powercfg: {type(e).__name__}"
            return None
        ac = dc = None
        lines = (r.stdout or "").splitlines()
        for line in lines:
            hexes = re.findall(r"0x[0-9a-fA-F]+", line)
            if len(hexes) != 1:
                continue
            val = int(hexes[0], 16)
            low = line.lower()
            if "ac" in low and ac is None:
                ac = val
            elif "dc" in low and dc is None:
                dc = val
        if ac is None or dc is None:          # localised beyond the word match
            tail = [int(h, 16) for ln in lines for h in re.findall(r"0x[0-9a-f]+", ln)]
            if len(tail) >= 2:
                ac, dc = tail[-2], tail[-1]
        want = ac if on_ac() else dc
        if want is None:
            self.monitor_seed = "no timeout in powercfg output"
            return None
        return float(want) if want > 0 else None   # 0 = never, i.e. nothing to derive

    def seed_monitor(self) -> str:
        """Work out whether the displays are already off, because nobody will say so.

        `GUID_CONSOLE_DISPLAY_STATE` is a *change* notification. A process that starts
        while the monitors are already asleep is told nothing, so `monitor_on` stays
        None — and "unknown" correctly means *do not act*, which is also why, after a
        reboot into a dark room, the desk panel sat lit for up to
        `display.screen_off_after_min`. Nothing can answer "is the monitor on right
        now": `GetDevicePowerState(\\\\.\\DISPLAY1)` fails outright on this machine
        (measured: it returns 0 — the documented ERROR_INVALID_FUNCTION for displays on
        modern GPU drivers), and `CallNtPowerInformation(SystemPowerState)` reports the
        machine's sleep state, not the screen's. So the answer is a derivation from two
        facts we do have — how long the desk has been idle, and what the power scheme
        says the timeout is — used once, at start-up, labelled as a seed everywhere it
        appears, and overridden by the first real notification.
        """
        if self.monitor_on is not None:
            return f"no seed needed (monitor={self.monitor_on})"
        timeout = self._display_timeout_s()
        if timeout is None:
            return self.monitor_seed or "no seed: no display timeout"
        idle = idle_seconds()
        if idle is None:
            return f"no seed: idle clock unreadable (timeout {timeout:.0f}s)"
        off = idle > timeout
        # The seed also refreshes the idle clock it just read: the start-up status line
        # is printed right after this, and it would otherwise say `idle=0s` about a
        # desk that has been empty for hours (a full `tick()` here would consume the
        # first-tick exemption the slow panel bring-up depends on, so it is not that).
        self.idle_s, self.idle_known = idle, True
        self.monitor_on = not off
        self.monitor_seeded = True
        self.monitor_seed = (f"{'off' if off else 'on'}: idle {idle:.0f}s vs "
                             f"{timeout:.0f}s display timeout")
        self.last_event = "monitor-seed"
        return self.monitor_seed

    # -- polling -------------------------------------------------------------
    def tick(self, dt: float, work_s: float | None = None) -> None:
        """Refresh derived state and raise the resume edge. Once per loop tick.

        `dt` is the caller's elapsed time since its previous tick — the clamped one,
        `main.elapsed_dt` — and it drives the interval counters and nothing else.
        `work_s` is how much of that elapsed time the caller spent *executing*; the
        gap measured here minus that is time this process did not run at all, which is
        the one thing a gap can evidence. A caller that cannot say (the probe, which
        sleeps between its own ticks) leaves it None and the whole gap stands.
        """
        # Clear *before* reading: a message that lands during the tick then leaves
        # the flag set, and the wait after this tick returns at once instead of
        # sleeping through an event nobody read.
        self.event.clear()
        now = time.monotonic()
        gap = now - self._tick_now
        self._tick_now = now
        seen = idle_seconds()
        self.idle_known = seen is not None
        self.idle_s = seen if seen is not None else 0.0
        if self.monitor_seeded and self.idle_known and self.idle_s < 2.0:
            # Fresh input means somebody is at the desk, and on Windows any input
            # brings the displays back — so a monitor state that was only *derived*
            # must never be what keeps the panel dark while the user is typing. An
            # answer that came from Windows (`monitor_seeded` False) is a fact and is
            # left alone; this only ever overrides a guess.
            self.monitor_on = True
            self.monitor_seeded = False
            self.last_event = "input-after-seed"
        # Judged against the cadence the loop was asked to keep, never against a multiple
        # of the elapsed time under test. main.py hands this function its own elapsed
        # dt, so `gap > 3 * dt` asked a 30-second suspend to outweigh 90 seconds and
        # announced nothing: past the dt clamp the threshold stopped rising, which left
        # the configured five-second fallback able to fire only beyond three minutes.
        # What is left after the caller's own work is subtracted is time this process
        # did not run — and subtracting it is also what keeps the threshold, now that
        # it no longer moves with the gap, from reading a slow link rebuild as the wake
        # it was recovering from and asking for the rebuild all over again.
        unaccounted = gap if work_s is None else gap - min(max(work_s, 0.0), gap)
        threshold = max(self.gap_s, GAP_TOLERANCE * self.cadence_s)
        if self._primed and unaccounted > threshold:
            # Frozen: asleep, hibernating, or starved. Whichever it was, the panel
            # and the ETW child cannot be trusted until they have been re-made.
            #
            # Decided before the input rule, and deliberately so. A frozen loop is
            # the one piece of evidence here that cannot be confused by the two clocks
            # disagreeing - after a real sleep the idle clock has not aged at all while
            # the loop's has aged by the whole suspend - so it answers for the suspend
            # it swallowed, and the panel-off intent ends with a resume edge named for
            # what actually happened rather than for a keystroke nobody made.
            self.last_event = f"gap {gap:.0f}s"
            why = (f"wake-after-{self.asleep_reason}" if self.asleep
                   else f"gap:{gap:.0f}s")
            self._exit_asleep(self.resume or why)
        if self.suspend_pending and self._note_input(seen, gap):
            # Nobody types while the machine is suspended, so input that moved the
            # clock *forward from the request* is proof it is awake again - the belt
            # to the resume message's braces, and the only answer available when a
            # sleep was aborted after the query (lid closed then reopened, a suspend
            # that failed to take) and no resume message is coming. What it must not be
            # is a small *age*: the click that invoked Sleep is input, and every
            # reading for the next two seconds says so. Judged over `gap`, the interval
            # between the two readings themselves - not the caller's dt, which is a
            # different number and made a stale reading answer (#43).
            self.last_event = "input after suspend"
            self._exit_asleep(self.resume or "input-after-suspend")
        # The first tick has no history to compare against: construction-to-first-tick
        # includes the panel bring-up, which takes half a minute when the screen is
        # deaf — and that is a slow start, not a suspend. Proven here: a deaf panel
        # made the app announce `[resume] gap:36s` before it had done anything.
        self._primed = True
        self._poll_left -= dt
        if self._poll_left <= 0.0:
            self._poll_left = self.poll_s
            if self._event_lock is None:
                seen = logon_ui_running()
                if seen is not None:
                    self.locked = seen
        if self._events_wanted and not self._closed:
            # The tick is the only witness this thread has. A pump that stopped
            # taking messages leaves the panel dark-on-suspend-by-guesswork only,
            # and `summary()` would keep naming sources that stopped answering.
            self._ev.supervise()

    def take_resume(self) -> str | None:
        """Consume the resume edge (single-shot per wake)."""
        r = self.resume
        if r:
            self.resume = None
            self._since_resume = time.monotonic()
        return r

    def take_refresh(self) -> str | None:
        """Consume the refresh edge (single-shot per burst of display events).

        The cheap half of the split: a monitor coming back on, an unlock, a resolution
        change. Whoever reads it still has to repaint the panel, and that is all -
        `app/recovery.py` turns it into one whole frame instead of a rebuild.
        """
        r = self.refresh
        if r:
            self.refresh = None
        return r

    def wait(self, timeout: float) -> bool:
        """Sleep until the next event, or `timeout`. True if an event woke us.

        The loop parks here instead of `time.sleep()`: a suspend query or a
        display-off has to reach the panel in milliseconds, and the only way to
        switch a screen off *before* the bus loses power is to not be asleep when
        the question is asked. `tick()` clears the flag *after* reading state, so
        an event that lands mid-tick wakes the next wait instead of being lost.
        """
        return bool(self.event.wait(timeout))

    def asleep_s(self) -> float:
        return (time.monotonic() - self._asleep_since) if self.asleep else 0.0

    def since_resume(self) -> float:
        return time.monotonic() - self._since_resume

    def summary(self) -> str:
        on = "?" if self.monitor_on is None else (
            f"{self.monitor_on}{'(seed)' if self.monitor_seeded else ''}")
        live = ",".join(k for k, v in self.events.items() if v) or "none"
        # `idle=0s` before the first read would claim a just-typed keyboard; `-` is the
        # truth (the first tick has not happened yet, or GetLastInputInfo failed).
        idle = f"{self.idle_s:.0f}s" if self.idle_known else "-"
        s = (f"asleep={self.asleep}({self.asleep_reason or '-'}) monitor={on} "
             f"locked={self.locked} console-lost={self.console_lost} "
             f"idle={idle} events={live}")
        # Worth its own field even though a request always darkens the panel today:
        # it is the difference between "asleep, and Windows has not asked for anything
        # back yet" and "asleep on an outstanding request the loop is holding a resume
        # edge against", and the log is where that gets read the next morning.
        if self.suspend_pending:
            s += " pending-suspend"
        if self.event_error:
            s += f" event-error={self.event_error}"
        if self.refresh:
            # Un-repainted display events are worth seeing in the log because the
            # symptom of a lost one is a panel showing an old desktop until somebody
            # touches the mouse.
            s += f" refresh={self.refresh}"
        if self.suspends or self.resumes:
            s += f" suspends={self.suspends} resumes={self.resumes}"
        return s

    def close(self) -> None:
        # Not supervising any more, and joined: this returns once the window is
        # destroyed and the notification handles are back, which is what a caller
        # shutting down (or a selftest measuring exactly-once) has to be able to ask.
        self._closed = True
        self._ev.close()
