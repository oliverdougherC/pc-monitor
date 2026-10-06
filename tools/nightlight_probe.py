"""Read, decode and self-test the Windows Night light registry blobs.

    .venv\\Scripts\\python tools\\nightlight_probe.py             # this machine, decoded
    .venv\\Scripts\\python tools\\nightlight_probe.py --watch 120  # follow a toggle
    .venv\\Scripts\\python tools\\nightlight_probe.py --selftest   # pinned fixtures

Night light has no public API, and `app/nightlight.py` reads the CloudStore blobs
that hold its state (Bond CompactBinary v1 inside a CloudStore wrapper). That is
reverse-engineered territory, so the decode is pinned to blobs captured on this
desk and to the annotated example from the format documentation: if a Windows
update changes the encoding, `--selftest` fails instead of the panel quietly
never going amber again. The schedule-on, override and missing-endpoint shapes
are re-encoded by hand into the captured blob's own frame (`wrap`), because the
combinations that matter — OFF inside an open window, a schedule whose endpoints
are gone — exist live only on a desk where someone has just changed Night light,
and the tests must never change this machine's.

`--watch` is for the verification you can only do by hand: run it, toggle Night
light in Quick Settings (or push its schedule past a transition), and watch
`enabled`/`temp` follow — the same values the panel will act on.

Two integration contracts are pinned here, because this file is where the merge
of #14 (manual override), #15 (hold the last confirmed look) and #16 (gamma-ramp
read) has to keep exactly one of each:

* `NightLight._read_windows` returns exactly five values —
  ``(on, temp_k, source, detail, state_mtime)`` (see `read_at`).
* `NightLight.lut(strength, temp_k)` renders the *effective* temperature the
  planner resolved; it makes no temperature decision of its own (issue #53).
  `gamma_gains()` returns a `RampReading`, never a bare tuple.

A third contract is about *sequence*, not one read (#68): the gamma ramp may only
take back the appearance the ramp itself published. Warm → neutral → warm, driven
through `_read` with `gamma_gains` replaced, must publish ON → OFF → ON; an
unreadable ramp must hold the last confirmed look instead; and a confirmed
Windows answer must survive a neutral ramp untouched. The defect lived in the
order of reads — the neutral read succeeded, changed nothing, and `_hold`
re-published the previous warm appearance — so a test of one reading at a time
could not have seen it, and the assertions below run the real sequence the poll
loop runs.
"""
import argparse
import sys
import time

sys.path.insert(0, ".")          # our tree first: vendor has its own main.py
from app import config as cfgmod                  # noqa: E402
from app import lights as lights_mod              # noqa: E402
from app import nightlight as nl                  # noqa: E402

sys.stdout.reconfigure(errors="replace")

# Captured on this desk, 2026-10-09, while Quick Settings showed Night light ON
# at 2525 K with no schedule — i.e. the manual toggle. Kept verbatim.
LIVE_STATE_ON = bytes.fromhex(
    "434201000A0201002A06F887D8D5062A2B0E15"
    "434201001000D00A02C614F6EFAB88D395D3EE0100000000")
LIVE_SETTINGS = bytes.fromhex(
    "434201000A0201002A06A38783D4062A2B0E21"
    "43420100CA140E1500CA1E0E0700CF28BA27CA320E142E0300CA3C0E062E1A00000000")

# The schedule side of the same encoding, re-encoded by hand into the frame
# `wrap()` builds: field 0 bool 1 (schedule on), field 10 bool 1 ("set hours"),
# start 21:00 and end 07:00 structs, temperature 2525 K. The captured blob
# above pins the field numbers and types; this one pins the shape Windows
# writes when the schedule is *on*, so the override and transition cases below
# have a real schedule to contradict. Re-encoding the captured frame is the
# honest stand-in for a second capture: producing these blobs live would mean
# changing this machine's Night light, which the tests must never do.
INNER_SETTINGS_SCHED = bytes.fromhex(
    "434201000201C20A01CA140E1500CA1E0E0700CF28BA2700")
# "Set hours" is on but fields 20/30 are missing — the endpoints cannot be read.
INNER_SETTINGS_NO_HOURS = bytes.fromhex("434201000201C20A01CF28BA2700")
# Sunset→sunrise mode (no field 10) with fields 50/60 missing: no window can
# honestly be evaluated from this, and inventing 20:00→07:00 would go amber on
# a guess about where the user lives.
INNER_SETTINGS_NO_SUN = bytes.fromhex("434201000201CF28BA2700")
# The schedule-on fixture after a warmth change (field 40 = 2200 K): the panel
# must read warmth from settings whatever the schedule or state says.
INNER_SETTINGS_WARM_2200 = bytes.fromhex(
    "434201000201C20A01CA140E1500CA1E0E0700CF28B02200")


def wrap(inner: bytes, ts: int = 1790313464) -> bytes:
    """Build the CloudStore wrapper around an inner Bond payload.

    Written byte for byte against the layout `unwrap()` documents — outer field 1 →
    struct{0: uint64 mtime, 1: struct{1: list<int8>}} — because the only honest way
    to test the decoder with a *modified* payload (night light off, a schedule on)
    is to re-encode the frame around it the way Windows does, including the missing
    closing BT_STOP it also writes.
    """
    n = len(inner)
    # The list is typed int8, but `unwrap` masks every element back to a byte, so
    # the original unsigned bytes are exactly what the reader expects to see.
    payload = bytes([0x2B, 0x0E, n]) + inner + b"\x00"
    container = bytes([0x06]) + _varint(ts) + bytes([0x2A]) + payload + b"\x00"
    outer = bytes([0x0A, 0x02, 0x01]) + b"\x00" + bytes([0x2A]) + container
    return b"CB\x01\x00" + outer


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _drop_field0(inner: bytes) -> bytes:
    """The state payload with field 0 removed — what Windows writes when the
    reduction is *not* applied: `initialized` and the FILETIME stay, the marker goes.

    The payload carries its own `CB\\x01\\x00` magic, so field 0 is at offset 4, not 0.
    """
    assert inner[:6] == bytes.fromhex("434201001000"), inner[:12].hex()
    return inner[:4] + inner[6:]


def blind_refresh(n, now: float):       # noqa: ANN001
    """`refresh` with a pinned monotonic clock.

    Before the fix `refresh` took no clock, so the TypeError *is* the result:
    "how long have we been holding" could not even be expressed, let alone
    tested.
    """
    try:
        return n.refresh(now=now)
    except TypeError:
        return None


def fake_night(state_inner: bytes | None = None, settings_inner: bytes | None = None):
    """A `NightLight` that reads the fixtures instead of the registry.

    `_blob` is the only door the reader opens onto Windows, so faking it replays
    any captured pair — including OFF-state-inside-an-open-window, a combination
    that exists live only on a desk whose Night light someone just turned off.
    """
    n = nl.NightLight({}, refresh_s=0.0)
    blobs: dict[str, bytes] = {}
    if state_inner is not None:
        blobs[nl._STATE_KEY] = wrap(state_inner)
    if settings_inner is not None:
        blobs[nl._SETTINGS_KEY] = wrap(settings_inner)
    n._blob = lambda key: blobs.get(key)
    return n


def read_at(n, hhmm: str):        # noqa: ANN001
    """`_read_windows` with a pinned clock, returning its ONE documented contract.

    ``(on, temp_k, source, detail, state_mtime)`` — five values, because the
    merged `_read_windows` publishes the CloudStore write time alongside the
    state (#15) *and* keeps the manual override authoritative (#14). The unpack
    here is the whole contract: a four-value reader no longer exists, so a
    regression that drops the timestamp fails loudly rather than silently.

    Before the #14 fix the method read the wall clock and took no clock
    argument, so the TypeError *is* the result: the contradiction the issue
    describes could not even be evaluated deterministically.
    """
    try:
        return n._read_windows(now=time.strptime(hhmm, "%H:%M"))
    except TypeError:
        return None, None, "no pinned clock", "TypeError", 0.0


def selftest() -> int:
    fails: list[str] = []

    def check(name, got, want):       # noqa: ANN001
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}"
              + ("" if ok else f" != {want!r}"))
        if not ok:
            fails.append(name)

    print("case: live blobs from this desk (night light ON, 2525 K, no schedule)")
    ts, inner = nl.unwrap(LIVE_STATE_ON)
    st = nl.parse_state(inner)
    check("state decoded", st is not None, True)
    check("enabled", (st or {}).get("enabled"), True)
    check("last_change is a real date", bool((st or {}).get("last_change")), True)
    check("wrapper timestamp is a 2026 unix time", 1760000000 < ts < 1900000000, True)
    _, inner2 = nl.unwrap(LIVE_SETTINGS)
    se = nl.parse_settings(inner2)
    check("settings decoded", se is not None, True)
    check("temperature", (se or {}).get("temp_k"), 2525)
    check("schedule enabled", (se or {}).get("schedule"), False)
    check("start", (se or {}).get("start"), (21, 0))
    check("end", (se or {}).get("end"), (7, 0))
    check("sunset", (se or {}).get("sunset"), (20, 3))
    check("sunrise", (se or {}).get("sunrise"), (6, 26))

    print("case: the same payload with the enabled marker removed")
    off = wrap(_drop_field0(inner))
    st2 = nl.parse_state(nl.unwrap(off)[1])
    check("still our schema", st2 is not None, True)
    check("enabled", (st2 or {}).get("enabled"), False)
    check("and the live blob still round-trips", nl.parse_state(nl.unwrap(
        wrap(inner))[1]).get("enabled"), True)

    print("case: garbage never raises")
    for bad in (b"", b"nope", b"CB\x01\x00\xff", LIVE_STATE_ON[:20],
                bytes(reversed(LIVE_STATE_ON))):
        try:
            out = None
            try:
                out = nl.parse_state(nl.unwrap(bad)[1])
            except nl.CBError:
                out = None
            check(f"blob {bad[:6]!r}… → None", out is None, True)
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL blob {bad[:6]!r}… raised {type(e).__name__}: {e}")
            fails.append("raises")

    print("case: schedule windows (sunset→sunrise and set hours, incl. wrap)")
    s = {"schedule": True, "set_hours": True, "start": (21, 0), "end": (7, 0)}
    check("22:00 inside", nl._in_window(22 * 60, s), True)
    check("03:00 inside (wrapped)", nl._in_window(3 * 60, s), True)
    check("12:00 outside", nl._in_window(12 * 60, s), False)
    s2 = {"schedule": True, "set_hours": True, "start": (9, 0), "end": (17, 0)}
    check("non-wrapping: 10:00 inside", nl._in_window(10 * 60, s2), True)
    check("non-wrapping: 18:00 outside", nl._in_window(18 * 60, s2), False)
    check("config fallback range", nl._in_clock_range("21:00-07:00",
                                                      time.strptime("23:00", "%H:%M")), True)
    check("config fallback outside", nl._in_clock_range("21:00-07:00",
                                                        time.strptime("09:00", "%H:%M")), False)

    print("case: a transient read failure keeps the last confirmed appearance (issue #15)")
    # `getattr(n, "stale", ...)`-style reads: before the fix these attributes
    # do not exist, and the selftest should report FAIL lines, not crash.
    inner_on = nl.unwrap(LIVE_STATE_ON)[1]
    inner_off = _drop_field0(inner_on)
    blobs = {nl._STATE_KEY: LIVE_STATE_ON, nl._SETTINGS_KEY: LIVE_SETTINGS}
    # check_gamma_ramp off: these cases pin the state-store behaviour, and the
    # gamma ramp is this desk's second opinion, not a fixture.
    n = nl.NightLight({"night": {"hold_grace_s": 300, "check_gamma_ramp": False}},
                      refresh_s=0.0)
    n._blob = lambda key: blobs.get(key)
    blind_refresh(n, 100.0)
    check("established from the live blob", (n.on, n.temp_k, n.source),
          (True, 2525, "windows"))
    check("the appearance records when Windows wrote the state",
          bool(getattr(n, "appearance", None))
          and 1760000000 < n.appearance.state_mtime < 1900000000, True)

    # The acceptance injections: a missing key, a torn/truncated blob, a read
    # exception, a settings-only failure. For every shape the panel must not
    # even briefly see day brightness: the held appearance keeps on/temp/gains
    # identical until a *confirmed* read replaces them.
    blobs.clear()
    blind_refresh(n, 103.0)
    check("missing keys hold the appearance",
          (n.on, n.temp_k, getattr(n, "stale", None)), (True, 2525, True))
    check("the detail explains the hold", "holding last confirmed" in n.detail, True)
    blobs[nl._STATE_KEY] = LIVE_STATE_ON[:20]        # torn / truncated wrapper
    blind_refresh(n, 106.0)
    check("a torn blob holds too",
          (n.on, n.temp_k, getattr(n, "stale", None)), (True, 2525, True))

    def boom(key):                                   # noqa: ANN001
        raise OSError("CloudStore key vanished mid-read")

    n._blob = boom
    blind_refresh(n, 109.0)
    check("a raising read holds too",
          (n.on, n.temp_k, getattr(n, "stale", None)), (True, 2525, True))
    check("and names the failure", "read failed" in n.detail, True)

    # Settings-only failure: the state is readable, the warmth is not. The
    # appearance must stay coherent - the new state never mixes with the old
    # temperature silently, and says where the warmth came from.
    n._blob = lambda key: blobs.get(key)
    blobs.clear()
    blobs[nl._STATE_KEY] = LIVE_STATE_ON
    blind_refresh(n, 112.0)
    check("new state, warmth held from the last good read",
          (n.on, n.temp_k, getattr(n, "stale", None)), (True, 2525, False))
    check("and the detail says so", "warmth held" in n.detail, True)

    # A confirmed OFF must still apply promptly, whatever came before it.
    blobs.clear()
    blobs[nl._STATE_KEY] = wrap(inner_off)
    blobs[nl._SETTINGS_KEY] = LIVE_SETTINGS
    blind_refresh(n, 115.0)
    check("a later confirmed OFF applies promptly",
          (n.on, n.temp_k, getattr(n, "stale", None)), (False, 2525, False))

    # Blind again after that, and the hold is the OFF: unknown must never
    # resurrect an old warmth.
    n._blob = lambda key: None
    check("the OFF is logged", n.changed_to_log() is not None, True)
    blind_refresh(n, 118.0)
    check("holding an OFF: still off, marked stale",
          (n.on, getattr(n, "stale", None)), (False, True))
    line = n.changed_to_log()
    check("entering the hold logs exactly one line",
          line is not None and "holding" in line, True)
    blind_refresh(n, 150.0)
    check("a steady failure logs nothing more", n.changed_to_log(), None)
    blind_refresh(n, 600.0)          # 482 s blind: past hold_grace_s
    check("grace expiry never brightens the panel", (n.on, n.temp_k), (False, 2525))
    line = n.changed_to_log()
    check("the expiry policy is stated once",
          line is not None and "hold_grace_s" in line, True)
    blind_refresh(n, 603.0)
    check("and then it stays quiet", n.changed_to_log(), None)

    print("case: startup with no prior valid state (nothing to hold)")
    m = nl.NightLight({"night": {"check_gamma_ramp": False}}, refresh_s=0.0)
    m._blob = lambda key: None
    blind_refresh(m, 10.0)
    check("startup with nothing known is unknown, not off",
          (m.on, m.source, getattr(m, "stale", None)), (None, "unknown", False))
    check("and says there was nothing to hold", "nothing confirmed to hold" in m.detail,
          True)
    check("no appearance was invented", getattr(m, "appearance", "x"), None)
    q = nl.NightLight({"night": {"mode": "on", "color_temp_k": 2700}}, refresh_s=0.0)
    q._blob = lambda key: None
    blind_refresh(q, 10.0)
    check("config mode on establishes an appearance",
          (q.on, q.temp_k, q.source), (True, 2700, "config"))

    print("case: the schedule fixture decodes as an enabled set-hours window")
    se2 = nl.parse_settings(INNER_SETTINGS_SCHED)
    check("schedule enabled", (se2 or {}).get("schedule"), True)
    check("set hours", (se2 or {}).get("set_hours"), True)
    check("start", (se2 or {}).get("start"), (21, 0))
    check("end", (se2 or {}).get("end"), (7, 0))
    check("temp", (se2 or {}).get("temp_k"), 2525)
    for hhmm, want in (("20:59", False), ("21:00", True), ("06:59", True), ("07:00", False)):
        h, m = (int(x) for x in hhmm.split(":"))
        check(f"{hhmm} in 21:00-07:00 window", nl._in_window(h * 60 + m, se2), want)

    print("case: a decoded OFF outranks an open schedule window (issue #14)")
    state_on = nl.unwrap(LIVE_STATE_ON)[1]
    state_off = _drop_field0(state_on)
    # The deterministic contradiction from the issue: the state blob says OFF
    # (field 0 absent — exactly what Windows writes when you turn Night light
    # off by hand for the rest of the window) while the schedule is open. The
    # OFF is the effective state and must win; OR-ing the window back in is
    # what kept the panel amber after the user had turned the light off.
    on, temp, src, det, _mtime = read_at(fake_night(state_off, INNER_SETTINGS_SCHED), "22:00")
    check("OFF inside an open window stays OFF", on, False)
    check("answer came from the effective state", src, "windows")
    check("detail names the honoured override", "override" in det, True)
    check("warmth still comes from settings", temp, 2525)
    # …and turning it back on by hand inside the same window must go warm again.
    on, temp, src, det, _mtime = read_at(fake_night(state_on, INNER_SETTINGS_SCHED), "22:00")
    check("manual ON inside the window is ON", on, True)
    check("still from the effective state", src, "windows")
    # Windows rewrites the state blob at each scheduled transition, so the
    # boundary crossings follow it, not a re-derived window.
    for hhmm, enabled in (("20:59", False), ("21:00", True), ("06:59", True), ("07:00", False)):
        on, temp, src, det, _mtime = read_at(
            fake_night(state_on if enabled else state_off, INNER_SETTINGS_SCHED), hhmm)
        check(f"{hhmm}: follows the transition state", on, enabled)
        check(f"{hhmm}: provenance is the state", src, "windows")

    print("case: with no state blob, the schedule is the honest source — and says so")
    on, temp, src, det, _mtime = read_at(fake_night(None, INNER_SETTINGS_SCHED), "22:00")
    check("inside window → on", on, True)
    check("provenance says schedule inference", src, "windows-schedule")
    on, temp, src, det, _mtime = read_at(fake_night(None, INNER_SETTINGS_SCHED), "12:00")
    check("outside window → off", on, False)
    check("provenance still says schedule inference", src, "windows-schedule")

    print("case: unreadable endpoints → unknown, never an invented window")
    check("set-hours without start/end",
          nl._in_window(22 * 60, nl.parse_settings(INNER_SETTINGS_NO_HOURS)), None)
    check("sunset mode without sunset/sunrise",
          nl._in_window(22 * 60, nl.parse_settings(INNER_SETTINGS_NO_SUN)), None)
    on, temp, src, det, _mtime = read_at(fake_night(None, INNER_SETTINGS_NO_HOURS), "22:00")
    check("no guess is made", on, None)
    check("provenance is unknown", src, "unknown")
    check("detail admits it did not guess", "not guessing" in det, True)
    on, temp, src, det, _mtime = read_at(fake_night(None, None), "22:00")
    check("nothing readable → unknown", (on, src), (None, "unknown"))

    print("case: warmth follows the settings blob whatever the state says")
    check("2200 K decodes", nl.parse_settings(INNER_SETTINGS_WARM_2200).get("temp_k"), 2200)
    on, temp, src, det, _mtime = read_at(fake_night(state_off, INNER_SETTINGS_WARM_2200), "22:00")
    check("temperature reported alongside an OFF state", temp, 2200)
    check("state still decides on/off", on, False)

    print("case: _read_windows publishes exactly ONE contract (5 values)")
    got = read_at(fake_night(state_on, INNER_SETTINGS_SCHED), "22:00")
    check("five values, in the documented order", len(got), 5)
    check("the last one is the CloudStore write time", 1760000000 < got[4] < 1900000000, True)
    check("and the first four are still on/temp/source/detail",
          (got[0], got[1], got[2], isinstance(got[3], str)), (True, 2525, "windows", True))

    print("case: lut() renders the temperature it is handed (issue #53)")
    # The defect: the planner resolved 2700 K for *reporting* while `lut()` asked
    # its own `None` temperature and returned no LUT, so the panel reported a
    # night temperature and rendered nothing. There is one temperature decision
    # and it arrives as this argument; no independent `self.temp_k` read is left.
    bare = nl.NightLight({}, refresh_s=0.0)
    check("no effective temperature handed in → no LUT", bare.lut(1.0), None)
    check("the planner's 2700 K fallback renders",
          bare.lut(1.0, 2700), nl.warm_lut(2700, 1.0))
    check("a 2700 K LUT is not None", bare.lut(1.0, 2700) is not None, True)
    check("a warmer effective temperature warms further",
          bare.lut(1.0, 2200)[767] < bare.lut(1.0, 2700)[767], True)
    check("no gains, no temperature → still nothing", bare.lut(1.0, None), None)

    print("case: a neutral ramp clears the ramp-owned look, and only that (issue #68)")
    # Warm and neutral are the SAME display measured twice, which is the live
    # shape: f.lux and LightBulb write the ramp, so a neutral reading is the
    # user having turned their warmer off. The gains below are the shape
    # `gamma_gains()` returns (peak-normalised mid-tones).
    warm = nl.RampReading((1.0, 0.72, 0.55), r"\\.\DISPLAY1", None)
    neutral = nl.RampReading((1.0, 0.99, 0.98), r"\\.\DISPLAY1", None)
    unreadable = nl.RampReading(None, None, "no active display device")
    check("a measured neutral carries gains and no reason",
          (neutral.gains is not None, neutral.reason), (True, None))
    check("an unreadable ramp carries the reason and no gains",
          (unreadable.gains, bool(unreadable.reason)), (None, True))

    real_ramp = nl.gamma_gains

    # `fake_night(None, None)` has no CloudStore state at all, so `on is None`
    # on every read below and the ramp is the only voice in the room - exactly
    # the path the defect was on. `gamma_gains` is the module global `_read`
    # calls, so replacing it drives the real method, not a copy of its logic.
    n = fake_night(None, None)

    def read_ramp(reading, t):            # noqa: ANN001, ANN202
        nl.gamma_gains = lambda *a, **k: reading
        n._read(t)

    # The render side, taken from the one place `night.on` becomes a brightness
    # and a LUT (`lights.LightPlanner`): an appearance that was cleared has to
    # change what the panel draws, or "the look went away" is only a log line.
    panel_cfg = {"display": {"brightness_idle": 45, "brightness_game": 70,
                             "brightness_dim": 12, "dim_after_s": 300,
                             "screen_off_after_min": 45, "stay_lit_in_game": True},
                 "night": {"mode": "auto", "strength": 1.0, "color_temp_k": 0,
                           "brightness_scale": 0.55, "brightness_floor": 8},
                 "power": {}}
    planner = lights_mod.LightPlanner(panel_cfg, night=n)

    def panel():                          # noqa: ANN202
        plan = planner.tick("idle", 0.0, 1.0)
        return plan.brightness, plan.lut

    # The Windows side of the same question: a state blob that says ON, and a
    # neutral ramp that must not touch it.
    w_blobs = {nl._STATE_KEY: LIVE_STATE_ON}
    w = nl.NightLight({"night": {"check_gamma_ramp": True}}, refresh_s=0.0)
    w._blob = lambda key: w_blobs.get(key)
    try:
        read_ramp(warm, 1.0)
        check("warm ramp: the ramp owns the look", (n.on, n.source), (True, "ramp"))
        check("with the measured gains the night LUT renders from",
              n.gains, warm.gains)
        check("and the detail names the warm measurement", "warm" in n.detail, True)
        bright, lut = panel()
        check("panel side: night ON dims the panel and pushes a LUT",
              (bright, lut is not None), (25, True))
        check("... the ramp's own LUT, not a colour-temperature one",
              lut[767], nl.gains_lut(warm.gains, 1.0)[767])

        read_ramp(neutral, 2.0)
        check("the same display measured neutral: night goes OFF",
              (n.on, n.source), (False, "ramp"))
        check("the detail says the ramp cleared its own look",
              "cleared the ramp-owned appearance" in n.detail, True)
        check("and says it was a measurement, not a failure to measure",
              ("neutral (measured)" in n.detail, "unreadable" in n.detail),
              (True, False))
        check("the measured gains go with it", n.gains, None)
        check("it is a confirmed OFF, not a hold", n.stale, False)
        bright, lut = panel()
        check("panel side: brightness comes back and the LUT is dropped",
              (bright, lut is None), (45, True))

        read_ramp(warm, 3.0)
        check("turning it back on: ON again, from the ramp",
              (n.on, n.source, n.gains), (True, "ramp", warm.gains))
        bright, lut = panel()
        check("panel side: dimmed and warm again",
              (bright, lut is not None), (25, True))

        read_ramp(unreadable, 4.0)
        check("an UNREADABLE ramp still holds the warm look - never an OFF",
              (n.on, n.source, n.stale), (True, "ramp", True))
        check("and says it could not ask the display",
              "ramp unreadable" in n.detail, True)
        bright, lut = panel()
        check("panel side: the held look keeps its brightness and its LUT",
              (bright, lut is not None), (25, True))

        read_ramp(neutral, 5.0)
        check("a later measured neutral still clears it",
              (n.on, n.source), (False, "ramp"))

        # Startup is the fourth shape: nothing to hold yet, so a *measured*
        # neutral and a failed read must not collapse into the same answer.
        fresh_n = fake_night(None, None)
        nl.gamma_gains = lambda *a, **k: neutral
        fresh_n._read(0.5)
        check("a neutral ramp at startup is unknown, not an OFF",
              (fresh_n.on, fresh_n.source), (None, "unknown"))
        check("... and its detail still reports the measurement",
              "neutral (measured)" in fresh_n.detail, True)
        blind_n = fake_night(None, None)
        nl.gamma_gains = lambda *a, **k: unreadable
        blind_n._read(0.5)
        check("... while an unreadable ramp at startup carries the reason",
              (blind_n.on, "neutral (measured)" in blind_n.detail,
               "ramp unreadable" in blind_n.detail), (None, False, True))

        # A confirmed Windows answer is not the ramp's to clear: the effective
        # state says ON, and the published appearance came from Windows.
        nl.gamma_gains = lambda *a, **k: neutral
        w._read(1.0)
        check("Windows' own ON is the published look",
              (w.on, w.source), (True, "windows"))
        check("a neutral ramp adds no warmth to it", w.gains, None)
        check("and is not allowed to clear it", w.appearance.source, "windows")
        w_blobs.clear()               # CloudStore goes unreadable mid-poll
        w._read(2.0)
        check("a windows-owned look is not the ramp's business: it holds, ON",
              (w.on, w.source, w.stale), (True, "windows", True))
    finally:
        nl.gamma_gains = real_ramp

    print("case: the warm LUT is a colour temperature, not a hue tint")
    g65 = nl.temp_gains(6500)
    check("6500 K ≈ neutral", max(abs(x - 1.0) for x in g65) < 0.02, True)
    prev = None
    for k in (1500, 2000, 2500, 3000, 4000, 5000, 6500):
        gains = nl.temp_gains(k)
        check(f"{k} K: red >= blue", gains[0] >= gains[2], True)
        if prev is not None:
            check(f"{k} K: no less blue than {prev[0]} K", gains[2] >= prev[1], True)
        prev = (k, gains[2])
    check("2500 K is visibly amber", nl.temp_gains(2500)[2] < 0.45, True)
    check("LUT keeps black black", nl.warm_lut(2500, 1.0)[0] == 0, True)
    check("LUT length 768", len(nl.warm_lut(2500, 1.0)), 768)
    check("6500 K → no LUT at all", nl.warm_lut(6500, 1.0), None)
    check("strength 0 → no LUT", nl.warm_lut(2000, 0.0), None)
    lut = nl.warm_lut(2500, 1.0)
    check("LUT is monotonic per channel", all(lut[i] <= lut[i + 1] for i in range(255)), True)

    print("\n" + ("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}"))
    return 1 if fails else 0


def show(n: nl.NightLight, tag: str = "") -> None:
    n.refresh()
    raw_state = n._blob(nl._STATE_KEY)
    raw_set = n._blob(nl._SETTINGS_KEY)
    print(f"{tag}{n.describe()}")
    for name, blob in (("state", raw_state), ("settings", raw_set)):
        if not blob:
            print(f"    {name:8s} (absent)")
            continue
        try:
            ts, inner = nl.unwrap(blob)
            parsed = (nl.parse_state(inner) if name == "state"
                      else nl.parse_settings(inner))
            print(f"    {name:8s} {len(blob)} B  written "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}  {parsed}")
        except nl.CBError as e:
            print(f"    {name:8s} {len(blob)} B  UNDECODABLE: {e}")
    lut = nl.warm_lut(n.temp_k or 0, float(n.cfg.get("strength", 1.0)))
    if lut and n.on:
        print(f"    gains  {nl.temp_gains(n.temp_k)} → panel LUT "
              f"R255={lut[255]} G255={lut[511]} B255={lut[767]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--watch", type=float, default=0.0, help="seconds to follow toggles")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    cfg = cfgmod.load(None)
    n = nl.NightLight(cfg, refresh_s=0.0)
    show(n)
    if a.watch <= 0:
        print("\n(toggle Night light in Quick Settings and re-run, or use --watch 120)")
        return 0
    print(f"watching {a.watch:.0f}s — toggle Night light now")
    last = None
    t_end = time.monotonic() + a.watch
    while time.monotonic() < t_end:
        if n.refresh():
            show(n, "→ ")
            last = n.on
        time.sleep(0.5)
    print(f"stopped: last state {'changed' if last is not None else 'unchanged'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
