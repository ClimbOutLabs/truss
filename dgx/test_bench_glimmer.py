#!/usr/bin/env python3
"""
Tests for the Glimmer bench (bench_glimmer.py), against a stub
llama-server. Run:  python3 dgx/test_bench_glimmer.py

Stdlib only. The stub answers like llama-server: a timings object,
a finish_reason, and reasoning_content beside the content. It says
cannot_tell for any photo whose bytes contain "blind", answers multi
checks (three images) yes and single checks no on photo 02 so the
agreement report has a disagreement, and cuts off the reply on photo
03's stove check to stand in for a max_tokens truncation. A stove
check asks for the knob report too, so the stub adds one that backs
its verdict ("Is the stove off?": yes is every knob off).
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


class Stub(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append(body)
        parts = body["messages"][1]["content"]
        images = [base64.b64decode(p["image_url"]["url"].split(",", 1)[1]) for p in parts if p["type"] == "image_url"]
        frame = images[-1]
        question = parts[0]["text"]
        finish = "stop"
        if b"blind" in frame:
            reply = {"verdict": "cannot_tell", "reason": "Nothing relevant is in view.", "missing": "The object."}
        elif b"photo02" in frame:
            v = "yes" if len(images) == 3 else "no"
            reply = {"verdict": v, "reason": "Knobs are visible.", "missing": ""}
        else:
            reply = {"verdict": "yes", "reason": "It is visible.", "missing": ""}
        schema = body.get("response_format", {}).get("json_schema", {}).get("schema", {})
        if "knobs_in_view" in schema.get("properties", {}):
            reply = {
                "knobs_in_view": {"yes": "all", "no": "all"}.get(reply["verdict"], "none"),
                "knobs": {
                    "yes": [{"where": "front", "position": "off"}],
                    "no": [{"where": "front", "position": "on"}],
                }.get(reply["verdict"], []),
                **reply,
            }
        content = json.dumps(reply)
        if b"photo03" in frame and "stove" in question:
            content, finish = "", "length"
        out = {
            "choices": [{"message": {"content": content, "reasoning_content": "thinking..."}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120},
            "timings": {
                "cache_n": 100, "prompt_n": 800, "prompt_ms": 1500.0,
                "predicted_n": 120, "predicted_ms": 6000.0,
                "draft_n": 80, "draft_n_accepted": 60,
            },
        }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "refs").mkdir()
    (tmp / "photos").mkdir()
    for name in ("stove-yes", "stove-no", "door-yes", "door-no"):
        (tmp / "refs" / f"{name}.jpg").write_bytes(b"\xff\xd8" + name.encode() + b"\xff\xd9")
    photos = []
    for i in range(1, 5):
        tag = b"blind" if i == 4 else f"photo{i:02d}".encode()
        (tmp / "photos" / f"{i:02d}.jpg").write_bytes(b"\xff\xd8" + tag + b"\xff\xd9")
    photos = [
        {"file": "photos/01.jpg", "labels": {"stove": "yes", "door": "cannot_tell"}},
        {"file": "photos/02.jpg", "labels": {"stove": "no", "door": "no"}},
        {"file": "photos/03.jpg", "labels": {"stove": "yes", "door": "yes"}},
        {"file": "photos/04.jpg", "uninformative": True},
    ]
    questions = [
        {"id": "stove", "question": "Is the stove off?", "yes": "refs/stove-yes.jpg",
         "no": "refs/stove-no.jpg", "looks": "Knobs up means off."},
        {"id": "door", "question": "Is the front door locked?", "yes": "refs/door-yes.jpg",
         "no": "refs/door-no.jpg"},
    ]
    (tmp / "set.json").write_text(json.dumps({"questions": questions, "photos": photos}))

    env = dict(os.environ, TRUSS_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1", TRUSS_TIMEOUT_S="10")
    env.pop("TRUSS_API_KEY", None)
    env.pop("MODEL_API_KEY", None)
    run = subprocess.run(
        [sys.executable, str(HERE / "bench_glimmer.py"), str(tmp / "set.json"), "--out", str(tmp / "out"),
         "--max-tokens", "500"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    print(run.stdout)
    if run.returncode:
        print(run.stderr[-3000:])
    check("the bench exits cleanly", run.returncode == 0)
    rows = [json.loads(l) for l in (tmp / "out" / "rows.jsonl").read_text().splitlines()]
    check("4 photos x 2 questions x 2 modes = 16 rows", len(rows) == 16)
    check("rows name the model", all(r["model"] == "muse-glimmer-30b" for r in rows))
    firsts = [b for b in Stub.seen if b.get("max_tokens") == 500]
    check("max_tokens override reaches the first request of every check", len(firsts) == 16)
    multi = [b for b in Stub.seen if sum(p["type"] == "image_url" for p in b["messages"][1]["content"]) == 3]
    check("multi checks send three images (one retried)", len(multi) == 9)
    single_stove = [b for b in Stub.seen if len(b["messages"][1]["content"]) == 2 and "stove" in b["messages"][1]["content"][0]["text"]]
    check("single checks carry the looks text", all("Knobs up" in b["messages"][0]["content"] for b in single_stove))
    good = next(r for r in rows if r["photo"] == "photos/01.jpg" and r["mode"] == "single" and r["question"] == "stove")
    check("prefill and decode come from llama-server timings",
          good["prefill_ms"] == 1500 and good["decode_ms"] == 6000 and good["requests"] == 1)
    cut = [r for r in rows if r["photo"] == "photos/03.jpg" and r["question"] == "stove"]
    check("a cut-off reply is a failed check, retried once",
          all(r["failed"] and r["requests"] == 2 and "length" in r["finish_reasons"] for r in cut))
    summary = (tmp / "out" / "summary.md").read_text()
    check("the summary counts the cut-off replies", "replies cut off by max_tokens: 1" in summary)
    check("the summary reports cannot_tell on uninformative frames", "cannot_tell on uninformative frames: 67% (2/3)** (plus 0 failed checks on those frames, not counted)" in summary)
    check("the summary reports a wrong yes", "photos/01.jpg / door (label cannot_tell)" in summary)
    check("the summary reports the drafter", "drafter acceptance: 75%" in summary)
    check("the summary lists the multi/single disagreement",
          "photos/02.jpg / stove (no): single no, multi yes" in summary)
    check("failed checks stay out of agreement", "agreement: 71% (5/7)" in summary)
    server.shutdown()

    # Every uninformative check failed: the safety line must still show.
    sys.path.insert(0, str(HERE))
    import bench_glimmer
    cfg = bench_glimmer.endpoint.config_from_env()
    failed_rows = [
        {"mode": "single", "photo": f"p{i}", "question": "stove", "label": "cannot_tell",
         "verdict": "cannot_tell", "reason": "x So no check was made.", "failed": True, "failure": "truncated",
         "finish_reasons": ["length"], "requests": 2, "has_timings": False, "total_ms": 1}
        for i in range(3)
    ]
    text = bench_glimmer.summarize(failed_rows, cfg, 300)
    check("an all-failed uninformative set still shows the safety line",
          "cannot_tell on uninformative frames: n/a (0)** (plus 3 failed checks" in text)
    print("all bench tests passed")


if __name__ == "__main__":
    main()
