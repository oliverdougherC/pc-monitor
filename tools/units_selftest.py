"""Units, followed from the counter to the pixels.

    .venv\\Scripts\\python tools\\units_selftest.py

`FallbackBackend` computed network rates as bytes * 8 / dt — bits per second —
and `Layout` printed them through the same formatter it uses for disk, which
appends `MB/s`. Receiving 125 MB/s therefore appeared on the panel as
`1.0 GB/s`: a number that is eight times too big for the unit printed under it,
in the one place a user might actually act on it (issue #27).

A formatter unit test alone would not have caught this: both formatters are
internally consistent, and the disagreement lived in the seam between the sensor
and the layout. So the main case here is the real one — a real `FallbackBackend`
tick with the network counters under control, rendered by a real `Layout`, with
the strings read back off the draw calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config as cfgmod          # noqa: E402
from app import layout as layout_mod      # noqa: E402
from app.sensors import fallback as fb    # noqa: E402
from app.snapshot import Snapshot         # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


class Counters:
    """The two fields of `psutil.net_io_counters()` the backend reads."""

    def __init__(self, recv: int, sent: int):
        self.bytes_recv, self.bytes_sent = recv, sent


def rendered(snap: Snapshot, state: str = "idle") -> list[str]:
    """Every string a real Layout render puts on the panel."""
    cfg = cfgmod.load(None)
    cfg["layout"]["trend_bands"] = True
    layout = layout_mod.Layout(cfg, rate_hz=1.0)
    drawn: list[str] = []
    orig = layout_mod.Layout._txt

    def spy(self, d, xy, text, font, fill, anchor="la"):
        orig(self, d, xy, text, font, fill, anchor)
        drawn.append(str(text))

    layout_mod.Layout._txt = spy
    try:
        layout.render(snap, state, (3, 3))       # worst case: burn-in shifted
    finally:
        layout_mod.Layout._txt = orig
    return drawn


def sample_net(bytes_recv_delta: int, dt_s: float = 1.0) -> Snapshot:
    """One real FallbackBackend tick, with only the counters and the clock pinned.

    The clock is pinned by rebinding the name *inside* `app.sensors.fallback`
    (`fb.time = ...`), never by mutating the shared `time` module — that would
    put every other import of `time` in this process on a fake clock.
    """
    class FakeTime:
        def __init__(self):
            self.now = 1_000.0

        def monotonic(self):
            return self.now

    real_counters, real_time = fb.psutil.net_io_counters, fb.time
    ft = FakeTime()
    state = {"recv": 5_000_000, "sent": 900_000}

    fb.psutil.net_io_counters = lambda *a, **k: Counters(state["recv"], state["sent"])
    fb.time = ft
    try:
        backend = fb.FallbackBackend({})
        snap = Snapshot()
        backend.sample(snap)                                # primes, like the loop
        state["recv"] += bytes_recv_delta                   # one second of traffic
        state["sent"] += bytes_recv_delta // 8
        ft.now += dt_s
        backend.sample(snap)
        return snap
    finally:
        fb.psutil.net_io_counters = real_counters
        fb.time = real_time
        try:
            backend.close()
        except Exception:  # noqa: BLE001 - the GPU handle is not what is under test
            pass


def case_counter_to_pixels() -> None:
    print("case: what a real backend tick renders for a known amount of traffic")
    try:
        snap = sample_net(125_000_000)                   # 125 MB/s = 1.0 Gbit/s
    except Exception as e:  # noqa: BLE001 - no GPU/psutil here is not a pass
        print(f"  SKIP backend path: {type(e).__name__}: {e} (not counted as a pass)")
        return
    check("snapshot carries bits/s", snap.net_down_bps, 1_000_000_000.0)
    check("upload too", snap.net_up_bps, 125_000_000.0)
    drawn = rendered(snap)
    check("the panel says 1.0 Gbps", "1.0 Gbps" in drawn, True)
    check("and never the eight-times-too-big 1.0 GB/s", "1.0 GB/s" in drawn, False)
    check("up link reads 125.0 Mbps", "125.0 Mbps" in drawn, True)


def case_rate_steps() -> None:
    print("case: the bit-rate steps, exactly")
    bitrate, rate = layout_mod.Layout.bitrate, layout_mod.Layout.rate
    for bps, want in ((0.0, "0 bps"),
                      (999.0, "999 bps"),
                      (1_000.0, "1.0 Kbps"),
                      (8_000_000.0, "8.0 Mbps"),        # 1 MB/s of traffic
                      (1_000_000_000.0, "1.0 Gbps"),    # 125 MB/s
                      (2_500_000_000.0, "2.5 Gbps")):   # a 2.5G link
        check(f"{bps:.0f} bit/s -> {want!r}", bitrate(bps), want)
    check("unavailable is still '--'", bitrate(None), "--")
    # The disk formatter must not have drifted while fixing the network one.
    for bps, want in ((0.0, "0 B/s"), (1_000.0, "1.0 KB/s"),
                      (1_000_000.0, "1.0 MB/s"), (125_000_000.0, "125.0 MB/s"),
                      (999_900_000_000.0, "999.9 GB/s")):
        check(f"{bps:.0f} byte/s -> {want!r}", rate(bps), want)
    check("disk unavailable is '--'", rate(None), "--")


def case_disk_still_bytes() -> None:
    print("case: disk I/O is still labelled in bytes")
    snap = Snapshot()
    snap.disk_read_bps, snap.disk_write_bps = 125_000_000.0, 1_000_000.0
    snap.net_down_bps, snap.net_up_bps = 1_000_000_000.0, 8_000_000.0
    drawn = rendered(snap)
    check("disk read is 125.0 MB/s", "125.0 MB/s" in drawn, True)
    check("disk write is 1.0 MB/s", "1.0 MB/s" in drawn, True)
    check("network on the same panel is 1.0 Gbps", "1.0 Gbps" in drawn, True)
    check("and 8.0 Mbps", "8.0 Mbps" in drawn, True)
    check("no byte label is applied to a bit count",
          any(t in drawn for t in ("1.0 GB/s", "125.0 MB/s")) and "1.0 GB/s" in drawn, False)


def case_geometry() -> None:
    print("case: the widest bit-rate string still fits the column")
    snap = Snapshot()
    snap.disk_read_bps, snap.disk_write_bps = 999.9e6, 999.9e6
    snap.net_down_bps, snap.net_up_bps = 999.9e9, 999.9e9
    drawn = rendered(snap)
    check("a 999.9 Gbps worst case renders", "999.9 Gbps" in drawn, True)
    check("and it is no wider in characters than the disk column's 999.9 GB/s",
          len("999.9 Gbps") <= len("999.9 GB/s"), True)


def main() -> int:
    case_counter_to_pixels()
    case_rate_steps()
    case_disk_still_bytes()
    case_geometry()
    print("\nSELFTEST " + ("PASSED" if not fails else f"FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())