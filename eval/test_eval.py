#!/usr/bin/env python3
"""Offline tests for the eval harness: the drop-in manifest, the runner
over the product checker, first_numbers.py end to end against a fake
llama-server and a fake SAM service, and the scorer's new terms.

    python3 eval/test_eval.py      prints ALL PASS

No network beyond 127.0.0.1, no model, no photos of anyone's home.
"""

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))

import build_manifest  # noqa: E402
import first_numbers  # noqa: E402
import run_eval  # noqa: E402
import score  # noqa: E402

JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEB"
    "AQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX"
    "/9k="
)

FAILED = []


def check(name, ok):
    print(("ok: " if ok else "FAIL: ") + name)
    if not ok:
        FAILED.append(name)


def quiet(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


# -- fakes -------------------------------------------------------------------------


class FakeModel:
    """A llama-server stand-in. Answers /v1/models with served ids and
    /v1/chat/completions by schema name: the compile gets looks, the
    verdict gets self.verdict (or a 500 while self.fail_next > 0)."""

    def __init__(self, served=("muse-glimmer-30b",)):
        self.served = list(served)
        self.verdict = "yes"
        self.fail_next = 0
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    return self._send(200, {"object": "list", "data": [{"id": m} for m in fake.served]})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = body.get("response_format", {}).get("json_schema", {}).get("name")
                fake.calls.append((name, body))
                if name == "plant_compile":
                    reply = {"prompts": ["stove", "knob"], "yes_look": "All knobs point up.",
                             "no_look": "One knob is turned.", "decides": "The knob positions."}
                else:
                    if fake.fail_next > 0:
                        fake.fail_next -= 1
                        return self._send(500, {"error": "boom"})
                    reply = {"verdict": fake.verdict, "reason": "The knobs point up.", "missing": ""}
                self._send(200, {
                    "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def verdict_calls(self):
        return [c for c in self.calls if c[0] == "verdict"]


class FakeSam:
    def __init__(self):
        fake = self
        self.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, {"ok": True, "model": "sam3.1", "device": "cuda"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.seen.append(body)
                phrases = body["phrases"]
                self._send(200, {
                    "model": "sam3.1",
                    "phrases": [{"phrase": p, "detections": [{"box": [0, 0, 1, 1], "score": 0.9}]}
                                for p in phrases[:1]] + [{"phrase": p, "detections": []} for p in phrases[1:]],
                    "union": {"count": 1, "phrases_matched": phrases[:1], "area": 1, "box": [0, 0, 1, 1]},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


def make_dataset(root, with_refs=("q01",)):
    """q01 (safety) as folder 'q01': 1 yes, 1 no, 1 cannot_tell.
    q05 as folder 'kitchen-faucet-off': 4 yes (so one is held out), no refs."""
    ds = root / "dataset"
    q01 = ds / "q01"
    for label, names in {"yes": ["day-01.jpg"], "no": ["evening-01.jpg"],
                         "cannot_tell": ["knobs-hidden.jpg"]}.items():
        (q01 / label).mkdir(parents=True)
        for n in names:
            (q01 / label / n).write_bytes(JPEG)
    q05 = ds / "kitchen-faucet-off"
    (q05 / "yes").mkdir(parents=True)
    for n in ["a.jpg", "b.jpg", "c.jpg", "d.JPG"]:
        (q05 / "yes" / n).write_bytes(JPEG)
    for qid, folder in (("q01", q01), ("q05", q05)):
        if qid in with_refs:
            (folder / "ref-yes.jpg").write_bytes(JPEG)
            (folder / "ref-no.jpg").write_bytes(JPEG)
    return ds


# -- tests -------------------------------------------------------------------------


def test_build_manifest(root):
    questions = build_manifest.load_questions()
    ds = make_dataset(root)
    (ds / "q01" / "yes" / "phone.HEIC").write_bytes(b"x")
    (ds / "q01" / "ref_yes.jpg").write_bytes(JPEG)
    (ds / "q01" / "maybe").mkdir()
    (ds / "not-a-question").mkdir()
    rows, notes, folders = build_manifest.rebuild(ds, questions)
    by_id = {r["frame_id"]: r for r in rows}
    check("folders named q01 and by slug both map to their question",
          set(folders) == {"q01", "q05"} and len(rows) == 7)
    check("cannot_tell is a label", by_id["q01-cannot_tell-knobs-hidden"]["label"] == "cannot_tell")
    check("lighting comes from the name",
          by_id["q01-yes-day-01"]["lighting"] == "day"
          and by_id["q01-no-evening-01"]["lighting"] == "evening"
          and by_id["q05-yes-a"]["lighting"] == "unknown")
    splits = [by_id[f"q05-yes-{n}"]["split"] for n in "abcd"]
    check("first three per state are tuning, the rest held out",
          splits == ["tuning", "tuning", "tuning", "heldout"])
    check("HEIC is named, not used", any("HEIC" in n.upper() and "JPEG" in n for n in notes))
    check("a misnamed reference is named", any("ref_yes.jpg" in n for n in notes))
    check("an unknown subfolder is named", any("maybe/" in n for n in notes))
    check("an unknown question folder is named", any("not-a-question/" in n for n in notes))
    check("the manifest is written", len(run_eval.read_jsonl(ds / "manifest.jsonl")) == 7)

    # A capture.py row survives a rebuild with its own split and kind.
    manifest = ds / "manifest.jsonl"
    old = run_eval.read_jsonl(manifest)
    for r in old:
        if r["frame_id"] == "q05-yes-d":
            r["split"], r["kind"], r["frame_id"] = "tuning", "hard_negative", "kept-id"
    build_manifest.write_manifest(manifest, old)
    (ds / "kitchen-faucet-off" / "yes" / "a.jpg").unlink()
    rows, notes, _ = build_manifest.rebuild(ds, questions)
    by_id = {r["frame_id"]: r for r in rows}
    check("a known row keeps its id, split and kind",
          by_id.get("kept-id", {}).get("kind") == "hard_negative"
          and by_id["kept-id"]["split"] == "tuning")
    check("a removed photo's row is dropped and named",
          "q05-yes-a" not in by_id and any("a.jpg" in n and "dropped" in n for n in notes))
    lines = build_manifest.status_lines(rows, questions, build_manifest.scan(ds, questions)[2])
    check("the status names safety questions still missing frames",
          any("Safety questions with no frames yet" in l and "q02" in l and "q01" not in l
              for l in lines))


def test_local_endpoint():
    check("the DGX's LAN address is local", run_eval.endpoint_is_local("http://192.168.0.214:8080/v1"))
    check("loopback is local", run_eval.endpoint_is_local("http://127.0.0.1:8080/v1"))
    check("a bare host name is local", run_eval.endpoint_is_local("http://spark-2516:8080/v1"))
    check("the Meta Model API is not", not run_eval.endpoint_is_local("https://api.meta.ai/v1"))


def env(model, sam=None):
    os.environ["TRUSS_BASE_URL"] = model.url
    os.environ["TRUSS_MODEL"] = "muse-glimmer-30b"
    os.environ["TRUSS_TIMEOUT_S"] = "10"
    os.environ["TRUSS_SAM_URL"] = sam.url if sam else "http://127.0.0.1:9"
    os.environ.pop("TRUSS_API_KEY", None)
    os.environ.pop("MODEL_API_KEY", None)


def test_first_numbers(root):
    ds = make_dataset(root)
    model = FakeModel()
    sam = FakeSam()
    env(model, sam)
    run_dir = EVAL_DIR / "runs" / "_test"
    try:
        model.fail_next = 1
        code, out = quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test", "--yes"])
        check("the run finishes", code == 0)
        rows = run_eval.read_jsonl(run_dir / "results.jsonl")
        generic = [r for r in rows if r["mode"] == "generic"]
        planted = [r for r in rows if r["mode"] == "planted"]
        check("generic checks every frame", len(generic) == 7)
        check("planted checks only questions with reference photos (q01)",
              len(planted) == 3 and {r["question_id"] for r in planted} == {"q01"})
        check("q05 without references is named as skipped", "q05 kitchen-faucet-off: no reference photos" in out)
        folder = ds / "q01"
        check("looks.txt and prompts.txt are compiled from the references",
              "knob" in (folder / "looks.txt").read_text().lower()
              and (folder / "prompts.txt").read_text().split() == ["stove", "knob"])
        check("planted checks carry the looks text",
              any("All knobs point up" in json.dumps(b["messages"]) for n, b in model.verdict_calls()))
        check("generic checks carry no looks text",
              sum("All knobs point up" in json.dumps(b["messages"]) for n, b in model.verdict_calls()) == 3)
        failed = [r for r in rows if r.get("failure")]
        check("a server error is a failed check, read cannot_tell",
              len(failed) == 1 and failed[0]["verdict"] == "cannot_tell")
        check("rows say the model and that it is local",
              all(r["model"] == "muse-glimmer-30b" and r["local"] for r in rows))
        sam_rows = run_eval.read_jsonl(run_dir / "sam.jsonl")
        check("SAM runs once per frame", len(sam_rows) == 7 and all(r["found"] for r in sam_rows))
        check("SAM uses the compiled object names where there are some",
              any(r["prompts"] == ["stove", "knob"] for r in sam_rows if r["question_id"] == "q01")
              and all(r["prompts"] == ["kitchen faucet"] for r in sam_rows if r["question_id"] == "q05"))
        card = (run_dir / "SCORECARD.md").read_text()
        check("the scorecard is written and copied to eval/SCORECARD.md",
              (EVAL_DIR / "SCORECARD.md").read_text() == card)
        check("the scorecard says the model and no per-call cost",
              "Model: muse-glimmer-30b." in card and "runs locally on the DGX" in card)
        check("the scorecard has a SAM section", "## SAM on the DGX" in card and "sam3.1" in card)
        check("a yes on a frame not in view is a false all-clear on a safety question",
              "safety false all-clear rate (yes while the truth is no or not in view): 66.7%" in card)

        # Rerun: nothing new, except the one failed check is retried.
        calls_before = len(model.verdict_calls())
        code, out = quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test", "--yes"])
        rows = run_eval.read_jsonl(run_dir / "results.jsonl")
        check("a rerun only retries the failed check",
              len(model.verdict_calls()) - calls_before == 1 and len(rows) == 10
              and not any(r.get("failure") for r in rows))
        check("a rerun does not recompile unchanged references",
              sum(1 for c in model.calls if c[0] == "plant_compile") == 1)

        # New reference photo: recompiled, its planted rows redone.
        (ds / "q01" / "ref-no.jpg").write_bytes(JPEG + b"\x00")
        calls_before = len(model.verdict_calls())
        quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test", "--yes"])
        check("a changed reference photo recompiles and redoes that question's planted rows",
              sum(1 for c in model.calls if c[0] == "plant_compile") == 2
              and len(model.verdict_calls()) - calls_before == 3)

        # A removed photo leaves the scorecard.
        (ds / "q01" / "cannot_tell" / "knobs-hidden.jpg").unlink()
        quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test", "--yes"])
        rows = run_eval.read_jsonl(run_dir / "results.jsonl")
        check("a removed photo's rows are dropped",
              not any("knobs-hidden" in r["frame_id"] for r in rows) and len(rows) == 8)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_refusals(root):
    ds = make_dataset(root)
    wrong = FakeModel(served=("deepseek-v4-flash-vision",))
    env(wrong)
    code, out = quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test2", "--yes", "--no-sam"])
    check("a server serving another model is refused before any check",
          code == 2 and "not muse-glimmer-30b" in out and not wrong.calls)
    os.environ["TRUSS_BASE_URL"] = "http://127.0.0.1:9/v1"
    code, out = quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test2", "--yes", "--no-sam"])
    check("no model server stops the run with how to start it",
          code == 2 and "nothing answered" in out and "bringup.sh serve" in out)
    model = FakeModel()
    env(model)
    os.environ["TRUSS_MODEL"] = "muse-spark-1.3-contributor"
    code, out = quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test2", "--yes", "--no-sam"])
    check("the contributor tier is refused", code == 2 and not model.calls)
    env(model)  # SAM pointed at a closed port
    code, out = quiet(first_numbers.main, ["--dataset", str(ds), "--run", "_test2", "--yes",
                                           "--modes", "generic"])
    check("SAM down skips SAM only", code == 0 and "Skipped: nothing answered" in out
          and not (EVAL_DIR / "runs" / "_test2" / "sam.jsonl").exists())
    shutil.rmtree(EVAL_DIR / "runs" / "_test2", ignore_errors=True)
    empty = root / "empty"
    empty.mkdir()
    code, out = quiet(first_numbers.main, ["--dataset", str(empty), "--run", "_test2", "--yes"])
    check("no photos says how to drop them in", code == 1 and "cannot_tell/*.jpg" in out)
    shutil.rmtree(EVAL_DIR / "runs" / "_test2", ignore_errors=True)


def test_mock_runner(root):
    ds = make_dataset(root)
    questions = run_eval.load_questions(EVAL_DIR / "questions.yaml")
    rows, _, _ = build_manifest.rebuild(ds, questions)
    out = root / "mock.jsonl"
    run_eval.run_mode(rows, questions, "mock", out)
    run_eval.run_mode(rows, questions, "mock", out, resume=True)
    got = run_eval.read_jsonl(out)
    check("mock mode runs and a resume adds nothing", len(got) == 7)
    check("mock never answers yes or no on a cannot_tell frame",
          all(r["verdict"] == "cannot_tell" for r in got if r["label"] == "cannot_tell"))
    run_eval.run_mode(rows, questions, "planted", out, resume=True)
    got = [r for r in run_eval.read_jsonl(out) if r["mode"] == "planted"]
    check("planted without looks.txt is a setup error, never a verdict",
          len(got) == 7 and all(r["error"] and r["verdict"] == "cannot_tell" for r in got))


def test_scorer_terms():
    q = {"q01": {"slug": "stove-off", "safety": True}, "q05": {"slug": "faucet", "safety": False}}

    def row(fid, qid, label, verdict, mode="generic", failure=None):
        return {"frame_id": fid, "question_id": qid, "label": label, "kind": "normal",
                "split": "heldout", "mode": mode, "verdict": verdict, "latency_ms": 1000,
                "usage": {"spark_input_tokens": 10, "spark_output_tokens": 2, "sam_images": 0},
                "failure": failure, "error": None, "local": True, "model": "muse-glimmer-30b"}

    rows = [
        row("a", "q01", "cannot_tell", "cannot_tell"),
        row("b", "q01", "cannot_tell", "yes"),
        row("c", "q01", "no", "yes"),
        row("d", "q01", "yes", "yes"),
        row("e", "q05", "yes", "cannot_tell", failure="timeout"),
        row("a", "q01", "cannot_tell", "cannot_tell", mode="planted"),
        row("b", "q01", "cannot_tell", "cannot_tell", mode="planted"),
        row("x", "q05", "yes", "yes", mode="planted"),
    ]
    rep = score.build_report(rows, q, 30)
    g = rep["groups"][("generic", "heldout")]
    check("cannot_tell on a frame labelled cannot_tell is correct", g["overall"]["accuracy"] == score.Fraction(2, 5))
    check("safety false all-clear counts yes on no and on not-in-view",
          g["safety"]["false_all_clear"] == score.Fraction(2, 4))
    check("false-fire keeps its meaning (yes on no only)", g["safety"]["false_fire"] == score.Fraction(1, 4))
    check("guesses on not-in-view frames are counted", g["overall"]["guesses"] == 1)
    check("failed checks are counted apart", g["overall"]["failed"] == 1 and g["failures"] == {"timeout": 1})
    check("latency leaves failed checks out", g["latency"]["p95"] == 1000)
    check("a local group has no price", g["local"] and g["cost"] is None)
    gap = rep["gap"]
    check("the planted gap is paired on frames both modes ran",
          gap["n"] == 2 and gap["planted"] == 1 and gap["generic"] == score.Fraction(1, 2))
    check("the scorer's own self-test still passes", quiet(score.self_test)[0])


def main():
    root = Path(tempfile.mkdtemp())
    saved = dict(os.environ)
    # first_numbers.py copies its scorecard to eval/SCORECARD.md; the
    # tests put the real one back.
    card = EVAL_DIR / "SCORECARD.md"
    card_backup = card.read_bytes() if card.exists() else None
    try:
        for name, fn in [("manifest", test_build_manifest), ("first", test_first_numbers),
                         ("refusals", test_refusals), ("mock", test_mock_runner)]:
            sub = root / name
            sub.mkdir()
            fn(sub)
        test_local_endpoint()
        test_scorer_terms()
    finally:
        if card_backup is not None:
            card.write_bytes(card_backup)
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(root, ignore_errors=True)
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
