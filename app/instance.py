"""One main role per machine: the ownership lock that starts the app.

Two main-role processes were never supposed to exist at the same time: they
reclaim the same named ETW session (each believes it is the first, which is
exactly what `FrameMonitor._reclaim_session` is built for), and they open the
same COM port. The autostart task's `MultipleInstances IgnoreNew` only stops
*the task* from double-firing; a manual launch alongside the logon start, or a
second checkout, was never covered, and their independent retry loops kept
undoing each other's recovery.

The primitive is a named mutex, held for the lifetime of the process. The OS
closes handles when a process dies — killed, crashed, logged out — so the lock
cannot outlive its owner the way a pid file does: there is no stale lock to
notice, distrust, or delete, which is the acceptance line this exists to meet.
The name is machine-wide, so two working directories compete for the same lock
instead of each trusting a file inside its own tree.

Scope, explicitly: the physical panel and the main role's capture session are
one ownership — one device and one collector per machine. The claim is taken on
the session-local name *and*, where the OS permits creating global objects, on
the Global name too. Both, not Global-or-Local: if one process were granted the
privilege and another were not, each holding only its own scope would let both
starts through — the fallback has to be a superset of the hole it covers. The
session-local half alone still covers the shape that actually happened on this
desk (autostart at logon and a manual start share a session); the Global half
adds the different-session case.

The app never releases the lock on purpose: the process lifetime *is* the
claim, and releasing it early on an exit path only opens a window where a
duplicate slips in while the first is still closing the port.
"""
from __future__ import annotations

import ctypes
import os

MAIN_ROLE = "PCMonitor-main-role"
_ERROR_ALREADY_EXISTS = 183

# Acquired handles, by claim name, so the selftest can give a claim back.
# main.py never calls release(): its claim lasts as long as the process does.
_handles: dict[str, list[int]] = {}


def acquire_main_role(name: str = MAIN_ROLE) -> tuple[bool, str]:
    """Claim the main role. Returns (ok, what): a False `ok` means this process
    could not establish *exclusive* ownership, and `what` is the sentence to
    print with it. Never raises — the startup path this guards must not die on
    the guard itself.

    **An access failure is not proof that another owner is absent.** That is the
    whole point of the two verdicts below, and the bug this used to have:

      * `Fresh` (`GetLastError() == 0`) — the object did not exist, so *we*
        created it and it is ours. Only this is exclusive ownership.
      * `_ERROR_ALREADY_EXISTS` (183) — somebody else holds it. Refuse.
      * Anything else — `ERROR_ACCESS_DENIED` (5) is the one that happens: an
        elevated process can create `Global\\` while a non-elevated one cannot,
        and a process in another Windows session sees a different `Local\\`.
        The object's *existence* is unknown, so exclusivity cannot be
        established — and an unknown owner must not be treated as no owner.
        This used to `continue` and, when every scope refused, return True with
        `NOT LOCKED ... starting anyway`, which is exactly the fail-open hole:
        a second copy would then open the same COM port and reclaim the same ETW
        session as the copy it could not see.

    Failing closed here is not the same as refusing to run: the caller must not
    touch shared hardware, but it may keep trying (see `SafeStart` in main.py).
    A process that cannot prove it is the only owner has no business driving the
    desk's panel or the machine's capture session — "no telemetry" is a far
    better evening than "two owners fighting over one panel".
    """
    if os.name != "nt":
        # The lock is a Windows object and the app ships on Windows; the
        # offline logic is still importable elsewhere.
        return True, f"[{name}] no OS lock outside Windows"
    import ctypes.wintypes as wt

    k32 = ctypes.windll.kernel32
    # restype/argtypes are not decoration (see app/frames._kernel32): a 64-bit
    # handle truncated to 32 bits turns a successful acquire into a bogus one.
    k32.CreateMutexW.restype = wt.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, ctypes.c_wchar_p]
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    held: list[tuple[str, int]] = []     # (scope, handle) actually claimed here
    refused: list[str] = []              # scopes whose ownership is unknown
    dup_at: str | None = None
    for scope, prefixed in (("all sessions", f"Global\\{name}"),
                            ("this session", f"Local\\{name}")):
        # Reset first: GetLastError is only meaningful for the call that set it,
        # and a stale non-zero value would read a fresh create as "already there".
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, prefixed)
        err = ctypes.GetLastError()
        if not h:
            refused.append(f"{scope}: winerr={err}")
            continue
        if err == _ERROR_ALREADY_EXISTS:
            # A handle to somebody else's mutex is nothing to keep.
            k32.CloseHandle(h)
            dup_at = scope
            break
        if err != 0:
            # A handle, but not one we can call ours: treat it like a refusal
            # rather than assume exclusivity we did not establish.
            k32.CloseHandle(h)
            refused.append(f"{scope}: winerr={err} on create")
            continue
        held.append((scope, h))

    if dup_at is not None:
        for _scope, h in held:           # half a claim is no claim
            k32.CloseHandle(h)
        return False, (f"another {name} owner is already running (seen on the "
                       f"'{dup_at}' lock); this start will not touch the panel "
                       f"or the capture")
    if held:
        _handles[name] = [h for _scope, h in held]
        scopes = " + ".join(s for s, _h in held)
        note = f" ({'; '.join(refused)})" if refused else ""
        return True, f"owned ({scopes}){note}"
    # Every scope refused. We cannot tell whether an owner exists, so we do not
    # behave as if one does not: no lock means no device and no capture session.
    return False, (
        f"could not establish ownership of {name} ({'; '.join(refused)}) — an "
        f"access failure is not proof that another owner is absent, and the "
        f"panel and the ETW session may already be in use by a process this one "
        f"cannot see (a different elevation, or a different Windows session). "
        f"Not touching them; run this at the same elevation as the installed "
        f"task, or stop the other copy first")


def release(name: str = MAIN_ROLE) -> bool:
    """Give a claim back. Only the selftest has a reason to call this: the
    app's claim is its process lifetime, and the OS does the releasing."""
    handles = _handles.pop(name, None)
    if not handles or os.name != "nt":
        return False
    import ctypes.wintypes as wt

    k32 = ctypes.windll.kernel32
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]
    return all(bool(k32.CloseHandle(h)) for h in handles)
