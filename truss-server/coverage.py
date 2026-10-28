#!/usr/bin/env python3
"""
Watch coverage: whether the glasses were with her during a window.

A missed-window alert must never say more than the loop knows. "I
didn't see it" means one thing when frames were arriving all window
long, and quite another when the glasses were on the table. Coverage
is read from frame arrivals alone: every captured photo, fired or
not, leaves one line in that day's frame log,

    log/<date>/frames.txt   one arrival time per line, nothing else

The photo itself is still deleted when no question fires on it; only
its arrival time is kept, so the next alert can say how much of the
window was watched.

Two frames no more than the gap apart (ten minutes unless told
otherwise) count as watched in between. A gap longer than that is
time the loop was not with her. Each frame also counts for half a
gap on either side, so frames arriving every few minutes from just
after the window opens to just before it closes cover the whole
window, and a frame never has to land on the opening minute exactly.

Three cases come out, and every alert is worded for its case:

    complete   watched the whole window
    partial    watched some of it; the spans are given in clock times
    none       not watched at any point in the window
"""

import threading
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_GAP_MINUTES = 10


class FrameLog:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _path(self, day):
        return self.root / day.isoformat() / "frames.txt"

    def record(self, when=None):
        """Add one frame arrival. Raises on a write failure; the
        capture path catches it, since a lost arrival line only
        makes a later alert more modest, never wrong the other way."""
        when = when or datetime.now()
        path = self._path(when.date())
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(when.isoformat(timespec="seconds") + "\n")

    def times(self, day):
        """That day's frame arrivals, sorted. A missing log is no
        frames; a line that cannot be read is skipped. Never raises."""
        try:
            text = self._path(day).read_text(encoding="utf-8")
        except Exception:
            return []
        out = []
        for line in text.splitlines():
            try:
                out.append(datetime.fromisoformat(line.strip()))
            except ValueError:
                continue
        out.sort()
        return out


def coverage(frames, start, end, gap_minutes=DEFAULT_GAP_MINUTES):
    """How much of the window from start to end was watched.

    Returns {"kind": "complete" | "partial" | "none", "spans": [...]},
    each span a (first, last) pair of frame times clipped to the
    window. The spans are what an alert may say out loud: real frame
    times, never the half-gap allowance around them.
    """
    gap = timedelta(minutes=gap_minutes)
    half = gap / 2
    near = sorted(t for t in frames if start - half <= t <= end + half)

    runs = []
    for t in near:
        if runs and t - runs[-1][-1] <= gap:
            runs[-1].append(t)
        else:
            runs.append([t])

    spans = []
    covered_whole = False
    for run in runs:
        first, last = run[0], run[-1]
        if last < start or first > end:
            # No frame of this run arrived inside the window; only
            # its allowance reaches in, and that is not a sighting.
            continue
        spans.append((max(start, first), min(end, last)))
        if first - half <= start and last + half >= end:
            covered_whole = True

    if not spans:
        return {"kind": "none", "spans": []}
    if covered_whole:
        return {"kind": "complete", "spans": spans}
    return {"kind": "partial", "spans": spans}
