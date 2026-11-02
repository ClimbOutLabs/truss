#!/usr/bin/env python3
"""
Starting and stopping the phone's watching from the Desk.

Watching (a glasses photo every few seconds) runs in the phone app, so
the Desk cannot start it by itself. Instead the Desk leaves a request
here, and the phone picks it up on its next check-in:

    POST /watch         the Desk asks: {"on": true} or {"on": false}
    POST /watch/phone   the phone checks in with how it stands and gets
                        the latest request back
    GET  /watch         the Desk reads the request and the phone's state

A request carries a fresh random id. The phone acts on an id it has not
acted on before, once, so pressing Stop on the phone after the Desk
asked for watching is not overruled on the next check-in, and a server
restart cannot make an old id look new. A request older than
FRESH_SECONDS is not acted on by a phone that only now opens the app.

Everything here is in memory: a restart forgets the request, which is
the safe side (the phone just keeps doing what it does).
"""

import threading
import uuid
from datetime import datetime

# How long a Desk request stays worth acting on.
FRESH_SECONDS = 600
# A phone not heard from for this long is shown as not reachable.
PHONE_QUIET_SECONDS = 30


def _clean_every(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 3600 else None


class WatchDesk:
    def __init__(self, clock=None):
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()
        self._request = None  # {"id", "on", "at"}
        self._phone = None  # {"watching", "away", "glasses", "pending", "every", "at", "from"}

    def _request_view(self, now):
        if self._request is None:
            return None
        age = int((now - self._request["at"]).total_seconds())
        return {
            "id": self._request["id"],
            "on": self._request["on"],
            "at": self._request["at"].isoformat(timespec="seconds"),
            "age_seconds": max(age, 0),
        }

    def ask(self, on):
        """The Desk asks the phone to start (True) or stop (False)."""
        if not isinstance(on, bool):
            raise ValueError('Body must be JSON like {"on": true}.')
        now = self._clock()
        with self._lock:
            self._request = {"id": uuid.uuid4().hex, "on": on, "at": now}
            return self._view(now)

    def report(self, data, address):
        """The phone checks in. Returns what it should know: the
        latest request, or None."""
        if not isinstance(data, dict):
            raise ValueError("Body must be a JSON object.")
        now = self._clock()
        with self._lock:
            self._phone = {
                "watching": data.get("watching") is True,
                "away": data.get("away") is True,
                "glasses": data.get("glasses") is True,
                "pending": data.get("pending") is True,
                "every": _clean_every(data.get("every")),
                "at": now,
                "from": address,
            }
            return {"request": self._request_view(now), "fresh_seconds": FRESH_SECONDS}

    def view(self):
        now = self._clock()
        with self._lock:
            return self._view(now)

    def _view(self, now):
        phone = None
        if self._phone is not None:
            ago = max(int((now - self._phone["at"]).total_seconds()), 0)
            phone = {k: v for k, v in self._phone.items() if k != "at"}
            phone["seconds_ago"] = ago
            phone["reachable"] = ago <= PHONE_QUIET_SECONDS
        return {"request": self._request_view(now), "phone": phone}
