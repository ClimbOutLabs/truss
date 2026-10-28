#!/usr/bin/env python3
"""
Tests for safety questions and coverage-aware missed-window alerts:
the safety flag, the three coverage cases, the scan that keeps every
alert sentence free of action verbs, and the state check spoken after
a missed window.

Run from the truss-server folder:  python3 test_safety.py
Stdlib only. Exits non-zero on the first failure.
"""

import json
import re
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import alerts as alerts_module  # noqa: E402
import checkloop as checkloop_module  # noqa: E402
from alerts import AlertStore, miss_line, sweep_missed  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from coverage import FrameLog, coverage  # noqa: E402
from questions import QuestionStore  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"
WINDOW = {"kind": "window", "after": "21:00", "until": "22:00"}
PILLS = "Did I take my night pills?"
LOOK_AT = "the {weekday} PM compartment"
DAY = datetime(2026, 10, 27)  # a Tuesday
AFTER_CLOSE = datetime(2026, 10, 27, 22, 30)


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def at(hour, minute):
    return DAY.replace(hour=hour, minute=minute)


def verdict(v, reason=""):
    return {"verdict": v, "reason": reason, "latency_ms": 1, "usage": {}}


def edit_watch(store_root, slug, **changes):
    path = Path(store_root) / slug / "watch.json"
    watch = json.loads(path.read_text())
    watch.update(changes)
    path.write_text(json.dumps(watch, indent=2) + "\n")


def planted(store, text=PILLS, kind="done", safety=True, look_at=LOOK_AT):
    slug = store.create(text, WINDOW, kind=kind, safety=safety, look_at=look_at)
    store.save_ref(slug, "yes", JPEG)
    store.save_ref(slug, "no", JPEG)
    edit_watch(store.root, slug, planted_at="2026-10-27T08:00:00")
    return slug


def sweep_rig():
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    alerts = AlertStore(tmp / "alerts")
    frames = FrameLog(tmp / "log")
    return tmp, store, alerts, frames


# -- the action-verb scan ----------------------------------------------------

# Verbs that would tell her to do the act itself. The scan is
# deliberately stricter than "imperative": none of these words may
# appear anywhere in a template, in any mood, so no rewording can
# slip an instruction to act past it. "Check" and "look" are the only
# directions an alert gives.
ACTION_VERBS = {
    "take", "turn", "feed", "lock", "unlock", "switch", "shut", "close",
    "open", "swallow", "give", "eat", "drink", "light", "pour", "put",
    "press", "push", "pull", "grab", "use", "refill", "fill", "do", "go",
    "finish", "start", "stop", "remember", "forget",
}


def action_verbs_in(text):
    """The action verbs in a text, with {placeholders} removed."""
    bare = re.sub(r"\{[a-z_]+\}", " ", text)
    words = re.findall(r"[a-z']+", bare.lower())
    return sorted({w for w in words if w in ACTION_VERBS})


def all_safety_templates():
    out = {}
    for name, text in alerts_module.MISS_TEMPLATES.items():
        out[f"alerts.MISS_TEMPLATES[{name}]"] = text
    for name, text in alerts_module.SPAN_TEMPLATES.items():
        out[f"alerts.SPAN_TEMPLATES[{name}]"] = text
    for name, text in checkloop_module.MISS_CHECK_TEMPLATES.items():
        out[f"checkloop.MISS_CHECK_TEMPLATES[{name}]"] = text
    return out


def test_the_scan_catches_instructions():
    for bad in [
        "Take your night pills now.",
        "Please turn off the stove.",
        "You should feed the dog.",
        "Lock the door before bed.",
        "Don't forget your pills.",
    ]:
        check(f"the scan catches: {bad}", action_verbs_in(bad) != [])
    check("the scan ignores placeholders",
          action_verbs_in("Check {take} first.") == [])


def test_no_safety_template_instructs_the_act():
    templates = all_safety_templates()
    check("there are templates to scan", len(templates) >= 9)
    for name, text in templates.items():
        found = action_verbs_in(text)
        check(f"{name} has no action verb (found {found})", found == [])
    for name in ("look_at", "look"):
        text = alerts_module.MISS_TEMPLATES[name].lower()
        check(f"the safety direction {name} directs to check",
              text.startswith("check "))


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


WINDOW_TIMES = (
    datetime.strptime("21:00", "%H:%M").time(),
    datetime.strptime("22:00", "%H:%M").time(),
)


# -- coverage: the three cases -----------------------------------------------


def test_coverage_complete():
    frames = [at(21, 3) + timedelta(minutes=7 * i) for i in range(9)]  # 9:03 .. 9:59
    cov = coverage(frames, at(21, 0), at(22, 0), gap_minutes=10)
    check("frames every 7 minutes all window long cover it completely",
          cov["kind"] == "complete")
    cov = coverage([at(20, 58)] + frames, at(21, 0), at(22, 0), gap_minutes=10)
    check("a frame just before the window still counts toward it",
          cov["kind"] == "complete" and cov["spans"][0][0] == at(21, 0))


def test_coverage_partial():
    frames = [at(21, 10), at(21, 18), at(21, 26), at(21, 34)]
    cov = coverage(frames, at(21, 0), at(22, 0), gap_minutes=10)
    check("frames for part of the window are partial coverage",
          cov["kind"] == "partial"
          and cov["spans"] == [(at(21, 10), at(21, 34))])
    frames = [at(21, 2), at(21, 9), at(21, 40), at(21, 48), at(21, 55)]
    cov = coverage(frames, at(21, 0), at(22, 0), gap_minutes=10)
    check("a gap longer than the gap minutes is a hole: partial, two spans",
          cov["kind"] == "partial"
          and cov["spans"] == [(at(21, 2), at(21, 9)), (at(21, 40), at(21, 55))])
    cov = coverage([at(21, 30)], at(21, 0), at(22, 0), gap_minutes=0)
    check("one frame inside the window is partial even with no allowance",
          cov["kind"] == "partial")


def test_coverage_none():
    cov = coverage([], at(21, 0), at(22, 0))
    check("no frames at all is no coverage", cov["kind"] == "none")
    cov = coverage([at(20, 0), at(20, 57), at(22, 4), at(23, 0)],
                   at(21, 0), at(22, 0), gap_minutes=10)
    check("frames only outside the window are no coverage",
          cov["kind"] == "none" and cov["spans"] == [])


def test_sweep_wording_for_the_three_cases():
    tail = ("Check the Tuesday PM compartment before anything else. "
            "When you look at it, I will tell you what I see.")
    opening = "It is past 10:00 PM. About your question: Did I take my night pills?"

    tmp, store, alerts, frames = sweep_rig()
    planted(store)
    for i in range(9):
        frames.record(at(21, 3) + timedelta(minutes=7 * i))
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)
    check("complete coverage: I watched and didn't see it",
          len(created) == 1
          and created[0]["coverage"] == "complete"
          and created[0]["safety"] is True
          and created[0]["line"]
          == f"{opening} I watched the whole window, from 9:00 PM to "
             f"10:00 PM, and didn't see it. {tail}")

    tmp, store, alerts, frames = sweep_rig()
    planted(store)
    for minute in (10, 18, 26, 34, 42, 50):
        frames.record(at(21, minute))
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)
    check("partial coverage: I was with you from ... to ... and didn't see it",
          created[0]["coverage"] == "partial"
          and created[0]["line"]
          == f"{opening} I was with you from 9:10 PM to 9:50 PM and "
             "didn't see it. I wasn't with you for the rest of the window. "
             f"{tail}")

    tmp, store, alerts, frames = sweep_rig()
    planted(store)
    frames.record(at(19, 0))
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)
    check("no coverage: I wasn't with you during that window",
          created[0]["coverage"] == "none"
          and created[0]["line"]
          == f"{opening} I wasn't with you during that window, from "
             f"9:00 PM to 10:00 PM, so I can't say either way. {tail}")


def test_partial_wording_with_several_spans():
    entry = {"question": PILLS, "safety": False}
    start, until = WINDOW_TIMES
    two = {"kind": "partial",
           "spans": [(at(21, 5), at(21, 5)), (at(21, 30), at(21, 45))]}
    line = miss_line(entry, until, cov=two, start=start, day=DAY.date())
    check("a one-frame span is spoken as a moment, spans joined with and",
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
    tmp, store, alerts, frames = sweep_rig()
    planted(store, look_at=None)
    created = sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)
    check("with nothing named to look at, she is sent to check for herself",
          created[0]["line"].endswith(
              "Check with your own eyes before anything else. "
              "When you look, I will tell you what I see."))


def test_frame_log_is_plain_and_forgiving():
    tmp, store, alerts, frames = sweep_rig()
    frames.record(at(21, 5))
    frames.record(at(21, 1))
    path = tmp / "log" / "2026-10-27" / "frames.txt"
    check("the frame log is a plain file of times",
          path.read_text() == "2026-10-27T21:05:00\n2026-10-27T21:01:00\n")
    with open(path, "a") as f:
        f.write("not a time\n\n")
    check("unreadable lines are skipped and times come back sorted",
          frames.times(DAY.date()) == [at(21, 1), at(21, 5)])
    check("a day with no log is no frames",
          frames.times(DAY.date() + timedelta(days=1)) == [])


# -- the safety flag -----------------------------------------------------------


def test_safety_flag_in_the_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        plain = store.create("Has the dog been fed?", WINDOW, kind="done")
        check("a question is not safety unless asked",
              store.get(plain)["safety"] is False
              and store.get(plain)["look_at"] is None)
        pills = store.create(PILLS, WINDOW, kind="done", safety=True,
                             look_at="  the {weekday} PM compartment ")
        check("a safety question keeps its flag and its look-at phrase",
              store.get(pills)["safety"] is True
              and store.get(pills)["look_at"] == "the {weekday} PM compartment")
        for bad in ("yes", 1, None):
            try:
                store.create("Is the stove off?", safety=bad)
                refused = False
            except ValueError:
                refused = True
            check(f"a safety flag of {bad!r} is refused", refused)
        try:
            store.create("Is the stove off?", look_at="x" * 200)
            refused = False
        except ValueError:
            refused = True
        check("an overlong look-at phrase is refused", refused)
        check("refused questions planted nothing", len(store.list()) == 2)
        edit_watch(tmp, plain, safety="true")
        check("a stored flag that is not a real true reads as not safety",
              store.get(plain)["safety"] is False)
        watch_path = Path(tmp) / pills / "watch.json"
        watch = json.loads(watch_path.read_text())
        for key in ("safety", "look_at", "miss_check"):
            watch.pop(key)
        watch_path.write_text(json.dumps(watch))
        check("a record from before the flag reads as not safety",
              store.get(pills)["safety"] is False
              and store.get(pills)["miss_check"] is None)


def test_only_safety_questions_owe_a_state_check():
    tmp, store, alerts, frames = sweep_rig()
    pills = planted(store)
    dog = planted(store, "Has the dog been fed?", safety=False, look_at=None)
    sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)
    check("a safety miss leaves a state check owed",
          store.get(pills)["miss_check"] == {"date": "2026-10-27", "state": "owed"})
    check("a miss that is not safety leaves none",
          store.get(dog)["miss_check"] is None)


# -- after the miss: the state check on the next frame showing it --------------


class StateChecker:
    def __init__(self, script):
        self.script = list(script)
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


def loop_rig(script):
    tmp, store, alerts, frames = sweep_rig()
    spoken = []
    clock = Clock(AFTER_CLOSE)
    loop = CheckLoop(
        store,
        StateChecker(script),
        tmp / "log",
        speak_fn=lambda text, entry, evidence: spoken.append(text),
        now_fn=clock,
    )
    captures = tmp / "captures"
    captures.mkdir()
    return tmp, store, alerts, frames, loop, spoken, clock, captures


def frame(captures, name):
    path = captures / name
    path.write_bytes(JPEG)
    return path


def test_state_check_after_a_miss():
    tmp, store, alerts, frames, loop, spoken, clock, captures = loop_rig([
        verdict("cannot_tell", "The organizer is not in view."),
        RuntimeError("model call failed"),
        verdict("yes", "The Tuesday PM compartment is empty."),
        verdict("yes", "The Tuesday PM compartment is empty."),
    ])
    slug = planted(store)
    sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)

    first = frame(captures, "1.jpg")
    result = loop.process_frame(first)
    check("the owed check is made under a state prompt",
          loop.check_fn.calls[0]["question"].startswith(
              "Judge only how things are in this frame right now")
          and PILLS in loop.check_fn.calls[0]["question"])
    check("the state check carries the question's reference looks",
          loop.check_fn.calls[0]["ref_yes_path"] == store.get(slug)["ref_yes_path"])
    check("a cannot_tell is silent and the frame is deleted",
          spoken == [] and result["speak"] == [] and not first.exists())

    clock.now = AFTER_CLOSE + timedelta(minutes=5)
    loop.process_frame(frame(captures, "2.jpg"))
    check("the state check obeys the gap between checks",
          len(loop.check_fn.calls) == 1)

    clock.now = AFTER_CLOSE + timedelta(minutes=20)
    result = loop.process_frame(frame(captures, "3.jpg"))
    check("a failed state check is silent and still owed",
          spoken == [] and store.get(slug)["miss_check"]["state"] == "owed")

    clock.now = AFTER_CLOSE + timedelta(minutes=40)
    result = loop.process_frame(frame(captures, "4.jpg"))
    expected = ("About your question, Did I take my night pills? "
                "Here is what I see now: The Tuesday PM compartment is empty.")
    check("the next frame showing the thing speaks what is there",
          spoken == [expected] and result["speak"] == [expected])
    check("that frame is kept as evidence",
          result["evidence"] is not None and Path(result["evidence"]).exists())
    log_text = Path(result["evidence"]).parent.joinpath("answers.txt").read_text()
    check("the answer log marks the state check after a missed window",
          "state check after a missed window | yes" in log_text)
    check("a state check is not a witness: no done time is ever claimed",
          store.witness_today(slug, now=clock.now) is None)

    clock.now = AFTER_CLOSE + timedelta(minutes=60)
    loop.process_frame(frame(captures, "5.jpg"))
    check("the state check result is spoken once",
          spoken == [expected] and len(loop.check_fn.calls) == 3)


def test_state_check_no_and_no_reason():
    tmp, store, alerts, frames, loop, spoken, clock, captures = loop_rig(
        [verdict("no")]
    )
    planted(store)
    sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)
    loop.process_frame(frame(captures, "1.jpg"))
    check("a no with no reason is described against her own looks",
          spoken == ["About your question, Did I take my night pills? "
                     "What I see now looks like your no look."])


def test_state_check_is_for_today_only():
    tmp, store, alerts, frames, loop, spoken, clock, captures = loop_rig(
        [verdict("yes", "The compartment is empty.")]
    )
    planted(store)
    sweep_missed(store, alerts, now=AFTER_CLOSE, frames=frames)
    clock.now = datetime(2026, 10, 28, 7, 30)
    f = frame(captures, "1.jpg")
    loop.process_frame(f)
    check("yesterday's owed check is never offered a frame",
          loop.check_fn.calls == [] and spoken == [])


def test_no_state_check_without_a_miss():
    tmp, store, alerts, frames, loop, spoken, clock, captures = loop_rig(
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


def post(url, body, content_type):
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
        server_module.FRAMES = FrameLog(tmp / "log")
        server_module.CAPTURES_DIR = tmp / "captures"
        server_module.CAPTURES_DIR.mkdir()
        httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            status, body = post(
                base + "/plant/question",
                json.dumps({"text": PILLS, "kind": "done", "pattern": WINDOW,
                            "safety": True, "look_at": LOOK_AT}).encode(),
                "application/json",
            )
            check("planting a safety question returns 200 and says so",
                  status == 200 and body["safety"] is True)
            status, _ = post(
                base + "/plant/question",
                json.dumps({"text": "Is the stove off?",
                            "safety": "yes"}).encode(),
                "application/json",
            )
            check("a safety flag that is not true or false is refused",
                  status == 400)
            with urllib.request.urlopen(base + "/questions") as resp:
                listing = json.loads(resp.read())
            check("the listing carries the safety flag",
                  [q["safety"] for q in listing["questions"]] == [True])

            img, ctype = multipart(JPEG)
            post(base + "/capture", img, ctype)
            today = datetime.now().date()
            check("a capture leaves its arrival time in the frame log",
                  len(server_module.FRAMES.times(today)) == 1)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    test_the_scan_catches_instructions()
    test_no_safety_template_instructs_the_act()
    test_rendered_safety_lines_have_no_action_verbs()
    test_coverage_complete()
    test_coverage_partial()
    test_coverage_none()
    test_sweep_wording_for_the_three_cases()
    test_partial_wording_with_several_spans()
    test_safety_without_look_at()
    test_frame_log_is_plain_and_forgiving()
    test_safety_flag_in_the_store()
    test_only_safety_questions_owe_a_state_check()
    test_state_check_after_a_miss()
    test_state_check_no_and_no_reason()
    test_state_check_is_for_today_only()
    test_no_state_check_without_a_miss()
    test_http()
    print("ALL PASS")
