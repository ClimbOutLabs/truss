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
"""

from datetime import time

ROUND = [
    "Is the stove off?",
    "Is the front door locked?",
    "Is tonight's pill compartment empty?",
]


def plant_round(store, after="20:00", gap_minutes=1):
    """Plant the round's questions as nightly, in walk order.

    Questions already planted (standing, or still collecting their
    reference looks) are left as they are and included. A retired round
    question stops the planting with a plain error: the wearer retired
    it on purpose, and the round does not quietly bring it back.
    Returns the slugs in walk order.
    """
    try:
        time.fromisoformat(after)
    except (ValueError, TypeError):
        raise ValueError(f"Not a time of day: {after!r}") from None
    if (
        isinstance(gap_minutes, bool)
        or not isinstance(gap_minutes, (int, float))
        or gap_minutes < 0
    ):
        raise ValueError("gap_minutes must be a number of minutes, zero or more")

    pattern = {"kind": "nightly", "after": after, "minutes": gap_minutes}
    by_text = {e["question"]: e for e in store.list()}
    slugs = []
    for text in ROUND:
        entry = by_text.get(text)
        if entry is None:
            slugs.append(store.create(text, pattern))
        elif entry["state"] == "retired":
            raise ValueError(f"That question was retired: {text}")
        else:
            slugs.append(entry["slug"])
    return slugs
