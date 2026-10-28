#!/usr/bin/env python3
"""
The gate-miss detector, for the SAM presence gate (work item E).

NOT WIRED. The gate itself (a frame reaches a due question's verdict
only when SAM finds one of the question's phrases in it) waits for app
sampling (item G): until frames arrive on their own during wear, every
frame is one she chose to send, and none should be held back. This is
the guard that gate will need, built and tested now so wiring it is
one call per due question.

The risk it guards: phrases that SAM never matches in this home (a
"burner" that SAM calls a "stove") would gate the question shut
forever, silently. So after N consecutive frames with no SAM match on
a due question (N starts at 10), the gate is bypassed for that
question: its frames go to the verdict model as if there were no gate,
and the bypass is logged once, with the question and the phrases.
The bypass holds until SAM matches for that question again, or the
question is reset.

    detector = GateMissDetector(log_dir)
    if detector.passes(entry["slug"], sam_client.has_match(reply), phrases):
        ... check the frame as usual ...

State is in memory: a restart starts every count again at zero.
"""

import threading
from datetime import datetime
from pathlib import Path

DEFAULT_N = 10


class GateMissDetector:
    def __init__(self, log_dir=None, n=DEFAULT_N, now_fn=None, log_fn=print):
        if not isinstance(n, int) or n < 1:
            raise ValueError("n must be a whole number of frames, 1 or more")
        self.n = n
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.now_fn = now_fn or datetime.now
        self.log_fn = log_fn
        self._misses = {}
        self._bypassed = set()
        self._lock = threading.Lock()

    def passes(self, slug, matched, phrases=()):
        """Whether this frame goes on to the verdict for this question:
        yes when SAM matched, or when the gate is bypassed for it."""
        with self._lock:
            if matched:
                self._misses[slug] = 0
                self._bypassed.discard(slug)
                return True
            if slug in self._bypassed:
                return True
            self._misses[slug] = self._misses.get(slug, 0) + 1
            if self._misses[slug] < self.n:
                return False
            self._bypassed.add(slug)
        self._log(slug, phrases)
        return True

    def bypassed(self, slug):
        with self._lock:
            return slug in self._bypassed

    def misses(self, slug):
        with self._lock:
            return self._misses.get(slug, 0)

    def reset(self, slug):
        """Forget the question's count and bypass (for a question
        answered, removed or replanted)."""
        with self._lock:
            self._misses.pop(slug, None)
            self._bypassed.discard(slug)

    def _log(self, slug, phrases):
        now = self.now_fn()
        line = (
            f"{now.strftime('%H:%M:%S')} | {slug} | gate bypassed after {self.n} "
            f"frames with no SAM match | phrases: {', '.join(phrases) or 'none'}"
        )
        self.log_fn(line)
        if self.log_dir is None:
            return
        try:
            day = self.log_dir / now.strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True)
            with open(day / "gate.txt", "a") as f:
                f.write(line + "\n")
        except OSError:
            # The bypass itself already happened; a failed log line
            # must not close the gate again.
            pass
