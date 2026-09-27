"""Watch the machine's own power/display/session state, live.

    .venv\\Scripts\\python tools\\hoststate_probe.py            # 2 Hz, prints changes
    .venv\\Scripts\\python tools\\hoststate_probe.py --trace     # every message too
    .venv\\Scripts\\python tools\\hoststate_probe.py --seconds 3600

This is the instrument for "the panel does not follow the PC to sleep and does
not come back". Sleep and monitor-off cannot be simulated honestly, so run this,
put the machine to sleep / lock it / let the displays time out, and read what it
printed when you get back:

    events live=True {'session-display': True, ...}   ← which notifications arrived
    asleep=True(query-suspend)  monitor=False            ← the pre-suspend query got here
    resume:0x12 (PBT_APMRESUMEAUTOMATIC)                 ← the wake, before the desktop
    gap 412s                                             ← or: no events, caught by time

`--trace` also dumps the raw WM_POWERBROADCAST/PBT codes and the GUID of any
power-setting notification. The three display settings are registered in precedence
order (session, console, legacy monitor power), so whichever of those lines appears
when the display times out is the one this build delivers, and the one `last_event`
is named for; a better-ranked one that is live wins over the ones below it.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")          # our tree first: vendor has its own main.py
from app import config as cfgmod  # noqa: E402
from app.hoststate import (NT, PBT_POWERSETTINGCHANGE, WM_DISPLAYCHANGE,  # noqa: E402
                           WM_POWERBROADCAST, WM_WTSSESSION_CHANGE, HostState,
                           console_session, logon_ui_running)

sys.stdout.reconfigure(errors="replace")
NAMES = {WM_POWERBROADCAST: "WM_POWERBROADCAST", WM_WTSSESSION_CHANGE: "WM_WTSSESSION_CHANGE",
         WM_DISPLAYCHANGE: "WM_DISPLAYCHANGE"}
PBTS = {0x0: "PBT_APMQUERYSUSPEND", 0x2: "PBT_APMQUERYSUSPENDFAILED", 0x4: "PBT_APMSUSPEND",
        0x6: "PBT_APMUSERSUSPEND/RESUMECRITICAL", 0x7: "PBT_APMRESUMESUSPEND",
        0x8: "PBT_APMUSERRESUME", 0xA: "PBT_APMRESUMEONBATTERY",
        0xB: "PBT_APMQUERYUSERSUSPEND", 0x12: "PBT_APMRESUMEAUTOMATIC",
        PBT_POWERSETTINGCHANGE: "PBT_POWERSETTINGCHANGE"}
WTSS = {0x1: "CONSOLE_CONNECT", 0x2: "CONSOLE_DISCONNECT", 0x3: "REMOTE_CONNECT",
        0x4: "REMOTE_DISCONNECT", 0x5: "SESSION_LOGON", 0x6: "SESSION_LOGOFF",
        0x7: "SESSION_LOCK", 0x8: "SESSION_UNLOCK", 0x9: "SESSION_REMOTE_CONTROL",
        0xA: "SESSION_CREATE(reserved)", 0xB: "SESSION_TERMINATE(reserved)",
        0xF: "SESSION_DESKTOP_READY"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=600.0)
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()

    cfg = cfgmod.load(None)
    # The probe drives its own cadence, so it declares it: the gap watchdog judges
    # a freeze against the tick interval it was given, not against the gap it found.
    hs = HostState(gap_s=float(cfg["display"].get("wake_gap_s", 5.0)),
                   cadence_s=1.0 / a.hz)
    print(f"python {'elevated' if _admin() else 'not elevated'}  "
          f"session={console_session()}  logonui={logon_ui_running()}")
    print(f"events live={hs.events_live} {hs.events} error={hs.event_error or '-'}")
    if not hs.events_live:
        print("  no event window: sleep/monitor following falls back to the gap "
              "watchdog + idle timeout only")
    print("tick  asleep monitor locked idle_s  last_event            resume")

    if a.trace:
        sink = hs._on_event

        def traced(msg, w, l):                     # noqa: E743
            name = NAMES.get(msg, f"0x{msg:x}")
            sub = PBTS.get(w, WTSS.get(w, hex(w))) if msg in NAMES else ""
            print(f"   msg {name} {sub} l={l if msg != PBT_POWERSETTINGCHANGE else ''}")
            sink(msg, w, l)

        hs._ev.sink = traced

    t_end = time.monotonic() + a.seconds
    dt = 1.0 / a.hz
    last: tuple | None = None
    n = 0
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        n += 1
        hs.tick(dt)
        key = (hs.asleep, hs.asleep_reason, hs.monitor_on, hs.locked, hs.console_lost,
               hs.resume, hs.last_event, int(hs.idle_s))
        if key != last:
            print(f"{n:5d}  {str(hs.asleep):5s} "
                  f"{str(hs.monitor_on):7s} {str(hs.locked):5s} {hs.idle_s:6.0f}  "
                  f"{hs.last_event:20s} {hs.resume or ''}")
            last = key
        if logon_ui_running() and not hs.locked:
            print(f"{n:5d}  note: LogonUI is running but no lock event arrived")
        r = hs.take_resume()
        if r:
            print(f"        → RESUME ({r}): reconnect the panel, restart the capture, "
                  f"full repaint")
        left = dt - (time.monotonic() - t0)
        if left > 0:
            time.sleep(left)
    # What a fresh start would have concluded before Windows said anything. Printed
    # last so it cannot be mistaken for something the run observed: if the ticks above
    # already show `monitor=False` from a real notification, this says so too.
    seed = hs.seed_monitor()
    print(f"seed: {seed}")
    hs.close()
    print(f"done: {hs.summary()}")
    return 0


def _admin() -> bool:
    if not NT:
        return False
    try:
        return bool(ctypes_admin())
    except Exception:  # noqa: BLE001
        return False


def ctypes_admin() -> bool:
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


if __name__ == "__main__":
    sys.exit(main())
