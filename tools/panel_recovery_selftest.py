"""The USB recovery journal, proven against crashes instead of against good luck.

    .venv\Scripts\python tools\panel_recovery_selftest.py

`app/panel.py` disables a USB device in order to reset it, and a device left disabled
stays disabled through a reboot: what this defends against is not a dark panel for a
minute, it is a screen that is simply absent until a human opens Device Manager. The
only defence is a record that is on disk *before* the destructive call and is removed
only once the device node says the device is back - which is exactly the behaviour that
a happy-path test cannot show.

So every case here injures the process on purpose: a `pnputil` that answers in German
while the device is still off the bus, a state directory that cannot be written, and
real child processes that kill themselves with `os._exit` at each point of the state
machine (no finally, no atexit - nothing a power loss would have skipped either). What
is left on disk is then read back by a fresh link, from a different working directory,
the way the next start would read it.

The expectations are literals throughout - the phases, the device ids, the WQL
escaping, the verdict table - rather than constants imported from the module, so a
change to the module cannot quietly redefine what "correct" means here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="paneljournal-"))   # the old mark lived in the cwd
sys.path.insert(0, ROOT)                             # absolute: the cwd has moved
from app import panel as panel_mod                   # noqa: E402
from app.panel import PanelLink, state_dir           # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []

# Real shapes from this desk: the composite device inside the screen, and the CDC
# interface below it. Written out here rather than imported, so the test keeps its own
# idea of what an instance id looks like.
PANEL = "USB\\VID_1D6B&PID_0106\\20080411"
IFACE = "USB\\VID_1D6B&PID_0106&MI_00\\8&2222&0&0000"
JOURNAL = "usb_recovery.json"
GERMAN_OK = "Die Anfrage wurde erfolgreich bestaetigt."


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def read_journal(path: Path) -> dict:
    """What is on disk, parsed - or {} which every check then fails on."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_journal(path: Path, record: dict) -> None:
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


class Scratch:
    """A state directory and a link pointed at it, undone on the way out.

    The offline gate must never write into a real user profile, and each case needs its
    own journal so that one case's crash is not the next case's history.
    """

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="state-"))
        self.journal = self.dir / JOURNAL
        self.log: list[str] = []
        self._saved_dir = panel_mod.STATE_DIR
        self._saved_settle = panel_mod._BUS_SETTLE_S

    def __enter__(self) -> "Scratch":
        panel_mod.STATE_DIR = self.dir
        panel_mod._BUS_SETTLE_S = 0.0     # the waiting is not what is under test
        return self

    def __exit__(self, *exc) -> None:
        panel_mod.STATE_DIR = self._saved_dir
        panel_mod._BUS_SETTLE_S = self._saved_settle

    def link(self) -> PanelLink:
        link = PanelLink({"display": {"revision": "C", "com_port": "COM9",
                                      "portrait_width": 480, "portrait_height": 800,
                                      "orientation": "landscape"}}, log=self.log.append)
        link.port = "COM9"
        return link


class FakeBus:
    """pnputil and the device node, scripted, with every device verb recorded.

    `says` is what the node answers to successive state queries: `off` is disabled
    (Device Manager problem code 22), `on` is working properly. The wording is German
    on purpose - the module must not read anything into it.
    """

    def __init__(self, says=(), disable_ok: bool = True, enable_ok: bool = True) -> None:
        self.calls: list[str] = []
        self.state_calls: list[str] = []
        self.says = list(says)
        self.disable_ok = disable_ok
        self.enable_ok = enable_ok

    def pnputil(self, verb: str, dev: str):
        self.calls.append(f"{verb} {dev}")
        if verb == "/disable-device":
            return self.disable_ok, GERMAN_OK
        return self.enable_ok, GERMAN_OK

    def state(self, dev: str) -> str:
        self.state_calls.append(dev)
        return self.says.pop(0) if self.says else "on"


class FakeRun:
    """`subprocess.run` as seen from inside app.panel, so the verdict table is testable.

    Replacing the name on our module shadows it for app.panel only; no real process is
    started, which is the point - the answers a device node gives cannot be scripted
    against a live machine without disabling something.
    """

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.commands: list[str] = []

    def run(self, argv, **kw):
        self.commands.append(" ".join(argv))
        return SimpleNamespace(stdout=self.stdout, stderr="", returncode=0)


def bare_link() -> PanelLink:
    """A link that is only asked to read the bus, so it needs nowhere to write."""
    return PanelLink({"display": {"revision": "C", "com_port": "COM9",
                                  "portrait_width": 480, "portrait_height": 800}},
                     log=lambda m: None)


def case_intent_before_the_disable() -> None:
    print("case: the intent is on disk before the device is stopped, gone after it is back")
    with Scratch() as s:
        bus = FakeBus(says=["off", "on"])
        seen: list[str] = []

        def pnputil(verb, dev):
            if verb == "/disable-device":
                seen.append(read_journal(s.journal).get("phase", "no record"))
            return bus.pnputil(verb, dev)

        link = s.link()
        link._pnputil = pnputil
        link._device_state = bus.state
        ok, _ = link._disable_and_enable(PANEL)
        check("cycle reported successful", ok, True)
        check("the record said 'disabling' at the moment of the disable", seen, ["disabling"])
        check("both verbs, in that order", bus.calls,
              [f"/disable-device {PANEL}", f"/enable-device {PANEL}"])
        check("the record is gone once the node confirmed the device", s.journal.exists(),
              False)
        check("nothing is owed", link.recovery_pending, "")


def case_a_journal_that_cannot_be_written_refuses() -> None:
    print("case: no journal, no disable")
    with Scratch() as s:
        # The state directory cannot be made: the path is already a file. This is the
        # read-only-profile / disk-full / permission case without needing either.
        blocked = s.dir / "a-file"
        blocked.write_text("not a directory", encoding="utf-8")
        panel_mod.STATE_DIR = blocked
        bus = FakeBus(says=["off", "on"])
        link = s.link()
        link._pnputil = bus.pnputil
        link._device_state = bus.state
        ok, line = link._disable_and_enable(PANEL)
        check("refused", ok, False)
        check("no device verb was run at all", bus.calls, [])
        check("the node was never queried either", bus.state_calls, [])
        check("the reason names the journal", line.startswith("journal:"), True)
        check("the log says the destructive rung was skipped",
              any("NOT disabling" in m for m in s.log), True)


def case_wording_does_not_clear_the_record() -> None:
    print("case: success wording does not clear the record - the device node does")
    with Scratch() as s:
        bus = FakeBus(says=["off"] * 4)          # still disabled after every ask
        link = s.link()
        link._pnputil = bus.pnputil              # and every answer says "successful"
        link._device_state = bus.state
        ok, _ = link._disable_and_enable(PANEL)
        check("reported as not confirmed", ok, False)
        rec = read_journal(s.journal)
        check("the record survived", rec.get("device_id"), PANEL)
        check("and says the device is off the bus", rec.get("phase"), "disabled")
        check("the version is the one this test pins", rec.get("version"), 1)
        check("the app knows it owes this device", link.recovery_pending, PANEL)
        check("the summary carries it", f"recovery_pending={PANEL}" in link.summary(), True)
        check("the log says what to click", any("COULD NOT CONFIRM" in m for m in s.log),
              True)
        check("the enable was asked for more than once",
              bus.calls.count(f"/enable-device {PANEL}"), 3)

        # The next bring-up reads the node first: a device that came back on its own is
        # not asked to come back again.
        bus2 = FakeBus(says=["on"])
        link2 = s.link()
        link2._pnputil = bus2.pnputil
        link2._device_state = bus2.state
        check("the next process pays the debt", link2._reconcile_recovery(), True)
        check("without touching the device", bus2.calls, [])
        check("record gone", s.journal.exists(), False)

        # And when it really is still off the bus, the id in the record is the one enabled.
        write_journal(s.journal, {"version": 1, "phase": "disabled", "device_id": PANEL})
        bus3 = FakeBus(says=["off", "on"])
        link3 = s.link()
        link3._pnputil = bus3.pnputil
        link3._device_state = bus3.state
        check("a still-disabled device is enabled by the next process",
              link3._reconcile_recovery(), True)
        check("the enable went to the recorded device", bus3.calls,
              [f"/enable-device {PANEL}"])
        check("and only once per bring-up", bus3.calls.count(f"/enable-device {PANEL}"), 1)


def case_crash_at_every_transition() -> None:
    print("case: a process killed at any transition leaves a record the next one can use")
    elsewhere = tempfile.mkdtemp(prefix="cwd-")   # the app's cwd is not the journal's home
    child = Path(elsewhere) / "crash_probe.py"
    child.write_text(CHILD, encoding="utf-8")
    for point, want_phase in KILL_POINTS.items():
        with Scratch() as s:
            here = os.getcwd()
            os.chdir(elsewhere)                   # read the record back from elsewhere
            try:
                r = subprocess.run([sys.executable, str(child), ROOT, str(s.dir), point],
                                   cwd=elsewhere, capture_output=True, text=True,
                                   timeout=180)
                check(f"{point}: the process died where it was told", r.returncode, 7)
                rec = read_journal(s.journal)
                check(f"{point}: a record survived", rec.get("device_id"), PANEL)
                check(f"{point}: it says what was in progress", rec.get("phase"),
                      want_phase)
                check(f"{point}: the journal is not under the working directory",
                      str(s.journal).lower().startswith(elsewhere.lower()), False)
                bus = FakeBus(says=["off", "on"])
                link = s.link()
                link._pnputil = bus.pnputil
                link._device_state = bus.state
                check(f"{point}: the next process pays it off",
                      link._reconcile_recovery(), True)
                check(f"{point}: it enabled the recorded device", bus.calls,
                      [f"/enable-device {PANEL}"])
                check(f"{point}: nothing left behind", s.journal.exists(), False)
            finally:
                os.chdir(here)


def case_a_second_cycle_is_refused_while_one_is_owed() -> None:
    print("case: a device cycle is never stacked on top of an unfinished one")
    with Scratch() as s:
        write_journal(s.journal, {"version": 1, "phase": "disabled", "device_id": PANEL})
        bus = FakeBus(says=["off", "on"])
        link = s.link()
        link._pnputil = bus.pnputil
        link._device_state = bus.state
        ok, line = link._disable_and_enable(IFACE)
        check("refused", ok, False)
        check("no device verb was run", bus.calls, [])
        check("the refusal names the debt", PANEL in line, True)


def case_an_unreadable_record_is_kept_aside() -> None:
    print("case: a record that cannot be read is kept, not deleted")
    with Scratch() as s:
        s.journal.write_text("{ not json", encoding="utf-8")
        bus = FakeBus()
        link = s.link()
        link._pnputil = bus.pnputil
        link._device_state = bus.state
        check("an unreadable record is not 'nothing owed'", link._reconcile_recovery(),
              False)
        aside = s.dir / (JOURNAL + ".unreadable")
        check("its bytes are kept", aside.read_text(encoding="utf-8"), "{ not json")
        check("nothing was enabled on a guess", bus.calls, [])
        check("the log names the manual step", any("Device Manager" in m for m in s.log),
              True)
        check("and the debt shows in the summary", "journal=" in link.summary(), True)

        # A record written by a newer version of the app is the same shape of problem.
        write_journal(s.journal, {"version": 2, "phase": "disabled", "device_id": PANEL})
        link2 = s.link()
        link2._pnputil = bus.pnputil
        link2._device_state = bus.state
        check("a newer version's record is not acted on", link2._reconcile_recovery(),
              False)
        check("and it is kept too", (s.dir / (JOURNAL + ".unreadable")).exists(), True)


def case_the_old_mark_is_adopted() -> None:
    print("case: the journal's old working-directory home is adopted, not dropped")
    with Scratch() as s:
        old = Path(".panel_reset_pending")        # where a pre-fix run left it
        old.write_text(PANEL + "\n", encoding="utf-8")
        bus = FakeBus(says=["off", "on"])
        link = s.link()
        link._pnputil = bus.pnputil
        link._device_state = bus.state
        check("the debt was found", link._reconcile_recovery(), True)
        check("it was carried into the state directory", any("adopted" in m for m in s.log),
              True)
        check("the enable went to the device it named", bus.calls,
              [f"/enable-device {PANEL}"])
        check("the old file is gone from the working directory", old.exists(), False)

        # Junk under the old name is not deleted either: it is the only trace there is.
        old.write_text("USB Serial Device (COM9)\n", encoding="utf-8")
        link2 = s.link()
        link2._pnputil = bus.pnputil
        link2._device_state = bus.state
        link2._reconcile_recovery()
        check("an unreadable old mark keeps its bytes",
              old.with_name(old.name + ".unreadable").exists(), True)
        check("and stops being re-read", old.exists(), False)


def case_the_state_verdicts() -> None:
    print("case: the answers a device node can give, and only those")
    real = panel_mod.subprocess
    try:
        for stdout, want in (
                ("PANELSTATE True 0", "on"),
                ("PANELSTATE True 22", "off"),
                ("PANELSTATE True 45", "degraded"),
                ("PANELSTATE False 0", "degraded"),
                ("PANELSTATE absent", "absent"),
                ("PANELSTATE ambiguous", "unknown"),
                ("Der Befehl wurde erfolgreich ausgefuehrt.", "unknown"),
                ("", "unknown")):
            panel_mod.subprocess = FakeRun(stdout)
            got = bare_link()._device_state(PANEL)
            check(f"{stdout!r} reads as {want}", got, want)

        fake = FakeRun("PANELSTATE True 0")
        panel_mod.subprocess = fake
        bare_link()._device_state(PANEL)
        cmd = fake.commands[0]
        check("one query", len(fake.commands), 1)
        check("it filters on an exact DeviceID", "Win32_PnPEntity -Filter" in cmd, True)
        check("the id is WQL-escaped", "USB\\\\VID_1D6B&PID_0106\\\\20080411" in cmd, True)
        check("it does not search by display name", "Name LIKE" in cmd, False)
        check("it does not take the first of several", "First 1" in cmd, False)
    finally:
        panel_mod.subprocess = real


def case_only_a_real_instance_id_is_touched() -> None:
    print("case: a record that is not an instance id never reaches the device")
    with Scratch() as s:
        link = s.link()
        bus = FakeBus(says=["off", "on"])
        link._pnputil = bus.pnputil
        link._device_state = bus.state
        for bad in ("", "USB Serial Device", "USB\\X;del", 'USB\\X"Y', "USB\\X\nY",
                    "USB\\X`whoami"):
            ok, line = link._disable_and_enable(bad)
            check(f"{bad!r} is refused", ok, False)
            check(f"{bad!r} is not usable", panel_mod._is_instance_id(bad), False)
        check("no device verb was run", bus.calls, [])
        check("no record was written", s.journal.exists(), False)
        check("a real interface id is usable", panel_mod._is_instance_id(IFACE), True)
        check("the state of an unusable id is unknown, not 'enabled'",
              bare_link()._device_state("USB Serial Device"), "unknown")
        check("the escaping for WQL doubles backslashes",
              panel_mod._wql_id("USB\\VID_1D6B&PID_0106\\20080411"),
              "USB\\\\VID_1D6B&PID_0106\\\\20080411")
        check("and a quote becomes four through two parsers",
              panel_mod._wql_id("USB\\A'B\\C"), "USB\\\\A''''B\\\\C")
        check("the state directory is absolute", state_dir().is_absolute(), True)


# What the journal has to say when the process dies at each point of the machine. The
# record is always there, always parseable, and always in a phase a later process can
# act on - which is the whole claim the fix makes.
KILL_POINTS = {
    "before-disable": "disabling",
    "after-disable": "disabling",
    "before-phase-disabled": "disabling",
    "after-phase-disabled": "disabled",
    "before-phase-enabling": "disabled",
    "after-phase-enabling": "enabling",
    "before-enable": "enabling",
    "after-enable": "enabling",
}

CHILD = r'''
"""One panel recovery, killed at a chosen point. Written by panel_recovery_selftest.py.

`os._exit` is the whole point: no finally runs, no atexit, no exception handler - so
what is on disk afterwards is what a power loss would have left behind.
"""
import os
import sys

ROOT, STATE, POINT = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, ROOT)
from app import panel as panel_mod

panel_mod.STATE_DIR = STATE
panel_mod._BUS_SETTLE_S = 0.0        # the waiting is not what is under test

DEV = "USB\\VID_1D6B&PID_0106\\20080411"

link = panel_mod.PanelLink({"display": {"revision": "C", "com_port": "COM9",
                                        "portrait_width": 480,
                                        "portrait_height": 800}}, log=lambda m: None)
link.port = "COM9"


def die(where):
    if POINT == where:
        os._exit(7)


def fake_pnputil(verb, dev):
    if verb == "/disable-device":
        die("before-disable")
    else:
        die("before-enable")
    if verb == "/disable-device":
        die("after-disable")
    else:
        die("after-enable")
    return True, "Die Anfrage wurde erfolgreich bestaetigt."


SAYS = ["off", "on", "on", "on"]


def fake_state(dev):
    return SAYS.pop(0) if SAYS else "on"


record_phase = link._record_phase


def fake_record_phase(dev, phase):
    die("before-phase-" + phase)
    reason = record_phase(dev, phase)
    die("after-phase-" + phase)
    return reason


link._pnputil = fake_pnputil
link._device_state = fake_state
link._record_phase = fake_record_phase
link._disable_and_enable(DEV)
'''


def main() -> int:
    for fn in (case_intent_before_the_disable,
               case_a_journal_that_cannot_be_written_refuses,
               case_wording_does_not_clear_the_record,
               case_crash_at_every_transition,
               case_a_second_cycle_is_refused_while_one_is_owed,
               case_an_unreadable_record_is_kept_aside,
               case_the_old_mark_is_adopted,
               case_the_state_verdicts,
               case_only_a_real_instance_id_is_touched):
        fn()
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())