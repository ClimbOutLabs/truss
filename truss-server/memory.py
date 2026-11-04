#!/usr/bin/env python3
"""
Truss Memory: one question's checks for a day, read back for the app.

Every check the loop makes is printed as one line and kept in the
server log (log/server.log, see serverlog.py), stamped when the check
finished:

    2026-11-03 23:50:30 | check    | did-i-take-my-night-pills: no (24.7 s) A red pill is visible in the Tue compartment.

That log is the only place a check that did not fire is kept whole. A
no or a cannot_tell is silence: nothing is spoken, the frame is
deleted, and nothing goes into the day's answers. The question's own
record keeps only when it was last checked, never what was seen. So
this module reads the log lines back for one question and one day, in
time order, verdict and reason as recorded.

A check that fired kept its frame as evidence in the day's answers
(see history.py). The answer line is stamped when its frame arrived
and the check line when the check finished, so a fired check's answer
sits about the check's own seconds before it. An answer is joined to
a check only when it is the one nearest match for that question
inside that small window; anything less certain is left without a
photo rather than shown with the wrong one.

The reasons are the server's own, exactly as the log kept them. The
log writes each reason on one line, and the printed check line is
shortened to 400 characters, so a very long reason is kept only as
far as the log kept it.

This module only reads; it never writes or deletes. A line that does
not parse is skipped, never fatal.
"""

import re
from datetime import datetime
from pathlib import Path

import history

# One kept check line from the server log: the day and clock the tidy
# log stamped it with, its "check" tag, then "<slug>: <verdict>
# (<seconds> s) <reason>". The tag is padded to a fixed width.
_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \| check +\| (.*)$"
)
_CHECK = re.compile(
    r"^([a-z0-9-]+): (yes|no|cannot_tell|no verdict) \((\d+(?:\.\d+)?) s\)(?: (.*))?$"
)

# A fired check's answer was stamped when its frame arrived, and the
# check line when the check finished; the two stamps sit the check's
# own seconds apart, give or take the moments around them.
_JOIN_SLACK_S = 10


def parse_check_line(line):
    """One server log line as a check, or None when it is not one.

    The result carries the day and clock it was stamped with, the
    slug, the verdict in the server's words (a check that failed
    before any verdict reads as cannot_tell, which is what it meant
    everywhere else), the reason as recorded, and how many seconds
    the check took, for the evidence join."""
    matched = _LINE.match(line.rstrip("\n"))
    if matched is None:
        return None
    day, clock, message = matched.groups()
    check = _CHECK.match(message)
    if check is None:
        return None
    slug, verdict, seconds, reason = check.groups()
    if verdict == "no verdict":
        verdict = "cannot_tell"
    return {
        "day": day,
        "time": clock,
        "slug": slug,
        "verdict": verdict,
        "reason": (reason or "").strip(),
        "seconds": float(seconds),
    }


def _clock_seconds(clock):
    """Seconds into the day for an HH:MM:SS clock, or None."""
    try:
        parsed = datetime.strptime(clock, "%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return parsed.hour * 3600 + parsed.minute * 60 + parsed.second


def _join_evidence(checks, answers, question):
    """Give each fired check the evidence photo its answer kept.

    answers are the day's parsed answer lines (history.answers). Only
    a plain answer joins a yes, and only a spoken refusal joins a
    cannot_tell; a no kept no frame, so it is never joined. Each
    answer joins at most one check, and only when it is the single
    nearest candidate inside the slack around the check's start."""
    candidates = [
        a
        for a in answers
        if a["question"] == question
        and a["evidence"]
        and a["kind"] in ("answer", "cannot_tell")
    ]
    used = set()
    for entry in checks:
        kind = {"yes": "answer", "cannot_tell": "cannot_tell"}.get(entry["verdict"])
        if kind is None:
            continue
        finished = _clock_seconds(entry["time"])
        if finished is None:
            continue
        started = finished - entry["seconds"]
        best = None
        tied = False
        for index, answer in enumerate(candidates):
            if index in used or answer["kind"] != kind:
                continue
            arrived = _clock_seconds(answer["time"])
            if arrived is None:
                continue
            gap = abs(arrived - started)
            if gap > _JOIN_SLACK_S:
                continue
            if best is None or gap < best[0]:
                best = (gap, index)
                tied = False
            elif gap == best[0]:
                tied = True
        if best is not None and not tied:
            used.add(best[1])
            entry["evidence"] = candidates[best[1]]["evidence"]


def timeline(log_dir, day, slug, question):
    """One question's checks on one day, in time order.

    Each entry is {"time", "verdict", "reason", "evidence"}: the
    clock the check finished, the verdict in the server's words, the
    reason as recorded, and the name of the evidence photo the server
    kept for it, or None. Raises ValueError for a day that is not a
    real calendar day, like history.answers."""
    if not history.valid_day(day):
        raise ValueError("A day must look like 2026-10-29.")
    path = Path(log_dir) / "server.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    checks = []
    for line in lines:
        parsed = parse_check_line(line)
        if parsed is None or parsed["day"] != day or parsed["slug"] != slug:
            continue
        checks.append(
            {
                "time": parsed["time"],
                "verdict": parsed["verdict"],
                "reason": parsed["reason"],
                "seconds": parsed["seconds"],
                "evidence": None,
            }
        )
    try:
        answers = history.answers(log_dir, day)
    except ValueError:
        answers = []
    _join_evidence(checks, answers, question)
    return [
        {
            "time": entry["time"],
            "verdict": entry["verdict"],
            "reason": entry["reason"],
            "evidence": entry["evidence"],
        }
        for entry in checks
    ]
