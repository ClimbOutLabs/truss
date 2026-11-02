#!/usr/bin/env python3
"""
Tests for safety questions and coverage-aware missed-window alerts:
the safety flag, the three coverage cases read from frames actually
checked, the scan that keeps every alert sentence free of action
verbs, and the state check spoken after a missed window.

Run from the truss-server folder:  python3 test_safety.py
Stdlib only. Exits non-zero on the first failure.
"""

import json
import os
import sys
import tempfile
import threading
import time as time_module
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import alerts as alerts_module  # noqa: E402
import checkloop as checkloop_module  # noqa: E402
from alerts import AlertStore, miss_line, sweep_missed  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from coverage import CheckLog, coverage  # noqa: E402
from questions import QuestionStore  # noqa: E402
from wording import action_verbs_in  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"
WINDOW = {"kind": "window", "after": "21:00", "until": "22:00"}
PILLS = "Did I take my night pills?"
LOOK_AT = "the {weekday} PM compartment"
DAY = datetime(2026, 10, 27)  # a Tuesday
AFTER_CLOSE = datetime(2026, 10, 27, 22, 30)
TAIL = ("Check the Tuesday PM compartment first, before you act on this. "
        "When I see it, I'll tell you what's there.")
OPENING = "It is past 10:00 PM. About your question: Did I take my night pills?"


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def at(hour, minute, second=0):
    return DAY.replace(hour=hour, minute=minute, second=second)


def verdict(v, reason=""):
    return {"verdict": v, "reason": reason, "latency_ms": 1, "usage": {}}


def edit_watch(store_root, slug, **changes):
    path = Path(store_root) / slug / "watch.json"
    watch = json.loads(path.read_text())
    watch.update(changes)
    path.write_text(json.dumps(watch, indent=2) + "\n")


def planted(store, text=PILLS, kind="done", safety=True, look_at=LOOK_AT,
            pattern=WINDOW):
    slug = store.create(text, pattern, kind=kind, safety=safety, look_at=look_at)
    store.save_ref(slug, "yes", JPEG)
    store.save_ref(slug, "no", JPEG)
    edit_watch(store.root, slug, planted_at="2026-10-27T08:00:00")
    return slug


def sweep_rig():
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    alerts = AlertStore(tmp / "alerts")
    checks = CheckLog(tmp / "log")
    return tmp, store, alerts, checks


# -- the action-verb scan ----------------------------------------------------


def all_safety_templates():
    out = {}
    for name, text in alerts_module.MISS_TEMPLATES.items():
        out[f"alerts.MISS_TEMPLATES[{name}]"] = text
    for name, text in alerts_module.SPAN_TEMPLATES.items():
        out[f"alerts.SPAN_TEMPLATES[{name}]"] = text
    for name, text in checkloop_module.MISS_CHECK_TEMPLATES.items():
        out[f"checkloop.MISS_CHECK_TEMPLATES[{name}]"] = text
    # The refusal is spoken for safety questions only (the stove).
    # The redo reminder is not scanned: it is spoken only after the act
    # was seen, and says what she did ("You took your night pills at
    # 9:05 PM"), never what to do.
    for name, text in checkloop_module.REFUSAL_TEMPLATES.items():
        out[f"checkloop.REFUSAL_TEMPLATES[{name}]"] = text
    return out


def test_the_scan_catches_instructions():
    for bad in [
        "Take your night pills now.",
        "Please turn off the stove.",
        "You should feed the dog.",
        "Lock the door before bed.",
        "Don't forget your pills.",
        "You were taking them earlier.",
        "The stove was turned off.",
        "The door is locked.",
        "You took them at nine.",
        "The dog was fed.",
    ]:
        check(f"the scan catches: {bad}", action_verbs_in(bad) != [])
    check("the scan ignores placeholders",
          action_verbs_in("Check {take} first.") == [])
    check("the scan leaves ordinary words alone",
          action_verbs_in("I didn't see it. I can't say either way. "
                          "Check the front door first.") == [])


def test_no_safety_template_instructs_the_act():
    templates = all_safety_templates()
    check("there are templates to scan", len(templates) >= 9)
    for name, text in templates.items():
        found = action_verbs_in(text)
        check(f"{name} has no action verb (found {found})", found == [])
    for name in ("look_at", "look"):
        text = alerts_module.MISS_TEMPLATES[name]
        check(f"the safety direction {name} says check first, before acting",
              text.startswith("Check ") and "first, before you act on this" in text)
    check("there is no bare yes or no line for the state check",
          set(checkloop_module.MISS_CHECK_TEMPLATES) == {"reason", "reason_look_at"})


WINDOW_TIMES = (
    datetime.strptime("21:00", "%H:%M").time(),
    datetime.strptime("22:00", "%H:%M").time(),
)


def test_rendered_safety_lines_have_no_action_verbs():
    """Every rendered safety line, with her own words taken out,
    carries no action verb: the scan holds for the lines as spoken,
    not only for the templates."""
    entry = {"question": PILLS, "safety": True, "look_at": LOOK_AT}
    plain = dict(entry, look_at=None)
    start, until = WINDOW_TIMES
    cases = [
        {"kind": "complete", "spans": [(at(21, 0), at(22, 0))]},
        {"kind": "partial", "spans": [(at(21, 10), at(21, 40))]},
        {"kind": "partial", "spans": [(at(21, 10), at(21, 10)),
                                      (at(21, 30), at(21, 40))]},
        {"kind": "partial", "spans": [(at(21, m), at(21, m + 2))
                                      for m in (0, 10, 20, 30, 40)]},
        {"kind": "none", "spans": []},
    ]
    for cov in cases:
        for e in (entry, plain):
            line = miss_line(e, until, cov=cov, start=start, day=DAY.date())
            spoken = line.replace(PILLS, " ").replace("the Tuesday PM compartment", " ")
            found = action_verbs_in(spoken)
            check(f"rendered {cov['kind']} line has no action verb ({found})",
                  found == [])
            check("a safety line never says the act was not done",
                  "not been done" not in line and "didn't take" not in line)


# -- coverage: the three cases -----------------------------------------------


def test_coverage_complete():
    checks = [at(21, 3) + timedelta(minutes=7 * i) for i in range(9)]  # 9:03 .. 9:59
    cov = coverage(checks, at(21, 0), at(22, 0), gap_minutes=10)
    check("checks every 7 minutes all window long cover it completely",
          cov["kind"] == "complete")
    cov = coverage([at(20, 58)] + checks, at(21, 0), at(22, 0), gap_minutes=10)
    check("a check just before the window still counts toward it",
          cov["kind"] == "complete" and cov["spans"][0][0] == at(21, 0))


def test_coverage_partial():
    checks = [at(21, 10), at(21, 18), at(21, 26), at(21, 34)]
    cov = coverage(checks, at(21, 0), at(22, 0), gap_minutes=10)
    check("checks for part of the window are partial coverage",
          cov["kind"] == "partial"
          and cov["spans"] == [(at(21, 10), at(21, 34))])
    checks = [at(21, 2), at(21, 9), at(21, 40), at(21, 48), at(21, 55)]
    cov = coverage(checks, at(21, 0), at(22, 0), gap_minutes=10)
    check("a gap longer than the gap minutes is a hole: partial, two spans",
          cov["kind"] == "partial"
          and cov["spans"] == [(at(21, 2), at(21, 9)), (at(21, 40), at(21, 55))])
    cov = coverage([at(21, 30)], at(21, 0), at(22, 0), gap_minutes=0)
    check("one check inside the window is partial even with no allowance",
          cov["kind"] == "partial")


def test_coverage_none():
    cov = coverage([], at(21, 0), at(22, 0))
    check("no checks at all is no coverage", cov["kind"] == "none")
    cov = coverage([at(20, 0), at(20, 57), at(22, 4), at(23, 0)],
                   at(21, 0), at(22, 0), gap_minutes=10)
    check("checks only outside the window are no coverage",
          cov["kind"] == "none" and cov["spans"] == [])


def test_coverage_across_the_repeated_hour():
    """The night the clocks go back, 1 AM happens twice. A window
    from 00:30 to 02:30 that was watched through the first 1 AM but
    not the second has an hour-long hole, and must not read as
    complete."""
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time_module.tzset()
    try:
        night = datetime(2026, 11, 1)
        start, end = night.replace(hour=0, minute=30), night.replace(hour=2, minute=30)
        first_pass = [night.replace(hour=0, minute=30) + timedelta(minutes=5 * i)
                      for i in range(18)]  # 00:30 .. 01:55, first pass
        second_pass = [night.replace(hour=1, minute=5 * i, fold=1) for i in range(12)]
        after = [night.replace(hour=2, minute=5 * i) for i in range(7)]  # 02:00 .. 02:30

        tmp, store, alerts, checks = sweep_rig()
        for moment in first_pass + after:
            checks.record("pills", when=moment)
        cov = coverage(checks.times(night.date(), "pills"), start, end)
        check("an unwatched repeated hour is a hole, not complete",
              cov["kind"] == "partial" and len(cov["spans"]) == 2)

        for moment in second_pass:
            checks.record("pills", when=moment)
        cov = coverage(checks.times(night.date(), "pills"), start, end)
        check("watching both passes through 1 AM is complete",
              cov["kind"] == "complete")
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time_module.tzset()


def test_sweep_wording_for_the_three_cases():
    tmp, store, alerts, checks = sweep_rig()
    slug = planted(store)
    for i in range(9):
        checks.record(slug, when=at(21, 3) + timedelta(minutes=7 * i))
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("complete coverage: I was with you and didn't see it",
          len(created) == 1
          and created[0]["coverage"] == "complete"
          and created[0]["safety"] is True
          and created[0]["line"]
          == f"{OPENING} I was with you the whole window, from 9:00 PM to "
             f"10:00 PM, and didn't see it. {TAIL}")

    tmp, store, alerts, checks = sweep_rig()
    slug = planted(store)
    for minute in (10, 18, 26, 34, 42, 50):
        checks.record(slug, when=at(21, minute))
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("partial coverage: I was with you from ... to ... and didn't see it",
          created[0]["coverage"] == "partial"
          and created[0]["line"]
          == f"{OPENING} I was with you from 9:10 PM to 9:50 PM and "
             "didn't see it. I wasn't with you for the rest of the window. "
             f"{TAIL}")

    tmp, store, alerts, checks = sweep_rig()
    slug = planted(store)
    checks.record(slug, when=at(19, 0))
    checks.record("some-other-question", when=at(21, 30))
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("no coverage: I wasn't with you during that window, "
          "and another question's checks do not count",
          created[0]["coverage"] == "none"
          and created[0]["line"]
          == f"{OPENING} I wasn't with you during that window, from "
             f"9:00 PM to 10:00 PM, so I can't say either way. {TAIL}")


def test_partial_wording_with_several_spans():
    entry = {"question": PILLS, "safety": False}
    start, until = WINDOW_TIMES
    two = {"kind": "partial",
           "spans": [(at(21, 5), at(21, 5)), (at(21, 30), at(21, 45))]}
    line = miss_line(entry, until, cov=two, start=start, day=DAY.date())
    check("a one-check span is spoken as a moment, spans joined with and",
          "I was with you at 9:05 PM and from 9:30 PM to 9:45 PM and "
          "didn't see it." in line)
    many = {"kind": "partial",
            "spans": [(at(21, m), at(21, m + 3)) for m in (0, 12, 24, 36)]}
    line = miss_line(entry, until, cov=many, start=start, day=DAY.date())
    check("more than three spans are summed up",
          "I was with you on and off between 9:00 PM and 9:39 PM" in line)
    check("a question that is not safety gets no direction at all",
          "Check" not in line)


def test_safety_without_look_at():
    tmp, store, alerts, checks = sweep_rig()
    planted(store, look_at=None)
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("with nothing named to look at, she is sent to check for herself",
          created[0]["line"].endswith(
              "Check with your own eyes first, before you act on this. "
              "When I see it, I'll tell you what's there."))


def test_check_log_is_plain_and_forgiving():
    tmp, store, alerts, checks = sweep_rig()
    checks.record("pills", when=at(21, 5))
    checks.record("pills", when=at(21, 1))
    path = tmp / "log" / "2026-10-27" / "checks-pills.txt"
    lines = path.read_text().splitlines()
    check("the check log is a plain file of times with their offsets",
          len(lines) == 2
          and datetime.fromisoformat(lines[0]).tzinfo is not None)
    with open(path, "a") as f:
        f.write("not a time\n\n2026-10-27T21:3\n2026-10-27T21:30:00\n")
    check("unreadable, cut-short and offsetless lines are skipped; times sorted",
          [t.replace(tzinfo=None) for t in checks.times(DAY.date(), "pills")]
          == [at(21, 1), at(21, 5)])
    check("a day with no log is no checks",
          checks.times(DAY.date() + timedelta(days=1), "pills") == [])


# -- coverage comes from frames checked, not frames arriving -----------------


class StateChecker:
    """Scripts checks in order; anything past the script is
    cannot_tell. Records every call."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def __call__(self, frame_path, entry):
        self.calls.append(entry)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return verdict("cannot_tell", "The organizer is not in view.")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def loop_rig(script=None, start=AFTER_CLOSE):
    tmp, store, alerts, checks = sweep_rig()
    spoken = []
    clock = Clock(start)
    loop = CheckLoop(
        store,
        StateChecker(script),
        tmp / "log",
        speak_fn=lambda text, entry, evidence: spoken.append(text),
        now_fn=clock,
        checks=checks,
    )
    captures = tmp / "captures"
    captures.mkdir()
    return tmp, store, alerts, checks, loop, spoken, clock, captures


def frame(captures, name):
    path = captures / name
    path.write_bytes(JPEG)
    return path


def run_window(loop, clock, captures, every_seconds):
    t = at(21, 0)
    n = 0
    while t < at(22, 0):
        clock.now = t
        loop.process_frame(frame(captures, f"{n}.jpg"))
        t += timedelta(seconds=every_seconds)
        n += 1
    return n


def test_frames_inside_the_check_gap_are_not_watching():
    """The reviewer's case: frames every 30 seconds all window long,
    but a question checked only once per 15 minutes. It saw four
    frames, so it must never say it watched the whole window."""
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        start=at(21, 0))
    slug = planted(store, pattern=dict(WINDOW, minutes=15))
    arrived = run_window(loop, clock, captures, 30)
    check("120 frames arrived but the question was checked 4 times",
          arrived == 120 and len(loop.check_fn.calls) == 4)
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("so the alert does not claim the whole window",
          created[0]["coverage"] == "partial"
          and "the whole window" not in created[0]["line"]
          and "I was with you on and off between 9:00 PM and 9:45 PM"
          in created[0]["line"])
    check("the state check is owed for that question",
          store.get(slug)["miss_check"]["state"] == "owed")


def test_a_done_window_is_checked_on_every_frame():
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        start=at(21, 0))
    planted(store)
    arrived = run_window(loop, clock, captures, 60)
    check("with no gap given, every frame in the window is checked",
          len(loop.check_fn.calls) == arrived == 60)
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("and then the whole window really was watched",
          created[0]["coverage"] == "complete")


def test_failed_checks_are_not_watching():
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        script=[RuntimeError("model call failed")] * 60, start=at(21, 0))
    planted(store)
    run_window(loop, clock, captures, 60)
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("checks that failed count as no watching at all",
          created[0]["coverage"] == "none")


# -- the safety flag -----------------------------------------------------------


def test_safety_flag_in_the_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        done = store.create("Did I lock the back door?", WINDOW, kind="done")
        state = store.create("Is the stove off?")
        check("left out, a done question is a safety question",
              store.get(done)["safety"] is True)
        check("left out, a state question is not",
              store.get(state)["safety"] is False)
        dog = store.create("Has the dog been fed?", WINDOW, kind="done", safety=False)
        check("a done question can opt out explicitly",
              store.get(dog)["safety"] is False and store.get(dog)["look_at"] is None)
        pills = store.create(PILLS, WINDOW, kind="done", safety=True,
                             look_at="  the {weekday} PM compartment ")
        check("a safety question keeps its look-at phrase, trimmed",
              store.get(pills)["look_at"] == "the {weekday} PM compartment")
        for bad in ("yes", 1):
            try:
                store.create("Is the tap running?", safety=bad)
                refused = False
            except ValueError:
                refused = True
            check(f"a safety flag of {bad!r} is refused", refused)
        for bad in ("x" * 200, "the pills you should take", "the stove, turned off",
                    "tonight's PM compartment", "the Tuesday PM compartment",
                    "the {Weekday} PM compartment", "the {day} compartment",
                    "the {weekday PM compartment", "the Tue PM compartment",
                    "the TUES slot", "the Weds compartment", "the Thurs PM box",
                    "the Sat compartment", "the Mon-Fri row", "the Tuesdays row",
                    "last night's compartment", "this evening's compartment",
                    "this morning's slot", "the next day compartment",
                    "the morrow's slot"):
            try:
                store.create("Is the tap running?", look_at=bad)
                refused = False
            except ValueError:
                refused = True
            check(f"a look-at phrase is refused: {bad[:30]}", refused)
        for bad in ("Take my night pills", "Please turn off the stove?",
                    "Don't forget to lock up", "Should I take my pills?"):
            try:
                store.create(bad, WINDOW, kind="done")
                refused = False
            except ValueError:
                refused = True
            check(f"a safety question that is not asked as one is refused: {bad}",
                  refused)
        for bad in ("Take my night pills", "Have your pills now",
                    "Do take my night pills", "Did take the pills",
                    "Have Some pills now", "Do Not forget the stove",
                    "Can Make sure the stove is off", "Do Remind me",
                    "Have Got my pills"):
            try:
                store.create(bad, WINDOW, kind="done", safety=False)
                refused = False
            except ValueError:
                refused = True
            check(f"a done question not asked as one is refused, safety or not: {bad}",
                  refused)
        fridge = store.create(PILLS, WINDOW, kind="done",
                              look_at="the {weekday} night slot by the fridge")
        check("ordinary words that start like a day are not taken for one",
              store.get(fridge)["look_at"] == "the {weekday} night slot by the fridge")
        store.retire(fridge)
        rex = store.create("Has Rex been fed", WINDOW, kind="done", safety=False)
        check("a helper opener followed by a name is a question",
              store.get(rex)["question"] == "Has Rex been fed")
        unasked = store.create("Take a look at the garden", safety=False)
        check("a state question that is not safety is not gated",
              store.get(unasked)["safety"] is False)
        check("refused questions planted nothing", len(store.list()) == 7)
        edit_watch(tmp, dog, safety="false")
        check("a stored flag that is not true or false reads as its kind's default",
              store.get(dog)["safety"] is True)
        watch_path = Path(tmp) / pills / "watch.json"
        watch = json.loads(watch_path.read_text())
        for key in ("safety", "look_at", "miss_check"):
            watch.pop(key)
        watch_path.write_text(json.dumps(watch))
        check("a done record from before the flag reads as safety",
              store.get(pills)["safety"] is True
              and store.get(pills)["miss_check"] is None)


def test_only_safety_questions_owe_a_state_check():
    tmp, store, alerts, checks = sweep_rig()
    pills = planted(store)
    dog = planted(store, "Has the dog been fed?", safety=False, look_at=None)
    sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    check("a safety miss leaves a state check owed",
          store.get(pills)["miss_check"] == {"date": "2026-10-27", "state": "owed"})
    check("a miss that is not safety leaves none",
          store.get(dog)["miss_check"] is None)


def test_a_spoken_state_check_is_never_owed_again_that_day():
    tmp, store, alerts, checks = sweep_rig()
    slug = planted(store)
    store.owe_miss_check(slug, "2026-10-27")
    store.mark_miss_check_spoken(slug)
    store.owe_miss_check(slug, "2026-10-27")
    check("owing again the same day leaves a spoken check spoken",
          store.get(slug)["miss_check"]["state"] == "spoken")
    store.owe_miss_check(slug, "2026-10-28")
    check("the next day's miss owes a new check",
          store.get(slug)["miss_check"] == {"date": "2026-10-28", "state": "owed"})

    class FailingAlerts(AlertStore):
        def add(self, alert):
            raise OSError("disk full")

    tmp, store, _, checks = sweep_rig()
    slug = planted(store)
    failing = FailingAlerts(tmp / "alerts")
    sweep_missed(store, failing, now=AFTER_CLOSE, checks=checks)
    store.mark_miss_check_spoken(slug)
    sweep_missed(store, failing, now=AFTER_CLOSE, checks=checks)
    check("a retried sweep after a failed alert write never re-owes it",
          store.get(slug)["miss_check"]["state"] == "spoken")


# -- after the miss: the state check on the next frame showing it --------------


def test_state_check_after_a_miss():
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig([
        verdict("cannot_tell", "The organizer is not in view."),
        RuntimeError("model call failed"),
        verdict("yes", "The Tuesday PM compartment is empty."),
        verdict("yes", "The Tuesday PM compartment is empty."),
    ])
    slug = planted(store)
    sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)

    first = frame(captures, "1.jpg")
    result = loop.process_frame(first)
    prompt = loop.check_fn.calls[0]["question"]
    check("the owed check is made under a state prompt that asks for description",
          prompt.startswith("Look only at the Tuesday PM compartment.")
          and "Judge only how things are in this frame right now" in prompt
          and "Describe only what is visible" in prompt
          and PILLS in prompt)
    check("the state check carries the question's reference looks",
          loop.check_fn.calls[0]["ref_yes_path"] == store.get(slug)["ref_yes_path"])
    check("a cannot_tell is silent and the frame is deleted",
          spoken == [] and result["speak"] == [] and not first.exists())

    clock.now = AFTER_CLOSE + timedelta(minutes=1)
    result = loop.process_frame(frame(captures, "2.jpg"))
    check("the state check has no gap: her look a minute later is checked",
          len(loop.check_fn.calls) == 2)
    check("a failed state check is silent and still owed",
          spoken == [] and store.get(slug)["miss_check"]["state"] == "owed")

    clock.now = AFTER_CLOSE + timedelta(minutes=2)
    result = loop.process_frame(frame(captures, "3.jpg"))
    expected = ("About your question, Did I take my night pills? "
                "Here is what I see now at the Tuesday PM compartment: "
                "The Tuesday PM compartment is empty.")
    check("the next frame showing the thing speaks what is there",
          spoken == [expected] and result["speak"] == [expected])
    check("that frame is kept as evidence",
          result["evidence"] is not None and Path(result["evidence"]).exists())
    log_text = Path(result["evidence"]).parent.joinpath("answers.txt").read_text()
    check("the answer log marks the state check after a missed window",
          "state check after a missed window | yes" in log_text)
    check("a state check is not a witness: no done time is ever claimed",
          store.witness_today(slug, now=clock.now) is None)

    clock.now = AFTER_CLOSE + timedelta(minutes=3)
    loop.process_frame(frame(captures, "4.jpg"))
    check("the state check result is spoken once",
          spoken == [expected] and len(loop.check_fn.calls) == 3)


def test_a_verdict_with_no_description_is_never_spoken():
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig([
        verdict("no"),
        verdict("yes", "   "),
        {"verdict": "no", "reason": None},
        verdict("no", "The Tuesday PM compartment still holds pills."),
    ])
    slug = planted(store)
    sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    for n in range(3):
        clock.now = AFTER_CLOSE + timedelta(minutes=n)
        f = frame(captures, f"{n}.jpg")
        loop.process_frame(f)
        check(f"a bare verdict ({n}) is silent and its frame is deleted",
              spoken == [] and not f.exists())
    check("after bare verdicts the check is still owed",
          store.get(slug)["miss_check"]["state"] == "owed")
    clock.now = AFTER_CLOSE + timedelta(minutes=5)
    loop.process_frame(frame(captures, "described.jpg"))
    check("a no with a description is spoken as that description",
          spoken == ["About your question, Did I take my night pills? Here is "
                     "what I see now at the Tuesday PM compartment: "
                     "The Tuesday PM compartment still holds pills."])


def test_a_miss_settled_after_midnight_is_yesterdays():
    late = {"kind": "window", "after": "23:00", "until": "23:59"}
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        [verdict("yes", "The Tuesday PM compartment is empty.")],
        start=datetime(2026, 10, 28, 0, 20),
    )
    slug = planted(store, pattern=late)
    for i in range(12):
        checks.record(slug, when=at(23, 2) + timedelta(minutes=5 * i))
    created = sweep_missed(store, alerts, now=clock.now, checks=checks)
    check("a miss settled after midnight reads yesterday's checks and weekday",
          len(created) == 1
          and created[0]["coverage"] == "complete"
          and created[0]["line"].startswith("Yesterday's window ended at 11:59 PM.")
          and "the Tuesday PM compartment" in created[0]["line"])
    check("its state check is owed for yesterday",
          store.get(slug)["miss_check"] == {"date": "2026-10-27", "state": "owed"})
    clock.now = datetime(2026, 10, 28, 3, 0)
    loop.process_frame(frame(captures, "night.jpg"))
    check("the overnight prompt names the owed day's compartment, not today's",
          loop.check_fn.calls[0]["question"].startswith(
              "Look only at the Tuesday PM compartment.")
          and "Wednesday" not in loop.check_fn.calls[0]["question"])
    check("and is offered through the small hours, naming Tuesday's compartment",
          spoken == ["About your question, Did I take my night pills? Here is "
                     "what I see now at the Tuesday PM compartment: "
                     "The Tuesday PM compartment is empty."])

    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        [verdict("yes", "The compartment is empty.")],
        start=datetime(2026, 10, 28, 0, 20),
    )
    slug = planted(store, pattern=late)
    sweep_missed(store, alerts, now=clock.now, checks=checks)
    clock.now = datetime(2026, 10, 28, 7, 0)
    loop.process_frame(frame(captures, "morning.jpg"))
    check("but not the next morning",
          loop.check_fn.calls == [] and spoken == [])

    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        [verdict("yes", "The compartment is empty.")],
        start=datetime(2026, 10, 28, 0, 20),
    )
    planted(store, pattern=late, look_at=None)
    sweep_missed(store, alerts, now=clock.now, checks=checks)
    clock.now = datetime(2026, 10, 28, 0, 30)
    loop.process_frame(frame(captures, "unnamed.jpg"))
    check("overnight, a question naming no thing to look at is not checked",
          loop.check_fn.calls == [] and spoken == [])

    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        [verdict("yes", "The organizer has pills in it.")],
        start=datetime(2026, 10, 28, 0, 20),
    )
    planted(store, pattern=late, look_at="the pill organizer")
    sweep_missed(store, alerts, now=clock.now, checks=checks)
    clock.now = datetime(2026, 10, 28, 3, 0)
    loop.process_frame(frame(captures, "dayless.jpg"))
    check("overnight, a look-at phrase with no {weekday} is not checked",
          loop.check_fn.calls == [] and spoken == [])


def test_state_check_is_for_today_only():
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        [verdict("yes", "The compartment is empty.")]
    )
    planted(store)
    sweep_missed(store, alerts, now=AFTER_CLOSE, checks=checks)
    clock.now = datetime(2026, 10, 28, 7, 30)
    loop.process_frame(frame(captures, "1.jpg"))
    check("yesterday's owed check is never offered a frame",
          loop.check_fn.calls == [] and spoken == [])


def test_no_state_check_without_a_miss():
    tmp, store, alerts, checks, loop, spoken, clock, captures = loop_rig(
        [verdict("yes", "The compartment is empty.")]
    )
    planted(store)
    loop.process_frame(frame(captures, "1.jpg"))
    check("no state check is made before any window was missed",
          loop.check_fn.calls == [] and spoken == [])


# -- over HTTP ---------------------------------------------------------------


def multipart(image_bytes):
    boundary = "----truss-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="frame.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + image_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def post(url, body, content_type="application/json"):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_http():
    import server as server_module

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        server_module.STORE = QuestionStore(tmp / "questions")
        server_module.ALERTS = AlertStore(tmp / "alerts")
        server_module.CHECKS = CheckLog(tmp / "log")
        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        server_module.CLOCK = lambda: noon
        server_module.LOOP = CheckLoop(
            server_module.STORE,
            StateChecker(),
            tmp / "log",
            now_fn=lambda: noon,
            checks=server_module.CHECKS,
        )
        server_module.GUIDE["session"] = None
        server_module.CAPTURES_DIR = tmp / "captures"
        server_module.CAPTURES_DIR.mkdir()
        httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        all_day = {"kind": "window", "after": "00:00", "until": "23:59"}
        try:
            status, body = post(
                base + "/plant/question",
                json.dumps({"text": PILLS, "kind": "done", "pattern": all_day,
                            "look_at": LOOK_AT}).encode(),
            )
            check("a done question planted with no flag is safety, and says so",
                  status == 200 and body["safety"] is True)
            slug = body["slug"]
            status, body = post(
                base + "/plant/question",
                json.dumps({"text": "Has the dog been fed?", "kind": "done",
                            "pattern": all_day, "safety": False}).encode(),
            )
            check("a done question can opt out over HTTP",
                  status == 200 and body["safety"] is False)
            status, _ = post(
                base + "/plant/question",
                json.dumps({"text": "Is the stove off?", "safety": "yes"}).encode(),
            )
            check("a safety flag that is not true or false is refused",
                  status == 400)
            status, _ = post(
                base + "/plant/question",
                json.dumps({"text": "Is the stove off?",
                            "look_at": "the knobs, turned off"}).encode(),
            )
            check("a look-at phrase with an action verb is refused", status == 400)
            with urllib.request.urlopen(base + "/questions") as resp:
                listing = json.loads(resp.read())
            check("the listing carries the safety flag",
                  [q["safety"] for q in listing["questions"]] == [True, False])

            img, ctype = multipart(JPEG)
            post(f"{base}/plant/ref?slug={slug}&kind=yes", img, ctype)
            post(f"{base}/plant/ref?slug={slug}&kind=no", img, ctype)
            post(base + "/capture", img, ctype)
            today = noon.date()
            check("a capture the loop checks leaves a check time",
                  len(server_module.CHECKS.times(today, slug)) == 1)

            status, _ = post(
                base + "/guide/start",
                json.dumps({"title": "Water", "steps": ["Is the tap off?"]}).encode(),
            )
            check("a guide starts", status == 200)
            post(base + "/capture", img, ctype)
            check("a frame the guide takes is not watching for the question",
                  len(server_module.CHECKS.times(today, slug)) == 1)
            post(base + "/guide/stop", b"{}")
        finally:
            httpd.shutdown()
            server_module.CLOCK = datetime.now


if __name__ == "__main__":
    test_the_scan_catches_instructions()
    test_no_safety_template_instructs_the_act()
    test_rendered_safety_lines_have_no_action_verbs()
    test_coverage_complete()
    test_coverage_partial()
    test_coverage_none()
    test_coverage_across_the_repeated_hour()
    test_sweep_wording_for_the_three_cases()
    test_partial_wording_with_several_spans()
    test_safety_without_look_at()
    test_check_log_is_plain_and_forgiving()
    test_frames_inside_the_check_gap_are_not_watching()
    test_a_done_window_is_checked_on_every_frame()
    test_failed_checks_are_not_watching()
    test_safety_flag_in_the_store()
    test_only_safety_questions_owe_a_state_check()
    test_a_spoken_state_check_is_never_owed_again_that_day()
    test_state_check_after_a_miss()
    test_a_verdict_with_no_description_is_never_spoken()
    test_a_miss_settled_after_midnight_is_yesterdays()
    test_state_check_is_for_today_only()
    test_no_state_check_without_a_miss()
    test_http()
    print("ALL PASS")
