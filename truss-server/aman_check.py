#!/usr/bin/env python3
"""
Checking with Aman, for now.

While the Meta model key is awaited, the Truss server can ask the
owner's own Aman instance to make its checks. A check is one full
Aman turn: the frame (and the question's reference looks, when it has
them) is posted as a turn into a dedicated Aman chat whose model can
read images, worded to demand a JSON verdict, and the turn's answer
is read back as the check result.

This is a bridge, not a Meta check. The result shape matches the
canonical checker in eval/checker.py so the check loop and the guide
cannot tell the two apart. Usage is reported as zeros on purpose:
Aman's own meter counts the tokens on its side.

Setup comes from the environment only: AMAN_BASE (default
http://127.0.0.1:8791), AMAN_TOKEN (the browser sign-in token),
AMAN_CHECK_SESSION_ID (the check chat), and AMAN_CHECK_TIMEOUT_S
(default 120).

A failed check must never read as a verdict. Missing setup, an
unreachable Aman, an HTTP error, a turn that ends without an answer,
a turn that pauses for an approval (a check never answers a card),
a timeout, or an answer that cannot be read all come back as
cannot_tell with a plain reason. This module never raises for those,
and never prints: the token value appears in no reason, no log line,
and no exception text. Requests are made with a no-redirect opener,
so the sign-in header can only ever reach AMAN_BASE's origin.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ZERO_USAGE = {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0}

VERDICTS = ("yes", "no", "cannot_tell")

# The JSON-only instruction from the proven spike script, word for word.
QUESTION_SUFFIX = (
    " Reply with JSON only, in exactly this shape: "
    '{"verdict": "yes", "reason": "<one sentence>"} '
    "where verdict is yes, no, or cannot_tell. Say cannot_tell when "
    "the detail that would decide the answer is not visible in the "
    "photo. Never guess. Do not use any tools."
)

# Planted checks carry the wearer's own reference looks, mirroring the
# planted prompt in eval/checker.py.
PLANTED_INSTRUCTION = (
    " You are also given two reference photos taken earlier in the "
    "wearer's own home: the first reference shows what a yes answer "
    "looks like there, the second shows what a no answer looks like "
    "there. Compare the new photo against these two references "
    "instead of a generic idea of the scene."
)

POLL_SECONDS = 2


class _NoCheck(Exception):
    """A check that could not be made. The message is the plain reason
    the cannot_tell result carries."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. Following one would re-send the sign-in
    header to whatever address the redirect names, possibly a
    different origin. A 3xx comes back as a plain HTTP error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _reason_text(value):
    """A verdict's reason, as text, in one place. A model can return a
    reason that is not a string at all; words and numbers are coerced,
    anything else counts as no reason, rather than breaking the
    verdict it came with."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _timeout_seconds():
    raw = os.environ.get("AMAN_CHECK_TIMEOUT_S", "120")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 120.0
    return value if value > 0 else 120.0


def _config():
    missing = []
    token = os.environ.get("AMAN_TOKEN") or ""
    session_id = os.environ.get("AMAN_CHECK_SESSION_ID") or ""
    if not token:
        missing.append("AMAN_TOKEN")
    if not session_id:
        missing.append("AMAN_CHECK_SESSION_ID")
    if missing:
        names = " and ".join(missing)
        verb = "are" if len(missing) > 1 else "is"
        raise _NoCheck(
            f"Aman checking is not set up: {names} {verb} not set, "
            "so no check was made."
        )
    base = os.environ.get("AMAN_BASE", "http://127.0.0.1:8791").rstrip("/")
    return base, token, session_id, _timeout_seconds()


def _mime(path):
    return "image/png" if Path(path).suffix.lower() == ".png" else "image/jpeg"


def _file_part(path, what):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise _NoCheck(f"{what} could not be read, so no check was made.")
    data = base64.b64encode(raw).decode("ascii")
    return {
        "type": "file",
        "name": Path(path).name,
        "data": f"data:{_mime(path)};base64,{data}",
    }


def _turn_body(frame_path, question, ref_yes, ref_no):
    planted = ref_yes is not None and ref_no is not None
    text = f"Question: {question}"
    if planted:
        text += PLANTED_INSTRUCTION
    text += QUESTION_SUFFIX
    parts = [{"type": "text", "text": text}]
    parts.append(_file_part(frame_path, "The photo"))
    if planted:
        parts.append(
            {"type": "text", "text": "Reference photo: what a yes answer looks like."}
        )
        parts.append(_file_part(ref_yes, "A reference photo"))
        parts.append(
            {"type": "text", "text": "Reference photo: what a no answer looks like."}
        )
        parts.append(_file_part(ref_no, "A reference photo"))
    return {
        "input": [{"type": "user.message", "content": parts}],
        "previous_turn_id": "none",
        "stream": False,
    }


def _status_reason(status):
    if status in (401, 403):
        return (
            "Aman refused the sign-in token, so no check was made. "
            "The token may have expired."
        )
    return f"Aman answered with an error (status {status}), so no check was made."


def _request(method, url, token, timeout, body=None):
    """One HTTP call. Returns (status, text). HTTP errors come back as
    their status; anything at the network level is a _NoCheck. The
    token rides in the header only and is never written anywhere."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method=method,
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
    except Exception:
        raise _NoCheck("Aman did not answer, so no check was made.")


def _read_turn_id(text):
    try:
        return json.loads(text)["data"]["id"]
    except Exception:
        raise _NoCheck("Aman's reply could not be read, so no check was made.")


def _read_state(text):
    try:
        state = json.loads(text)["data"]["state"]
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        return state
    except Exception:
        raise _NoCheck("Aman's reply could not be read, so no check was made.")


def _content_text(content):
    """The model's answer text from a finished turn's output content:
    a plain string, or the text parts of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return " ".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def _parse_verdict(text):
    """(verdict, reason) from the model's answer, or None when the
    answer cannot be read as a verdict. A bare JSON object is tried
    first, then the first {...} span, for answers wrapped in prose."""
    text = (text or "").strip()
    if not text:
        return None
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        return None
    verdict = parsed.get("verdict")
    if verdict not in VERDICTS:
        return None
    return verdict, _reason_text(parsed.get("reason"))


def _run(frame_path, question, ref_yes, ref_no):
    base, token, session_id, timeout_s = _config()
    body = _turn_body(frame_path, question, ref_yes, ref_no)
    deadline = time.monotonic() + timeout_s
    turns_url = f"{base}/api/v1/sessions/{session_id}/turns"

    status, text = _request(
        "POST", turns_url, token, deadline - time.monotonic(), body
    )
    if status != 200:
        raise _NoCheck(_status_reason(status))
    turn_id = _read_turn_id(text)
    turn_url = f"{turns_url}/{turn_id}"

    final = None
    while final is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _NoCheck(
                "Aman did not finish the check in time, so no check was made."
            )
        status, text = _request("GET", turn_url, token, remaining)
        if status != 200:
            raise _NoCheck(_status_reason(status))
        state = _read_state(text)
        if state.get("status") == "running":
            time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            continue
        final = state

    if final.get("status") != "done":
        raise _NoCheck("The Aman turn ended without an answer, so no check was made.")
    if final.get("required_actions"):
        raise _NoCheck(
            "Aman paused for an approval, which a check never answers, "
            "so no check was made."
        )
    output = final.get("output")
    content = output.get("content") if isinstance(output, dict) else None
    parsed = _parse_verdict(_content_text(content))
    if parsed is None:
        raise _NoCheck(
            "Aman's answer could not be read as a verdict, so no check was made."
        )
    return parsed


def check(frame_path, question, ref_yes=None, ref_no=None):
    """Ask Aman one visual question about one photo.

    Returns the canonical checker shape (see eval/checker.py). Every
    failure is a cannot_tell with a plain reason; this never raises.
    """
    started = time.monotonic()
    try:
        verdict, reason = _run(frame_path, question, ref_yes, ref_no)
    except _NoCheck as e:
        verdict, reason = "cannot_tell", str(e)
    except Exception:
        verdict, reason = (
            "cannot_tell",
            "The Aman check failed, so no check was made.",
        )
    return {
        "verdict": verdict,
        "reason": reason,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "usage": dict(ZERO_USAGE),
    }
