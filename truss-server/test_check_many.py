#!/usr/bin/env python3
"""
Tests for one call per photo: verdict.check_many asks every due
question about a frame in a single model call.

Run from the truss-server folder:  python3 test_check_many.py
Stdlib only. Exits non-zero on the first failure. Every call goes to
the stub chat/completions server from test_endpoint.py; no real model
is called.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import verdict  # noqa: E402
from test_endpoint import FRAME, StubModel, cfg, check  # noqa: E402

STOVE = {"question": "Is the stove off?", "looks": "Knobs all at the top.", "safety": True}
DOOR = {"question": "Is the front door locked?", "looks": None, "safety": False}
PILLS = {"question": "Did I take my night pills?", "looks": None, "safety": False}


def answer(number, v, reason="", missing=""):
    return {"number": number, "verdict": v, "reason": reason, "missing": missing}


def ask(items, reply):
    with StubModel([reply]) as stub:
        results = verdict.check_many(FRAME, items, config=cfg(stub))
    return results, stub


def user_text(stub):
    return " ".join(
        part["text"] for part in stub.body()["messages"][1]["content"] if part["type"] == "text"
    )


def test_one_call_one_photo():
    results, stub = ask(
        [STOVE, DOOR],
        {"answers": [
            answer(1, "yes", "Every knob points to the top."),
            answer(2, "no", "The deadbolt thumb turn is vertical."),
        ]},
    )
    check("one request for two questions", len(stub.requests) == 1)
    check("the photo is sent once", len(stub.images()) == 1)
    text = user_text(stub)
    check("both questions are numbered in the request",
          "Question 1: Is the stove off?" in text and "Question 2: Is the front door locked?" in text)
    check("the safety question is marked", "Is the stove off? (a safety question)" in text)
    check("the unflagged question is not marked", "locked? (a safety question)" not in text)
    check("the looks ride with their own question", "For question 1," in text and "Knobs all at the top." in text)
    system = stub.system()
    check("the intent prompt is still the base", system.startswith(verdict.SYSTEM_PROMPT))
    check("each is judged on its own", "judge each on its own" in system)
    check("the safety rule is sent", "marked as safety questions" in system)
    check("one result per question", len(results) == 2)
    check("first answer is the stove's", results[0]["verdict"] == "yes"
          and results[0]["reason"] == "Every knob points to the top.")
    check("second answer is the door's", results[1]["verdict"] == "no")
    check("no failures", all(r["failure"] is None for r in results))
    check("usage counted once", results[0]["usage"]["spark_input_tokens"] == 1000
          and results[1]["usage"]["spark_input_tokens"] == 0)
    check("each result says it was batched", all(r["batched"] == 2 for r in results))


def test_no_safety_rule_without_a_safety_question():
    _, stub = ask([DOOR, PILLS], {"answers": [answer(1, "cannot_tell", "Dark.", "the lock"),
                                               answer(2, "cannot_tell", "Dark.", "the pill box")]})
    check("no safety rule when none is a safety question",
          "marked as safety questions" not in stub.system())


def test_answers_in_any_order():
    results, _ = ask([STOVE, DOOR], {"answers": [
        answer(2, "no", "The deadbolt is vertical."),
        answer(1, "cannot_tell", "The knobs are cut off.", "the knobs on the stove"),
    ]})
    check("matched by number, not position",
          results[0]["verdict"] == "cannot_tell" and results[1]["verdict"] == "no")
    check("a cannot_tell keeps its missing", results[0]["missing"] == "the knobs on the stove")


def test_left_out_or_doubled_answers_are_unanswered():
    results, _ = ask([STOVE, DOOR, PILLS], {"answers": [
        answer(1, "yes", "Knobs at the top."),
        answer(3, "yes", "The box is open."),
        answer(3, "no", "The box is shut."),
        answer(7, "yes", "No such question."),
    ]})
    check("the answered one is used", results[0]["verdict"] == "yes" and results[0]["failure"] is None)
    check("a left-out question is unanswered",
          results[1]["verdict"] == "cannot_tell" and results[1]["failure"] == "unanswered")
    check("a doubled answer is never used",
          results[2]["verdict"] == "cannot_tell" and results[2]["failure"] == "unanswered")


def test_the_single_check_guards_still_hold():
    results, _ = ask([STOVE, DOOR], {"answers": [
        answer(1, "yes", ""),
        answer(2, "no", "The latch is open.", "the deadbolt"),
    ]})
    check("a verdict with no reason is unexplained",
          results[0]["verdict"] == "cannot_tell" and results[0]["failure"] == "unexplained")
    check("a verdict that names something missing reads cannot_tell",
          results[1]["verdict"] == "cannot_tell" and results[1]["missing"] == "the deadbolt")


def test_a_failed_call_fails_every_question():
    results, _ = ask([STOVE, DOOR], 500)
    check("every question failed", all(r["failure"] for r in results))
    check("every question reads cannot_tell", all(r["verdict"] == "cannot_tell" for r in results))
    check("the reason says no check was made", all("no check was made" in r["reason"] for r in results))


def test_a_reply_off_the_schema_fails_every_question():
    results, _ = ask([STOVE, DOOR], {"verdict": "yes", "reason": "One answer only.", "missing": ""})
    check("an off-schema reply is never read as answers",
          all(r["failure"] and r["verdict"] == "cannot_tell" for r in results))


if __name__ == "__main__":
    test_one_call_one_photo()
    test_no_safety_rule_without_a_safety_question()
    test_answers_in_any_order()
    test_left_out_or_doubled_answers_are_unanswered()
    test_the_single_check_guards_still_hold()
    test_a_failed_call_fails_every_question()
    test_a_reply_off_the_schema_fails_every_question()
    print("ALL PASS")
