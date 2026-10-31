#!/usr/bin/env python3
"""
The day's answers, read back for the app's History screen.

The check loop writes one plain-text line per answer into
log/<YYYY-MM-DD>/answers.txt, fields separated by " | ":

    20:28:31 | Is the stove off? | yes | <reason> | evidence: 202831_x.jpg | model: m
    21:40:02 | Did I take my pills? | redo reminder | <line> | evidence: <witness photo> | now: ...
    22:05:10 | ... | state check after a missed window | no | <line> | evidence: ...
    21:38:12 | Is the stove off? | cannot tell | <line> | evidence: ... | model: m

This module only reads those files; it never writes or deletes. A line
that does not parse is skipped, never fatal. Evidence photos are served
by exact name from one day's folder: a name with anything but letters,
digits, dashes, underscores and one ".jpg" is refused, so no request can
reach a file outside the log folder.
"""

import re
from datetime import date
from pathlib import Path

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHOTO = re.compile(r"^[A-Za-z0-9_-]+\.jpg$")


def valid_day(day):
    """True for a real calendar day written YYYY-MM-DD."""
    if not isinstance(day, str) or not _DAY.match(day):
        return False
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True


def parse_line(line):
    """One answer line as a dict, or None when it is not one."""
    fields = [f.strip() for f in line.rstrip("\n").split(" | ")]
    if len(fields) < 4:
        return None
    clock, question = fields[0], fields[1]
    if not re.match(r"^\d{2}:\d{2}:\d{2}$", clock) or not question:
        return None
    evidence = None
    model = None
    rest = []
    for field in fields[2:]:
        if field.startswith("evidence: ") and evidence is None:
            evidence = field[len("evidence: "):].strip()
        elif field.startswith("model: ") and model is None:
            model = field[len("model: "):].strip()
        elif field.startswith("now: "):
            # A redo line's return frame; its evidence is the witness.
            continue
        else:
            rest.append(field)
    if not rest:
        return None
    kind = rest[0]
    if kind == "yes":
        sort = "answer"
        said = " | ".join(rest[1:])
    elif kind == "redo reminder":
        sort = "redo"
        said = " | ".join(rest[1:])
    elif kind == "cannot tell":
        sort = "cannot_tell"
        said = " | ".join(rest[1:])
    elif kind == "state check after a missed window":
        sort = "state_check"
        # The verdict, then the spoken line.
        said = " | ".join(rest[2:]) if len(rest) > 2 else " | ".join(rest[1:])
    else:
        sort = "other"
        said = " | ".join(rest)
    if evidence is not None and not _PHOTO.match(evidence):
        evidence = None
    return {
        "time": clock,
        "question": question,
        "kind": sort,
        "text": said,
        "evidence": evidence,
        "model": model,
    }


def answers(log_dir, day):
    """The answers logged on one day, newest first. Each one says whether
    an outlined copy of its evidence photo exists beside it."""
    if not valid_day(day):
        raise ValueError("A day must look like 2026-10-29.")
    folder = Path(log_dir) / day
    path = folder / "answers.txt"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        item = parse_line(line)
        if item is None:
            continue
        outline = None
        if item["evidence"]:
            candidate = item["evidence"][: -len(".jpg")] + "_outline.jpg"
            if (folder / candidate).is_file():
                outline = candidate
        item["outline"] = outline
        out.append(item)
    out.reverse()
    return out


def photo_path(log_dir, day, name):
    """The path of one kept photo, or None when the day or name is not
    acceptable or no such file is kept."""
    if not valid_day(day) or not isinstance(name, str) or not _PHOTO.match(name):
        return None
    root = Path(log_dir).resolve()
    path = (root / day / name).resolve()
    if path.parent != root / day or not path.is_file():
        return None
    return path
