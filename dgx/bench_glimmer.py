#!/usr/bin/env python3
"""
The Glimmer bring-up bench (work item D): 10 glasses photos x 3
questions, each checked in single and multi mode through the real
Truss checker (truss-server/verdict.py over endpoint.py).

    python3 dgx/bench_glimmer.py dgx/bench-set.json
    python3 dgx/bench_glimmer.py SET --modes single --max-tokens 600

The endpoint comes from the same environment the server reads
(TRUSS_BASE_URL, default http://127.0.0.1:8080/v1; TRUSS_MODEL;
TRUSS_API_KEY; TRUSS_TIMEOUT_S), so from the laptop:

    TRUSS_BASE_URL=http://<dgx>:8080/v1 python3 dgx/bench_glimmer.py SET

Reported, per mode:
    prefill   llama-server's prompt_ms (image encoding and prompt)
    decode    llama-server's predicted_ms (thinking and the reply)
    total     wall time of the whole check, retries included
    the dflash drafter's acceptance, when llama-server reports it
    cannot_tell rate on the frames labelled cannot_tell (deliberately
              uninformative), and on the rest
    failed checks: no verdict came back at all (timeout, bad JSON,
              reply cut off by max_tokens). These read cannot_tell in
              the server, but they are counted apart here, because a
              failure is not the model saying it cannot see
    multi vs single agreement, photo by photo, with the disagreements
              listed

The set file is JSON; paths are relative to it. See
dgx/bench-set.example.json. A question gives its text and two reference
photos (yes, no) and optionally looks (or looks_file), or names a
planted question folder (folder), from which question.txt, ref-yes.jpg,
ref-no.jpg and looks.txt are read. A photo gives labels per question id:
yes, no, or cannot_tell (the frame does not show what decides it).
"uninformative": true labels every question cannot_tell for that photo.
Labels are optional; without them only timings and agreement report.

Writes rows.jsonl (one line per check) and summary.md into the output
folder, and prints the summary. Standard library only.
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "truss-server"))

import endpoint  # noqa: E402
import verdict  # noqa: E402

LABELS = ("yes", "no", "cannot_tell")
NO_CHECK = "no check was made"


# -- llama-server timings -----------------------------------------------------
#
# endpoint.ask_json() keeps only token counts from a reply. llama-server
# also returns a "timings" object (prompt_ms, predicted_ms, draft_n,
# ...) and the finish_reason; the bench reads those by wrapping the one
# function that posts, without changing what the checker sends.

_calls = []
_real_post = endpoint._post


def _recording_post(url, body, api_key, timeout):
    status, text = _real_post(url, body, api_key, timeout)
    call = {"status": status, "response_format": "response_format" in body}
    try:
        data = json.loads(text)
        timings = data.get("timings") or {}
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        call["timings"] = {k: v for k, v in timings.items() if isinstance(v, (int, float))}
        call["finish_reason"] = choice.get("finish_reason")
        reasoning = message.get("reasoning_content")
        call["reasoning_chars"] = len(reasoning) if isinstance(reasoning, str) else 0
        content = message.get("content")
        call["content_chars"] = len(content) if isinstance(content, str) else 0
    except Exception:
        call["timings"] = {}
    _calls.append(call)
    return status, text


endpoint._post = _recording_post


# -- the set ------------------------------------------------------------------


def _read_text(path):
    try:
        return Path(path).read_text().strip() or None
    except OSError:
        return None


def load_set(path):
    path = Path(path).resolve()
    base = path.parent
    data = json.loads(path.read_text())
    questions = []
    for q in data["questions"]:
        qid = q["id"]
        if q.get("folder"):
            folder = base / q["folder"]
            text = q.get("question") or _read_text(folder / "question.txt")
            yes, no = folder / "ref-yes.jpg", folder / "ref-no.jpg"
            looks = q.get("looks") or verdict.read_looks(folder)
        else:
            text = q.get("question")
            yes, no = base / q["yes"], base / q["no"]
            looks = q.get("looks") or (_read_text(base / q["looks_file"]) if q.get("looks_file") else None)
        if not text:
            raise SystemExit(f"Question {qid} has no question text.")
        for ref in (yes, no):
            if not ref.exists():
                raise SystemExit(f"Question {qid}: reference photo {ref} is missing.")
        questions.append({"id": qid, "question": text, "yes": yes, "no": no, "looks": looks})
    ids = {q["id"] for q in questions}
    photos = []
    for p in data["photos"]:
        file = base / p["file"]
        if not file.exists():
            raise SystemExit(f"Photo {file} is missing.")
        labels = dict(p.get("labels") or {})
        if p.get("uninformative"):
            labels = {qid: "cannot_tell" for qid in ids}
        for qid, label in labels.items():
            if qid not in ids or label not in LABELS:
                raise SystemExit(f"Photo {p['file']}: bad label {qid}={label}.")
        photos.append({"file": file, "name": p["file"], "labels": labels})
    return questions, photos


# -- one check ----------------------------------------------------------------


def run_check(photo, q, mode, cfg):
    _calls.clear()
    if mode == "multi":
        result = verdict.check(
            photo["file"], q["question"], mode="multi",
            ref_yes=q["yes"], ref_no=q["no"], config=cfg,
        )
    else:
        result = verdict.check(
            photo["file"], q["question"], mode="single", looks=q["looks"], config=cfg,
        )

    def total(key):
        return sum(c["timings"].get(key, 0) for c in _calls)

    return {
        "photo": photo["name"],
        "question": q["id"],
        "mode": mode,
        "model": result["model"],
        "label": photo["labels"].get(q["id"]),
        "verdict": result["verdict"],
        "reason": result["reason"],
        "missing": result["missing"],
        "failed": NO_CHECK in result["reason"],
        "total_ms": result["latency_ms"],
        "prefill_ms": round(total("prompt_ms")),
        "prompt_tokens": total("prompt_n"),
        "cached_tokens": total("cache_n"),
        "decode_ms": round(total("predicted_ms")),
        "decode_tokens": total("predicted_n"),
        "draft_tokens": total("draft_n"),
        "draft_accepted": total("draft_n_accepted"),
        "requests": len(_calls),
        "statuses": [c["status"] for c in _calls],
        "finish_reasons": [c.get("finish_reason") for c in _calls],
        "reasoning_chars": sum(c.get("reasoning_chars", 0) for c in _calls),
        "has_timings": any(c["timings"] for c in _calls),
    }


# -- the report ---------------------------------------------------------------


def _pct(n, d):
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a (0)"


def _dist(values):
    values = sorted(values)
    if not values:
        return "n/a"
    p95 = values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]
    return (
        f"p50 {statistics.median(values) / 1000:.1f} s, "
        f"p95 {p95 / 1000:.1f} s, mean {statistics.mean(values) / 1000:.1f} s"
    )


def summarize(rows, cfg, max_tokens):
    out = [
        "# Glimmer bring-up bench",
        "",
        f"Endpoint {cfg.base_url}, model {cfg.model}, max_tokens {max_tokens}, "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}.",
        "",
    ]
    for mode in ("single", "multi"):
        mine = [r for r in rows if r["mode"] == mode]
        if not mine:
            continue
        ok = [r for r in mine if not r["failed"]]
        out += [f"## Mode {mode} ({len(mine)} checks)", ""]
        out.append(f"- failed checks (no verdict came back): {_pct(len(mine) - len(ok), len(mine))}")
        cut = sum(1 for r in mine if "length" in r["finish_reasons"])
        retried = sum(1 for r in mine if r["requests"] > 1)
        out.append(f"- replies cut off by max_tokens: {cut}; checks that needed the retry: {retried}")
        timed = [r for r in ok if r["has_timings"]]
        out.append(f"- prefill: {_dist([r['prefill_ms'] for r in timed])}")
        out.append(f"- decode: {_dist([r['decode_ms'] for r in timed])}")
        out.append(f"- total: {_dist([r['total_ms'] for r in ok])}")
        dec_ms = sum(r["decode_ms"] for r in timed)
        dec_n = sum(r["decode_tokens"] for r in timed)
        if dec_ms:
            out.append(
                f"- decode speed {1000 * dec_n / dec_ms:.1f} tok/s; "
                f"mean {dec_n / len(timed):.0f} tokens per check (thinking included); "
                f"mean prompt {sum(r['prompt_tokens'] for r in timed) / len(timed):.0f} tokens"
            )
        drafted = sum(r["draft_tokens"] for r in timed)
        if drafted:
            out.append(f"- drafter acceptance: {_pct(sum(r['draft_accepted'] for r in timed), drafted)}")
        else:
            out.append("- drafter: no draft tokens reported (not loaded, or not used)")
        counts = {v: sum(1 for r in ok if r["verdict"] == v) for v in LABELS}
        out.append(f"- verdicts: yes {counts['yes']}, no {counts['no']}, cannot_tell {counts['cannot_tell']}")
        blind = [r for r in ok if r["label"] == "cannot_tell"]
        seen = [r for r in ok if r["label"] in ("yes", "no")]
        blind_failed = sum(1 for r in mine if r["failed"] and r["label"] == "cannot_tell")
        if blind or blind_failed:
            out.append(
                "- **cannot_tell on uninformative frames: "
                f"{_pct(sum(1 for r in blind if r['verdict'] == 'cannot_tell'), len(blind))}**"
                f" (plus {blind_failed} failed checks on those frames, not counted)"
            )
        if seen:
            out.append(f"- cannot_tell on informative frames: {_pct(sum(1 for r in seen if r['verdict'] == 'cannot_tell'), len(seen))}")
            out.append(f"- right on informative frames: {_pct(sum(1 for r in seen if r['verdict'] == r['label']), len(seen))}")
        if blind or seen:
            wrong_yes = [r for r in ok if r["verdict"] == "yes" and r["label"] in ("no", "cannot_tell")]
            out.append(f"- **yes where the label was no or cannot_tell: {len(wrong_yes)}**")
            for r in wrong_yes:
                out.append(f"  - {r['photo']} / {r['question']} (label {r['label']}): {r['reason']}")
        out.append("")

    single = {(r["photo"], r["question"]): r for r in rows if r["mode"] == "single" and not r["failed"]}
    multi = {(r["photo"], r["question"]): r for r in rows if r["mode"] == "multi" and not r["failed"]}
    both = sorted(set(single) & set(multi))
    if both:
        agree = [k for k in both if single[k]["verdict"] == multi[k]["verdict"]]
        out += ["## Multi vs single", "", f"- agreement: {_pct(len(agree), len(both))}"]
        labelled = [k for k in both if single[k]["label"]]
        if labelled:
            out.append(
                f"- right, single: {_pct(sum(1 for k in labelled if single[k]['verdict'] == single[k]['label']), len(labelled))}; "
                f"multi: {_pct(sum(1 for k in labelled if multi[k]['verdict'] == multi[k]['label']), len(labelled))}"
            )
        for k in both:
            if k not in agree:
                label = single[k]["label"] or "unlabelled"
                out.append(
                    f"- {k[0]} / {k[1]} ({label}): single {single[k]['verdict']}, multi {multi[k]['verdict']}"
                )
        out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Time and score Glimmer through the Truss checker.")
    parser.add_argument("set", help="the bench set JSON (see bench-set.example.json)")
    parser.add_argument("--modes", default="single,multi", help="comma-separated: single, multi")
    parser.add_argument("--out", help="output folder (default dgx/results/<time>)")
    parser.add_argument(
        "--max-tokens", type=int, default=verdict.MAX_TOKENS,
        help=f"reply cap per check, thinking included (the checker's is {verdict.MAX_TOKENS})",
    )
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for m in modes:
        if m not in verdict.MODES:
            raise SystemExit(f"Unknown mode {m}.")
    verdict.MAX_TOKENS = args.max_tokens
    questions, photos = load_set(args.set)
    cfg = endpoint.config_from_env()
    out_dir = Path(args.out) if args.out else Path(__file__).resolve().parent / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    n = len(photos) * len(questions) * len(modes)
    print(f"{len(photos)} photos x {len(questions)} questions x {len(modes)} modes = {n} checks "
          f"against {cfg.model} at {cfg.base_url}", file=sys.stderr)
    rows = []
    started = time.monotonic()
    with open(out_dir / "rows.jsonl", "w") as f:
        for photo in photos:
            for q in questions:
                for mode in modes:
                    row = run_check(photo, q, mode, cfg)
                    rows.append(row)
                    f.write(json.dumps(row) + "\n")
                    f.flush()
                    flag = " FAILED" if row["failed"] else ""
                    print(
                        f"[{len(rows)}/{n}] {row['photo']} {row['question']} {mode}: "
                        f"{row['verdict']} in {row['total_ms'] / 1000:.1f} s "
                        f"(prefill {row['prefill_ms'] / 1000:.1f}, decode {row['decode_ms'] / 1000:.1f}){flag}",
                        file=sys.stderr,
                    )
    summary = summarize(rows, cfg, args.max_tokens)
    summary += f"\nWall time for the run: {(time.monotonic() - started) / 60:.1f} min.\n"
    (out_dir / "summary.md").write_text(summary)
    print(summary)
    print(f"Rows and summary in {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
