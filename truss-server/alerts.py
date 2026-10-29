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
from datetime import datetime, time, timedelta
from pathlib import Path

from questions import _window_times

# Until this time of day, the sweep also settles yesterday's windows.
# A window that ends near midnight can close with a check still in
# flight, or with no sweep landing before the day turns; its miss is
# then raised just after midnight instead of never. Later than this,
# yesterday is left alone: a laptop that was off overnight does not
# bring up old misses in the morning.
SETTLE_YESTERDAY_UNTIL = time(1, 0)


def _valid_alert(alert):
    """Whether a stored record is a whole alert: an id, a question
    slug, a date, and the line to speak, all text. A valid-JSON file
    in any other shape is not an alert and is skipped everywhere,
    so it can never raise, block a listing, or stand in for one."""
    if not isinstance(alert, dict):
        return False
    return all(
        isinstance(alert.get(field), str) and alert.get(field)
        for field in ("id", "slug", "date", "line")
    )


class AlertStore:
    def __init__(self, root):
        self.root = Path(root)
        self.disabled = False
        self._lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.root.is_dir():
                raise OSError("the alerts path is not a folder")
        except Exception:
            # A server that cannot open its alerts folder still
            # starts and does everything else; alerts degrade to a
            # disabled store that holds nothing and clears nothing.
            self.disabled = True
            print(
                "Warning: missed-window alerts are off because the "
                "alerts folder could not be opened."
            )

    def _path(self, alert_id):
        return self.root / f"{alert_id}.json"

    def _read(self, path):
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _write_to(self, path, alert):
        # Write to a side file and move it into place, so a reader in
        # the server's other thread never sees a half-written alert.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(alert, indent=2) + "\n")
        tmp.replace(path)

    def _write(self, alert):
        self._write_to(self._path(alert["id"]), alert)

    def pending(self):
        """Alerts still waiting to be spoken, oldest first. A file
        that cannot be read, or that is not a whole alert, is
        skipped, never fatal."""
        if self.disabled:
            return []
        out = []
        for path in sorted(self.root.glob("*.json")):
            alert = self._read(path)
            if _valid_alert(alert) and not alert.get("spoken"):
                out.append(alert)
        out.sort(key=lambda a: (a["date"], a["id"]))
        return out

    def exists_for(self, slug, date):
        """Whether a whole alert for this question and day exists,
        spoken or not. An acknowledged alert still counts: the sweep
        must never raise the same miss twice."""
        if self.disabled:
            return False
        for path in self.root.glob("*.json"):
            alert = self._read(path)
            if _valid_alert(alert) and alert["slug"] == slug and alert["date"] == date:
                return True
        return False

    def add(self, alert):
        if self.disabled:
            return
        with self._lock:
            self._write(alert)

    def ack(self, alert_id):
        """Mark one alert as spoken. Returns True when a pending alert
        was cleared, False for anything else. This is the only way
        an alert ever clears.

        The id is never used as a path. The store's own files are
        searched for a whole alert whose stored id matches, and only
        that file, inside the alerts folder, is written back."""
        if self.disabled:
            return False
        if not isinstance(alert_id, str) or not alert_id:
            return False
        if "/" in alert_id or "\\" in alert_id:
            return False
        with self._lock:
            for path in sorted(self.root.glob("*.json")):
                alert = self._read(path)
                if not _valid_alert(alert):
                    continue
                if alert["id"] != alert_id or alert.get("spoken"):
                    continue
                alert["spoken"] = True
                self._write_to(path, alert)
                return True
        return False


def miss_line(entry, until, yesterday=False):
    """The spoken line for a missed window: plain, factual, and it
    reads sensibly whatever words the question itself uses."""
    until_text = until.strftime("%I:%M %p").lstrip("0")
    if yesterday:
        return (
            f"Yesterday's window closed at {until_text}. "
            f"This was not done yesterday: {entry['question']}"
        )
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
    if getattr(alerts, "disabled", False):
        return []
    try:
        entries = store.list()
    except Exception:
        # The store cannot be read at all; that is the check loop's
        # day-1 fallback case, and the sweep simply raises nothing.
        return []
    days = [now.date()]
    if now.time() < SETTLE_YESTERDAY_UNTIL:
        days.insert(0, now.date() - timedelta(days=1))
    created = []
    for entry in entries:
        for day in days:
            try:
                alert = _sweep_one(store, alerts, entry, now, day)
            except Exception:
                continue
            if alert is not None:
                created.append(alert)
    return created


def _seen_on(store, slug, day):
    """Whether a done question was seen done on the given day, read
    fresh from its record. The answered mark is written together with
    the witness and is never cleared, so it still tells after
    midnight, when yesterday's witness may already have been cleared.
    A record that cannot be read counts as seen: damage degrades to
    silence, never to an accusation."""
    entry = store.get(slug)
    if entry is None:
        return True
    witness = entry.get("witness")
    stamps = [witness["at"]] if witness else []
    if entry.get("answered_at"):
        stamps.append(entry["answered_at"])
    for stamp in stamps:
        try:
            if datetime.fromisoformat(stamp).date() == day:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _sweep_one(store, alerts, entry, now, day):
    if entry.get("kind") != "done":
        return None
    if entry.get("state") in ("planting", "retired"):
        return None
    pattern = entry.get("pattern")
    times = _window_times(pattern)
    if times is None:
        # The same window-validity rule as the store's due checks:
        # a window that does not end after it starts never raises
        # a miss, on any day.
        return None
    after, until = times
    if day != now.date() and now.time() >= after:
        # Today's window has opened, so a firing today may already
        # have replaced yesterday's answered mark and witness on the
        # record. Yesterday can no longer be read truthfully, and it
        # is left alone rather than risk calling a seen act missed.
        return None
    today = day.isoformat()
    planted = entry.get("planted_at")
    if isinstance(planted, str):
        try:
            planted_dt = datetime.fromisoformat(planted)
        except ValueError:
            planted_dt = None
        if planted_dt is not None:
            if planted_dt.date() > day:
                return None
            if planted_dt.date() == day and planted_dt.time() >= until:
                # Planted after today's window had already closed:
                # the miss happened before the question existed.
                return None
    if day == now.date() and now.time() < until:
        return None
    if store.check_in_flight(entry["slug"]):
        # A check that may witness the act is still running. The next
        # sweep decides, once its witness is written or not.
        return None
    if _seen_on(store, entry["slug"], day):
        return None
    if alerts.exists_for(entry["slug"], today):
        return None
    alert = {
        "id": f"{today}-{entry['slug']}",
        "slug": entry["slug"],
        "question": entry["question"],
        "date": today,
        "until": pattern.get("until"),
        "line": miss_line(entry, until, yesterday=day != now.date()),
        "spoken": False,
        "created_at": now.isoformat(timespec="seconds"),
    }
    alerts.add(alert)
    return alert
