#!/usr/bin/env python3
"""
"Hold on, let me check" (holdon.py): with TRUSS_HOLD_ON=1 a quick gate
beside the full check answers the phone early, and the full answer
follows at GET /capture/result. Off, or on a gate no, nothing changes.

    python3 test_holdon.py
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL", "TRUSS_HOLD_ON"):
    os.environ.pop(_name, None)

import holdon  # noqa: E402
import server as S  # noqa: E402


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


class SlowLoop:
    def __init__(self, seconds):
        self.seconds = seconds
        self.calls = 0

    def process_frame(self, path):
        self.calls += 1
        time.sleep(self.seconds)
        return {"managed": True, "fired": ["pills"], "speak": ["You took your pills."], "errors": 0}


class DueStore:
    def due(self, now=None):
        return [{"question": "Did I take my pills?", "kind": "done"}]

    def redo_watch(self, now=None):
        return []


def multipart(data=b"\xff\xd8x"):
    b = "xTRUSSx"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"f.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def run(loop, gate_fn, hold_on="1"):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "captures").mkdir()
    saved = (S.CAPTURES_DIR, S.LOOP, S.STORE, holdon.gate, S.endpoint.meta_on, S.run_sweep, S.HOLD)
    S.CAPTURES_DIR, S.LOOP, S.STORE = tmp / "captures", loop, DueStore()
    holdon.gate = gate_fn
    S.endpoint.meta_on = lambda: True
    S.run_sweep = lambda: None
    S.HOLD = holdon.Pending()
    S.GUIDE["session"] = None
    os.environ["TRUSS_HOLD_ON"] = hold_on
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.TrussHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        body, ctype = multipart()
        started = time.monotonic()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/capture", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        first = json.loads(urllib.request.urlopen(req, timeout=10).read())
        first_s = time.monotonic() - started
        later = None
        if first.get("pending"):
            later = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/capture/result?id={first['pending']}", timeout=10).read())
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/capture/result?id=nope", timeout=5)
            unknown = 200
        except urllib.error.HTTPError as e:
            unknown = e.code
        return first, first_s, later, unknown
    finally:
        httpd.shutdown()
        os.environ.pop("TRUSS_HOLD_ON", None)
        (S.CAPTURES_DIR, S.LOOP, S.STORE, holdon.gate, S.endpoint.meta_on, S.run_sweep, S.HOLD) = saved


def test_gate_yes_answers_early():
    loop = SlowLoop(1.5)
    first, first_s, later, unknown = run(loop, lambda f, q: True)
    check("gate yes: the phone hears hold-on first", first["speak"] == ["Hold on, let me check something."])
    check("gate yes: before the full check is done", first_s < 1.2)
    check("gate yes: nothing fired in the early reply", first["fired"] == [])
    check("gate yes: the loop's own line follows when it cannot be reworded",
          later == {"speak": ["You took your pills."], "fired": ["pills"]})
    check("gate yes: the full check ran once", loop.calls == 1)
    check("an unknown id is a 404", unknown == 404)


def test_gate_no_changes_nothing():
    first, _, later, _ = run(SlowLoop(0.3), lambda f, q: False)
    check("gate no: the reply is the full answer", first["speak"] == ["You took your pills."])
    check("gate no: no pending id", "pending" not in first and later is None)


class QuietLoop:
    def process_frame(self, path):
        time.sleep(1.5)
        return {"managed": True, "fired": [], "speak": [], "errors": 0}


def test_hold_on_then_nothing_says_so():
    first, _, later, _ = run(QuietLoop(), lambda f, q: True)
    check("nothing found: the follow-up says so, never a state",
          first["speak"][0].startswith("Hold on") and later["speak"] == [holdon.NOTHING_LINE])


class NoLoop:
    def process_frame(self, path):
        time.sleep(1.5)
        return {"managed": True, "fired": [], "speak": [], "errors": 0, "checked": [
            {"slug": "pills", "kind": "done", "verdict": "no", "question": "Did I take my pills?",
             "reason": "The Wednesday pills are still in the box."}]}


def test_hold_on_then_no_says_what_it_saw():
    _, _, later, _ = run(NoLoop(), lambda f, q: True)
    check("a clear no: says what it saw",
          later["speak"] == ["Not yet. The Wednesday pills are still in the box."])


def test_lines():
    check("line: one fixed sentence, never the question",
          holdon.hold_line([{"question": "Did I take my night pills?", "kind": "done"}])
          == "Hold on, let me check something.")
    check("follow-up: a done question's yes says when it was seen",
          holdon.follow_up([{"slug": "p", "kind": "done", "verdict": "yes",
                             "reason": "The Wednesday compartment is empty."}],
                           fired=["p"], speak=["loop line"],
                           now=__import__("datetime").datetime(2026, 11, 4, 11, 30))
          == ["The Wednesday compartment is empty. I saw it at 11:30 AM."])
    check("follow-up: other lines are said as the loop wrote them",
          holdon.follow_up([], fired=[], speak=["a refusal"]) == ["a refusal"])
    check("follow-up: cannot_tell is the couldn't-confirm line",
          holdon.follow_up([{"verdict": "cannot_tell", "reason": "too dark", "kind": "done"}])
          == [holdon.NOTHING_LINE])
    check("follow-up: a failed check is the couldn't-confirm line",
          holdon.follow_up([{"verdict": None, "reason": "no check was made", "kind": "done"}])
          == [holdon.NOTHING_LINE])
    check("follow-up: a state no is never read back (it can be an all-clear)",
          holdon.follow_up([{"verdict": "no", "reason": "All the knobs point to off.", "kind": "state"}])
          == [holdon.NOTHING_LINE])


def test_full_check_first_wins():
    def slow_gate(f, q):
        time.sleep(1.0)
        return True
    first, _, later, _ = run(SlowLoop(0.1), slow_gate)
    check("full first: no hold-on", first["speak"] == ["You took your pills."] and later is None)


def test_off_by_default():
    first, _, later, _ = run(SlowLoop(0.3), lambda f, q: True, hold_on="")
    check("off: no hold-on even on a gate yes", first["speak"] == ["You took your pills."] and later is None)


def test_rest_and_uncollected():
    p = holdon.Pending()
    check("pending: may hold at first", p.may_hold())
    p.held()
    check("pending: rests after a hold-on", not p.may_hold())
    saved = holdon.COLLECT_S
    holdon.COLLECT_S = 0.1
    try:
        pid = p.open()
        p.finish(pid, {"speak": ["line"], "fired": []})
        quiet = p.open()
        p.finish(quiet, {"speak": [holdon.NOTHING_LINE], "fired": []})
        time.sleep(0.4)
    finally:
        holdon.COLLECT_S = saved
    waiting = p.waiting()
    check("pending: an uncollected answer is offered as an alert",
          waiting == [{"id": f"hold-{pid}", "line": "line"}])
    check("pending: the couldn't-confirm line is never offered later", len(waiting) == 1)
    check("pending: and is not given out twice", p.take(pid, wait_s=0.1) is None)
    check("pending: an ack clears it", p.said(f"hold-{pid}") and p.waiting() == [])
    check("pending: a new guide cannot clear it (own queue)", not hasattr(p, "_say_later"))


def test_gate_failure_is_no():
    saved = holdon.endpoint.ask_json
    holdon.endpoint.ask_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        check("gate: a failure is a no", holdon.gate("/nonexistent.jpg", ["q"]) is False)
    finally:
        holdon.endpoint.ask_json = saved


if __name__ == "__main__":
    test_gate_yes_answers_early()
    test_gate_no_changes_nothing()
    test_hold_on_then_nothing_says_so()
    test_hold_on_then_no_says_what_it_saw()
    test_lines()
    test_full_check_first_wins()
    test_off_by_default()
    test_rest_and_uncollected()
    test_gate_failure_is_no()
    print("ALL PASS")
