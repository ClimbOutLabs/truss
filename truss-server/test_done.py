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
import time
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


def test_concurrent_watch_writes():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create(PILLS, WINDOW, kind="done")
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)
        when = datetime(2026, 10, 27, 21, 5)
        errors = []
        barrier = threading.Barrier(2)

        def run_checked():
            try:
                barrier.wait()
                for _ in range(300):
                    store.mark_checked(slug, when=when)
            except Exception as exc:
                errors.append(exc)

        def run_witness():
            try:
                barrier.wait()
                for _ in range(300):
                    store.mark_answered_with_witness(
                        slug, "evidence.jpg", when=when
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run_checked),
            threading.Thread(target=run_witness),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        check("concurrent mark_checked and witness writes raise nothing",
              errors == [])
        raw = (Path(tmp) / slug / "watch.json").read_text()
        try:
            parses = isinstance(json.loads(raw), dict)
        except Exception:
            parses = False
        check("concurrent writes leave watch.json as one JSON record",
              parses)
        entry = store.get(slug)
        check("concurrent writes keep the answered mark and the witness",
              entry is not None
              and entry["answered_at"] is not None
              and entry["witness"] is not None
              and entry["witness"]["frame"] == "evidence.jpg")


def test_concurrent_watch_writes_many_threads():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create(PILLS, WINDOW, kind="done")
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)
        when = datetime(2026, 10, 27, 21, 5)
        errors = []
        barrier = threading.Barrier(8)

        def run(index):
            try:
                barrier.wait()
                for n in range(100):
                    if (index + n) % 2 == 0:
                        store.mark_checked(slug, when=when)
                    else:
                        store.mark_answered_with_witness(
                            slug, "evidence.jpg", when=when
                        )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        check("eight threads writing one watch record raise nothing",
              errors == [])
        raw = (Path(tmp) / slug / "watch.json").read_text()
        try:
            parses = isinstance(json.loads(raw), dict)
        except Exception:
            parses = False
        check("eight threads leave watch.json as one JSON record",
              parses)
        entry = store.get(slug)
        check("eight threads keep the answered mark and the witness",
              entry is not None
              and entry["answered_at"] is not None
              and entry["witness"] is not None
              and entry["witness"]["frame"] == "evidence.jpg")


def test_crash_residue_without_watch_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        broken = Path(tmp) / "half-planted"
        broken.mkdir()
        (broken / "question.txt").write_text("Is the stove off?\n")
        (broken / "watch.json.tmp").write_text('{"kind": "done", "pat')
        check("a folder with only a partial side file is not listed",
              store.list() == [])
        check("getting a folder with no readable watch returns nothing",
              store.get("half-planted") is None)
        slug = store.create("Is the front door locked?")
        check("planting a new question still works beside crash residue",
              slug == "is-the-front-door-locked")
        check("the new question is listed and the broken one is skipped",
              [e["slug"] for e in store.list()] == [slug])


def test_torn_watch_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        good = store.create("Is the stove off?")
        torn = Path(tmp) / "torn-question"
        torn.mkdir()
        (torn / "question.txt").write_text("Is the tap running?\n")
        whole = {
            "kind": "state",
            "pattern": {"kind": "interval", "minutes": 15},
            "state": "standing",
            "seq": 99,
            "planted_at": "2026-10-27T08:00:00",
            "last_checked_at": None,
            "answered_at": None,
            "witness": None,
            "redo_spoken": False,
        }
        (torn / "watch.json").write_text(json.dumps(whole) + json.dumps(whole))
        check("a torn watch record is skipped in the listing",
              [e["slug"] for e in store.list()] == [good])
        check("getting a torn watch record returns nothing",
              store.get("torn-question") is None)
        check("a good question still reads beside a torn one",
              store.get(good)["question"] == "Is the stove off?")


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
    clock.now = datetime(2026, 10, 27, 21, 35)
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


def test_done_firing_write_failure_records_nothing():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty.")]
    )
    slug = plant(store, PILLS, WINDOW, kind="done")
    edit_watch(store.root, slug, planted_at="2026-10-27T08:00:00")
    original_write = store._write_watch

    def failing_write(target_slug, watch):
        if watch.get("witness") is not None:
            raise OSError("disk gone")
        return original_write(target_slug, watch)

    store._write_watch = failing_write
    result = loop.process_frame(frame(captures, "1.jpg"))
    check("a done firing whose witness write fails does not fire",
          result["fired"] == [])
    check("a failed done firing speaks nothing", spoken == [])
    entry = store.get(slug)
    check("a failed done firing leaves no answered mark and no witness",
          entry["answered_at"] is None and entry["witness"] is None
          and entry["state"] == "standing")
    answers = Path(captures).parent / "log" / "2026-10-27" / "answers.txt"
    check("a failed done firing writes no answer log line",
          not answers.exists())
    alerts = AlertStore(Path(captures).parent / "alerts")
    created = sweep_missed(store, alerts, now=datetime(2026, 10, 27, 22, 30))
    check("the miss alert after a failed firing is true: nothing was recorded",
          len(created) == 1 and created[0]["slug"] == slug)


def test_done_firing_retry_after_write_failure():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty."),
                verdict("yes", "Tonight's compartment is empty.")]
    )
    slug = plant(store, PILLS, WINDOW, kind="done")
    edit_watch(store.root, slug, planted_at="2026-10-27T08:00:00")
    original_write = store._write_watch

    def failing_write(target_slug, watch):
        if watch.get("witness") is not None:
            raise OSError("disk gone")
        return original_write(target_slug, watch)

    store._write_watch = failing_write
    loop.process_frame(frame(captures, "1.jpg"))
    store._write_watch = original_write
    clock.now = datetime(2026, 10, 27, 21, 20)
    result = loop.process_frame(frame(captures, "2.jpg"))
    check("a later frame witnesses the act after a failed firing",
          result["fired"] == [slug]
          and store.witness_today(slug, now=clock.now) is not None)
    check("the retried firing is spoken once", len(spoken) == 1)
    alerts = AlertStore(Path(captures).parent / "alerts")
    check("no miss is raised after the retried witness",
          sweep_missed(store, alerts,
                       now=datetime(2026, 10, 27, 22, 30)) == [])


def test_redo_checks_obey_the_check_gap():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty.")],
        redo=[verdict("cannot_tell", "The organizer, but no hand near it."),
              verdict("yes", "She is at the organizer again.")],
        start=datetime(2026, 10, 27, 20, 50),
    )
    plant(store, PILLS, WINDOW, kind="done")
    loop.process_frame(frame(captures, "fire.jpg"))
    clock.now = datetime(2026, 10, 27, 21, 5)
    for n in range(10):
        loop.process_frame(frame(captures, f"watch-{n}.jpg"))
    check("ten quick frames cause at most one return check",
          len(loop.check_fn.redo_calls) == 1)
    clock.now = datetime(2026, 10, 27, 21, 20)
    loop.process_frame(frame(captures, "watch-after-gap.jpg"))
    check("a return check is allowed again after the gap",
          len(loop.check_fn.redo_calls) == 2)
    check("the allowed return check can speak the reminder",
          spoken[-1] == "That is already done. You did it at 8:50 PM.")


def test_retired_witnessed_question_never_reminds():
    store, loop, spoken, captures, clock = rig(
        normal=[verdict("yes", "Tonight's compartment is empty.")],
        redo=[verdict("yes", "She is at the organizer again.")],
    )
    slug = plant(store, PILLS, WINDOW, kind="done")
    loop.process_frame(frame(captures, "1.jpg"))
    store.retire(slug)
    clock.now = datetime(2026, 10, 27, 21, 20)
    loop.process_frame(frame(captures, "2.jpg"))
    check("a retired witnessed question is never return-checked",
          loop.check_fn.redo_calls == [])
    check("a retired witnessed question speaks no reminder",
          len(spoken) == 1)


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


def test_no_miss_while_a_check_is_in_flight():
    """A frame just inside the window whose model call returns after
    the window closes: the sweep that runs during the call must not
    call it missed, and once the witness lands nothing is raised."""
    tmp = tempfile.mkdtemp()
    alerts = AlertStore(Path(tmp) / "alerts")
    for answer, expect_alert in (("yes", False), ("no", True)):
        store, loop, spoken, captures, clock = rig(
            start=datetime(2026, 10, 27, 21, 59, 50)
        )
        alerts = AlertStore(Path(store.root).parent / "alerts")
        slug = plant(store, PILLS, WINDOW, kind="done")
        edit_watch(store.root, slug, planted_at="2026-10-27T08:00:00")
        during = []

        def slow_check(frame_path, entry, answer=answer):
            # The window closes while the model is still answering.
            during.append(sweep_missed(
                store, alerts, now=datetime(2026, 10, 27, 22, 5)))
            return verdict(answer, "Tonight's compartment is empty.")

        loop.check_fn = slow_check
        loop.process_frame(frame(captures, "late.jpg"))
        check(f"{answer}: the sweep during the check raises nothing",
              during == [[]] and alerts.pending() == [])
        after = sweep_missed(store, alerts, now=datetime(2026, 10, 27, 22, 6))
        if expect_alert:
            check("no: once the check is over, the true miss is raised",
                  len(after) == 1 and len(alerts.pending()) == 1)
        else:
            check("yes: the witnessed act is never called missed",
                  after == [] and alerts.pending() == [])
            check("yes: the answer was spoken",
                  spoken == ["Did I take my night pills? Yes. "
                             "Tonight's compartment is empty."])
        check(f"{answer}: the claim is released after the check",
              not store.check_in_flight(slug))


def test_miss_for_a_window_ending_at_midnight():
    late = {"kind": "window", "after": "21:00", "until": "23:59"}
    night = datetime(2026, 10, 27, 23, 59, 10)
    after_midnight = datetime(2026, 10, 28, 0, 0, 10)

    tmp, store, alerts = sweep_rig()
    planted_done(store, pattern=late)
    check("no miss while a 23:59 window is still open",
          sweep_missed(store, alerts, now=datetime(2026, 10, 27, 23, 58, 50)) == [])
    created = sweep_missed(store, alerts, now=night)
    check("a 23:59 window raises its miss at 23:59:10",
          len(created) == 1 and created[0]["date"] == "2026-10-27")
    check("the miss is not raised again after midnight",
          sweep_missed(store, alerts, now=after_midnight) == []
          and len(alerts.pending()) == 1)

    tmp, store, alerts = sweep_rig()
    planted_done(store, pattern=late)
    created = sweep_missed(store, alerts, now=after_midnight)
    check("with no sweep before midnight, the miss is raised just after",
          len(created) == 1 and created[0]["date"] == "2026-10-27"
          and created[0]["id"] == "2026-10-27-did-i-take-my-night-pills")
    check("the line after midnight speaks of yesterday",
          created[0]["line"]
          == "Yesterday's window closed at 11:59 PM. This was not done "
             "yesterday: Did I take my night pills?")

    tmp, store, alerts = sweep_rig()
    slug = planted_done(store, pattern=late)
    store.mark_answered_with_witness(slug, "pills.jpg",
                                     when=datetime(2026, 10, 27, 23, 30))
    # A frame just after midnight clears yesterday's witness lazily.
    store.witness_today(slug, now=datetime(2026, 10, 28, 0, 0, 5))
    check("yesterday's witness was cleared",
          store.get(slug)["witness"] is None)
    check("an act seen yesterday is never called missed after midnight",
          sweep_missed(store, alerts, now=after_midnight) == [])

    early = {"kind": "window", "after": "00:30", "until": "02:00"}
    tmp, store, alerts = sweep_rig()
    slug = planted_done(store, pattern=early, planted_at="2026-10-26T08:00:00")
    store.mark_answered_with_witness(slug, "pills.jpg",
                                     when=datetime(2026, 10, 27, 0, 40))
    check("before an early window opens, yesterday's seen act stays seen",
          sweep_missed(store, alerts, now=datetime(2026, 10, 28, 0, 20)) == [])
    # Taken again tonight: today's firing replaces yesterday's record.
    store.mark_answered_with_witness(slug, "pills2.jpg",
                                     when=datetime(2026, 10, 28, 0, 45))
    check("once today's early window opens, yesterday is never called missed",
          sweep_missed(store, alerts, now=datetime(2026, 10, 28, 0, 50)) == []
          and alerts.pending() == [])

    tmp, store, alerts = sweep_rig()
    planted_done(store, pattern=late)
    check("yesterday is left alone after 1 AM",
          sweep_missed(store, alerts, now=datetime(2026, 10, 28, 1, 0)) == [])


def test_no_miss_while_a_check_runs_past_midnight():
    for answer, expect_alert in (("yes", False), ("no", True)):
        store, loop, spoken, captures, clock = rig(
            start=datetime(2026, 10, 27, 23, 58, 50)
        )
        alerts = AlertStore(Path(store.root).parent / "alerts")
        slug = plant(store, PILLS, {"kind": "window", "after": "21:00",
                                    "until": "23:59"}, kind="done")
        edit_watch(store.root, slug, planted_at="2026-10-27T08:00:00")
        during = []

        def slow_check(frame_path, entry, answer=answer):
            for moment in (datetime(2026, 10, 27, 23, 59, 30),
                           datetime(2026, 10, 28, 0, 0, 30)):
                during.append(sweep_missed(store, alerts, now=moment))
            return verdict(answer, "Tonight's compartment is empty.")

        loop.check_fn = slow_check
        loop.process_frame(frame(captures, "late.jpg"))
        check(f"{answer}: no sweep during a check across midnight raises",
              during == [[], []] and alerts.pending() == [])
        after = sweep_missed(store, alerts, now=datetime(2026, 10, 28, 0, 1))
        if expect_alert:
            check("no: the true miss is raised for yesterday once it is over",
                  len(after) == 1 and after[0]["date"] == "2026-10-27")
        else:
            check("yes: the act seen at 11:58 PM is never called missed",
                  after == [] and alerts.pending() == [])

def test_sweep_never_alerts_for_an_invalid_window():
    tmp, store, alerts = sweep_rig()
    slug = planted_done(store)
    edit_watch(store.root, slug,
               pattern={"kind": "window", "after": "22:00", "until": "21:00"})
    check("an invalid window raises no miss on the first day",
          sweep_missed(store, alerts,
                       now=datetime(2026, 10, 27, 23, 0)) == [])
    check("an invalid window raises no miss on a later day either",
          sweep_missed(store, alerts,
                       now=datetime(2026, 10, 28, 23, 0)) == []
          and alerts.pending() == [])


def test_ack_is_confined_to_real_alert_files():
    tmp, store, alerts = sweep_rig()
    root = Path(tmp)
    alerts_root = Path(alerts.root)
    today = "2026-10-27"

    def alert_file(path, alert):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(alert, indent=2) + "\n")

    outside = root / "elsewhere" / "thing.json"
    alert_file(outside, {
        "id": "../elsewhere/thing", "slug": "a-question", "date": today,
        "line": "A line that must never be cleared from outside.",
        "spoken": False,
    })
    nested = alerts_root / "a" / "b.json"
    alert_file(nested, {
        "id": "a/b", "slug": "a-question", "date": today,
        "line": "A line in a subfolder, not an alert file.",
        "spoken": False,
    })
    ghost = alerts_root / "ghost.json"
    ghost.write_text(json.dumps(
        {"id": "ghost", "spoken": False, "date": today}
    ) + "\n")

    def files_now():
        return {
            str(p.relative_to(root))
            for p in root.rglob("*") if p.is_file()
        }

    before = files_now()
    check("an ack with path separators is refused",
          alerts.ack("../elsewhere/thing") is False)
    check("an ack naming a subfolder path is refused",
          alerts.ack("a/b") is False)
    check("an ack of a shapeless file's id is refused",
          alerts.ack("ghost") is False)
    check("the outside file was not marked spoken",
          json.loads(outside.read_text())["spoken"] is False)
    check("the subfolder file was not marked spoken",
          json.loads(nested.read_text())["spoken"] is False)
    check("the refused acks created and removed no files",
          files_now() == before)


def test_alert_store_on_a_file_path_is_disabled_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "alerts-as-a-file"
        path.write_text("not a folder")
        alerts = AlertStore(path)
        check("an alerts path that is a file does not raise", True)
        check("a disabled alert store holds nothing pending",
              alerts.pending() == [])
        check("a disabled alert store refuses every ack",
              alerts.ack("anything") is False)
        store = QuestionStore(Path(tmp) / "questions")
        check("the sweep is a no-op with a disabled alert store",
              sweep_missed(store, alerts,
                           now=datetime(2026, 10, 27, 23, 0)) == [])


def test_shapeless_alert_files_are_skipped_everywhere():
    import server as server_module

    with tempfile.TemporaryDirectory() as tmp:
        server_module.STORE = QuestionStore(Path(tmp) / "questions")
        slug = server_module.STORE.create(PILLS, WINDOW, kind="done")
        server_module.ALERTS = AlertStore(Path(tmp) / "alerts")
        today = datetime.now().date().isoformat()
        alerts_root = Path(tmp) / "alerts"
        (alerts_root / "broken.json").write_text(json.dumps(
            {"spoken": False, "date": today}
        ) + "\n")
        good = {
            "id": f"{today}-good-alert",
            "slug": slug,
            "question": PILLS,
            "date": today,
            "until": "22:00",
            "line": "It is past 10:00 PM. This has not been done today: "
                    "Did I take my night pills?",
            "spoken": False,
            "created_at": f"{today}T22:30:00",
        }
        (alerts_root / "good.json").write_text(json.dumps(good, indent=2) + "\n")
        httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            got = get_json(base + "/alerts")
            check("GET /alerts returns the good alert beside a shapeless one",
                  len(got["alerts"]) == 1
                  and got["alerts"][0]["id"] == good["id"])
            listing = get_json(base + "/questions")
            check("GET /questions works beside a shapeless alert file",
                  len(listing["questions"]) == 1
                  and listing["questions"][0]["miss_pending"] is True)
            status, _ = post(
                base + "/alerts/ack",
                json.dumps({"id": good["id"]}).encode(),
                "application/json",
            )
            check("acking the good alert still works",
                  status == 200 and server_module.ALERTS.pending() == [])
        finally:
            httpd.shutdown()


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
            # The real clock drives this rig, so the window must have
            # closed already: run inside 00:00 to 00:01 and it waits.
            closed = datetime.now().replace(hour=0, minute=1, second=5, microsecond=0)
            if datetime.now() < closed:
                time.sleep((closed - datetime.now()).total_seconds())
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
    test_concurrent_watch_writes()
    test_concurrent_watch_writes_many_threads()
    test_crash_residue_without_watch_is_skipped()
    test_torn_watch_is_skipped()
    test_done_fire_records_witness()
    test_redo_reminder_speaks_once()
    test_redo_silence()
    test_no_redo_without_a_witness()
    test_yesterdays_witness_stays_silent()
    test_state_window_question_is_unchanged()
    test_done_firing_write_failure_records_nothing()
    test_done_firing_retry_after_write_failure()
    test_redo_checks_obey_the_check_gap()
    test_retired_witnessed_question_never_reminds()
    test_miss_alert_lifecycle()
    test_miss_alert_not_raised()
    test_miss_alert_survives_reload_and_broken_records()
    test_no_miss_while_a_check_is_in_flight()
    test_miss_for_a_window_ending_at_midnight()
    test_no_miss_while_a_check_runs_past_midnight()
    test_sweep_never_alerts_for_an_invalid_window()
    test_ack_is_confined_to_real_alert_files()
    test_alert_store_on_a_file_path_is_disabled_not_fatal()
    test_shapeless_alert_files_are_skipped_everywhere()
    test_http()
    print("ALL PASS")
