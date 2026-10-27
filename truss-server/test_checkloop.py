#!/usr/bin/env python3
"""
Tests for the check loop.

Run from the truss-server folder:  python3 test_checkloop.py
Stdlib only. Exits non-zero on the first failure.
"""

import sys
import tempfile
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


def rig(scripts):
    tmp = tempfile.mkdtemp()
    store = QuestionStore(Path(tmp) / "questions")
    spoken = []
    loop = CheckLoop(
        store,
        ScriptedChecker(scripts),
        Path(tmp) / "log",
        speak_fn=lambda text, entry, evidence: spoken.append(text),
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


if __name__ == "__main__":
    test_unmanaged_without_standing_questions()
    test_cannot_tell_is_silent_and_deletes()
    test_yes_on_third_check_speaks_and_keeps_evidence()
    test_one_frame_two_questions()
    test_checker_failure_is_silent()
    test_not_due_question_is_not_checked()
    test_nightly_fires_once_per_evening()
    print("ALL PASS")
