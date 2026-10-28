#!/usr/bin/env python3
"""
Tests for the endpoint checker (endpoint.py and verdict.py).

Run from the truss-server folder:  python3 test_endpoint.py
Stdlib only. Exits non-zero on the first failure.

Every test runs against a stub OpenAI-compatible chat/completions
server in a thread, standing in for llama-server or the Meta Model
API. No real model is ever called from here. The API key is a
sentinel: it must show up in the Authorization header the stub
receives, and nowhere in any returned reason.
"""

import base64
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

import endpoint  # noqa: E402
import verdict  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from questions import QuestionStore  # noqa: E402

KEY = "SENTINEL-KEY-endpoint-tests"
FRAME_BYTES = b"\xff\xd8\xff\xe0" + b"current-frame" + b"\xff\xd9"
YES_BYTES = b"\xff\xd8\xff\xe0" + b"yes-look" + b"\xff\xd9"
NO_BYTES = b"\xff\xd8\xff\xe0" + b"no-look" + b"\xff\xd9"

TMP = Path(tempfile.mkdtemp())
FRAME = TMP / "frame.jpg"
FRAME.write_bytes(FRAME_BYTES)
FRAME_PNG = TMP / "frame.png"
FRAME_PNG.write_bytes(b"\x89PNG\r\n\x1a\n" + b"frame")
REF_YES = TMP / "ref-yes.jpg"
REF_YES.write_bytes(YES_BYTES)
REF_NO = TMP / "ref-no.jpg"
REF_NO.write_bytes(NO_BYTES)

YES = {"verdict": "yes", "reason": "All four knobs point to off.", "missing": ""}
CANNOT = {
    "verdict": "cannot_tell",
    "reason": "The stove top is out of frame.",
    "missing": "The four knobs on the front of the stove.",
}


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def uri(data, mime="image/jpeg"):
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class StubModel:
    """A stand-in chat/completions endpoint. replies is a list, one per
    request in order; each is a dict (sent as the message content, JSON
    encoded), a str (sent as the content as is), or an int (an HTTP
    error status with no body worth reading). The last reply repeats."""

    def __init__(self, replies, delay=0, redirect_to=None):
        self.replies = list(replies)
        self.delay = delay
        self.redirect_to = redirect_to
        self.requests = []
        self.base = None

    def __enter__(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up first, as the timeout test means it to

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                stub.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw) if raw else {},
                    }
                )
                if stub.delay:
                    time.sleep(stub.delay)
                if stub.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", stub.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                n = len(stub.requests) - 1
                reply = stub.replies[min(n, len(stub.replies) - 1)]
                if isinstance(reply, int):
                    # The body echoes the key, to prove it is never read
                    # back into a reason.
                    self._send(reply, {"error": {"message": f"refused {KEY}"}})
                    return
                content = reply if isinstance(reply, str) else json.dumps(reply)
                self._send(
                    200,
                    {
                        "choices": [{"message": {"role": "assistant", "content": content}}],
                        "usage": {"prompt_tokens": 1000, "completion_tokens": 40},
                    },
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def body(self, n=0):
        return self.requests[n]["body"]

    def images(self, n=0):
        return [
            part["image_url"]["url"]
            for part in self.body(n)["messages"][1]["content"]
            if part["type"] == "image_url"
        ]

    def system(self, n=0):
        return self.body(n)["messages"][0]["content"]


def cfg(stub, model="muse-glimmer-30b", key=None, timeout=10):
    return endpoint.EndpointConfig(stub.base, model, key, timeout)


def check_shape(name, result, want):
    check(f"{name}: verdict is {want}", result["verdict"] == want)
    check(f"{name}: reason is text", isinstance(result["reason"], str))
    check(f"{name}: missing is text", isinstance(result["missing"], str))
    check(f"{name}: latency is counted", isinstance(result["latency_ms"], int))
    check(
        f"{name}: usage has the canonical keys",
        set(result["usage"]) == {"spark_input_tokens", "spark_output_tokens", "sam_images"},
    )
    check(f"{name}: the key stays out of the reason", KEY not in result["reason"])


@contextlib.contextmanager
def env(**values):
    names = [
        "TRUSS_BASE_URL", "TRUSS_MODEL", "TRUSS_API_KEY", "TRUSS_TIMEOUT_S",
        "MODEL_API_KEY", "TRUSS_CHECKER", "TRUSS_CHECK_MODE",
    ]
    saved = {k: os.environ.get(k) for k in names}
    for k in names:
        os.environ.pop(k, None)
    for k, v in values.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -- configuration ------------------------------------------------------------


def test_config_defaults_to_llama_server():
    with env():
        c = endpoint.config_from_env()
    check("config: default base_url is llama-server on 8080",
          c.base_url == "http://127.0.0.1:8080/v1")
    check("config: default model is Glimmer", c.model == "muse-glimmer-30b")
    check("config: no key by default", c.api_key is None)
    check("config: default timeout", c.timeout_s == endpoint.DEFAULT_TIMEOUT_S)


def test_config_reads_the_environment():
    with env(TRUSS_BASE_URL="https://api.meta.ai/v1/", TRUSS_MODEL="muse-spark-1.3",
             MODEL_API_KEY=KEY, TRUSS_TIMEOUT_S="inf"):
        c = endpoint.config_from_env()
    check("config: base_url read, trailing slash dropped",
          c.base_url == "https://api.meta.ai/v1")
    check("config: model read", c.model == "muse-spark-1.3")
    check("config: MODEL_API_KEY is the fallback key", c.api_key == KEY)
    check("config: a non-finite timeout falls back", c.timeout_s == endpoint.DEFAULT_TIMEOUT_S)
    with env(TRUSS_API_KEY="first", MODEL_API_KEY="second"):
        check("config: TRUSS_API_KEY wins", endpoint.config_from_env().api_key == "first")


# -- the request --------------------------------------------------------------


def test_single_request_shape():
    with StubModel([YES]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check_shape("single", result, "yes")
    check("single: reason carried through", result["reason"] == YES["reason"])
    check("single: missing is empty on a yes", result["missing"] == "")
    check("single: the result names the model", result["model"] == "muse-glimmer-30b")
    check("single: usage counted", result["usage"]["spark_input_tokens"] == 1000
          and result["usage"]["spark_output_tokens"] == 40)
    req = stub.requests[0]
    body = req["body"]
    check("single: posted to /v1/chat/completions", req["path"] == "/v1/chat/completions")
    check("single: the model is named in the body", body["model"] == "muse-glimmer-30b")
    check("single: system then user", [m["role"] for m in body["messages"]] == ["system", "user"])
    check("single: exactly one image, the frame", stub.images() == [uri(FRAME_BYTES)])
    check("single: the question rides as text",
          body["messages"][1]["content"][0] == {"type": "text", "text": "Question: Is the stove off?"})
    rf = body.get("response_format", {})
    check("single: response_format is json_schema", rf.get("type") == "json_schema")
    check("single: the schema is strict", rf["json_schema"]["strict"] is True)
    check("single: the schema is the verdict schema",
          rf["json_schema"]["schema"] == verdict.VERDICT_SCHEMA)
    check("single: the schema is also spelled out in the prompt",
          json.dumps(verdict.VERDICT_SCHEMA, separators=(",", ":")) in stub.system())
    check("single: no looks text without looks", "what tells the yes-look" not in stub.system())
    check("single: no Authorization header without a key", "authorization" not in req["headers"])
    check("single: temperature 0", body["temperature"] == 0)


def test_single_with_looks():
    looks = "On the yes-look all four knobs point straight up; on the no-look one is turned left."
    with StubModel([YES]) as stub:
        verdict.check(FRAME, "Is the stove off?", ref_yes=REF_YES, ref_no=REF_NO,
                      looks=looks, config=cfg(stub))
    check("looks: the looks text is in the system prompt", looks in stub.system())
    check("looks: single mode still sends only the frame", stub.images() == [uri(FRAME_BYTES)])


def test_read_looks():
    folder = TMP / "q-looks"
    folder.mkdir()
    check("read_looks: missing file is None", verdict.read_looks(folder) is None)
    (folder / "looks.txt").write_text("   \n")
    check("read_looks: empty file is None", verdict.read_looks(folder) is None)
    (folder / "looks.txt").write_text("  Knobs up means off.\n")
    check("read_looks: text is stripped", verdict.read_looks(folder) == "Knobs up means off.")


def test_multi_request_shape():
    with StubModel([YES]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", mode="multi",
                               ref_yes=REF_YES, ref_no=REF_NO, looks="ignored", config=cfg(stub))
    check_shape("multi", result, "yes")
    check("multi: three images, yes-look, no-look, then the frame",
          stub.images() == [uri(YES_BYTES), uri(NO_BYTES), uri(FRAME_BYTES)])
    check("multi: the prompt explains the three photos", "three photos" in stub.system())


def test_multi_without_references_runs_single():
    with StubModel([YES]) as stub:
        verdict.check(FRAME, "Is the water off?", mode="multi", config=cfg(stub))
    check("multi, no refs: one image", stub.images() == [uri(FRAME_BYTES)])


def test_png_mime():
    with StubModel([YES]) as stub:
        verdict.check(FRAME_PNG, "Is the stove off?", config=cfg(stub))
    check("png: labelled image/png", stub.images()[0].startswith("data:image/png;base64,"))


def test_meta_api_swap_needs_no_code_change():
    # The same call, configured only from the environment the way the
    # Meta Model API would be: a different base_url, muse-spark-1.3,
    # and a key.
    with StubModel([YES]) as stub, env(TRUSS_BASE_URL=stub.base,
                                       TRUSS_MODEL="muse-spark-1.3", TRUSS_API_KEY=KEY):
        result = verdict.check(FRAME, "Is the stove off?")
    check_shape("meta swap", result, "yes")
    check("meta swap: the model is muse-spark-1.3", stub.body()["model"] == "muse-spark-1.3")
    check("meta swap: the result names it", result["model"] == "muse-spark-1.3")
    check("meta swap: the key rides as a bearer header",
          stub.requests[0]["headers"].get("authorization") == f"Bearer {KEY}")
    check("meta swap: same path", stub.requests[0]["path"] == "/v1/chat/completions")


# -- validate and retry once ---------------------------------------------------


def test_cannot_tell_keeps_missing():
    with StubModel([CANNOT]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check_shape("cannot_tell", result, "cannot_tell")
    check("cannot_tell: missing names the knobs", "knobs" in result["missing"])
    check("cannot_tell: the model's reason stands", result["reason"] == CANNOT["reason"])


def test_missing_blanked_on_a_verdict():
    reply = dict(YES, missing="Nothing, though the oven light is unclear.")
    with StubModel([reply]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check("verdict with missing: missing is blanked", result["missing"] == "")


def test_no_json_mode_retries_without_response_format():
    with StubModel([400, YES]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check_shape("400 then ok", result, "yes")
    check("400 then ok: two requests", len(stub.requests) == 2)
    check("400 then ok: first asked for response_format", "response_format" in stub.body(0))
    check("400 then ok: retry left it off", "response_format" not in stub.body(1))
    check("400 then ok: retry still spells the schema out",
          json.dumps(verdict.VERDICT_SCHEMA, separators=(",", ":")) in stub.system(1))


def test_bad_json_retries_once():
    with StubModel(["The stove looks off to me.", YES]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check_shape("prose then ok", result, "yes")
    check("prose then ok: two requests", len(stub.requests) == 2)
    check("prose then ok: usage summed over both",
          result["usage"]["spark_input_tokens"] == 2000)


def test_bad_twice_is_cannot_tell():
    with StubModel(["not json", "still not json"]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check_shape("bad twice", result, "cannot_tell")
    check("bad twice: exactly one retry", len(stub.requests) == 2)
    check("bad twice: the reason says no check was made", "no check was made" in result["reason"])
    check("bad twice: the model's words are not in the reason",
          "still not json" not in result["reason"])


def test_schema_violations_retry():
    cases = [
        ("unknown verdict", {"verdict": "maybe", "reason": "Hm.", "missing": ""}),
        ("missing field", {"verdict": "yes", "reason": "Knobs up."}),
        ("extra field", dict(YES, confidence=0.9)),
        ("wrong type", {"verdict": "yes", "reason": 7, "missing": ""}),
        ("not an object", ["yes"]),
    ]
    for name, bad in cases:
        with StubModel([bad, bad]) as stub:
            result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
        check(f"{name}: cannot_tell after one retry",
              result["verdict"] == "cannot_tell" and len(stub.requests) == 2)
    with StubModel([dict(YES, confidence=0.9), YES]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check("extra field then ok: the retry's verdict stands", result["verdict"] == "yes")


def test_prose_wrapped_json_is_read():
    wrapped = "```json\n" + json.dumps(YES) + "\n```"
    with StubModel([wrapped]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub))
    check("fenced JSON: read on the first try",
          result["verdict"] == "yes" and len(stub.requests) == 1)


# -- failures are cannot_tell, never a verdict --------------------------------


def test_refused_key():
    with StubModel([401]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub, key=KEY))
    check_shape("401", result, "cannot_tell")
    check("401: the reason names the key", "key" in result["reason"].lower())
    check("401: no retry", len(stub.requests) == 1)


def test_server_error():
    with StubModel([500]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub, key=KEY))
    check_shape("500", result, "cannot_tell")
    check("500: the status is named", "500" in result["reason"])


def test_unreachable():
    c = endpoint.EndpointConfig("http://127.0.0.1:9/v1", "muse-glimmer-30b", None, 5)
    result = verdict.check(FRAME, "Is the stove off?", config=c)
    check_shape("unreachable", result, "cannot_tell")
    check("unreachable: the reason says so", "did not answer" in result["reason"])


def test_timeout():
    with StubModel([YES], delay=3) as stub:
        started = time.monotonic()
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub, timeout=1))
        elapsed = time.monotonic() - started
    check_shape("timeout", result, "cannot_tell")
    check("timeout: the wait was bounded", elapsed < 2.5)


def test_redirect_not_followed():
    with StubModel([YES]) as target, StubModel([YES], redirect_to=None) as stub:
        stub.redirect_to = target.base + "/chat/completions"
        result = verdict.check(FRAME, "Is the stove off?", config=cfg(stub, key=KEY))
    check_shape("redirect", result, "cannot_tell")
    check("redirect: the other server was never hit", target.requests == [])


def test_one_reference_is_a_setup_error():
    with StubModel([YES]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", mode="multi",
                               ref_yes=REF_YES, config=cfg(stub))
    check_shape("one reference", result, "cannot_tell")
    check("one reference: nothing was sent", stub.requests == [])


def test_unreadable_frame():
    with StubModel([YES]) as stub:
        result = verdict.check(TMP / "nope.jpg", "Is the stove off?", config=cfg(stub))
    check_shape("unreadable frame", result, "cannot_tell")
    check("unreadable frame: nothing was sent", stub.requests == [])


def test_unknown_mode():
    with StubModel([YES]) as stub:
        result = verdict.check(FRAME, "Is the stove off?", mode="triple", config=cfg(stub))
    check_shape("unknown mode", result, "cannot_tell")
    check("unknown mode: nothing was sent", stub.requests == [])


# -- ask_json for other callers (the plant-time compile) ----------------------


def test_ask_json_for_other_schemas():
    schema = {
        "type": "object",
        "properties": {
            "prompts": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
            "looks": {"type": "string"},
        },
        "required": ["prompts", "looks"],
        "additionalProperties": False,
    }
    good = {"prompts": ["stove", "knob"], "looks": "Knobs up means off."}
    with StubModel([{"prompts": ["stove"], "looks": "x"}, good]) as stub:
        reply = endpoint.ask_json(
            "Compile this question.",
            [endpoint.text_part("Is the stove off?"), endpoint.image_part(REF_YES)],
            schema, "compile", config=cfg(stub),
        )
    check("ask_json: too few items retried, then accepted",
          reply["prompts"] == ["stove", "knob"] and len(stub.requests) == 2)
    check("ask_json: the schema name rides in response_format",
          stub.body()["response_format"]["json_schema"]["name"] == "compile")
    check("ask_json: the model is reported", reply["_model"] == "muse-glimmer-30b")
    with StubModel(["nope"]) as stub:
        try:
            endpoint.ask_json("x", [endpoint.text_part("y")], schema, "compile", config=cfg(stub))
            raised = False
        except endpoint.EndpointError:
            raised = True
    check("ask_json: raises EndpointError after one retry", raised)


def test_validate():
    v = endpoint.validate
    s = verdict.VERDICT_SCHEMA
    check("validate: the canonical reply is valid", v(YES, s) == [])
    check("validate: a bool is not an integer", v(True, {"type": "integer"}) != [])
    check("validate: maxLength", v("abcd", {"type": "string", "maxLength": 3}) != [])
    check("validate: nested items checked",
          v(["a", 2], {"type": "array", "items": {"type": "string"}}) != [])


# -- wiring through server.py and the answer log ------------------------------


def load_server(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_server_wiring():
    looks_dir = TMP / "q-wired"
    looks_dir.mkdir()
    (looks_dir / "ref-yes.jpg").write_bytes(YES_BYTES)
    (looks_dir / "ref-no.jpg").write_bytes(NO_BYTES)
    (looks_dir / "looks.txt").write_text("Knobs up means off.")
    entry = {
        "slug": "is-the-stove-off",
        "question": "Is the stove off?",
        "ref_yes_path": str(looks_dir / "ref-yes.jpg"),
        "ref_no_path": str(looks_dir / "ref-no.jpg"),
    }
    with StubModel([YES]) as stub, env(TRUSS_BASE_URL=stub.base):
        wired = load_server("server_wired_endpoint")
        result = wired.production_check(FRAME, entry)
        wired.guide_check(FRAME, "Is the water off?")
    check("wiring: the endpoint checker is the default", wired.CHECKER == "endpoint")
    check("wiring: single is the default mode", wired.CHECK_MODE == "single")
    check("wiring: production_check answered by the endpoint", result["verdict"] == "yes")
    check("wiring: the loop's check reads looks.txt", "Knobs up means off." in stub.system(0))
    check("wiring: the guide's check has no looks", "Knobs up means off." not in stub.system(1))
    check("wiring: the guide's check sends one image", len(stub.images(1)) == 1)

    with StubModel([YES]) as stub, env(TRUSS_BASE_URL=stub.base, TRUSS_CHECK_MODE="multi"):
        wired = load_server("server_wired_multi")
        wired.production_check(FRAME, entry)
    check("wiring multi: three images", len(stub.images()) == 3)

    with env(TRUSS_CHECK_MODE="banana", TRUSS_CHECKER="meta"):
        wired = load_server("server_wired_bad_mode")
    check("wiring: an unknown mode falls back to single", wired.CHECK_MODE == "single")
    check("wiring: meta is the endpoint checker", wired.CHECKER == "endpoint")


def test_answer_log_names_the_model():
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    slug = store.create("Is the stove off?", {"kind": "interval", "minutes": 0})
    store.save_ref(slug, "yes", YES_BYTES)
    store.save_ref(slug, "no", NO_BYTES)
    captures = tmp / "captures"
    captures.mkdir()
    shot = captures / "1.jpg"
    shot.write_bytes(FRAME_BYTES)
    with StubModel([YES]) as stub, env(TRUSS_BASE_URL=stub.base):
        wired = load_server("server_wired_log")
        loop = CheckLoop(store, wired.production_check, tmp / "log",
                         speak_fn=lambda *a: None)
        result = loop.process_frame(shot)
    check("log: the frame fired", result["fired"] == [slug])
    text = (Path(result["evidence"]).parent / "answers.txt").read_text()
    check("log: the answer line names the model", "| model: muse-glimmer-30b" in text)


if __name__ == "__main__":
    test_config_defaults_to_llama_server()
    test_config_reads_the_environment()
    test_single_request_shape()
    test_single_with_looks()
    test_read_looks()
    test_multi_request_shape()
    test_multi_without_references_runs_single()
    test_png_mime()
    test_meta_api_swap_needs_no_code_change()
    test_cannot_tell_keeps_missing()
    test_missing_blanked_on_a_verdict()
    test_no_json_mode_retries_without_response_format()
    test_bad_json_retries_once()
    test_bad_twice_is_cannot_tell()
    test_schema_violations_retry()
    test_prose_wrapped_json_is_read()
    test_refused_key()
    test_server_error()
    test_unreachable()
    test_timeout()
    test_redirect_not_followed()
    test_one_reference_is_a_setup_error()
    test_unreadable_frame()
    test_unknown_mode()
    test_ask_json_for_other_schemas()
    test_validate()
    test_server_wiring()
    test_answer_log_names_the_model()
    print("ALL PASS")
