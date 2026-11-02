#!/usr/bin/env python3
"""
Tests for guided tasks: a named job written as guide steps by the model
(task_steps.py), and the server's /guide/task with its lines queued for
the phone's alert poll.

Run from the truss-server folder:  python3 test_task_steps.py
Stdlib only, no network. Exits non-zero on the first failure.
"""

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import task_steps  # noqa: E402
from task_steps import TaskError, compose_steps, is_safety_step, left_out_line  # noqa: E402


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def text_part(text):
    return {"type": "text", "text": text}


def asker(answer, calls=None):
    def ask_json(system, content, schema, schema_name, max_tokens=None):
        if calls is not None:
            calls.append({"system": system, "content": content, "schema": schema})
        if isinstance(answer, Exception):
            raise answer
        return dict(answer, _model="muse-spark-test")
    return ask_json


LOCK_UP = {
    "title": "Lock up",
    "steps": [
        "Is the back door bolt across",
        "Are the living room windows shut?",
        "Is the stove off?",
        "Is the porch light on?",
    ],
    "left_out": [],
}


def test_steps_come_back_cleaned():
    calls = []
    plan = compose_steps("  lock up   for the night ", asker(LOCK_UP, calls), text_part)
    check("the job's words are sent, trimmed", calls[0]["content"] == [text_part("The job: lock up for the night")])
    check("no photo is sent", all(p["type"] == "text" for p in calls[0]["content"]))
    check("a step without a question mark gets one", plan["steps"][0] == "Is the back door bolt across?")
    check("the title is kept", plan["title"] == "Lock up")
    check("the answering model is named", plan["model"] == "muse-spark-test")


def test_a_safety_step_is_moved_out():
    plan = compose_steps("lock up", asker(LOCK_UP), text_part)
    check("the stove step is not a guide step", "Is the stove off?" not in plan["steps"])
    check("it is named as left out", plan["left_out"] == ["Is the stove off"])
    check("the others stand, in order", plan["steps"] == [
        "Is the back door bolt across?",
        "Are the living room windows shut?",
        "Is the porch light on?",
    ])


def test_a_thermometer_reading_stays():
    check("doneness by eye is a safety step", is_safety_step("Is the chicken cooked through?"))
    check("a thermometer reading is not", not is_safety_step("Does the thermometer read 74 or more?"))
    check("pills by eye are a safety step", is_safety_step("Did you take your pills?"))
    check("a plain step is not", not is_safety_step("Are the onions golden brown?"))
    check("a word inside another word does not count", not is_safety_step("Is the gasket seated?"))


def test_model_left_out_is_kept_and_spoken():
    answer = {"title": "Curry", "steps": ["Are the onions golden brown?"], "left_out": ["chicken cooked through"]}
    plan = compose_steps("chicken curry", asker(answer), text_part)
    check("the model's left out is kept", plan["left_out"] == ["chicken cooked through"])
    check("the spoken line names it", left_out_line(plan["left_out"]) == "Not checked in this guide: chicken cooked through.")
    check("nothing left out, nothing spoken", left_out_line([]) is None)
    many = left_out_line(["a", "b", "c", "d", "e"])
    check("a long list is cut to three and a count", many == "Not checked in this guide: a; b; c and 2 more.")


def test_failures_are_plain():
    for task in ("", "   ", None, 7, "x" * 201):
        try:
            compose_steps(task, asker(LOCK_UP), text_part)
            check(f"bad job {task!r} refused", False)
        except TaskError:
            check(f"bad job {str(task)[:12]!r} refused", True)
    try:
        compose_steps("lock up", asker(RuntimeError("Muse Spark did not answer")), text_part)
        check("a failed call raises", False)
    except TaskError as e:
        check("a failed call says so", str(e) == "Muse Spark did not answer, so no steps were written.")
    try:
        compose_steps("make dinner", asker({"title": "x", "steps": ["Is the stove off?"], "left_out": []}), text_part)
        check("only safety steps raises", False)
    except TaskError as e:
        check("only safety steps is refused plainly", "standing question" in str(e))


def test_steps_are_capped_and_deduplicated():
    answer = {"title": "Shelf", "steps": [f"Is part {i} on?" for i in range(12)] + ["Is part 0 on?"], "left_out": []}
    plan = compose_steps("build the shelf", asker(answer), text_part)
    check("at most eight steps", len(plan["steps"]) == task_steps.MAX_STEPS)
    check("no repeats", len(set(plan["steps"])) == len(plan["steps"]))


# -- the server ---------------------------------------------------------------

def serve():
    import server as server_module
    from alerts import AlertStore
    tmp = Path(tempfile.mkdtemp())
    server_module.ALERTS = AlertStore(tmp / "alerts")
    server_module.CAPTURES_DIR = tmp / "captures"
    server_module.CAPTURES_DIR.mkdir()
    server_module.GUIDE["session"] = None
    server_module.GUIDE_SAY.clear()
    httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return server_module, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def call(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="GET" if body is None else "POST")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def capture(base):
    boundary = "truss-test-boundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"f.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + b"\xff\xd8\xff\xe0frame\xff\xd9" + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base + "/capture", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def test_server_task_queues_lines_for_the_phone():
    server_module, httpd, base = serve()
    real_ask = server_module.endpoint.ask_json
    curry = {"title": "Curry", "steps": ["Are the onions golden brown?", "Is the chicken in the pan?",
                                         "Is the chicken cooked through?"], "left_out": []}
    server_module.endpoint.ask_json = asker(curry)
    try:
        status, reply = call(base, "/guide/task", {"task": "chicken curry", "phone": True})
        check("POST /guide/task starts a guide", status == 200 and reply["of"] == 2)
        check("the reply lists what was left out", reply["left_out"] == ["Is the chicken cooked through"])
        check("the reply speaks the left-out line first",
              reply["speak"][0] == "Not checked in this guide: Is the chicken cooked through.")
        status, again = call(base, "/guide/task", {"task": "chicken curry", "phone": True})
        check("a second guide is refused while one runs", status == 400)

        _, alerts = call(base, "/alerts")
        lines = [a for a in alerts["alerts"] if a["id"].startswith("guide-")]
        check("one guide line waits for the phone", len(lines) == 1)
        check("it carries the first step", "Step 1 of 2: Are the onions golden brown?" in lines[0]["line"])

        _, st = call(base, "/guide/status")
        check("status lists the steps", st["steps"] == ["Are the onions golden brown?", "Is the chicken in the pan?"])

        status, _ = call(base, "/alerts/ack", {"id": lines[0]["id"]})
        check("the phone's ack clears it", status == 200 and not [
            a for a in call(base, "/alerts")[1]["alerts"] if a["id"].startswith("guide-")])
        status, _ = call(base, "/alerts/ack", {"id": lines[0]["id"]})
        check("a second ack of the same line is refused", status == 404)

        status, reply = call(base, "/guide/skip", {"phone": True})
        check("skip moves on", status == 200 and reply["step"] == 2)
        queued = server_module.guide_waiting()
        check("the skip's line is queued", len(queued) == 1 and "Step 2 of 2" in queued[0]["line"])

        # A capture that confirms the step speaks in its own reply; the
        # line still queued for the phone is now stale and is dropped.
        real_check = server_module.guide_check
        server_module.guide_check = lambda frame, question: {"verdict": "no", "reason": ""}
        try:
            status, cap = capture(base)
            check("a no leaves the queued line", status == 200 and len(server_module.guide_waiting()) == 1)
            server_module.guide_check = lambda frame, question: {"verdict": "cannot_tell", "reason": ""}
            capture(base)
            status, cap = capture(base)
            check("a repeat after two cannot-tells is spoken in the reply",
                  cap["speak"] == ["Have another look. Is the chicken in the pan?"])
            check("and the stale queued line is dropped", server_module.guide_waiting() == [])
        finally:
            server_module.guide_check = real_check
        server_module.guide_say(["a line still waiting"])
        call(base, "/guide/stop", {"phone": False})
        check("a stop without phone drops every queued line", server_module.guide_waiting() == [])
        _, st = call(base, "/guide/status")
        check("the guide is stopped", st["state"] == "stopped")
    finally:
        server_module.endpoint.ask_json = real_ask
        httpd.shutdown()


def test_server_task_failure_starts_nothing():
    server_module, httpd, base = serve()
    real_ask = server_module.endpoint.ask_json
    server_module.endpoint.ask_json = asker(RuntimeError("Muse Spark did not answer"))
    try:
        status, _ = call(base, "/guide/task", {"task": "lock up", "phone": True})
        check("a failed compose is a 400", status == 400)
        check("no guide was started", server_module.GUIDE["session"] is None)
        check("nothing is queued", server_module.guide_waiting() == [])
    finally:
        server_module.endpoint.ask_json = real_ask
        httpd.shutdown()


def test_stop_with_phone_replaces_the_queue():
    server_module, httpd, base = serve()
    try:
        call(base, "/guide/start", {"title": "T", "steps": ["Is the lid on?", "Is the box shut?"], "phone": True})
        check("start with phone queues", len(server_module.guide_waiting()) == 1)
        call(base, "/guide/stop", {"phone": True})
        queued = server_module.guide_waiting()
        check("stop leaves only its own line", [q["line"] for q in queued] == ["Stopped."])
        server_module.GUIDE_SAY.clear()
        call(base, "/guide/start", {"title": "T", "steps": ["Is the lid on?"]})
        check("start without phone queues nothing", server_module.guide_waiting() == [])
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    test_steps_come_back_cleaned()
    test_a_safety_step_is_moved_out()
    test_a_thermometer_reading_stays()
    test_model_left_out_is_kept_and_spoken()
    test_failures_are_plain()
    test_steps_are_capped_and_deduplicated()
    test_server_task_queues_lines_for_the_phone()
    test_server_task_failure_starts_nothing()
    test_stop_with_phone_replaces_the_queue()
    print("ALL PASS")
