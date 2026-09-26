"""The panel as a link that can fail — because it does, on this desk, for real.

Everything the app draws goes through one 115200-baud CDC-ACM port on a board
that is power-cycled by the PC's USB bus. Three failure shapes have actually
been seen in log.log:

  * the port vanishes across a sleep/resume, and the vendor driver's own recovery
    (`WriteLine` → close → `openSerial`) gives up after 10 tries by calling
    `sys.exit(0)` — and `os._exit(0)` if that raises. A missing screen has
    therefore been able to terminate the whole app, which is the "it never came
    back" the panel gets blamed for;
  * the endpoint stops draining and a write blocks in `WriteFile` forever: the
    panel is frozen, the loop is frozen, and the log just stops;
  * the panel reboots (its own watchdog, a replug, a resume) and comes back in
    *portrait* at default brightness, while the diff transport still believes its
    copy of the pixels is current — so it sends only the changed bands of a frame
    the panel is no longer displaying in the right orientation.

So the link owns the device object and nothing else touches it:

  - every call is serialised, timed, and wrapped: a raise, a `SystemExit`, or a
    write that never returns marks the link down instead of escaping;
  - a down link is rebuilt here, on our own schedule, and **never** by letting the
    vendor's blind 10-try loop run while the port is missing — we ask for an awake
    port first and stay down (loudly, once) until there is one;
  - a rebuilt link replays the panel's setup — HELLO, orientation, brightness,
    screen-on — and demands a full repaint, because its memory is not ours.

`DiffPusher` takes this object in place of the raw driver; the call surface it
uses (`DisplayPILImage`, `get_width`, `get_height`) is unchanged, so the render
path did not have to learn about any of this.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from app.display import SERIAL_REVISIONS, _HELLO_WAIT_S, _RESET_WAIT_S, ensure_vendor_path

# Written before a `/disable-device` and removed after the matching `/enable-device`.
# A device left disabled stays disabled through a reboot, so the intent has to outlive
# this process: whoever starts next reads this and finishes the job. It sits beside
# log.log because the app's working directory is the project (as it is for the task).
_MARK_FILE = Path(".panel_reset_pending")

# A push of the whole 800x480 frame is ~0.82 s on revision C, so "slow" starts
# above that; anything past _WRITE_GIVEUP_S is the endpoint that stopped draining.
_SLOW_PUSH_S = 1.6
_WRITE_GIVEUP_S = 8.0
_RETRY_S = 10.0             # wait between rebuild attempts after a failure
_RETRY_SLOW_S = 60.0        # same, once the device ladder has been walked to the end
_RELINK_LOG_S = 30.0        # how often to say "still no screen", while retrying
# After this long deaf, stop only reopening the port and ask Windows to restart the
# device itself (pnputil). Reopening a COM port cannot unstick an endpoint that
# stopped draining — measured here: the port opens, HELLO's write blocks, and the
# vendor's RESTART command never reaches the panel.
_USB_RESTART_AFTER_S = 120.0
# The ladder is stepped one rung per attempt, so the wait between attempts decides how
# long the strongest rung takes to arrive. All three are walked a minute apart — reaching
# the last resort about four minutes into a real outage instead of a quarter of an hour
# — and only once they have all been tried does the cadence settle down, because a
# device that survives three rungs will not be persuaded by a fourth every minute.
_USB_RESTART_FIRST_S = 60.0
_USB_RESTART_EVERY_S = 300.0
# How often to repeat "this needs a hand on the cable" once the ladder is exhausted.
_GIVEUP_LOG_S = 900.0
# …and how often to repeat a safety refusal. A refusal is usually permanent until a
# human changes something (the COM number moved, the panel is on a shared hub), so
# saying it every ten seconds would bury everything else in the log.
_REFUSAL_LOG_S = 300.0
# These are local reads of the device store, not the vendor's serial dance: twenty
# seconds is generous, and there can be four of them before one destructive verb.
_QUERY_TIMEOUT_S = 20.0
# Device Manager's problem code, read off the device node instead of off a command's
# wording: 0 is "working properly" and 22 is "this device is disabled".
_CM_PROB_NONE = 0
_CM_PROB_DISABLED = 22

# Which hardware ids this build is willing to call "a panel" without having heard it
# answer first. This mirrors what the vendored revision-C detector itself selects on
# (`_get_awake_com_port`: serial 20080411, 0525:a4a7, 1d6b:0121/0106) - it is not
# invented here. A revision that is not listed has no expectation, and then escalation
# waits for an identity that actually answered HELLO.
_EXPECTED_HARDWARE_IDS = {
    "C": ("USB\\VID_1D6B&PID_0106", "USB\\VID_1D6B&PID_0121", "USB\\VID_0525&PID_A4A7"),
}

# The one compatible id that says "this is a composite device - the thing inside the
# screen", as opposed to a hub. Above the panel's *wake-up* face on this desk sits
# `USB\VID_1A40&PID_0101\6&23491980&0&2`, a hub the rest of the desk shares, and
# disabling a hub to fix a screen is not a recovery, it is an outage with extra steps.
_COMPOSITE_COMPATIBLE_ID = "USB\\COMPOSITE"

# A COM number as Windows spells it inside a device's display name.
_COM_IN_NAME = re.compile(r"\bCOM\d+\b", re.IGNORECASE)


def _wql_id(dev: str) -> str:
    """Escape an instance id for a WQL string literal inside a PowerShell literal.

    WQL wants every backslash doubled; a PowerShell single-quoted literal wants every
    quote doubled - and since WQL's own escaped quote is two quotes, one quote in the
    id becomes four here. Getting this wrong does not raise, it silently matches a
    different device, so it is spelled out once instead of improvised at a query.
    """
    return dev.replace("\\", "\\\\").replace("'", "''''")


def _ps_id(dev: str) -> str:
    """Escape an instance id for a PowerShell single-quoted literal (no WQL layer)."""
    return dev.replace("'", "''")


def _port_in_name(name: str) -> str:
    """The COM number a device's display name claims, uppercased, or "" for none."""
    m = _COM_IN_NAME.search(name or "")
    return m.group(0).upper() if m else ""


class PanelNode:
    """What the device store says about exactly one instance id."""

    def __init__(self, dev: str, present: bool, code: int, name: str, pnpclass: str,
                 hardware_ids: tuple[str, ...], compatible_ids: tuple[str, ...]) -> None:
        self.device_id = dev
        self.present = present
        self.code = code
        self.name = name
        self.pnpclass = pnpclass
        self.hardware_ids = hardware_ids
        self.compatible_ids = compatible_ids

    @property
    def disabled(self) -> bool:
        """Problem code 22: Windows says this device is switched off."""
        return self.code == _CM_PROB_DISABLED

    @property
    def healthy(self) -> bool:
        """Present with problem code 0 - the answer that lets a verb be called done."""
        return self.present and self.code == _CM_PROB_NONE

    def is_composite(self) -> bool:
        """True for the composite device inside the screen, false for hubs and hosts."""
        return any(c.upper() == _COMPOSITE_COMPATIBLE_ID for c in self.compatible_ids)


class PanelIdentity:
    """Where the panel actually sits on the bus, recorded while we were talking to it.

    The COM number is in here, but it is not an address: Windows hands those out again
    whenever it likes, which is how a remembered COM4 ends up belonging to something
    else. The address is `interface_id`, and every destructive verb re-asks Windows that
    this id still exists, still claims this COM number, and still has the hardware ids
    it had when we captured it. `parent_id` is never derived by trimming the interface's
    id or by matching its VID/PID - two screens of the same model are an ordinary thing
    to own - it is read from the device relationship Windows itself recorded, and
    confirmed by asking the parent to name this interface back.

    `confirmed` means the device behind this id answered HELLO, which is the only
    evidence here that is not Windows' opinion about a name. `expected` means its
    hardware ids are ones this build recognises as a panel, which is what lets a
    recovery run during an outage, when nothing will answer HELLO.
    """

    def __init__(self, interface_id: str, port: str, hardware_ids: tuple[str, ...],
                 expected: bool, confirmed: bool) -> None:
        self.interface_id = interface_id
        self.port = port
        self.hardware_ids = hardware_ids
        self.expected = expected
        self.confirmed = confirmed

    def may_reset(self) -> bool:
        """An identity is good for a destructive verb once, and only on one of these."""
        return self.confirmed or self.expected

    def describe(self) -> str:
        how = "answered HELLO" if self.confirmed else (
            "matches a known panel" if self.expected else "not confirmed as the panel")
        return f"{self.interface_id} ({self.port}, {how})"


class PanelLink:
    """One guarded, self-healing connection to the desk panel."""

    def __init__(self, cfg: dict, log=print) -> None:
        self.cfg = cfg
        self.log = log
        self.d = cfg["display"]
        self.rev = str(self.d["revision"]).upper()
        self.lcd = None
        self.ok = False
        self.port: str | None = None
        self.down_reason = ""
        self.rebuilds = 0
        self.slow_writes = 0
        self.errors = 0
        self._lock = threading.RLock()
        self._retry_at = 0.0
        self._building = False
        self._last_down_log = 0.0
        self._last_giveup_log = 0.0
        self._down_since: float | None = None
        self._last_usb_restart = 0.0
        self._restart_depth = 0
        self._restart_attempts = 0
        self._device = ""
        self.usb_restarts = 0
        self.usb_restart_error = ""
        self.usb_restart_refused = ""     # why the last escalation touched nothing
        self._restart_touched = 0         # destructive verbs actually issued, not asked for
        self._last_refusal_log = 0.0
        self._identity: PanelIdentity | None = None
        self._identity_error = ""
        self._brightness: int | None = None
        self._screen_on: bool | None = None
        self.needs_full = True       # panel memory is not trustworthy yet
        self._on_relink = None

    # ---------------------------------------------------------------- setup
    def open(self, on_relink=None) -> bool:
        """First bring-up. Returns False instead of exiting when there is no screen.

        `app.display.make_lcd` deliberately exits the process when the panel will
        not talk — right for a manual run, wrong for the autostarted loop, which
        should keep trying: a PC that boots before the panel enumerates currently
        stays dead until someone notices. This keeps the same bring-up code and
        turns its fatal answers into a retryable False.

        The one synchronous build: at start-up the loop has no frames to be late
        for, and it is better to draw the first frame on a live link than to have
        the first ten ticks find nothing.
        """
        self._on_relink = on_relink
        return self._build_now(first=True)

    def _build(self, reason: str = "") -> bool:
        """Synchronous rebuild — used for a resume, where the caller wants the answer."""
        return self._build_now(reason=reason)

    def start_build(self, reason: str = "background retry") -> None:
        """Rebuild on a background thread, so a deaf panel cannot stall the loop.

        A bring-up against a wedged device takes tens of seconds (auto-detect, HELLO
        bounded at 8 s, sometimes a reset and a second HELLO). Doing that inline made
        the loop's own tick gap look like a suspend — the watchdog fired every rebuild
        and the app "recovered" from a sleep that never happened. The link is down
        while this runs, so every call short-circuits and nothing waits on it.
        """
        with self._lock:
            if self.ok or self._building:
                return
            self._building = True

        def work() -> None:
            try:
                self._build_now(reason=reason)
            finally:
                with self._lock:
                    self._building = False

        threading.Thread(target=work, daemon=True, name="panel-bring-up").start()

    def _build_now(self, first: bool = False, reason: str = "") -> bool:
        """Bring the link up. Holds the lock only to publish the result.

        The slow part — auto-detect, HELLO, maybe a reset and a second HELLO — runs
        without the lock on purpose: `push()` takes that lock, and a build that holds
        it for half a minute while the panel ignores us would freeze the render loop
        exactly as badly as doing the work inline. Nothing can use the device while
        `ok` is False, so there is nothing to guard during the attempt.
        """
        ensure_vendor_path()
        if _MARK_FILE.exists():
            # Somebody (probably an earlier us) stopped this device to reset it and did
            # not finish the job. Fix that before complaining that it will not answer.
            self._heal_disabled()
        from library.lcd.lcd_comm import Orientation   # noqa: F401 (per attempt)
        import importlib

        from app import display as disp

        rev = self.rev
        w, h = int(self.d["portrait_width"]), int(self.d["portrait_height"])
        port = self.d["com_port"]
        try:
            if rev == "SIMU":
                from library.lcd.lcd_simulated import LcdSimulated
                lcd = LcdSimulated(display_width=w, display_height=h)
                lcd.Reset()
                lcd.InitializeComm()
            else:
                cls = disp._CLS.get(rev)
                if cls is None:
                    self.down_reason = f"unknown revision {rev}"
                    return False
                mod = importlib.import_module(cls[0])
                port = self._awake_port(mod, cls[1]) if str(port).upper() == "AUTO" else port
                if port is None:
                    return False        # nothing to open: stay down, retry later
                lcd = getattr(mod, cls[1])(com_port=port, display_width=w,
                                           display_height=h)
                self.port = getattr(lcd, "com_port", port)
                # Where the panel actually sits, recorded before anything can go wrong
                # with it: every device-level recovery is addressed by this and re-read
                # immediately before it touches anything.
                self._capture_identity()
            outcome = disp._bring_up(lcd, bool(self.d.get("reset_on_start", False)))
            if outcome != "ok":
                self.down_reason = f"bring-up said {outcome}"
                try:
                    lcd.closeSerial()
                except Exception:  # noqa: BLE001
                    pass
                return False
            # HELLO answered down this exact port. Of everything Windows can tell us
            # about names and numbers, that is the one piece of evidence that the device
            # behind the id we captured really is the panel.
            self._capture_identity(confirmed=True)
        except SystemExit as e:          # the vendor's "cannot open COM port" exit
            self.down_reason = f"driver gave up on the port (code {e.code})"
            return False
        except Exception as e:  # noqa: BLE001 - an absent screen is a normal state
            self.down_reason = f"{type(e).__name__}: {e}"
            return False

        # Says *why* the link had to be re-made, not just that it was: `down_reason`
        # describes the failure, which is a different question from what asked.
        why = reason or self.down_reason or "relinked"
        with self._lock:
            self.lcd = lcd
            self.ok = True
            self._brightness = self._screen_on = None
            self.needs_full = True
            self.down_reason = ""
            self._restart_depth = 0       # it answered: start the escalation over
            self._restart_attempts = 0
        self._apply_setup()
        if not first:
            self.rebuilds += 1
            self.log(f"[panel] link rebuilt ({why}; rebuild #{self.rebuilds}) — "
                     f"full repaint")
        if self._on_relink is not None:
            try:
                self._on_relink()
            except Exception:  # noqa: BLE001
                pass
        return True

    def _awake_port(self, mod, cls_name: str) -> str | None:
        """An awake CDC-ACM port, or None. Also wakes a sleeping panel.

        The vendor's `openSerial()` opens whatever it is given up to ten times and
        then kills the process; asking first means that loop only ever runs against
        a port that exists. For revision C this also pokes the 1A86:CA21 "sleeping"
        face that wakes the panel over.
        """
        try:
            cls = getattr(mod, cls_name)
            return cls.auto_detect_com_port()
        except SystemExit:
            return None
        except Exception as e:  # noqa: BLE001
            self.down_reason = f"auto-detect: {type(e).__name__}: {e}"
            return None

    def _apply_setup(self) -> None:
        """Replay what the panel needs to show landscape pixels at all.

        A panel that rebooted came back portrait at its default brightness, and the
        driver's `orientation` attribute — which decides the width/height the layout
        renders for — still says whatever we last told it. Both are re-said here,
        so "the app came back" and "the panel shows the app" are the same event.
        """
        if self.lcd is None:
            return
        from library.lcd.lcd_comm import Orientation
        land = str(self.d.get("orientation", "landscape")).lower() == "landscape"
        orient = Orientation.LANDSCAPE if land else Orientation.PORTRAIT
        # Guarded like every other write: SetOrientation is a command, and a panel
        # that answers HELLO but has stopped draining its endpoint would hang here.
        if self._call("orientation", self.lcd.SetOrientation, orient, giveup=4.0):
            # The driver's own notion of the orientation decides the width/height the
            # layout renders for, so it is set even if the panel did not acknowledge.
            self.lcd.orientation = orient
        else:
            self.log("[panel] orientation could not be re-applied — the panel is not "
                     "taking commands; it will be retried on the next rebuild")
        try:
            self.lcd.lcd_serial.write_timeout = 2.0
        except Exception:  # noqa: BLE001 - optional hardening
            pass

    # ------------------------------------------------------- guarded calls
    def _call(self, what: str, fn, *args, giveup: float = _WRITE_GIVEUP_S,
              bounded: bool = True):
        """One device call, serialised, timed, and survivable.

        Returns True/False for "the panel took it". Never raises, and never lets a
        blocked write block the caller twice: after a timeout the link is marked
        down and every later call short-circuits until a rebuild.
        """
        with self._lock:
            if not self.ok or self.lcd is None:
                return False
            t0 = time.monotonic()
            box: list = []

            def run() -> None:
                try:
                    box.append(fn(*args))
                except BaseException as e:     # noqa: BLE001 - SystemExit included
                    box.append(e)

            if bounded:
                th = threading.Thread(target=run, daemon=True, name="panel-io")
                th.start()
                th.join(giveup)
                if th.is_alive():
                    self.slow_writes += 1
                    self.ok = False
                    self.down_reason = f"{what} blocked >{giveup:.0f}s"
                    self.errors += 1
                    self._retry_at = time.monotonic() + _RETRY_S
                    self.log(f"[panel] {self.down_reason} — endpoint stopped draining; "
                             f"link down, will rebuild")
                    return False
                res = box[0] if box else None
            else:
                try:
                    box.append(fn(*args))
                except BaseException as e:  # noqa: BLE001
                    box.append(e)
                res = box[0]
            dt = time.monotonic() - t0
            if dt > _SLOW_PUSH_S:
                self.slow_writes += 1
                self.log(f"[panel] slow {what}: {dt:.2f}s on a {self.rev} link")
            if isinstance(res, SystemExit):
                self.ok = False
                self.errors += 1
                self.down_reason = f"{what}: driver exited the process (code {res.code})"
                self._retry_at = time.monotonic() + _RETRY_S
                self.log(f"[panel] {self.down_reason} — caught; the app stays up and "
                         f"the link will be rebuilt")
                return False
            if isinstance(res, BaseException):
                self.ok = False
                self.errors += 1
                self.down_reason = f"{what}: {type(res).__name__}: {res}"
                self._retry_at = time.monotonic() + _RETRY_S
                self.log(f"[panel] {self.down_reason} — link down, will rebuild")
                return False
            return True

    def push(self, img, x: int = 0, y: int = 0) -> bool:
        return bool(self._call(f"push {x},{y}", self._push, img, x, y))

    def _push(self, img, x: int, y: int) -> None:
        assert self.lcd is not None
        self.lcd.DisplayPILImage(img, x, y)

    # Vendor-shaped passthroughs, so DiffPusher can hold this instead of the driver.
    def DisplayPILImage(self, image, x: int = 0, y: int = 0,   # noqa: N802
                        image_width: int = 0, image_height: int = 0) -> None:
        self.push(image, x, y)

    def get_width(self) -> int:      # noqa: N802
        return int(self.d["portrait_height"]) if str(self.d.get("orientation", "landscape")) \
            .lower() == "landscape" else int(self.d["portrait_width"])

    def get_height(self) -> int:     # noqa: N802
        return int(self.d["portrait_width"]) if str(self.d.get("orientation", "landscape")) \
            .lower() == "landscape" else int(self.d["portrait_height"])

    def set_brightness(self, pct: int) -> bool:
        pct = max(0, min(100, int(pct)))
        if self.ok and self._brightness == pct:
            return True
        if self._call(f"brightness {pct}%", self._brightness_now, pct, giveup=4.0):
            self._brightness = pct
            return True
        return False

    def _brightness_now(self, pct: int) -> None:
        assert self.lcd is not None
        self.lcd.SetBrightness(pct)

    def screen(self, on: bool) -> bool:
        if self.ok and self._screen_on is on:
            return True
        what = "screen-on" if on else "screen-off"
        if self._call(what, self._screen, on, giveup=4.0):
            self._screen_on = on
            return True
        return False

    def _screen(self, on: bool) -> None:
        assert self.lcd is not None
        (self.lcd.ScreenOn if on else self.lcd.ScreenOff)()

    # ------------------------------------------------------------- lifecycle
    def invalidate(self) -> None:
        """The next push must be a whole frame (the panel's memory is not ours)."""
        self.needs_full = True

    def relink(self, reason: str) -> bool:
        """Rebuild after a resume/replug/display change. Safe to call spuriously."""
        with self._lock:
            self._retry_at = 0.0
            was = self.ok
            if self.lcd is not None:
                try:
                    self.lcd.closeSerial()
                except Exception:  # noqa: BLE001
                    pass
            self.ok = False
            self._screen_on = self._brightness = None
            # `down_reason` stays about the failure, not about the request: it is what
            # the beat line and the retry log quote while the link is down.
            built = self._build(reason=reason)
            if built and was:
                self.log(f"[panel] re-linked after {reason}")
            return built

    def tick(self) -> bool:
        """Keep the link honest: rebuild it when it is down and due."""
        if self.ok:
            self._down_since = None
            return True
        now = time.monotonic()
        if self._down_since is None:
            self._down_since = now
        if now < self._retry_at:
            return False
        wait = self._retry_interval()
        self._retry_at = now + wait
        if self.ok:
            return True
        if self._building:
            return False                      # an attempt is already in flight
        self.start_build()
        if self.ok:
            return True
        # Reopening the port has been failing for a while: the device itself needs a
        # word. pnputil needs Administrator — which the scheduled task has — and when
        # it does not, the answer is logged, not swallowed.
        if (self.cfg["display"].get("usb_restart_on_fail", True)
                and now - self._down_since > _USB_RESTART_AFTER_S
                and now - self._last_usb_restart > self._restart_wait()):
            self._last_usb_restart = now
            self._usb_restart()
        if now - self._last_down_log > _RELINK_LOG_S:
            self._last_down_log = now
            self.log(f"[panel] no usable screen ({self.down_reason or 'not built'}); "
                     f"retrying every {wait:.0f}s — the app keeps running")
        self._exhausted_notice(now)
        return False

    def _retry_interval(self) -> float:
        """How long to sit before the next rebuild attempt.

        Ten seconds is right while there is something left to try — a replug, a port
        that has come back, a rung of the device ladder still unspent. It is wrong once
        the ladder has run out: an attempt is a full 35-second vendor bring-up, so
        attempts back-to-back spend the night re-timing-out against a board that only a
        power cycle will wake, and they fill the log the app itself has to read in the
        morning. After the last rung the cadence drops to `_RETRY_SLOW_S`, which still
        takes a replug inside a minute — and any real event (resume, replug, display
        change) calls `relink()`, which ignores this and tries immediately.
        """
        return _RETRY_S if self._restart_attempts < 3 else _RETRY_SLOW_S

    def _exhausted_notice(self, now: float) -> None:
        """Say it once, plainly, when the software ladder has run out.

        Every rung above logs its own result, which is right for a log but wrong for a
        person: three successful `pnputil` calls and three HELLO timeouts is the
        evidence that the fault is past software, and the app should draw that
        conclusion instead of leaving it to whoever is reading at 7 a.m. One line,
        repeated only every `_GIVEUP_LOG_S`, because after that it is the same sentence.

        It counts verbs actually issued (`_restart_touched`), not escalations asked
        for: a ladder that was refused at every rung has not proved anything about the
        firmware, and it says so in its own refusal line instead.
        """
        if (self._restart_touched < 3 or self.ok
                or now - self._last_giveup_log < _GIVEUP_LOG_S):
            return
        self._last_giveup_log = now
        self.log("[panel] software recovery exhausted — the device has been restarted "
                 "twice and then disabled/re-enabled, and it still will not answer "
                 "HELLO. That is past software: unplug the screen's USB (or its hub "
                 "port), wait five seconds, plug it back. This loop takes it back "
                 "within a minute on its own; nothing needs restarting.")

    def _restart_wait(self) -> float:
        """How long to sit before the next rung of the ladder.

        The whole ladder is walked a minute at a time: escalation is pointless if the
        strongest rung takes a quarter of an hour to arrive. Only after every rung has
        been tried does it settle down to `_USB_RESTART_EVERY_S` — a device that
        survives all three is not going to be persuaded by a fourth every minute.
        """
        return (_USB_RESTART_FIRST_S if self._restart_attempts < 3
                else _USB_RESTART_EVERY_S)

    # -------------------------------------------------- device-level recovery
    def _port_device(self) -> str:
        """The COM port to blame for a restart: the one we last tried to use."""
        want = str(self.port or self.d.get("com_port") or "").upper()
        return want if want.startswith("COM") else ""

    # ---------------------------------------------- the device store, read-only
    def _bus(self, script: str) -> str:
        """One machine-readable query against the device store: its stdout, or "".

        Single-quoted literals and `[char]39` only, no double quotes anywhere: the
        script travels as one argv element through Python's join and PowerShell's
        parser, and every extra quoting layer has to survive both. Every answer is
        prefixed with a marker word this module chose, so localized command output,
        progress noise and a module's own banner cannot be mistaken for one - and an
        answer that does not arrive in that shape is treated as no answer at all.
        """
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                "-Command", script], capture_output=True, text=True,
                               timeout=_QUERY_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - a bus we cannot read is a state, not a crash
            self.usb_restart_error = f"query: {type(e).__name__}: {e}"
            return ""
        return r.stdout or ""

    @staticmethod
    def _answers(out: str, marker: str) -> list[str]:
        """The marked lines a query printed, in order, with the marker stripped."""
        return [ln.strip()[len(marker):] for ln in (out or "").splitlines()
                if ln.strip().startswith(marker)]

    @staticmethod
    def _node_script(where: str) -> str:
        """A query that prints exactly one node's facts, or how many nodes answered.

        `where` is the whole `Get-CimInstance` expression, so the same reading works for
        an exact instance id and for the one discovery-by-port-number query.
        """
        return ("$d = @(" + where + ");"
                " if ($d.Count -ne 1) { 'PANELNODE|count=' + [string]$d.Count }"
                " else { $x = $d[0]; 'PANELNODE|1|' + [string]$x.Present + '|'"
                " + [string]$x.ConfigManagerErrorCode + '|' + [string]$x.Name + '|'"
                " + [string]$x.PNPClass + '|' + ($x.HardwareID -join ';')"
                " + '|' + ($x.CompatibleID -join ';') + '|' + [string]$x.DeviceID }")

    def _read_node(self, out: str, dev: str) -> tuple[PanelNode | None, str]:
        """Parse one node line. `absent` and `ambiguous` come back as reasons, not None.

        They are the two answers that have to stop something destructive, and each needs
        saying out loud: "no such device any more" and "several nodes claim to be this
        one" are different faults with different fixes, and neither is guessable.
        """
        lines = self._answers(out, "PANELNODE|")
        if not lines:
            return None, f"Windows would not say anything about {dev}"
        fields = lines[-1].split("|")
        if fields[0].startswith("count="):
            n = fields[0][len("count="):]
            if n == "0":
                return None, f"{dev} is not in the device store any more"
            return None, f"{n} device nodes answer to the one id {dev}"
        if len(fields) != 8:
            return None, f"the node for {dev} did not answer in the expected shape"
        try:
            code = int(fields[2])
        except ValueError:
            return None, f"the node for {dev} reported problem code {fields[2]!r}"
        # The id comes back from the store, never from whatever we searched by: the one
        # query that is allowed to search by port name is how an instance id is *found*,
        # and reusing the search key as the answer would record "COM7" as the panel's
        # identity - a number Windows hands out again whenever it likes.
        return (PanelNode(fields[7] or dev, fields[1] == "True", code, fields[3],
                          fields[4],
                          tuple(h for h in fields[5].split(";") if h),
                          tuple(c for c in fields[6].split(";") if c))), ""

    def _node(self, dev: str) -> tuple[PanelNode | None, str]:
        """The device node that answers to this exact instance id - the only address.

        Never a name pattern and never a VID/PID prefix: a device is addressed by the
        one string that names it, escaped for WQL once in `_wql_id`.
        """
        wql = "'DeviceID=' + [char]39 + '" + _wql_id(dev) + "' + [char]39"
        return self._read_node(self._bus(self._node_script(
            "Get-CimInstance Win32_PnPEntity -Filter " + wql)), dev)

    def _node_owning(self, port: str) -> tuple[PanelNode | None, str]:
        """The single node that claims `port` - used once, at discovery, to find an id.

        A COM number is published nowhere but the display name, so this is the one query
        anywhere that searches by name, and it is how an instance id is *found*, never
        how a destructive verb is addressed. Two claimants (a Bluetooth outbound link
        takes COM numbers too) is a refusal, not a `Select-Object -First 1`.
        """
        wql = "'Name LIKE \"%(" + _wql_id(port) + ")%\"'"
        return self._read_node(self._bus(self._node_script(
            "Get-CimInstance Win32_PnPEntity -Filter " + wql)), port)

    def _parent_of(self, dev: str) -> str:
        """The instance id Windows itself recorded as this device's parent.

        `Win32_PnPEntity.Parent` is empty for a CDC interface, and the old code worked
        around that by trimming the `&MI_xx` suffix off the interface id and LIKE-matching
        what was left - a guess about a name, which finds *a* device of the same model
        rather than *this* device's parent. `DEVPKEY_Device_Parent` is the relationship
        the enumerator recorded, which is the same answer `CM_Get_Parent` gives.
        """
        script = ("$p = @(Get-PnpDeviceProperty -InstanceId '" + _ps_id(dev) + "'"
                  " -KeyName DEVPKEY_Device_Parent -ErrorAction SilentlyContinue);"
                  " if ($p.Count -eq 1 -and $p[0].Data) { 'PANELPARENT|' + [string]$p[0].Data }"
                  " else { 'PANELPARENT|' }")
        lines = self._answers(self._bus(script), "PANELPARENT|")
        return lines[-1].strip() if lines else ""

    def _children_of(self, dev: str) -> list[str]:
        """The children Windows recorded for a device, so a claimed parent can be asked
        to name this interface back. Case-insensitively: the property store hands the
        instance part of an id back in lower case (`…\\8&e9576a&0&0000` for a node whose
        DeviceID reads `8&E9576A`), measured here."""
        script = ("$c = @(Get-PnpDeviceProperty -InstanceId '" + _ps_id(dev) + "'"
                  " -KeyName DEVPKEY_Device_Children -ErrorAction SilentlyContinue);"
                  " if ($c.Count -eq 1 -and $c[0].Data) { 'PANELCHILD|' + (($c[0].Data) -join ';') }"
                  " else { 'PANELCHILD|' }")
        lines = self._answers(self._bus(script), "PANELCHILD|")
        return [c for c in (lines[-1].split(";") if lines else []) if c]

    def _verb_delivered(self, verb: str, dev: str) -> tuple[bool, str]:
        """Did the device end up where this verb promises it would?

        Each verb promises a state, and the device store is asked for that state: a
        disable has to leave the node reading disabled, a restart or an enable has to
        leave it present and not disabled. Anything else - including a node that cannot
        be read - is not a success. A restart that did nothing is indistinguishable from
        one that worked by state alone, so this proves the device is where it should be,
        not that the verb was the thing that put it there.
        """
        node, why = self._node(dev)
        if node is None:
            return False, f"{dev} cannot be read after {verb}: {why}"
        if verb == "/disable-device":
            if node.disabled:
                return True, f"{dev} now reads disabled"
            return False, (f"{dev} reads problem code {node.code}, not disabled - "
                           f"{verb} did not take")
        if verb in ("/enable-device", "/restart-device"):
            if node.disabled:
                return False, f"{dev} still reads disabled after {verb}"
            if not node.present:
                return False, f"{dev} is not present after {verb}"
            return True, ""
        return False, f"no device state is promised by {verb}, so it cannot be believed"

    # ------------------------------------------------------- the panel's identity
    def _capture_identity(self, confirmed: bool = False) -> None:
        """Record which device on the bus the panel is, while we are talking to it.

        Cheap on repeat: the store is only re-read when the port has changed, or when
        `confirmed` says a HELLO just came back down it. Everything device-level later
        is addressed by what is recorded here and re-read again immediately before it
        acts; a COM number on its own is never an address.
        """
        if self.rev not in SERIAL_REVISIONS:
            return                      # SIMU has no bus presence, TUR_USB no COM port
        port = self._port_device()
        if not port:
            self._identity = None
            self._identity_error = "no COM port was resolved for the panel"
            return
        if self._identity is not None and self._identity.port == port:
            if confirmed:
                self._identity.confirmed = True
            return
        node, why = self._node_owning(port)
        if node is None:
            self._identity = None
            self._identity_error = f"{port}: {why}"
            return
        known = _EXPECTED_HARDWARE_IDS.get(self.rev, ())
        expected = any(h.upper().startswith(k.upper())
                       for h in node.hardware_ids for k in known)
        self._identity = PanelIdentity(node.device_id, port, node.hardware_ids,
                                       expected, confirmed)
        self._identity_error = ""
        self.log(f"[panel] panel identified on the bus as {self._identity.describe()}")

    def _verified_target(self, parent: bool) -> tuple[str, str]:
        """The one device instance this escalation may touch, or ("" , why it may not).

        Asked immediately before the verb and never once at start-up, because everything
        it rests on can change in between: Windows reissues COM numbers, an interface
        vanishes across a resume, and the device above an interface is not always the
        screen. An empty answer means touch nothing - not "do the smaller thing
        instead", and not "assume it worked anyway".
        """
        ident = self._identity
        if ident is None:
            return "", (self._identity_error or
                        "the panel was never identified on the bus")
        if not ident.may_reset():
            return "", (f"{ident.interface_id} is not established as the panel: it never "
                        f"answered HELLO, and its hardware ids are not a revision {self.rev} "
                        f"screen this build knows")
        node, why = self._node(ident.interface_id)
        if node is None:
            return "", f"the panel's interface {ident.interface_id}: {why}"
        if node.disabled:
            return "", (f"{ident.interface_id} is already disabled - what it needs is "
                        f"enabling, which is not what any of these verbs are for")
        if node.hardware_ids != ident.hardware_ids:
            return "", (f"{ident.interface_id} now reports hardware ids "
                        f"{';'.join(node.hardware_ids) or 'none'} instead of "
                        f"{';'.join(ident.hardware_ids)} - that is a different device "
                        f"wearing this instance id")
        claimed = _port_in_name(node.name)
        if claimed != ident.port:
            return "", (f"{ident.interface_id} no longer claims {ident.port} (its name is "
                        f"{node.name or 'blank'}) - whatever owns {ident.port} now is not "
                        f"the panel and will not be touched")
        if not parent:
            return ident.interface_id, ""
        par = self._parent_of(ident.interface_id)
        if not par:
            return "", f"Windows will not say what the parent of {ident.interface_id} is"
        pnode, why = self._node(par)
        if pnode is None:
            return "", f"the parent {par}: {why}"
        if not pnode.is_composite():
            return "", (f"the parent of {ident.interface_id} is {par} "
                        f"({pnode.name or 'unnamed'}), which is not the screen's own "
                        f"composite device - other devices hang off it")
        if not pnode.present:
            return "", f"the parent {par} is not present"
        if ident.interface_id.lower() not in [k.lower() for k in self._children_of(par)]:
            return "", (f"{par} does not list {ident.interface_id} among its children, so "
                        f"the parent link does not go both ways")
        return par, ""

    def _refuse_restart(self, why: str) -> bool:
        """Nothing was touched, and that is reported as itself.

        A refusal is neither a recovery (`usb_restarts` stays where it was) nor the
        firmware verdict: the exhausted notice must not get to claim the ladder was
        walked when it was never allowed to step. The cadence still advances, because a
        refusal that outlives a reboot would otherwise be logged every ten seconds.
        """
        self.usb_restart_refused = why
        self._restart_attempts += 1
        now = time.monotonic()
        if now - self._last_refusal_log > _REFUSAL_LOG_S:
            self._last_refusal_log = now
            self.log(f"[panel] NOT touching any device: {why}. No restart and no disable "
                     f"were issued; the port is still being retried on its own schedule.")
        return False

    def _usb_restart(self) -> bool:
        """Restart the panel's USB device. The last resort that is not a hand on the cable.

        When the CDC endpoint has stopped draining, nothing can be delivered — not
        even the vendor's own RESTART — so retrying the port forever cannot help;
        measured here with `tools/screen_wake_probe.py`: the port opens, the first
        write blocks. It needs Administrator — the scheduled task has it — and pnputil
        **exits 0 even when it refuses**, so a verdict is read from the device store
        afterwards, never from what the command said.
        (`Restart-PnpDevice`, the cmdlet that would do the same, has been removed from
        this build's PnpDevice module.)

        Three rungs, each tried once before stepping up, all of them aimed at a device
        `_verified_target` has just confirmed is the panel's:

        1. restart the CDC interface (`…&MI_00`) — re-initialises the serial pipe;
        2. restart the composite device above it — the software unplug/replug;
        3. disable and re-enable that device — the strongest thing software can do to
           the port, and the only rung that can leave the device *disabled*, so it is
           bracketed by a marker file and a start-up heal (see `_power_cycle`).

        None of them runs on a guess. A COM number is reissued by Windows whenever it
        likes, and two screens of the same model differ only by their instance id, so
        the ladder aims at the id captured while the panel was answering and re-read
        immediately before the verb; if that reading is ambiguous, stale, or lands on a
        hub the rest of the desk shares, the answer is a refusal - which is neither a
        recovery nor a verdict about the panel's firmware.

        Measured on this desk: the interface restart brought the pipe back (a read
        returned where the write had blocked) but HELLO came back empty — pipe alive,
        MCU not — and the parent restart did not clear it either. So the ladder exists
        to take the fault as far down as it can go and to say plainly, when it runs
        out, that the answer is the cable.
        """
        port = self._port_device()
        if not port:
            self.usb_restart_error = "no COM port known for the panel"
            return False
        want_parent = self._restart_depth > 0
        target, why = self._verified_target(parent=want_parent)
        if not target and want_parent:
            # Losing the parent is a reason to take a smaller rung, not a reason to do
            # nothing: the interface is still ours to restart. Say which rung was lost.
            self.log(f"[panel] cannot reach the device above {port}: {why} - stepping "
                     f"down to the panel's serial interface")
            target, why = self._verified_target(parent=False)
        if not target:
            return self._refuse_restart(why)
        aimed_parent = target != self._identity.interface_id
        self.usb_restart_refused = ""
        self._device = target           # remembered: the heal path needs an id to fix
        depth = self._restart_depth
        self._restart_attempts += 1     # how long to wait for the next rung
        self._restart_depth += 1        # next time, aim one rung higher
        self._restart_touched += 1      # something is genuinely about to be disrupted
        if depth >= 2:
            ok, line = self._power_cycle(target)
            did = "power-cycled the panel's USB device"
        else:
            ok, line = self._pnputil("/restart-device", target)
            did = f"restarted the panel's USB {'device' if aimed_parent else 'port'}"
        if ok:
            self.usb_restarts += 1
            self._retry_at = time.monotonic() + 5.0     # the device is re-enumerating
            self.log(f"[panel] {did} ({target}) — waiting for it to come back")
            return True
        self._restart_depth = 0
        self.usb_restart_error = line
        self.log(f"[panel] device reset did not work ({self.usb_restart_error}) — this "
                 f"needs Administrator; the loop keeps retrying the port")
        return False

    def _pnputil(self, verb: str, dev: str) -> tuple[bool, str]:
        """One pnputil device verb, believed only as far as the device store confirms it.

        The exit code lies (`Access is denied.` comes back with a 0) and the wording is
        localized, so the old `fail`/`denied`/`error` sniffing was never a verdict - it
        was an English accent, and `Zugriff verweigert` read as success. Each verb
        promises a state, so `_verb_delivered` asks the device node for that state and
        that answer is the verdict; the command's own last line is kept for the log.
        """
        try:
            r = subprocess.run(["pnputil", verb, dev], capture_output=True, text=True,
                               timeout=60)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        line = (out.splitlines() or [f"exit {r.returncode}"])[-1][:140]
        if r.returncode != 0:
            return False, f"{line} (exit {r.returncode})"
        delivered, why = self._verb_delivered(verb, dev)
        return delivered, (why or line)

    def _power_cycle(self, dev: str) -> tuple[bool, str]:
        """Disable, then re-enable: take the device off the bus and put it back.

        `/restart-device` re-initialises the driver stack while the device stays
        claimed; disabling tears it down and re-enabling re-enumerates it, which is as
        close to pulling the plug as an OS will let a program get.

        The risk is the gap between the two words: a device left disabled stays
        disabled **through a reboot**, which would turn a dark panel into an absent one.
        So the intent is written down before the disable (`_MARK_FILE`) and the enable
        is retried; if even that fails, the log says what to click, because the app has
        then genuinely made the desk worse and must not pretend otherwise.
        """
        ok, line = self._pnputil("/disable-device", dev)
        if not ok:
            return False, f"disable: {line}"      # never re-enable what we did not stop
        try:
            _MARK_FILE.write_text(dev + "\n", encoding="utf-8")
        except OSError:
            pass   # the marker is the safety net; the enable below is the real step
        time.sleep(2.0)                # the hub needs a moment to drop it
        last = line
        for _ in range(3):
            ok, last = self._pnputil("/enable-device", dev)
            if ok:
                try:
                    _MARK_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                return True, last
            time.sleep(2.0)
        self.log(f"[panel] COULD NOT RE-ENABLE the panel's USB device ({dev}): {last} — "
                 f"Device Manager → USB → enable it, or replug the screen. "
                 f"A disabled device stays disabled through a reboot.")
        return False, f"enable: {last}"

    def _heal_disabled(self) -> None:
        """If a previous run left the device disabled (crash between the two words),
        enable it before anything else. The marker file is the whole memory: this may
        be a different process, or the first tick after a boot."""
        try:
            dev = _MARK_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not dev:
            try:
                _MARK_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.log(f"[panel] a previous run left {dev} disabled — enabling it")
        ok, line = self._pnputil("/enable-device", dev)
        if ok:
            try:
                _MARK_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            self.log("[panel] device re-enabled")
        else:
            self.log(f"[panel] still could not enable {dev}: {line}")

    def close(self) -> None:
        with self._lock:
            if self.lcd is not None:
                try:
                    self.lcd.closeSerial()
                except Exception:  # noqa: BLE001
                    pass
            self.lcd = None
            self.ok = False

    def summary(self) -> str:
        state = "up" if self.ok else f"down({self.down_reason or 'never built'})"
        s = (f"panel={state} rev={self.rev} port={self.port or '-'} "
             f"bright={self._brightness} screen={self._screen_on}")
        if self.rebuilds:
            s += f" rebuilds={self.rebuilds}"
        if self.usb_restarts:
            s += f" usb_restarts={self.usb_restarts}"
        elif self.usb_restart_error:
            s += f" usb_restart={self.usb_restart_error}"
        if self.usb_restart_refused:
            s += f" usb_refused={self.usb_restart_refused[:70]}"
        if self.errors or self.slow_writes:
            s += f" errors={self.errors} slow={self.slow_writes}"
        return s
