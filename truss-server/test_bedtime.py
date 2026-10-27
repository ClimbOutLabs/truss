#!/usr/bin/env python3
"""
Tests for the bedtime round.

Run from the truss-server folder:  python3 test_bedtime.py
Stdlib only. Exits non-zero on the first failure.
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bedtime import ROUND, plant_round  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from questions import QuestionStore  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def verdict(v, reason=""):
    return {"verdict": v, "reason": reason, "latency_ms": 1, "usage": {}}


class ScriptedChecker:
    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def __call__(self, frame_path, entry):
        self.calls.append(entry["slug"])
        script = self.scripts.get(entry["slug"], [])
        if script:
            return script.pop(0)
        return verdict("cannot_tell", "Nothing decisive is visible.")


def test_plant_round():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slugs = plant_round(store)
        check("round plants all three in walk order", slugs == [
            "is-the-stove-off",
            "is-the-front-door-locked",
            "is-tonight-s-pill-compartment-empty",
        ])
        entries = store.list()
        check(
            "listed in planting order",
            [e["question"] for e in entries] == ROUND,
        )
        check(
            "nightly pattern with a one minute gap",
            all(
                e["pattern"] == {"kind": "nightly", "after": "20:00", "minutes": 1}
                for e in entries
            ),
        )
        again = plant_round(store)
        check("planting again is a no-op", again == slugs and len(store.list()) == 3)
        store.retire(slugs[1])
        try:
            plant_round(store)
            check("retired member stops the planting", False)
        except ValueError:
            check("retired member stops the planting", True)
        try:
            plant_round(store, after="bedtime")
            check("bad time refused", False)
        except ValueError:
            check("bad time refused", True)
        try:
            plant_round(store, gap_minutes="soon")
            check("bad gap refused", False)
        except ValueError:
            check("bad gap refused", True)


def test_the_round():
    """The choreography: one walk down the hall, two passes. The first
    pass settles the stove, cannot read the door, and finds a pill
    still in tonight's compartment. The second pass, a minute later,
    settles the other two. Answers are spoken in the order the wearer
    meets the questions."""
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(Path(tmp) / "questions")
        slugs = plant_round(store)
        for slug in slugs:
            store.save_ref(slug, "yes", JPEG)
            store.save_ref(slug, "no", JPEG)
        check(
            "whole round standing after references",
            all(store.get(s)["state"] == "standing" for s in slugs),
        )

        stove, door, pills = slugs
        checker = ScriptedChecker(
            {
                stove: [verdict("yes", "All four burners are dark.")],
                door: [
                    verdict("cannot_tell", "The latch is in shadow."),
                    verdict("yes", "The deadbolt is thrown."),
                ],
                pills: [
                    verdict("no", "A pill is still in tonight's compartment."),
                    verdict("yes", "Tonight's compartment is empty."),
                ],
            }
        )
        spoken = []
        clock = [datetime(2026, 10, 27, 21, 0, 0)]
        loop = CheckLoop(
            store,
            checker,
            Path(tmp) / "log",
            speak_fn=lambda text, entry, evidence: spoken.append(text),
            now_fn=lambda: clock[0],
        )
        captures = Path(tmp) / "captures"
        captures.mkdir()

        def frame(name):
            path = captures / name
            path.write_bytes(JPEG)
            return path

        first = loop.process_frame(frame("pass1.jpg"))
        check("first pass fires only the stove", first["fired"] == [stove])
        check(
            "the miss and the no stay silent on the first pass",
            spoken == ["Is the stove off? Yes. All four burners are dark."],
        )

        clock[0] += timedelta(seconds=90)
        second = loop.process_frame(frame("pass2.jpg"))
        check(
            "second pass settles the door and the pills",
            second["fired"] == [door, pills],
        )
        check(
            "answers spoken in the order the wearer meets them",
            spoken
            == [
                "Is the stove off? Yes. All four burners are dark.",
                "Is the front door locked? Yes. The deadbolt is thrown.",
                "Is tonight's pill compartment empty? Yes. Tonight's compartment is empty.",
            ],
        )
        check(
            "the second pass rechecked the unsettled questions",
            checker.calls.count(door) == 2 and checker.calls.count(pills) == 2,
        )
        check(
            "nothing left due that evening",
            store.due(now=clock[0] + timedelta(minutes=5)) == [],
        )
        check(
            "the whole round is due again the next evening",
            len(store.due(now=clock[0] + timedelta(days=1))) == 3,
        )
        log_lines = (Path(tmp) / "log" / "2026-10-27" / "answers.txt").read_text()
        check(
            "answer log carries all three answers",
            log_lines.count(" | yes | ") == 3,
        )
        evidence = list((Path(tmp) / "log" / "2026-10-27").glob("*.jpg"))
        check("one evidence photo per pass that fired", len(evidence) == 2)
        check("no frames left in captures", list(captures.iterdir()) == [])


if __name__ == "__main__":
    test_plant_round()
    test_the_round()
    print("ALL PASS")
