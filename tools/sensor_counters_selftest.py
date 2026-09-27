"""Disk and network counters re-prime; they do not raise, and they do not lie.

    .venv\\Scripts\\python tools\\sensor_counters_selftest.py

psutil hands back *sums of per-device counters*, and the old code subtracted the
previous sum from the current one and divided by however much time it thought had
passed. That is right until the machine stops being a single, never-changing set of
devices running without interruption — and a desk does all of: an adapter goes away,
a driver resets, the box hibernates, a counter path comes back `None`. Then the
subtraction produced one of two things, both reproduced here against the code as it
was:

  * `AttributeError: 'NoneType' object has no attribute 'read_bytes'`, on every tick
    from then on — a counter never re-primes itself, and because one `sample()` is one
    unit upstream, the raise discarded the CPU, memory and GPU readings that had been
    taken fine; the panel froze on its last snapshot while the log said nothing new;
  * a rate of `-7.2e13` bits/s, or a night of updates divided by the eight hours it
    took, both of which go straight to the panel.

So every case here drives a real `FallbackBackend.sample()` with only two things
under control — psutil's counters and the monotonic clock — and asserts one contract:
an interval that cannot be measured yields `None` (which the panel already draws as
`--`) and re-primed state, never a raise, never a negative, never a spike. The rate
comes back on the next real interval, and CPU/RAM are measured throughout.

The clock is pinned by rebinding the name *inside* `app.sensors.fallback`, never by
mutating the shared `time` module. Nothing here needs the vendored panel library, the
theme fonts, a GPU, or admin.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.sensors import fallback as fb    # noqa: E402
from app.snapshot import Snapshot         # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []

# Above this it is not a rate this desk can produce (10 Gbit/s; 10 GB/s for disk): it
# is what a bad subtraction looks like on the way to the panel.
SANE_MAX = 1e10


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def sane(v) -> bool:
    """A number the panel may print, or `None` for "not measurable this tick"."""
    return v is None or (isinstance(v, (int, float)) and 0.0 <= v < SANE_MAX)


class FakeTime:
    """A monotonic clock the case moves by hand, so a tick costs no wall time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


class Rig:
    """A `FallbackBackend` with the counters and the clock under control.

    The fake answers `pernic=`/`perdisk=` the way psutil does — a per-device dict when
    asked, one aggregate object when not — so one rig drives the code before the fix,
    which never asks for the device set, and after it, which does.
    """

    def __init__(self, cfg: dict | None = None, net: str = "ok", disk: str = "ok") -> None:
        self.nics = {"Ethernet": SimpleNamespace(bytes_recv=1_000, bytes_sent=500)}
        self.disks = {"PhysicalDrive0": SimpleNamespace(read_bytes=10_000,
                                                        write_bytes=5_000)}
        # Set before the backend is built: a counter that is already missing when the
        # backend starts is the reported shape, and a case that only breaks the
        # counters afterwards tests a different bug.
        self.net_state, self.disk_state = net, disk
        self.clock = FakeTime()
        self._real_psutil, self._real_time = fb.psutil, fb.time
        fb.psutil = SimpleNamespace(
            cpu_percent=lambda interval=None: 33.0,
            cpu_times_percent=lambda interval=None: None,
            cpu_freq=lambda: SimpleNamespace(current=3_000.0),
            virtual_memory=lambda: SimpleNamespace(total=64e9, available=48e9),
            net_io_counters=self.net,
            disk_io_counters=self.disk,
        )
        fb.time = self.clock
        self.backend = fb.FallbackBackend(cfg or {})
        self.backend._gpu = None        # the GPU driver is not what is under test

    def close(self) -> None:
        fb.psutil, fb.time = self._real_psutil, self._real_time

    # ---------------------------------------------------------------- the counters
    def net(self, pernic: bool = False):
        if self.net_state == "raises":
            raise OSError("dead network counter path")
        if self.net_state == "missing":
            return None
        if pernic:
            return dict(self.nics)
        return SimpleNamespace(bytes_recv=sum(c.bytes_recv for c in self.nics.values()),
                               bytes_sent=sum(c.bytes_sent for c in self.nics.values()))

    def disk(self, perdisk: bool = False):
        if self.disk_state == "raises":
            raise OSError("dead disk counter path")
        if self.disk_state == "missing":
            return None
        if perdisk:
            return dict(self.disks)
        return SimpleNamespace(read_bytes=sum(c.read_bytes for c in self.disks.values()),
                               write_bytes=sum(c.write_bytes for c in self.disks.values()))

    # -------------------------------------------------------------------- the moves
    def traffic(self, recv: int = 0, sent: int = 0, read: int = 0, write: int = 0,
                nic: str = "Ethernet", disk: str = "PhysicalDrive0") -> None:
        self.nics[nic].bytes_recv += recv
        self.nics[nic].bytes_sent += sent
        self.disks[disk].read_bytes += read
        self.disks[disk].write_bytes += write

    def add_adapter(self, name: str, recv: int, sent: int) -> None:
        self.nics[name] = SimpleNamespace(bytes_recv=recv, bytes_sent=sent)

    def drop_adapter(self, name: str) -> None:
        del self.nics[name]

    def tick(self, dt: float = 1.0) -> Snapshot:
        """One interval of the loop, by the pinned clock."""
        self.clock.now += dt
        snap = Snapshot()
        self.backend.sample(snap)
        return snap


def run(name: str, fn, net: str = "ok", disk: str = "ok") -> None:
    """A case that raises is a failed case, not a crashed run.

    That distinction is the whole point on the "before" side: the bug *is* a raise, so
    the case has to be able to say FAIL about it rather than end the file.
    """
    print(f"case: {name}")
    rig = None
    try:
        rig = Rig(net=net, disk=disk)
        fn(rig)
    except Exception as e:  # noqa: BLE001 - this raise is the fault under test
        fails.append(name)
        print(f"  FAIL {name}: sample() raised {type(e).__name__}: {e}")
    finally:
        if rig is not None:
            rig.close()
        print()


def telemetry_live(snap: Snapshot) -> bool:
    """CPU and RAM were measured this tick, whatever the counters did."""
    return snap.cpu.load_pct == 33.0 and snap.ram_used_mb is not None


# --------------------------------------------------------------------------- cases
def case_missing_then_valid_then_valid(rig: Rig) -> None:
    rig.disk_state = "missing"                 # no disk counters at start-up
    s0 = rig.tick()
    check("tick 1 draws no disk number", (s0.disk_read_bps, s0.disk_write_bps), (None, None))
    rig.disk_state = "ok"
    s1 = rig.tick()
    check("the first valid sample re-primes and still says nothing",
          (s1.disk_read_bps, s1.disk_write_bps), (None, None))
    rig.traffic(read=2_000, write=700)
    s2 = rig.tick()
    check("the next interval is a real rate", (s2.disk_read_bps, s2.disk_write_bps),
          (2000.0, 700.0))
    check("no tick raised, so CPU and RAM survived every one", telemetry_live(s2), True)


def case_net_missing_at_init(rig: Rig) -> None:
    rig.net_state = "missing"                  # the reported crash, for network
    rig.tick()
    rig.net_state = "ok"
    rig.tick()
    rig.traffic(recv=1_000, sent=125)
    s = rig.tick()
    check("network rates come back", (s.net_down_bps, s.net_up_bps), (8_000.0, 1_000.0))
    check("and they are bits/s, not bytes/s", s.net_down_bps, 1_000 * 8)


def case_valid_missing_restored(rig: Rig) -> None:
    rig.tick()
    rig.traffic(recv=800)
    check("a normal interval still measures", rig.tick().net_down_bps, 6_400.0)
    rig.net_state = "missing"
    s = rig.tick()
    check("a missing sample reports nothing", s.net_down_bps, None)
    check("and does not raise", telemetry_live(s), True)
    rig.net_state = "ok"
    s = rig.tick()
    check("the tick it returns on re-primes rather than spanning the gap",
          s.net_down_bps, None)
    rig.traffic(recv=2_000)
    check("and the next interval is a rate again", rig.tick().net_down_bps, 16_000.0)


def case_counter_decrease(rig: Rig) -> None:
    rig.tick()
    rig.traffic(recv=5_000, read=4_000)
    s = rig.tick()
    check("measured across a normal tick", (s.net_down_bps, s.disk_read_bps),
          (40_000.0, 4_000.0))
    # A driver reset: the counters start again from a smaller number, under the same
    # device names, so nothing but the subtraction itself looks wrong.
    rig.nics["Ethernet"] = SimpleNamespace(bytes_recv=10, bytes_sent=10)
    rig.disks["PhysicalDrive0"] = SimpleNamespace(read_bytes=10, write_bytes=10)
    s = rig.tick()
    check("a decreased counter is not a negative rate", (s.net_down_bps, s.disk_read_bps),
          (None, None))
    check("nothing absurd reached the panel",
          sane(s.net_down_bps) and sane(s.disk_read_bps), True)
    check("and the tick is not lost to an exception", telemetry_live(s), True)
    rig.traffic(recv=1_000, read=500)
    s = rig.tick()
    check("rates resume on the next fresh interval",
          (s.net_down_bps, s.disk_read_bps), (8_000.0, 500.0))


def case_device_removed(rig: Rig) -> None:
    rig.add_adapter("NordLynx", recv=9_000_000, sent=4_000_000)
    rig.tick()
    rig.traffic(recv=1_000, read=300)
    s = rig.tick()
    check("both adapters counted", s.net_down_bps, 8_000.0)
    check("disk measured in the same tick", s.disk_read_bps, 300.0)
    rig.drop_adapter("NordLynx")               # the aggregate drops by 9 MB
    rig.traffic(read=400)
    s = rig.tick()
    check("an adapter leaving is not a negative rate", s.net_down_bps, None)
    check("and not a spike either", sane(s.net_down_bps), True)
    check("disk was unaffected by the network topology change", s.disk_read_bps, 400.0)
    rig.traffic(recv=250)
    check("network resumes on a fresh interval", rig.tick().net_down_bps, 2_000.0)


def case_device_replaced(rig: Rig) -> None:
    rig.tick()
    rig.traffic(recv=1_000)
    check("measured before the swap", rig.tick().net_down_bps, 8_000.0)
    # The adapter re-enumerates under a new name carrying its own totals: the
    # aggregate only goes *up*, so nothing in the numbers themselves says this is not
    # traffic. Only the device set does, which is why the set is kept.
    rig.drop_adapter("Ethernet")
    rig.add_adapter("Ethernet 2", recv=9_000_000_000, sent=1_000)
    s = rig.tick()
    check("a replacement is not reported as a burst", s.net_down_bps, None)
    check("the spike never reaches the panel", sane(s.net_down_bps), True)
    rig.traffic(recv=100, nic="Ethernet 2")
    check("and the next interval is the real rate", rig.tick().net_down_bps, 800.0)


def case_suspend_gap(rig: Rig) -> None:
    rig.tick()
    rig.traffic(recv=1_000, read=200)
    s = rig.tick()
    check("measured before the suspend", (s.net_down_bps, s.disk_read_bps),
          (8_000.0, 200.0))
    rig.traffic(recv=40_000_000)               # what a night of updates looks like
    s = rig.tick(dt=8 * 3600.0)                # eight hours by the monotonic clock
    check("a suspend gap is dropped, not averaged into a plausible lie",
          s.net_down_bps, None)
    check("and the tick still carries the rest of the telemetry", telemetry_live(s), True)
    rig.traffic(recv=1_500)
    check("the next interval is a rate again", rig.tick().net_down_bps, 12_000.0)


def case_counter_path_raises(rig: Rig) -> None:
    rig.tick()
    rig.traffic(recv=1_000, read=1_000)
    s = rig.tick()
    check("measured before the counter path dies",
          (s.net_down_bps, s.disk_read_bps), (8_000.0, 1_000.0))
    rig.disk_state = "raises"
    s = rig.tick()
    check("a dead disk counter is not an exception", s.disk_read_bps, None)
    check("CPU and RAM are still measured", telemetry_live(s), True)
    check("network is still measured beside it", s.net_down_bps is not None, True)
    rig.disk_state = "ok"                          # the disk path comes back
    rig.tick()                                 # and re-primes after its outage
    rig.net_state = "raises"
    rig.traffic(read=600)
    s = rig.tick()
    check("a dead network counter is not an exception either", s.net_down_bps, None)
    check("disk keeps measuring through it", s.disk_read_bps, 600.0)
    check("CPU and RAM survive both", telemetry_live(s), True)


def case_other_telemetry_stays_live(rig: Rig) -> None:
    """The whole point, stated once: nothing else pays for a counter gap."""
    live = all(telemetry_live(rig.tick()) for _ in range(5))
    check("five ticks with both families missing, CPU/RAM live every one", live, True)
    rig.net_state = rig.disk_state = "ok"
    rig.tick()
    rig.traffic(recv=10, read=10)
    s = rig.tick()
    check("both rates come back together", (s.net_down_bps, s.disk_read_bps), (80.0, 10.0))


def main() -> int:
    # (title, case, how the counters are already behaving when the backend starts)
    for name, fn, rig_kw in (
        ("disk counters missing at start-up, then valid, then valid",
         case_missing_then_valid_then_valid, {"disk": "missing"}),
        ("network counters missing at start-up (the reported crash)",
         case_net_missing_at_init, {"net": "missing"}),
        ("valid, then missing, then restored", case_valid_missing_restored, {}),
        ("a counter that goes backwards (driver reset)", case_counter_decrease, {}),
        ("a device removed from the aggregate", case_device_removed, {}),
        ("a device replaced behind a new name", case_device_replaced, {}),
        ("a suspend gap must not be averaged into a lie", case_suspend_gap, {}),
        ("a counter path that raises instead of answering", case_counter_path_raises, {}),
        ("one I/O failure does not cost the other telemetry",
         case_other_telemetry_stays_live, {"net": "missing", "disk": "missing"}),
    ):
        run(name, fn, **rig_kw)
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
