"""Diff-based partial-update transport.

The panel only accepts bitmaps, but we can push *regions*. We render the full
logical frame each tick (cheap at 1 Hz), diff against the previous frame with
numpy, and send back the smallest set of horizontal bands that changed.
- Static labels: never re-sent.
- A changing digit: a few hundred bytes instead of a full frame.
- Mode switch / shift animation: falls back to one full-frame push.
"""
from __future__ import annotations

import numpy as np


def _runs(indices: np.ndarray) -> list[tuple[int, int]]:
    """[1,2,3,7,9,10] -> [(1,4),(7,8),(9,11)]  (start, end-exclusive)"""
    if indices.size == 0:
        return []
    split = np.flatnonzero(np.diff(indices) > 1)
    starts = np.concatenate(([0], split + 1))
    ends = np.concatenate((split + 1, [indices.size]))
    return [(int(indices[s]), int(indices[e - 1] + 1)) for s, e in zip(starts, ends)]


class DiffPusher:
    def __init__(self, lcd, merge_gap: int = 6, full_fraction: float = 0.35):
        self.lcd = lcd
        self.prev: np.ndarray | None = None
        self.merge_gap = merge_gap
        self.full_fraction = full_fraction

    def push(self, img) -> None:
        new = np.asarray(img, dtype=np.uint8)
        if self.prev is not None and self.prev.shape == new.shape:
            changed = np.any(new != self.prev, axis=2)
            frac = changed.mean()
            if frac == 0.0:
                return
            if frac <= self.full_fraction:
                row_idx = np.flatnonzero(changed.any(axis=1))
                bands = _runs(row_idx)
                # merge bands separated by tiny gaps
                merged: list[list[int]] = [list(bands[0])]
                for s, e in bands[1:]:
                    if s - merged[-1][1] <= self.merge_gap:
                        merged[-1][1] = e
                    else:
                        merged.append([s, e])
                if len(merged) <= 6:
                    for y0, y1 in merged:
                        col_idx = np.flatnonzero(changed[y0:y1].any(axis=0))
                        x0, x1 = int(col_idx[0]), int(col_idx[-1]) + 1
                        crop = img.crop((x0, y0, x1, y1))
                        self.lcd.DisplayPILImage(crop, x0, y0)
                    self.prev = new
                    return
        self.lcd.DisplayPILImage(img, 0, 0)
        self.prev = new

    def invalidate(self) -> None:
        """Force next push to be a full frame."""
        self.prev = None
