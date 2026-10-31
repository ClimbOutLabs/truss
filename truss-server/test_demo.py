#!/usr/bin/env python3
"""
Tests for the demo support (item H): the spoken-line templates, the
stove refusal, the safety-held check, GET /status, the pre-flight,
and the check gaps across the night the clocks go back.

Run from the truss-server folder:  python3 test_demo.py
Stdlib only. Exits non-zero on the first failure.
"""

import json
import os
import sys
import tempfile
import threading
import time as time_module
import urllib.request
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import checkloop  # noqa: E402
import history  # noqa: E402
import preflight  # noqa: E402
import verdict  # noqa: E402
from bedtime import plant_round  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from coverage import CheckLog, coverage  # noqa: E402
from questions import QuestionStore  # noqa: E402
from wording import action_verbs_in  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"
EVENING = datetime(2026, 11, 3, 20, 31, 0)
STOVE = "Is the stove off?"
PILLS = "Did I take my night pills?"
KNOBS = "The four knobs on the front of the stove."


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def verdict_of(v, reason="", missing="", failure=None):
    return {"verdict": v, "reason": reason, "missing": missing, "failure": failure,
            "model": "muse-glimmer-30b", "latency_ms": 1, "usage": {}}


def plant(store, text, pattern, kind="state", safety=None, prompts=None, looks=True):
    slug = store.create(text, pattern, kind=kind, safety=safety)
    store.save_ref(slug, "yes", JPEG)
    store.save_ref(slug, "no", JPEG)
    if prompts:
        (store.root / slug / "prompts.txt").write_text("\n".join(prompts) + "\n")
    if looks:
        (store.root / slug / "looks.txt").write_text("What decides it: the knobs.\n")
    return slug


def frame(folder, name):
    path = Path(folder) / name
    path.write_bytes(JPEG)
    return path


# -- spoken lines -----------------------------------------------------------


def test_done_act():
    check("pills: said back in the past tense, in her words made yours",
          checkloop.done_act(PILLS) == "took your night pills")
    check("the dog", checkloop.done_act("Did I feed the dog?") == "fed the dog")
    check("a regular verb", checkloop.done_act("did i lock the front door") == "locked the front door")
    check("a verb ending in e", checkloop.done_act("Did I close my window?") == "closed your window")
    check("a verb ending in consonant y", checkloop.done_act("Did I dry the dishes?") == "dried the dishes")
    check("not a Did I question: no act", checkloop.done_act("Has the dog been fed?") is None)


def test_redo_lines():
    witness = {"date": "2026-11-03", "at": "2026-11-03T13:02:00", "frame": "x.jpg"}
    check("the redo names the act, the time, and that it was seen",
          checkloop.redo_text(witness, {"question": PILLS})
          == "You took your night pills at 1:02 PM. I saw you.")
    check("a question with no act gets the plain reminder",
          checkloop.redo_text(witness, {"question": "Has the dog been fed?"})
          == "That is already done. You did it at 1:02 PM. I saw you.")


def test_firing_lines_name_the_time():
    entry = {"question": STOVE, "kind": "state"}
    check("a state firing: question, yes, time, reason",
          checkloop.speak_text(entry, verdict_of("yes", "All four knobs point to off."), EVENING)
          == "Is the stove off? Yes, as of 8:31 PM. All four knobs point to off.")
    check("with no reason, the time still",
          checkloop.speak_text(entry, verdict_of("yes"), EVENING)
          == "Is the stove off? Yes, as of 8:31 PM.")
    done = {"question": PILLS, "kind": "done"}
    check("a done firing says when it was seen",
          checkloop.speak_text(done, verdict_of("yes", "The compartment is empty."), EVENING)
          == "Did I take my night pills? Yes. I saw it at 8:31 PM. The compartment is empty.")


# -- the refusal ---------------------------------------------------------------


def test_refusal_detail():
    check("the knobs, as a phrase",
          checkloop.refusal_detail(KNOBS) == "the four knobs on the front of the stove")
    check("a bare noun gets an article", checkloop.refusal_detail("knobs") == "the knobs")
    check("the stove check's own missing line is spoken as the thing",
          checkloop.refusal_detail(verdict.KNOBS_MISSING) == "all of the stove's control knobs")
    check("a sentence about the photo is not spoken inside the line",
          checkloop.refusal_detail("The knobs need to be in view.") is None)
    check("an action verb is never spoken", checkloop.refusal_detail("the knob you turn") is None)
    check("empty is nothing", checkloop.refusal_detail("  ") is None)
    check("not text is nothing", checkloop.refusal_detail(7) is None)
    entry = {"question": STOVE}
    check("the refusal line",
          checkloop.refusal_text(entry, verdict_of("cannot_tell", "Stovetop only.", KNOBS))
          == "Is the stove off? I can't tell. I can't see the four knobs on the front of the stove from here.")
    check("the plain refusal line",
          checkloop.refusal_text(entry, verdict_of("cannot_tell", "Hm.", "A better angle is needed."))
          == "Is the stove off? I can't tell from here.")
    for name, text in checkloop.REFUSAL_TEMPLATES.items():
        check(f"refusal template {name} holds no action verb", action_verbs_in(text) == [])


class Scripted:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, frame_path, entry):
        self.calls.append(entry["question"])
        return self.answers.pop(0) if self.answers else verdict_of("cannot_tell", "Nothing.", "the stove")


class Presence:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, frame_path, question):
        self.calls.append(question)
        return self.answers.pop(0)


def refusal_rig(checks, presence, safety=True, prompts=("stove", "knob"), kind="state"):
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    pattern = {"kind": "nightly", "after": "20:00", "minutes": 0}
    slug = plant(store, STOVE, pattern, safety=safety, prompts=prompts)
    clock = [EVENING]
    spoken = []
    loop = CheckLoop(store, Scripted(checks), tmp / "log",
                     speak_fn=lambda text, entry, evidence: spoken.append(text),
                     now_fn=lambda: clock[0], presence_fn=Presence(presence))
    captures = tmp / "captures"
    captures.mkdir()
    return tmp, store, slug, loop, spoken, clock, captures


def test_refusal_in_the_loop():
    cannot = verdict_of("cannot_tell", "Only the stovetop is in view.", KNOBS)
    tmp, store, slug, loop, spoken, clock, captures = refusal_rig(
        [cannot, cannot, cannot, verdict_of("yes", "All four knobs point to off.")],
        [verdict_of("yes", "The stovetop is in view."), verdict_of("yes", "Stove.")],
    )
    f = frame(captures, "doorway.jpg")
    result = loop.process_frame(f)
    line = "Is the stove off? I can't tell. I can't see the four knobs on the front of the stove from here."
    check("the refusal is spoken once the stove is confirmed in view", spoken == [line])
    check("the presence check asks about the stove itself",
          "stove" in loop.presence_fn.calls[0] and "knob" not in loop.presence_fn.calls[0])
    check("the result names the refused question", result["refused"] == [slug])
    check("nothing fired", result["fired"] == [])
    check("the refusal frame is kept as evidence", result["evidence"] and Path(result["evidence"]).exists())
    day = tmp / "log" / EVENING.strftime("%Y-%m-%d")
    items = history.answers(tmp / "log", EVENING.strftime("%Y-%m-%d"))
    check("History shows it as a cannot-tell with its photo and model",
          len(items) == 1 and items[0]["kind"] == "cannot_tell" and items[0]["text"] == line
          and items[0]["evidence"] and items[0]["model"] == "muse-glimmer-30b")
    check("the stove stays standing", store.get(slug)["state"] == "standing")

    clock[0] += timedelta(minutes=2)
    loop.process_frame(frame(captures, "still-doorway.jpg"))
    check("inside the rest, a second refusal is not spoken", len(spoken) == 1)
    check("and no presence check is spent on it", len(loop.presence_fn.calls) == 1)

    clock[0] += timedelta(minutes=10)
    loop.process_frame(frame(captures, "later.jpg"))
    check("after the rest, it may be said again", len(spoken) == 2)

    clock[0] += timedelta(minutes=1)
    loop.process_frame(frame(captures, "closer.jpg"))
    check("stepping closer, the yes is spoken with its time",
          spoken[-1] == "Is the stove off? Yes, as of 8:44 PM. All four knobs point to off.")
    check("the answer log holds both", (day / "answers.txt").read_text().count("\n") == 3)


def test_no_refusal_when_the_stove_is_not_there():
    cannot = verdict_of("cannot_tell", "A hallway.", KNOBS)
    tmp, store, slug, loop, spoken, clock, captures = refusal_rig(
        [cannot], [verdict_of("no", "No stove in this photo.")]
    )
    f = frame(captures, "hall.jpg")
    result = loop.process_frame(f)
    check("a hallway frame stays silent", spoken == [] and result["refused"] == [])
    check("and is deleted like any frame nothing fired on", not f.exists())


def test_refusal_needs_every_condition():
    cannot = verdict_of("cannot_tell", "Stovetop only.", KNOBS)
    seen = verdict_of("yes", "The stove is in view.")
    cases = [
        ("a question that is not safety", dict(safety=False)),
        ("no prompts.txt to name the stove", dict(prompts=None)),
    ]
    for name, kwargs in cases:
        tmp, store, slug, loop, spoken, clock, captures = refusal_rig([cannot], [seen], **kwargs)
        loop.process_frame(frame(captures, "a.jpg"))
        check(f"{name}: silent", spoken == [] and loop.presence_fn.calls == [])
    for name, answer in [
        ("a failed check is not a refusal",
         verdict_of("cannot_tell", "No check was made.", "", failure="timeout")),
        ("a cannot-tell naming nothing is not a refusal", verdict_of("cannot_tell", "Dark.", "")),
        ("a no is not a refusal", verdict_of("no", "Knob two points to high.")),
    ]:
        tmp, store, slug, loop, spoken, clock, captures = refusal_rig([answer], [seen])
        loop.process_frame(frame(captures, "a.jpg"))
        check(f"{name}: silent", spoken == [] and loop.presence_fn.calls == [])
    tmp, store, slug, loop, spoken, clock, captures = refusal_rig([cannot], [seen])
    loop.presence_fn = None
    loop.process_frame(frame(captures, "a.jpg"))
    check("with no presence check, every cannot-tell is silent", spoken == [])

    def broken(frame_path, question):
        raise RuntimeError("down")

    tmp, store, slug, loop, spoken, clock, captures = refusal_rig([cannot], [seen])
    loop.presence_fn = broken
    result = loop.process_frame(frame(captures, "a.jpg"))
    check("a presence check that fails is silent, not an error",
          spoken == [] and result["fired"] == [])


def test_bedtime_round_marks_safety():
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    slugs = plant_round(store)["slugs"]
    flags = [store.get(slug)["safety"] for slug in slugs]
    check("the stove and the pills are safety questions, the door is not",
          flags == [True, False, True])
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    store.create(STOVE, {"kind": "nightly", "after": "20:00", "minutes": 1})
    notes = plant_round(store)["notes"]
    check("a stove planted before without the flag is kept, and said",
          any("without the safety flag" in note for note in notes))


# -- the safety-held check -------------------------------------------------------


def test_safety_instruction():
    photo = Path(tempfile.mkdtemp()) / "f.jpg"
    photo.write_bytes(JPEG)
    system, _ = verdict._request(photo, STOVE, "single", None, None, None, safety=True)
    check("a safety check is held to the deciding detail",
          verdict.SAFETY_INSTRUCTION in system and "flame" in system)
    system, _ = verdict._request(photo, STOVE, "single", None, None, None)
    check("an ordinary check is not", verdict.SAFETY_INSTRUCTION not in system)
    check("missing is asked for as a short noun phrase",
          "short noun phrase" in verdict.SYSTEM_PROMPT)


# -- the redo evidence -------------------------------------------------------------


def test_redo_log_shows_the_witness_photo():
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    window = {"kind": "window", "after": "12:45", "until": "14:30", "minutes": 0}
    slug = plant(store, PILLS, window, kind="done")
    clock = [datetime(2026, 11, 3, 13, 2)]

    def checker(frame_path, entry):
        if entry["question"].startswith("Is the wearer at the same place"):
            return verdict_of("yes", "Her hand is at the organizer again.")
        return verdict_of("yes", "The Tuesday PM compartment is empty.")

    spoken = []
    loop = CheckLoop(store, checker, tmp / "log", now_fn=lambda: clock[0],
                     speak_fn=lambda text, entry, evidence: spoken.append(text))
    captures = tmp / "captures"
    captures.mkdir()
    first = loop.process_frame(frame(captures, "act.jpg"))
    clock[0] = datetime(2026, 11, 3, 13, 40)
    second = loop.process_frame(frame(captures, "reach.jpg"))
    check("the stopped reach is spoken with the witnessed time",
          spoken[-1] == "You took your night pills at 1:02 PM. I saw you.")
    items = history.answers(tmp / "log", "2026-11-03")
    redo = items[0]
    check("History shows the redo with the witness photo, the moment she was seen",
          redo["kind"] == "redo" and redo["evidence"] == Path(first["evidence"]).name)
    check("the return frame is kept and not read as spoken text",
          Path(second["evidence"]).exists() and "now:" not in redo["text"])


# -- GET /status and the pre-flight -------------------------------------------------


def get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def test_status_endpoint():
    import server as server_module

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        server_module.STORE = QuestionStore(tmp / "questions")
        server_module.CAPTURES_DIR = tmp / "captures"
        server_module.CAPTURES_DIR.mkdir()
        server_module.GUIDE["session"] = None
        server_module.APP_SEEN.update(poll=None, capture=None)
        httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            body = get(base + "/status")
            check("status: the clock carries its offset", body["now"][-6] in "+-")
            check("status: the checker and model are named",
                  body["checker"] == "endpoint" and body["model"])
            check("status: the phone has not been heard from yet", body["last_poll"] is None)
            get(base + "/alerts")
            body = get(base + "/status")
            check("status: an alerts poll counts as the phone",
                  body["last_poll"]["from"] == "127.0.0.1" and body["last_poll"]["seconds_ago"] <= 2)
            check("status: no photo yet", body["last_capture"] is None)
            lines = preflight.check_phone(body)
            check("pre-flight: a fresh poll passes the phone", lines[0][0] == preflight.PASS)
            lines = preflight.check_server(body)
            check("pre-flight: the default server passes",
                  all(level == preflight.PASS for level, _, _ in lines))
        finally:
            httpd.shutdown()


def test_preflight_server_rules():
    base = {"now": "x", "checker": "endpoint", "model": "muse-glimmer-30b",
            "base_url": "http://dgx:8080/v1", "check_mode": "single", "refusals": True,
            "bedtime_after": "20:00", "plant_minutes": None}
    levels = lambda status: [level for level, _, _ in preflight.check_server(status)]
    check("no server is a FAIL", levels(None) == [preflight.FAIL])
    check("Aman is a FAIL", preflight.FAIL in levels(dict(base, checker="aman")))
    check("the contributor tier is a FAIL",
          preflight.FAIL in levels(dict(base, model="muse-spark-1.3-contributor")))
    check("a model that is not Meta's warns", preflight.WARN in levels(dict(base, model="llava")))
    check("live-test settings warn",
          levels(dict(base, plant_minutes="0", bedtime_after="00:00")).count(preflight.WARN) == 2)
    check("refusals off is a FAIL", preflight.FAIL in levels(dict(base, refusals=False)))
    check("a silent phone is a FAIL",
          preflight.check_phone({"last_poll": {"seconds_ago": 400, "from": "x"}})[0][0] == preflight.FAIL)
    check("no photo yet warns",
          preflight.check_glasses({"last_capture": None})[0][0] == preflight.WARN)

    states = [{"last_capture": None}, {"last_capture": None},
              {"last_capture": {"at": "t1", "from": "10.0.0.7"}}]
    result = preflight.check_glasses({"last_capture": None}, lambda: states.pop(0),
                                      wait_s=30, say=lambda *_: None, sleep=lambda _: None)
    check("waiting for a photo passes when one arrives",
          result[0][0] == preflight.PASS and "10.0.0.7" in result[0][2])


class FakeDGX(BaseHTTPRequestHandler):
    health = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = {"status": "ok"} if FakeDGX.health == 200 else {"error": "loading"}
            code = FakeDGX.health
        elif self.path == "/v1/models":
            body, code = {"data": [{"id": "muse-glimmer-30b"}]}, 200
        else:
            body, code = {}, 404
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def test_preflight_llama():
    httpd = HTTPServer(("127.0.0.1", 0), FakeDGX)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    try:
        FakeDGX.health = 200
        lines = preflight.check_llama(base)
        check("llama-server healthy, model named",
              lines[0][0] == preflight.PASS and "muse-glimmer-30b" in lines[0][2])
        FakeDGX.health = 503
        lines = preflight.check_llama(base)
        check("llama-server still loading is a FAIL",
              lines[0][0] == preflight.FAIL and "loading" in lines[0][2].lower())
    finally:
        httpd.shutdown()
    check("nothing answering is a FAIL",
          preflight.check_llama("http://127.0.0.1:9/v1")[0][0] == preflight.FAIL)
    check("SAM down with the outline off only warns",
          preflight.check_sam({"sam_url": "http://127.0.0.1:9", "evidence_outline": False})[0][0]
          == preflight.WARN)
    check("SAM down with the outline on is a FAIL",
          preflight.check_sam({"sam_url": "http://127.0.0.1:9", "evidence_outline": True})[0][0]
          == preflight.FAIL)


def test_preflight_shots():
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    w, r, b = time(13, 0), time(13, 40), time(20, 30)
    lines = preflight.check_shots(store, w, r, b, now=EVENING)
    check("an empty store fails every shot",
          {name for level, name, _ in lines if level == preflight.FAIL}
          == {"pills (done)", "bedtime", "refusal"})
    check("the fix for the pills names a plant command with a window around the shots",
          '"after": "12:45"' in lines[0][2] and '"until": "14:10"' in lines[0][2])

    plant(store, PILLS, {"kind": "window", "after": "12:45", "until": "14:30", "minutes": 0},
          kind="done")
    slugs = plant_round(store)["slugs"]
    for slug in slugs:
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)
        (store.root / slug / "looks.txt").write_text("What decides it: x.\n")
    (store.root / slugs[0] / "prompts.txt").write_text("stove\nknob\n")
    lines = preflight.check_shots(store, w, r, b, now=EVENING)
    check("everything planted passes every shot",
          all(level == preflight.PASS for level, _, _ in lines))

    lines = preflight.check_shots(store, w, time(15, 0), b, now=EVENING)
    check("a reach after the window closes is a FAIL",
          any(level == preflight.FAIL and name == "pills (done)" for level, name, _ in lines))
    lines = preflight.check_shots(store, w, r, b, refusal_at=time(15, 0), now=EVENING)
    check("an afternoon refusal with a nightly stove is a FAIL, and says when it is due",
          any(level == preflight.FAIL and name == "refusal" and "8:00 PM" in text
              for level, name, text in lines))
    (store.root / slugs[0] / "looks.txt").unlink()
    lines = preflight.check_shots(store, w, r, b, now=EVENING)
    check("a stove with no looks.txt is a FAIL that says how to compile it",
          any(level == preflight.FAIL and "plant_compile.py" in text for level, _, text in lines))


def test_rehearse_refusal():
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    photo = tmp / "doorway.jpg"
    photo.write_bytes(JPEG)
    asked = {}

    def fake(path, question, looks=None, safety=False):
        asked.update(question=question, safety=safety)
        return verdict_of("cannot_tell", "Only the stovetop is in view.", KNOBS)

    lines = preflight.rehearse_refusal(store, photo, check=fake)
    check("the rehearsal passes on a cannot-tell naming the knobs, and prints the line",
          lines[0][0] == preflight.PASS and "I can't see the four knobs" in lines[0][2])
    check("the rehearsal asks the stove question with safety on",
          asked == {"question": STOVE, "safety": True})
    lines = preflight.rehearse_refusal(
        store, photo, check=lambda *a, **k: verdict_of("yes", "No flame is visible."))
    check("a yes from the refusal spot is a FAIL", lines[0][0] == preflight.FAIL)
    lines = preflight.rehearse_refusal(
        store, photo, check=lambda *a, **k: verdict_of("cannot_tell", "x", "", failure="timeout"))
    check("no check made is a FAIL", lines[0][0] == preflight.FAIL)


# -- the night the clocks go back ---------------------------------------------------


def pacific():
    """Run the rest in US Pacific time, the way the laptop does."""
    os.environ["TZ"] = "America/Los_Angeles"
    time_module.tzset()


def local(utc_text):
    """A UTC moment as the naive local time the laptop's clock reads,
    fold and all: 08:30Z and 09:30Z on Nov 1 both read 1:30 AM."""
    return datetime.fromtimestamp(datetime.fromisoformat(utc_text).timestamp())


def test_gaps_across_the_repeated_hour():
    pacific()
    first_155 = local("2026-11-01T08:55:00+00:00")   # 1:55 AM PDT
    second_105 = local("2026-11-01T09:05:00+00:00")  # 1:05 AM PST, ten minutes later
    second_115 = local("2026-11-01T09:15:00+00:00")  # 1:15 AM PST, twenty minutes later
    check("the second 1:05 is the later one", second_105.fold == 1 and second_105 < first_155)

    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    every15 = plant(store, "Is the porch light on?", {"kind": "interval", "minutes": 15})
    nightly = plant(store, "Is the back door locked?", {"kind": "nightly", "after": "00:00", "minutes": 15})
    window = plant(store, "Is the heater off?", {"kind": "window", "after": "00:30", "until": "03:00", "minutes": 15})
    for slug in (every15, nightly, window):
        store.mark_checked(slug, when=first_155)
    due = {e["slug"] for e in store.due(now=second_105)}
    check("ten real minutes later, nothing is due yet", due == set())
    due = {e["slug"] for e in store.due(now=second_115)}
    check("twenty real minutes later, all three are due, not an hour later",
          due == {every15, nightly, window})

    # A check made in the repeated hour is stored without its fold and
    # reads as the first pass: the gap can only look longer, never
    # shorter, so a check is early rather than late.
    for slug in (every15, nightly, window):
        store.mark_checked(slug, when=second_105)
    due = {e["slug"] for e in store.due(now=second_115)}
    check("a stamp from the repeated hour never delays the next check", due == {every15, nightly, window})

    after = local("2026-11-01T10:30:00+00:00")  # 2:30 AM PST
    store.mark_checked(every15, when=local("2026-11-01T10:20:00+00:00"))
    check("after the change, ordinary gaps are back",
          every15 not in {e["slug"] for e in store.due(now=after)})


def test_nightly_windows_across_the_change():
    pacific()
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    pills = plant(store, PILLS, {"kind": "window", "after": "21:00", "until": "22:00", "minutes": 0},
                  kind="done")
    stove = plant(store, STOVE, {"kind": "nightly", "after": "20:00", "minutes": 1}, safety=True)
    sat_night = datetime(2026, 10, 31, 21, 30)
    sun_night = datetime(2026, 11, 1, 21, 30)
    check("Saturday 9:30 PM (PDT): both are due",
          {e["slug"] for e in store.due(now=sat_night)} == {pills, stove})
    store.mark_answered_with_witness(pills, "x.jpg", when=sat_night)
    store.mark_answered(stove, when=sat_night)
    for moment in (local("2026-11-01T08:30:00+00:00"), local("2026-11-01T09:30:00+00:00"),
                   datetime(2026, 11, 1, 19, 59)):
        check(f"neither is due at {moment.strftime('%H:%M')} (fold {moment.fold}) on Sunday",
              store.due(now=moment) == [])
    check("Sunday 9:30 PM (PST): both are due again, once",
          {e["slug"] for e in store.due(now=sun_night)} == {pills, stove})
    check("Saturday's witness is not Sunday's", store.witness_today(pills, now=sun_night) is None)

    # Coverage on Sunday's window counts real minutes in PST.
    checks = [datetime(2026, 11, 1, 21, m) for m in range(0, 60, 5)]
    cov = coverage(checks, datetime(2026, 11, 1, 21, 0), datetime(2026, 11, 1, 22, 0))
    check("Sunday's window, checked every five minutes, is complete", cov["kind"] == "complete")
    log = CheckLog(tmp / "log")
    log.record(pills, when=local("2026-11-01T08:30:00+00:00"))
    log.record(pills, when=local("2026-11-01T09:30:00+00:00"))
    written = (tmp / "log" / "2026-11-01" / f"checks-{pills}.txt").read_text().split()
    check("the two 1:30 AMs are written with their own offsets",
          written == ["2026-11-01T01:30:00-07:00", "2026-11-01T01:30:00-08:00"])


if __name__ == "__main__":
    test_done_act()
    test_redo_lines()
    test_firing_lines_name_the_time()
    test_refusal_detail()
    test_refusal_in_the_loop()
    test_no_refusal_when_the_stove_is_not_there()
    test_refusal_needs_every_condition()
    test_bedtime_round_marks_safety()
    test_safety_instruction()
    test_redo_log_shows_the_witness_photo()
    test_status_endpoint()
    test_preflight_server_rules()
    test_preflight_llama()
    test_preflight_shots()
    test_rehearse_refusal()
    # Last: these switch the process to Pacific time.
    test_gaps_across_the_repeated_hour()
    test_nightly_windows_across_the_change()
    print("ALL PASS")
