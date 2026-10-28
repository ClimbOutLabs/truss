#!/usr/bin/env python3
"""
Missed-window alerts for done questions.

When a done question's window closes and no witness was recorded that
day, that is not a note in a file: it is a message that has to reach
the wearer and be spoken. An alert is a plain file under the alerts
folder, one per question per day:

    <date>-<slug>.json   the question, the window's end, and the line
                         to speak, plus a "spoken" flag

An alert stays pending until the phone confirms it was spoken (the
ack). Nothing else clears it: not time passing, not the act being
done late. The sweep that creates alerts is idempotent, so it can run
on a timer and at the start of every capture without ever raising a
second alert for the same question and day.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

from questions import _parse_time


class AlertStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, alert_id):
        return self.root / f"{alert_id}.json"

    def _read(self, path):
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _write(self, alert):
        # Write to a side file and move it into place, so a reader in
        # the server's other thread never sees a half-written alert.
        tmp = self._path(alert["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(alert, indent=2) + "\n")
        tmp.replace(self._path(alert["id"]))

    def pending(self):
        """Alerts still waiting to be spoken, oldest first. A file
        that cannot be read is skipped, never fatal."""
        out = []
        for path in sorted(self.root.glob("*.json")):
            alert = self._read(path)
            if alert is not None and not alert.get("spoken"):
                out.append(alert)
        out.sort(key=lambda a: (a.get("date", ""), a.get("id", "")))
        return out

    def exists_for(self, slug, date):
        """Whether an alert for this question and day exists, spoken
        or not. An acknowledged alert still counts: the sweep must
        never raise the same miss twice."""
        for path in self.root.glob("*.json"):
            alert = self._read(path)
            if alert and alert.get("slug") == slug and alert.get("date") == date:
                return True
        return False

    def add(self, alert):
        with self._lock:
            self._write(alert)

    def ack(self, alert_id):
        """Mark one alert as spoken. Returns True when a pending alert
        with that id was cleared, False for an unknown or already
        spoken id. This is the only way an alert ever clears."""
        if not isinstance(alert_id, str) or not alert_id:
            return False
        with self._lock:
            path = self._path(alert_id)
            if not path.exists():
                return False
            alert = self._read(path)
            if alert is None or alert.get("spoken"):
                return False
            alert["spoken"] = True
            self._write(alert)
            return True


def miss_line(entry, until):
    """The spoken line for a missed window: plain, factual, and it
    reads sensibly whatever words the question itself uses."""
    until_text = until.strftime("%I:%M %p").lstrip("0")
    return (
        f"It is past {until_text}. "
        f"This has not been done today: {entry['question']}"
    )


def sweep_missed(store, alerts, now=None):
    """Create one pending alert for each done question whose window
    has closed today with no witness. Returns the alerts created.

    Safe to run as often as anyone likes: a question and day that
    already have an alert are skipped, and a record that cannot be
    read is skipped with them. Never raises.
    """
    now = now or datetime.now()
    try:
        entries = store.list()
    except Exception:
        # The store cannot be read at all; that is the check loop's
        # day-1 fallback case, and the sweep simply raises nothing.
        return []
    created = []
    for entry in entries:
        try:
            alert = _sweep_one(store, alerts, entry, now)
        except Exception:
            continue
        if alert is not None:
            created.append(alert)
    return created


def _sweep_one(store, alerts, entry, now):
    if entry.get("kind") != "done":
        return None
    if entry.get("state") in ("planting", "retired"):
        return None
    pattern = entry.get("pattern")
    if not isinstance(pattern, dict) or pattern.get("kind") != "window":
        return None
    until = _parse_time(pattern.get("until"))
    if until is None:
        return None
    today = now.date().isoformat()
    planted = entry.get("planted_at")
    if isinstance(planted, str):
        try:
            planted_dt = datetime.fromisoformat(planted)
        except ValueError:
            planted_dt = None
        if planted_dt is not None:
            if planted_dt.date() > now.date():
                return None
            if planted_dt.date() == now.date() and planted_dt.time() >= until:
                # Planted after today's window had already closed:
                # the miss happened before the question existed.
                return None
    if now.time() < until:
        return None
    if store.witness_today(entry["slug"], now=now) is not None:
        return None
    if alerts.exists_for(entry["slug"], today):
        return None
    alert = {
        "id": f"{today}-{entry['slug']}",
        "slug": entry["slug"],
        "question": entry["question"],
        "date": today,
        "until": pattern.get("until"),
        "line": miss_line(entry, until),
        "spoken": False,
        "created_at": now.isoformat(timespec="seconds"),
    }
    alerts.add(alert)
    return alert
