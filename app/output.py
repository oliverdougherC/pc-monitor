"""Diff-based partial-update transport.

The panel only accepts bitmaps, but we can push *regions*. We render the full
logical frame each tick (cheap at 1 Hz), diff against the previous frame with
numpy, and send back the smallest set of horizontal bands that changed.
- Static labels: never re-sent.
- A changing digit: a few hundred bytes instead of a full frame.
- Mode switch / shift animation: falls back to one full-frame push.

The shadow framebuffer is a promise about the panel's memory, so it is kept
only when the panel actually acknowledged the bytes: `push()` commits after
every band returned True on the same connection generation, and a failed,
partial, timed-out or relink-interrupted update leaves a full frame pending
instead. A frame that never reached the screen must not make its twin
skippable, and bands that half-arrived must not make the missing ones look
unchanged.
"""
from __future__ import annotations

import threading
import time

import numpy as np

# Revisions whose link is a 115200-baud serial port: one full 800x480 RGB565
# frame is ~750 KB and takes ~68 s, so no extra frame is ever affordable there.
SERIAL_REVISIONS = {"A", "B", "C", "D", "WEACT_A", "WEACT_B"}


def wipe_supported(revision: str, cfg: dict) -> bool:
    """Should state changes be wiped? On (default) unless the layout opts out or
    the transport is serial, where the extra frame is physically impossible."""
    mode = str(cfg["layout"].get("transition", "wipe")).lower()
    return mode != "none" and str(revision).upper() not in SERIAL_REVISIONS


def wipe(pusher, blank_frame, new_frame, hold_s: float = 0.12) -> None:
    """Push a dark frame, hold, then the new layout — used when the layout changes
    (idle ↔ game). The panel has no framebuffer and slow pixels: without the gap
    the outgoing layout ghosts through the incoming one for a few hundred ms,
    which reads as a glitch rather than a switch. Both pushes are full frames,
    since neither resembles the other; near-black compresses to ~1 KB as PNG."""
    pusher.invalidate()
    pusher.push(blank_frame)
    time.sleep(max(0.0, hold_s))
    pusher.invalidate()
    pusher.push(new_frame)


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
        # Guards the two fields that together form the transaction: the shadow
        # frame and the invalidation counter. A relink runs `invalidate()` on a
        # background thread while a push is in flight; whichever order the two
        # writes land in, "invalidated after the bytes were sent" has to win.
        self._lock = threading.Lock()
        self._invalidations = 0

    @property
    def generation(self):
        """The transport's connection generation, or None if it does not count.

        A generation is the identity of one bring-up: `PanelLink` bumps it every
        time it rebuilds, so a push that began and ended on the same one spoke to
        the same live connection the whole way through.
        """
        return getattr(self.lcd, "generation", None)

    def push(self, img) -> bool:
        """Send what changed. True only when the panel acknowledged every byte.

        The acknowledgement is an explicit True from the transport: `PanelLink`
        answers with one; a raw vendor driver answers with None, and a swallowed
        vendor error is not a display. `prev` is committed only if the push
        completed on the generation it started on and nothing invalidated it in
        flight; otherwise the next push is a full frame, because we can no longer
        say what the panel is holding.
        """
        new = np.asarray(img, dtype=np.uint8)
        gen = self.generation
        with self._lock:
            prev = self.prev
            invalidations = self._invalidations
        if prev is not None and prev.shape == new.shape:
            changed = np.any(new != prev, axis=2)
            frac = changed.mean()
            if frac == 0.0:
                # Nothing to send: the frame is on the screen *if* the cache
                # still believes so, and the commit re-reads that belief.
                return self._commit(new, gen, invalidations)
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
                        if self.lcd.DisplayPILImage(crop, x0, y0) is not True:
                            # A band short of a whole update: the panel holds a
                            # frame that is neither the old one nor the new one,
                            # so nothing smaller than a full frame can fix it.
                            return self._discard()
                    return self._commit(new, gen, invalidations)
        if self.lcd.DisplayPILImage(img, 0, 0) is not True:
            return self._discard()
        return self._commit(new, gen, invalidations)

    def _commit(self, new, gen, invalidations: int) -> bool:
        """Publish the shadow frame, unless the world moved while it was sent."""
        with self._lock:
            if self._invalidations != invalidations or self.generation != gen:
                self.prev = None
                return False
            self.prev = new
            return True

    def _discard(self) -> bool:
        """The update did not land: the next push must repaint everything."""
        with self._lock:
            self.prev = None
        return False

    def invalidate(self) -> None:
        """Force next push to be a full frame."""
        with self._lock:
            self._invalidations += 1
            self.prev = None
