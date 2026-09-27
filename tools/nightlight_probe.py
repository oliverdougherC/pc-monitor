"""Read, decode and self-test the Windows Night light registry blobs.

    .venv\\Scripts\\python tools\\nightlight_probe.py             # this machine, decoded
    .venv\\Scripts\\python tools\\nightlight_probe.py --watch 120  # follow a toggle
    .venv\\Scripts\\python tools\\nightlight_probe.py --selftest   # pinned fixtures

Night light has no public API, and `app/nightlight.py` reads the CloudStore blobs
that hold its state (Bond CompactBinary v1 inside a CloudStore wrapper). That is
reverse-engineered territory, so the decode is pinned to blobs captured on this
desk and to the annotated example from the format documentation: if a Windows
update changes the encoding, `--selftest` fails instead of the panel quietly
never going amber again.

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
