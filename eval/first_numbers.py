#!/usr/bin/env python3
"""The first eval numbers, in one command, on the DGX.

    python3 eval/first_numbers.py

What it does, in order, saying each step as it goes:

  1. Builds eval/dataset/manifest.jsonl from the photos dropped into
     eval/dataset/ (see build_manifest.py for the layout) and prints
     what each question has.
  2. Checks that the model endpoint answers and serves the Meta model
     the checker is set to (TRUSS_MODEL, default muse-glimmer-30b). A
     server that answers with any other model is refused, so the
     numbers can never come from the wrong model.
  3. Checks that SAM answers (TRUSS_SAM_URL, default
     http://127.0.0.1:8090). If it does not, the SAM part is skipped
     with one line, and the verdict numbers still run.
  4. Compiles each question's looks.txt and prompts.txt from its two
     reference photos, through the product's own plant-time compile
     (truss-server/plant_compile.py), once per pair of photos.
  5. Runs every frame through the product's own checker
     (truss-server/verdict.py) in each mode: generic (frame and
     question only) and planted (plus looks.txt, the product default).
     --modes adds planted-photos (the two reference photos as images).
  6. Asks SAM once per frame whether it finds the question's objects.
  7. Writes the scorecard to eval/runs/<run>/SCORECARD.md and copies it
     to eval/SCORECARD.md, and prints the headline.

Results are kept as they are made (eval/runs/<run>/results.jsonl and
sam.jsonl). Run the same command again after adding photos and only
the new frames are checked; checks that never got an answer
(timeouts, an unreachable server) are retried. --fresh starts
the run over. A question whose reference photos change is recompiled
and its planted rows are redone.

On the DGX, with Glimmer serving (./dgx/bringup.sh serve) and SAM
serving (./dgx/sam/setup.sh serve), the defaults point at both. From
the laptop instead:

    TRUSS_BASE_URL=http://<dgx-ip>:8080/v1 \\
    TRUSS_SAM_URL=http://<dgx-ip>:8090 python3 eval/first_numbers.py

Needs PyYAML (sudo apt install python3-yaml). Nothing else outside the
standard library.
"""

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(REPO / "truss-server"))

import build_manifest  # noqa: E402
import checker  # noqa: E402
import endpoint  # noqa: E402
import plant_compile  # noqa: E402
import run_eval  # noqa: E402
import sam_client  # noqa: E402
import score  # noqa: E402

DEFAULT_MODES = ("generic", "planted")
SECONDS_PER_CHECK_GUESS = 10  # Glimmer on the DGX took 5-16 s per new photo
COMPILED_FROM = ".looks-from"  # fingerprint of the reference photos looks.txt came from


def say(line=""):
    print(line, flush=True)


# -- step 2: the model endpoint --------------------------------------------------


def _get_json(url, timeout=10, headers=None):
    # endpoint's opener refuses redirects, so a key is never re-sent
    # somewhere the configured URL did not name.
    req = urllib.request.Request(url, headers=headers or {})
    with endpoint._OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def served_models(base_url, api_key=None):
    """The model ids the endpoint lists, or raises with a plain reason.
    The key goes only where the checker would send it (https or this
    machine); llama-server on the DGX needs none."""
    headers = {}
    if api_key and endpoint._key_allowed(base_url):
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        reply = _get_json(base_url.rstrip("/") + "/models", headers=headers)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{base_url}/models answered HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", None) or e
        raise RuntimeError(f"nothing answered at {base_url} ({reason})")
    ids = []
    for entry in (reply.get("data") or reply.get("models") or []) if isinstance(reply, dict) else []:
        if isinstance(entry, dict):
            for key in ("id", "model", "name"):
                if isinstance(entry.get(key), str):
                    ids.append(entry[key])
    return sorted(set(ids))


def check_endpoint(config):
    """None when the endpoint serves config.model; else the reason it
    is refused."""
    try:
        ids = served_models(config.base_url, config.api_key)
    except RuntimeError as e:
        return str(e)
    if config.model not in ids:
        listed = ", ".join(ids) or "nothing"
        return (
            f"{config.base_url} serves {listed}, not {config.model}. The eval only "
            "runs against the Meta model the checker is set to."
        )
    return None


# -- step 3: SAM ----------------------------------------------------------------


def check_sam(sam_config):
    """(model name, None) when SAM answers, else (None, reason)."""
    try:
        reply = _get_json(sam_config.url + "/health", timeout=10)
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", None) or e
        return None, f"nothing answered at {sam_config.url} ({reason})"
    if not isinstance(reply, dict) or not reply.get("ok"):
        return None, f"{sam_config.url}/health did not say ok"
    return str(reply.get("model") or "sam"), None


# -- step 4: compile -------------------------------------------------------------


def question_folders(rows):
    """{question_id: folder} for the folders the manifest's frames are in."""
    out = {}
    for row in rows:
        out.setdefault(row["question_id"], run_eval.question_dir_for_row(row))
    return out


def _write_if_changed(path, text):
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def compile_looks(folders, questions, recompile=False, compile_fn=None):
    """Compile looks.txt and prompts.txt for every question folder with
    both reference photos. Returns ({qid: "fresh" | "kept" | reason},
    {qid that was recompiled})."""
    compile_fn = compile_fn or plant_compile.compile_question
    status = {}
    changed = set()
    for qid, folder in sorted(folders.items()):
        # plant_compile reads the question's words from question.txt.
        _write_if_changed(folder / "question.txt", questions[qid]["question"] + "\n")
        fingerprint = plant_compile.looks_fingerprint(folder)
        if fingerprint is None:
            status[qid] = "no reference photos (ref-yes.jpg and ref-no.jpg)"
            continue
        try:
            compiled_from = (folder / COMPILED_FROM).read_text().strip()
        except OSError:
            compiled_from = ""
        if (
            not recompile
            and compiled_from == fingerprint
            and checker.verdict.read_looks(folder)
        ):
            status[qid] = "kept"
            continue
        say(f"  compiling {qid} {questions[qid]['slug']} ...")
        result = compile_fn(folder)
        if result.get("ok"):
            _write_if_changed(folder / COMPILED_FROM, fingerprint + "\n")
            status[qid] = "fresh"
            changed.add(qid)
            say("    " + result["looks"].rstrip().replace("\n", "\n    "))
        else:
            # A failed compile leaves no stale looks.txt in use.
            for name in ("looks.txt", COMPILED_FROM):
                try:
                    (folder / name).unlink()
                except OSError:
                    pass
            status[qid] = result.get("reason") or "compile failed"
            changed.add(qid)
    return status, changed


def refs_fingerprint(folder):
    return plant_compile.looks_fingerprint(folder)


# -- results upkeep ----------------------------------------------------------------


def prune(path, keep):
    """Rewrite a results file keeping only rows for which keep(row)."""
    rows = run_eval.read_jsonl(path)
    kept = [r for r in rows if keep(r)]
    if len(kept) != len(rows):
        tmp = Path(path).with_name(f".{Path(path).name}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, path)
    return len(rows) - len(kept)


# -- step 6: SAM pass ------------------------------------------------------------


def sam_prompts_for(folder, question):
    phrases = plant_compile.compiled_prompts(folder)
    return phrases or list(question.get("sam_prompts") or [])


def run_sam(rows, questions, folders, out_path, sam_config, locate=None):
    locate = locate or checker.locate
    done = set()
    for r in run_eval.read_jsonl(out_path):
        if not r.get("error"):
            done.add(r.get("frame_id"))
    prune(out_path, lambda r: not r.get("error"))
    todo = [r for r in rows if r["frame_id"] not in done]
    with open(out_path, "a", encoding="utf-8") as f:
        for index, row in enumerate(todo, start=1):
            qid = row["question_id"]
            prompts = sam_prompts_for(folders[qid], questions[qid])
            found = locate(run_eval.EVAL_DIR / row["path"], prompts, config=sam_config)
            out = {
                "frame_id": row["frame_id"],
                "question_id": qid,
                "label": row.get("label"),
                "split": row.get("split"),
                "prompts": prompts,
                "found": found["found"],
                "count": found["count"],
                "top_score": found["top_score"],
                "phrases": found["phrases"],
                "sam_model": found.get("model"),
                "latency_ms": found["latency_ms"],
                "error": found["error"],
            }
            f.write(json.dumps(out) + "\n")
            f.flush()
            say(
                f"  [{index}/{len(todo)}] sam {row['frame_id']}: "
                + (f"failed ({found['error']})" if found["error"] else
                   f"{'found' if found['found'] else 'nothing'} ({found['latency_ms']} ms)")
            )
    return len(todo)


# -- the run -----------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="The first Truss eval numbers, in one command.")
    parser.add_argument("--run", default="first", help="Run name; results go in eval/runs/<run>/ (default: first).")
    parser.add_argument(
        "--modes", default=",".join(DEFAULT_MODES),
        help="Comma-separated: generic, planted, planted-photos (default: generic,planted).",
    )
    parser.add_argument("--split", default="all", choices=["tuning", "heldout", "all"])
    parser.add_argument("--questions", default=None, help="Only these question ids, comma-separated (e.g. q01,q02).")
    parser.add_argument("--dataset", default=str(build_manifest.DATASET_DIR))
    parser.add_argument("--no-sam", action="store_true", help="Skip the SAM part.")
    parser.add_argument("--recompile", action="store_true", help="Compile looks.txt again for every question.")
    parser.add_argument("--fresh", action="store_true", help="Start this run over instead of resuming it.")
    parser.add_argument("--yes", action="store_true", help="Do not stop to show the time estimate.")
    args = parser.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in ("generic", "planted", "planted-photos")]
    if bad or not modes:
        parser.error(f"unknown mode(s): {', '.join(bad) or '(none)'}")

    questions = build_manifest.load_questions()

    say("1. Photos")
    rows, notes, _ = build_manifest.rebuild(args.dataset, questions)
    for note in notes:
        say("   Note: " + note)
    if args.questions:
        wanted = {q.strip() for q in args.questions.split(",") if q.strip()}
        rows = [r for r in rows if r["question_id"] in wanted]
    if args.split != "all":
        rows = [r for r in rows if r.get("split") == args.split]
    if not rows:
        say(f"   No frames to check in {args.dataset}. Drop photos in like this, then run again:")
        say("     eval/dataset/q01-stove-off/ref-yes.jpg, ref-no.jpg")
        say("     eval/dataset/q01-stove-off/yes/*.jpg, no/*.jpg, cannot_tell/*.jpg")
        say("   (python3 eval/build_manifest.py --check shows what it sees.)")
        return 1
    _, _, folders_all = build_manifest.scan(args.dataset, questions)
    for line in build_manifest.status_lines(rows, questions, folders_all):
        say("   " + line)
    folders = question_folders(rows)

    say("2. Model")
    try:
        config = endpoint.config_from_env()
    except endpoint.EndpointError as e:
        say(f"   Stopped: {e}")
        return 2
    problem = check_endpoint(config)
    if problem:
        say(f"   Stopped: {problem}")
        say("   On the DGX: ./dgx/bringup.sh serve (and ./dgx/bringup.sh status).")
        return 2
    say(f"   {config.model} answers at {config.base_url}.")

    say("3. SAM")
    sam_config = None
    if args.no_sam:
        say("   Skipped (--no-sam).")
    else:
        sam_config = sam_client.config_from_env()
        sam_model, why = check_sam(sam_config)
        if why:
            say(f"   Skipped: {why}. Start it with ./dgx/sam/setup.sh serve, or pass --no-sam.")
            sam_config = None
        else:
            say(f"   {sam_model} answers at {sam_config.url}.")

    run_dir = EVAL_DIR / "runs" / args.run
    results_path = run_dir / "results.jsonl"
    sam_path = run_dir / "sam.jsonl"
    if args.fresh and run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    say("4. Looks (plant-time compile from the reference photos)")
    status, recompiled = (
        compile_looks(folders, questions, args.recompile)
        if any(m in ("planted", "planted-photos") for m in modes) else ({}, set())
    )
    for qid, state in sorted(status.items()):
        if state not in ("kept", "fresh"):
            say(f"   {qid} {questions[qid]['slug']}: {state}; planted modes skip it.")
    say(f"   {sum(1 for s in status.values() if s in ('kept', 'fresh'))} of {len(folders)} questions have looks.")

    # A question whose reference photos changed was recompiled; its
    # planted rows were made from the old photos or words, so redo them.
    current = {r["frame_id"] for r in rows}
    refs_now = {qid: refs_fingerprint(f) for qid, f in folders.items()}
    prune(results_path, lambda r: not (
        r.get("mode") in ("planted", "planted-photos") and r.get("question_id") in recompiled
    ))

    plans = []
    for mode in modes:
        if mode == "generic":
            mode_rows = rows
        elif mode == "planted":
            mode_rows = [r for r in rows if status.get(r["question_id"]) in ("kept", "fresh")]
        else:
            mode_rows = [r for r in rows if refs_now.get(r["question_id"])]
        plans.append((mode, mode_rows))
    done_before = {
        (r.get("mode"), r.get("frame_id"))
        for r in run_eval.read_jsonl(results_path) if not run_eval.needs_retry(r)
    }
    to_check = sum(1 for mode, mrows in plans for r in mrows if (mode, r["frame_id"]) not in done_before)
    say(
        f"5. Checks: {to_check} to run across {', '.join(modes)}; at about "
        f"{SECONDS_PER_CHECK_GUESS} s each, roughly {to_check * SECONDS_PER_CHECK_GUESS / 60:.0f} min."
    )
    if to_check and not args.yes and sys.stdin.isatty():
        try:
            input("   Press Enter to start (Ctrl-C to stop; a rerun picks up where it left off). ")
        except (EOFError, KeyboardInterrupt):
            say("")
            return 130

    for mode, mode_rows in plans:
        if not mode_rows:
            say(f"   {mode}: no frames (no reference photos or looks yet).")
            continue
        say(f"   {mode}: {len(mode_rows)} frames")
        run_eval.run_mode(
            mode_rows, questions, mode, results_path, resume=True,
            progress=run_eval.print_progress,
        )
    # Score only what is on disk now, in the modes asked for.
    prune(results_path, lambda r: r.get("frame_id") in current and r.get("mode") in modes)

    say("6. SAM")
    if sam_config is None:
        say("   Skipped.")
    else:
        prune(sam_path, lambda r: r.get("frame_id") in current)
        run_sam(rows, questions, folders, sam_path, sam_config)

    say("7. Scorecard")
    card = run_dir / "SCORECARD.md"
    report = score.run_score(
        str(results_path), str(build_manifest.QUESTIONS_PATH), str(card), 30,
        sam_path=str(sam_path) if sam_path.exists() else None, quiet=True,
    )
    shutil.copyfile(card, EVAL_DIR / "SCORECARD.md")
    say(f"   Written to {card.relative_to(REPO)} and eval/SCORECARD.md.")
    say("")
    say("Every frame:")
    for mode, data in report["every"].items():
        o = data["overall"]
        line = (
            f"  {mode:<15} n={o['n']:<4} accuracy {score.pct(o['accuracy'])}, "
            f"cannot tell {score.pct(o['cannot_tell'])}"
        )
        if data["safety"]:
            line += f", safety false all-clear {score.pct(data['safety']['false_all_clear'])}"
        if o["failed"]:
            line += f", {o['failed']} failed checks"
        say(line)
    if report.get("gap_all"):
        g = report["gap_all"]
        say(f"  planted minus generic, {g['n']} paired frames: {score.pct(g['gap'])}")
    latencies = [
        f"{mode} {data['latency']['p50'] / 1000:.1f} s"
        for (mode, split), data in report["groups"].items() if data["latency"]
    ]
    if latencies:
        say("  time per check (p50, by mode and split): " + ", ".join(latencies))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to pick up where it left off.")
        sys.exit(130)
