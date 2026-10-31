#!/usr/bin/env python3
"""
The bedtime round.

Three nightly questions planted in the order the wearer meets them on
one walk down the hall: the stove in the kitchen, the front door, and
tonight's pill compartment. One pass should resolve most of them; a
question a pass cannot settle stays standing and is checked again on
the next pass, rather than being forgotten.

The round plants with a short check gap (one minute unless told
otherwise) because the wearer is mid-round: a door that was too dark
to read on the first pass gets another look a minute later, not a
quarter of an hour later. The questions are ordinary nightly standing
questions in the store; the round is a way of planting them together,
nothing more, so the check loop, the evidence rule, and the answer log
all work exactly as they do for any other question.

Planting is careful about what is already there:

- A round question already planted on the round's own terms is
  included as it is.
- One planted on different terms (a different pattern) keeps its own
  terms, and the planting says so in its notes rather than silently
  presenting the round as something it is not.
- One that was retired stops the planting before anything is
  created: the wearer retired it on purpose, and the error names it.
- Whatever order the questions were planted in before, the round's
  members are put into walk order, so answers are spoken in the order
  the wearer meets them.
"""

import math
from datetime import time

ROUND = [
    "Is the stove off?",
    "Is the front door locked?",
    "Is tonight's pill compartment empty?",
]

# The round's safety questions: a wrong word on these does harm, so a
# yes is held to the deciding detail (the knobs, the compartment) and
# a cannot-tell with the thing in view is spoken as a refusal.
SAFETY = {"Is the stove off?", "Is tonight's pill compartment empty?"}

_STATE_RANK = {"standing": 0, "planting": 1, "retired": 2}


def _norm(text):
    return text.strip().lower().rstrip("?").strip()


def plant_round(store, after="20:00", gap_minutes=1):
    """Plant the round's questions as nightly, in walk order.

    Returns {"slugs": [...], "notes": [...]}, the slugs in walk order
    and one plain note per member that was kept on its own terms.
    """
    try:
        time.fromisoformat(after)
    except (ValueError, TypeError):
        raise ValueError(f"Not a time of day: {after!r}") from None
    if (
        isinstance(gap_minutes, bool)
        or not isinstance(gap_minutes, (int, float))
        or not math.isfinite(gap_minutes)
        or gap_minutes < 0
    ):
        raise ValueError("gap_minutes must be a finite number of minutes, zero or more")

    pattern = {"kind": "nightly", "after": after, "minutes": gap_minutes}
    entries = store.list()

    # Pick the round's existing members, matching the wearer's words
    # loosely (a missing question mark, different case) and preferring
    # a live copy over a retired one when a text exists twice.
    chosen = {}
    for text in ROUND:
        matches = [e for e in entries if _norm(e["question"]) == _norm(text)]
        if matches:
            matches.sort(key=lambda e: (_STATE_RANK.get(e["state"], 3), -e["seq"]))
            chosen[text] = matches[0]

    for text in ROUND:
        entry = chosen.get(text)
        if entry is not None and entry["state"] == "retired":
            raise ValueError(
                f"Nothing was planted. That question was retired: {entry['question']}"
            )

    notes = []
    slugs = []
    for text in ROUND:
        entry = chosen.get(text)
        if entry is None:
            slugs.append(store.create(text, pattern, safety=text in SAFETY))
        else:
            if entry["pattern"] != pattern:
                notes.append(
                    f"{entry['question']} was already planted on different "
                    "terms, so it was left as it is."
                )
            elif entry.get("safety") is not (text in SAFETY):
                notes.append(
                    f"{entry['question']} was already planted "
                    + ("without" if text in SAFETY else "with")
                    + " the safety flag, so it was left as it is."
                )
            slugs.append(entry["slug"])

    # Put the members into walk order, whatever came before. Only
    # their order among themselves is set; other questions keep the
    # numbers they had.
    current = {e["slug"]: e for e in store.list()}
    base = min(current[slug]["seq"] for slug in slugs)
    for i, slug in enumerate(slugs):
        if current[slug]["seq"] != base + i:
            store.set_sequence(slug, base + i)

    return {"slugs": slugs, "notes": notes}
