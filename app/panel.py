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

import subprocess
import threading
import time
from pathlib import Path

from app.display import _HELLO_WAIT_S, _RESET_WAIT_S, ensure_vendor_path

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
            outcome = disp._bring_up(lcd, bool(self.d.get("reset_on_start", False)))
            if outcome != "ok":
                self.down_reason = f"bring-up said {outcome}"
                try:
                    lcd.closeSerial()
                except Exception:  # noqa: BLE001
                    pass
                return False
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
        """
        if (self._restart_attempts < 3 or self.ok
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

    def _device_id(self, port: str, parent: bool = False) -> str:
        """The PnP instance id of the device that owns `port`, via WMI.

        Only single quotes inside the PowerShell command and no double quotes at all:
        this string is passed as one argv element and every extra quoting layer has to
        survive both Python's join and PowerShell's parser.

        `parent` walks one level up — from the CDC interface (`…&MI_00`) to the
        composite device inside the screen. Restarting the interface re-initialises the
        serial pipe; restarting the parent is the full unplug/replug, which is what the
        panel's MCU needs when the pipe came back but the firmware did not.
        """
        q = (f"$d = Get-CimInstance Win32_PnPEntity -Filter 'Name LIKE \"%({port})%\"' "
             "| Select-Object -First 1")
        if parent:
            # Win32_PnPEntity.Parent is empty for this device's CDC interface, so the
            # parent is derived: strip the `&MI_xx` suffix, escape the backslashes for
            # WQL (a string literal needs them doubled), and LIKE-match what is left.
            # The quote character is built with [char]39 rather than nested, because
            # this string is passed as one argv element through two parsers already.
            # Raw strings throughout: every backslash below is the character PowerShell
            # and WQL are meant to see. Checked against `tools/screen_wake_probe.py
            # --restart --query`, which prints both ids.
            q += (r" ; if ($d) { $b = $d.DeviceID -replace '&MI_[0-9A-F]+\\.*$', '';"
                  r" $e = $b -replace '\\', '\\'; $q = [char]39;"
                  r' $f = "DeviceID LIKE " + $q + $e + "\\%" + $q;'
                  r" $p = Get-CimInstance Win32_PnPEntity -Filter $f | Select-Object -First 1;"
                  r" if ($p) { $d = $p } }")
        ps = q + "; if ($d) { $d.DeviceID }"
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                "-Command", ps], capture_output=True, text=True,
                               timeout=60)
        except Exception as e:  # noqa: BLE001
            self.usb_restart_error = f"query: {type(e).__name__}: {e}"
            return ""
        out = (r.stdout or "").strip()
        return out.splitlines()[-1].strip() if out else ""

    def _usb_restart(self) -> bool:
        """Restart the panel's USB device. The last resort that is not a hand on the cable.

        When the CDC endpoint has stopped draining, nothing can be delivered — not
        even the vendor's own RESTART — so retrying the port forever cannot help;
        measured here with `tools/screen_wake_probe.py`: the port opens, the first
        write blocks. It needs Administrator — the scheduled task has it — and pnputil
        **exits 0 even when it refuses**, so every verdict below is read from its text.
        (`Restart-PnpDevice`, the cmdlet that would do the same, has been removed from
        this build's PnpDevice module.)

        Three rungs, each tried once before stepping up:

        1. restart the CDC interface (`…&MI_00`) — re-initialises the serial pipe;
        2. restart the composite device above it — the software unplug/replug;
        3. disable and re-enable that device — the strongest thing software can do to
           the port, and the only rung that can leave the device *disabled*, so it is
           bracketed by a marker file and a start-up heal (see `_power_cycle`).

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
        parent = self._restart_depth > 0
        dev = self._device_id(port, parent=parent)
        if (not dev or "\\" not in dev) and parent:
            parent, dev = False, self._device_id(port)      # no parent: try the port
        if not dev or "\\" not in dev:
            self.usb_restart_error = f"no device found for {port}"
            self.log(f"[panel] cannot restart the device: {self.usb_restart_error} "
                     f"(is {port} still there?)")
            return False
        self._device = dev              # remembered: the heal path needs an id to fix
        depth = self._restart_depth
        self._restart_attempts += 1     # how long to wait for the next rung
        self._restart_depth += 1        # next time, aim one rung higher
        if depth >= 2:
            ok, line = self._power_cycle(dev)
            did = "power-cycled the panel's USB device"
        else:
            ok, line = self._pnputil("/restart-device", dev)
            did = f"restarted the panel's USB {'device' if parent else 'port'}"
        if ok:
            self.usb_restarts += 1
            self._retry_at = time.monotonic() + 5.0     # the device is re-enumerating
            self.log(f"[panel] {did} ({dev}) — waiting for it to come back")
            return True
        self._restart_depth = 0
        self.usb_restart_error = line
        self.log(f"[panel] device reset did not work ({self.usb_restart_error}) — this "
                 f"needs Administrator; the loop keeps retrying the port")
        return False

    def _pnputil(self, verb: str, dev: str) -> tuple[bool, str]:
        """One pnputil device verb, judged by its output: the exit code lies."""
        try:
            r = subprocess.run(["pnputil", verb, dev], capture_output=True, text=True,
                               timeout=60)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        low = out.lower()
        bad = "fail" in low or "denied" in low or "error" in low
        line = (out.splitlines() or [f"exit {r.returncode}"])[-1][:140]
        return (r.returncode == 0 and not bad), line

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
        if self.errors or self.slow_writes:
            s += f" errors={self.errors} slow={self.slow_writes}"
        return s
