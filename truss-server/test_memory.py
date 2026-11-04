#!/usr/bin/env python3
"""
Truss Memory: GET /memory serves one question's checks for a day,
read back from the server log, in time order.

    python3 test_memory.py
"""

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import memory  # noqa: E402
import server as server_module  # noqa: E402
from questions import QuestionStore  # noqa: E402


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


SLUG = "did-i-take-my-night-pills"
QUESTION = "Did I take my night pills?"

# Tonight's real shape: a pill seen, a cropped frame, the pill seen
# again, then the empty compartment no single photo can settle.
LOG_LINES = [
    "2026-11-03 23:49:31 | check    | no question is due on this frame (each waits for its gap)",
    f"2026-11-03 23:50:30 | check    | {SLUG}: no (24.7 s) A red pill is visible in the Tue compartment of the pill organizer on the table in front of the laptop.",
    f"2026-11-03 23:50:51 | check    | {SLUG}: cannot_tell (15.0 s) The Tue PM compartment interior is cropped out at the bottom of the photo so emptiness versus a pill cannot be read.",
    f"2026-11-03 23:51:03 | check    | {SLUG}: no (6.5 s) The Tue PM compartment in the bottom row clearly contains a red pill, showing the night dose was not taken.",
    f"2026-11-03 23:52:14 | check    | {SLUG}: cannot_tell (55.3 s) The Tue PM compartment appears empty but an empty compartment looks the same whether the pill was taken or never filled.",
    "2026-11-03 23:53:00 | check    | is-the-stove-off: yes (9.1 s) All four knobs point to off.",
    "2026-11-04 00:01:00 | check    | " + SLUG + ": yes (5.0 s) The next day is another day.",
    "2026-11-03 23:54:00 | capture  | saved capture_20261103_235400_000000.jpg (150000 bytes)",
    "not a log line at all",
]


def test_parse():
    one = memory.parse_check_line(LOG_LINES[1])
    check("a check line parses", one is not None)
    check("its day and clock are kept", one["day"] == "2026-11-03" and one["time"] == "23:50:30")
    check("its verdict is kept", one["verdict"] == "no")
    check("its reason is kept whole", one["reason"].startswith("A red pill is visible"))
    check("its seconds are kept", one["seconds"] == 24.7)
    check("a line with no slug check does not parse", memory.parse_check_line(LOG_LINES[0]) is None)
    check("another tag does not parse", memory.parse_check_line(LOG_LINES[7]) is None)
    check("junk does not parse", memory.parse_check_line(LOG_LINES[8]) is None)
    failed = memory.parse_check_line(
        f"2026-11-03 23:55:00 | check    | {SLUG}: no verdict (0.0 s) check failed: boom"
    )
    check("a failed check reads as cannot_tell", failed["verdict"] == "cannot_tell")
    check("a failed check keeps its recorded reason", failed["reason"] == "check failed: boom")


def _write_logs(tmp, extra_answers=()):
    log_dir = Path(tmp)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "server.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    day = log_dir / "2026-11-03"
    day.mkdir(parents=True, exist_ok=True)
    (day / "answers.txt").write_text(
        "".join(line + "\n" for line in extra_answers), encoding="utf-8"
    )
    return log_dir


def test_timeline():
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = _write_logs(tmp)
        items = memory.timeline(log_dir, "2026-11-03", SLUG, QUESTION)
        check("the day's checks come back", len(items) == 4)
        check(
            "in time order, oldest first",
            [i["time"] for i in items] == ["23:50:30", "23:50:51", "23:51:03", "23:52:14"],
        )
        check(
            "verdicts in order",
            [i["verdict"] for i in items] == ["no", "cannot_tell", "no", "cannot_tell"],
        )
        check("the first reason is whole", items[0]["reason"].startswith("A red pill is visible"))
        check("no evidence without a firing", all(i["evidence"] is None for i in items))
        stove = memory.timeline(log_dir, "2026-11-03", "is-the-stove-off", "Is the stove off?")
        check("another question gets only its own checks", len(stove) == 1 and stove[0]["verdict"] == "yes")
    with tempfile.TemporaryDirectory() as tmp:
        check("no server log is an empty timeline", memory.timeline(tmp, "2026-11-03", SLUG, QUESTION) == [])
    try:
        memory.timeline(tempfile.mkdtemp(), "Nov 3", SLUG, QUESTION)
        check("a bad day is refused", False)
    except ValueError:
        check("a bad day is refused", True)


def test_evidence_join():
    yes_line = (
        f"2026-11-03 21:12:24 | check    | {SLUG}: yes (6.0 s) "
        "The Tue PM compartment is empty, its pill gone."
    )
    answer_line = (
        "21:12:18 | Did I take my night pills? | yes | The Tue PM compartment "
        "is empty, its pill gone. | evidence: 211218_did-i-take-my-night-pills.jpg "
        "| model: muse-spark-1.3"
    )
    refusal_line = (
        f"2026-11-03 21:30:10 | check    | {SLUG}: cannot_tell (8.0 s) "
        "The organizer is too far away to read."
    )
    refusal_answer = (
        "21:30:02 | Did I take my night pills? | cannot tell | "
        "I can't see the pill organizer from here. | "
        "evidence: 213002_did-i-take-my-night-pills.jpg | model: muse-spark-1.3"
    )
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = _write_logs(tmp, [answer_line, refusal_answer])
        server_log = log_dir / "server.log"
        server_log.write_text(
            server_log.read_text(encoding="utf-8") + yes_line + "\n" + refusal_line + "\n",
            encoding="utf-8",
        )
        items = memory.timeline(log_dir, "2026-11-03", SLUG, QUESTION)
        by_time = {i["time"]: i for i in items}
        check(
            "a fired yes joins its kept photo",
            by_time["21:12:24"]["evidence"] == "211218_did-i-take-my-night-pills.jpg",
        )
        check(
            "a spoken refusal joins its kept photo",
            by_time["21:30:10"]["evidence"] == "213002_did-i-take-my-night-pills.jpg",
        )
        check(
            "a plain no never joins a photo",
            by_time["23:51:03"]["evidence"] is None,
        )


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")


def _status(port, path):
    try:
        _get(port, path).read()
        return 200
    except urllib.error.HTTPError as e:
        return e.code


def _serve():
    httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_endpoint():
    tmp = Path(tempfile.mkdtemp())
    log_dir = _write_logs(tmp / "log")
    store = QuestionStore(tmp / "questions")
    slug = store.create(QUESTION)
    saved_log, saved_store = server_module.LOG_DIR, server_module.STORE
    server_module.LOG_DIR, server_module.STORE = log_dir, store
    httpd, port = _serve()
    try:
        with _get(port, f"/memory?slug={slug}&day=2026-11-03") as resp:
            body = json.loads(resp.read())
        check("the endpoint names the question", body["question"] == QUESTION and body["slug"] == slug)
        check(
            "the endpoint serves the checks in order",
            [c["verdict"] for c in body["checks"]] == ["no", "cannot_tell", "no", "cannot_tell"],
        )
        check(
            "the endpoint serves the recorded reason",
            body["checks"][0]["reason"].startswith("A red pill is visible"),
        )
        check("an unknown slug is a 404", _status(port, "/memory?slug=no-such-question") == 404)
        check("a slug with a path in it is a 404", _status(port, "/memory?slug=..%2Fsecret") == 404)
        check("a missing slug is a 404", _status(port, "/memory") == 404)
        check("a bad day is a 400", _status(port, f"/memory?slug={slug}&day=Nov+3") == 400)
    finally:
        httpd.shutdown()
        server_module.LOG_DIR, server_module.STORE = saved_log, saved_store


def main():
    test_parse()
    test_timeline()
    test_evidence_join()
    test_endpoint()
    print("ALL PASS")


if __name__ == "__main__":
    main()
