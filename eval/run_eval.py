#!/usr/bin/env python3
"""Runner for the Mannie eval.

Reads a manifest, filters by split, calls checker.check() once per
frame, and writes one results row per frame to a results JSONL file.
For the whole first-numbers run (manifest, compile, both modes, SAM,
scorecard) use first_numbers.py; this runs one mode.

    python3 eval/run_eval.py --mode generic
    python3 eval/run_eval.py --mode planted --split tuning
    python3 eval/run_eval.py --mode mock --manifest eval/dummy/manifest.jsonl

The model endpoint comes from the environment the server reads
(TRUSS_BASE_URL, TRUSS_MODEL, TRUSS_API_KEY, TRUSS_TIMEOUT_S,
TRUSS_MAX_TOKENS); see checker.py. Mock mode never touches it.

Rows are appended and flushed one at a time, so a long run that stops
part way keeps what it did. Running again with --resume skips frames
already answered for this mode and retries the ones whose check
failed (a timeout, an unreachable endpoint), so a failure is never
mistaken for the model's answer.

A frame whose check throws records the error and verdict cannot_tell;
the run continues. One bad frame never kills a 200-frame run.
"""

import argparse
import ipaddress
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import checker

try:
    import yaml
except ImportError:
    yaml = None

EVAL_DIR = Path(__file__).resolve().parent


def load_questions(path):
    if yaml is None:
        raise SystemExit(
            "PyYAML is not installed. Install it with: sudo apt install python3-yaml "
            "(or python3 -m pip install pyyaml)"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {q["id"]: q for q in data["questions"]}


def read_jsonl(path):
    rows = []
    if not Path(path).exists():
        return rows
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


read_manifest = read_jsonl


def question_dir_for_row(row):
    """dataset/<question-folder>/<label>/<frame>.jpg -> the question folder."""
    return (EVAL_DIR / row["path"]).parent.parent


def refs_for_row(row):
    """Reference photo paths for a manifest row: ref-yes.jpg and
    ref-no.jpg in the frame's question folder."""
    question_dir = question_dir_for_row(row)
    return question_dir / "ref-yes.jpg", question_dir / "ref-no.jpg"


def endpoint_is_local(base_url):
    """True when the model runs on this network (the DGX), so a check
    has no per-call charge: loopback, a private address, a bare host
    name, or a .local name."""
    host = (urlparse(base_url or "").hostname or "").lower()
    if not host:
        return True
    if host == "localhost" or host.endswith(".local") or "." not in host:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def endpoint_base_url():
    return os.environ.get("TRUSS_BASE_URL") or "http://127.0.0.1:8080/v1"


def done_keys(out_path, mode):
    """Rows already in out_path that need no rerun, and the file's rows
    with every failed row of this mode dropped (so it is retried)."""
    kept = []
    done = set()
    for row in read_jsonl(out_path):
        if row.get("mode") == mode and (row.get("failure") or row.get("error")):
            continue
        kept.append(row)
        if row.get("mode") == mode:
            done.add(row.get("frame_id"))
    return done, kept


def _check_row(row, question, mode, mock_error_rate):
    frame_path = EVAL_DIR / row["path"]
    ref_yes = ref_no = looks = None
    if mode == "planted-photos":
        ref_yes, ref_no = refs_for_row(row)
        if not ref_yes.exists() or not ref_no.exists():
            raise FileNotFoundError(
                f"missing reference photos for {row.get('question_id')}: "
                f"{ref_yes.name} / {ref_no.name} in {ref_yes.parent}"
            )
    elif mode == "planted":
        looks = checker.verdict.read_looks(question_dir_for_row(row))
        if not looks:
            raise FileNotFoundError(
                f"no looks.txt for {row.get('question_id')} in {question_dir_for_row(row)}; "
                "first_numbers.py compiles it from the two reference photos"
            )
    return checker.check(
        frame_path=frame_path,
        question_text=question["question"],
        mode=mode,
        ref_yes=ref_yes,
        ref_no=ref_no,
        looks=looks,
        frame_id=row.get("frame_id"),
        label=row.get("label"),
        mock_error_rate=mock_error_rate,
    )


def run_mode(manifest_rows, questions, mode, out_path, resume=False,
             mock_error_rate=0.15, progress=None):
    """Check every manifest row in one mode, appending to out_path.
    Returns (processed, errors, skipped). progress, when given, is
    called with (index, total, out_row) after each frame."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if resume:
        done, kept = done_keys(out_path, mode)
        tmp = out_path.with_name(f".{out_path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for row in kept:
                f.write(json.dumps(row) + "\n")
        os.replace(tmp, out_path)
    else:
        out_path.write_text("", encoding="utf-8")

    todo = [r for r in manifest_rows if r.get("frame_id") not in done]
    skipped = len(manifest_rows) - len(todo)
    base_url = endpoint_base_url()
    local = True if mode == "mock" else endpoint_is_local(base_url)

    processed = 0
    errors = 0
    with open(out_path, "a", encoding="utf-8") as out_file:
        for index, row in enumerate(todo, start=1):
            question = questions.get(row.get("question_id"))
            error = None
            try:
                if question is None:
                    raise KeyError(f"question_id {row.get('question_id')!r} not in questions.yaml")
                result = _check_row(row, question, mode, mock_error_rate)
            except Exception as e:
                errors += 1
                error = str(e) or type(e).__name__
                result = {
                    "verdict": "cannot_tell",
                    "reason": f"Check not run, recorded as cannot_tell: {error}",
                    "latency_ms": 0,
                    "usage": {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0},
                }

            out_row = {
                "frame_id": row.get("frame_id"),
                "question_id": row.get("question_id"),
                "label": row.get("label"),
                "kind": row.get("kind"),
                "lighting": row.get("lighting"),
                "split": row.get("split"),
                "mode": mode,
                "verdict": result["verdict"],
                "reason": result["reason"],
                "missing": result.get("missing", ""),
                "model": result.get("model"),
                "failure": result.get("failure"),
                "looks": result.get("looks"),
                "local": local,
                "latency_ms": result["latency_ms"],
                "usage": result["usage"],
                "error": error,
            }
            out_file.write(json.dumps(out_row) + "\n")
            out_file.flush()
            processed += 1
            if progress:
                progress(index, len(todo), out_row)

    return processed, errors, skipped


def print_progress(index, total, row, started=[None]):
    """One line per frame, with a rough time left."""
    now = time.monotonic()
    if started[0] is None or index == 1:
        started[0] = now - row["latency_ms"] / 1000
    elapsed = now - started[0]
    left = (elapsed / index) * (total - index)
    tag = row["failure"] or ("error" if row["error"] else "")
    print(
        f"  [{index}/{total}] {row['mode']} {row['frame_id']}: label {row['label']}, "
        f"said {row['verdict']}{' (' + tag + ')' if tag else ''}, "
        f"{row['latency_ms'] / 1000:.1f} s; about {left / 60:.0f} min left",
        flush=True,
    )


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
        choices=list(checker.MODES),
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
        "--resume",
        action="store_true",
        help="Keep rows already in --out for this mode; retry only failed checks.",
    )
    parser.add_argument(
        "--mock-error-rate",
        type=float,
        default=0.15,
        help="Mock mode only: fraction of frames whose label is deliberately flipped (default: 0.15).",
    )
    args = parser.parse_args()

    if args.out is None:
        args.out = str(Path(args.manifest).resolve().parent / "results.jsonl")

    questions = load_questions(args.questions)
    manifest_rows = read_manifest(args.manifest)
    if args.split != "all":
        manifest_rows = [r for r in manifest_rows if r.get("split") == args.split]
    if not manifest_rows:
        print(f"No manifest rows match split={args.split!r} in {args.manifest}.")
    if args.mode != "mock":
        print(f"Endpoint: {endpoint_base_url()}, model {os.environ.get('TRUSS_MODEL') or 'muse-glimmer-30b'}.")

    processed, errors, skipped = run_mode(
        manifest_rows, questions, args.mode, args.out, resume=args.resume,
        mock_error_rate=args.mock_error_rate,
        progress=None if args.mode == "mock" else print_progress,
    )
    note = f", {skipped} already done" if skipped else ""
    print(f"Processed {processed} frames ({errors} with errors{note}). Results written to {args.out}.")


if __name__ == "__main__":
    sys.exit(main())
