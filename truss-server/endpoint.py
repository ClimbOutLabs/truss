#!/usr/bin/env python3
"""
The model endpoint, in one place.

Every model call the server makes goes through ask_json(): one OpenAI
chat/completions request whose user content carries text and image
parts, asking for a reply in a strict JSON schema. The same code runs
unchanged against the DGX's llama-server (the default) and against the
Meta Model API; only the configuration differs:

    TRUSS_BASE_URL  default http://127.0.0.1:8080/v1 (llama-server)
                    the Meta Model API is https://api.meta.ai/v1
    TRUSS_MODEL     default muse-glimmer-30b
                    on the Meta Model API, muse-spark-1.3 (never the
                    contributor tier)
    TRUSS_API_KEY   optional; MODEL_API_KEY is read when it is unset.
                    llama-server needs none.
    TRUSS_TIMEOUT_S default 120, for the whole call including its retry

Muse Spark over the Meta Model API, first, with the above as fallback:

    TRUSS_META_API_KEY    turns it on. Sent over https only; never
                          logged, never put in a reason.
    TRUSS_META_BASE_URL   default https://api.meta.ai/v1 (https only)
    TRUSS_META_MODEL      default muse-spark-1.3 (never the
                          contributor tier)
    TRUSS_META_TIMEOUT_S  default 45

With TRUSS_META_API_KEY set, every call that names no config of its
own (checks, the plant-time compile, the guide) goes to Muse Spark.
The endpoint above is then a fallback only if TRUSS_BASE_URL is set
explicitly (Glimmer on the DGX need not be running at all). A Muse
Spark call that fails is asked again of the fallback, and a failure
that says the API is down or refusing (unreachable, timeout, refused,
an HTTP error) rests Muse Spark for META_REST_S seconds so each check
does not wait out the timeout first. Every line printed about it names
the model. With TRUSS_META_API_KEY unset nothing changes.

A model name naming the contributor tier is refused outright. A key is
only sent over https or to this machine, never over plain http to the
DGX.

The reply is held to the schema twice over. The request asks for it
with response_format json_schema, strict, and the system prompt spells
the schema out too, for servers that ignore response_format. Whatever
comes back is then validated here against the same schema. A reply
that does not validate, or a server that refuses response_format with
a 400, gets exactly one retry (the retry leaves response_format off).
A second failure raises EndpointError. A reply cut off by the token
limit is never parsed at all (see ask_json), and thinking blocks are
removed before the reply is read, which must then be one JSON object
and nothing else.

Standard library only. The key rides in the Authorization header only;
requests use a no-redirect opener so that header can only reach the
configured origin; response bodies are never printed or put into an
error message, so a reason can never carry the key or a model's
unvalidated words.
"""

import base64
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "muse-glimmer-30b"
DEFAULT_TIMEOUT_S = 120.0

DEFAULT_META_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_META_MODEL = "muse-spark-1.3"
DEFAULT_META_TIMEOUT_S = 45.0
# How long Muse Spark rests after a failure saying the API is down, when
# there is a fallback to ask instead.
META_REST_S = 300.0
_REST_KINDS = ("unreachable", "timeout", "refused", "http")

# A thinking model that runs out of room is retried once with this
# much more room.
TRUNCATION_GROWTH = 2

EndpointConfig = namedtuple(
    "EndpointConfig", ["base_url", "model", "api_key", "timeout_s"]
)


class EndpointError(Exception):
    """A model call that did not produce a valid reply. The message is
    a plain reason, safe to speak or log: it never carries the key or
    the reply body. kind names the failure for counting: "config",
    "unreachable", "refused", "http", "timeout", "truncated" or
    "invalid"."""

    def __init__(self, message, kind="invalid", model=None):
        super().__init__(message)
        self.kind = kind
        self.model = model


def refuse_contributor(model):
    """Frames never go to a trained-on tier. The Muse Spark contributor
    tier trains on what it is sent, so any model name naming it is
    refused before anything is sent."""
    if isinstance(model, str) and "contributor" in model.lower():
        raise EndpointError(
            "The configured model is a contributor tier, which trains on "
            "what it is sent, so nothing was sent to it.",
            kind="config",
        )


def _key_allowed(base_url):
    """A key is only ever sent over https, or to this machine. Over
    plain http to another machine (the DGX on the LAN) it is left off:
    llama-server needs none, and the Meta key must never cross the
    network in the clear."""
    parsed = urlparse(base_url)
    if parsed.scheme == "https":
        return True
    return parsed.hostname in ("127.0.0.1", "localhost", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the Authorization header is never
    re-sent to an address the configured endpoint did not name."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _timeout_seconds(raw, default=DEFAULT_TIMEOUT_S):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def config_from_env():
    """The endpoint configuration from the environment, with the
    llama-server defaults."""
    base_url = os.environ.get("TRUSS_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("TRUSS_MODEL") or DEFAULT_MODEL
    api_key = os.environ.get("TRUSS_API_KEY") or os.environ.get("MODEL_API_KEY") or None
    timeout_s = _timeout_seconds(os.environ.get("TRUSS_TIMEOUT_S"))
    refuse_contributor(model)
    return EndpointConfig(base_url.rstrip("/"), model, api_key, timeout_s)


def meta_config_from_env():
    """Muse Spark over the Meta Model API, or None when
    TRUSS_META_API_KEY is unset. Raises EndpointError for a
    contributor-tier model, or for a base URL that is not https: the
    key never crosses the network in the clear."""
    api_key = (os.environ.get("TRUSS_META_API_KEY") or "").strip() or None
    if api_key is None:
        return None
    base_url = (os.environ.get("TRUSS_META_BASE_URL") or "").strip() or DEFAULT_META_BASE_URL
    model = (os.environ.get("TRUSS_META_MODEL") or "").strip() or DEFAULT_META_MODEL
    timeout_s = _timeout_seconds(os.environ.get("TRUSS_META_TIMEOUT_S"), DEFAULT_META_TIMEOUT_S)
    refuse_contributor(model)
    if urlparse(base_url).scheme != "https":
        raise EndpointError(
            "TRUSS_META_BASE_URL is not https, so the Meta key is never sent there.",
            kind="config",
            model=model,
        )
    return EndpointConfig(base_url.rstrip("/"), model, api_key, timeout_s)


def meta_on():
    """True when TRUSS_META_API_KEY is set, whether or not the rest of
    the Meta settings are usable."""
    return bool((os.environ.get("TRUSS_META_API_KEY") or "").strip())


def fallback_config_from_env():
    """The endpoint asked when Muse Spark is not, or None. With the Meta
    key set, the fallback is used only when TRUSS_BASE_URL is set
    explicitly; without it, this is config_from_env() as always."""
    if meta_on() and not (os.environ.get("TRUSS_BASE_URL") or "").strip():
        return None
    return config_from_env()


_rest = {"until": 0.0}
_rest_lock = threading.Lock()


def _resting():
    with _rest_lock:
        return time.monotonic() < _rest["until"]


def _rest_meta():
    with _rest_lock:
        _rest["until"] = time.monotonic() + META_REST_S


def reset_rest():
    """Let Muse Spark be asked again at once. For tests."""
    with _rest_lock:
        _rest["until"] = 0.0
    with _fallback_lock:
        _fallback_seen.clear()


# Where lines about Muse Spark and its fallback go: the server log.
report = print

# What a fallback must say it serves before Muse Spark's work goes to it.
# Whatever answers at TRUSS_BASE_URL is asked GET /models first (cached
# for FALLBACK_CHECK_S), and only a Meta model name lets the call through,
# so a DGX serving something else on that port is never asked.
_META_NAME = ("glimmer", "muse")
FALLBACK_CHECK_S = 60.0
_fallback_seen = {}
_fallback_lock = threading.Lock()


def _served_models(base_url):
    req = urllib.request.Request(f"{base_url}/models", method="GET")
    try:
        with _OPENER.open(req, timeout=3) as resp:
            data = json.loads(resp.read(256 * 1024).decode("utf-8", "replace"))
    except Exception:
        return None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")]


def fallback_serves_meta(cfg):
    """None when the fallback serves a Meta model, otherwise the plain
    reason it is not asked. Never sends a key."""
    now = time.monotonic()
    with _fallback_lock:
        hit = _fallback_seen.get(cfg.base_url)
        if hit and hit[0] > now:
            return hit[1]
    names = _served_models(cfg.base_url)
    if names is None:
        reason = f"the fallback at {cfg.base_url} did not list its models"
    elif any(word in n.lower() for n in names for word in _META_NAME):
        reason = None
    else:
        reason = f"the fallback at {cfg.base_url} serves {', '.join(names)[:120]}, not a Meta model"
    with _fallback_lock:
        _fallback_seen[cfg.base_url] = (now + FALLBACK_CHECK_S, reason)
    return reason


def describe_from_env():
    """What GET /status and the startup line show: the model asked
    first, its base URL, the fallback's model and base URL, and any
    setup problem. Never the key."""
    out = {"model": None, "base_url": None, "fallback_model": None,
           "fallback_base_url": None, "meta_resting": False, "config_error": None}
    problems = []
    try:
        meta = meta_config_from_env()
    except EndpointError as e:
        meta = None
        problems.append(str(e))
    try:
        fallback = fallback_config_from_env()
    except EndpointError as e:
        fallback = None
        problems.append(str(e))
    first = meta or fallback
    if first is not None:
        out["model"], out["base_url"] = first.model, first.base_url
    if meta is not None and fallback is not None:
        out["fallback_model"], out["fallback_base_url"] = fallback.model, fallback.base_url
        out["meta_resting"] = _resting()
    if first is None and not problems:
        problems.append("No model is set up: set TRUSS_META_API_KEY or TRUSS_BASE_URL.")
    out["config_error"] = " ".join(problems) or None
    return out


# -- content parts ------------------------------------------------------------


def _mime(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "image/jpeg"


def text_part(text):
    return {"type": "text", "text": text}


def image_part(path):
    """An image_url content part carrying the file as a data URI.
    Raises EndpointError when the file cannot be read."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise EndpointError(f"The photo {Path(path).name} could not be read.", kind="config")
    data = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{_mime(path)};base64,{data}"}}


# -- schema validation --------------------------------------------------------

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value, schema, where="reply"):
    """Check value against the subset of JSON Schema the server's own
    schemas use: type, enum, properties, required, additionalProperties
    (false only), items, minItems, maxItems, minLength, maxLength.
    Returns a list of problems; empty means valid."""
    problems = []
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_TYPES.get(k, lambda v: False)(value) for k in kinds):
            return [f"{where} is not of type {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where} is not one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{where} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{where} is too long")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{where} is missing {name}")
        if schema.get("additionalProperties") is False:
            if any(name not in props for name in value):
                problems.append(f"{where} has an unexpected field")
        for name, sub in props.items():
            if name in value:
                problems.extend(validate(value[name], sub, f"{where}.{name}"))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{where} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{where} has too many items")
        if "items" in schema:
            for i, item in enumerate(value):
                problems.extend(validate(item, schema["items"], f"{where}[{i}]"))
    return problems


class _Truncated(Exception):
    pass


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _strip_thinking(text):
    """The reply with every closed <think>...</think> block removed.
    An unclosed <think> means the answer never came: _Truncated."""
    while True:
        start = text.find("<think>")
        if start == -1:
            break
        end = text.find("</think>", start)
        if end == -1:
            raise _Truncated()
        text = text[:start] + text[end + len("</think>"):]
    if "</think>" in text:
        # Some servers strip the opening tag but keep the closing one:
        # everything before it is thinking.
        text = text[text.rfind("</think>") + len("</think>"):]
    return text


def _strip_fence(text):
    """The inside of one ```json ... ``` fence wrapping the whole reply."""
    if text.startswith("```") and text.endswith("```") and len(text) >= 6:
        inner = text[3:-3]
        newline = inner.find("\n")
        first_line = inner[:newline] if newline != -1 else inner
        if newline != -1 and first_line.strip().isalpha():
            inner = inner[newline + 1:]
        return inner.strip()
    return text


def _parse_json(text):
    """The reply as JSON, strictly: thinking blocks removed, one fence
    around the whole reply allowed, and then the whole remaining text
    must be one JSON value with no duplicate keys. Prose around the
    object, or a draft object inside thinking, is never read as the
    answer. None when it does not parse; _Truncated when thinking never
    closed."""
    text = _strip_fence(_strip_thinking(text or "").strip())
    if not text:
        return None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError:
        return None


# -- the call -----------------------------------------------------------------


def _post(url, body, api_key, timeout):
    """One POST. Returns (status, text). Network-level failures are an
    EndpointError; HTTP errors come back as their status."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with _OPENER.open(req, timeout=max(1.0, timeout)) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return e.code, text
    except TimeoutError:
        raise EndpointError("The model endpoint did not finish in time.", kind="timeout")
    except Exception as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            raise EndpointError("The model endpoint did not finish in time.", kind="timeout")
        raise EndpointError("The model endpoint did not answer.", kind="unreachable")


def _status_reason(status):
    if status in (401, 403):
        return "The model endpoint refused the API key."
    if 300 <= status < 400:
        return f"The model endpoint redirected (status {status}), which is not followed."
    return f"The model endpoint answered with an error (status {status})."


def _schema_instruction(schema):
    return (
        " Reply with one JSON object only, no other text, matching this "
        f"JSON schema exactly: {json.dumps(schema, separators=(',', ':'))}"
    )


def _reply_content(text):
    """(content, usage, finish_reason) from a chat/completions response
    body. Thinking a server puts in reasoning_content is never read."""
    try:
        data = json.loads(text)
        choice = data["choices"][0]
        message = choice["message"]
    except Exception:
        raise EndpointError("The model endpoint's reply could not be read.")
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def count(name):
        n = usage.get(name, 0)
        return n if isinstance(n, int) and not isinstance(n, bool) else 0

    return (
        content if isinstance(content, str) else "",
        {"input_tokens": count("prompt_tokens"), "output_tokens": count("completion_tokens")},
        finish,
    )


def ask_json(system, content, schema, schema_name, config=None, max_tokens=None):
    """Ask the model one question and return its reply as a dict that
    validates against schema.

    system is the system prompt, content a list of OpenAI content parts
    (text_part and image_part), schema a JSON schema for an object, and
    schema_name the name response_format carries. The returned dict
    also carries "_usage" ({"input_tokens", "output_tokens"}, summed
    over attempts) and "_model" (the configured model), which are not
    part of the schema. Raises EndpointError when no valid reply comes
    back within one retry.

    A reply cut off by the token limit (finish_reason "length", or
    thinking that never closed) is never parsed, whatever it holds: it
    is retried once with TRUNCATION_GROWTH times the room, and a second
    cut-off raises EndpointError with kind "truncated".

    With no config, the call goes to Muse Spark when TRUSS_META_API_KEY
    is set and falls back as the module docstring says; "_model" names
    the model that answered, and an EndpointError's model the last one
    asked.
    """
    if config is not None:
        return _ask_one(config, system, content, schema, schema_name, max_tokens)
    meta, fallback, meta_problem = None, None, None
    try:
        meta = meta_config_from_env()
    except EndpointError as e:
        meta_problem = e
    fallback = fallback_config_from_env()

    def usable(cfg):
        """The fallback, if Muse Spark's work may go to it: on the Meta
        route it must list a Meta model (see fallback_serves_meta).
        Checked only when the fallback is needed, so a healthy Muse
        Spark costs no extra request."""
        if cfg is None or (meta is None and meta_problem is None):
            return cfg
        why_not = fallback_serves_meta(cfg)
        if why_not is None:
            return cfg
        report(f"{cfg.model} is not asked: {why_not}.")
        return None

    if meta_problem is not None:
        fallback = usable(fallback)
        if fallback is None:
            raise meta_problem
        report(f"Muse Spark is not used ({meta_problem}); asking {fallback.model}.")
    elif meta is not None:
        # A resting Muse Spark is skipped only for a fallback that may
        # stand in; with none, Muse Spark is asked as usual.
        if not (_resting() and fallback is not None and usable(fallback) is not None):
            try:
                return _ask_one(meta, system, content, schema, schema_name, max_tokens)
            except EndpointError as e:
                fallback = usable(fallback)
                if fallback is None:
                    # Nothing valid can stand in, so Muse Spark is never
                    # rested: the next call asks it again.
                    raise
                if e.kind in _REST_KINDS:
                    _rest_meta()
                    rest = f" Resting {meta.model} for {int(META_REST_S)} s."
                else:
                    rest = ""
                report(f"{meta.model} failed ({e}); asking {fallback.model} instead.{rest}")
    if fallback is None:
        raise EndpointError(
            "No model is set up: set TRUSS_META_API_KEY or TRUSS_BASE_URL.", kind="config"
        )
    return _ask_one(fallback, system, content, schema, schema_name, max_tokens)


def _ask_one(cfg, system, content, schema, schema_name, max_tokens):
    """ask_json against one endpoint, with its one retry. An
    EndpointError raised here names cfg's model."""
    try:
        return _ask_one_inner(cfg, system, content, schema, schema_name, max_tokens)
    except EndpointError as e:
        if e.model is None:
            e.model = cfg.model
        raise


def _ask_one_inner(cfg, system, content, schema, schema_name, max_tokens):
    refuse_contributor(cfg.model)
    api_key = cfg.api_key if _key_allowed(cfg.base_url) else None
    url = f"{cfg.base_url}/chat/completions"
    messages = [
        {"role": "system", "content": system + _schema_instruction(schema)},
        {"role": "user", "content": content},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
    }
    deadline = time.monotonic() + cfg.timeout_s
    usage = {"input_tokens": 0, "output_tokens": 0}
    use_format = True
    failure, failure_kind = None, "invalid"
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EndpointError("The model endpoint did not finish in time.", kind="timeout")
        body = {"model": cfg.model, "messages": messages, "temperature": 0}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if use_format:
            body["response_format"] = response_format
        status, text = _post(url, body, api_key, remaining)
        if status in (400, 422) and use_format and attempt == 0:
            # Most likely a server without JSON-schema mode: ask again
            # without response_format, with the schema in the prompt.
            use_format = False
            failure, failure_kind = _status_reason(status), "http"
            continue
        if status != 200:
            kind = "refused" if status in (401, 403) else "http"
            raise EndpointError(_status_reason(status), kind=kind)
        reply, used, finish = _reply_content(text)
        usage["input_tokens"] += used["input_tokens"]
        usage["output_tokens"] += used["output_tokens"]
        try:
            if finish == "length":
                raise _Truncated()
            parsed = _parse_json(reply)
        except _Truncated:
            # Whatever a cut-off reply holds is never read: a draft
            # inside unfinished thinking must not become the answer.
            failure, failure_kind = (
                "The model's reply was cut off before it finished.",
                "truncated",
            )
            max_tokens = (max_tokens or 1024) * TRUNCATION_GROWTH
            continue
        if parsed is None:
            failure, failure_kind = "The model's reply was not JSON.", "invalid"
            continue
        problems = validate(parsed, schema)
        if problems:
            failure, failure_kind = (
                f"The model's reply did not match the schema ({problems[0]}).",
                "invalid",
            )
            continue
        parsed["_usage"] = usage
        parsed["_model"] = cfg.model
        return parsed
    raise EndpointError(f"{failure} No valid reply after one retry.", kind=failure_kind)


# -- smoke test ---------------------------------------------------------------


def main(argv):
    """python3 endpoint.py --smoke: one short words-only call, routed the
    way every check is, printing which model answered and how long it
    took. No photo is sent. Exits 1 when no model answered."""
    if argv != ["--smoke"]:
        print("usage: python3 endpoint.py --smoke")
        return 2
    where = describe_from_env()
    if where["config_error"]:
        print(f"Setup: {where['config_error']}")
    line = f"Asking {where['model']} at {where['base_url']}"
    if where["fallback_model"]:
        line += f" (fallback {where['fallback_model']} at {where['fallback_base_url']})"
    print(line)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    started = time.monotonic()
    try:
        reply = ask_json(
            "You are a smoke test. Answer in one word.",
            [text_part("What color is a clear daytime sky?")],
            schema, "smoke", max_tokens=1024,
        )
    except EndpointError as e:
        print(f"FAILED ({e.model or 'no model'}): {e}")
        return 1
    seconds = time.monotonic() - started
    print(f"OK: {reply['_model']} answered {reply['answer']!r} in {seconds:.1f} s")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
