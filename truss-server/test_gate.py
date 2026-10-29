#!/usr/bin/env python3
"""
Tests for the gate-miss detector (gate.py). The gate itself is not
wired; these hold the detector to its rule so wiring it is safe.

Run from the truss-server folder:  python3 test_gate.py
Stdlib only. Exits non-zero on the first failure.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gate import GateMissDetector  # noqa: E402


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


NOW = datetime(2026, 10, 30, 21, 5, 0)


def rig(n=10):
    tmp = Path(tempfile.mkdtemp())
    logs = []
    return tmp, logs, GateMissDetector(tmp, n=n, now_fn=lambda: NOW, log_fn=logs.append)


def test_tenth_miss_bypasses_and_logs_once():
    tmp, logs, d = rig()
    passed = [d.passes("stove", False, ["stove", "knob"]) for _ in range(9)]
    check("nine misses keep the gate shut", passed == [False] * 9 and not d.bypassed("stove"))
    check("the tenth consecutive miss bypasses the gate", d.passes("stove", False, ["stove", "knob"]))
    check("and it stays bypassed", d.passes("stove", False) and d.passes("stove", False))
    check("logged once", len(logs) == 1)
    text = (tmp / "2026-10-30" / "gate.txt").read_text()
    check("the log names the question, the count and the phrases",
          text == "21:05:00 | stove | gate bypassed after 10 frames with no SAM match"
                  " | phrases: stove, knob\n")


def test_a_match_resets_the_count():
    tmp, logs, d = rig()
    for _ in range(9):
        d.passes("stove", False)
    check("a match passes", d.passes("stove", True))
    check("and resets the count", d.misses("stove") == 0)
    check("so nine more misses stay shut", not any(d.passes("stove", False) for _ in range(9)))
    check("nothing was logged", logs == [])


def test_a_match_closes_a_bypass_again():
    tmp, logs, d = rig(n=2)
    d.passes("stove", False)
    d.passes("stove", False)
    check("bypassed after n", d.bypassed("stove"))
    d.passes("stove", True)
    check("a match ends the bypass", not d.bypassed("stove"))
    check("a miss after the match is gated again", not d.passes("stove", False))


def test_questions_are_counted_apart():
    tmp, logs, d = rig(n=3)
    for _ in range(2):
        d.passes("stove", False)
        d.passes("door", False)
    d.passes("door", True)
    check("one question's match does not reset another", d.passes("stove", False))
    check("the other keeps its own count", not d.passes("door", False) and d.misses("door") == 1)


def test_reset_and_bad_n():
    tmp, logs, d = rig(n=1)
    check("n=1 bypasses on the first miss", d.passes("pills", False))
    d.reset("pills")
    check("reset forgets the bypass", not d.bypassed("pills") and d.misses("pills") == 0)
    for bad in (0, -1, 2.5, "10"):
        try:
            GateMissDetector(n=bad)
            check(f"n={bad!r} is refused", False)
        except ValueError:
            check(f"n={bad!r} is refused", True)


def test_unwritable_log_still_bypasses():
    blocker = Path(tempfile.mkdtemp()) / "file"
    blocker.write_text("not a folder")
    logs = []
    d = GateMissDetector(blocker, n=1, now_fn=lambda: NOW, log_fn=logs.append)
    check("a log folder that cannot be written does not close the gate", d.passes("stove", False))
    check("the bypass is still printed", len(logs) == 1)


if __name__ == "__main__":
    test_tenth_miss_bypasses_and_logs_once()
    test_a_match_resets_the_count()
    test_a_match_closes_a_bypass_again()
    test_questions_are_counted_apart()
    test_reset_and_bad_n()
    test_unwritable_log_still_bypasses()
    print("ALL PASS")
