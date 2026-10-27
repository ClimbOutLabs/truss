#!/usr/bin/env python3
"""
Run the bedtime round without glasses, a phone, or a model key.

The round is planted in a scratch folder, the reference looks are
dummy frames, and the checks are scripted: the first pass down the
hall settles the stove, cannot read the door in the dark, and finds a
pill still in tonight's compartment; the second pass settles the
other two. What prints is what the wearer would hear, in order, plus
where the evidence and the answer log landed.

    python3 bedtime_demo.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bedtime import plant_round  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from questions import QuestionStore  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"demo-frame" + b"\xff\xd9"
SCRIPT = {
    "is-the-stove-off": [
        ("yes", "All four burners are dark."),
    ],
    "is-the-front-door-locked": [
        ("cannot_tell", "The latch is in shadow."),
        ("yes", "The deadbolt is thrown."),
    ],
    "is-tonight-s-pill-compartment-empty": [
        ("no", "A pill is still in tonight's compartment."),
        ("yes", "Tonight's compartment is empty."),
    ],
}


def scripted_check(frame_path, entry):
    script = SCRIPT.get(entry["slug"], [])
    if script:
        verdict, reason = script.pop(0)
    else:
        verdict, reason = "cannot_tell", "Nothing decisive is visible."
    return {"verdict": verdict, "reason": reason, "latency_ms": 0, "usage": {}}


def main():
    tmp = tempfile.mkdtemp(prefix="truss-bedtime-demo-")
    store = QuestionStore(Path(tmp) / "questions")
    slugs = plant_round(store)["slugs"]
    for slug in slugs:
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)

    heard = []
    clock = [datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)]
    loop = CheckLoop(
        store,
        scripted_check,
        Path(tmp) / "log",
        speak_fn=lambda text, entry, evidence: heard.append(text),
        now_fn=lambda: clock[0],
    )
    captures = Path(tmp) / "captures"
    captures.mkdir()

    print("The bedtime round, two passes down the hall.")
    print("Planted, in walk order:")
    for entry in store.list():
        print(f"  {entry['question']}")
    for label in ("First pass", "Second pass, a minute later"):
        frame_path = captures / f"{label.split()[0].lower()}.jpg"
        frame_path.write_bytes(JPEG)
        before = len(heard)
        loop.process_frame(frame_path)
        print(f"\n{label}:")
        if len(heard) > before:
            for line in heard[before:]:
                print(f"  Mannie says: {line}")
        else:
            print("  (silence)")
        clock[0] += timedelta(seconds=90)

    day = Path(tmp) / "log" / clock[0].strftime("%Y-%m-%d")
    print(f"\nEvidence and the answer log are in {day}")
    print("The round is done for the night and re-arms tomorrow evening.")


if __name__ == "__main__":
    main()
