#!/usr/bin/env python3
"""Spike: can the Truss server use Aman as its checker for now?

Run this on the Mac where Aman and Truss both live. It changes no repo
and no code. Probe mode only reads; turn mode posts one real turn into
the chat you choose, and that turn stays in that chat, so use a
throwaway chat. It answers four questions, in order:

  1. Is Aman reachable at http://127.0.0.1:8791 from a plain script?
  2. What auth does a script need (none, a bearer token, a cookie)?
  3. Can a script get a session and post one turn carrying a photo and
     one standing question, worded to demand JSON only?
  4. Does the reply come back as a parseable JSON verdict, with no
     approval cards and no tool calls, and how long did it take?

Usage:
  python3 aman_truss_spike.py probe
  python3 aman_truss_spike.py turn --session-id <id> --photo stove.jpg \
      --question "Is the stove off?"

If you have a bearer token, put it in AMAN_TOKEN in the environment.
If your Aman uses the sign-in profile, a browser cookie header can go
in AMAN_COOKIE instead. Both are read from the environment only. The
script redacts their exact values from anything it prints, and it does
not follow redirects, so the headers can only ever reach the Aman
address in AMAN_BASE.

The output itself carries live details from your Aman: session ids,
chat titles, and model replies, which can describe your home. Paste
the whole output back only to a place you trust with that.
Verdict lines start with SPIKE.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("AMAN_BASE", "http://127.0.0.1:8791")
EXTERNAL_ID = "truss-spike"


def _secret_values():
    """The credential values in play, for redaction. Read fresh each
    time so nothing stale is kept."""
    values = []
    for name in ("AMAN_TOKEN", "AMAN_COOKIE"):
        value = os.environ.get(name)
        if value:
            values.append(value)
    return values


def _redact(text):
    """Remove credential values and any password in the Aman address
    from a string before it can be printed."""
    out = text
    for value in _secret_values():
        out = out.replace(value, "[redacted]")
    parsed = urllib.parse.urlparse(BASE)
    if parsed.password:
        out = out.replace(parsed.password, "[redacted]")
    if parsed.username:
        out = out.replace(parsed.username, "[redacted]")
    return out


def _safe_base():
    """The Aman address with any userinfo taken out, for printing."""
    parsed = urllib.parse.urlparse(BASE)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        parsed = parsed._replace(netloc=host)
        return urllib.parse.urlunparse(parsed)
    return BASE


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. Following one would re-send the auth
    headers to whatever address the redirect names, possibly a
    different origin. A 3xx comes back as a plain HTTP error instead,
    and the spike reports it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

QUESTION_SUFFIX = (
    " Reply with JSON only, in exactly this shape: "
    '{"verdict": "yes", "reason": "<one sentence>"} '
    "where verdict is yes, no, or cannot_tell. Say cannot_tell when "
    "the detail that would decide the answer is not visible in the "
    "photo. Never guess. Do not use any tools."
)


def headers():
    h = {"Content-Type": "application/json"}
    token = os.environ.get("AMAN_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    cookie = os.environ.get("AMAN_COOKIE")
    if cookie:
        h["Cookie"] = cookie
    return h


def call(method, path, body=None, timeout=30):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers(), method=method)
    started = time.monotonic()
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, _redact(text), int((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        return e.code, _redact(text), int((time.monotonic() - started) * 1000)
    except Exception as e:
        note = _redact(f"{type(e).__name__}: {e}")
        return None, note, int((time.monotonic() - started) * 1000)


def short(text, limit=600):
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " ...[cut]"


def probe():
    ok = True
    status, text, ms = call("GET", "/")
    print(f"SPIKE reach: GET / -> {status} in {ms} ms")
    if status is None:
        print(f"SPIKE reach: FAILED, Aman did not answer at {_safe_base()}: {short(text)}")
        print("SPIKE next: start Aman on this Mac, then run the probe again.")
        return 1
    status, text, ms = call("GET", "/api/v1/sessions?limit=5")
    print(f"SPIKE auth: GET /api/v1/sessions -> {status} in {ms} ms")
    print(f"SPIKE auth body: {short(text)}")
    if status == 401 or status == 403:
        print("SPIKE auth: a script needs a token or cookie. Set AMAN_TOKEN "
              "or AMAN_COOKIE in the environment and run the probe again.")
        ok = False
    if status == 200:
        try:
            data = json.loads(text).get("data", [])
            print(f"SPIKE sessions: {len(data)} listed")
            for s in data:
                print(f"SPIKE session: id={s.get('id')} agent={s.get('agent')} "
                      f"title={s.get('title')!r}")
        except Exception:
            print("SPIKE sessions: the list did not parse as JSON session data.")
    agent_name = os.environ.get("AMAN_AGENT_NAME")
    if agent_name:
        body = {"external_id": EXTERNAL_ID, "agent": {"name": agent_name}}
        status, text, ms = call(
            "POST", "/api/internal/sessions/get-or-create-by-external-id", body)
        print(f"SPIKE get-or-create: -> {status} in {ms} ms")
        print(f"SPIKE get-or-create body: {short(text)}")
    else:
        print("SPIKE get-or-create: skipped. Set AMAN_AGENT_NAME to the "
              "registry name of the agent a Truss check chat should bind "
              "to, or use the turn mode with an existing session id.")
    print("SPIKE probe done." if ok else "SPIKE probe done, auth still open.")
    return 0


def turn(session_id, photo, question):
    photo_path = Path(photo)
    try:
        raw = photo_path.read_bytes()
    except OSError as e:
        print(f"SPIKE turn: FAILED, could not read the photo: {e}")
        return 1
    data = base64.b64encode(raw).decode("ascii")
    mime = "image/png" if photo_path.suffix.lower() == ".png" else "image/jpeg"
    parts = [
        {"type": "text", "text": f"Question: {question}{QUESTION_SUFFIX}"},
        {"type": "file", "name": photo_path.name,
         "data": f"data:{mime};base64,{data}"},
    ]
    body = {
        "input": [{"type": "user.message", "content": parts}],
        "previous_turn_id": "none",
        "stream": False,
        "client_timezone": os.environ.get("SPIKE_TIMEZONE", "UTC"),
    }
    status, text, ms = call(
        "POST", f"/api/v1/sessions/{session_id}/turns", body, timeout=60)
    print(f"SPIKE turn create: -> {status} in {ms} ms")
    if status != 200:
        print(f"SPIKE turn create body: {short(text, 1200)}")
        return 1
    turn_id = json.loads(text)["data"]["id"]
    print(f"SPIKE turn id: {turn_id}")
    deadline = time.monotonic() + 240
    final = None
    while time.monotonic() < deadline:
        status, text, ms = call(
            "GET", f"/api/v1/sessions/{session_id}/turns/{turn_id}")
        if status != 200:
            print(f"SPIKE turn poll: -> {status}: {short(text)}")
            return 1
        state = json.loads(text)["data"]["state"]
        print(f"SPIKE turn state: {state.get('status')}")
        if state.get("status") != "running":
            final = state
            break
        time.sleep(3)
    if final is None:
        print("SPIKE verdict: FAILED, the turn was still running after 240 s. "
              "That latency kills the Aman checker idea on its own.")
        return 1
    if final.get("status") != "done":
        print(f"SPIKE verdict: FAILED, turn ended as {final.get('status')}: "
              f"{short(json.dumps(final), 800)}")
        return 1
    actions = final.get("required_actions") or []
    print(f"SPIKE required actions: {len(actions)} "
          f"{[a.get('type') for a in actions]}")
    output = final.get("output") or {}
    content = output.get("content")
    print(f"SPIKE output: {short(json.dumps(content), 1200)}")
    metrics = final.get("metrics") or {}
    print(f"SPIKE metrics: {json.dumps(metrics)}")
    text_out = ""
    if isinstance(content, str):
        text_out = content
    elif isinstance(content, list):
        text_out = " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text")
    try:
        parsed = json.loads(text_out)
        verdict = parsed.get("verdict")
        if verdict in ("yes", "no", "cannot_tell"):
            print(f"SPIKE verdict: PASS, parsed verdict={verdict} "
                  f"reason={parsed.get('reason')!r}")
            if actions:
                print("SPIKE verdict: but the turn raised required actions, "
                      "so a check could stall on a card. That must be "
                      "designed out before building.")
            return 0
        print(f"SPIKE verdict: FAILED, JSON parsed but verdict is {verdict!r}.")
        return 1
    except Exception:
        print("SPIKE verdict: FAILED, the reply was not JSON in the asked "
              "shape. Aman checker would need a different turn contract.")
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("probe")
    t = sub.add_parser("turn")
    t.add_argument("--session-id", required=True)
    t.add_argument("--photo", required=True)
    t.add_argument("--question", required=True)
    args = parser.parse_args()
    if args.mode == "probe":
        return probe()
    return turn(args.session_id, args.photo, args.question)


if __name__ == "__main__":
    sys.exit(main())
