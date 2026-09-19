"""Sensor backends.

Backend selection:
  lhm      - LibreHardwareMonitorLib via pythonnet: full fidelity
             (package power, per-core clocks, Tctl temp, GPU detail). Needs admin.
  fallback - psutil + NVML: no admin, real host metrics, GPU via NVML.
             CPU temp/power/per-core clocks unavailable → None (rendered as "--").
  demo     - synthetic data to develop/preview the UI.
  auto     - lhm if importable, else fallback.
"""
from __future__ import annotations

import time

from app.snapshot import Snapshot


class SensorHub:
    """Wraps a backend; emits one Snapshot per tick()."""

    def __init__(self, backend):
        self.backend = backend

    def tick(self) -> Snapshot:
        snap = Snapshot(ts=time.time())
        self.backend.sample(snap)
        return snap


def make_hub(cfg: dict, force: str | None = None) -> SensorHub:
    want = (force or cfg["sensors"]["backend"]).lower()

    if want in ("auto", "lhm"):
        try:
            from app.sensors.lhm import LhmBackend
            return SensorHub(LhmBackend(cfg))
        except Exception as e:  # noqa: BLE001 - fallback by design
            if want == "lhm":
                raise
            print(f"[sensors] LHM unavailable ({e.__class__.__name__}: {e}); using fallback")

    if want in ("auto", "fallback"):
        from app.sensors.fallback import FallbackBackend
        return SensorHub(FallbackBackend(cfg))

    if want == "demo":
        from app.sensors.demo import DemoBackend
        return SensorHub(DemoBackend(cfg))

    raise ValueError(f"unknown sensor backend: {want}")
