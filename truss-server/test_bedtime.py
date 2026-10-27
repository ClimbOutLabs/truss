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
        planted = plant_round(store)
        slugs = planted["slugs"]
        check("round plants all three in walk order", slugs == [
            "is-the-stove-off",
            "is-the-front-door-locked",
            "is-tonight-s-pill-compartment-empty",
        ])
        check("a fresh round has no notes", planted["notes"] == [])
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
        check("planting again is a no-op", again["slugs"] == slugs and len(store.list()) == 3)
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
        for bad_gap in ("soon", float("nan"), float("inf")):
            try:
                plant_round(store, gap_minutes=bad_gap)
                check(f"bad gap refused ({bad_gap})", False)
            except ValueError:
                check(f"bad gap refused ({bad_gap})", True)


def test_retired_member_on_a_fresh_store_plants_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create(ROUND[1], {"kind": "nightly", "after": "20:00"})
        store.retire(slug)
        try:
            plant_round(store)
            check("retired member refuses the round", False)
        except ValueError:
            check("retired member refuses the round", True)
        check(
            "nothing was created by the refused planting",
            len(store.list()) == 1,
        )


def test_existing_member_on_different_terms_is_noted_not_rewritten():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        manual = store.create(ROUND[0])
        store.save_ref(manual, "yes", JPEG)
        store.save_ref(manual, "no", JPEG)
        planted = plant_round(store)
        check("manual member is the round's first slug", planted["slugs"][0] == manual)
        check(
            "one note names the kept terms",
            len(planted["notes"]) == 1 and ROUND[0] in planted["notes"][0],
        )
        check(
            "the member's own pattern is untouched",
            store.get(manual)["pattern"] == {"kind": "interval", "minutes": 15},
        )
        check("no duplicate stove question", len(store.list()) == 3)


def test_preplanted_member_joins_in_walk_order():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        pills = store.create(ROUND[2], {"kind": "nightly", "after": "20:00", "minutes": 1})
        store.save_ref(pills, "yes", JPEG)
        store.save_ref(pills, "no", JPEG)
        planted = plant_round(store)
        check("returned order is walk order", planted["slugs"][2] == pills)
        members = [e["question"] for e in store.list() if e["question"] in ROUND]
        check("store order is walk order too", members == ROUND)


def test_loose_text_match_is_the_same_question():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        manual = store.create("Is the stove off")
        planted = plant_round(store)
        check("no duplicate from a missing question mark", len(store.list()) == 3)
        check("the wearer's own copy is the member", planted["slugs"][0] == manual)


def test_broken_folder_never_blocks_planting():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        half = Path(tmp) / "half-planted"
        half.mkdir()
        (half / "question.txt").write_text("Is the porch light on?\n")
        slug = store.create("Is the gate closed?")
        check("planting works beside a broken folder", slug == "is-the-gate-closed")
        check(
            "the new question carries a sequence number",
            store.get(slug)["seq"] >= 1,
        )


def test_the_round():
    """The choreography: one walk down the hall, two passes. The first
    pass settles the stove, cannot read the door, and finds a pill
    still in tonight's compartment. A frame inside the check gap is
    not checked at all. The second pass, past the gap, settles the
    other two. Answers are spoken in the order the wearer meets the
    questions."""
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(Path(tmp) / "questions")
        slugs = plant_round(store)["slugs"]
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

        calls_after_first = len(checker.calls)
        clock[0] += timedelta(seconds=30)
        early = loop.process_frame(frame("early.jpg"))
        check(
            "a frame inside the gap is not checked",
            len(checker.calls) == calls_after_first and early["fired"] == [],
        )

        clock[0] += timedelta(seconds=60)
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
    test_retired_member_on_a_fresh_store_plants_nothing()
    test_existing_member_on_different_terms_is_noted_not_rewritten()
    test_preplanted_member_joins_in_walk_order()
    test_loose_text_match_is_the_same_question()
    test_broken_folder_never_blocks_planting()
    test_the_round()
    print("ALL PASS")
