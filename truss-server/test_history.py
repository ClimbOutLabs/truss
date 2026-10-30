#!/usr/bin/env python3
"""
Tests for reading the day's answers back (history.py).

Run from the truss-server folder:  python3 test_history.py
Stdlib only. Exits non-zero on the first failure.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import history  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


LINES = [
    "20:28:31 | Is the stove off? | yes | All knobs point to off. | "
    "evidence: 202831_is-the-stove-off.jpg | model: muse-glimmer-30b",
    "21:40:02 | Did I take my pills? | redo reminder | You took those at 21:05. I saw you. | "
    "evidence: 214002_did-i-take-my-pills.jpg",
    "22:05:10 | Did I take my pills? | state check after a missed window | no | "
    "The Thursday PM compartment is still full. | evidence: 220510_did-i-take-my-pills.jpg",
    "not an answer line",
    "",
    "23:00:00 | Is the door locked? | yes | Reason | evidence: ../../etc/passwd | model: m",
]


def test_parse_kinds():
    a = history.parse_line(LINES[0])
    check("yes line parses", a is not None and a["kind"] == "answer")
    check("yes line keeps the reason", a["text"] == "All knobs point to off.")
    check("yes line keeps the model", a["model"] == "muse-glimmer-30b")
    check("yes line keeps the evidence", a["evidence"] == "202831_is-the-stove-off.jpg")
    r = history.parse_line(LINES[1])
    check("redo line parses", r["kind"] == "redo" and r["text"].startswith("You took those"))
    check("redo line has no model", r["model"] is None)
    m = history.parse_line(LINES[2])
    check("state check line parses", m["kind"] == "state_check")
    check("state check keeps the spoken line", m["text"].startswith("The Thursday PM"))
    check("junk is skipped", history.parse_line(LINES[3]) is None)
    check("blank is skipped", history.parse_line(LINES[4]) is None)
    bad = history.parse_line(LINES[5])
    check("a path in the evidence field is dropped", bad is not None and bad["evidence"] is None)


def test_answers_newest_first_with_outline():
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp) / "2026-10-29"
        day.mkdir()
        (day / "answers.txt").write_text("\n".join(LINES) + "\n")
        (day / "202831_is-the-stove-off.jpg").write_bytes(JPEG)
        (day / "202831_is-the-stove-off_outline.jpg").write_bytes(JPEG)
        items = history.answers(tmp, "2026-10-29")
        check("four answers read", len(items) == 4)
        check("newest first", items[0]["time"] == "23:00:00" and items[-1]["time"] == "20:28:31")
        check("outline found", items[-1]["outline"] == "202831_is-the-stove-off_outline.jpg")
        check("no outline when none kept", items[-2]["outline"] is None)
        check("a day with no log is empty", history.answers(tmp, "2026-10-28") == [])
        try:
            history.answers(tmp, "../2026-10-29")
            check("a bad day is refused", False)
        except ValueError:
            check("a bad day is refused", True)


def test_photo_path_is_fenced():
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp) / "2026-10-29"
        day.mkdir()
        (day / "202831_x.jpg").write_bytes(JPEG)
        (Path(tmp) / "secret.jpg").write_bytes(JPEG)
        check("a kept photo is found", history.photo_path(tmp, "2026-10-29", "202831_x.jpg") is not None)
        check("a missing photo is None", history.photo_path(tmp, "2026-10-29", "nope.jpg") is None)
        check("dot-dot in the name is refused", history.photo_path(tmp, "2026-10-29", "../secret.jpg") is None)
        check("a slash in the name is refused", history.photo_path(tmp, "2026-10-29", "a/b.jpg") is None)
        check("dot-dot as the day is refused", history.photo_path(tmp, "..", "secret.jpg") is None)
        check("not a jpg is refused", history.photo_path(tmp, "2026-10-29", "answers.txt") is None)
        check("an impossible day is refused", history.photo_path(tmp, "2026-13-40", "202831_x.jpg") is None)


if __name__ == "__main__":
    test_parse_kinds()
    test_answers_newest_first_with_outline()
    test_photo_path_is_fenced()
    print("All history tests passed.")
