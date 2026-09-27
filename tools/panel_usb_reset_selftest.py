"""Which device a destructive recovery is allowed to touch, and which is not.

    .venv\\Scripts\\python tools\\panel_usb_reset_selftest.py

Escalation used to find the panel by *name*: `Name LIKE '%(COM7)%'` with
`Select-Object -First 1`, and then the parent by trimming `&MI_00` off the
interface id and LIKE-matching the VID/PID prefix that was left. Windows reissues
COM numbers whenever it likes, a second screen of the same model differs only by
its instance id, and the thing above a wake-up face on this desk is a hub the
rest of the machine shares. An elevated `pnputil /disable-device` aimed at a
guess is an outage on somebody else's device, so every case here asserts one of
two things: the verb went to the verified instance id, or **no verb was issued
at all**. Nothing in this file runs pnputil or touches a device.

pnputil also exits 0 when it refuses, and its wording is localized, so the
verdict here is the device node's state afterwards - never what the command
said in English.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import panel as panel_mod    # noqa: E402

sys.stdout.reconfigure(errors="replace")

# Where the journal goes when this suite walks the strong rung: a temp directory,
# created once. See `link()`. And the bus settle is real-world re-enumeration
# timing, not a claim any case here makes, so it does not get slept through.
import tempfile                                        # noqa: E402

_STATE_DIR = Path(tempfile.mkdtemp(prefix="pcmon-usbreset-state-"))
panel_mod.STATE_DIR = _STATE_DIR
panel_mod._BUS_SETTLE_S = 0.0

fails: list[str] = []

# Windows-side literals, written out as the device store reports them rather than
# imported from app.panel: if the module's expectations drift, this must disagree.
PANEL_IF = "USB\\VID_1D6B&PID_0106&MI_00\\7&1a2b3c4d&0&0000"
OTHER_IF = "USB\\VID_1D6B&PID_0106&MI_00\\7&9z8y7x6w&0&0000"
PANEL_PARENT = "USB\\VID_1D6B&PID_0106\\7&1a2b3c4d&0"
HUB = "USB\\VID_1A40&PID_0101\\6&23491980&0&2"
PANEL_HW = ("USB\\VID_1D6B&PID_0106&MI_00",)
PARENT_HW = ("USB\\VID_1D6B&PID_0106",)
HUB_HW = ("USB\\VID_1A40&PID_0101",)
COMPOSITE = ("USB\\COMPOSITE", "USB\\CLASS_USB")
# A CDC serial interface as the store really reports it: `CompatibleID` is never
# empty on a PnP node, and the module reads an answer whose shape it does not
# recognise as "I cannot tell", which is the safe direction.
IF_COMPAT = ("USB\\Class_02&SubClass_02&Prot_01",)
HUB_COMPAT = ("USB\\CLASS_HUB", "USB\\COMMON_CLASS")


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def _unquote(escaped: str) -> str:
    """Undo `_ps_id`'s single-quote doubling, so a query names the row it asked for."""
    return escaped.replace("''", "'")


class Node:
    def __init__(self, dev, name, hw, compat, present=True, code=0, pnpclass="Ports"):
        self.dev, self.name, self.hw, self.compat = dev, name, list(hw), list(compat)
        self.present, self.code, self.pnpclass = present, code, pnpclass


class Bus:
    """A fake device store that answers the module's own marked queries.

    It answers *queries*, not method calls: the topology is written as rows, and the
    module's script text is parsed for the id or port it is asking about. That keeps
    the test honest about the thing that is actually under test - which rows a
    wildcard search would have matched.
    """

    def __init__(self, nodes, parents=None, children=None):
        self.nodes = {n.dev: n for n in nodes}
        self.parents = parents or {}
        self.children = children or {}
        self.queries: list[str] = []

    def __call__(self, script: str) -> str:
        self.queries.append(script)
        if "DEVPKEY_Device_Parent" in script:
            m = re.search(r"InstanceId '([^']+)'", script)
            got = self.parents.get(_unquote(m.group(1)), "") if m else ""
            return f"PANELPARENT|{got}\n"
        if "DEVPKEY_Device_Children" in script:
            m = re.search(r"InstanceId '([^']+)'", script)
            kids = self.children.get(_unquote(m.group(1)), []) if m else []
            return f"PANELCHILD|{';'.join(kids)}\n"
        if "DeviceID=" in script:
            hit = [n for n in self.nodes.values() if panel_mod._wql_id(n.dev) in script]
            return self._node_line(hit)
        m = re.search(r'Name LIKE "%\((COM\d+)\)%"', script)
        port = m.group(1) if m else ""
        hit = [n for n in self.nodes.values() if f"({port})" in n.name]
        return self._node_line(hit)

    def _node_line(self, hit) -> str:
        if len(hit) != 1:
            return f"PANELNODE|count={len(hit)}\n"
        n = hit[0]
        return ("PANELNODE|1|{}|{}|{}|{}|{}|{}|{}\n".format(
            n.present, n.code, n.name, n.pnpclass, ";".join(n.hw), ";".join(n.compat),
            n.dev))


class Pnputil:
    """`pnputil` as this desk behaves: exit 0 whatever happens, wording localized."""

    def __init__(self, bus, text="Der Vorgang wurde erfolgreich beendet.", refuse=False):
        self.bus, self.text, self.refuse = bus, text, refuse
        self.calls: list[tuple[str, str]] = []

    def run(self, argv, **_kw):
        verb, dev = argv[1], argv[2]
        self.calls.append((verb, dev))
        n = self.bus.nodes.get(dev)

        class R:
            returncode, stdout, stderr = 0, "", ""

        r = R()
        r.stdout = self.text
        if n is not None and not self.refuse:     # a real verb changes the node; a
            if verb == "/disable-device":         # refusal changes nothing, and still
                n.code = 22                       # exits 0 on this Windows
            elif verb in ("/enable-device", "/restart-device"):
                n.code, n.present = 0, True
        return r


def link(bus: Bus, pnputil: Pnputil, rev: str = "C", port: str = "COM7"):
    """A link whose device store, command runner and state directory are all fake.

    The state directory is redirected for every link this suite makes because the
    strongest rung of the ladder is now the *journal-durable* disable/enable
    (#40): a test that walked the ladder would otherwise write a real recovery
    record into whoever's `%LOCALAPPDATA%` - and a record that says "a device is
    sitting disabled" is exactly the file no offline run may leave behind.
    """
    real_subprocess = panel_mod.subprocess
    lk = panel_mod.PanelLink({"display": {"revision": rev, "com_port": port,
                                          "width": 800, "height": 480}}, log=lambda m: None)
    lk.port = port
    lk._bus = bus                                  # every device query is answered here
    panel_mod.subprocess = pnputil                 # ... and no verb can reach the machine
    panel_mod.STATE_DIR = _STATE_DIR               # ... and no journal can reach the profile
    lk._close_real_subprocess = lambda: setattr(panel_mod, "subprocess", real_subprocess)
    return lk


def panel_bus(with_other: bool = False, parent: bool = True, hub: bool = False,
              kids: bool = True, name: str = "USB Serial Device (COM7)") -> Bus:
    nodes = [Node(PANEL_IF, name, PANEL_HW, IF_COMPAT)]
    parents, children = {}, {}
    if with_other:
        nodes.append(Node(OTHER_IF, "USB Serial Device (COM7)", PANEL_HW, IF_COMPAT))
    if parent:
        top = HUB if hub else PANEL_PARENT
        nodes.append(Node(top, "USB Root Hub (USB 3.0)" if hub else "USB Composite Device",
                          HUB_HW if hub else PARENT_HW, HUB_COMPAT if hub else COMPOSITE))
        parents[PANEL_IF] = top
        if kids:
            children[top] = [PANEL_IF]
    return Bus(nodes, parents, children)


def touched(p: Pnputil) -> list[str]:
    return [f"{v} {d}" for v, d in p.calls]


def case_two_screens_one_port() -> None:
    print("case: two screens of the same model, one COM number - nobody gets touched")
    bus, p = panel_bus(with_other=True), None
    lk = link(bus, (p := Pnputil(bus)))
    lk._capture_identity()
    check("identity is not captured from an ambiguous port", lk._identity, None)
    check("and the reason says two nodes answered", "2 device nodes" in lk._identity_error, True)
    check("_usb_restart refuses", lk._usb_restart(), False)
    check("no device verb was issued", touched(p), [])
    lk._close_real_subprocess()


def case_hello_or_hardware_ids() -> None:
    print("case: an interface that never answered HELLO is not established as the panel")
    bus = Bus([Node(PANEL_IF, "USB Serial Device (COM7)",
                    ("USB\\VID_0A12&PID_0001&MI_00",), IF_COMPAT)])   # a Bluetooth dongle's ids
    p = Pnputil(bus)
    lk = link(bus, p)
    lk._capture_identity(confirmed=False)
    check("identity was recorded", lk._identity is not None, True)
    check("but its hardware ids are not a screen this build knows",
          lk._identity.expected, False)
    check("so it may not be reset", lk._identity.may_reset(), False)
    check("_usb_restart refuses", lk._usb_restart(), False)
    check("no device verb was issued", touched(p), [])
    # The same device, once it has answered HELLO down the port, is the panel.
    lk._identity.confirmed = True
    check("and HELLO down that port is what establishes it", lk._identity.may_reset(), True)
    lk._close_real_subprocess()


def case_com_reassigned() -> None:
    print("case: the COM number moved to another device while we were not looking")
    bus = panel_bus()
    p = Pnputil(bus)
    lk = link(bus, p)
    lk._capture_identity(confirmed=True)
    check("panel identified on COM7", lk._identity.interface_id, PANEL_IF)
    # Windows hands the number out again: the panel's node now reads COM9, and a
    # different device answers to COM7.
    bus.nodes[PANEL_IF].name = "USB Serial Device (COM9)"
    bus.nodes[OTHER_IF] = Node(OTHER_IF, "USB Serial Device (COM7)", PANEL_HW, IF_COMPAT)
    check("_usb_restart refuses", lk._usb_restart(), False)
    check("no device verb was issued", touched(p), [])
    check("the refusal names the port that moved", "no longer claims COM7" in
          lk.usb_restart_refused, True)
    lk._close_real_subprocess()


def case_interface_gone() -> None:
    print("case: the interface vanished across a resume")
    bus = panel_bus()
    p = Pnputil(bus)
    lk = link(bus, p)
    lk._capture_identity(confirmed=True)
    del bus.nodes[PANEL_IF]
    check("_usb_restart refuses", lk._usb_restart(), False)
    check("no device verb was issued", touched(p), [])
    check("the refusal says it is gone", "not in the device store" in lk.usb_restart_refused,
          True)
    lk._close_real_subprocess()


def case_shared_hub() -> None:
    print("case: the device above the panel is a hub the rest of the desk shares")
    bus = panel_bus(hub=True)
    p = Pnputil(bus)
    lk = link(bus, p)
    lk._capture_identity(confirmed=True)
    target, why = lk._verified_target(parent=True)
    check("the hub is refused as a target", target, "")
    check("and the reason says other devices hang off it",
          "not the screen's own composite device" in why, True)
    lk._restart_depth = 1                      # the rung that wants the parent
    check("_usb_restart steps down instead", lk._usb_restart(), True)
    check("only the panel's own interface was touched", touched(p),
          [f"/restart-device {PANEL_IF}"])
    check("the hub's node is untouched", bus.nodes[HUB].code, 0)
    lk._close_real_subprocess()


def case_one_way_parent() -> None:
    print("case: a parent that will not name the panel back is not this panel's parent")
    bus = panel_bus(kids=False)
    p = Pnputil(bus)
    lk = link(bus, p)
    lk._capture_identity(confirmed=True)
    target, why = lk._verified_target(parent=True)
    check("the parent is refused", target, "")
    check("because the link does not go both ways", "does not go both ways" in why, True)
    lk._restart_depth = 1
    check("_usb_restart steps down to the interface", lk._usb_restart(), True)
    check("the composite device was not touched", touched(p),
          [f"/restart-device {PANEL_IF}"])
    lk._close_real_subprocess()


def case_localized_output() -> None:
    print("case: what pnputil said is never the verdict")
    bus = panel_bus()
    p = Pnputil(bus, text="Zugriff verweigert", refuse=True)   # "Access is denied.", exit 0
    lk = link(bus, p)
    ok, why = lk._pnputil("/disable-device", PANEL_IF)
    check("a refusal that exits 0 is not success", ok, False)
    check("and the reason comes from the node's state", "did not take" in why, True)
    # Identical wording, but this time the device really did end up disabled: same
    # words, opposite verdict, which is exactly why the wording cannot decide it.
    # (A no-op `/restart-device` cannot be told apart this way - leaving the node
    # present and enabled is what both outcomes look like - so the verb whose
    # promise is falsifiable is the one that proves the point.)
    p.refuse = False
    ok2, _ = lk._pnputil("/disable-device", PANEL_IF)
    check("the same words are success when the device is disabled", ok2, True)
    check("and it really reads disabled", bus.nodes[PANEL_IF].code, 22)
    lk._close_real_subprocess()


def case_refusal_is_its_own_state() -> None:
    print("case: a refusal is neither a recovery nor a firmware verdict")
    bus = Bus([Node(PANEL_IF, "USB Serial Device (COM7)",
                    ("USB\\VID_0A12&PID_0001&MI_00",), IF_COMPAT)])
    p = Pnputil(bus)
    lk = link(bus, p)
    lk._capture_identity()                        # never confirmed, ids unknown-ish
    lk._usb_restart()
    check("nothing was restarted", lk.usb_restarts, 0)
    check("no verb was actually issued", lk._restart_touched, 0)
    check("no firmware error is claimed", lk.usb_restart_error, "")
    check("the refusal is recorded as itself", bool(lk.usb_restart_refused), True)
    check("and it is not counted as a device reset", p.calls, [])
    lk._close_real_subprocess()


def case_verified_target_is_verb_target() -> None:
    print("case: when it is allowed, the verb goes to the verified instance id")
    bus = panel_bus()
    p = Pnputil(bus)
    lk = link(bus, p)
    lk._capture_identity(confirmed=True)
    check("_usb_restart proceeds", lk._usb_restart(), True)
    check("the verb named the instance id, not a port or a name", touched(p),
          [f"/restart-device {PANEL_IF}"])
    check("and it counts as one restart", lk.usb_restarts, 1)
    check("nothing else on the bus was addressed",
          [d for _v, d in p.calls if d != PANEL_IF], [])
    lk._close_real_subprocess()


def main() -> int:
    case_two_screens_one_port()
    case_hello_or_hardware_ids()
    case_com_reassigned()
    case_interface_gone()
    case_shared_hub()
    case_one_way_parent()
    case_localized_output()
    case_refusal_is_its_own_state()
    case_verified_target_is_verb_target()
    print("\nSELFTEST " + ("PASSED" if not fails else f"FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
