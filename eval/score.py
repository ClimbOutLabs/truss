#!/usr/bin/env python3
"""Scorer for the Mannie eval.

Reads a results JSONL (the schema run_eval.py writes) and writes
eval/SCORECARD.md, printing the same table to stdout.

Terms, exactly as defined in questions.yaml:
  false-fire  = verdict yes, label no.
  miss        = verdict no, label yes.
  cannot_tell = reported as its own rate, neither right nor wrong,
                except on q20 (the cannot_tell probe question), where
                cannot_tell is the correct verdict and a confident yes
                or no is wrong.

Run with --self-test to check the arithmetic against a synthetic,
hand-computable results set before trusting this on a real run.
"""

import argparse
import json
import math
import sys
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

EVAL_DIR = Path(__file__).resolve().parent

SPARK_INPUT_RATE_USD = Fraction(125, 100) / 1_000_000  # $1.25 per 1,000,000 tokens
SPARK_OUTPUT_RATE_USD = Fraction(425, 100) / 1_000_000  # $4.25 per 1,000,000 tokens
SAM_RATE_USD = Fraction(250, 100) / 1_000  # $2.50 per 1,000 images

MODE_ORDER = ["generic", "planted", "mock"]
SPLIT_ORDER = ["tuning", "heldout"]


def sort_key(order):
    def key(value):
        if value in order:
            return (0, order.index(value))
        return (1, value)

    return key


def load_questions(path):
    """Loads eval/questions.yaml, returns an ordered dict of question_id to record."""
    if yaml is None:
        raise SystemExit(
            "PyYAML is not installed. Install it with: python3 -m pip install pyyaml"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    questions = OrderedDict()
    for q in data["questions"]:
        questions[q["id"]] = q
    return questions


def read_results(path):
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


def is_probe(row):
    """True for the cannot_tell probe question (q20), where the correct
    verdict is cannot_tell and any confident yes or no is wrong, per
    questions.yaml."""
    return row.get("kind") == "cannot_tell_probe"


def is_correct(row):
    if is_probe(row):
        return row["verdict"] == "cannot_tell"
    if row["verdict"] == "cannot_tell":
        return False
    return row["verdict"] == row["label"]


def is_false_fire(row):
    return row["verdict"] == "yes" and row["label"] == "no"


def is_miss(row):
    return row["verdict"] == "no" and row["label"] == "yes"


def is_cannot_tell(row):
    return row["verdict"] == "cannot_tell"


def rate_block(rows):
    """Four rates over a set of rows: accuracy, false-fire, miss, cannot_tell.
    Returns None when there are no rows, so callers can print "no data"
    instead of dividing by zero."""
    n = len(rows)
    if n == 0:
        return None
    return {
        "n": n,
        "accuracy": Fraction(sum(1 for r in rows if is_correct(r)), n),
        "false_fire": Fraction(sum(1 for r in rows if is_false_fire(r)), n),
        "miss": Fraction(sum(1 for r in rows if is_miss(r)), n),
        "cannot_tell": Fraction(sum(1 for r in rows if is_cannot_tell(r)), n),
    }


def percentile(values, p):
    """Nearest-rank percentile. p is 0-100. Returns None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(p / 100 * len(ordered))))
    return ordered[rank - 1]


def average_cost_usd(rows):
    if not rows:
        return None
    total = Fraction(0)
    for r in rows:
        usage = r.get("usage") or {}
        total += (
            Fraction(usage.get("spark_input_tokens", 0) or 0) * SPARK_INPUT_RATE_USD
            + Fraction(usage.get("spark_output_tokens", 0) or 0) * SPARK_OUTPUT_RATE_USD
            + Fraction(usage.get("sam_images", 0) or 0) * SAM_RATE_USD
        )
    return total / len(rows)


def build_report(rows, questions, checks_per_day):
    safety_ids = {qid for qid, q in questions.items() if q.get("safety")}

    groups = OrderedDict()
    grouped_rows = OrderedDict()
    for row in rows:
        key = (row.get("mode"), row.get("split"))
        grouped_rows.setdefault(key, []).append(row)

    def group_sort_key(item):
        mode, split = item[0]
        return (sort_key(MODE_ORDER)(mode), sort_key(SPLIT_ORDER)(split))

    for key, group_rows in sorted(grouped_rows.items(), key=group_sort_key):
        mode, split = key
        overall = rate_block(group_rows)
        safety_rows = [r for r in group_rows if r.get("question_id") in safety_ids]
        safety = rate_block(safety_rows)

        per_question = OrderedDict()
        for qid in questions:
            q_rows = [r for r in group_rows if r.get("question_id") == qid]
            if q_rows:
                per_question[qid] = {
                    "n": len(q_rows),
                    "accuracy": Fraction(sum(1 for r in q_rows if is_correct(r)), len(q_rows)),
                }
            else:
                per_question[qid] = {"n": 0, "accuracy": None}

        latencies = [r["latency_ms"] for r in group_rows if r.get("latency_ms") is not None]
        latency = None
        if latencies:
            latency = {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)}

        avg_cost = average_cost_usd(group_rows)
        cost = None
        if avg_cost is not None:
            cost = {
                "avg_per_check_usd": avg_cost,
                "checks_per_day": checks_per_day,
                "cents_per_day": avg_cost * checks_per_day * 100,
            }

        errors = sum(1 for r in group_rows if r.get("error"))

        groups[key] = {
            "n": len(group_rows),
            "overall": overall,
            "safety": safety,
            "per_question": per_question,
            "latency": latency,
            "cost": cost,
            "errors": errors,
        }

    return {"groups": groups, "checks_per_day": checks_per_day}


def pct(fraction):
    if fraction is None:
        return "n/a"
    return f"{float(fraction) * 100:.1f}%"


def usd(amount, decimals=5):
    if amount is None:
        return "n/a"
    return f"${float(amount):.{decimals}f}"


def cents(amount):
    if amount is None:
        return "n/a"
    return f"{float(amount):.2f} cents"


def render_rate_table(block, label):
    lines = [f"{label}:"]
    if block is None:
        lines.append("  no data")
        return lines
    lines.append(f"  n = {block['n']}")
    lines.append(f"  accuracy = {pct(block['accuracy'])}")
    lines.append(f"  false-fire rate = {pct(block['false_fire'])}")
    lines.append(f"  miss rate = {pct(block['miss'])}")
    lines.append(f"  cannot-tell rate = {pct(block['cannot_tell'])}")
    return lines


def render_markdown(report, questions):
    out = []
    out.append("# Truss eval scorecard")
    out.append("")
    out.append("Terms, as defined in questions.yaml: false-fire means the check said")
    out.append("yes while the true answer was no. Miss means the check said no while")
    out.append("the true answer was yes. Cannot-tell is its own rate, not counted as")
    out.append("right or wrong, except on q20, the cannot-tell probe, where saying")
    out.append("cannot-tell is the correct answer and a confident yes or no is wrong.")
    out.append("")

    groups = report["groups"]

    heldout_by_mode = {
        mode: data
        for (mode, split), data in groups.items()
        if split == "heldout"
    }
    out.append("## Headline: held-out results")
    out.append("")
    if not heldout_by_mode:
        out.append("No held-out rows in this results file yet.")
    else:
        for mode, data in sorted(heldout_by_mode.items(), key=lambda kv: sort_key(MODE_ORDER)(kv[0])):
            out.append(f"Mode {mode}, held-out split, n = {data['n']}:")
            out.extend("  " + line for line in render_rate_table(data["overall"], "overall")[1:])
            out.append("")
        if "generic" in heldout_by_mode and "planted" in heldout_by_mode:
            g = heldout_by_mode["generic"]["overall"]
            p = heldout_by_mode["planted"]["overall"]
            if g and p:
                gap = p["accuracy"] - g["accuracy"]
                out.append(
                    f"Accuracy gap (planted minus generic), held-out: "
                    f"{pct(gap)} ({pct(p['accuracy'])} planted minus {pct(g['accuracy'])} generic)"
                )
        else:
            out.append(
                "Both generic and planted held-out results are needed to report the "
                "headline accuracy gap; only "
                + ", ".join(sorted(heldout_by_mode.keys()))
                + " present here."
            )
    out.append("")

    out.append("## Results by mode and split")
    out.append("")
    if not groups:
        out.append("No rows in the results file.")
    for (mode, split), data in groups.items():
        out.append(f"### Mode {mode}, split {split} (n = {data['n']})")
        out.append("")
        out.append("Overall:")
        for line in render_rate_table(data["overall"], "overall")[1:]:
            out.append("- " + line.strip())
        out.append("")
        out.append("Safety questions only (the 5 questions marked safety: true; a")
        out.append("false-fire here is a false all-clear, the expensive error):")
        for line in render_rate_table(data["safety"], "safety")[1:]:
            out.append("- " + line.strip())
        if data["safety"] is not None:
            out.append(
                f"- **safety false-fire rate (false all-clear): {pct(data['safety']['false_fire'])}**"
            )
        out.append("")
        if data["latency"]:
            out.append(
                f"Latency per check: p50 = {data['latency']['p50']} ms, "
                f"p95 = {data['latency']['p95']} ms"
            )
        else:
            out.append("Latency per check: no data")
        out.append("")
        if data["cost"]:
            c = data["cost"]
            out.append(f"Cost per check: {usd(c['avg_per_check_usd'])}")
            out.append(
                f"Assuming {c['checks_per_day']} checks per day: "
                f"{cents(c['cents_per_day'])} per day "
                f"({usd(c['cents_per_day'] / 100, decimals=4)} per day)"
            )
        else:
            out.append("Cost per check: no data")
        if data["errors"]:
            out.append(f"Frames with a recorded error: {data['errors']}")
        out.append("")
        out.append("Per-question accuracy:")
        out.append("")
        out.append("| question | n | accuracy |")
        out.append("|---|---|---|")
        for qid, qdata in data["per_question"].items():
            slug = questions.get(qid, {}).get("slug", qid)
            if qdata["n"] == 0:
                out.append(f"| {qid} {slug} | 0 | no data |")
            else:
                out.append(f"| {qid} {slug} | {qdata['n']} | {pct(qdata['accuracy'])} |")
        out.append("")

    return "\n".join(out) + "\n"


def run_score(results_path, questions_path, out_path, checks_per_day):
    questions = load_questions(questions_path)
    rows = read_results(results_path)
    report = build_report(rows, questions, checks_per_day)
    text = render_markdown(report, questions)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return report


# ----------------------------------------------------------------------
# Self-test: a synthetic results set with hand-computable outcomes.
# ----------------------------------------------------------------------


def _row(frame_id, question_id, label, verdict, kind="normal", mode="generic",
         split="heldout", latency_ms=0, spark_input_tokens=0, spark_output_tokens=0,
         sam_images=0, error=None):
    return {
        "frame_id": frame_id,
        "question_id": question_id,
        "label": label,
        "kind": kind,
        "split": split,
        "mode": mode,
        "verdict": verdict,
        "reason": "synthetic",
        "latency_ms": latency_ms,
        "usage": {
            "spark_input_tokens": spark_input_tokens,
            "spark_output_tokens": spark_output_tokens,
            "sam_images": sam_images,
        },
        "error": error,
    }


def _synthetic_questions():
    # Only the fields the scorer reads: safety and slug. Real safety flags
    # for q01, q05, q06, q20 come from the real questions.yaml too, but
    # the self-test spells them out here so it has no file dependency and
    # stays hand-computable on its own.
    return OrderedDict(
        [
            ("q01", {"id": "q01", "slug": "stove-off", "safety": True}),
            ("q05", {"id": "q05", "slug": "kitchen-faucet-off", "safety": False}),
            ("q06", {"id": "q06", "slug": "fridge-door-closed", "safety": False}),
            ("q20", {"id": "q20", "slug": "houseplant-watered", "safety": False}),
        ]
    )


def _synthetic_rows():
    rows = []

    # q05 (not safety), mode generic, split heldout, 5 rows, latencies 100..500,
    # usage 800 input / 200 output tokens, 0 sam images on every row.
    usage_a = dict(spark_input_tokens=800, spark_output_tokens=200, sam_images=0)
    rows.append(_row("q05-a", "q05", "yes", "yes", latency_ms=100, **usage_a))
    rows.append(_row("q05-b", "q05", "yes", "no", latency_ms=200, **usage_a))
    rows.append(_row("q05-c", "q05", "no", "no", latency_ms=300, **usage_a))
    rows.append(_row("q05-d", "q05", "no", "yes", latency_ms=400, **usage_a))
    rows.append(_row("q05-e", "q05", "yes", "cannot_tell", latency_ms=500, **usage_a))

    # q01 (safety), same mode and split, 5 rows, latencies 600..1000, same usage.
    rows.append(_row("q01-a", "q01", "no", "no", latency_ms=600, **usage_a))
    rows.append(_row("q01-b", "q01", "no", "yes", latency_ms=700, **usage_a))
    rows.append(_row("q01-c", "q01", "yes", "yes", latency_ms=800, **usage_a))
    rows.append(_row("q01-d", "q01", "yes", "no", latency_ms=900, **usage_a))
    rows.append(_row("q01-e", "q01", "no", "cannot_tell", latency_ms=1000, **usage_a))

    # q20, the cannot_tell probe, same mode and split, 5 rows, latencies 1100..1500.
    rows.append(_row("q20-a", "q20", "no", "cannot_tell", kind="cannot_tell_probe",
                      latency_ms=1100, **usage_a))
    rows.append(_row("q20-b", "q20", "yes", "cannot_tell", kind="cannot_tell_probe",
                      latency_ms=1200, **usage_a))
    rows.append(_row("q20-c", "q20", "no", "no", kind="cannot_tell_probe",
                      latency_ms=1300, **usage_a))
    rows.append(_row("q20-d", "q20", "yes", "yes", kind="cannot_tell_probe",
                      latency_ms=1400, **usage_a))
    rows.append(_row("q20-e", "q20", "no", "yes", kind="cannot_tell_probe",
                      latency_ms=1500, **usage_a))

    # q06 (not safety), mode planted, split tuning, 4 rows, latencies 50..350,
    # usage 500 input / 100 output tokens, 2 sam images on every row.
    usage_b = dict(spark_input_tokens=500, spark_output_tokens=100, sam_images=2)
    rows.append(_row("q06-a", "q06", "yes", "yes", mode="planted", split="tuning",
                      latency_ms=50, **usage_b))
    rows.append(_row("q06-b", "q06", "yes", "yes", mode="planted", split="tuning",
                      latency_ms=150, **usage_b))
    rows.append(_row("q06-c", "q06", "no", "yes", mode="planted", split="tuning",
                      latency_ms=250, **usage_b))
    rows.append(_row("q06-d", "q06", "no", "no", mode="planted", split="tuning",
                      latency_ms=350, **usage_b))

    return rows


def self_test():
    questions = _synthetic_questions()
    rows = _synthetic_rows()
    report = build_report(rows, questions, checks_per_day=30)
    groups = report["groups"]

    mismatches = []

    def check(label, actual, expected):
        if actual != expected:
            mismatches.append(f"{label}: expected {expected!r}, got {actual!r}")

    gh = groups.get(("generic", "heldout"))
    if gh is None:
        mismatches.append("missing group (generic, heldout)")
    else:
        check("generic/heldout n", gh["n"], 15)
        o = gh["overall"]
        check("generic/heldout overall accuracy", o["accuracy"], Fraction(6, 15))
        check("generic/heldout overall false_fire", o["false_fire"], Fraction(3, 15))
        check("generic/heldout overall miss", o["miss"], Fraction(2, 15))
        check("generic/heldout overall cannot_tell", o["cannot_tell"], Fraction(4, 15))

        s = gh["safety"]
        check("generic/heldout safety n", s["n"], 5)
        check("generic/heldout safety accuracy", s["accuracy"], Fraction(2, 5))
        check("generic/heldout safety false_fire", s["false_fire"], Fraction(1, 5))
        check("generic/heldout safety miss", s["miss"], Fraction(1, 5))
        check("generic/heldout safety cannot_tell", s["cannot_tell"], Fraction(1, 5))

        pq = gh["per_question"]
        check("generic/heldout q05 n", pq["q05"]["n"], 5)
        check("generic/heldout q05 accuracy", pq["q05"]["accuracy"], Fraction(2, 5))
        check("generic/heldout q01 accuracy", pq["q01"]["accuracy"], Fraction(2, 5))
        check("generic/heldout q20 n", pq["q20"]["n"], 5)
        check("generic/heldout q20 accuracy", pq["q20"]["accuracy"], Fraction(2, 5))
        check("generic/heldout q06 n (no data in this group)", pq["q06"]["n"], 0)
        check("generic/heldout q06 accuracy (no data)", pq["q06"]["accuracy"], None)

        check("generic/heldout latency p50", gh["latency"]["p50"], 800)
        check("generic/heldout latency p95", gh["latency"]["p95"], 1500)

        expected_cost = Fraction(800) * SPARK_INPUT_RATE_USD + Fraction(200) * SPARK_OUTPUT_RATE_USD
        check("generic/heldout avg cost per check", gh["cost"]["avg_per_check_usd"], expected_cost)
        check(
            "generic/heldout cents per day (30 checks)",
            gh["cost"]["cents_per_day"],
            expected_cost * 30 * 100,
        )

    pt = groups.get(("planted", "tuning"))
    if pt is None:
        mismatches.append("missing group (planted, tuning)")
    else:
        check("planted/tuning n", pt["n"], 4)
        o = pt["overall"]
        check("planted/tuning overall accuracy", o["accuracy"], Fraction(3, 4))
        check("planted/tuning overall false_fire", o["false_fire"], Fraction(1, 4))
        check("planted/tuning overall miss", o["miss"], Fraction(0, 4))
        check("planted/tuning overall cannot_tell", o["cannot_tell"], Fraction(0, 4))

        check("planted/tuning safety (no safety questions in this group)", pt["safety"], None)

        check("planted/tuning latency p50", pt["latency"]["p50"], 150)
        check("planted/tuning latency p95", pt["latency"]["p95"], 350)

        expected_cost = (
            Fraction(500) * SPARK_INPUT_RATE_USD
            + Fraction(100) * SPARK_OUTPUT_RATE_USD
            + Fraction(2) * SAM_RATE_USD
        )
        check("planted/tuning avg cost per check", pt["cost"]["avg_per_check_usd"], expected_cost)
        check(
            "planted/tuning cents per day (30 checks)",
            pt["cost"]["cents_per_day"],
            expected_cost * 30 * 100,
        )

    if mismatches:
        print("SELF-TEST FAILED")
        for m in mismatches:
            print(f"  {m}")
        return False

    print("PASS")
    return True


def main():
    parser = argparse.ArgumentParser(description="Score a Mannie eval results file.")
    parser.add_argument("--results", help="Path to a results JSONL file.")
    parser.add_argument(
        "--questions",
        default=str(EVAL_DIR / "questions.yaml"),
        help="Path to questions.yaml (default: eval/questions.yaml).",
    )
    parser.add_argument(
        "--out",
        default=str(EVAL_DIR / "SCORECARD.md"),
        help="Path to write the scorecard (default: eval/SCORECARD.md).",
    )
    parser.add_argument(
        "--checks-per-day",
        type=int,
        default=30,
        help="Checks per day assumption for the cost projection (default: 30).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in self-test instead of scoring a results file.",
    )
    args = parser.parse_args()

    if args.self_test:
        ok = self_test()
        sys.exit(0 if ok else 1)

    if not args.results:
        parser.error("--results is required unless --self-test is given")

    run_score(args.results, args.questions, args.out, args.checks_per_day)


if __name__ == "__main__":
    main()
