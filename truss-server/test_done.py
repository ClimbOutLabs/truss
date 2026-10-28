#!/usr/bin/env python3
"""
Tests for done questions: kinds, windows, the witness, the redo
reminder, and missed-window alerts that stay pending until spoken.

Run from the truss-server folder:  python3 test_done.py
Stdlib only. Exits non-zero on the first failure.
"""

import json
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alerts import AlertStore, sweep_missed  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from questions import QuestionStore  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"
WINDOW = {"kind": "window", "after": "20:00", "until": "22:00"}
PILLS = "Did I take my night pills?"


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def verdict(v, reason=""):
    return {"verdict": v, "reason": reason, "latency_ms": 1, "usage": {}}


def edit_watch(store_root, slug, **changes):
    path = Path(store_root) / slug / "watch.json"
    watch = json.loads(path.read_text())
    watch.update(changes)
    path.write_text(json.dumps(watch, indent=2) + "\n")


# -- the store: kinds and windows ------------------------------------------


def test_kinds():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        plain = store.create("Is the stove off?")
        check("a question with no kind is a state question",
              store.get(plain)["kind"] == "state")
        done = store.create(PILLS, WINDOW, kind="done")
        check("a done question keeps its kind", store.get(done)["kind"] == "done")
        try:
            store.create("Is the tap running?", kind="weird")
            refused = False
        except ValueError:
            refused = True
        check("an unknown kind is refused at planting time", refused)
        check("the refused question was not planted",
              len(store.list()) == 2)
        edit_watch(tmp, plain, kind="weird")
        check("a stored kind that is not understood reads as state",
              store.get(plain)["kind"] == "state")


def test_window_validation():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        for name, pattern in [
            ("a window ending before it starts",
             {"kind": "window", "after": "22:00", "until": "21:00"}),
            ("a window with an unreadable start",
             {"kind": "window", "after": "banana", "until": "22:00"}),
            ("a window with no end",
             {"kind": "window", "after": "21:00"}),
        ]:
            try:
                store.create(PILLS, pattern, kind="done")
                refused = False
            except ValueError:
                refused = True
            check(f"{name} is refused at planting time", refused)
        check("no refused window was planted", store.list() == [])


def test_window_due():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create(PILLS, WINDOW, kind="done")
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)
        day = datetime(2026, 10, 27)

        def at(hour, minute=0, days=0):
            return day.replace(hour=hour, minute=minute) + timedelta(days=days)

        check("window question not due before the window",
              not any(e["slug"] == slug for e in store.due(now=at(19, 0))))
        check("window question due inside the window",
              any(e["slug"] == slug for e in store.due(now=at(21, 0))))
        check("window question not due after the window",
              not any(e["slug"] == slug for e in store.due(now=at(22, 30))))
        store.mark_checked(slug, when=at(21, 0))
        check("window question waits out its gap after a check",
              not any(e["slug"] == slug for e in store.due(now=at(21, 5))))
        check("window question due again after the gap",
              any(e["slug"] == slug for e in store.due(now=at(21, 20))))
        store.mark_answered(slug, when=at(21, 20))
        check("answered window question stays standing for tomorrow",
              store.get(slug)["state"] == "standing")
        check("answered window question is not due again that day",
              not any(e["slug"] == slug for e in store.due(now=at(21, 45))))
        check("answered window question is due the next day",
              any(e["slug"] == slug for e in store.due(now=at(21, 0, days=1))))

        edit_watch(tmp, slug, pattern={"kind": "window",
                                       "after": "banana", "until": "22:00"})
        due = store.due(now=at(21, 0, days=2))
        check("a stored window that cannot be read is never due, never an error",
              not any(e["slug"] == slug for e in due))


def test_witness_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create(PILLS, WINDOW, kind="done")
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)
        evening = datetime(2026, 10, 27, 21, 5)
        store.record_witness(slug, "210500_did-i-take-my-night-pills.jpg",
                             when=evening)
        witness = store.witness_today(slug, now=evening)
        check("the witness is on the record the same day",
              witness is not None
              and witness["frame"] == "210500_did-i-take-my-night-pills.jpg")
        status = store.done_status(slug, now=evening)
        check("the witness time reads as a spoken time",
              status["witness_time"] == "9:05 PM")
        check("the redo reminder starts unspoken",
              status["redo_spoken"] is False)
        tomorrow = evening + timedelta(days=1)
        check("yesterday's witness is never returned",
              store.witness_today(slug, now=tomorrow) is None)
        check("yesterday's witness is cleared from the record",
              store.get(slug)["witness"] is None)


# -- the loop: witness, redo reminder, silence ------------------------------


class DoneChecker:
    """Scripts normal checks and return-prompt checks separately."""

    def __init__(self, normal=None, redo=None):
        self.normal = list(normal or [])
        self.redo_script = list(redo or [])
        self.normal_calls = []
        self.redo_calls = []

    def __call__(self, frame_path, entry):
        if entry["question"].startswith("Is the wearer at the same place"):
            self.redo_calls.append(entry)
            script = self.redo_script
        else:
            self.normal_calls.append(entry)
            script = self.normal
        if script:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return verdict("cannot_tell", "Nothing decisive is visible.")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def rig(normal=None, redo=None, start=None):
    tmp = tempfile.mkdtemp()
    store = QuestionStore(Path(tmp) / "questions")
    spoken = []
    clock = Clock(start or datetime(2026, 10, 27, 21, 5))
    loop = CheckLoop(
        store,
        DoneChecker(normal, redo),
        Path(tmp) / "log",
        speak_fn=lambda text, entry, evidence: spoken.append(text),
        now_fn=clock,
    )
    captures = Path(tmp) / "captures"
    captures.mkdir()
    return store, loop, spoken, captures, clock


def plant(store, text, pattern, kind="state"):
    slug = store.create(text, pattern, kind=kind)
    store.save_ref(slug, "yes", JPEG)
    store.save_ref(slug, "no", JPEG)
    return slug


def frame(captures, name):
    path = captures / name
    path.write_bytes(JPEG)
    return path


def test_done_fire_records_witness():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty.")]
    )
    slug = plant(store, PILLS, WINDOW, kind="done")
    result = loop.process_frame(frame(captures, "1.jpg"))
    check("done question fires like any question", result["fired"] == [slug])
    check("the firing is spoken as before",
          spoken == ["Did I take my night pills? Yes. "
                     "Tonight's compartment is empty."])
    witness = store.witness_today(slug, now=clock.now)
    check("the firing leaves a witness with the evidence photo's name",
          witness is not None
          and witness["frame"] == Path(result["evidence"]).name)


def test_redo_reminder_speaks_once():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty.")],
        redo=[verdict("yes", "She is at the organizer again."),
              verdict("yes", "She is at the organizer again.")],
    )
    slug = plant(store, PILLS, WINDOW, kind="done")
    loop.process_frame(frame(captures, "1.jpg"))
    clock.now = datetime(2026, 10, 27, 21, 20)
    result = loop.process_frame(frame(captures, "2.jpg"))
    check("the return prompt is built from the question's own words",
          loop.check_fn.redo_calls[0]["question"]
          == "Is the wearer at the same place as this question again, "
             "about to do it again: Did I take my night pills?")
    check("the return check carries the question's reference looks",
          loop.check_fn.redo_calls[0]["ref_yes_path"]
          == store.get(slug)["ref_yes_path"])
    check("the reminder names the witnessed time",
          spoken[-1] == "That is already done. You did it at 9:05 PM.")
    check("the return frame is kept as evidence",
          result["evidence"] is not None and Path(result["evidence"]).exists())
    log_text = Path(result["evidence"]).parent.joinpath("answers.txt").read_text()
    check("the answer log marks the redo reminder",
          "redo reminder" in log_text)
    clock.now = datetime(2026, 10, 27, 21, 25)
    loop.process_frame(frame(captures, "3.jpg"))
    check("the reminder is spoken exactly once",
          spoken.count("That is already done. You did it at 9:05 PM.") == 1)
    check("after the reminder the question is not return-checked again",
          len(loop.check_fn.redo_calls) == 1)


def test_redo_silence():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty.")],
        redo=[verdict("cannot_tell", "Too dark to tell."),
              RuntimeError("model call failed")],
    )
    plant(store, PILLS, WINDOW, kind="done")
    loop.process_frame(frame(captures, "1.jpg"))
    clock.now = datetime(2026, 10, 27, 21, 20)
    f = frame(captures, "2.jpg")
    loop.process_frame(f)
    check("a cannot-tell return check speaks no reminder", len(spoken) == 1)
    check("a cannot-tell return frame is deleted", not f.exists())
    clock.now = datetime(2026, 10, 27, 21, 25)
    f = frame(captures, "3.jpg")
    result = loop.process_frame(f)
    check("a failed return check stays silent", len(spoken) == 1)
    check("a failed return check is counted", result["errors"] == 1)
    check("a failed return frame is deleted", not f.exists())


def test_no_redo_without_a_witness():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("cannot_tell", "Nothing decisive is visible.")]
    )
    plant(store, PILLS, WINDOW, kind="done")
    loop.process_frame(frame(captures, "1.jpg"))
    check("no witness means no return check is ever made",
          loop.check_fn.redo_calls == [])


def test_yesterdays_witness_stays_silent():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty."),
                verdict("cannot_tell", "Nothing decisive is visible.")],
        redo=[verdict("yes", "She is at the organizer again.")],
        start=datetime(2026, 10, 26, 21, 5),
    )
    slug = plant(store, PILLS, WINDOW, kind="done")
    loop.process_frame(frame(captures, "1.jpg"))
    clock.now = datetime(2026, 10, 27, 21, 10)
    loop.process_frame(frame(captures, "2.jpg"))
    check("the next day asks the ordinary question again",
          len(loop.check_fn.normal_calls) == 2)
    check("yesterday's witness triggers no return check",
          loop.check_fn.redo_calls == [])
    check("yesterday's witness is cleared by the next day",
          store.witness_today(slug, now=clock.now) is None)


def test_state_window_question_is_unchanged():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "All four burners are dark.")]
    )
    slug = plant(store, "Is the stove off?", WINDOW, kind="state")
    result = loop.process_frame(frame(captures, "1.jpg"))
    check("a state question in a window fires as before",
          result["fired"] == [slug]
          and spoken == ["Is the stove off? Yes. All four burners are dark."])
    check("a state firing leaves no witness",
          store.get(slug)["witness"] is None)
    loop.process_frame(frame(captures, "2.jpg"))
    check("a state question is never return-checked",
          loop.check_fn.redo_calls == [])


# -- the sweep: missed windows, delivery until spoken ------------------------


def sweep_rig():
    tmp = tempfile.mkdtemp()
    store = QuestionStore(Path(tmp) / "questions")
    alerts = AlertStore(Path(tmp) / "alerts")
    return tmp, store, alerts


def planted_done(store, text=PILLS, pattern=WINDOW, kind="done",
                 planted_at="2026-10-27T08:00:00"):
    slug = store.create(text, pattern, kind=kind)
    store.save_ref(slug, "yes", JPEG)
    store.save_ref(slug, "no", JPEG)
    edit_watch(store.root, slug, planted_at=planted_at)
    return slug


def test_miss_alert_lifecycle():
    tmp, store, alerts = sweep_rig()
    slug = planted_done(store)
    after_close = datetime(2026, 10, 27, 22, 30)
    created = sweep_missed(store, alerts, now=after_close)
    check("a closed window with no witness raises one alert",
          len(created) == 1 and created[0]["slug"] == slug)
    check("the alert line is plain and factual",
          created[0]["line"]
          == "It is past 10:00 PM. This has not been done today: "
             "Did I take my night pills?")
    check("the sweep is idempotent",
          sweep_missed(store, alerts, now=after_close) == []
          and len(alerts.pending()) == 1)
    alert_id = created[0]["id"]
    check("an unknown ack is refused", alerts.ack("no-such-alert") is False)
    check("the ack clears the alert", alerts.ack(alert_id) is True)
    check("nothing is pending after the ack", alerts.pending() == [])
    check("a second ack of the same alert is refused",
          alerts.ack(alert_id) is False)
    check("an acked miss is never raised again that day",
          sweep_missed(store, alerts, now=after_close) == [])


def test_miss_alert_not_raised():
    tmp, store, alerts = sweep_rig()
    witnessed = planted_done(store, "Did I feed the dog?")
    store.record_witness(witnessed, "dog.jpg",
                         when=datetime(2026, 10, 27, 21, 5))
    planted_done(store, "Is the stove off?", kind="state")
    after_close = datetime(2026, 10, 27, 22, 30)
    check("no alert when a witness exists, and none for a state question",
          sweep_missed(store, alerts, now=after_close) == [])

    tmp, store, alerts = sweep_rig()
    planted_done(store)
    check("no alert before the window closes",
          sweep_missed(store, alerts,
                       now=datetime(2026, 10, 27, 21, 30)) == [])

    tmp, store, alerts = sweep_rig()
    planted_done(store, planted_at="2026-10-27T22:15:00")
    check("no alert for a window that closed before the question existed",
          sweep_missed(store, alerts, now=after_close) == [])


def test_miss_alert_survives_reload_and_broken_records():
    tmp, store, alerts = sweep_rig()
    planted_done(store)
    after_close = datetime(2026, 10, 27, 22, 30)
    sweep_missed(store, alerts, now=after_close)
    fresh_alerts = AlertStore(Path(tmp) / "alerts")
    check("a pending alert survives a reload: it lives in a file",
          len(fresh_alerts.pending()) == 1)
    fresh_store = QuestionStore(Path(tmp) / "questions")
    check("a fresh store and sweep raise no duplicate after reload",
          sweep_missed(fresh_store, fresh_alerts, now=after_close) == [])

    broken = Path(tmp) / "questions" / "broken-question"
    broken.mkdir()
    (broken / "watch.json").write_text("{not json")
    check("a broken record never makes the sweep raise",
          sweep_missed(store, alerts, now=after_close) == [])


# -- HTTP: planting kinds, the alerts endpoints ------------------------------


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


def get_json(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def test_http():
    import server as server_module

    with tempfile.TemporaryDirectory() as tmp:
        server_module.STORE = QuestionStore(Path(tmp) / "questions")
        server_module.ALERTS = AlertStore(Path(tmp) / "alerts")
        server_module.CAPTURES_DIR = Path(tmp) / "captures"
        server_module.CAPTURES_DIR.mkdir()
        httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            window = {"kind": "window", "after": "00:00", "until": "00:01"}
            status, body = post(
                base + "/plant/question",
                json.dumps({"text": PILLS, "kind": "done",
                            "pattern": window}).encode(),
                "application/json",
            )
            check("planting a done question returns 200", status == 200)
            check("the planting reply names the kind", body["kind"] == "done")
            slug = body["slug"]

            img, ctype = multipart(JPEG)
            post(f"{base}/plant/ref?slug={slug}&kind=yes", img, ctype)
            status, body = post(
                f"{base}/plant/ref?slug={slug}&kind=no", img, ctype
            )
            check("both reference looks make the done question stand",
                  status == 200 and body["state"] == "standing")

            req = urllib.request.Request(
                base + "/plant/question",
                data=json.dumps({"text": "Is the tap running?",
                                 "kind": "weird"}).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                urllib.request.urlopen(req)
                bad_status = 200
            except urllib.error.HTTPError as e:
                bad_status = e.code
            check("planting with an unknown kind is refused",
                  bad_status == 400)

            listing = get_json(base + "/questions")
            check("the refused kind planted nothing",
                  len(listing["questions"]) == 1)
            entry = listing["questions"][0]
            check("the listing carries the kind and the done fields",
                  entry["kind"] == "done"
                  and entry["witness_time"] is None
                  and entry["redo_spoken"] is False
                  and entry["miss_pending"] is False)

            # The window closed at one minute past midnight; the
            # question was planted after that, so move its planted
            # time back to midnight, as if it had stood all day.
            edit_watch(Path(tmp) / "questions", slug,
                       planted_at=datetime.now().strftime("%Y-%m-%dT00:00:00"))
            status, _ = post(base + "/capture", img, ctype)
            check("a capture still returns 200", status == 200)
            pending = server_module.ALERTS.pending()
            check("the capture's sweep raised the missed-window alert",
                  len(pending) == 1 and pending[0]["slug"] == slug)

            listing = get_json(base + "/questions")
            check("the listing now shows the miss as pending",
                  listing["questions"][0]["miss_pending"] is True)

            got = get_json(base + "/alerts")
            check("GET /alerts returns the pending alert",
                  len(got["alerts"]) == 1
                  and got["alerts"][0]["id"] == pending[0]["id"]
                  and "has not been done today" in got["alerts"][0]["line"])

            status, _ = post(
                base + "/alerts/ack",
                json.dumps({"id": "no-such-alert"}).encode(),
                "application/json",
            )
            check("acking an unknown alert is refused", status == 404)
            check("the refused ack changed nothing",
                  len(server_module.ALERTS.pending()) == 1)

            status, body = post(
                base + "/alerts/ack",
                json.dumps({"id": pending[0]["id"]}).encode(),
                "application/json",
            )
            check("acking the alert returns 200", status == 200)
            check("the acked alert is gone from GET /alerts",
                  get_json(base + "/alerts")["alerts"] == [])
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    test_kinds()
    test_window_validation()
    test_window_due()
    test_witness_store()
    test_done_fire_records_witness()
    test_redo_reminder_speaks_once()
    test_redo_silence()
    test_no_redo_without_a_witness()
    test_yesterdays_witness_stays_silent()
    test_state_window_question_is_unchanged()
    test_miss_alert_lifecycle()
    test_miss_alert_not_raised()
    test_miss_alert_survives_reload_and_broken_records()
    test_http()
    print("ALL PASS")
