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
from the frames actually checked for that question (see coverage.py):
the whole window, the spans it was watched, or none of it.

For a safety question (the stove, the pills) it directs her to look
and never tells her to act. "You didn't take your pills" after a
missed witness is how the product would cause the double dose it
exists to prevent. The line sends her to the thing itself, and the
sweep leaves a state check owed: the next frame that shows the thing
is checked as it is now, and that result is spoken (see checkloop.py).

Every sentence an alert can speak is in MISS_TEMPLATES below, and
test_safety.py scans all of them for action verbs (see wording.py).
"""

import json
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

from coverage import DEFAULT_GAP_MINUTES, coverage
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


# Every sentence a missed-window alert can speak. Placeholders are
# filled with times, the question's own words, and the look-at
# phrase; nothing else is ever added to a line. The safety direction
# is the only instruction any alert gives, and it is to look.
MISS_TEMPLATES = {
    "opening": "It is past {until}. About your question: {question}",
    "opening_yesterday": (
        "Yesterday's window ended at {until}. About your question: {question}"
    ),
    "complete": "I was with you the whole window, from {start} to {end}, and didn't see it.",
    "partial": (
        "I was with you {spans} and didn't see it. "
        "I wasn't with you for the rest of the window."
    ),
    "none": (
        "I wasn't with you during that window, from {start} to {end}, "
        "so I can't say either way."
    ),
    "look_at": (
        "Check {look_at} first, before you act on this. "
        "When I see it, I'll tell you what's there."
    ),
    "look": (
        "Check with your own eyes first, before you act on this. "
        "When I see it, I'll tell you what's there."
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


def miss_line(entry, until, cov=None, start=None, day=None, yesterday=False):
    """The spoken line for a missed window, worded for what was
    watched. Without a coverage reading, nothing is known to have
    been watched, and the line says so. A miss settled just after
    midnight is spoken as yesterday's."""
    cov = cov or {"kind": "none", "spans": []}
    day = day or datetime.now().date()
    start_text = _clock(datetime.combine(day, start)) if start else None
    until_text = _clock(datetime.combine(day, until))
    opening = MISS_TEMPLATES["opening_yesterday" if yesterday else "opening"]
    sentences = [opening.format(until=until_text, question=_question_text(entry))]
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


def _desk_name(question):
    """The short name of a done question for the Desk banner: "Did I
    take my night pills?" names "the night pills". None when the
    question does not open "Did I <verb>", so the banner falls back
    to the question's own words."""
    words = question.strip().rstrip("?.").split()
    if len(words) < 4 or words[0].lower() != "did" or words[1].lower() != "i":
        return None
    rest = words[3:]
    if rest and rest[0].lower() in ("my", "your"):
        rest[0] = "the"
    return " ".join(rest) or None


def desk_banner(alert):
    """The Desk's words for a pending alert: a headline and one line
    under it, in the shape the owner chose, filled from the alert's
    own record (the question it names, the time its window closed).

    Display only. The phone speaks alert["line"], which miss_line
    composes under the safety rules above; this never touches that
    line, so what the wearer hears is unchanged. None when the
    record holds no question to name, and the Desk then shows the
    spoken line as it did before."""
    question = alert.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    name = _desk_name(question) or question.strip()
    day = "tonight" if alert.get("date") == datetime.now().date().isoformat() else "last night"
    headline = f"One thing from {day} is still open: {name}"
    if not headline.endswith("?"):
        headline += "."
    closed = ""
    until = alert.get("until")
    if isinstance(until, str):
        try:
            closed = f" at {_clock(datetime.strptime(until.strip(), '%H:%M'))}"
        except ValueError:
            closed = ""
    note = f"The window closed{closed} with nothing seen. You may well have taken them."
    return headline, note


def sweep_missed(store, alerts, now=None, checks=None, gap_minutes=DEFAULT_GAP_MINUTES):
    """Create one pending alert for each done question whose window
    has closed today with no witness. Returns the alerts created.

    Safe to run as often as anyone likes: a question and day that
    already have an alert are skipped, and a record that cannot be
    read is skipped with them. Never raises.

    The check log (a coverage.CheckLog) says how much of the window
    each question was actually watched. Without one, nothing is known
    to have been watched.
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
                alert = _sweep_one(store, alerts, entry, now, day, checks, gap_minutes)
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


def _sweep_one(store, alerts, entry, now, day, checks, gap_minutes):
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
    try:
        checked = checks.times(day, entry["slug"]) if checks is not None else []
    except Exception:
        checked = []
    cov = coverage(
        checked,
        datetime.combine(day, after),
        datetime.combine(day, until),
        gap_minutes=gap_minutes,
    )
    safety = bool(entry.get("safety"))
    if safety:
        # Owed before the alert is written: an owed check with no
        # alert only means one extra look is spoken, while an alert
        # with no owed check would leave her sent to look with no
        # one to tell her what is there. Owing is once per day: a
        # check already owed or spoken today is left as it is, so a
        # retried sweep never speaks a state check twice.
        store.owe_miss_check(entry["slug"], today)
    alert = {
        "id": f"{today}-{entry['slug']}",
        "slug": entry["slug"],
        "question": entry["question"],
        "date": today,
        "until": pattern.get("until"),
        "safety": safety,
        "coverage": cov["kind"],
        "line": miss_line(
            entry, until, cov=cov, start=after, day=day, yesterday=day != now.date()
        ),
        "spoken": False,
        "created_at": now.isoformat(timespec="seconds"),
    }
    alerts.add(alert)
    return alert
