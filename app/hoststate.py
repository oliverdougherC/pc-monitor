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
     notifications for the console display state (the monitor), and
     `WM_WTSSESSION_CHANGE` for the session lock. This is the only way to see a
     suspend *before* it happens — the only moment at which the panel can still
     be switched off by us.

  2. THE LOOP ITSELF — `tick()` measures the gap between ticks. A gap far larger
     than the tick interval means this process was frozen: asleep, hibernating,
     or starved. It cannot tell you which, and it does not need to: the recovery
     is the same for all three, so it raises the same resume edge. It is also the
     belt to the window's braces — a broadcast is best effort, and a process
     frozen across a suspend never sees the pre-suspend query — but it cannot lie
     about time.

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
PBT_APMUSERSUSPEND = 0x0006          # collides with PBT_APMRESUMECRITICAL in the headers
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMUSERRESUME = 0x0008
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_CONSOLE_CONNECT, WTS_CONSOLE_DISCONNECT = 0x1, 0x2
WTS_REMOTE_CONNECT, WTS_REMOTE_DISCONNECT = 0x3, 0x4
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
WTS_SESSION_LOGON, WTS_SESSION_LOGOFF = 0xA, 0xB
NOTIFY_FOR_THIS_SESSION = 0x0
DEVICE_NOTIFY_WINDOW_HANDLE = 0x0

# `GUID_CONSOLE_DISPLAY_STATE` is the documented "the console display is on/off"
# setting; `GUID_MONITOR_POWER_ON` is the older name for roughly the same thing.
# Which one a given Windows build actually delivers is not a thing to guess at, so
# both are registered and either counts — and tools/hoststate_probe.py prints the
# GUID it saw, which is how this was pinned on this desk.
GUID_CONSOLE_DISPLAY_STATE = bytes.fromhex("9da5dd6b092e2548 b2c21bb778014562")
GUID_MONITOR_POWER_ON = bytes.fromhex("1510730210452645 99e6e5a17e1a0a1b")


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
def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input, or None where unanswerable.

    None, not 0.0, when the call fails: "nobody has touched the keyboard for 0
    seconds" and "I cannot tell" look identical to the screen-off rule and opposite
    things to the wake rule below, where *proof* of input is what says the machine is
    awake again.
    """
    if not NT:
        return None
    try:
        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
    except Exception:  # noqa: BLE001 - a missing idle clock must not break the loop
        pass
    return None


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
        self.reg_console = self.reg_monitor = self.reg_session = False
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

        # RegisterPowerSettingNotification lives in user32 (kernel32 only on Windows CE).
        # Its return value is the registration handle, not a success flag: it is the
        # only thing the unregister call takes, so keeping it is what makes the
        # lifecycle reversible instead of a slow leak. The two GUIDs are named locals
        # because `byref` does not keep what it points at alive: a temporary array
        # can be freed while the call is still reading it.
        console_guid = _guid_bytes(GUID_CONSOLE_DISPLAY_STATE)
        monitor_guid = _guid_bytes(GUID_MONITOR_POWER_ON)
        console = a.RegisterPowerSettingNotification(hwnd, ctypes.byref(console_guid),
                                                     DEVICE_NOTIFY_WINDOW_HANDLE)
        monitor = a.RegisterPowerSettingNotification(hwnd, ctypes.byref(monitor_guid),
                                                     DEVICE_NOTIFY_WINDOW_HANDLE)
        self.reg_console, self.reg_monitor = bool(console), bool(monitor)
        self._notify = [h for h in (console, monitor) if h]
        self.reg_session = a.register_session(hwnd)

    def _teardown(self) -> None:
        if not self.hwnd:
            return
        a = self.api
        hwnd, handles, session = self.hwnd, self._notify, self.reg_session
        self.hwnd, self._notify = None, []
        self.reg_console = self.reg_monitor = self.reg_session = False
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
class HostState:
    """One refreshed view of the machine, per tick.

    `take_resume()` is an edge, not a level: it returns a reason once after a
    wake/gap/display-change and then clears itself, so the loop reconnects the
    panel and restarts the capture exactly once per wake instead of every tick
    while the panel enumerates.
    """

    def __init__(self, gap_s: float = 5.0, poll_s: float = 5.0, events: bool = True) -> None:
        self.gap_s = float(gap_s)
        self.poll_s = float(poll_s)
        self.asleep = False
        self.asleep_reason = ""
        self.monitor_on: bool | None = None      # None = nothing has said yet
        self.monitor_seeded = False              # the current value is a derivation
        self.monitor_seed = ""                   # …and how it was arrived at
        self.locked = False
        self.console_lost = False
        self.idle_s = 0.0
        self.idle_known = False
        self.resume: str | None = None
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
        return {"console-display": self._ev.reg_console,
                "monitor-power": self._ev.reg_monitor,
                "session": self._ev.reg_session}

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
                self._enter_asleep("query-suspend")
                self.last_event = "suspend-query"
            elif w == PBT_APMSUSPEND or w == PBT_APMUSERSUSPEND:
                self._enter_asleep("suspend")
                self.last_event = "suspend"
            elif w in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND, PBT_APMUSERRESUME):
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
                # Unlocking means somebody is sitting at the machine and typing:
                # the display is on, whatever the last notification said. It is
                # also worth a repaint — the panel has been showing a locked
                # desktop's worth of nothing.
                self.monitor_on = True
                self.monitor_seeded = False   # an answer, not the start-up guess
                self.resume = self.resume or "session-unlock"
            elif w in (WTS_CONSOLE_DISCONNECT, WTS_SESSION_LOGOFF):
                self.console_lost = True
                self.last_event = f"session:{w:#x}"
            elif w == WTS_CONSOLE_CONNECT:
                self.console_lost = False
                self.last_event = "console-connect"
                self.resume = self.resume or "console-connect"
            return
        if msg == WM_DISPLAYCHANGE:
            # A monitor arriving or leaving changes which COM port and which
            # orientation the panel ends up with; treat it as a re-sync point.
            self.last_event = "display-change"
            if self.monitor_on is False:
                self.monitor_on = True
                self.monitor_seeded = False
            self.resume = self.resume or "display-change"

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
        if guid == GUID_CONSOLE_DISPLAY_STATE:
            which = "console"
        elif guid == GUID_MONITOR_POWER_ON:
            which = "monitor"
        else:
            self.last_event = f"setting:{guid[0:4].hex()}={data}"
            return
        on = data != 0
        self.monitor_on = on
        # This is the notification the whole monitor rule is built on: it outranks the
        # start-up derivation, so the derivation's label has to go — otherwise the
        # "input beats a guess" rule in `tick()` would keep overwriting a fact.
        self.monitor_seeded = False
        self.last_event = f"{which}-display-{'on' if on else 'off'}"
        if on:
            self.resume = self.resume or f"{which}-display-on"

    def _enter_asleep(self, why: str) -> None:
        if not self.asleep:
            self.suspends += 1
            self._asleep_since = time.monotonic()
        self.asleep = True
        self.asleep_reason = why

    def _exit_asleep(self, why: str) -> None:
        was = self.asleep
        if was:
            self.resumes += 1
        self.asleep = False
        self.asleep_reason = ""
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
    def tick(self, dt: float) -> None:
        """Refresh derived state and raise the resume edge. Once per loop tick."""
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
        if self.asleep and self.idle_known and self.idle_s < 2.0:
            # Nobody types while the machine is suspended, so fresh input is proof it
            # is awake — the belt to the resume message's braces. Without it, a sleep
            # that was aborted after the suspend query (lid closed then reopened, a
            # sleep that failed to take) could leave the panel dark on a live desktop
            # with no event left to save it.
            self.last_event = "input after suspend"
            self._exit_asleep(self.resume or "input-after-suspend")
        if self.monitor_seeded and self.idle_known and self.idle_s < 2.0:
            # Fresh input means somebody is at the desk, and on Windows any input
            # brings the displays back — so a monitor state that was only *derived*
            # must never be what keeps the panel dark while the user is typing. An
            # answer that came from Windows (`monitor_seeded` False) is a fact and is
            # left alone; this only ever overrides a guess.
            self.monitor_on = True
            self.monitor_seeded = False
            self.last_event = "input-after-seed"
        if self._primed and gap > max(self.gap_s, 3.0 * max(dt, 0.1)):
            # Frozen: asleep, hibernating, or starved. Whichever it was, the panel
            # and the ETW child cannot be trusted until they have been re-made.
            self.last_event = f"gap {gap:.0f}s"
            why = (f"wake-after-{self.asleep_reason}" if self.asleep
                   else f"gap:{gap:.0f}s")
            self._exit_asleep(self.resume or why)
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
        if self.event_error:
            s += f" event-error={self.event_error}"
        if self.suspends or self.resumes:
            s += f" suspends={self.suspends} resumes={self.resumes}"
        return s

    def close(self) -> None:
        # Not supervising any more, and joined: this returns once the window is
        # destroyed and the notification handles are back, which is what a caller
        # shutting down (or a selftest measuring exactly-once) has to be able to ask.
        self._closed = True
        self._ev.close()
