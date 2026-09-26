"""Exercise the panel link without a panel — the simulated device, plus fake ones.

    .venv\\Scripts\\python tools\\panel_link_selftest.py

`app/panel.py` is the module that decides whether the user sees anything at all, and
its whole job is what happens when the device is *wrong*: it must survive a raise, a
`SystemExit`, a write that never returns, and it must come back by itself. Those
paths cannot be tested against a healthy panel, and testing them against a sick real
one means waiting for it to get sick.

So: the vendored simulated driver for the happy path (bring-up, orientation,
brightness, screen on/off, a real image through `DiffPusher`), and deliberately bad
device objects for the rest.

Runs in a temporary directory: the simulated driver writes `screencap.png` into the
current working directory, and the vendor tree must stay clean.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="panellink-"))     # before vendor imports
sys.path.insert(0, ROOT)                            # absolute: the cwd has moved
from app import config as cfgmod          # noqa: E402
from app import panel as panel_mod        # noqa: E402
from app.output import DiffPusher         # noqa: E402
from app.panel import PanelLink           # noqa: E402

sys.stdout.reconfigure(errors="replace")
from PIL import Image                     # noqa: E402

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def simu_cfg() -> dict:
    c = cfgmod.load(None)
    c["display"] = dict(c["display"])
    c["display"]["revision"] = "SIMU"
    c["display"]["orientation"] = "landscape"
    return c


class Deaf:
    """Answers nothing, like a panel whose endpoint stopped draining mid-frame."""

    def __init__(self) -> None:
        self.raises = 0

    def DisplayPILImage(self, *a, **k) -> None:  # noqa: N802
        self.raises += 1
        raise RuntimeError("endpoint gone")

    SetBrightness = ScreenOn = ScreenOff = closeSerial = DisplayPILImage
    SetOrientation = DisplayPILImage

    def get_width(self) -> int:     # noqa: N802
        return 800

    def get_height(self) -> int:     # noqa: N802
        return 480


class Wedged:
    """Never returns: the write that blocks in WriteFile forever."""

    def DisplayPILImage(self, *a, **k) -> None:  # noqa: N802
        time.sleep(30)

    SetBrightness = ScreenOn = ScreenOff = closeSerial = DisplayPILImage


def case_happy() -> None:
    print("case: the simulated panel comes up and takes frames")
    link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
    check("open()", link.open(), True)
    check("width is landscape", link.get_width(), 800)
    check("height is landscape", link.get_height(), 480)
    img = Image.new("RGB", (800, 480), (12, 34, 56))
    check("push whole frame", link.push(img), True)
    check("push band", link.push(img.crop((0, 0, 800, 40)), 0, 440), True)
    check("brightness", link.set_brightness(45), True)
    check("screen off", link.screen(False), True)
    check("screen on", link.screen(True), True)
    # DiffPusher is what main.py actually drives: it must accept this object in
    # place of the raw driver and still do its band diffing.
    pusher = DiffPusher(link)
    pusher.push(img)
    pusher.push(img)                  # identical: diff says nothing to send
    pusher.invalidate()
    pusher.push(img.rotate(0))
    check("diff pusher survived", link.ok, True)
    print(f"    {link.summary()}")
    link.close()
    check("closed", link.ok, False)


def case_deaf_recovers() -> None:
    print("case: the device starts raising — link down, then rebuilt on its own")
    link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
    check("open()", link.open(), True)
    link.lcd = Deaf()
    img = Image.new("RGB", (800, 480), (1, 2, 3))
    check("push reports failure", link.push(img), False)
    check("link marked down", link.ok, False)
    check("reason recorded", "RuntimeError" in link.down_reason, True)
    # Every later call short-circuits: no second attempt against a dead handle while
    # the loop keeps running.
    n = link.lcd.raises
    check("push short-circuits", link.push(img), False)
    check("device not touched again", link.lcd.raises, n)
    check("brightness short-circuits", link.set_brightness(10), False)
    check("screen short-circuits", link.screen(True), False)

    # tick() must not block on the rebuild — a bring-up against a dead device can
    # take tens of seconds, and the 1 Hz loop cannot wait for it.
    t0 = time.monotonic()
    link.tick()
    took = time.monotonic() - t0
    check("tick returned immediately", took < 0.5, True)
    deadline = time.monotonic() + 20.0
    while not link.ok and time.monotonic() < deadline:
        time.sleep(0.2)
        link.tick()
    check("background rebuild brought it back", link.ok, True)
    check("full repaint demanded", link.needs_full, True)
    check("push works again", link.push(img), True)
    print(f"    {link.summary()}")
    link.close()


def case_wedged_is_bounded() -> None:
    print("case: a write that never returns is given up on, quickly")
    link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
    check("open()", link.open(), True)
    link.lcd = Wedged()
    t0 = time.monotonic()
    ok = link._call("wedged write", lambda: link.lcd.DisplayPILImage(None, 0, 0),
                    giveup=0.5)
    took = time.monotonic() - t0
    check("call reported failure", ok, False)
    check("bounded (did not wait for the write)", took < 2.0, True)
    check("link down with the reason", "blocked" in link.down_reason, True)
    check("slow-write counter", link.slow_writes >= 1, True)
    # The blocked thread is still out there holding the device; the link must not
    # hand the same handle to a second call.
    check("later calls short-circuit", link.push(Image.new("RGB", (8, 8))), False)
    link.close()


def case_relink_is_synchronous() -> None:
    print("case: relink (the resume path) answers synchronously")
    link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
    check("open()", link.open(), True)
    invocations: list[int] = []
    check("relink ok", link.relink("test-resume"), True)
    link._on_relink = lambda: invocations.append(1)
    check("relink again", link.relink("test-resume-2"), True)
    check("relink callback fired", len(invocations), 1)
    check("still up", link.ok, True)
    check("rebuilds counted", link.rebuilds >= 2, True)
    print(f"    {link.summary()}")
    link.close()


def case_ladder() -> None:
    print("case: the device-recovery ladder steps up, one rung per attempt")
    link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
    calls: list[str] = []
    link.port = "COM9"
    # The two things `_usb_restart` does to the outside world, replaced: which device it is
    # allowed to aim at, and what the device verbs return. Everything else is the real
    # ladder logic — and the order of the rungs is the whole point of it. (What makes a
    # target *verified* is `tools/panel_usb_identity_selftest.py`'s subject.)
    link._identity = panel_mod.PanelIdentity(
        "USB\\VID_1D6B&PID_0106&MI_00\\8&1", "COM9",
        ("USB\\VID_1D6B&PID_0106&MI_00",), expected=True, confirmed=True)
    link._verified_target = lambda parent=False: (
        ("USB\\VID_1D6B&PID_0106\\20080411", "") if parent
        else ("USB\\VID_1D6B&PID_0106&MI_00\\8&1", ""))
    link._pnputil = lambda verb, dev: (calls.append(f"{verb} {dev}"), (True, "stubbed"))[1]
    waits = []
    for _ in range(3):
        waits.append(link._restart_wait())
        link._usb_restart()
    check("rung 1: restart the CDC interface", calls[0],
          "/restart-device USB\\VID_1D6B&PID_0106&MI_00\\8&1")
    check("rung 2: restart the composite device", calls[1],
          "/restart-device USB\\VID_1D6B&PID_0106\\20080411")
    check("rung 3: disable it", calls[2].split()[0], "/disable-device")
    check("rung 3: then enable it again", calls[3].split()[0], "/enable-device")
    check("rung 3 aimed at the composite device",
          calls[3].split()[1], "USB\\VID_1D6B&PID_0106\\20080411")
    check("restarts counted", link.usb_restarts, 3)
    # The cadence is the reason escalation is worth having: the whole ladder walks a
    # minute apart, and only then does it slow down.
    check("the ladder walks quickly while it has rungs left", waits, [60.0, 60.0, 60.0])
    check("and settles down once it has walked", link._restart_wait(), 300.0)
    check("the panel answering resets the ladder", link.relink("recovered"), True)
    check("quick again for the next outage", link._restart_wait(), 60.0)
    check("depth reset too", link._restart_depth, 0)
    link.close()


def case_power_cycle_safety() -> None:
    print("case: a power cycle never leaves the device disabled")
    real_mark = panel_mod._MARK_FILE
    tmp = Path(tempfile.mkdtemp()) / "marker"
    panel_mod._MARK_FILE = tmp
    try:
        link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))

        # 1. a disable that is refused must not be followed by an enable: that would
        #    "re-enable" a device we never stopped, and write a marker about it.
        calls: list[str] = []
        link._pnputil = lambda verb, dev: (calls.append(verb), (False, "Access is denied."))[1]
        ok, line = link._power_cycle("USB\\X")
        check("refused disable fails honestly", ok, False)
        check("no enable attempted", calls, ["/disable-device"])
        check("the refusal is the message", "denied" in line.lower(), True)
        check("no marker written", tmp.exists(), False)

        # 2. the happy path writes the intent and removes it.
        link._pnputil = lambda verb, dev: (calls.append(verb), (True, "ok"))[1]
        ok, _ = link._power_cycle("USB\\X")
        check("disable then enable", ok, True)
        check("both verbs ran", calls[1:], ["/disable-device", "/enable-device"])
        check("marker cleaned up", tmp.exists(), False)

        # 3. the dangerous path: stopped it, cannot start it again. The marker must
        #    survive so the next start finishes the job, and the log must be blunt.
        seen: list[str] = []
        link.log = lambda m: seen.append(m)
        link._pnputil = lambda verb, dev: (
            calls.append(verb), (verb != "/enable-device", "stub"))[1]
        ok, line = link._power_cycle("USB\\X")
        check("reported as failed", ok, False)
        check("enable retried", calls.count("/enable-device") >= 3, True)
        check("marker left behind", tmp.exists(), True)
        check("the log says how to undo it", any("COULD NOT RE-ENABLE" in m for m in seen),
              True)

        # 4. the next start-up finds the marker and finishes the job.
        link.log = lambda m: seen.append(m)
        link._pnputil = lambda verb, dev: (calls.append(verb), (True, "ok"))[1]
        link._heal_disabled()
        check("enabled on the next start", calls[-1], "/enable-device")
        check("marker gone", tmp.exists(), False)
        check("and it said so", any("re-enabled" in m for m in seen), True)
        link.close()
    finally:
        panel_mod._MARK_FILE = real_mark


def case_exhausted_notice() -> None:
    print("case: when the ladder runs out, it says what to do")
    seen: list[str] = []
    link = PanelLink(simu_cfg(), log=seen.append)
    link.ok = False
    now = time.monotonic()
    link._restart_attempts = 0
    link._exhausted_notice(now)
    check("silent while rungs remain", any("exhausted" in m for m in seen), False)
    link._restart_attempts = 3
    link._exhausted_notice(now)
    check("a ladder that was never allowed to step claims no verdict",
          sum("exhausted" in m for m in seen), 0)
    link._restart_touched = 3          # three device verbs actually issued
    link._exhausted_notice(now)
    check("says it once the ladder is out", sum("exhausted" in m for m in seen), 1)
    check("and names the action", "unplug" in seen[-1], True)
    link._exhausted_notice(now + 1.0)
    check("does not repeat every tick", sum("exhausted" in m for m in seen), 1)
    link._exhausted_notice(now + 1000.0)
    check("repeats only after the window", sum("exhausted" in m for m in seen), 2)
    link.ok = True
    link._exhausted_notice(now + 5000.0)
    check("never while the link is up", sum("exhausted" in m for m in seen), 2)
    print(f"    {seen[-1][:110]}…")
    link.close()


def case_backoff_when_all_is_lost() -> None:
    print("case: a panel that is simply gone is retried politely, not flat out")
    seen: list[str] = []
    link = PanelLink(simu_cfg(), log=seen.append)

    def fail_build(first: bool = False, reason: str = "") -> bool:
        link.down_reason = "no port present"
        return False

    link._build_now = fail_build        # the port is not there; no vendor bring-up
    check("quick while there are rungs left", link._retry_interval(), 10.0)
    link.tick()
    gap = link._retry_at - time.monotonic()
    check("first attempt schedules the next in ~10s", 5.0 < gap <= 12.0, True)

    link._restart_attempts = 3          # every rung of the ladder has now been spent
    link._retry_at = 0.0
    link._last_down_log = 0.0
    link.tick()
    gap = link._retry_at - time.monotonic()
    check("attempts slow down after the ladder", 50.0 < gap <= 62.0, True)
    check("the log quotes the real number", any("retrying every 60s" in m for m in seen),
          True)
    # The wait exists to stop churn, not to miss a replug: an event cuts through it.
    link._retry_at = time.monotonic() + 300
    check("relink answers even mid-backoff", link.relink("replugged"), False)
    check("and clears the wait", link._retry_at <= time.monotonic(), True)
    print(f"    {link.summary()}")
    link.close()


def main() -> int:
    for fn in (case_happy, case_deaf_recovers, case_wedged_is_bounded,
               case_relink_is_synchronous, case_ladder, case_power_cycle_safety,
               case_exhausted_notice, case_backoff_when_all_is_lost):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
