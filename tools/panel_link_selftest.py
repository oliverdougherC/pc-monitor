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


def _usb_fixtures():
    """The fake device store from the USB-reset suite, shared rather than duplicated.

    Both suites need the same thing - a device store that answers the module's own
    marked queries - and #50's gate means the ladder can no longer be exercised
    without one. Importing the sibling keeps the two honest about the same rows.
    """
    import importlib.util
    p = Path(__file__).resolve().parent / "panel_usb_reset_selftest.py"
    spec = importlib.util.spec_from_file_location("panel_usb_fixtures", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def case_ladder() -> None:
    print("case: the device-recovery ladder steps up, one rung per attempt")
    # Reconciled during integration (#40 x #50): the rungs and the cadence are
    # unchanged, but every verb is now aimed through the identity gate - the
    # instance id has to be established as the panel before anything is touched -
    # and rung 3 is the journal-durable disable/enable rather than the old
    # unrecorded power cycle. So the case gives the link a fake device store and
    # an identity a HELLO would have confirmed. The claim under test is still the
    # order of the rungs; what it now also proves is that the ladder walks *while
    # properly aimed*, which is the only way it may walk at all.
    fix = _usb_fixtures()
    bus = fix.panel_bus()
    put = fix.Pnputil(bus)
    real_subprocess, real_state = panel_mod.subprocess, panel_mod.STATE_DIR
    real_settle = panel_mod._BUS_SETTLE_S
    panel_mod.subprocess = put
    panel_mod.STATE_DIR = Path(tempfile.mkdtemp())
    panel_mod._BUS_SETTLE_S = 0.0        # the settle is real-world timing, not a claim
    try:
        link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
        link.port = "COM7"
        link._bus = bus
        link._identity = panel_mod.PanelIdentity(fix.PANEL_IF, "COM7",
                                                 tuple(fix.PANEL_HW), True, True)
        waits = []
        for _ in range(3):
            waits.append(link._restart_wait())
            link._usb_restart()
        touched = fix.touched(put)
        check("rung 1: restart the CDC interface", touched[0],
              f"/restart-device {fix.PANEL_IF}")
        check("rung 2: restart the composite device", touched[1],
              f"/restart-device {fix.PANEL_PARENT}")
        check("rung 3: disable it", touched[2].split()[0], "/disable-device")
        check("rung 3: then enable it again", touched[3].split()[0], "/enable-device")
        check("rung 3 aimed at the composite device",
              touched[3].split()[1], fix.PANEL_PARENT)
        check("restarts counted", link.usb_restarts, 3)
        check("nothing is owed after a paid cycle", link._pending_recovery(), "")
        # The cadence is the reason escalation is worth having: the whole ladder walks a
        # minute apart, and only then does it slow down.
        check("the ladder walks quickly while it has rungs left", waits, [60.0, 60.0, 60.0])
        check("and settles down once it has walked", link._restart_wait(), 300.0)
        check("the panel answering resets the ladder", link.relink("recovered"), True)
        check("quick again for the next outage", link._restart_wait(), 60.0)
        check("depth reset too", link._restart_depth, 0)
        link.close()
    finally:
        panel_mod.subprocess = real_subprocess
        panel_mod.STATE_DIR = real_state
        panel_mod._BUS_SETTLE_S = real_settle


def case_disable_enable_safety() -> None:
    print("case: a device cycle never leaves the device disabled")
    # Reconciled during integration (#40): the marker file became a journaled
    # record in the per-user state directory (`STATE_DIR` is the seam),
    # `_power_cycle` became `_disable_and_enable`, and the start-up heal became
    # `_reconcile_recovery`. The same four claims, on the real machinery: a
    # refusal writes nothing, success writes then clears, a stuck enable leaves
    # the record, and the next bring-up pays the debt.
    real_state, real_settle = panel_mod.STATE_DIR, panel_mod._BUS_SETTLE_S
    panel_mod.STATE_DIR = Path(tempfile.mkdtemp())
    panel_mod._BUS_SETTLE_S = 0.0
    try:
        link = PanelLink(simu_cfg(), log=lambda m: print(f"    | {m}"))
        dev = "USB\\VID_1D6B&PID_0106&MI_00\\7&1a2b3c4d&0&0000"

        # 1. a disable that did not take must not be followed by an enable: that
        #    would "re-enable" a device we never stopped, and write a record about it.
        calls: list[str] = []
        link._pnputil = lambda verb, d: (calls.append(verb), (False, "Access is denied."))[1]
        link._device_state = lambda d: "on"       # the node says: never stopped
        ok, line = link._disable_and_enable(dev)
        check("refused disable fails honestly", ok, False)
        check("no enable attempted", calls, ["/disable-device"])
        check("the refusal is the message", "did not take effect" in line, True)
        check("no record written", link._pending_recovery(), "")

        # 2. the happy path writes the intent and removes it.
        calls.clear()
        link._pnputil = lambda verb, d: (calls.append(verb), (True, "ok"))[1]
        states = iter(["off", "on"])              # off after disable, on after enable
        link._device_state = lambda d: next(states)
        ok, _ = link._disable_and_enable(dev)
        check("disable then enable", ok, True)
        check("both verbs ran", calls, ["/disable-device", "/enable-device"])
        check("record cleaned up", link._pending_recovery(), "")

        # 3. the dangerous path: stopped it, cannot start it again. The record must
        #    survive so the next start finishes the job, and the log must be blunt.
        seen: list[str] = []
        link.log = lambda m: seen.append(m)
        calls.clear()
        link._pnputil = lambda verb, d: (
            calls.append(verb), (verb != "/enable-device", "stub"))[1]
        link._device_state = lambda d: "off"      # still disabled, whatever it says
        ok, line = link._disable_and_enable(dev)
        check("reported as failed", ok, False)
        check("enable retried", calls.count("/enable-device") >= 3, True)
        check("record left behind", link._pending_recovery(), dev)
        check("the log says how to undo it", any("COULD NOT CONFIRM" in m for m in seen),
              True)

        # 4. the next bring-up finds the record and finishes the job. The node still
        #    says disabled when reconcile first looks - that is the whole reason it
        #    asks - and says enabled once the enable has been issued.
        calls.clear()
        link.log = lambda m: seen.append(m)
        link._pnputil = lambda verb, d: (calls.append(verb), (True, "ok"))[1]
        states = iter(["off", "on"])
        link._device_state = lambda d: next(states)
        check("reconcile says the debt is paid", link._reconcile_recovery(), True)
        check("enabled on the next start", calls[-1], "/enable-device")
        check("record gone", link._pending_recovery(), "")
        check("and it said so", any("enabled again" in m.lower() or "re-enabled" in m.lower()
                                    for m in seen), True)
        link.close()
    finally:
        panel_mod.STATE_DIR = real_state
        panel_mod._BUS_SETTLE_S = real_settle


def case_exhausted_notice() -> None:
    print("case: when the ladder runs out, it says what to do")
    seen: list[str] = []
    link = PanelLink(simu_cfg(), log=seen.append)
    link.ok = False
    # An absolute instant, not the live monotonic clock. `_exhausted_notice` gates on
    # `now - self._last_giveup_log < _GIVEUP_LOG_S`, and `_last_giveup_log` starts at 0.0
    # — so with a *live* clock the case silently depends on how long the machine has been
    # up. It passed on a desk at ~1e6 seconds of uptime and failed on a freshly booted CI
    # runner, where the subtraction lands inside the window and the notice is correctly
    # suppressed. Pinning the instant removes the machine from the assertion.
    now = 1_000_000.0
    link._last_giveup_log = 0.0
    link._restart_attempts = 0
    link._restart_touched = 0
    link._exhausted_notice(now)
    check("silent while rungs remain", any("exhausted" in m for m in seen), False)
    # Reconciled during integration (#50): the notice counts verbs *actually
    # issued*, because a ladder refused at every rung has proved nothing about the
    # firmware and must not claim to have been walked. So the case issues rungs,
    # it does not merely ask for them.
    link._restart_attempts = 3
    link._restart_touched = 3
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
    # A ladder that was refused at every rung stays quiet about the firmware.
    link.ok = False
    link._restart_touched = 0
    link._last_giveup_log = 0.0
    link._exhausted_notice(now + 6000.0)
    check("a refused ladder never claims it was walked",
          sum("exhausted" in m for m in seen), 2)
    print(f"    {seen[-1][:110]}…")
    link.close()


def case_backoff_when_all_is_lost() -> None:
    print("case: a panel that is simply gone is retried politely, not flat out")
    seen: list[str] = []
    link = PanelLink(simu_cfg(), log=seen.append)

    def fail_build(first: bool = False, reason: str = "",
                   wait_s: float = 0.0) -> bool:
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
    # The first attempt runs on the background worker, so wait for it to hand the build
    # lease back before timing the next one: this case is about the retry clock, and a
    # tick that lands while a build is still in flight returns early without logging.
    deadline = time.monotonic() + 10.0
    while link.building and time.monotonic() < deadline:
        time.sleep(0.05)
    seen.clear()                        # the assertion below is about *this* tick's line
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
               case_relink_is_synchronous, case_ladder, case_disable_enable_safety,
               case_exhausted_notice, case_backoff_when_all_is_lost):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
