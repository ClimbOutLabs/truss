#!/usr/bin/env python3
"""Spike: can the Truss server use Aman as its checker for now?

Run this on the Mac where Aman and Truss both live. It changes nothing
in either repo. It answers four questions, in order:

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
in AMAN_COOKIE instead. Both are read from the environment only and
are never printed.

Paste the whole output back to whoever asked you to run this.
Verdict lines start with SPIKE.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("AMAN_BASE", "http://127.0.0.1:8791")
EXTERNAL_ID = "truss-spike"

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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, text, int((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        return e.code, text, int((time.monotonic() - started) * 1000)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", int((time.monotonic() - started) * 1000)


def short(text, limit=600):
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " ...[cut]"


def probe():
    ok = True
    status, text, ms = call("GET", "/")
    print(f"SPIKE reach: GET / -> {status} in {ms} ms")
    if status is None:
        print(f"SPIKE reach: FAILED, Aman did not answer at {BASE}: {short(text)}")
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
    data = base64.b64encode(photo_path.read_bytes()).decode("ascii")
    parts = [
        {"type": "text", "text": f"Question: {question}{QUESTION_SUFFIX}"},
        {"type": "file", "name": photo_path.name,
         "data": f"data:image/jpeg;base64,{data}"},
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
