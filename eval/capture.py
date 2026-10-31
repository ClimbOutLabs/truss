#!/usr/bin/env python3
"""Capture tool for the Mannie eval.

A tiny standalone HTTP server, standard library only, on port 8793.
Port 8791 is Aman's, port 8792 is Mannie's day-1 server. This stays on
8793 so it never collides with either.

It exists so the staging session is button presses, not file shuffling:

POST /frame?question=<slug>&label=<yes|no|cannot_tell>&lighting=<day|evening>
     &kind=<normal|hard_negative|cannot_tell_probe>
  Body: a JPEG. Saves it into the dataset tree under the next free
  sequence name and appends one manifest row with the correct split.

POST /ref?question=<slug>&which=<yes|no>
  Body: a JPEG. Saves it as ref-yes.jpg or ref-no.jpg for that question.

GET /status
  Returns counts per question, label, and split, so the person staging
  can see what is still missing.

A POST for a question slug that is not in questions.yaml is refused
with a plain one-line error. An existing frame file is never
overwritten; a second POST of the same frame is refused instead.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import yaml
except ImportError:
    yaml = None

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVAL_DIR / "questions.yaml"
DATASET_DIR = EVAL_DIR / "dataset"
MANIFEST_PATH = DATASET_DIR / "manifest.jsonl"

PORT = 8793
VALID_LABELS = {"yes", "no", "cannot_tell"}
REF_LABELS = {"yes", "no"}
VALID_LIGHTINGS = {"day", "evening"}
VALID_KINDS = {"normal", "hard_negative", "cannot_tell_probe"}
TUNING_FRAMES_PER_STATE = 3


def load_question_slugs():
    if yaml is None:
        raise SystemExit(
            "PyYAML is not installed. Install it with: python3 -m pip install pyyaml"
        )
    with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    by_slug = {}
    for q in data["questions"]:
        by_slug[q["slug"]] = q["id"]
    return by_slug


def question_dir(question_id, slug):
    return DATASET_DIR / f"{question_id}-{slug}"


def count_existing_frames(question_id, slug, label):
    """How many frame files already exist for this question and label,
    across every lighting, so we know the next sequence number and the
    tuning or heldout split."""
    folder = question_dir(question_id, slug) / label
    if not folder.exists():
        return 0
    return len([p for p in folder.iterdir() if p.is_file()])


def append_manifest_row(row):
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def read_manifest_rows():
    if not MANIFEST_PATH.exists():
        return []
    rows = []
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[capture] {self.address_string()} {format % args}")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_line(self, status, message):
        body = (message.rstrip("\n") + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/status":
            self._handle_status()
        else:
            self._send_error_line(404, "Not found.")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/frame":
            self._handle_frame(parsed)
        elif parsed.path == "/ref":
            self._handle_ref(parsed)
        else:
            self._send_error_line(404, "Not found.")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _handle_frame(self, parsed):
        params = parse_qs(parsed.query)
        question_slug = (params.get("question") or [""])[0]
        label = (params.get("label") or [""])[0]
        lighting = (params.get("lighting") or [""])[0]
        kind = (params.get("kind") or ["normal"])[0]

        slugs = load_question_slugs()
        if question_slug not in slugs:
            self._send_error_line(400, f"Unknown question slug: {question_slug}")
            return
        if label not in VALID_LABELS:
            self._send_error_line(400, f"label must be one of {sorted(VALID_LABELS)}")
            return
        if lighting not in VALID_LIGHTINGS:
            self._send_error_line(400, f"lighting must be one of {sorted(VALID_LIGHTINGS)}")
            return
        if kind not in VALID_KINDS:
            self._send_error_line(400, f"kind must be one of {sorted(VALID_KINDS)}")
            return

        body = self._read_body()
        if not body:
            self._send_error_line(400, "Empty body; expected a JPEG.")
            return

        question_id = slugs[question_slug]
        folder = question_dir(question_id, question_slug) / label
        folder.mkdir(parents=True, exist_ok=True)

        existing = count_existing_frames(question_id, question_slug, label)
        seq = existing + 1
        frame_name = f"{lighting}-{seq:02d}.jpg"
        frame_path = folder / frame_name

        if frame_path.exists():
            self._send_error_line(
                409, f"Frame already exists, not overwriting: {frame_path.relative_to(EVAL_DIR)}"
            )
            return

        frame_path.write_bytes(body)

        split = "tuning" if existing < TUNING_FRAMES_PER_STATE else "heldout"
        frame_id = f"{question_id}-{label}-{lighting}-{seq:02d}"
        row = {
            "frame_id": frame_id,
            "question_id": question_id,
            "label": label,
            "kind": kind,
            "lighting": lighting,
            "split": split,
            "path": str(frame_path.relative_to(EVAL_DIR)),
        }
        append_manifest_row(row)
        self._send_json(201, {"saved": str(frame_path.relative_to(EVAL_DIR)), "manifest_row": row})

    def _handle_ref(self, parsed):
        params = parse_qs(parsed.query)
        question_slug = (params.get("question") or [""])[0]
        which = (params.get("which") or [""])[0]

        slugs = load_question_slugs()
        if question_slug not in slugs:
            self._send_error_line(400, f"Unknown question slug: {question_slug}")
            return
        if which not in REF_LABELS:
            self._send_error_line(400, "which must be one of ['no', 'yes']")
            return

        body = self._read_body()
        if not body:
            self._send_error_line(400, "Empty body; expected a JPEG.")
            return

        question_id = slugs[question_slug]
        folder = question_dir(question_id, question_slug)
        folder.mkdir(parents=True, exist_ok=True)
        ref_path = folder / f"ref-{which}.jpg"

        if ref_path.exists():
            self._send_error_line(
                409, f"Reference already exists, not overwriting: {ref_path.relative_to(EVAL_DIR)}"
            )
            return

        ref_path.write_bytes(body)
        self._send_json(201, {"saved": str(ref_path.relative_to(EVAL_DIR))})

    def _handle_status(self):
        slugs = load_question_slugs()
        rows = read_manifest_rows()
        counts = {}
        for slug, qid in slugs.items():
            counts[qid] = {
                "slug": slug,
                "yes": {"tuning": 0, "heldout": 0},
                "no": {"tuning": 0, "heldout": 0},
                "cannot_tell": {"tuning": 0, "heldout": 0},
                "ref_yes": question_dir(qid, slug).joinpath("ref-yes.jpg").exists(),
                "ref_no": question_dir(qid, slug).joinpath("ref-no.jpg").exists(),
            }
        for row in rows:
            qid = row.get("question_id")
            label = row.get("label")
            split = row.get("split")
            if qid in counts and label in VALID_LABELS and split in ("tuning", "heldout"):
                counts[qid][label][split] += 1
        self._send_json(200, {"questions": counts, "total_frames": len(rows)})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Capture server listening on port {PORT}.")
    print(f"Dataset tree: {DATASET_DIR}")
    print("POST /frame?question=...&label=yes|no|cannot_tell&lighting=day|evening&kind=normal (JPEG body)")
    print("POST /ref?question=...&which=yes|no (JPEG body)")
    print("GET /status")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
