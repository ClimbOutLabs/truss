#!/usr/bin/env python3
"""Runner for the Mannie eval.

Reads a manifest, filters by split, calls checker.check() once per
frame, and writes one results row per frame to a results JSONL file.

Without MODEL_API_KEY set, any mode other than mock refuses with one
plain line and exit code 2, before touching the manifest file at all.
Mock mode never needs the key.

A frame whose check throws records the error and verdict cannot_tell;
the run continues. One bad frame never kills a 200-frame run.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import checker

try:
    import yaml
except ImportError:
    yaml = None

EVAL_DIR = Path(__file__).resolve().parent


def load_questions(path):
    if yaml is None:
        raise SystemExit(
            "PyYAML is not installed. Install it with: python3 -m pip install pyyaml"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {q["id"]: q for q in data["questions"]}


def read_manifest(path):
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


def refs_for_row(row):
    """Reference photo paths for a manifest row, derived from its frame
    path: dataset/<question-folder>/<yes|no>/<frame>.jpg has its
    references at dataset/<question-folder>/ref-yes.jpg and ref-no.jpg."""
    frame_path = Path(row["path"])
    question_dir = frame_path.parent.parent
    return EVAL_DIR / question_dir / "ref-yes.jpg", EVAL_DIR / question_dir / "ref-no.jpg"


def run(args):
    questions = load_questions(args.questions)
    manifest_rows = read_manifest(args.manifest)

    if args.split != "all":
        manifest_rows = [r for r in manifest_rows if r.get("split") == args.split]

    if not manifest_rows:
        print(f"No manifest rows match split={args.split!r} in {args.manifest}.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    processed = 0
    errors = 0
    with open(out_path, "w", encoding="utf-8") as out_file:
        for row in manifest_rows:
            question = questions.get(row.get("question_id"))
            error = None
            try:
                if question is None:
                    raise KeyError(f"question_id {row.get('question_id')!r} not in questions.yaml")
                frame_path = EVAL_DIR / row["path"]
                ref_yes = ref_no = None
                if args.mode == "planted":
                    ref_yes, ref_no = refs_for_row(row)
                    if not ref_yes.exists() or not ref_no.exists():
                        raise FileNotFoundError(
                            f"missing reference photos for {row.get('question_id')}: "
                            f"{ref_yes} / {ref_no}"
                        )
                result = checker.check(
                    frame_path=frame_path,
                    question_text=question["question"],
                    mode=args.mode,
                    ref_yes=ref_yes,
                    ref_no=ref_no,
                    frame_id=row.get("frame_id"),
                    label=row.get("label"),
                    mock_error_rate=args.mock_error_rate,
                )
            except Exception as e:
                errors += 1
                error = str(e)
                result = {
                    "verdict": "cannot_tell",
                    "reason": f"Check failed, recorded as cannot_tell: {error}",
                    "latency_ms": 0,
                    "usage": {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0},
                }

            out_row = {
                "frame_id": row.get("frame_id"),
                "question_id": row.get("question_id"),
                "label": row.get("label"),
                "kind": row.get("kind"),
                "split": row.get("split"),
                "mode": args.mode,
                "verdict": result["verdict"],
                "reason": result["reason"],
                "latency_ms": result["latency_ms"],
                "usage": result["usage"],
                "error": error,
            }
            out_file.write(json.dumps(out_row) + "\n")
            processed += 1

    print(f"Processed {processed} frames ({errors} with errors). Results written to {out_path}.")


def main():
    parser = argparse.ArgumentParser(description="Run the Mannie eval checker over a manifest.")
    parser.add_argument(
        "--manifest",
        default=str(EVAL_DIR / "dataset" / "manifest.jsonl"),
        help="Path to a manifest JSONL file (default: eval/dataset/manifest.jsonl).",
    )
    parser.add_argument(
        "--questions",
        default=str(EVAL_DIR / "questions.yaml"),
        help="Path to questions.yaml (default: eval/questions.yaml).",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["generic", "planted", "mock"],
        help="Check mode to run.",
    )
    parser.add_argument(
        "--split",
        default="all",
        choices=["tuning", "heldout", "all"],
        help="Which manifest split to run (default: all).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Path to write results JSONL (default: results.jsonl next to the manifest).",
    )
    parser.add_argument(
        "--mock-error-rate",
        type=float,
        default=0.15,
        help="Mock mode only: fraction of frames whose label is deliberately flipped (default: 0.15).",
    )
    args = parser.parse_args()

    if args.mode != "mock" and not os.environ.get("MODEL_API_KEY"):
        print(
            f"MODEL_API_KEY is not set; refusing to run mode {args.mode}. "
            "Set MODEL_API_KEY, or use --mode mock to test without the API."
        )
        sys.exit(2)

    if args.out is None:
        args.out = str(Path(args.manifest).resolve().parent / "results.jsonl")

    run(args)


if __name__ == "__main__":
    main()
