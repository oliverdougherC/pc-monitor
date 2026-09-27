"""Windows Night light: read what the OS decided, and turn it into a panel look.

Why this is not a settings file we own: the user asked for the panel to follow
*their* night mode — the toggle in Quick Settings, the "set hours" schedule, the
warmth slider. Windows exposes no public API for it, but it does persist the
state, and the encoding is documented by reverse engineering (CloudStore stores
its values as Microsoft Bond CompactBinary v1 payloads):

  ...CloudStore\\Store\\DefaultAccount\\Current\\
     default$windows.data.bluelightreduction.bluelightreductionstate\\
       windows.data.bluelightreduction.bluelightreductionstate   → "Data"
     default$windows.data.bluelightreduction.settings\\
       windows.data.bluelightreduction.settings                  → "Data"

Both are an outer CloudStore wrapper (metadata struct + a Unix timestamp + the
payload as a list<int8>) whose payload is *itself* a marshaled CB struct:

  state    field 0  int32   PRESENT ⇒ night light is force-enabled right now
           field 10 int32   initialized (always 1)
           field 20 uint64  FILETIME of the last on/off transition
  settings field 0  bool    a schedule is enabled
           field 10 bool    PRESENT ⇒ "set hours" (absent ⇒ sunset→sunrise)
           field 20/30 struct{0:hour,1:minute}  schedule start / end
           field 40 int16   colour temperature, Kelvin
           field 50/60 struct{0:hour,1:minute}  sunset / sunrise

Verified on this desk 2026-10-09 against the live values: the state payload is
`4342010010 00 D00A02 C614<filetime>` — field 0 present, i.e. ON — with settings
temperature 2525 K and a disabled schedule, which is exactly what Quick Settings
showed. Nothing here guesses: `parse_state`/`parse_settings` either decode the
fields or return `None`, and an undecodable blob degrades to "unknown". Unknown
is a third answer, never a quiet "off": CloudStore is rewritten by another
process, so a failed read holds the last *confirmed* appearance (see
`NightLight`) instead of flashing the panel back to day brightness, and the log
says where every answer came from.

The panel has no colour-temperature hardware, so the warmth is applied to the
pixels: `warm_lut()` builds a 768-entry per-channel LUT from the Kelvin value
(the same Helland blackbody approximation every temperature→RGB helper uses) and
`app.layout.Layout` runs it over the rendered frame. Scaling channels keeps
near-black at near-black, which is what the burn-in/power design needs.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

# Bond CompactBinary v1 type ids (only the ones the night-light schemas use).
_STOP, _STOP_BASE, _BOOL, _UINT8, _UINT16, _UINT32, _UINT64 = 0, 1, 2, 3, 4, 5, 6
_FLOAT, _DOUBLE, _STRING, _STRUCT, _LIST, _SET, _MAP, _INT8 = 7, 8, 9, 10, 11, 12, 13, 14
_INT16, _INT32, _INT64, _WSTRING = 15, 16, 17, 18

_STATE_KEY = (r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount"
              r"\Current\default$windows.data.bluelightreduction.bluelightreductionstate"
              r"\windows.data.bluelightreduction.bluelightreductionstate")
_SETTINGS_KEY = (r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount"
                 r"\Current\default$windows.data.bluelightreduction.settings"
                 r"\windows.data.bluelightreduction.settings")


# ------------------------------------------------------------------ CB reader
class CBError(ValueError):
    """The blob is not the CompactBinary shape we know how to read."""


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = shift = 0
    while True:
        if i >= len(b):
            raise CBError("varint runs past the end of the blob")
        c = b[i]
        i += 1
        r |= (c & 0x7F) << shift
        if c < 0x80:
            return r, i
        shift += 7
        if shift > 63:
            raise CBError("varint too long")


def _zigzag(v: int) -> int:
    return (v >> 1) ^ (-(v & 1))


class Reader:
    """Minimal Bond CB v1 walker: yields (field_id, type, value) for one struct."""

    def __init__(self, b: bytes, pos: int = 0, end: int | None = None,
                 tolerant: bool = False):
        self.b = b
        self.i = pos
        self.end = len(b) if end is None else end
        self.tolerant = tolerant

    def _take(self, n: int) -> bytes:
        if self.i + n > self.end:
            raise CBError("field runs past the end of the struct")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def value(self, typ: int):
        b = self.b
        if typ == _BOOL:
            return self._take(1)[0] != 0
        if typ in (_UINT8, _INT8):
            v = self._take(1)[0]
            return v - 256 if (typ == _INT8 and v > 127) else v
        if typ in (_UINT16, _UINT32, _UINT64):
            v, self.i = _varint(b, self.i)
            return v
        if typ in (_INT16, _INT32, _INT64):
            v, self.i = _varint(b, self.i)
            return _zigzag(v)
        if typ == _FLOAT:
            return float(int.from_bytes(self._take(4), "little"))
        if typ == _DOUBLE:
            return float(int.from_bytes(self._take(8), "little", signed=True))
        if typ in (_STRING, _WSTRING):
            n, self.i = _varint(b, self.i)
            raw = self._take(n)
            return raw.decode("utf-8", "replace" if typ == _STRING else "utf-16-le")
        if typ == _STRUCT:
            r = Reader(b, self.i, self.end, self.tolerant)
            out = r.fields()
            # fields() consumed the struct's own BT_STOP on its way out; advancing
            # past it again swallows the next field's header byte, which is how a
            # real blob ends up decoded as garbage.
            self.i = r.i
            return out
        if typ in (_LIST, _SET):
            elem = self._take(1)[0]
            n, self.i = _varint(b, self.i)
            return [self.value(elem) for _ in range(n)]
        if typ == _MAP:
            kt = self._take(1)[0]
            vt = self._take(1)[0]
            n, self.i = _varint(b, self.i)
            return {self.value(kt): self.value(vt) for _ in range(n)}
        raise CBError(f"unsupported Bond type {typ}")

    def fields(self) -> dict[int, object]:
        """One struct: field id → value. BT_STOP ends it, BT_STOP_BASE is skipped.

        `tolerant` (set on the reader) is for the CloudStore wrapper, which on this
        machine is written without its closing BT_STOP — the inner payload's struct
        is stopped exactly as Bond says, but the outer frame is not, and a strict
        reader walks off the end and calls a perfectly good value undecodable. The
        payload keeps the strict rule: a truncated *payload* really does mean "not
        our schema".
        """
        out: dict[int, object] = {}
        while self.i < self.end:
            h = self.b[self.i]
            self.i += 1
            typ = h & 0x1F
            if typ == _STOP:
                return out
            if typ == _STOP_BASE:
                continue
            idbits = h >> 5
            if idbits < 6:
                fid = idbits
            elif idbits == 6:
                fid = self._take(1)[0]
            elif idbits == 7:
                fid = int.from_bytes(self._take(2), "little")
            else:
                raise CBError("bad field header")
            out[fid] = self.value(typ)
        if self.tolerant:
            return out
        raise CBError("struct not terminated (missing BT_STOP)")


def unwrap(data: bytes) -> tuple[float, bytes]:
    """Outer CloudStore wrapper → (unix timestamp of the write, inner payload).

    Layout, decoded byte by byte from this machine's own blobs (offsets for the
    43-byte state value):

        [4]  0x0a  field 0  struct { 0: bool }        "modified" flag
        [8]  0x2a  field 1  struct {
                 [9]  0x06  field 0  uint64   unix mtime of the write
                 [15] 0x2a  field 1  struct {
                 [16] 0x2b field 1  list<int8>  the payload, 21 bytes
                       }
                 }

    i.e. the payload is three containers deep and the timestamp sits beside it. The
    payload arrives as a *list of signed bytes*, not a byte string, because Bond has
    no bytes type. One level of the nesting is tolerated on the way down, so a flatter
    blob still decodes; anything else is a `CBError`, and the caller degrades to
    "unknown" rather than acting on a guess.
    """
    if data[:4] != b"CB\x01\x00":
        raise CBError(f"not a marshaled CB v1 blob (starts {data[:4]!r})")
    top = Reader(data, 4, tolerant=True).fields()
    container = top.get(1)
    if not isinstance(container, dict):
        raise CBError("wrapper has no payload container")
    ts = container.get(0)
    node: object = container.get(1)
    for _ in range(2):
        if isinstance(node, dict):
            node = node.get(1)
    if not isinstance(node, list) or not node:
        raise CBError("wrapper has no list<int8> payload")
    blob = bytes(v & 0xFF for v in node)
    if blob[:4] != b"CB\x01\x00":
        raise CBError("inner payload is not a marshaled CB v1 struct")
    return float(ts or 0.0), blob


def _clock(block) -> tuple[int, int] | None:
    if not isinstance(block, dict):
        return None
    h = block.get(0, 0)
    m = block.get(1, 0)
    if not isinstance(h, int) or not isinstance(m, int):
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h, m


def parse_state(blob: bytes) -> dict | None:
    """{enabled, last_change} — or None when the payload is not our schema."""
    try:
        f = Reader(blob, 4).fields()
    except CBError:
        return None
    if 10 not in f:      # `initialized` — the marker that this is the state schema
        return None
    ft = f.get(20)
    dt = None
    if isinstance(ft, int) and ft > 122192928000000000:   # 1990-01-01 as FILETIME
        dt = time.gmtime(ft / 10000000 - 11644473600)
    return {"enabled": 0 in f, "last_change": dt}


def parse_settings(blob: bytes) -> dict | None:
    """{schedule, set_hours, start, end, temp_k, sunset, sunrise} or None."""
    try:
        f = Reader(blob, 4).fields()
    except CBError:
        return None
    if not ({0, 10, 20, 30, 40, 50, 60} & set(f)):
        return None
    start, end = _clock(f.get(20)), _clock(f.get(30))
    sunset, sunrise = _clock(f.get(50)), _clock(f.get(60))
    temp = f.get(40)
    return {
        "schedule": bool(f.get(0)),
        "set_hours": 10 in f,
        "start": start,
        "end": end,
        "temp_k": int(temp) if isinstance(temp, int) and temp > 0 else None,
        "sunset": sunset,
        "sunrise": sunrise,
    }


# ------------------------------------------------------------------ appearances
@dataclass(frozen=True)
class NightAppearance:
    """One coherent night look, published from a single read.

    Bundled so the panel can never act on a mixture - a fresh ON beside the
    old warmth, or an old ON beside a fresh warmth. `source` and `detail` say
    where the answer came from; `state_mtime` is the CloudStore wrapper's own
    write time (0.0 when the answer was inferred rather than read), and
    `since` is the monotonic clock when it was confirmed - what the hold
    grace period is measured against.
    """

    on: bool
    temp_k: int | None
    gains: tuple[float, float, float] | None
    source: str
    detail: str
    state_mtime: float = 0.0
    since: float = 0.0


# ------------------------------------------------------------------- the reader
class NightLight:
    """Answers "is the user's night mode on, and how warm did they set it?".

    Poll-throttled (a registry read is cheap, but the tick loop has no business
    doing one every second) and *self-describing*: the first read logs the whole
    decode, because a wrong read here is invisible on the panel — it just never
    goes amber, or never stops being amber.

    The answer is published as a whole `NightAppearance`, and the last confirmed
    one survives a blind poll: CloudStore is rewritten by another process, so a
    torn read or a missing key is a transient failure, not the user turning the
    light off. While holding, `stale` is True and the detail says so; past
    `hold_grace_s` the policy is explicitly to keep holding, because the
    alternative — guessing "day" from a store we cannot read — is the one
    outcome that brightens a dark room on a read error. A *confirmed* read
    replaces the appearance at once, OFF included.
    """

    def __init__(self, cfg: dict, refresh_s: float = 3.0):
        self.cfg = cfg.get("night", {})
        self.refresh_s = refresh_s
        self._next = 0.0
        self.on: bool | None = None   # None = unknown (nothing ever confirmed)
        self.temp_k: int | None = None
        self.gains: tuple[float, float, float] | None = None   # measured warm ramp
        self.source = "unknown"           # windows | windows-schedule | ramp | config | unknown
        self.detail = "not read yet"
        self.stale = False                # True while holding the last appearance
        self.appearance: NightAppearance | None = None
        self.hold_grace_s = float(self.cfg.get("hold_grace_s", 900))
        self._held_since = 0.0
        self._said: tuple = ()
        self._reads = 0

    # -- registry -----------------------------------------------------------
    def _blob(self, key: str) -> bytes | None:
        if os.name != "nt":
            return None
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                v, _ = winreg.QueryValueEx(k, "Data")
            return bytes(v) if v else None
        except OSError:
            return None       # absent value = the feature has never been touched

    def _read_windows(self) -> tuple[bool | None, int | None, str, str, float]:
        """(on, temp_k, source, detail, state_mtime) from the two CloudStore blobs.

        `state_mtime` is the wrapper timestamp of the blob the on/off answer
        came from - Windows' own write time for it, kept so a published
        appearance can say how fresh it is instead of re-guessing. The two
        blobs are read apart because they are written apart; the caller turns
        their answer into one appearance, so a half-failure never swaps in a
        new state beside an old warmth.
        """
        state = settings = None
        state_mtime = 0.0
        try:
            raw = self._blob(_STATE_KEY)
            if raw:
                state_mtime, inner = unwrap(raw)
                state = parse_state(inner)
        except CBError:
            state = None
        try:
            raw = self._blob(_SETTINGS_KEY)
            if raw:
                settings = parse_settings(unwrap(raw)[1])
        except CBError:
            settings = None
        temp = settings.get("temp_k") if settings else None
        if state is not None:
            # `enabled` is what Windows has applied *right now*: it flips on a
            # manual toggle and on each scheduled transition. The window computed
            # from `settings` is OR-ed in rather than trusted or ignored: it is
            # what the user's own schedule says, so it can only ever agree with
            # an intent they set, and it covers a build where the state blob
            # tracks only the manual toggle.
            now = time.localtime()
            mins = now.tm_hour * 60 + now.tm_min
            sched = bool(settings and settings["schedule"] and _in_window(mins, settings))
            on = bool(state["enabled"]) or sched
            det = f"state enabled={state['enabled']} schedule-now={sched}"
            if state.get("last_change"):
                det += ", changed " + time.strftime("%H:%M:%S", state["last_change"])
            if state_mtime:
                det += ", written " + time.strftime("%H:%M:%S",
                                                    time.localtime(state_mtime))
            if settings:
                det += (f"; schedule={settings['schedule']} "
                        f"{'set-hours' if settings['set_hours'] else 'sunset-sunrise'}"
                        f" {_hhmm(settings['start'])}-{_hhmm(settings['end'])} temp={temp}K")
            return on, temp, "windows", det, state_mtime
        if settings and settings["schedule"]:
            # No state blob but a schedule we can evaluate ourselves: honest,
            # and it keeps night mode working on a build that stores state
            # somewhere we cannot read.
            now = time.localtime()
            mins = now.tm_hour * 60 + now.tm_min
            win = _in_window(mins, settings)
            det = (f"no state blob; schedule {_hhmm(settings['start'])}-"
                   f"{_hhmm(settings['end'])} now={now.tm_hour:02d}:{now.tm_min:02d} "
                   f"{'in' if win else 'outside'} window")
            return win, temp, "windows-schedule", det, 0.0
        return (None, temp, "unknown", "no readable night-light state in CloudStore", 0.0)

    def _read(self, now: float) -> None:
        mode = str(self.cfg.get("mode", "auto")).lower()
        if mode == "on":
            temp = int(self.cfg.get("color_temp_k") or 0) or self.temp_k
            self._publish(NightAppearance(True, temp, None, "config",
                                          "night.mode: on", 0.0, now), now)
            return
        if mode == "off":
            self._publish(NightAppearance(False, self.temp_k, None, "config",
                                          "night.mode: off", 0.0, now), now)
            return
        on, temp, src, det, state_mtime = self._read_windows()
        temp = int(self.cfg.get("color_temp_k") or 0) or temp
        if on is None:
            sched = str(self.cfg.get("schedule") or "").strip()
            if sched:
                on = _in_clock_range(sched, time.localtime())
                src = "config-schedule"
                det = f"{det}; using night.schedule {sched} → {on}"
            else:
                det = f"{det}; night mode unavailable"
        # Second opinion: the gamma ramp the OS is actually applying. Night light
        # does not touch it on this build, but f.lux and friends do, and the ask was
        # to follow the user's night mode however they run it. Only ever *adds* an
        # on — a neutral ramp never overrides a registry that says warm, because that
        # is the normal Windows case and the registry is the better source there.
        gains = None
        if mode == "auto" and bool(self.cfg.get("check_gamma_ramp", True)):
            ramp = gamma_gains()
            if ramp is not None:
                r, _g, b = ramp
                warm = (r - b) >= float(self.cfg.get("ramp_warm_margin", 0.12))
                gains = ramp if warm else None
                det += f"; ramp r/b={r:.2f}/{b:.2f}{' warm' if warm else ''}"
                if warm and not on:
                    on, src = True, "ramp"
        if on is None:
            # Nothing this poll can say for certain: keep the last confirmed
            # appearance instead of dropping the panel to "unknown", which the
            # planner would read as night off - a white flash through a warm
            # evening, from a store that was merely mid-rewrite.
            self._hold(now, det)
            return
        if temp is None and self.appearance is not None and self.appearance.temp_k:
            # A settings-only failure: the state is fresh, the warmth is not.
            # Carry the warmth from the last appearance that had one, and say
            # where it came from - the alternative is the panel dimming with
            # no warm LUT at all, which is neither look the user asked for.
            temp = self.appearance.temp_k
            det += "; warmth held from the last settings read"
        self._publish(NightAppearance(bool(on), temp, gains, src, det,
                                      state_mtime, now), now)

    def _publish(self, app: NightAppearance, now: float) -> None:
        """Put a whole confirmed appearance on the object in one swap."""
        self.appearance = app
        self.on, self.temp_k, self.gains = app.on, app.temp_k, app.gains
        self.source, self.detail = app.source, app.detail
        self.stale = False
        self._held_since = 0.0

    def _hold(self, now: float, why: str) -> None:
        """A blind poll: keep showing the last confirmed appearance.

        Unknown and stale are answers in their own right, never a quiet off:
        the panel must not brighten just because a read failed, so the whole
        appearance is re-published unchanged and only `stale`/`detail` move.
        Within `hold_grace_s` this is a transient failure; past it the policy
        is explicit and non-disruptive - keep holding, because inventing a
        day state from an unreadable store is the one guess that can hurt.
        """
        if self.appearance is None:
            # Startup with nothing ever confirmed: there is no look to keep,
            # and the honest answer is unknown (the planner keeps the panel
            # in its day look until the first real answer arrives).
            self.on, self.source = None, "unknown"
            self.detail = f"{why}; nothing confirmed to hold yet"
            self.stale = False
            return
        if not self.stale:
            self._held_since = now
        app = self.appearance
        self.on, self.temp_k, self.gains = app.on, app.temp_k, app.gains
        self.source = app.source
        self.stale = True
        if now - self._held_since > self.hold_grace_s:
            self.detail = (f"holding last confirmed appearance past "
                           f"hold_grace_s={self.hold_grace_s:.0f}s; keeping it - "
                           f"a read failure is not the user turning night off ({why})")
        else:
            self.detail = f"holding last confirmed appearance ({why})"

    def lut(self, strength: float = 1.0) -> list[int] | None:
        """The panel's warmth: from the measured ramp if that is the evidence, else
        from the colour temperature the user set."""
        if self.gains is not None:
            return gains_lut(self.gains, strength)
        return warm_lut(self.temp_k, strength)

    def refresh(self, now: float | None = None) -> bool:
        """Re-read at most every `refresh_s`; True when the answer changed.

        `now` is a pinned monotonic clock for the deterministic tests; the
        live loop lets the module take it. A raising read is the same story
        as an undecodable one: hold the last confirmed appearance, because
        the failure mode of replacing it is a white flash at 3 a.m.
        """
        now = time.monotonic() if now is None else now
        if now < self._next:
            return False
        self._next = now + self.refresh_s
        was = (self.on, self.temp_k, self.source)
        try:
            self._read(now)
        except Exception as e:  # noqa: BLE001 - a settings store we cannot read is not fatal
            self._hold(now, f"read failed: {type(e).__name__}: {e}")
        self._reads += 1
        return (self.on, self.temp_k, self.source) != was

    def describe(self) -> str:
        return (f"night={'on' if self.on else ('off' if self.on is False else 'unknown')}"
                f"{'(stale)' if self.stale else ''} "
                f"src={self.source} temp={self.temp_k}K ({self.detail})")

    def changed_to_log(self) -> str | None:
        """A sentence per distinct state, so a wrong decode is visible in log.log."""
        key = (self.on, self.temp_k, self.source, self.detail)
        if key == self._said:
            return None
        self._said = key
        return "[night] " + self.describe()


def _mins(hm: tuple | None, default: int) -> int:
    return default if hm is None else hm[0] * 60 + hm[1]


def _hhmm(hm: tuple | None) -> str:
    """`(21, 30)` → `"21:30"`, `None` → `--:--`: log lines are read at arm's length,
    and a Python tuple full of commas does not scan as a time."""
    return "--:--" if hm is None else f"{hm[0]:02d}:{hm[1]:02d}"


def _in_window(mins: int, s: dict) -> bool:
    """True inside the schedule, including a window that wraps past midnight."""
    start = _mins(s.get("start"), 21 * 60) if s.get("set_hours", True) \
        else _mins(s.get("sunset"), 20 * 60)
    end = _mins(s.get("end"), 7 * 60) if s.get("set_hours", True) \
        else _mins(s.get("sunrise"), 7 * 60)
    return (mins >= start or mins < end) if start > end else (start <= mins < end)


def _in_clock_range(spec: str, now) -> bool:
    """`"21:00-07:00"` — the config-side fallback schedule."""
    try:
        a, b = spec.split("-", 1)
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
    except ValueError:
        return False
    mins, start, end = now.tm_hour * 60 + now.tm_min, ah * 60 + am, bh * 60 + bm
    return (mins >= start or mins < end) if start > end else (start <= mins < end)


# ------------------------------------------------------------------ the look
def gamma_gains() -> tuple[float, float, float] | None:
    """Mid-tone per-channel gains the OS is currently applying to the display.

    Measured on this desk: Windows Night light does *not* go through the gamma ramp
    (it stayed identity at 2525 K, applied further down the display pipeline), so
    this cannot confirm Night light. It is still worth reading, because f.lux,
    LightBulb and Twilight all do write the ramp, and the user asked to follow
    "night mode", not "the CloudStore key". A neutral ramp is honest evidence too —
    `NightLight` logs which evidence it acted on, so a wrong guess is visible.

    Returns None when there is no display to ask (service session, disconnected
    RDP, no window station) — never a fake neutral.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        u = ctypes.windll.user32
        g = ctypes.windll.gdi32

        class DeviceMode(ctypes.Structure):
            _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_uint16),
                        ("dmDriverVersion", ctypes.c_uint16), ("dmSize", ctypes.c_uint16),
                        ("dmDriverExtra", ctypes.c_uint16), ("dmFields", ctypes.c_uint32),
                        ("dmOrientation", ctypes.c_int16), ("dmPrintQuality", ctypes.c_int16),
                        ("dmColor", ctypes.c_int16), ("dmDuplex", ctypes.c_int16),
                        ("dmFormSize", ctypes.c_int16), ("dmNumpels", ctypes.c_int16),
                        ("dmDisplayFrequency", ctypes.c_uint32),
                        ("dmDisplayFixedOutput", ctypes.c_uint32),
                        ("dmDefaultSource", ctypes.c_uint32),
                        ("dmPositions", ctypes.c_ubyte * 32), ("dmDriver", ctypes.c_ubyte * 12),
                        ("dmSizeComplete", ctypes.c_uint32),
                        ("dmColorDepth", ctypes.c_uint16), ("dmDisplayOrientation", ctypes.c_uint16)]

        dm = DeviceMode()
        dm.dmSize = ctypes.sizeof(dm)
        dm.dmDriverExtra = 0
        if not g.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):   # ENUM_CURRENT_SETTINGS
            return None
        name = ("\\\\.\\" + dm.dmDeviceName) if dm.dmDeviceName else None
        hdc = g.CreateDCW("DISPLAY", name, None, None)
        if not hdc or hdc == -1:
            return None
        try:
            ramp = (ctypes.c_ushort * 768)()
            if not g.GetDeviceGammaRamp(ctypes.c_void_p(hdc), ctypes.byref(ramp)):
                return None
            mid = [ramp[c * 256 + 128] / 65535.0 for c in range(3)]      # 50% tone
        finally:
            g.DeleteDC(ctypes.c_void_p(hdc))
        peak = max(mid) or 1.0
        return tuple(max(0.0, min(1.0, v / peak)) for v in mid)     # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - a display we cannot measure is not an error
        return None


def gains_lut(gains: tuple[float, float, float], strength: float = 1.0) -> list[int] | None:
    """768-entry LUT straight from measured gains — the f.lux-shaped case, where the
    user's warmth is a measurement rather than a colour temperature."""
    return _lut([1.0 - s + s * float(g) for g in gains]
                if (s := max(0.0, min(1.0, float(strength)))) > 0 else (1.0, 1.0, 1.0))


def temp_gains(kelvin: float) -> tuple[float, float, float]:
    """Per-channel gains for a blackbody temperature (Helland's approximation).

    Normalised so the brightest channel is 1.0: night light removes light, it
    never adds it, and a gain above 1 would clip the white digits.
    """
    k = max(1000.0, min(10000.0, float(kelvin))) / 100.0
    if k <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(k) - 161.1195681661 if k > 0 else 0.0
    else:
        r = 329.698727446 * (k - 60.0) ** -0.1332047592
        g = 288.1223951229 * (k - 60.0) ** -0.0755148492
    if k >= 66:
        b = 255.0
    elif k <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(k - 10.0) - 305.0447927307
    vals = [max(0.0, min(255.0, v)) for v in (r, g, b)]
    peak = max(vals) or 1.0
    return tuple(min(1.0, max(0.04, v / peak)) for v in vals)


def _lut(gains: list[float]) -> list[int] | None:
    """Scale gains → 768-entry PIL point() LUT, or None if they are all ~1."""
    if all(abs(g - 1.0) < 0.01 for g in gains):
        return None
    lut: list[int] = []
    for gain in gains:
        lut.extend(max(0, min(255, int(round(v * gain)))) for v in range(256))
    return lut


def warm_lut(kelvin: float | None, strength: float = 1.0) -> list[int] | None:
    """768-entry PIL point() LUT (R, G, B), or None when there is nothing to do.

    A per-channel LUT is the whole point: it is exactly what a colour-temperature
    shift is, it costs one C call per frame, and it leaves the panel's near-black
    background near-black instead of lifting it like a blend would.
    """
    if not kelvin or strength <= 0.0:
        return None
    k = float(kelvin)
    if k >= 6400.0:
        return None
    s = max(0.0, min(1.0, float(strength)))
    return _lut([1.0 - s + s * g for g in temp_gains(k)])
