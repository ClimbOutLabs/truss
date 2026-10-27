#!/usr/bin/env python3
"""Dummy data tool for the Mannie eval.

By default, creates 2 questions x 2 labels x 4 frames of tiny valid
placeholder JPEGs plus a manifest, in a scratch tree (eval/dummy/), so
the whole pipeline (runner, checker in mock mode, scorer) can run
before a single real photo exists. Refuses to write into eval/dataset/.

--all-questions and --frames-per-state scale this up, for example to
exercise the scorer with a bigger synthetic set across all 20
questions. The output always stays under eval/dummy/.
"""

import argparse
import base64
import json
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

EVAL_DIR = Path(__file__).resolve().parent
DATASET_DIR = (EVAL_DIR / "dataset").resolve()
DUMMY_DIR = EVAL_DIR / "dummy"
QUESTIONS_PATH = EVAL_DIR / "questions.yaml"

# A minimal, valid 1x1 pixel JPEG, used as placeholder frame content.
# The pipeline never decodes pixels, it only needs a real JPEG file on
# disk, so the same tiny image is reused for every dummy frame.
_TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEB"
    "AQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX"
    "/9k="
)

DEFAULT_QUESTIONS = ["q01", "q02"]
LABELS = ["yes", "no"]
DEFAULT_FRAMES_PER_STATE = 4
TUNING_FRAMES_PER_STATE = 3
LIGHTINGS = ["day", "evening"]


def tiny_jpeg_bytes():
    return base64.b64decode(_TINY_JPEG_B64)


def load_question_kinds(path):
    """Maps question id to its manifest kind: cannot_tell_probe for q20,
    normal for everything else, per questions.yaml's own 'kind' field.
    Also returns the ids in file order, for --all-questions."""
    if yaml is None:
        raise SystemExit(
            "PyYAML is not installed. Install it with: python3 -m pip install pyyaml"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    kinds = {}
    order = []
    for q in data["questions"]:
        order.append(q["id"])
        kinds[q["id"]] = q.get("kind", "normal")
    return order, kinds


def main():
    parser = argparse.ArgumentParser(description="Create a dummy dataset for the Mannie eval.")
    parser.add_argument(
        "--all-questions",
        action="store_true",
        help="Use every question id in questions.yaml instead of the small default set.",
    )
    parser.add_argument(
        "--questions",
        default=None,
        help="Comma-separated question ids to use (ignored if --all-questions is given).",
    )
    parser.add_argument(
        "--frames-per-state",
        type=int,
        default=DEFAULT_FRAMES_PER_STATE,
        help=f"Frames per (question, label) state (default: {DEFAULT_FRAMES_PER_STATE}).",
    )
    args = parser.parse_args()

    if DUMMY_DIR.resolve() == DATASET_DIR or DATASET_DIR in DUMMY_DIR.resolve().parents:
        raise SystemExit("Refusing to write into eval/dataset/; this tool only writes eval/dummy/.")

    if DUMMY_DIR.exists():
        raise SystemExit(
            f"{DUMMY_DIR} already exists. Remove it first if you want a fresh dummy set."
        )

    order, kinds = load_question_kinds(QUESTIONS_PATH)

    if args.all_questions:
        question_ids = order
    elif args.questions:
        question_ids = [q.strip() for q in args.questions.split(",") if q.strip()]
    else:
        question_ids = DEFAULT_QUESTIONS

    frames_per_state = args.frames_per_state

    jpeg_bytes = tiny_jpeg_bytes()
    manifest_rows = []

    for question_id in question_ids:
        kind = kinds.get(question_id, "normal")
        for label in LABELS:
            folder = DUMMY_DIR / question_id / label
            folder.mkdir(parents=True, exist_ok=True)
            for seq in range(1, frames_per_state + 1):
                lighting = LIGHTINGS[(seq - 1) % len(LIGHTINGS)]
                frame_name = f"{lighting}-{seq:02d}.jpg"
                frame_path = folder / frame_name
                frame_path.write_bytes(jpeg_bytes)

                split = "tuning" if seq <= TUNING_FRAMES_PER_STATE else "heldout"
                frame_id = f"{question_id}-{label}-{lighting}-{seq:02d}"
                manifest_rows.append(
                    {
                        "frame_id": frame_id,
                        "question_id": question_id,
                        "label": label,
                        "kind": kind,
                        "lighting": lighting,
                        "split": split,
                        "path": str(frame_path.relative_to(EVAL_DIR)),
                    }
                )

    manifest_path = DUMMY_DIR / "manifest.jsonl"
    with open(manifest_path, "w", encoding="utf-8") as f:
        for row in manifest_rows:
            f.write(json.dumps(row) + "\n")

    print(f"Wrote {len(manifest_rows)} dummy frames and a manifest to {DUMMY_DIR}.")
    print(f"Questions: {len(question_ids)}, frames per state: {frames_per_state}.")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
