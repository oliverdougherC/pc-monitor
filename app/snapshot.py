"""Sensor snapshot: a single immutable-ish view of all telemetry at one instant."""
from dataclasses import dataclass, field


@dataclass
class CpuStats:
    load_pct: float | None = None        # total load 0-100
    temp_c: float | None = None          # package temp
    clock_max_mhz: float | None = None   # fastest active core (single-thread peak)
    clock_avg_mhz: float | None = None   # average across all threads
    power_w: float | None = None         # package power (sensor or estimate)


@dataclass
class GpuStats:
    load_pct: float | None = None
    temp_c: float | None = None
    core_mhz: float | None = None
    power_w: float | None = None
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None


@dataclass
class FrameStats:
    fps: float | None = None
    low1_pct: float | None = None        # 1% low
    low01_pct: float | None = None       # 0.1% low
    latency_ms: float | None = None      # avg frame latency
    # True when these are the *last measured* numbers rather than live ones: the
    # game is alive but has stopped presenting (minimised, alt-tabbed, a loading
    # screen that renders nothing). The panel renders them dimmed instead of
    # inventing a live rate, and `age_s` says how stale they are.
    stale: bool = False
    age_s: float = 0.0
    # True when these numbers were invented for a preview rather than measured. It
    # rides with the values themselves so the renderer cannot draw them as live:
    # `app/layout.py` marks such a pane SIMULATED, and a preview's status line reads
    # the same flag off the same snapshot. A measurement never sets it.
    simulated: bool = False
    gpu_pct: float | None = None         # this process's own GPU busyness, %


@dataclass
class Snapshot:
    ts: float = 0.0
    cpu: CpuStats = field(default_factory=CpuStats)
    gpu: GpuStats = field(default_factory=GpuStats)
    ram_used_mb: float | None = None
    ram_total_mb: float | None = None
    disk_read_bps: float | None = None      # bytes/s  (formatted by Layout.rate)
    disk_write_bps: float | None = None     # bytes/s  (formatted by Layout.rate)
    # Network is quoted the way links are quoted: bits per second, and formatted by
    # Layout.bitrate. The two families differ by a factor of eight, so the suffix
    # means the unit and not "per second" — sending them through one formatter is
    # what printed "1.0 GB/s" for 125 MB/s (issue #27).
    net_down_bps: float | None = None       # bits/s   (formatted by Layout.bitrate)
    net_up_bps: float | None = None         # bits/s   (formatted by Layout.bitrate)
    frames: FrameStats = field(default_factory=FrameStats)
    power_total_w: float | None = None
    # Telemetry provenance, filled by SensorHub (app/sensors/__init__.py):
    # `source` names the backend that answered; `held` marks a re-published
    # last-good sample (a blind tick), with `age_s` how old it is; `failed`
    # names the metric groups that did not answer this tick. A held sample is
    # rendered but never pushed into history, and a failed group is None - a
    # gap - because the honest alternative is drawing old data as a flat,
    # measured line.
    source: str = ""
    held: bool = False
    age_s: float = 0.0
    failed: tuple[str, ...] = ()
