#!/usr/bin/env python3
"""
The standing-question store for the Truss server.

Plain files, one folder per question under the store root:

    <slug>/question.txt   the question in the wearer's own words
    <slug>/ref-yes.jpg     the reference look for yes, taken at plant time
    <slug>/ref-no.jpg      the reference look for no, taken at plant time
    <slug>/watch.json      the time pattern, the state, and the check history

States: planting (still collecting reference looks), standing (live),
answered (fired; may re-arm if the pattern says so), retired.

A question also has a kind of its own: "state" (is the stove off?) or
"done" (did she take her pills?). Records planted before kinds existed
read as state questions. A done question that fires leaves a witness
on its record: the day, the time, and the evidence photo's name. The
witness lasts the rest of that day and powers the redo watch in the
check loop and the missed-window alerts in alerts.py.

A question can also carry a safety flag (the stove, the pills): one
where a wrong word does harm. A missed-window alert for a safety
question directs her to look and never tells her to act, and after
the miss the next frame that shows the thing gets a state check whose
result is spoken (see alerts.py and checkloop.py). An optional "look
at" phrase names the thing to look at in her own words, like "the
{weekday} PM compartment"; {weekday} is spoken as the alert's day.
A done question is a safety question unless planted with an explicit
opt-out, and a record with no usable flag reads the same way.

Patterns:
    {"kind": "interval", "minutes": 15}   check every 15 minutes
    {"kind": "nightly", "after": "20:00"} due once per evening at or
                                          after the given local time,
                                          until it answers that night
    {"kind": "window", "after": "21:00", "until": "22:00"}
                                          due inside a daily window,
                                          done for the day once it
                                          answers, back the next day
"""

import json
import math
import re
import threading
import uuid
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path

from wording import action_verbs_in


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:48].strip("-") or "question")


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _parse_after(value):
    """Parse a nightly after time the one way every reader agrees on:
    the parser due() itself uses, and only a plain clock time, with
    no offset attached. Returns a time, or None when the value is not
    one. Validating with any other parser lets values through that
    due() then raises on."""
    if not isinstance(value, str):
        return None
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed


def _clean_pattern(pattern):
    """Validate a watch pattern for create() and fill every field the
    checker reads, so a stored pattern can always be evaluated.
    Raises ValueError with a plain message for anything unusable. A
    window is checked here, at planting time, so a bad window is
    refused to the wearer's face instead of failing quietly every
    evening afterwards."""
    if pattern is None:
        return {"kind": "interval", "minutes": 15}
    if not isinstance(pattern, dict):
        raise ValueError("A watch pattern must say how often to check.")
    kind = pattern.get("kind", "interval")
    if kind == "interval":
        minutes = pattern.get("minutes", 15)
        if not _is_number(minutes):
            raise ValueError("How often to check must be a number of minutes.")
        return {"kind": "interval", "minutes": minutes}
    if kind == "nightly":
        parsed_after = _parse_after(pattern.get("after", "20:00"))
        if parsed_after is None:
            raise ValueError("The nightly time must look like 21:30.")
        after = parsed_after.strftime("%H:%M")
        gap = pattern.get("minutes", 15)
        if not _is_number(gap):
            raise ValueError("The gap between checks must be a number of minutes.")
        return {"kind": "nightly", "after": after, "minutes": gap}
    if kind == "window":
        after = _parse_after(pattern.get("after"))
        until = _parse_after(pattern.get("until"))
        if after is None or until is None:
            raise ValueError(
                "A window needs a start time and an end time, like 21:00 and 22:00."
            )
        if until <= after:
            raise ValueError(
                "A window must end after it starts. Windows that run "
                "past midnight are not supported."
            )
        gap = pattern.get("minutes", 15)
        if not _is_number(gap) or gap < 0:
            raise ValueError("The gap between checks must be a number of minutes.")
        return {
            "kind": "window",
            "after": after.strftime("%H:%M"),
            "until": until.strftime("%H:%M"),
            "minutes": gap,
        }
    raise ValueError("A watch pattern is either interval, nightly, or window.")


QUESTION_KINDS = ("state", "done")


def _clean_kind(value):
    """A stored kind that is missing or not understood reads as a
    state question. Cleaning never raises: a damaged record degrades
    to the plainer behavior instead of breaking the store."""
    return value if value in QUESTION_KINDS else "state"


def _parse_time(value):
    """A local time of day from stored text, or None when the value
    cannot be read as one. Never raises."""
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError:
            return None
    return None


def _window_times(pattern):
    """A window pattern's start and end times, or None when the
    pattern is not a readable window that ends after it starts.
    This is the one window-validity rule: planting, due checks, and
    the missed-window sweep all read a window through here."""
    if not isinstance(pattern, dict) or pattern.get("kind") != "window":
        return None
    after = _parse_time(pattern.get("after"))
    until = _parse_time(pattern.get("until"))
    if after is None or until is None or until <= after:
        return None
    return after, until


def _gap_seconds(pattern):
    """The minimum seconds between checks for a pattern. A stored gap
    that is not a usable number falls back to fifteen minutes rather
    than raising."""
    minutes = pattern.get("minutes", 15)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return 15 * 60
    if minutes < 0:
        return 15 * 60
    return minutes * 60


def _clean_witness(value):
    """A stored witness record, or None when it is missing or not in
    the shape the loop writes: a date, a time, and a photo name, all
    text. Never raises."""
    if not isinstance(value, dict):
        return None
    date = value.get("date")
    at = value.get("at")
    frame = value.get("frame")
    if not all(isinstance(v, str) for v in (date, at, frame)):
        return None
    try:
        datetime.fromisoformat(at)
    except ValueError:
        return None
    return {"date": date, "at": at, "frame": frame}


LOOK_AT_MAX = 80


def _clean_look_at(value):
    """A stored look-at phrase, or None when it is missing or not
    one line of text. Never raises."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > LOOK_AT_MAX or "\n" in value:
        return None
    return value


# The words a question may open with. "Should" and "must" are left
# out on purpose: "Should I take my pills" invites a verdict on acting.
# No question mark is required, since voice transcripts often lack one.
QUESTION_OPENERS = {
    "is", "are", "was", "were", "am", "did", "do", "does", "have", "has",
    "had", "can", "could", "will", "would", "which", "what", "where",
    "when", "who", "how",
}


# Openers that are also helper verbs, and so also open commands: "Have
# your pills now", "Do take my night pills". After one of these, the
# next word must be who or what is asked about.
HELPER_OPENERS = {"have", "has", "had", "do", "does", "did", "can", "could", "will", "would"}
SUBJECT_WORDS = {
    # "your" is left out: "Have your pills now" is a command, and she
    # asks about her own things as "my".
    "i", "you", "we", "he", "she", "it", "they", "the", "my",
    "our", "his", "her", "their", "this", "that", "these", "those", "a",
    "an", "there", "anyone", "someone", "somebody", "tonight's", "today's",
}


def _asks_a_question(text):
    """Whether the words open as a question. A helper-verb opener must
    be followed by a subject: a word from SUBJECT_WORDS, or a name
    (capitalized in her words, and not an action verb)."""
    words = re.findall(r"[A-Za-z']+", text)
    if not words or words[0].lower() not in QUESTION_OPENERS:
        return False
    if words[0].lower() not in HELPER_OPENERS:
        return True
    if len(words) < 2:
        return False
    nxt = words[1]
    if nxt.lower() in SUBJECT_WORDS:
        return True
    return nxt[0].isupper() and not action_verbs_in(nxt)


def look_at_for(entry, date_text):
    """A question's look-at phrase for one day, with {weekday} spoken as
    that day's name, or None when it has none. The day is the owed
    day, never the day it happens to be now: at 3 AM on Wednesday, a
    check owed for Tuesday is about Tuesday's compartment."""
    look_at = entry.get("look_at")
    if not look_at:
        return None
    try:
        day = datetime.fromisoformat(date_text)
    except (TypeError, ValueError):
        return None
    return look_at.replace("{weekday}", day.strftime("%A"))


# How long into the night a state check owed for yesterday's miss is
# still offered: a window ending near midnight is settled just after
# it, and her look at the organizer at half past midnight still
# deserves an answer.
OWED_OVERNIGHT_UNTIL = time(4, 0)


def _clean_safety(value, kind):
    """A stored safety flag. A record with no usable flag (planted
    before the flag existed, or damaged) reads as the default for its
    kind: a done question is a safety question. Failing toward the
    careful wording is the safe way to fail. Never raises."""
    if isinstance(value, bool):
        return value
    return _clean_kind(kind) == "done"


def _clean_miss_check(value):
    """A stored post-miss state check, or None when it is missing or
    not in the shape the sweep writes: a date and a state, "owed" or
    "spoken". Never raises."""
    if not isinstance(value, dict):
        return None
    date = value.get("date")
    state = value.get("state")
    if not isinstance(date, str) or state not in ("owed", "spoken"):
        return None
    return {"date": date, "state": state}


def display_time(iso_text):
    """An ISO datetime as a spoken time of day, like 8:05 PM."""
    moment = datetime.fromisoformat(iso_text)
    return moment.strftime("%I:%M %p").lstrip("0")


class QuestionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # One lock for the whole store. Every read-modify-write of
        # a watch record runs under it, so the check loop, the
        # missed-window sweep thread, and planting can never
        # interleave a read and a write on the same record and lose
        # or tear an update. It is re-entrant because public methods
        # call each other (list calls get, redo_watch calls
        # witness_today, and so on) while already holding it.
        # Contention is tiny: a handful of questions, a check every
        # so often, and a sweep every half minute.
        self._lock = threading.RLock()
        # Questions with a check running right now. The check loop
        # claims its due done questions before the model call and
        # releases them after the firing is recorded, so the
        # missed-window sweep never calls a window missed while a
        # check that may witness it is still in flight. Kept in
        # memory only: after a restart no check is running.
        self._in_flight = Counter()

    # -- internal helpers -------------------------------------------------

    def _folder(self, slug):
        return self.root / slug

    def _read_watch(self, slug):
        """The stored watch record as a dict, or None when there is
        no readable watch record: the file is missing, unreadable,
        not valid JSON, two texts torn together, or valid JSON that
        is not a record at all. Never raises. Callers treat None as
        a broken or half-planted record, never as a reason to fail
        the whole store. Call with the store lock held."""
        path = self._folder(slug) / "watch.json"
        try:
            text = path.read_text()
        except Exception:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _write_watch(self, slug, watch):
        # Write to a side file and move it into place, so a watch
        # record is never seen half-written and a failed write leaves
        # the previous record whole. The side file name is unique to
        # this write, so no two writers, and no leftover from a
        # crash, can ever collide on it. Call with the store lock
        # held; the lock is what makes the wider read-modify-write
        # that ends here atomic, not the rename alone.
        folder = self._folder(slug)
        path = folder / "watch.json"
        tmp = folder / f"watch.json.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(watch, indent=2) + "\n")
                handle.flush()
            tmp.replace(path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    # -- planting ---------------------------------------------------------

    def create(self, text, pattern=None, kind="state", safety=None, look_at=None):
        """Start a new question in the planting state. Returns its slug.

        The kind is "state" unless a done question is asked for. The
        safety flag is a plain true or false; left out, a done question
        is a safety question and a state question is not, so the
        careful wording is what a planter gets without asking for it.
        The look-at phrase, when given, is one short line of text with
        no action verb in it, since it is spoken inside the direction
        to look (see wording.py).

        A done question's window with no gap given is checked on every
        frame inside it: an act takes seconds, and a question resting
        between checks would neither see it nor be able to say it
        watched.
        """
        with self._lock:
            text = text.strip()
            if not text:
                raise ValueError("The question text is empty.")
            if kind not in QUESTION_KINDS:
                raise ValueError("A question is either a state question or a done question.")
            if safety is None:
                safety = kind == "done"
            if not isinstance(safety, bool):
                raise ValueError("The safety flag is either true or false.")
            if (safety or kind == "done") and not _asks_a_question(text):
                # A safety or done question's words are spoken back
                # inside its alert. Words that do not open as a question
                # ("Take my night pills") would be an instruction to act.
                raise ValueError(
                    "This must be asked as a question, "
                    "like: Did I take my night pills?"
                )
            if isinstance(look_at, str) and not look_at.strip():
                look_at = None
            if look_at is not None:
                if _clean_look_at(look_at) is None:
                    raise ValueError(
                        f"What to look at is one short line, up to {LOOK_AT_MAX} characters."
                    )
                look_at = look_at.strip()
                verbs = action_verbs_in(look_at)
                if verbs:
                    raise ValueError(
                        "What to look at names the thing, never the act. "
                        f"Leave out: {', '.join(verbs)}."
                    )
            if (
                kind == "done"
                and isinstance(pattern, dict)
                and pattern.get("kind") == "window"
                and "minutes" not in pattern
            ):
                pattern = dict(pattern, minutes=0)
            # Clean the pattern before anything is created, so a refused
            # planting leaves no folder behind and burns no sequence.
            pattern = _clean_pattern(pattern)
            base = slugify(text)
            slug = base
            n = 2
            while self._folder(slug).exists():
                slug = f"{base}-{n}"
                n += 1
            # Planting order is a fact about the store, not the clock: two
            # questions planted in the same second still have an order.
            seq = self._next_seq()
            folder = self._folder(slug)
            folder.mkdir(parents=True)
            (folder / "question.txt").write_text(text + "\n")
            watch = {
                "kind": kind,
                "pattern": pattern,
                "state": "planting",
                "seq": seq,
                "planted_at": datetime.now().isoformat(timespec="seconds"),
                "last_checked_at": None,
                "answered_at": None,
                "witness": None,
                "redo_spoken": False,
                "safety": safety,
                "look_at": look_at,
                "miss_check": None,
            }
            self._write_watch(slug, watch)
            return slug

    def save_ref(self, slug, kind, data):
        """Save one reference look (kind is "yes" or "no").

        When both looks exist, a planting question starts standing.
        Returns the question's state after the save.
        """
        with self._lock:
            if kind not in ("yes", "no"):
                raise ValueError("A reference look is either yes or no.")
            folder = self._folder(slug)
            watch = self._read_watch(slug)
            if not self._watch_ok(watch) or not (folder / "question.txt").is_file():
                # Refuse before writing anything: a look saved into a
                # folder reads cannot see would plant a phantom.
                raise KeyError(f"No question named {slug}.")
            (folder / f"ref-{kind}.jpg").write_bytes(data)
            if (
                watch["state"] == "planting"
                and (folder / "ref-yes.jpg").exists()
                and (folder / "ref-no.jpg").exists()
            ):
                watch["state"] = "standing"
                self._write_watch(slug, watch)
            return watch["state"]

    def _next_seq(self):
        """The next planting sequence number. A counter file in the
        store root is the source; when it is missing or unreadable the
        number is derived by scanning the existing questions, skipping
        any folder too broken to read, so one bad folder can never
        block planting. Call with the store lock held."""
        counter = self.root / ".seq"
        try:
            last = int(counter.read_text().strip())
        except (OSError, ValueError):
            last = 0
            if self.root.exists():
                for folder in self.root.iterdir():
                    if not folder.is_dir():
                        continue
                    watch = self._read_watch(folder.name)
                    if isinstance(watch, dict):
                        seq = watch.get("seq", 0)
                        if isinstance(seq, int) and not isinstance(seq, bool):
                            last = max(last, seq)
        nxt = last + 1
        try:
            counter.write_text(str(nxt))
        except OSError:
            pass
        return nxt

    def set_sequence(self, slug, seq):
        """Rewrite a question's planting sequence number. Used when a
        set of questions must be put into a known order, as the
        bedtime round does."""
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            watch["seq"] = seq
            self._write_watch(slug, watch)

    # -- reading ----------------------------------------------------------

    @staticmethod
    def _watch_ok(watch):
        """Whether a watch record is complete enough to read as a
        question: a mapping with a pattern, a state, and timestamps
        that parse. Anything less is damage, and reads treat the
        folder as no question rather than raising over it."""
        if not isinstance(watch, dict):
            return False
        if not isinstance(watch.get("state"), str):
            return False
        pattern = watch.get("pattern")
        if not isinstance(pattern, dict):
            return False
        kind = pattern.get("kind")
        if kind == "interval":
            if not _is_number(pattern.get("minutes")):
                return False
        elif kind == "nightly":
            if _parse_after(pattern.get("after")) is None:
                return False
            if not _is_number(pattern.get("minutes", 15)):
                return False
        elif kind == "window":
            # A window damaged after planting still reads as a
            # question: due() and the missed-window sweep both go
            # through _window_times and treat it as never due.
            pass
        else:
            # No kind, or a kind due() has no branch for: due() would
            # silently never check it, so it is not a readable record.
            return False
        for key in ("planted_at", "last_checked_at", "answered_at"):
            value = watch.get(key)
            if value is None:
                if key == "planted_at":
                    return False
                continue
            if not isinstance(value, str):
                return False
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return False
            if parsed.tzinfo is not None:
                # The store writes plain local times; a stamp carrying
                # an offset cannot be compared with them.
                return False
        return True

    @staticmethod
    def _stamp(watch, key):
        value = watch.get(key)
        return value if isinstance(value, str) else None

    def get(self, slug):
        """One question's record, or None when it cannot be read as
        one: the folder is missing, the question text is missing,
        or there is no readable watch record (a crash during the
        first write can leave only question.txt and a side file
        behind, and a torn watch.json reads as no record at all).
        A broken record is skipped, never fatal: it must not block
        listing, planting, or checks for every other question."""
        with self._lock:
            folder = self._folder(slug)
            if not folder.exists():
                return None
            watch = self._read_watch(slug)
            if not self._watch_ok(watch):
                return None
            try:
                question_text = (folder / "question.txt").read_text().strip()
            except Exception:
                return None
            if not question_text:
                return None
            seq = watch.get("seq", 0)
            return {
                "slug": slug,
                "question": question_text,
                "kind": _clean_kind(watch.get("kind")),
                "pattern": watch["pattern"],
                "state": watch["state"],
                "witness": _clean_witness(watch.get("witness")),
                "redo_spoken": bool(watch.get("redo_spoken")),
                "safety": _clean_safety(watch.get("safety"), watch.get("kind")),
                "look_at": _clean_look_at(watch.get("look_at")),
                "miss_check": _clean_miss_check(watch.get("miss_check")),
                "planted_at": watch["planted_at"],
                "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
                "last_checked_at": self._stamp(watch, "last_checked_at"),
                "answered_at": self._stamp(watch, "answered_at"),
                "has_ref_yes": (folder / "ref-yes.jpg").exists(),
                "has_ref_no": (folder / "ref-no.jpg").exists(),
                "ref_yes_path": str(folder / "ref-yes.jpg"),
                "ref_no_path": str(folder / "ref-no.jpg"),
            }

    def list(self):
        with self._lock:
            out = []
            try:
                folders = sorted(self.root.iterdir())
            except Exception:
                return []
            for folder in folders:
                if folder.is_dir():
                    entry = self.get(folder.name)
                    if entry is not None:
                        out.append(entry)
            # Planting order, not folder order: it is the order the wearer
            # meets the questions in, and the order due() hands them to
            # the check loop. Questions planted before seq existed all
            # carry 0 and fall back to their timestamps.
            out.sort(key=lambda e: (e["seq"], e["planted_at"] or "", e["slug"]))
            return out

    # -- the check loop's view ---------------------------------------------

    def due(self, now=None):
        """Standing questions whose pattern says a check is due now."""
        with self._lock:
            now = now or datetime.now()
            out = []
            for entry in self.list():
                if entry["state"] != "standing":
                    continue
                pattern = entry["pattern"]
                kind = pattern.get("kind")
                if kind == "interval":
                    last = entry["last_checked_at"] or entry["planted_at"]
                    last_dt = datetime.fromisoformat(last)
                    if (now - last_dt).total_seconds() >= pattern["minutes"] * 60:
                        out.append(entry)
                elif kind == "nightly":
                    after = time.fromisoformat(pattern.get("after", "20:00"))
                    if now.time() < after:
                        continue
                    answered = entry["answered_at"]
                    if answered and datetime.fromisoformat(answered).date() == now.date():
                        continue
                    last = entry["last_checked_at"]
                    if last:
                        gap = pattern.get("minutes", 15) * 60
                        if (now - datetime.fromisoformat(last)).total_seconds() < gap:
                            continue
                    out.append(entry)
                elif kind == "window":
                    times = _window_times(pattern)
                    if times is None:
                        # A window that cannot be read is never due. It
                        # was refused at planting time; a record damaged
                        # afterwards degrades to silence, not an error.
                        continue
                    after, until = times
                    if not after <= now.time() < until:
                        continue
                    answered = entry["answered_at"]
                    if answered:
                        try:
                            if datetime.fromisoformat(answered).date() == now.date():
                                continue
                        except ValueError:
                            pass
                    last = entry["last_checked_at"]
                    if last:
                        try:
                            last_dt = datetime.fromisoformat(last)
                        except ValueError:
                            last_dt = None
                        if (
                            last_dt is not None
                            and (now - last_dt).total_seconds() < _gap_seconds(pattern)
                        ):
                            continue
                    out.append(entry)
            return out

    def mark_checked(self, slug, when=None):
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            watch["last_checked_at"] = (when or datetime.now()).isoformat(
                timespec="seconds"
            )
            self._write_watch(slug, watch)

    def mark_answered(self, slug, when=None):
        """Record a firing. Nightly and window questions re-arm for
        the next day; interval questions retire until the wearer
        plants them again."""
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            when = when or datetime.now()
            watch["answered_at"] = when.isoformat(timespec="seconds")
            pattern = watch.get("pattern")
            pattern_kind = pattern.get("kind") if isinstance(pattern, dict) else None
            if pattern_kind in ("nightly", "window"):
                watch["state"] = "standing"
                watch["last_checked_at"] = when.isoformat(timespec="seconds")
            else:
                watch["state"] = "answered"
            self._write_watch(slug, watch)

    # -- checks in flight ---------------------------------------------------

    def claim_due(self, now=None):
        """due(), with every due done question claimed in the same
        locked step. Reading what is due and claiming it can then
        never be split by a sweep: a sweep either runs first, when
        the window is still open or the question is not due, or
        finds the claim. Release with end_checks()."""
        with self._lock:
            due = self.due(now=now)
            self._in_flight.update(
                e["slug"] for e in due if e.get("kind") == "done"
            )
            return due

    def end_checks(self, slugs):
        with self._lock:
            self._in_flight.subtract(slugs)
            self._in_flight += Counter()

    def check_in_flight(self, slug):
        with self._lock:
            return self._in_flight[slug] > 0

    # -- the witness: what a done question saw, and when ------------------

    def mark_answered_with_witness(self, slug, frame_name, when=None):
        """Record a done question's firing and its witness together,
        in one watch record write. The two facts are never separable:
        either both land, or the write fails and neither does, so a
        wearer can never be told later that a seen act was not done
        only because half of its firing was saved."""
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            when = when or datetime.now()
            watch["answered_at"] = when.isoformat(timespec="seconds")
            pattern = watch.get("pattern")
            if isinstance(pattern, dict) and pattern.get("kind") in (
                "nightly",
                "window",
            ):
                watch["state"] = "standing"
                watch["last_checked_at"] = when.isoformat(timespec="seconds")
            else:
                watch["state"] = "answered"
            watch["witness"] = {
                "date": when.date().isoformat(),
                "at": when.isoformat(timespec="seconds"),
                "frame": frame_name,
            }
            watch["redo_spoken"] = False
            self._write_watch(slug, watch)

    def record_witness(self, slug, frame_name, when=None):
        """Write the witness for a done question that just fired: the
        day, the time, and the evidence photo's name. A fresh witness
        clears any redo flag left from an older one."""
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            when = when or datetime.now()
            watch["witness"] = {
                "date": when.date().isoformat(),
                "at": when.isoformat(timespec="seconds"),
                "frame": frame_name,
            }
            watch["redo_spoken"] = False
            self._write_watch(slug, watch)

    def witness_today(self, slug, now=None):
        """Today's witness for a question, or None. A witness from an
        earlier day is cleared on the way out and never returned: it
        must never be spoken from on a later day."""
        with self._lock:
            now = now or datetime.now()
            watch = self._read_watch(slug)
            if watch is None:
                return None
            witness = _clean_witness(watch.get("witness"))
            if witness is None:
                return None
            if witness["date"] != now.date().isoformat():
                watch["witness"] = None
                watch["redo_spoken"] = False
                self._write_watch(slug, watch)
                return None
            return witness

    def mark_redo_spoken(self, slug):
        """Record that today's redo reminder has been spoken. It is
        spoken once per witness, never twice."""
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            watch["redo_spoken"] = True
            self._write_watch(slug, watch)

    def redo_watch(self, now=None):
        """Done questions holding a witness from today whose redo
        reminder has not been spoken yet. These are no longer checked
        as ordinary questions; the loop offers them frames under a
        return prompt instead. A return check obeys the same gap
        between checks as an ordinary due question, counted from the
        last recorded check of any kind, so a watched question costs
        at most one return check per gap."""
        with self._lock:
            now = now or datetime.now()
            out = []
            for entry in self.list():
                if entry["kind"] != "done" or entry["redo_spoken"]:
                    continue
                if entry["state"] != "standing":
                    continue
                if self.witness_today(entry["slug"], now=now) is None:
                    continue
                last = entry.get("last_checked_at")
                if last:
                    try:
                        last_dt = datetime.fromisoformat(last)
                    except (TypeError, ValueError):
                        last_dt = None
                    if (
                        last_dt is not None
                        and (now - last_dt).total_seconds()
                        < _gap_seconds(entry["pattern"])
                    ):
                        continue
                out.append(entry)
            return out

    def done_status(self, slug, now=None):
        """For listings: today's witness as a spoken time of day (or
        None), and whether today's redo reminder has been spoken."""
        with self._lock:
            now = now or datetime.now()
            witness = self.witness_today(slug, now=now)
            if witness is None:
                return {"witness_time": None, "redo_spoken": False}
            watch = self._read_watch(slug)
            return {
                "witness_time": display_time(witness["at"]),
                "redo_spoken": bool(watch.get("redo_spoken")) if watch else False,
            }

    # -- after a missed window: the owed state check --------------------

    def owe_miss_check(self, slug, date):
        """Record that a safety question's window closed unwitnessed
        on this day, so the next frame that shows the thing gets a
        state check and its result is spoken."""
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            current = _clean_miss_check(watch.get("miss_check"))
            if current is not None and current["date"] == date:
                return
            watch["miss_check"] = {"date": date, "state": "owed"}
            self._write_watch(slug, watch)

    def miss_check_watch(self, now=None):
        """Safety done questions owed a state check today, with no
        witness today. A miss settled just after midnight is owed for
        yesterday, and is offered through the small hours, until
        OWED_OVERNIGHT_UNTIL, but only when the question names the
        thing to look at, so the check can say which day it means. Any older owed check is never offered:
        that day's compartment is not today's. There is no
        gap between state checks: the alert promised to say what is
        there when she looks, and a hallway frame a minute earlier
        must not make the loop miss her look at the thing itself."""
        with self._lock:
            now = now or datetime.now()
            today = now.date().isoformat()
            days = {today}
            if now.time() < OWED_OVERNIGHT_UNTIL:
                days.add((now.date() - timedelta(days=1)).isoformat())
            out = []
            for entry in self.list():
                if entry["kind"] != "done" or not entry["safety"]:
                    continue
                if entry["state"] != "standing":
                    continue
                owed = entry["miss_check"]
                if owed is None or owed["state"] != "owed" or owed["date"] not in days:
                    continue
                if (
                    owed["date"] == today
                    and self.witness_today(entry["slug"], now=now) is not None
                ):
                    continue
                if owed["date"] != today and not entry["look_at"]:
                    # Overnight, a question that names no thing to look
                    # at cannot say which day's compartment it means,
                    # so it is not checked at all.
                    continue
                out.append(entry)
            return out

    def mark_miss_check_spoken(self, slug):
        """Record that the owed state check's result was spoken. It is
        spoken once per miss, never twice."""
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            owed = _clean_miss_check(watch.get("miss_check"))
            if owed is None:
                raise KeyError(f"No state check is owed for {slug}.")
            owed["state"] = "spoken"
            watch["miss_check"] = owed
            self._write_watch(slug, watch)

    def retire(self, slug):
        with self._lock:
            watch = self._read_watch(slug)
            if watch is None:
                raise KeyError(f"No question named {slug}.")
            watch["state"] = "retired"
            self._write_watch(slug, watch)
