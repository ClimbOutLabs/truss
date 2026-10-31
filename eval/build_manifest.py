#!/usr/bin/env python3
"""Build eval/dataset/manifest.jsonl from the photos in eval/dataset/.

This is the drop-in path: copy photos into folders, run this, and the
manifest matches what is on disk. capture.py (the HTTP capture server)
still works and writes the same layout; rows it wrote are kept as they
are, so its split and kind survive a rebuild.

Layout, one folder per question:

    eval/dataset/q01-stove-off/      (q01 or stove-off also work)
        ref-yes.jpg                  her yes-look (the stove off)
        ref-no.jpg                   her no-look (a burner on)
        yes/<any name>.jpg           frames whose true answer is yes
        no/<any name>.jpg            frames whose true answer is no
        cannot_tell/<any name>.jpg   frames that do not show what
                                     decides it (knobs out of view,
                                     too dark, blurred); the right
                                     answer is cannot_tell

Frame files may be .jpg, .jpeg, .png or .webp. A name starting with
"hard" marks a hard negative (kind hard_negative); a name starting
with "day" or "evening" (or "night") sets the lighting. Within each
question and label, the first three frames by name are the tuning
split and the rest are held out, the same rule capture.py uses.

Rebuilding is safe to repeat: rows for files still on disk keep their
frame_id, kind and split; rows for files that are gone are dropped and
named; new files are added.

    python3 eval/build_manifest.py          rebuild and print the status
    python3 eval/build_manifest.py --check  print the status only
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

EVAL_DIR = Path(__file__).resolve().parent
DATASET_DIR = EVAL_DIR / "dataset"
QUESTIONS_PATH = EVAL_DIR / "questions.yaml"

LABEL_DIRS = {"yes": "yes", "no": "no", "cannot_tell": "cannot_tell", "cannot-tell": "cannot_tell"}
LABELS = ("yes", "no", "cannot_tell")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
UNREADABLE_SUFFIXES = {".heic", ".heif", ".dng", ".raw"}
TUNING_FRAMES_PER_STATE = 3


def load_questions(path=QUESTIONS_PATH):
    if yaml is None:
        raise SystemExit(
            "PyYAML is not installed. Install it with: sudo apt install python3-yaml "
            "(or python3 -m pip install pyyaml)"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {q["id"]: q for q in data["questions"]}


def question_for_folder(name, questions):
    """The question id a dataset folder belongs to, or None. Accepts
    q01-stove-off, q01, and stove-off."""
    by_slug = {q["slug"]: qid for qid, q in questions.items()}
    for qid, q in questions.items():
        if name == f"{qid}-{q['slug']}":
            return qid
    if name in questions:
        return name
    return by_slug.get(name)


def _lighting(stem):
    low = stem.lower()
    if low.startswith("day"):
        return "day"
    if low.startswith("evening") or low.startswith("night"):
        return "evening"
    return "unknown"


def _kind(stem, question):
    if question.get("kind") == "cannot_tell_probe":
        return "cannot_tell_probe"
    if stem.lower().startswith("hard"):
        return "hard_negative"
    return "normal"


def _rel(path):
    """A frame's manifest path: relative to eval/ when it is under it."""
    try:
        return os.path.normpath(str(Path(path).resolve().relative_to(EVAL_DIR)))
    except ValueError:
        return os.path.normpath(str(Path(path).resolve()))


def _frame_id(qid, label, stem, taken):
    base = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "frame"
    frame_id = f"{qid}-{label}-{base}"
    n = 2
    while frame_id in taken:
        frame_id = f"{qid}-{label}-{base}-{n}"
        n += 1
    return frame_id


def read_manifest(path):
    if not Path(path).exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{line_number}: bad JSON line: {e}")
    return rows


def scan(dataset_dir, questions, existing_rows=()):
    """(rows, notes, folders): the manifest rows for every frame on
    disk, plain-words notes about anything skipped or dropped, and
    {question_id: [folder, ...]} for the folders found."""
    dataset_dir = Path(dataset_dir)
    notes = []
    folders = {}
    if not dataset_dir.exists():
        return [], [f"No dataset folder yet at {dataset_dir}."], folders

    existing = {}
    for row in existing_rows:
        path = row.get("path")
        if path:
            existing[os.path.normpath(path)] = row

    rows = []
    taken = set()
    seen_paths = set()
    for folder in sorted(p for p in dataset_dir.iterdir() if p.is_dir()):
        qid = question_for_folder(folder.name, questions)
        if qid is None:
            notes.append(f"{folder.name}/: not a question id or slug in questions.yaml, skipped.")
            continue
        folders.setdefault(qid, []).append(folder)
        question = questions[qid]
        for child in sorted(folder.iterdir()):
            if child.is_file():
                low = child.name.lower()
                if child.name in ("ref-yes.jpg", "ref-no.jpg") or not low.startswith("ref"):
                    continue
                if low.startswith("ref"):
                    notes.append(
                        f"{folder.name}/{child.name}: reference photos must be named "
                        "exactly ref-yes.jpg and ref-no.jpg (JPEG); not used."
                    )
            elif child.is_dir() and child.name not in LABEL_DIRS:
                notes.append(
                    f"{folder.name}/{child.name}/: not yes, no or cannot_tell, skipped."
                )
        for dir_name, label in LABEL_DIRS.items():
            label_dir = folder / dir_name
            if not label_dir.is_dir():
                continue
            files = sorted(p for p in label_dir.iterdir() if p.is_file() and not p.name.startswith("."))
            group_existing = []
            group_new = []
            for f in files:
                suffix = f.suffix.lower()
                if suffix in UNREADABLE_SUFFIXES:
                    notes.append(
                        f"{f.relative_to(dataset_dir)}: {suffix} is not read by the model "
                        "server; export it as JPEG. Skipped."
                    )
                    continue
                if suffix not in IMAGE_SUFFIXES:
                    continue
                rel = _rel(f)
                seen_paths.add(rel)
                if rel in existing:
                    group_existing.append((f, existing[rel]))
                else:
                    group_new.append(f)
            index = 0
            for f, old in group_existing:
                row = dict(old)
                row["question_id"] = qid
                row["label"] = label
                if row.get("frame_id") in taken or not row.get("frame_id"):
                    row["frame_id"] = _frame_id(qid, label, f.stem, taken)
                taken.add(row["frame_id"])
                row.setdefault("kind", _kind(f.stem, question))
                row.setdefault("lighting", _lighting(f.stem))
                row.setdefault(
                    "split", "tuning" if index < TUNING_FRAMES_PER_STATE else "heldout"
                )
                rows.append(row)
                index += 1
            for f in group_new:
                frame_id = _frame_id(qid, label, f.stem, taken)
                taken.add(frame_id)
                rows.append(
                    {
                        "frame_id": frame_id,
                        "question_id": qid,
                        "label": label,
                        "kind": _kind(f.stem, question),
                        "lighting": _lighting(f.stem),
                        "split": "tuning" if index < TUNING_FRAMES_PER_STATE else "heldout",
                        "path": _rel(f),
                    }
                )
                index += 1

    gone = sorted(set(existing) - seen_paths)
    for path in gone:
        notes.append(f"{path}: in the old manifest but not on disk; dropped.")
    return rows, notes, folders


def write_manifest(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    os.replace(tmp, path)


def status_lines(rows, questions, folders):
    counts = {qid: {label: 0 for label in LABELS} for qid in questions}
    for row in rows:
        if row.get("question_id") in counts and row.get("label") in LABELS:
            counts[row["question_id"]][row["label"]] += 1
    lines = ["question                          yes  no  cannot_tell  refs  looks"]
    for qid, q in questions.items():
        c = counts[qid]
        qfolders = folders.get(qid, [])
        if not qfolders and not any(c.values()):
            continue
        refs = any((f / "ref-yes.jpg").exists() and (f / "ref-no.jpg").exists() for f in qfolders)
        looks = any((f / "looks.txt").exists() for f in qfolders)
        name = f"{qid} {q['slug']}" + (" (safety)" if q.get("safety") else "")
        lines.append(
            f"{name:<33} {c['yes']:>3} {c['no']:>3} {c['cannot_tell']:>12}  "
            f"{'both' if refs else 'NO':>4}  {'yes' if looks else '-':>5}"
        )
    missing_safety = [
        f"{qid} {q['slug']}" for qid, q in questions.items()
        if q.get("safety") and not any(counts[qid].values())
    ]
    if missing_safety:
        lines.append("Safety questions with no frames yet: " + ", ".join(missing_safety))
    lines.append(f"Total frames: {len(rows)}")
    return lines


def rebuild(dataset_dir=DATASET_DIR, questions=None, write=True):
    questions = questions or load_questions()
    manifest = Path(dataset_dir) / "manifest.jsonl"
    old = read_manifest(manifest)
    rows, notes, folders = scan(dataset_dir, questions, old)
    if write and Path(dataset_dir).exists():
        write_manifest(manifest, rows)
    return rows, notes, folders


def main():
    parser = argparse.ArgumentParser(description="Build the eval manifest from eval/dataset/.")
    parser.add_argument("--dataset", default=str(DATASET_DIR), help="Dataset folder.")
    parser.add_argument("--check", action="store_true", help="Print the status; write nothing.")
    args = parser.parse_args()
    questions = load_questions()
    rows, notes, folders = rebuild(args.dataset, questions, write=not args.check)
    for line in status_lines(rows, questions, folders):
        print(line)
    for note in notes:
        print("Note: " + note)
    if not args.check and Path(args.dataset).exists():
        print(f"Manifest: {Path(args.dataset) / 'manifest.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
