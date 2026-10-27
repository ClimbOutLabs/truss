#!/usr/bin/env python3
"""
Tests for the bedtime round.

Run from the truss-server folder:  python3 test_bedtime.py
Stdlib only. Exits non-zero on the first failure.
"""

import json
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


def _raw_folder(root, slug, watch, question="Is the porch light on?"):
    folder = Path(root) / slug
    folder.mkdir()
    if question is not None:
        (folder / "question.txt").write_text(question + "\n")
    (folder / "watch.json").write_text(json.dumps(watch))
    return folder


def _watch(pattern, **over):
    watch = {
        "pattern": pattern,
        "state": "standing",
        "seq": 1,
        "planted_at": "2026-10-27T20:00:00",
        "last_checked_at": None,
        "answered_at": None,
    }
    watch.update(over)
    return watch


def test_bad_patterns_never_stop_checking():
    bad = [
        ("interval without minutes", _watch({"kind": "interval"})),
        ("interval minutes as words", _watch({"kind": "interval", "minutes": "15"})),
        ("interval minutes as a list", _watch({"kind": "interval", "minutes": [15]})),
        ("nightly after as a word", _watch({"kind": "nightly", "after": "banana"})),
        ("nightly after missing", _watch({"kind": "nightly", "after": None})),
        (
            "nightly gap as words",
            _watch(
                {"kind": "nightly", "after": "21:00", "minutes": "soon"},
                last_checked_at="2026-10-27T21:00:00",
            ),
        ),
        ("an unknown kind", _watch({"kind": "weekly", "minutes": 15})),
        (
            "nightly after in single digits",
            _watch({"kind": "nightly", "after": "7:05"}),
        ),
        ("a pattern with no kind", _watch({"minutes": 0})),
    ]
    for i, (label, watch) in enumerate(bad):
        with tempfile.TemporaryDirectory() as tmp:
            store = QuestionStore(tmp)
            healthy = store.create("Is the gate closed?")
            store.save_ref(healthy, "yes", JPEG)
            store.save_ref(healthy, "no", JPEG)
            store.mark_checked(healthy, when=datetime.now() - timedelta(hours=1))
            _raw_folder(tmp, f"bad-{i}", watch)
            check(f"{label}: reads as no question", store.get(f"bad-{i}") is None)
            check(
                f"{label}: due still returns the healthy question",
                [e["slug"] for e in store.due()] == [healthy],
            )
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        _raw_folder(
            tmp,
            "offset-stamp",
            _watch(
                {"kind": "interval", "minutes": 15},
                planted_at="2026-10-27T20:00:00+00:00",
            ),
        )
        check(
            "a stamp carrying an offset reads as no question",
            store.get("offset-stamp") is None and store.due() == [],
        )


def test_pattern_validation_at_planting():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create("Is the gate closed?", {"kind": "interval"})
        check(
            "a bare interval kind gains its default minutes",
            store.get(slug)["pattern"] == {"kind": "interval", "minutes": 15},
        )
        slug2 = store.create("Is the porch dark?", {"kind": "nightly", "after": "21:30"})
        check(
            "a nightly pattern gains the store default gap",
            store.get(slug2)["pattern"]["minutes"] == 15,
        )
        for label, pattern in (
            ("minutes as words", {"kind": "interval", "minutes": "15"}),
            ("an unknown kind", {"kind": "weekly"}),
            ("after as a word", {"kind": "nightly", "after": "banana"}),
            ("after in single digits", {"kind": "nightly", "after": "7:05"}),
            ("after with an offset", {"kind": "nightly", "after": "20:00+00:00"}),
            ("a pattern that is not a mapping", "every hour"),
        ):
            try:
                store.create("Is anything on?", pattern)
                check(f"create refuses {label}", False)
            except ValueError:
                check(f"create refuses {label}", True)
        slug3 = store.create(
            "Is the hall dark?", {"kind": "nightly", "after": "20:00:00"}
        )
        check(
            "an after with seconds is stored as a plain clock time",
            store.get(slug3)["pattern"]["after"] == "20:00",
        )
        before = sorted(p.name for p in Path(tmp).iterdir())
        try:
            store.create("Is the gate closed?", {"kind": "interval", "minutes": "soon"})
        except ValueError:
            pass
        check(
            "a refused planting leaves no folder behind",
            sorted(p.name for p in Path(tmp).iterdir()) == before,
        )
        retry = store.create("Is the gate closed?")
        check(
            "the retry of a refused text keeps the plain slug",
            retry == "is-the-gate-closed-2",
        )


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
        check(
            "the broken folder reads as no question",
            store.get("half-planted") is None,
        )
        check(
            "lists skip the broken folder",
            [e["slug"] for e in store.list()] == [slug],
        )
        planted = plant_round(store)
        check(
            "the round plants beside a broken folder",
            len(planted["slugs"]) == 3,
        )

    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        corrupt = Path(tmp) / "corrupt"
        corrupt.mkdir()
        (corrupt / "question.txt").write_text("Is the porch light on?\n")
        (corrupt / "watch.json").write_text("{not json")
        check(
            "a corrupt watch file reads as no question",
            store.get("corrupt") is None,
        )
        planted = plant_round(store)
        check(
            "the round plants beside a corrupt folder",
            len(planted["slugs"]) == 3 and len(store.list()) == 3,
        )

    # Watch files that parse but are not complete records: an empty
    # object, a bare number, a state with nothing else.
    for i, content in enumerate(
        ("{}", "5", '{"state": "standing"}')
    ):
        with tempfile.TemporaryDirectory() as tmp:
            store = QuestionStore(tmp)
            broken = Path(tmp) / f"broken-{i}"
            broken.mkdir()
            (broken / "question.txt").write_text("Is the porch light on?\n")
            (broken / "watch.json").write_text(content)
            check(
                f"incomplete watch {i} reads as no question",
                store.get(f"broken-{i}") is None,
            )
            healthy = store.create("Is the gate closed?")
            check(
                f"lists stay healthy beside incomplete watch {i}",
                [e["slug"] for e in store.list()] == [healthy],
            )
            planted = plant_round(store)
            check(
                f"the round plants beside incomplete watch {i}",
                len(planted["slugs"]) == 3,
            )

    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create("Is the porch light on?")
        folder = Path(tmp) / slug
        (folder / "question.txt").unlink()
        try:
            store.save_ref(slug, "yes", JPEG)
            check("a look into a folder reads cannot see is refused", False)
        except KeyError:
            check("a look into a folder reads cannot see is refused", True)
        check(
            "the refused look wrote nothing",
            not (folder / "ref-yes.jpg").exists(),
        )

    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create("Is the porch light on?")
        folder = Path(tmp) / slug
        (folder / "question.txt").unlink()
        (folder / "question.txt").mkdir()
        try:
            store.save_ref(slug, "yes", JPEG)
            check("a look is refused when the question text is a folder", False)
        except KeyError:
            check("a look is refused when the question text is a folder", True)
        check(
            "that refused look wrote nothing either",
            not (folder / "ref-yes.jpg").exists(),
        )

    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        empty = Path(tmp) / "empty-words"
        empty.mkdir()
        (empty / "question.txt").write_text("")
        (empty / "watch.json").write_text(
            json.dumps(_watch({"kind": "interval", "minutes": 15}))
        )
        check(
            "a question with no words reads as no question",
            store.get("empty-words") is None,
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
    test_bad_patterns_never_stop_checking()
    test_pattern_validation_at_planting()
    test_retired_member_on_a_fresh_store_plants_nothing()
    test_existing_member_on_different_terms_is_noted_not_rewritten()
    test_preplanted_member_joins_in_walk_order()
    test_loose_text_match_is_the_same_question()
    test_broken_folder_never_blocks_planting()
    test_the_round()
    print("ALL PASS")
