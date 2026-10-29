#!/usr/bin/env python3
"""
Watch coverage: how much of a window a done question actually saw.

A missed-window alert must never say more than the loop knows. "I
didn't see it" means one thing when the question was checked against
frames all window long, and quite another when the glasses were on
the table, a guide had the frames, or the question was resting
between checks. So coverage is read from the frames that were
actually checked for this question, never from frames merely
arriving. Every check of a done question that comes back with a
verdict leaves one line in that day's check log,

    log/<date>/checks-<slug>.txt   one check time per line, nothing else

The photo itself is still deleted when no question fires on it; only
the time of the check is kept, so the next alert can say how much of
the window was watched.

Times are written with their UTC offset and compared as absolute
times, so the night the clocks go back, a repeated hour with no
checks in it is a hole, not an hour that never happened.

Two checks no more than the gap apart (ten minutes unless told
otherwise) count as watched in between. A gap longer than that is
time the question was not watched. Each check also counts for half a
gap on either side, so checks every few minutes from just after the
window opens to just before it closes cover the whole window.

Three cases come out, and every alert is worded for its case:

    complete   watched the whole window
    partial    watched some of it; the spans are given in clock times
    none       not watched at any point in the window
"""

import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_GAP_MINUTES = 10

# One check per line, exactly as record() writes it: seconds and a
# UTC offset, nothing missing.
_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def _absolute(moment):
    """A moment as UTC. A naive time is read as local time, keeping
    its fold, so the second pass through a repeated hour stays the
    second pass."""
    return moment.astimezone(timezone.utc)


def _local(moment):
    """An absolute moment back as a naive local time, for speaking."""
    return moment.astimezone().replace(tzinfo=None)


class CheckLog:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _path(self, day, slug):
        return self.root / day.isoformat() / f"checks-{slug}.txt"

    def record(self, slug, when=None):
        """Add one check of a question. Raises on a write failure; the
        check loop catches it, since a lost line only makes a later
        alert more modest, never bolder."""
        when = when or datetime.now()
        path = self._path(when.date(), slug)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(when.astimezone().isoformat(timespec="seconds") + "\n")

    def times(self, day, slug):
        """That day's checks of a question, sorted. A missing log is no
        checks; a line that cannot be read is skipped. Never raises."""
        try:
            text = self._path(day, slug).read_text(encoding="utf-8")
        except Exception:
            return []
        out = []
        for line in text.splitlines():
            # Only a whole line counts: one cut short by a crash is
            # skipped, never read as a check at some other time.
            if not _LINE.match(line):
                continue
            try:
                out.append(datetime.fromisoformat(line))
            except ValueError:
                continue
        out.sort(key=_absolute)
        return out


def coverage(checks, start, end, gap_minutes=DEFAULT_GAP_MINUTES):
    """How much of the window from start to end was watched.

    Returns {"kind": "complete" | "partial" | "none", "spans": [...]},
    each span a (first, last) pair of check times clipped to the
    window, as naive local times. The spans are what an alert may say
    out loud: real check times, never the half-gap allowance around
    them.
    """
    gap = timedelta(minutes=gap_minutes)
    half = gap / 2
    start, end = _absolute(start), _absolute(end)
    near = sorted(
        t for t in (_absolute(c) for c in checks) if start - half <= t <= end + half
    )

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
            # No check of this run fell inside the window; only its
            # allowance reaches in, and that is not a sighting.
            continue
        spans.append((_local(max(start, first)), _local(min(end, last))))
        if first - half <= start and last + half >= end:
            covered_whole = True

    if not spans:
        return {"kind": "none", "spans": []}
    if covered_whole:
        return {"kind": "complete", "spans": spans}
    return {"kind": "partial", "spans": spans}
