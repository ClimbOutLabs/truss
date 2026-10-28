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
import re
from datetime import datetime, time
from pathlib import Path


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:48].strip("-") or "question")


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


def display_time(iso_text):
    """An ISO datetime as a spoken time of day, like 8:05 PM."""
    moment = datetime.fromisoformat(iso_text)
    return moment.strftime("%I:%M %p").lstrip("0")


class QuestionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- internal helpers -------------------------------------------------

    def _folder(self, slug):
        return self.root / slug

    def _read_watch(self, slug):
        path = self._folder(slug) / "watch.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _write_watch(self, slug, watch):
        # Write to a side file and move it into place, so a watch
        # record is never seen half-written and a failed write leaves
        # the previous record whole.
        path = self._folder(slug) / "watch.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(watch, indent=2) + "\n")
        tmp.replace(path)

    # -- planting ---------------------------------------------------------

    def create(self, text, pattern=None, kind="state"):
        """Start a new question in the planting state. Returns its slug.

        The kind is "state" unless a done question is asked for. A
        window pattern is checked here, at planting time, so a bad
        window is refused to the wearer's face instead of failing
        quietly every evening afterwards.
        """
        text = text.strip()
        if not text:
            raise ValueError("The question text is empty.")
        if kind not in QUESTION_KINDS:
            raise ValueError("A question is either a state question or a done question.")
        if isinstance(pattern, dict) and pattern.get("kind") == "window":
            after = _parse_time(pattern.get("after"))
            until = _parse_time(pattern.get("until"))
            if after is None or until is None:
                raise ValueError(
                    "A window needs a start time and an end time, like 21:00 and 22:00."
                )
            if until <= after:
                raise ValueError(
                    "A window must end after it starts. Windows that run "
                    "past midnight are not supported."
                )
        base = slugify(text)
        slug = base
        n = 2
        while self._folder(slug).exists():
            slug = f"{base}-{n}"
            n += 1
        # Planting order is a fact about the store, not the clock: two
        # questions planted in the same second still have an order.
        # Counted before the new folder exists, so the half-planted
        # folder is never read back.
        seq = max((e["seq"] for e in self.list()), default=0) + 1
        folder = self._folder(slug)
        folder.mkdir(parents=True)
        (folder / "question.txt").write_text(text + "\n")
        watch = {
            "kind": kind,
            "pattern": pattern or {"kind": "interval", "minutes": 15},
            "state": "planting",
            "seq": seq,
            "planted_at": datetime.now().isoformat(timespec="seconds"),
            "last_checked_at": None,
            "answered_at": None,
            "witness": None,
            "redo_spoken": False,
        }
        self._write_watch(slug, watch)
        return slug

    def save_ref(self, slug, kind, data):
        """Save one reference look (kind is "yes" or "no").

        When both looks exist, a planting question starts standing.
        Returns the question's state after the save.
        """
        if kind not in ("yes", "no"):
            raise ValueError("A reference look is either yes or no.")
        folder = self._folder(slug)
        if not folder.exists():
            raise KeyError(f"No question named {slug}.")
        (folder / f"ref-{kind}.jpg").write_bytes(data)
        watch = self._read_watch(slug)
        if (
            watch["state"] == "planting"
            and (folder / "ref-yes.jpg").exists()
            and (folder / "ref-no.jpg").exists()
        ):
            watch["state"] = "standing"
            self._write_watch(slug, watch)
        return watch["state"]

    # -- reading ----------------------------------------------------------

    def get(self, slug):
        folder = self._folder(slug)
        if not folder.exists():
            return None
        watch = self._read_watch(slug)
        return {
            "slug": slug,
            "question": (folder / "question.txt").read_text().strip(),
            "kind": _clean_kind(watch.get("kind")),
            "pattern": watch["pattern"],
            "state": watch["state"],
            "witness": _clean_witness(watch.get("witness")),
            "redo_spoken": bool(watch.get("redo_spoken")),
            "planted_at": watch["planted_at"],
            "seq": watch.get("seq", 0),
            "last_checked_at": watch["last_checked_at"],
            "answered_at": watch["answered_at"],
            "has_ref_yes": (folder / "ref-yes.jpg").exists(),
            "has_ref_no": (folder / "ref-no.jpg").exists(),
            "ref_yes_path": str(folder / "ref-yes.jpg"),
            "ref_no_path": str(folder / "ref-no.jpg"),
        }

    def list(self):
        out = []
        for folder in sorted(self.root.iterdir()):
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
        watch = self._read_watch(slug)
        watch["last_checked_at"] = (when or datetime.now()).isoformat(
            timespec="seconds"
        )
        self._write_watch(slug, watch)

    def mark_answered(self, slug, when=None):
        """Record a firing. Nightly and window questions re-arm for
        the next day; interval questions retire until the wearer
        plants them again."""
        watch = self._read_watch(slug)
        when = when or datetime.now()
        watch["answered_at"] = when.isoformat(timespec="seconds")
        if watch["pattern"].get("kind") in ("nightly", "window"):
            watch["state"] = "standing"
            watch["last_checked_at"] = when.isoformat(timespec="seconds")
        else:
            watch["state"] = "answered"
        self._write_watch(slug, watch)

    # -- the witness: what a done question saw, and when ------------------

    def mark_answered_with_witness(self, slug, frame_name, when=None):
        """Record a done question's firing and its witness together,
        in one watch record write. The two facts are never separable:
        either both land, or the write fails and neither does, so a
        wearer can never be told later that a seen act was not done
        only because half of its firing was saved."""
        watch = self._read_watch(slug)
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
        watch = self._read_watch(slug)
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
        now = now or datetime.now()
        watch = self._read_watch(slug)
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
        watch = self._read_watch(slug)
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
        now = now or datetime.now()
        witness = self.witness_today(slug, now=now)
        if witness is None:
            return {"witness_time": None, "redo_spoken": False}
        watch = self._read_watch(slug)
        return {
            "witness_time": display_time(witness["at"]),
            "redo_spoken": bool(watch.get("redo_spoken")),
        }

    def retire(self, slug):
        watch = self._read_watch(slug)
        watch["state"] = "retired"
        self._write_watch(slug, watch)
