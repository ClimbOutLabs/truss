#!/usr/bin/env python3
"""
Tests for the stove check: its verdict rests on the knob positions.

Run from the truss-server folder:  python3 test_knobs.py
Stdlib only. Exits non-zero on the first failure.

Every check goes to the stub chat/completions server from
test_endpoint.py; no real model is called. The stub plays a model
that reports the knobs and then gives a verdict, and the tests hold
that verdict to the report: off only when every knob is in view and
off, on when any knob is clearly on, and cannot_tell otherwise, above
all when the knobs are out of view.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import verdict  # noqa: E402
from test_endpoint import FRAME, REF_NO, REF_YES, StubModel, cfg, check  # noqa: E402

OFF_Q = "Is the stove off?"
ON_Q = "Did I leave the stove on?"

ALL_OFF = [
    {"where": "front left", "position": "off"},
    {"where": "back left", "position": "off"},
    {"where": "back right", "position": "off"},
    {"where": "front right", "position": "off"},
]


def reply(in_view, knobs, v, reason="The knobs are read.", missing=""):
    return {"knobs_in_view": in_view, "knobs": knobs, "verdict": v,
            "reason": reason, "missing": missing}


def ask(question, model_reply, **kw):
    with StubModel([model_reply]) as stub:
        result = verdict.check(FRAME, question, config=cfg(stub), **kw)
    return result, stub


def test_which_questions_are_stove_questions():
    p = verdict.stove_polarity
    check("polarity: is the stove off", p("Is the stove off?") == "off")
    check("polarity: turned off", p("Is the stove turned off?") == "off")
    check("polarity: burners", p("Are all the burners off?") == "off")
    check("polarity: cooktop", p("Is the cooktop off?") == "off")
    check("polarity: did I turn off the stove", p("Did I turn off the stove?") == "off")
    check("polarity: left on", p("Did I leave the stove on?") == "on")
    check("polarity: case does not matter", p("IS THE STOVE OFF") == "off")
    check("polarity: not a stove", p("Is the front door locked?") is None)
    check("polarity: not a stove, says off", p("Is the TV off?") is None)
    check("polarity: a stove with no state is not held", p("Is the stove clean?") is None)
    check("polarity: both words is not guessed", p("Is the stove on or off?") is None)
    check("polarity: stovepipe is not a stove", p("Is the stovepipe off?") is None)
    check("polarity: not text", p(None) is None)
    check("polarity: negated, not on", p("Is the stove not on?") is None)
    check("polarity: forget to turn off", p("Did I forget to turn the stove off?") is None)
    check("polarity: forgot to turn off", p("Did I forget to turn off the stove?") is None)
    check("polarity: isn't", p("Isn't the stove off?") is None)
    check("polarity: never", p("Did I never turn the stove off?") is None)
    check("polarity: without", p("Did I leave without turning the stove off?") is None)


def test_request_asks_for_the_knobs_first():
    _, stub = ask(OFF_Q, reply("all", ALL_OFF, "yes"))
    rf = stub.body()["response_format"]["json_schema"]
    check("request: the stove schema is used", rf["schema"] == verdict.KNOB_SCHEMA)
    check("request: knobs come before the verdict",
          list(rf["schema"]["properties"])[:3] == ["knobs_in_view", "knobs", "verdict"])
    check("request: the system prompt says only the knobs decide",
          "only its control knobs decide" in stub.system())
    _, stub = ask("Is the front door locked?",
                  {"verdict": "yes", "reason": "Bolt across.", "missing": ""})
    check("request: other questions keep the plain schema",
          stub.body()["response_format"]["json_schema"]["schema"] == verdict.VERDICT_SCHEMA)
    check("request: other questions get no knob instruction",
          "control knobs" not in stub.system())


def test_all_knobs_off_is_off():
    result, _ = ask(OFF_Q, reply("all", ALL_OFF, "yes", "All four knobs point to off."))
    check("all off: yes", result["verdict"] == "yes" and result["failure"] is None)
    check("all off: reason kept", result["reason"] == "All four knobs point to off.")
    check("all off: the knob report is in the result",
          result["knobs"] == {"in_view": "all", "knobs": ALL_OFF})
    result, _ = ask(ON_Q, reply("all", ALL_OFF, "no", "All four knobs point to off."))
    check("all off, asked if on: no", result["verdict"] == "no" and result["failure"] is None)


def test_a_knob_on_is_on():
    knobs = [dict(k) for k in ALL_OFF]
    knobs[2]["position"] = "on"
    result, _ = ask(OFF_Q, reply("all", knobs, "no", "The back right knob is turned to high."))
    check("one on: no", result["verdict"] == "no" and result["failure"] is None)
    result, _ = ask(ON_Q, reply("all", knobs, "yes", "The back right knob is turned to high."))
    check("one on, asked if on: yes", result["verdict"] == "yes")
    seen = [{"where": "front left", "position": "on"}]
    result, _ = ask(OFF_Q, reply("some", seen, "no", "The front left knob is on."))
    check("one on, others out of view: still no", result["verdict"] == "no")


def test_knobs_out_of_view_is_cannot_tell():
    # The model calls it off from the dark burners with no knob seen.
    result, _ = ask(OFF_Q, reply("none", [], "yes", "The burners are dark."))
    check("no knobs, model said yes: cannot_tell", result["verdict"] == "cannot_tell")
    check("no knobs, model said yes: counted as inconsistent",
          result["failure"] == "inconsistent")
    check("no knobs: missing names the knobs", "knobs" in result["missing"])
    check("no knobs: the model's burner reason is not kept",
          "burners are dark" not in result["reason"])
    # Two of four knobs seen, both off: not enough for an all-clear.
    result, _ = ask(OFF_Q, reply("some", ALL_OFF[:2], "yes", "The left knobs are off."))
    check("half the knobs off: cannot_tell", result["verdict"] == "cannot_tell")
    check("half the knobs off: missing names the knobs", "knobs" in result["missing"])
    # Nor is it enough for "it is not on".
    result, _ = ask(ON_Q, reply("some", ALL_OFF[:2], "no", "The left knobs are off."))
    check("half the knobs off, asked if on: cannot_tell", result["verdict"] == "cannot_tell")
    # "all" in view but the list is empty is no reading at all.
    result, _ = ask(OFF_Q, reply("all", [], "yes", "All off."))
    check("all in view with no knobs listed: cannot_tell", result["verdict"] == "cannot_tell")
    # "none" in view cannot carry a knob reading.
    result, _ = ask(OFF_Q, reply("none", ALL_OFF, "yes", "Off."))
    check("none in view with knobs listed: cannot_tell", result["verdict"] == "cannot_tell")


def test_an_unclear_knob_is_cannot_tell():
    knobs = [dict(k) for k in ALL_OFF]
    knobs[1]["position"] = "unclear"
    result, _ = ask(OFF_Q, reply("all", knobs, "yes", "Three knobs off, one blurred."))
    check("unclear knob: cannot_tell", result["verdict"] == "cannot_tell")
    check("unclear knob: counted as inconsistent", result["failure"] == "inconsistent")


def test_verdict_against_its_own_knobs_is_not_used():
    knobs = [dict(k) for k in ALL_OFF]
    knobs[0]["position"] = "on"
    result, _ = ask(OFF_Q, reply("all", knobs, "yes", "The stove is off."))
    check("yes with a knob on: cannot_tell", result["verdict"] == "cannot_tell")
    check("yes with a knob on: inconsistent", result["failure"] == "inconsistent")
    check("yes with a knob on: the reason says why", "did not match the knobs" in result["reason"])
    result, _ = ask(OFF_Q, reply("all", ALL_OFF, "no", "A pot is on the stove."))
    check("no with every knob off: cannot_tell", result["verdict"] == "cannot_tell")


def test_model_may_be_more_careful_than_its_knobs():
    result, _ = ask(OFF_Q, reply("all", ALL_OFF, "cannot_tell", "Glare on the knobs.",
                                 "A photo without the glare."))
    check("careful cannot_tell stands", result["verdict"] == "cannot_tell")
    check("careful cannot_tell: an answer, not a failure", result["failure"] is None)
    check("careful cannot_tell: its own missing is kept",
          result["missing"] == "A photo without the glare.")
    result, _ = ask(OFF_Q, reply("none", [], "cannot_tell", "The knobs are out of frame.", ""))
    check("cannot_tell with no missing: the knobs are named", "knobs" in result["missing"])


def test_the_usual_guards_still_hold():
    result, _ = ask(OFF_Q, reply("all", ALL_OFF, "yes", "   "))
    check("empty reason: cannot_tell", result["verdict"] == "cannot_tell")
    check("empty reason: unexplained", result["failure"] == "unexplained")
    result, _ = ask(OFF_Q, reply("all", ALL_OFF, "yes", "Off.", "The back knobs."))
    check("yes that says something was missing: cannot_tell", result["verdict"] == "cannot_tell")
    # A plain verdict with no knob report never stands for a stove.
    with StubModel([{"verdict": "yes", "reason": "Burners dark.", "missing": ""}]) as stub:
        result = verdict.check(FRAME, OFF_Q, config=cfg(stub))
    check("no knob report: cannot_tell", result["verdict"] == "cannot_tell")
    check("no knob report: retried once", len(stub.requests) == 2)
    bad = reply("all", [{"where": "front", "position": "low"}], "yes")
    result, _ = ask(OFF_Q, bad)
    check("unknown knob position: cannot_tell", result["verdict"] == "cannot_tell")
    many = reply("all", ALL_OFF * 4, "yes")
    result, _ = ask(OFF_Q, many)
    check("too many knobs: cannot_tell", result["verdict"] == "cannot_tell")
    result, _ = ask("Is the front door locked?",
                    {"verdict": "yes", "reason": "Bolt across.", "missing": ""})
    check("other questions: no knob report", result["knobs"] is None)


def test_multi_mode_is_held_to_the_knobs_too():
    with StubModel([reply("none", [], "yes", "Matches the yes photo.")]) as stub:
        result = verdict.check(FRAME, OFF_Q, mode="multi", ref_yes=REF_YES,
                               ref_no=REF_NO, config=cfg(stub))
    check("multi: three images sent", len(stub.images()) == 3)
    check("multi: knob instruction sent", "only its control knobs decide" in stub.system())
    check("multi: knobs out of view is cannot_tell", result["verdict"] == "cannot_tell")


def test_failed_check_has_no_knob_report():
    with StubModel([500]) as stub:
        result = verdict.check(FRAME, OFF_Q, config=cfg(stub))
    check("failed check: cannot_tell", result["verdict"] == "cannot_tell")
    check("failed check: no knob report", result["knobs"] is None)
    check("failed check: result is JSON", json.dumps(result) is not None)


if __name__ == "__main__":
    test_which_questions_are_stove_questions()
    test_request_asks_for_the_knobs_first()
    test_all_knobs_off_is_off()
    test_a_knob_on_is_on()
    test_knobs_out_of_view_is_cannot_tell()
    test_an_unclear_knob_is_cannot_tell()
    test_verdict_against_its_own_knobs_is_not_used()
    test_model_may_be_more_careful_than_its_knobs()
    test_the_usual_guards_still_hold()
    test_multi_mode_is_held_to_the_knobs_too()
    test_failed_check_has_no_knob_report()
    print("ALL PASS")
