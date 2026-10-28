#!/usr/bin/env python3
"""
Tests for the Aman checker.

Run from the truss-server folder:  python3 test_aman_check.py
Stdlib only. Exits non-zero on the first failure.

Every test runs against a stub Aman HTTP server in a thread. No real
Aman is ever called from here. The token is a sentinel: it must show
up in the Authorization header the stub receives, and nowhere in any
returned reason.
"""

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aman_check  # noqa: E402

TOKEN = "SENTINEL-TOKEN-aman-check-tests"
ZERO_USAGE = {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0}
JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"

TMP = Path(tempfile.mkdtemp())
FRAME = TMP / "frame.jpg"
FRAME.write_bytes(JPEG)
FRAME_PNG = TMP / "frame.png"
FRAME_PNG.write_bytes(b"\x89PNG\r\n\x1a\n" + b"frame-bytes")
REF_YES = TMP / "ref-yes.jpg"
REF_YES.write_bytes(JPEG)
REF_NO = TMP / "ref-no.jpg"
REF_NO.write_bytes(JPEG)
ENTRY = {
    "question": "Is the tv on?",
    "ref_yes_path": str(REF_YES),
    "ref_no_path": str(REF_NO),
}


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def check_result(name, result, verdict):
    check(f"{name}: verdict is {verdict}", result["verdict"] == verdict)
    check(f"{name}: reason is text", isinstance(result["reason"], str))
    check(f"{name}: usage is the zero shape", result["usage"] == ZERO_USAGE)
    check(f"{name}: latency is counted", isinstance(result["latency_ms"], int))
    check(
        f"{name}: sentinel token stays out of the reason",
        TOKEN not in result["reason"],
    )


class StubAman:
    """A stand-in Aman turns API. running_polls counts how many polls
    answer "running" before the final state; None means the turn
    never finishes."""

    def __init__(
        self,
        output=None,
        actions=None,
        post_status=200,
        get_status=200,
        running_polls=0,
        final_status="done",
    ):
        self.output = output
        self.actions = actions or []
        self.post_status = post_status
        self.get_status = get_status
        self.running_polls = running_polls
        self.final_status = final_status
        self.requests = []
        self.polls = 0
        self.base = None
        self._server = None

    def __enter__(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw) if raw else {}
                except Exception:
                    body = {}
                stub.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )
                if stub.post_status != 200:
                    self._reply(
                        stub.post_status, {"error": {"message": "stub refusal"}}
                    )
                    return
                self._reply(
                    200, {"data": {"id": "turn-1", "state": {"status": "running"}}}
                )

            def do_GET(self):
                stub.requests.append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": None,
                    }
                )
                if stub.get_status != 200:
                    self._reply(
                        stub.get_status, {"error": {"message": "stub refusal"}}
                    )
                    return
                stub.polls += 1
                if stub.running_polls is None or stub.polls <= stub.running_polls:
                    self._reply(
                        200,
                        {"data": {"id": "turn-1", "state": {"status": "running"}}},
                    )
                    return
                state = {
                    "status": stub.final_status,
                    "required_actions": stub.actions,
                }
                if stub.final_status == "done":
                    state["output"] = {"content": stub.output}
                self._reply(200, {"data": {"id": "turn-1", "state": state}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


@contextlib.contextmanager
def stub_env(stub, **overrides):
    values = {
        "AMAN_BASE": stub.base if stub is not None else None,
        "AMAN_TOKEN": TOKEN,
        "AMAN_CHECK_SESSION_ID": "check-chat-1",
        "AMAN_CHECK_TIMEOUT_S": None,
        "TRUSS_CHECKER": None,
        "MODEL_API_KEY": None,
    }
    values.update(overrides)
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def header_of(request, name):
    for key, value in request["headers"].items():
        if key.lower() == name.lower():
            return value
    return None


def parts_of(request):
    return request["body"]["input"][0]["content"]


# -- verdicts ---------------------------------------------------------------


def test_yes_verdict():
    out = '{"verdict": "yes", "reason": "The television is on."}'
    with StubAman(output=out) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("yes", result, "yes")
    check("yes: reason carried through", result["reason"] == "The television is on.")
    auth = header_of(stub.requests[0], "Authorization")
    check("yes: the stub received the token as a bearer header",
          auth == f"Bearer {TOKEN}")


def test_no_verdict():
    out = '{"verdict": "no", "reason": "A burner is lit."}'
    with StubAman(output=out) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the stove off?")
    check_result("no", result, "no")
    check("no: reason carried through", result["reason"] == "A burner is lit.")


def test_cannot_tell_verdict():
    out = '{"verdict": "cannot_tell", "reason": "The burners are not visible."}'
    with StubAman(output=out) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the stove off?")
    check_result("cannot_tell", result, "cannot_tell")
    check(
        "cannot_tell: the model's own reason stands",
        result["reason"] == "The burners are not visible.",
    )


def test_verdict_wrapped_in_prose():
    out = 'Here is the answer: {"verdict": "yes", "reason": "Seen clearly."} Done.'
    with StubAman(output=out) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("prose-wrapped verdict", result, "yes")
    check("prose-wrapped verdict: reason read", result["reason"] == "Seen clearly.")


def test_list_content_parts():
    out = [{"type": "text", "text": '{"verdict": "no", "reason": "It is dark."}'}]
    with StubAman(output=out) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("list content", result, "no")


def test_polling_until_done():
    out = '{"verdict": "yes", "reason": "Settled on the third look."}'
    with StubAman(output=out, running_polls=2) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("polling", result, "yes")
    check("polling: the stub was polled until done", stub.polls == 3)


# -- the request body ---------------------------------------------------------


def test_planted_request_body():
    out = '{"verdict": "yes", "reason": "Matches the yes look."}'
    with StubAman(output=out) as stub, stub_env(stub):
        result = aman_check.check(
            FRAME, "Is the tv on?", ref_yes=REF_YES, ref_no=REF_NO
        )
    check_result("planted", result, "yes")
    parts = parts_of(stub.posts()[0])
    kinds = [part["type"] for part in parts]
    check(
        "planted: parts are text, frame, then a label and a file per reference",
        kinds == ["text", "file", "text", "file", "text", "file"],
    )
    files = [part for part in parts if part["type"] == "file"]
    check("planted: three file parts ride along", len(files) == 3)
    check(
        "planted: the question text names the reference looks",
        "reference" in parts[0]["text"].lower()
        and "yes" in parts[0]["text"].lower(),
    )
    check(
        "planted: the JSON-only instruction rides along",
        "JSON only" in parts[0]["text"],
    )


def test_generic_request_body_has_one_file():
    out = '{"verdict": "yes", "reason": "On."}'
    with StubAman(output=out) as stub, stub_env(stub):
        aman_check.check(FRAME, "Is the tv on?")
    parts = parts_of(stub.posts()[0])
    files = [part for part in parts if part["type"] == "file"]
    check("generic: exactly one file part, the frame", len(files) == 1)
    check(
        "generic: the frame is labelled image/jpeg",
        files[0]["data"].startswith("data:image/jpeg;base64,"),
    )


def test_png_frame_uses_png_mime():
    out = '{"verdict": "yes", "reason": "On."}'
    with StubAman(output=out) as stub, stub_env(stub):
        aman_check.check(FRAME_PNG, "Is the tv on?")
    parts = parts_of(stub.posts()[0])
    files = [part for part in parts if part["type"] == "file"]
    check(
        "png: the frame is labelled image/png",
        files[0]["data"].startswith("data:image/png;base64,"),
    )


# -- failures are cannot_tell, never a verdict --------------------------------


def test_required_actions_gives_cannot_tell():
    out = '{"verdict": "yes", "reason": "On."}'
    actions = [{"type": "tool.approval_required"}]
    with StubAman(output=out, actions=actions) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("required actions", result, "cannot_tell")
    check(
        "required actions: the reason names the approval",
        "approval" in result["reason"].lower(),
    )


def test_unauthorized_gives_cannot_tell():
    with StubAman(post_status=401) as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("401", result, "cannot_tell")
    check("401: the reason names the token", "token" in result["reason"].lower())


def test_non_json_reply_gives_cannot_tell():
    with StubAman(output="I think it is on, probably.") as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("non-JSON reply", result, "cannot_tell")


def test_error_turn_gives_cannot_tell():
    with StubAman(final_status="error") as stub, stub_env(stub):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("error turn", result, "cannot_tell")


def test_never_finishes_times_out():
    with StubAman(running_polls=None) as stub, stub_env(
        stub, AMAN_CHECK_TIMEOUT_S="1"
    ):
        started = time.monotonic()
        result = aman_check.check(FRAME, "Is the tv on?")
        elapsed = time.monotonic() - started
    check_result("timeout", result, "cannot_tell")
    check("timeout: the wait was short", elapsed < 10)
    check(
        "timeout: the reason names the wait",
        "in time" in result["reason"].lower(),
    )


def test_missing_token():
    with StubAman() as stub, stub_env(stub, AMAN_TOKEN=None):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("missing token", result, "cannot_tell")
    check(
        "missing token: the reason names what is missing",
        "AMAN_TOKEN" in result["reason"],
    )
    check("missing token: nothing was sent", stub.requests == [])


def test_missing_session_id():
    with StubAman() as stub, stub_env(stub, AMAN_CHECK_SESSION_ID=None):
        result = aman_check.check(FRAME, "Is the tv on?")
    check_result("missing session id", result, "cannot_tell")
    check(
        "missing session id: the reason names what is missing",
        "AMAN_CHECK_SESSION_ID" in result["reason"],
    )
    check("missing session id: nothing was sent", stub.requests == [])


# -- wiring through server.py -------------------------------------------------


def load_server(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wiring_aman_routes_to_the_stub():
    out = '{"verdict": "yes", "reason": "The television is on."}'
    with StubAman(output=out) as stub, stub_env(stub, TRUSS_CHECKER="aman"):
        wired = load_server("server_wired_aman")
        result = wired.production_check(FRAME, ENTRY)
        guide_result = wired.guide_check(FRAME, "Is the tv on?")
    check("wiring aman: production_check is answered by Aman",
          result["verdict"] == "yes")
    check("wiring aman: guide_check is answered by Aman",
          guide_result["verdict"] == "yes")
    check("wiring aman: two turns were posted", len(stub.posts()) == 2)


def test_wiring_default_keeps_the_no_key_result():
    with stub_env(None):
        wired = load_server("server_wired_meta")
        result = wired.production_check(FRAME, ENTRY)
        guide_result = wired.guide_check(FRAME, "Is the tv on?")
    check(
        "wiring default: production_check keeps the no-key result",
        result == wired.no_key_result(),
    )
    check(
        "wiring default: guide_check keeps the no-key result",
        guide_result == wired.no_key_result(),
    )


def test_wiring_unknown_value_falls_back_to_meta():
    with stub_env(None, TRUSS_CHECKER="banana"):
        wired = load_server("server_wired_banana")
        result = wired.production_check(FRAME, ENTRY)
    check(
        "wiring unknown: the checker falls back to meta",
        wired.CHECKER == "meta",
    )
    check(
        "wiring unknown: the no-key result stands",
        result == wired.no_key_result(),
    )


if __name__ == "__main__":
    test_yes_verdict()
    test_no_verdict()
    test_cannot_tell_verdict()
    test_verdict_wrapped_in_prose()
    test_list_content_parts()
    test_polling_until_done()
    test_planted_request_body()
    test_generic_request_body_has_one_file()
    test_png_frame_uses_png_mime()
    test_required_actions_gives_cannot_tell()
    test_unauthorized_gives_cannot_tell()
    test_non_json_reply_gives_cannot_tell()
    test_error_turn_gives_cannot_tell()
    test_never_finishes_times_out()
    test_missing_token()
    test_missing_session_id()
    test_wiring_aman_routes_to_the_stub()
    test_wiring_default_keeps_the_no_key_result()
    test_wiring_unknown_value_falls_back_to_meta()
    print("ALL PASS")
