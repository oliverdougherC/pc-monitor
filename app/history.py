"""Fixed-window ring buffers feeding the 60s trend lines in the layouts.

Sensors tick at `sensors.interval_s` (1 Hz by default), so `trend_window_s /
interval_s` samples ≈ that many seconds of history. Everything is bounded:
maxlen on the deque, fixed sample count, no growth over time.

Missing samples (no sensor, e.g. CPU temp on the psutil fallback) are stored as
None and drawn as gaps rather than interpolated — a trend line must never imply
data we do not have.
"""
from __future__ import annotations

from collections import deque


class Ring:
    """Fixed-length series; newest sample last."""

    __slots__ = ("d",)

    def __init__(self, n: int):
        self.d: deque[float | None] = deque(maxlen=max(2, int(n)))

    def push(self, v: float | None) -> None:
        self.d.append(None if v is None else float(v))

    def series(self) -> list[float | None]:
        return list(self.d)

    def last(self) -> float | None:
        for v in reversed(self.d):
            if v is not None:
                return v
        return None

    def span(self, min_span: float = 0.0) -> tuple[float, float] | None:
        """min/max of the window, widened to at least `min_span` so flat data
        does not make one degree of noise fill the graph."""
        vals = [v for v in self.d if v is not None]
        if len(vals) < 2:
            return None
        lo, hi = min(vals), max(vals)
        if hi - lo < min_span:
            mid = (hi + lo) / 2.0
            lo, hi = mid - min_span / 2.0, mid + min_span / 2.0
        return lo, hi


class History:
    """One named Ring per metric, all the same length."""

    def __init__(self, samples: int):
        self.samples = max(2, int(samples))
        self._rings: dict[str, Ring] = {}

    def ring(self, name: str) -> Ring:
        r = self._rings.get(name)
        if r is None:
            r = self._rings[name] = Ring(self.samples)
        return r

    def push(self, name: str, v: float | None) -> None:
        self.ring(name).push(v)

    def series(self, name: str) -> list[float | None]:
        return self.ring(name).series()

    def span(self, name: str, min_span: float = 0.0):
        return self.ring(name).span(min_span)

    def last(self, name: str) -> float | None:
        return self.ring(name).last()
