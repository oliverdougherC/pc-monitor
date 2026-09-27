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
"""
import argparse
import sys
import time

sys.path.insert(0, ".")          # our tree first: vendor has its own main.py
from app import config as cfgmod                  # noqa: E402
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
    """`_read_windows` with a pinned clock.

    Before the fix the method reads the wall clock and takes no clock argument,
    so the TypeError *is* the result: the contradiction the issue describes
    could not even be evaluated deterministically.
    """
    try:
        return n._read_windows(now=time.strptime(hhmm, "%H:%M"))
    except TypeError:
        return None, None, "no pinned clock", "TypeError"


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
    on, temp, src, det = read_at(fake_night(state_off, INNER_SETTINGS_SCHED), "22:00")
    check("OFF inside an open window stays OFF", on, False)
    check("answer came from the effective state", src, "windows")
    check("detail names the honoured override", "override" in det, True)
    check("warmth still comes from settings", temp, 2525)
    # …and turning it back on by hand inside the same window must go warm again.
    on, temp, src, det = read_at(fake_night(state_on, INNER_SETTINGS_SCHED), "22:00")
    check("manual ON inside the window is ON", on, True)
    check("still from the effective state", src, "windows")
    # Windows rewrites the state blob at each scheduled transition, so the
    # boundary crossings follow it, not a re-derived window.
    for hhmm, enabled in (("20:59", False), ("21:00", True), ("06:59", True), ("07:00", False)):
        on, temp, src, det = read_at(
            fake_night(state_on if enabled else state_off, INNER_SETTINGS_SCHED), hhmm)
        check(f"{hhmm}: follows the transition state", on, enabled)
        check(f"{hhmm}: provenance is the state", src, "windows")

    print("case: with no state blob, the schedule is the honest source — and says so")
    on, temp, src, det = read_at(fake_night(None, INNER_SETTINGS_SCHED), "22:00")
    check("inside window → on", on, True)
    check("provenance says schedule inference", src, "windows-schedule")
    on, temp, src, det = read_at(fake_night(None, INNER_SETTINGS_SCHED), "12:00")
    check("outside window → off", on, False)
    check("provenance still says schedule inference", src, "windows-schedule")

    print("case: unreadable endpoints → unknown, never an invented window")
    check("set-hours without start/end",
          nl._in_window(22 * 60, nl.parse_settings(INNER_SETTINGS_NO_HOURS)), None)
    check("sunset mode without sunset/sunrise",
          nl._in_window(22 * 60, nl.parse_settings(INNER_SETTINGS_NO_SUN)), None)
    on, temp, src, det = read_at(fake_night(None, INNER_SETTINGS_NO_HOURS), "22:00")
    check("no guess is made", on, None)
    check("provenance is unknown", src, "unknown")
    check("detail admits it did not guess", "not guessing" in det, True)
    on, temp, src, det = read_at(fake_night(None, None), "22:00")
    check("nothing readable → unknown", (on, src), (None, "unknown"))

    print("case: warmth follows the settings blob whatever the state says")
    check("2200 K decodes", nl.parse_settings(INNER_SETTINGS_WARM_2200).get("temp_k"), 2200)
    on, temp, src, det = read_at(fake_night(state_off, INNER_SETTINGS_WARM_2200), "22:00")
    check("temperature reported alongside an OFF state", temp, 2200)
    check("state still decides on/off", on, False)

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
