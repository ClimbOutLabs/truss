#!/usr/bin/env python3
"""
Tests for the checker's memory of tonight: a check carries this
question's earlier check results (time, verdict, reason) as context,
read back from the server log the checks were already written to.

Run from the truss-server folder:  python3 test_check_memory.py
Stdlib only. Exits non-zero on the first failure. Every model call
goes to the stub chat/completions server from test_endpoint.py; no
real model is called.
"""

import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import serverlog  # noqa: E402
import verdict  # noqa: E402
from test_endpoint import FRAME, REF_NO, REF_YES, StubModel, cfg, check  # noqa: E402

PILLS = "Did I take my night pills?"
SLUG = "did-i-take-my-night-pills-4"
DAY = date(2026, 11, 3)
EARLIER = [
    (datetime(2026, 11, 3, 23, 50), "no",
     "A red pill is visible in the Tue compartment of the pill organizer."),
    (datetime(2026, 11, 3, 23, 52), "cannot_tell",
     "The Tue PM compartment is cropped out at the bottom of the photo."),
]
YES = {"verdict": "yes", "reason": "The Tue PM compartment is empty.", "missing": ""}


def user_parts(stub):
    return stub.body()["messages"][1]["content"]


def user_text(stub):
    return " ".join(part["text"] for part in user_parts(stub) if part["type"] == "text")


def ask_single(earlier, **kwargs):
    with StubModel([YES]) as stub:
        result = verdict.check(FRAME, PILLS, config=cfg(stub), earlier=earlier, **kwargs)
    return result, stub


def test_a_check_carries_earlier_results():
    result, stub = ask_single(EARLIER)
    text = user_text(stub)
    check("the earlier label rides in the request",
          "Earlier tonight, checks of this same question found" in text)
    check("the time, verdict and reason ride",
          "- 11:50 PM: no. A red pill is visible in the Tue compartment" in text)
    check("a cannot_tell rides too, with its time",
          "- 11:52 PM: cannot_tell. The Tue PM compartment is cropped out" in text)
    check("oldest first", text.index("11:50 PM") < text.index("11:52 PM"))
    system = stub.system()
    check("the system prompt frames them as context",
          "context about what was seen earlier tonight" in system)
    check("and says they never answer for this photo",
          "an earlier result never answers for this photo" in system)
    check("the answer still comes from the model", result["verdict"] == "yes")


def test_no_history_sends_the_unchanged_request():
    _, stub_none = ask_single(None)
    _, stub_empty = ask_single([])
    check("none and empty build the same request",
          stub_none.body() == stub_empty.body())
    check("no earlier section without history",
          "Earlier tonight" not in user_text(stub_none))
    check("the system prompt carries no earlier framing either",
          stub_none.system().startswith(verdict.SYSTEM_PROMPT)
          and "earlier result never answers" not in stub_none.system())
    parts = user_parts(stub_none)
    check("the content is the question and the photo, as before",
          len(parts) == 2 and parts[0]["text"] == f"Question: {PILLS}"
          and parts[1]["type"] == "image_url")


def test_earlier_rides_in_multi_mode_too():
    with StubModel([YES]) as stub:
        verdict.check(FRAME, PILLS, mode="multi", ref_yes=REF_YES, ref_no=REF_NO,
                      config=cfg(stub), earlier=EARLIER)
    texts = [p["text"] for p in user_parts(stub) if p["type"] == "text"]
    joined = "\n".join(texts)
    check("the earlier lines ride in multi mode",
          "- 11:50 PM: no. A red pill is visible" in joined)
    check("and land before the current photo is named",
          joined.index("Earlier tonight") < joined.index("The current photo:"))


def test_many_carries_earlier_with_its_question_only():
    items = [
        {"question": PILLS, "looks": None, "safety": False, "earlier": EARLIER},
        {"question": "Is the front door locked?", "looks": None, "safety": False},
    ]
    reply = {"answers": [
        {"number": 1, "verdict": "yes", "reason": "The compartment is empty.", "missing": ""},
        {"number": 2, "verdict": "no", "reason": "The thumb turn is vertical.", "missing": ""},
    ]}
    with StubModel([reply]) as stub:
        results = verdict.check_many(FRAME, items, config=cfg(stub))
    text = user_text(stub)
    check("the earlier lines name their question",
          "For question 1, Earlier tonight, checks of this same question found" in text)
    check("only one question carries them", text.count("Earlier tonight") == 1)
    check("the context framing is in the system prompt",
          "context about what was seen earlier tonight" in stub.system())
    check("both answers still come back", [r["verdict"] for r in results] == ["yes", "no"])

    plain = [
        {"question": PILLS, "looks": None, "safety": False},
        {"question": "Is the front door locked?", "looks": None, "safety": False},
    ]
    with StubModel([reply]) as stub:
        verdict.check_many(FRAME, plain, config=cfg(stub))
    check("no history, no earlier section in a batch either",
          "Earlier tonight" not in user_text(stub)
          and "earlier result never answers" not in stub.system())


def tidy(stamp, tag, message):
    return f"{stamp} | {tag:<8} | {message}"


def write_log(lines):
    path = Path(tempfile.mkdtemp()) / "server.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_history_reads_only_this_question_today():
    log = write_log([
        tidy("2026-11-02 23:59:10", "check",
             f"{SLUG}: no (20.0 s) A line from the day before."),
        tidy("2026-11-03 23:41:00", "check",
             "is-the-stove-off: no (9.1 s) A burner is lit."),
        tidy("2026-11-03 23:44:00", "check",
             f"{SLUG}: no verdict (0.3 s) check failed: boom"),
        tidy("2026-11-03 23:47:00", "check",
             f"{SLUG}: cannot_tell (0.0 s) The model endpoint did not answer. "
             "So no check was made."),
        tidy("2026-11-03 23:48:00", "capture",
             "saved capture_20261103_234800.jpg (176142 bytes)"),
        tidy("2026-11-03 23:49:00", "check",
             "did-i-take-my-night-pills: yes (5.0 s) A different question entirely."),
        tidy("2026-11-03 23:50:30", "check",
             f"{SLUG}: no (24.7 s) A red pill is visible in the Tue compartment "
             "of the pill organizer."),
        tidy("2026-11-03 23:50:31", "check", "(the line above, 2 more times)"),
        tidy("2026-11-03 23:52:12", "check",
             f"{SLUG}: cannot_tell (55.3 s) The Tue PM compartment is cropped "
             "out at the bottom of the photo."),
    ])
    got = serverlog.check_history(log, SLUG, DAY)
    check("only this question's real results from today come back", got == [
        (datetime(2026, 11, 3, 23, 50, 30), "no",
         "A red pill is visible in the Tue compartment of the pill organizer."),
        (datetime(2026, 11, 3, 23, 52, 12), "cannot_tell",
         "The Tue PM compartment is cropped out at the bottom of the photo."),
    ])
    check("the day before reads as its own day",
          len(serverlog.check_history(log, SLUG, date(2026, 11, 2))) == 1)
    check("a missing log is no history",
          serverlog.check_history(log.parent / "nope.log", SLUG, DAY) == [])


def test_history_keeps_only_the_most_recent_six():
    lines = [
        tidy(f"2026-11-03 23:0{i}:00", "check",
             f"{SLUG}: no (1{i}.0 s) Reason {i}.")
        for i in range(1, 9)
    ]
    got = serverlog.check_history(write_log(lines), SLUG, DAY)
    check("the cap holds at six", len(got) == 6)
    check("the most recent six, oldest first",
          [reason for _, _, reason in got] == [f"Reason {i}." for i in range(3, 9)])


if __name__ == "__main__":
    test_a_check_carries_earlier_results()
    test_no_history_sends_the_unchanged_request()
    test_earlier_rides_in_multi_mode_too()
    test_many_carries_earlier_with_its_question_only()
    test_history_reads_only_this_question_today()
    test_history_keeps_only_the_most_recent_six()
    print("ALL PASS")
