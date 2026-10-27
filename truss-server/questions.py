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

Patterns:
    {"kind": "interval", "minutes": 15}   check every 15 minutes
    {"kind": "nightly", "after": "20:00"} due once per evening at or
                                          after the given local time,
                                          until it answers that night
"""

import json
import math
import re
from datetime import datetime, time
from pathlib import Path


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:48].strip("-") or "question")


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _clean_pattern(pattern):
    """Validate a watch pattern for create() and fill every field the
    checker reads, so a stored pattern can always be evaluated.
    Raises ValueError with a plain message for anything unusable."""
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
        after = pattern.get("after", "20:00")
        valid = isinstance(after, str)
        if valid:
            try:
                datetime.strptime(after, "%H:%M")
            except ValueError:
                valid = False
        if not valid:
            raise ValueError("The nightly time must look like 21:30.")
        gap = pattern.get("minutes", 15)
        if not _is_number(gap):
            raise ValueError("The gap between checks must be a number of minutes.")
        return {"kind": "nightly", "after": after, "minutes": gap}
    raise ValueError("A watch pattern is either interval or nightly.")


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
        path = self._folder(slug) / "watch.json"
        path.write_text(json.dumps(watch, indent=2) + "\n")

    # -- planting ---------------------------------------------------------

    def create(self, text, pattern=None):
        """Start a new question in the planting state. Returns its slug."""
        text = text.strip()
        if not text:
            raise ValueError("The question text is empty.")
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
            "pattern": _clean_pattern(pattern),
            "state": "planting",
            "seq": seq,
            "planted_at": datetime.now().isoformat(timespec="seconds"),
            "last_checked_at": None,
            "answered_at": None,
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
        try:
            watch = self._read_watch(slug)
        except Exception:
            watch = None
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
        block planting."""
        counter = self.root / ".seq"
        try:
            last = int(counter.read_text().strip())
        except (OSError, ValueError):
            last = 0
            if self.root.exists():
                for folder in self.root.iterdir():
                    if not folder.is_dir():
                        continue
                    try:
                        watch = self._read_watch(folder.name)
                    except Exception:
                        continue
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
        kind = pattern.get("kind", "interval")
        if kind == "interval":
            if not _is_number(pattern.get("minutes")):
                return False
        elif kind == "nightly":
            after = pattern.get("after")
            if not isinstance(after, str):
                return False
            try:
                datetime.strptime(after, "%H:%M")
            except ValueError:
                return False
            if not _is_number(pattern.get("minutes", 15)):
                return False
        else:
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
        folder = self._folder(slug)
        if not folder.exists():
            return None
        try:
            watch = self._read_watch(slug)
            question = (folder / "question.txt").read_text().strip()
        except Exception:
            watch, question = None, None
        if not question or not self._watch_ok(watch):
            # A folder too broken to read (no watch file, a watch file
            # that no longer parses or is not a complete record, no
            # question text) is not a readable question. Reads skip
            # it, so one broken folder can never take down the store's
            # lists, the round's planting, or the check loop's view.
            # Writes to that slug still surface the real error.
            return None
        seq = watch.get("seq", 0)
        return {
            "slug": slug,
            "question": question,
            "pattern": watch["pattern"],
            "state": watch["state"],
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
        return out

    def mark_checked(self, slug, when=None):
        watch = self._read_watch(slug)
        watch["last_checked_at"] = (when or datetime.now()).isoformat(
            timespec="seconds"
        )
        self._write_watch(slug, watch)

    def mark_answered(self, slug, when=None):
        """Record a firing. Nightly questions re-arm for the next night;
        interval questions retire until the wearer plants them again."""
        watch = self._read_watch(slug)
        when = when or datetime.now()
        watch["answered_at"] = when.isoformat(timespec="seconds")
        if watch["pattern"].get("kind") == "nightly":
            watch["state"] = "standing"
            watch["last_checked_at"] = when.isoformat(timespec="seconds")
        else:
            watch["state"] = "answered"
        self._write_watch(slug, watch)

    def retire(self, slug):
        watch = self._read_watch(slug)
        watch["state"] = "retired"
        self._write_watch(slug, watch)
