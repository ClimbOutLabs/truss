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
import re
from datetime import datetime, time
from pathlib import Path


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:48].strip("-") or "question")


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
        folder = self._folder(slug)
        folder.mkdir(parents=True)
        (folder / "question.txt").write_text(text + "\n")
        watch = {
            "pattern": pattern or {"kind": "interval", "minutes": 15},
            "state": "planting",
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
            "pattern": watch["pattern"],
            "state": watch["state"],
            "planted_at": watch["planted_at"],
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
                if now.time() >= after:
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
