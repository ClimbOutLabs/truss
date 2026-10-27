#!/usr/bin/env python3
"""
Tests for the check loop.

Run from the truss-server folder:  python3 test_checkloop.py
Stdlib only. Exits non-zero on the first failure.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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
    """Returns scripted results per question slug, in call order."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def __call__(self, frame_path, entry):
        self.calls.append(entry["slug"])
        script = self.scripts.get(entry["slug"], [])
        if script:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return verdict("cannot_tell", "Nothing decisive is visible.")


def rig(scripts, now_fn=None):
    tmp = tempfile.mkdtemp()
    store = QuestionStore(Path(tmp) / "questions")
    spoken = []
    loop = CheckLoop(
        store,
        ScriptedChecker(scripts),
        Path(tmp) / "log",
        speak_fn=lambda text, entry, evidence: spoken.append(text),
        now_fn=now_fn,
    )
    captures = Path(tmp) / "captures"
    captures.mkdir()
    return store, loop, spoken, captures


def plant(store, text, pattern):
    slug = store.create(text, pattern)
    store.save_ref(slug, "yes", JPEG)
    store.save_ref(slug, "no", JPEG)
    return slug


def frame(captures, name):
    path = captures / name
    path.write_bytes(JPEG)
    return path


def test_unmanaged_without_standing_questions():
    store, loop, spoken, captures = rig({})
    f = frame(captures, "a.jpg")
    result = loop.process_frame(f)
    check("no standing questions means unmanaged", result["managed"] is False)
    check("unmanaged frame is left in place", f.exists())
    check("nothing spoken", spoken == [])


def test_cannot_tell_is_silent_and_deletes():
    store, loop, spoken, captures = rig(
        {"is-the-stove-off": [verdict("cannot_tell", "The burners are not visible.")]}
    )
    slug = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    f = frame(captures, "a.jpg")
    result = loop.process_frame(f)
    check("frame managed", result["managed"] is True)
    check("cannot_tell fires nothing", result["fired"] == [])
    check("cannot_tell speaks nothing", spoken == [])
    check("cannot_tell frame deleted", not f.exists())
    entry = store.get(slug)
    check("question still standing", entry["state"] == "standing")
    check("check was recorded", entry["last_checked_at"] is not None)


def test_yes_on_third_check_speaks_and_keeps_evidence():
    store, loop, spoken, captures = rig(
        {
            "is-the-stove-off": [
                verdict("no", "A burner is on."),
                verdict("cannot_tell", "Too dark to tell."),
                verdict("yes", "All four burners are dark."),
            ]
        }
    )
    slug = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    loop.process_frame(frame(captures, "1.jpg"))
    loop.process_frame(frame(captures, "2.jpg"))
    check("first two frames deleted", list(captures.iterdir()) == [])
    result = loop.process_frame(frame(captures, "3.jpg"))
    check("third check fires", result["fired"] == [slug])
    check(
        "spoken line names the question, the answer, the observation",
        spoken == ["Is the stove off? Yes. All four burners are dark."],
    )
    evidence = Path(result["evidence"])
    check("evidence photo kept", evidence.exists() and evidence.read_bytes() == JPEG)
    logs = list(Path(evidence).parent.glob("answers.txt"))
    check("answer log written", len(logs) == 1)
    text = logs[0].read_text()
    check(
        "log line carries question and reason",
        "Is the stove off?" in text and "All four burners are dark." in text,
    )
    check("interval question answered", store.get(slug)["state"] == "answered")


def test_one_frame_two_questions():
    store, loop, spoken, captures = rig(
        {
            "is-the-stove-off": [verdict("yes", "All four burners are dark.")],
            "is-the-front-door-locked": [verdict("no", "The latch is open.")],
        }
    )
    stove = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    door = plant(
        store, "Is the front door locked?", {"kind": "interval", "minutes": 0}
    )
    result = loop.process_frame(frame(captures, "a.jpg"))
    check("only the yes question fires", result["fired"] == [stove])
    check("one spoken line", len(spoken) == 1)
    check("shared frame kept as evidence", Path(result["evidence"]).exists())
    check("door question still standing", store.get(door)["state"] == "standing")
    check(
        "door question was still checked",
        store.get(door)["last_checked_at"] is not None,
    )


def test_checker_failure_is_silent():
    store, loop, spoken, captures = rig(
        {"is-the-stove-off": [RuntimeError("model call failed")]}
    )
    slug = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    f = frame(captures, "a.jpg")
    result = loop.process_frame(f)
    check("failure counted", result["errors"] == 1)
    check("failure fires nothing", result["fired"] == [] and spoken == [])
    check("failure frame deleted", not f.exists())
    check("question still standing", store.get(slug)["state"] == "standing")


def test_not_due_question_is_not_checked():
    store, loop, spoken, captures = rig(
        {"is-the-stove-off": [verdict("yes", "All four burners are dark.")]}
    )
    plant(store, "Is the stove off?", {"kind": "interval", "minutes": 15})
    f = frame(captures, "a.jpg")
    result = loop.process_frame(f)
    check("checker never called", loop.check_fn.calls == [])
    check("frame still deleted", not f.exists())
    check("nothing fired or spoken", result["fired"] == [] and spoken == [])


def test_nightly_fires_once_per_evening():
    store, loop, spoken, captures = rig(
        {
            "is-the-front-door-locked": [
                verdict("yes", "The deadbolt is thrown."),
                verdict("yes", "The deadbolt is thrown."),
            ]
        }
    )
    plant(
        store,
        "Is the front door locked?",
        {"kind": "nightly", "after": "00:00"},
    )
    loop.process_frame(frame(captures, "1.jpg"))
    check("nightly question fires in the evening", len(spoken) == 1)
    loop.process_frame(frame(captures, "2.jpg"))
    check(
        "same evening does not check or fire again",
        loop.check_fn.calls == ["is-the-front-door-locked"] and len(spoken) == 1,
    )


def test_broken_store_never_breaks_capture():
    tmp = tempfile.mkdtemp()
    root = Path(tmp) / "questions"
    bad = root / "broken-question"
    bad.mkdir(parents=True)
    (bad / "watch.json").write_text("{not json")
    store = QuestionStore(root)
    spoken = []
    loop = CheckLoop(
        store,
        ScriptedChecker({}),
        Path(tmp) / "log",
        speak_fn=lambda text, entry, evidence: spoken.append(text),
    )
    captures = Path(tmp) / "captures"
    captures.mkdir()
    f = frame(captures, "a.jpg")
    result = loop.process_frame(f)
    check("broken store means unmanaged", result["managed"] is False)
    check(
        "the broken folder is skipped, not an error",
        result["errors"] == 0,
    )
    check("frame kept when the store is broken", f.exists())


def test_evidence_failure_still_speaks_once():
    tmp = tempfile.mkdtemp()
    store = QuestionStore(Path(tmp) / "questions")
    log_path = Path(tmp) / "log"
    log_path.write_text("not a folder")
    spoken = []
    loop = CheckLoop(
        store,
        ScriptedChecker(
            {"is-the-stove-off": [verdict("yes", "All four burners are dark.")]}
        ),
        log_path,
        speak_fn=lambda text, entry, evidence: spoken.append(text),
    )
    slug = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    captures = Path(tmp) / "captures"
    captures.mkdir()
    f = frame(captures, "a.jpg")
    result = loop.process_frame(f)
    check("evidence failure does not stop the firing", result["fired"] == [slug])
    check(
        "spoken once despite the evidence failure",
        spoken == ["Is the stove off? Yes. All four burners are dark."],
    )
    check("frame kept in captures as the record", f.exists())
    check("question answered despite the failure", store.get(slug)["state"] == "answered")
    check("failures counted", result["errors"] >= 1)


def test_speak_failure_cannot_duplicate_the_answer():
    tmp = tempfile.mkdtemp()
    store = QuestionStore(Path(tmp) / "questions")
    heard = []

    def flaky_speaker(text, entry, evidence):
        if "stove" in text:
            raise RuntimeError("speaker down")
        heard.append(text)

    loop = CheckLoop(
        store,
        ScriptedChecker(
            {
                "is-the-stove-off": [verdict("yes", "All four burners are dark.")],
                "is-the-front-door-locked": [verdict("yes", "The deadbolt is thrown.")],
            }
        ),
        Path(tmp) / "log",
        speak_fn=flaky_speaker,
    )
    stove = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    door = plant(store, "Is the front door locked?", {"kind": "interval", "minutes": 0})
    captures = Path(tmp) / "captures"
    captures.mkdir()
    result = loop.process_frame(frame(captures, "a.jpg"))
    check("both questions fire", result["fired"] == [stove, door] or result["fired"] == [door, stove])
    check("speak failure counted once", result["errors"] == 1)
    check(
        "both questions marked answered before speaking",
        store.get(stove)["state"] == "answered"
        and store.get(door)["state"] == "answered",
    )
    check("the healthy speaker still heard its line", len(heard) == 1)


def test_nondict_check_result_is_a_silent_error():
    store, loop, spoken, captures = rig({"is-the-stove-off": [None]})
    plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    f = frame(captures, "a.jpg")
    result = loop.process_frame(f)
    check("non-dict result counted as an error", result["errors"] == 1)
    check("non-dict result fires and speaks nothing", result["fired"] == [] and spoken == [])
    check("non-dict frame deleted", not f.exists())


def test_bookkeeping_failure_skips_one_question_only():
    store, loop, spoken, captures = rig(
        {
            "is-the-stove-off": [verdict("yes", "All four burners are dark.")],
            "is-the-front-door-locked": [verdict("yes", "The deadbolt is thrown.")],
        }
    )
    stove = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    door = plant(store, "Is the front door locked?", {"kind": "interval", "minutes": 0})
    original = store.mark_checked

    def flaky(slug, when=None):
        if slug == door:
            raise OSError("disk gone")
        return original(slug, when=when)

    store.mark_checked = flaky
    result = loop.process_frame(frame(captures, "a.jpg"))
    check("the healthy question still fires", result["fired"] == [stove])
    check("the healthy question is spoken", len(spoken) == 1)
    check("the failed question is not answered", store.get(door)["state"] == "standing")
    check("the failure is counted", result["errors"] >= 1)


def test_evidence_names_never_overwrite():
    fixed = datetime(2026, 10, 27, 21, 30, 0)
    store, loop, spoken, captures = rig({}, now_fn=lambda: fixed)
    slug = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    entry = store.get(slug)
    first = loop._save_evidence(frame(captures, "1.jpg"), entry, fixed)
    second = loop._save_evidence(frame(captures, "2.jpg"), entry, fixed)
    check("same-second evidence gets distinct names", first != second)
    check("both evidence photos kept", first.exists() and second.exists())


def test_malformed_reason_still_speaks():
    """A model can answer yes with a reason that is not a string. The
    answer must still be spoken (without the reason), logged, and marked
    answered, and a healthy question in the same frame must not lose
    its line."""
    store, loop, spoken, captures = rig(
        {
            "is-the-stove-off": [
                {"verdict": "yes", "reason": 123, "latency_ms": 1, "usage": {}}
            ],
            "is-the-front-door-locked": [
                verdict("yes", "The deadbolt is thrown.")
            ],
        }
    )
    stove = plant(store, "Is the stove off?", {"kind": "interval", "minutes": 0})
    door = plant(
        store, "Is the front door locked?", {"kind": "interval", "minutes": 0}
    )
    result = loop.process_frame(frame(captures, "a.jpg"))
    check(
        "both questions fire despite the malformed reason",
        sorted(result["fired"]) == sorted([stove, door]),
    )
    check(
        "malformed reason spoken without it, healthy reason spoken with it",
        sorted(spoken)
        == sorted(
            [
                "Is the stove off? Yes.",
                "Is the front door locked? Yes. The deadbolt is thrown.",
            ]
        ),
    )
    check("malformed reason is handled, not an error", result["errors"] == 0)
    check(
        "both questions answered",
        store.get(stove)["state"] == "answered"
        and store.get(door)["state"] == "answered",
    )


if __name__ == "__main__":
    test_unmanaged_without_standing_questions()
    test_cannot_tell_is_silent_and_deletes()
    test_yes_on_third_check_speaks_and_keeps_evidence()
    test_one_frame_two_questions()
    test_checker_failure_is_silent()
    test_not_due_question_is_not_checked()
    test_nightly_fires_once_per_evening()
    test_broken_store_never_breaks_capture()
    test_evidence_failure_still_speaks_once()
    test_speak_failure_cannot_duplicate_the_answer()
    test_nondict_check_result_is_a_silent_error()
    test_bookkeeping_failure_skips_one_question_only()
    test_evidence_names_never_overwrite()
    test_malformed_reason_still_speaks()
    print("ALL PASS")
