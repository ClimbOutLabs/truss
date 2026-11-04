#!/usr/bin/env python3
"""
"Hold on, let me check": a quick look beside the full check.

Off unless TRUSS_HOLD_ON=1, and only for the phone app built with it
(the app must know to fetch the answer afterwards).

When a photo arrives with questions due, the full check starts as
always. Beside it, a quick gate asks Muse Spark one tiny question at
minimal effort: is the thing one of these questions is about, or the
act it asks about, clearly in view right now? If the gate says yes
before the full check is done, the capture is answered at once with
"Hold on, let me check." and a pending id, so the wearer hears it and
can lower their hand. The phone then fetches GET /capture/result?id=
and speaks the full check's lines when they are ready.

The gate never gives a verdict. Nothing it says is logged as an
answer, counted as watching, or spoken except the one fixed line; a
gate failure, a gate no, or a gate slower than the full check changes
nothing. The full check runs exactly as before, under the same lock.

A result the phone never collects is not lost: after COLLECT_S
seconds its lines go to the spoken-alert queue the phone polls.
"""

import os
import threading
import time
import uuid

import endpoint

HOLD_LINE = "Hold on, let me check."
# Said after a hold-on when the full check found nothing to say.
NOTHING_LINE = "I couldn't confirm it that time."
# How long the gate may take before it is no longer worth waiting for.
GATE_LIMIT_S = 8.0
# After a hold-on is spoken, the next one waits this long, so a hand
# held in view does not hear it on every photo.
HOLD_REST_S = 60.0
# How long a finished result waits to be collected before its lines
# go to the alert queue instead.
COLLECT_S = 45.0
# How long GET /capture/result waits for the full check.
RESULT_WAIT_S = 280.0

GATE_SCHEMA = {
    "type": "object",
    "properties": {"in_view": {"type": "boolean"}},
    "required": ["in_view"],
    "additionalProperties": False,
}

GATE_SYSTEM = (
    "You look at a photo from smart glasses the wearer has on, at home. "
    "You are given one or more questions the wearer asked to be watched. "
    "Do not answer them. Say only whether the thing one of them is about, "
    "or the act it asks about, is clearly in view in this photo, close "
    "enough to check right now: in_view true or false."
)


def enabled():
    return (os.environ.get("TRUSS_HOLD_ON") or "").strip() in ("1", "true", "yes", "on")


def gate_effort():
    raw = (os.environ.get("TRUSS_GATE_EFFORT") or "").strip().lower()
    return raw if raw in endpoint.REASONING_EFFORTS else "minimal"


def gate(frame_path, questions):
    """True when the quick look says one of the questions' things is in
    view; False on a no or any failure. Never raises."""
    try:
        lines = "\n".join(f"- {q}" for q in questions if q)
        content = [
            endpoint.text_part(f"Questions (do not answer them):\n{lines}"),
            endpoint.image_part(frame_path),
        ]
        reply = endpoint.ask_json(
            GATE_SYSTEM, content, GATE_SCHEMA, "gate", max_tokens=2048, effort=gate_effort()
        )
        return reply.get("in_view") is True
    except Exception:
        return False


class Pending:
    """Full-check results waiting for the phone, by id."""

    def __init__(self, say_later):
        # say_later(lines) queues lines for the phone's alert poll.
        self._say_later = say_later
        self._lock = threading.Lock()
        self._items = {}
        self._last_hold = 0.0

    def may_hold(self):
        with self._lock:
            return time.monotonic() - self._last_hold >= HOLD_REST_S

    def held(self):
        with self._lock:
            self._last_hold = time.monotonic()

    def open(self):
        pid = uuid.uuid4().hex[:12]
        with self._lock:
            self._items[pid] = {"done": threading.Event(), "result": None, "taken": False}
        return pid

    def finish(self, pid, result):
        with self._lock:
            item = self._items.get(pid)
        if item is None:
            return
        item["result"] = result
        item["done"].set()
        timer = threading.Timer(COLLECT_S, self._uncollected, args=(pid,))
        timer.daemon = True
        timer.start()

    def take(self, pid, wait_s=RESULT_WAIT_S):
        """The result for pid once ready, or None for an unknown id or
        one not ready in time."""
        with self._lock:
            item = self._items.get(pid)
        if item is None or not item["done"].wait(wait_s):
            return None
        with self._lock:
            if item["taken"]:
                return None
            item["taken"] = True
            self._items.pop(pid, None)
        return item["result"]

    def _uncollected(self, pid):
        with self._lock:
            item = self._items.pop(pid, None)
            if item is None or item["taken"]:
                return
            item["taken"] = True
        lines = (item["result"] or {}).get("speak") or []
        if lines:
            print(f"  hold-on: answer {pid} was not collected; it goes to the alert queue.", flush=True)
            self._say_later(lines)
