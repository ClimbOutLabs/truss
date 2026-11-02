#!/usr/bin/env python3
"""
Tests for intent over literal: a check answers what the question means.

Run from the truss-server folder:  python3 test_intent.py
Stdlib only. Exits non-zero on the first failure.

There are no per-object rules in code. Every question, the stove
included, gets the same schema and the same prompt, which tells the
model to answer the real state the question asks about from any clear,
direct sign of it, not only the one deciding detail the plant-time
compile named. Every check goes to the stub chat/completions server
from test_endpoint.py; no real model is called.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import plant_compile  # noqa: E402
import verdict  # noqa: E402
from test_endpoint import FRAME, REF_NO, REF_YES, StubModel, cfg, check  # noqa: E402

ON_Q = "Is the stove on?"
OFF_Q = "Is the stove off?"
LOOKS = "Yes look: a burner lit. No look: all dark. What decides it: the knob positions."


def ask(question, reply, **kw):
    with StubModel([reply]) as stub:
        result = verdict.check(FRAME, question, config=cfg(stub), **kw)
    return result, stub


def test_no_question_gets_its_own_rule():
    systems = set()
    for q in (ON_Q, OFF_Q, "Did I leave the stove on?", "Is the front door locked?",
              "Is the kettle boiling?"):
        _, stub = ask(q, {"verdict": "cannot_tell", "reason": "Dark.", "missing": "the thing"})
        rf = stub.body()["response_format"]["json_schema"]
        check(f"one schema for every question: {q}", rf["schema"] == verdict.VERDICT_SCHEMA)
        check(f"the intent prompt is sent: {q}", stub.system().startswith(verdict.SYSTEM_PROMPT))
        systems.add(stub.system())
    check("one system prompt for every question", len(systems) == 1)
    check("no stove code left", not hasattr(verdict, "stove_polarity")
          and not hasattr(verdict, "KNOB_SCHEMA"))


def test_prompt_asks_for_the_intent():
    p = verdict.SYSTEM_PROMPT
    check("prompt: answer what the question means", "Answer what the question means" in p)
    check("prompt: any clear direct sign decides", "any clear, direct sign" in p)
    check("prompt: even when the usual detail is unreadable",
          "even when the usual deciding detail cannot be read" in p)
    check("prompt: an absence is not a sign", "absence of a sign" in p)
    check("prompt: answering from a sign leaves missing empty",
          "when you answer from it, missing is an empty string" in p)
    check("looks: the usual detail, not the only evidence",
          "not the only evidence" in verdict.LOOKS_INSTRUCTION)


def test_safety_holds_the_all_clear_only():
    s = verdict.SAFETY_INSTRUCTION
    check("safety: the all-clear needs the deciding detail or as conclusive",
          "all-clear only when the deciding detail itself" in s and "just as conclusive" in s)
    check("safety: a suggestion is still not enough", "no flame, no glow, or no light" in s)
    check("safety: the danger needs only a clear sign", "needs only a clear, direct sign" in s)


def test_a_sign_answers_when_the_detail_is_unreadable():
    # The Nov 1 live run: burner lit, knobs unreadable.
    result, stub = ask(ON_Q, {"verdict": "yes",
                              "reason": "A blue flame is burning under the pan.",
                              "missing": ""}, looks=LOOKS, safety=True)
    check("flame, knobs unreadable: yes", result["verdict"] == "yes" and result["failure"] is None)
    check("flame: reason kept", "flame" in result["reason"])
    check("flame: looks still sent", LOOKS in str(stub.body()["messages"]))
    result, _ = ask(OFF_Q, {"verdict": "no", "reason": "A flame is lit on the front burner.",
                            "missing": ""}, looks=LOOKS, safety=True)
    check("flame, asked if off: no", result["verdict"] == "no")
    result, _ = ask("Is the kettle boiling?", {"verdict": "yes",
                                               "reason": "Steam pours from the spout.",
                                               "missing": ""})
    check("not stove specific: steam answers the kettle", result["verdict"] == "yes")
    # Nov 2: chicken at a rolling boil, knobs unreadable.
    result, _ = ask(ON_Q, {"verdict": "yes",
                           "reason": "The pot of chicken is at a rolling boil.",
                           "missing": ""}, looks=LOOKS, safety=True)
    check("a boiling pot answers the stove", result["verdict"] == "yes")
    check("prompt names a boiling pot as a sign", "rolling boil" in verdict.SYSTEM_PROMPT)


def test_the_guards_still_hold():
    result, _ = ask(ON_Q, {"verdict": "yes", "reason": "  ", "missing": ""})
    check("empty reason: cannot_tell", result["verdict"] == "cannot_tell"
          and result["failure"] == "unexplained")
    result, _ = ask(ON_Q, {"verdict": "yes", "reason": "Flame.", "missing": "the knobs"})
    check("a verdict that says it lacked something: cannot_tell",
          result["verdict"] == "cannot_tell" and result["missing"] == "the knobs")
    result, _ = ask(OFF_Q, {"verdict": "cannot_tell", "reason": "Knobs turned away.",
                            "missing": "the knobs on the front of the stove"}, safety=True)
    check("cannot_tell stands with its missing",
          result["verdict"] == "cannot_tell"
          and result["missing"] == "the knobs on the front of the stove")
    check("result has no knobs field", "knobs" not in result)


def test_multi_mode_gets_the_same_prompt():
    with StubModel([{"verdict": "yes", "reason": "A flame is lit.", "missing": ""}]) as stub:
        result = verdict.check(FRAME, ON_Q, mode="multi", ref_yes=REF_YES, ref_no=REF_NO,
                               config=cfg(stub))
    check("multi: three images", len(stub.images()) == 3)
    check("multi: intent prompt", "Answer what the question means" in stub.system())
    check("multi: yes", result["verdict"] == "yes")


def test_compile_names_the_usual_detail_not_the_only_one():
    p = plant_compile.SYSTEM_PROMPT
    check("compile: still names the deciding detail", "decides:" in p)
    check("compile: the usual way, not the only one", "not the only one" in p)
    check("compile: another clear sign may answer", "another clear," in p)
    check("compile: else cannot_tell", "cannot_tell" in p)


if __name__ == "__main__":
    test_no_question_gets_its_own_rule()
    test_prompt_asks_for_the_intent()
    test_safety_holds_the_all_clear_only()
    test_a_sign_answers_when_the_detail_is_unreadable()
    test_the_guards_still_hold()
    test_multi_mode_gets_the_same_prompt()
    test_compile_names_the_usual_detail_not_the_only_one()
    print("ALL PASS")
