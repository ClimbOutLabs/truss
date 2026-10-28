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

What an alert says is held to two rules.

It never says more than the loop knows. A missed witness is not a
missed act: she may have done it with the glasses off. So the line
never says the thing was not done. It says what was watched, read
from frame arrivals (see coverage.py): the whole window, the spans
the glasses were with her, or none of it.

For a safety question (the stove, the pills) it directs her to look
and never tells her to act. "You didn't take your pills" after a
missed witness is how the product would cause the double dose it
exists to prevent. The line sends her to the thing itself, and the
sweep leaves a state check owed: the next frame that shows the thing
is checked as it is now, and that result is spoken (see checkloop.py).

Every sentence an alert can speak is in MISS_TEMPLATES below, and
test_safety.py scans all of them for action verbs.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

from coverage import DEFAULT_GAP_MINUTES, coverage
from questions import _window_times


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


# Every sentence a missed-window alert can speak. Placeholders are
# filled with times, the question's own words, and the look-at
# phrase; nothing else is ever added to a line. The safety direction
# is the only instruction any alert gives, and it is to look.
MISS_TEMPLATES = {
    "opening": "It is past {until}. About your question: {question}",
    "complete": "I watched the whole window, from {start} to {end}, and didn't see it.",
    "partial": (
        "I was with you {spans} and didn't see it. "
        "I wasn't with you for the rest of the window."
    ),
    "none": (
        "I wasn't with you during that window, from {start} to {end}, "
        "so I can't say either way."
    ),
    "look_at": (
        "Check {look_at} before anything else. "
        "When you look at it, I will tell you what I see."
    ),
    "look": (
        "Check with your own eyes before anything else. "
        "When you look, I will tell you what I see."
    ),
}

# How spans are joined into the partial sentence. Beyond three, the
# spans are summed up rather than read out one by one.
SPAN_TEMPLATES = {
    "range": "from {first} to {last}",
    "moment": "at {first}",
    "on_and_off": "on and off between {first} and {last}",
}


def _clock(moment):
    return moment.strftime("%I:%M %p").lstrip("0")


def _spans_text(spans):
    if len(spans) > 3:
        return SPAN_TEMPLATES["on_and_off"].format(
            first=_clock(spans[0][0]), last=_clock(spans[-1][1])
        )
    parts = []
    for first, last in spans:
        if _clock(first) == _clock(last):
            parts.append(SPAN_TEMPLATES["moment"].format(first=_clock(first)))
        else:
            parts.append(
                SPAN_TEMPLATES["range"].format(first=_clock(first), last=_clock(last))
            )
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _question_text(entry):
    question = entry["question"].strip()
    return question if question.endswith("?") else question + "?"


def look_at_text(entry, day):
    """The look-at phrase for a day, with {weekday} spoken as that
    day's name, or None when the question has none."""
    look_at = entry.get("look_at")
    if not look_at:
        return None
    return look_at.replace("{weekday}", day.strftime("%A"))


def miss_line(entry, until, cov=None, start=None, day=None):
    """The spoken line for a missed window, worded for what was
    watched. Without a coverage reading, nothing is known to have
    been watched, and the line says so."""
    cov = cov or {"kind": "none", "spans": []}
    day = day or datetime.now().date()
    start_text = _clock(datetime.combine(day, start)) if start else None
    until_text = _clock(datetime.combine(day, until))
    sentences = [
        MISS_TEMPLATES["opening"].format(until=until_text, question=_question_text(entry))
    ]
    kind = cov["kind"]
    if kind == "partial":
        sentences.append(MISS_TEMPLATES["partial"].format(spans=_spans_text(cov["spans"])))
    elif kind == "complete" and start_text:
        sentences.append(MISS_TEMPLATES["complete"].format(start=start_text, end=until_text))
    elif start_text:
        sentences.append(MISS_TEMPLATES["none"].format(start=start_text, end=until_text))
    if entry.get("safety"):
        target = look_at_text(entry, day)
        if target:
            sentences.append(MISS_TEMPLATES["look_at"].format(look_at=target))
        else:
            sentences.append(MISS_TEMPLATES["look"])
    return " ".join(sentences)


def sweep_missed(store, alerts, now=None, frames=None, gap_minutes=DEFAULT_GAP_MINUTES):
    """Create one pending alert for each done question whose window
    has closed today with no witness. Returns the alerts created.

    Safe to run as often as anyone likes: a question and day that
    already have an alert are skipped, and a record that cannot be
    read is skipped with them. Never raises.

    The frame log (a coverage.FrameLog) says how much of the window
    was watched. Without one, nothing is known to have been watched.
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
    created = []
    for entry in entries:
        try:
            alert = _sweep_one(store, alerts, entry, now, frames, gap_minutes)
        except Exception:
            continue
        if alert is not None:
            created.append(alert)
    return created


def _sweep_one(store, alerts, entry, now, frames, gap_minutes):
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
    try:
        arrivals = frames.times(now.date()) if frames is not None else []
    except Exception:
        arrivals = []
    cov = coverage(
        arrivals,
        datetime.combine(now.date(), after),
        datetime.combine(now.date(), until),
        gap_minutes=gap_minutes,
    )
    safety = bool(entry.get("safety"))
    if safety:
        # Owed before the alert is written: an owed check with no
        # alert only means one extra look is spoken, while an alert
        # with no owed check would leave her sent to look with no
        # one to tell her what is there.
        store.owe_miss_check(entry["slug"], today)
    alert = {
        "id": f"{today}-{entry['slug']}",
        "slug": entry["slug"],
        "question": entry["question"],
        "date": today,
        "until": pattern.get("until"),
        "safety": safety,
        "coverage": cov["kind"],
        "line": miss_line(entry, until, cov=cov, start=after, day=now.date()),
        "spoken": False,
        "created_at": now.isoformat(timespec="seconds"),
    }
    alerts.add(alert)
    return alert
