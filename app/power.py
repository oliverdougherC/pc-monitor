"""Power estimation + green->red gradient coloring.

There is no true "system watt" without an inline meter, so every whole-system
number here is a model:
  total = base (mobo/ram/fans/ssd + psu losses) + cpu + gpu
Where package-power sensors exist they are used directly; otherwise a
load-curved TDP model gives an educated guess. No calibration accuracy has been
measured for the configured system, so the answer carries its own story instead
of promising a tolerance: `estimate()` returns a PowerEstimate whose total is
None whenever the completeness policy says we cannot honestly name one, whose
`sources` say per component whether the figure came from a sensor or from the
load model, and whose `age_s`/`stale` say how old the telemetry was when asked.
A component the sensors cannot see is absent from the answer, never a silent
zero - a machine we cannot read is not a machine that draws nothing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.snapshot import Snapshot

# Per-component provenance words.
MEASURED = "measured"
MODELLED = "modelled"
UNKNOWN = "unknown"


@dataclass
class PowerEstimate:
    """One whole-system power answer, with the provenance it deserves.

    `total_w` is None when there is no honest total: nothing known at all, or
    - under the default `power.require_complete` policy - not every component
    known. `sources` records per component whether the figure was measured at
    a sensor or modelled from load. `age_s`/`stale` carry how old the snapshot
    was when asked (None/False when it carries no timestamp to age against).
    `status` is the one word the power strip draws beside the number, so the
    panel never shows a bare watt figure whose confidence the viewer has to
    guess.
    """
    total_w: float | None
    sources: dict[str, str]
    complete: bool
    age_s: float | None
    stale: bool
    status: str
    parts: dict = field(default_factory=dict)


def _model(load01: float, tdp: float, idle_w: float) -> float:
    # idle floor + superlinear rise toward TDP
    return idle_w + (tdp - idle_w) * (max(0.0, min(1.0, load01)) ** 1.25)


def _component(power_w, load_pct, tdp: float, idle_w: float):
    """One component's watts and where they came from: sensor, model, or nothing.

    "Nothing" is None, not 0.0. The zero was the bug: it let a completely
    unreadable machine total to `base_w` and read as a healthy idle.
    """
    if power_w is not None:
        return float(power_w), MEASURED
    if load_pct is not None:
        return _model(float(load_pct) / 100.0, tdp, idle_w), MODELLED
    return None, UNKNOWN


def estimate(snap: Snapshot, cfg: dict, now: float | None = None) -> PowerEstimate:
    """Model the whole-system draw from what the snapshot actually knows.

    `now` is the wall clock to age the snapshot against (defaults to the real
    one; tests and back-computation pass it explicitly). The total is governed
    by `power.require_complete`: by default both components must be known,
    because a total that silently excludes one real load reads as "34 W idle"
    on a machine nowhere near idle. With the policy relaxed, a one-sided total
    is allowed as an explicit floor - and the status still says "partial".
    With nothing known there is no total under any policy: base_w alone is the
    falsely reassuring number this module exists to stop producing.
    """
    p = cfg["power"]
    base = float(p["base_w"])  # non-CPU/GPU rails: board, RAM, SSD, fans, pump, misc
    require_complete = bool(p.get("require_complete", True))
    max_age_s = float(p.get("max_age_s", 8.0))

    # The idle floors are model constants, not measurements: GPUs sit at
    # ~20-35 W even at 0% reported load, and a modern desktop package at a
    # little under 20 W with the display on.
    cpu_w, cpu_src = _component(snap.cpu.power_w, snap.cpu.load_pct,
                                float(p["cpu_tdp"]), 18.0)
    gpu_w, gpu_src = _component(snap.gpu.power_w, snap.gpu.load_pct,
                                float(p["gpu_tdp"]), 28.0)

    age_s: float | None = None
    stale = False
    if snap.ts > 0.0:
        # A snapshot with no timestamp (hand-built, or from a source that does
        # not stamp one) has no age to claim: freshness stays unknown rather
        # than being invented as fresh - or aged into stale against clock zero.
        age_s = max(0.0, (now if now is not None else time.time()) - snap.ts)
        stale = age_s > max_age_s

    known = [w for w in (cpu_w, gpu_w) if w is not None]
    complete = len(known) == 2
    # CPU/GPU sensor values are measured at the socket / card connector;
    # VRM conversion (~5%) and PSU losses (~4%) happen upstream of them.
    rail_overhead = 1.0 + float(p.get("rail_overhead_pct", 0)) / 100.0
    total: float | None = None
    if known:
        total = base + sum(known) * rail_overhead
        if require_complete and not complete:
            total = None

    if total is None:
        status = "unavailable" if not known else "partial"
    elif not complete:
        status = "partial"        # a floor, with the policy relaxed
    elif stale:
        status = "stale"
    elif MODELLED in (cpu_src, gpu_src):
        status = "modelled input"
    else:
        status = "measured input"

    return PowerEstimate(
        total_w=total,
        sources={"cpu": cpu_src, "gpu": gpu_src},
        complete=complete,
        age_s=age_s,
        stale=stale,
        status=status,
        parts={"base": base, "cpu": cpu_w, "gpu": gpu_w,
               "overhead_pct": rail_overhead - 1.0},
    )


def power_color(watts: float, cfg: dict) -> tuple[int, int, int]:
    """Green at gradient_min_w -> red at gradient_max_w (HSV hue sweep)."""
    p = cfg["power"]
    lo, hi = float(p["gradient_min_w"]), float(p["gradient_max_w"])
    t = max(0.0, min(1.0, (watts - lo) / (hi - lo)))
    t = t ** float(p.get("gradient_gamma", 0.75))
    hue = (1.0 - t) * (130.0 / 360.0)  # 130deg green -> 0deg red
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
    return (round(r * 255), round(g * 255), round(b * 255))


def power_watts_for_display(snap: Snapshot, cfg: dict,
                            now: float | None = None) -> float | None:
    """The number for callers that only want the number.

    Always recomputed from the snapshot's own fields rather than read back out
    of `snap.power_total_w`: the stored figure is the loop's answer at tick
    time, and what the panel may show is a question about the fields at render
    time. None means "no honest total" - never a base-only stand-in, and never
    a total that quietly excludes an unreadable component.
    """
    return estimate(snap, cfg, now=now).total_w