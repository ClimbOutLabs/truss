#!/usr/bin/env python3
"""
Tests for the laptop's SAM client and the evidence outline in the
check loop, against a stub SAM service.

Run from the truss-server folder:  python3 test_sam_client.py
Stdlib only. Exits non-zero on the first failure.
"""

import base64
import json
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sam_client  # noqa: E402
from checkloop import CheckLoop  # noqa: E402
from questions import QuestionStore  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"
OUTLINED = b"\xff\xd8\xff\xe0" + b"outlined" + b"\xff\xd9"


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


class Stub(BaseHTTPRequestHandler):
    """Answers like sam_service.py: a match for any phrase containing
    "stove", nothing for anything else. A phrase "boom" gets a 500."""

    seen = []
    status = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if "slow" in body["phrases"]:
            import time
            time.sleep(1.0)
        if "boom" in body["phrases"]:
            return self._send(500, {"error": "SAM failed: RuntimeError"})
        if Stub.status:
            return self._send(Stub.status, {"error": "no"})
        matched = [p for p in body["phrases"] if "stove" in p]
        reply = {
            "model": "sam3",
            "width": 4,
            "height": 3,
            "phrases": [{"phrase": p, "detections": []} for p in body["phrases"]],
            "union": {"count": len(matched), "phrases_matched": matched, "area": 2,
                      "box": [1, 1, 3, 2] if matched else None},
            "timing_ms": {"total": 12.5},
        }
        if self.path == "/outline":
            reply["image_b64"] = base64.b64encode(OUTLINED).decode() if matched else None
        self._send(200, reply)

    def _send(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, sam_client.SamConfig(f"http://127.0.0.1:{server.server_port}", "lan-key", 5.0)


def photo(tmp, name="frame.jpg"):
    p = Path(tmp) / name
    p.write_bytes(JPEG)
    return p


def test_question_phrases():
    check("the thing is left of the question", sam_client.question_phrases("Is the TV on?") == ["tv"])
    check("question and state words dropped",
          sam_client.question_phrases("Did I take my night pills?") == ["pills"])
    check("place and lock words dropped",
          sam_client.question_phrases("Is the back door locked?") == ["door"])
    check("numbers dropped", sam_client.question_phrases("is my hr below 160") == ["hr"])
    check("possessives dropped", sam_client.question_phrases("Is Sam's laptop closed?") == ["laptop"])
    check("not text is nothing", sam_client.question_phrases(None) == [])
    check("forget is not a thing", sam_client.question_phrases("Did I forget to turn off the heater?") == ["heater"])
    check("-ing states dropped", sam_client.question_phrases("Is the kettle boiling?") == ["kettle"])
    check("weekdays dropped", sam_client.question_phrases("Did I take my Tuesday pills?") == ["pills"])
    check("at most three", len(sam_client.question_phrases("cup plate fork knife spoon")) == 3)


def test_read_prompts():
    tmp = Path(tempfile.mkdtemp())
    check("no prompts.txt reads as none", sam_client.read_prompts(tmp) == [])
    (tmp / "prompts.txt").write_text("stove\n\n  stove   knob \nstove\n")
    check("prompts are one per line, tidied, without repeats",
          sam_client.read_prompts(tmp) == ["stove", "stove knob"])
    check("a bad folder reads as none", sam_client.read_prompts(None) == [])


def test_segment_sends_photo_phrases_and_key(cfg):
    tmp = tempfile.mkdtemp()
    Stub.seen.clear()
    reply = sam_client.segment(photo(tmp), ["stove", "knob"], threshold=0.4, config=cfg)
    sent = Stub.seen[-1]
    check("segment posts to /segment", sent["path"] == "/segment")
    check("the photo's bytes go as base64", base64.b64decode(sent["body"]["image_b64"]) == JPEG)
    check("phrases and threshold are sent", sent["body"]["phrases"] == ["stove", "knob"]
          and sent["body"]["threshold"] == 0.4 and sent["body"]["masks"] is True)
    check("the SAM key rides in the Authorization header", sent["auth"] == "Bearer lan-key")
    check("a match is a match", sam_client.has_match(reply))
    reply = sam_client.segment(photo(tmp), ["kettle"], config=cfg)
    check("no match is no match", not sam_client.has_match(reply))
    check("has_match is false on junk", not sam_client.has_match({"union": {"count": "3"}})
          and not sam_client.has_match(None))


def test_outline_writes_only_on_a_match(cfg):
    tmp = Path(tempfile.mkdtemp())
    src = photo(tmp, "120000_stove.jpg")
    dest = sam_client.outline_path(src)
    check("outline path sits beside the evidence", dest == tmp / "120000_stove_outline.jpg")
    reply = sam_client.outline(src, ["stove"], dest, config=cfg)
    check("a match writes the outlined photo", reply["written"] and dest.read_bytes() == OUTLINED)
    check("the image is not kept in the reply", "image_b64" not in reply)
    check("the evidence photo is untouched", src.read_bytes() == JPEG)
    check("no temp file is left", sorted(p.name for p in tmp.iterdir())
          == ["120000_stove.jpg", "120000_stove_outline.jpg"])
    dest.unlink()
    reply = sam_client.outline(src, ["kettle"], dest, config=cfg)
    check("no match writes nothing", not reply["written"] and not dest.exists())


def test_errors_are_sam_errors(cfg):
    tmp = tempfile.mkdtemp()
    for name, fn in [
        ("a 500", lambda: sam_client.segment(photo(tmp), ["boom"], config=cfg)),
        ("no service", lambda: sam_client.segment(
            photo(tmp), ["stove"], config=sam_client.SamConfig("http://127.0.0.1:9", None, 1.0))),
    ]:
        try:
            fn()
            check(f"{name} raises", False)
        except sam_client.SamError:
            check(f"{name} is a SamError", True)
    Stub.status = 401
    try:
        sam_client.segment(photo(tmp), ["stove"], config=cfg)
        check("a refused key raises", False)
    except sam_client.SamError as e:
        check("a refused key names TRUSS_SAM_KEY", "TRUSS_SAM_KEY" in str(e))
    finally:
        Stub.status = None


def test_rle_helpers():
    # 3 rows x 4: row 0 ....  row 1 .##.  row 2 ###.
    rle = {"size": [3, 4], "counts": [5, 2, 1, 3, 1]}
    check("rle area", sam_client.rle_area(rle) == 5)
    check("all inside a box around it", sam_client.rle_outside_box(rle, [0, 1, 3, 3]) == 0)
    check("pixels left of the box count as outside",
          sam_client.rle_outside_box(rle, [1, 1, 3, 3]) == 1)
    check("rows above the box count as outside",
          sam_client.rle_outside_box(rle, [0, 2, 4, 3]) == 2)
    check("a fractional box covers the pixels it touches",
          sam_client.rle_outside_box(rle, [0.5, 1.2, 2.1, 2.5]) == 0)
    starts_on = {"size": [1, 3], "counts": [0, 2, 1]}
    check("a mask starting on its first pixel", sam_client.rle_runs(starts_on) == [(0, 2)])


def test_outliner_phrases_and_failures(cfg):
    tmp = Path(tempfile.mkdtemp())
    a, b, c = tmp / "a", tmp / "b", tmp / "c"
    for f in (a, b, c):
        f.mkdir()
    (a / "prompts.txt").write_text("stove\nknob\n")
    (b / "prompts.txt").write_text("knob\ngas stove\n")
    entries = [{"ref_yes_path": str(a / "ref-yes.jpg")}, {"ref_yes_path": str(b / "ref-yes.jpg")},
               {"ref_yes_path": str(c / "ref-yes.jpg")}, {"slug": "no-path"}]
    logs = []
    out = sam_client.EvidenceOutliner(config=cfg, log_fn=logs.append, background=False)
    check("phrases are the union across the fired questions",
          out.phrases_for(entries) == ["stove", "knob", "gas stove"])
    src = photo(tmp, "evidence.jpg")
    where = out(src, entries)
    check("the outliner says where the outline goes", where == str(tmp / "evidence_outline.jpg"))
    check("and writes it there", Path(where).read_bytes() == OUTLINED)
    check("the outline is logged with the phrases matched", "stove, gas stove" in logs[-1])
    check("no prompts means no call and no outline", out(src, entries[2:]) is None
          and "no prompts.txt" in logs[-1])
    worded = {"slug": "is-the-tv-on", "question": "Is the TV on?", "ref_yes_path": str(c / "ref-yes.jpg")}
    check("no prompts.txt falls back to the question's words", out.phrases_for([worded]) == ["tv"]
          and "from its words: tv" in logs[-1])
    check("compiled phrases win over the words",
          out.phrases_for([dict(worded, ref_yes_path=str(a / "ref-yes.jpg"))]) == ["stove", "knob"])
    (c / "prompts.txt").write_text("boom\n")
    (tmp / "evidence_outline.jpg").unlink()
    where = out(src, entries[2:3])
    check("a SAM failure is logged, not raised", where is not None and "500" in logs[-1])
    check("a SAM failure leaves no outline file", not (tmp / "evidence_outline.jpg").exists())


def loop_rig(outline_fn):
    tmp = Path(tempfile.mkdtemp())
    store = QuestionStore(tmp / "questions")
    slug = store.create("Is the stove off?", {"kind": "interval", "minutes": 0})
    store.save_ref(slug, "yes", JPEG)
    store.save_ref(slug, "no", JPEG)
    spoken = []

    def check_fn(frame_path, entry):
        return {"verdict": "yes", "reason": "The knobs point to off."}

    loop = CheckLoop(store, check_fn, tmp / "log",
                     speak_fn=lambda text, entry, evidence: spoken.append(text),
                     now_fn=datetime.now,
                     outline_fn=outline_fn)
    return tmp, store, slug, loop, spoken


def test_check_loop_outline():
    calls = []

    def outline_fn(evidence, entries):
        calls.append((Path(evidence), [e["slug"] for e in entries]))
        return str(sam_client.outline_path(evidence))

    tmp, store, slug, loop, spoken = loop_rig(outline_fn)
    result = loop.process_frame(photo(tmp))
    check("a firing asks for the outline of its evidence",
          len(calls) == 1 and calls[0][1] == [slug] and calls[0][0].exists())
    check("the outline path is in the result", result["outline"].endswith("_outline.jpg"))
    check("the answer is still spoken", len(spoken) == 1 and result["errors"] == 0)

    def broken(evidence, entries):
        raise RuntimeError("SAM exploded")

    tmp, store, slug, loop, spoken = loop_rig(broken)
    result = loop.process_frame(photo(tmp))
    check("a broken outline step never stops the answer",
          len(spoken) == 1 and result["fired"] == [slug] and result["outline"] is None
          and result["errors"] == 1)
    check("the evidence is kept anyway", Path(result["evidence"]).exists())

    tmp, store, slug, loop, spoken = loop_rig(None)
    result = loop.process_frame(photo(tmp))
    check("no outline function, no outline", result["outline"] is None and len(spoken) == 1)


def test_outliner_caps_and_names(cfg):
    tmp = Path(tempfile.mkdtemp())
    entries = []
    for i in range(4):
        f = tmp / f"q{i}"
        f.mkdir()
        (f / "prompts.txt").write_text(f"stove{i}\nknob{i}\nlid{i}\n")
        entries.append({"ref_yes_path": str(f / "ref-yes.jpg")})
    logs = []
    out = sam_client.EvidenceOutliner(config=cfg, log_fn=logs.append, background=False)
    phrases = out.phrases_for(entries)
    check("phrases from many questions are capped at the service's limit, first-fired first",
          len(phrases) == sam_client.MAX_PHRASES and phrases[:3] == ["stove0", "knob0", "lid0"])
    Stub.seen.clear()
    out(photo(tmp, "ev.jpg"), entries)
    check("so four questions firing together still get their outline",
          len(Stub.seen[-1]["body"]["phrases"]) == sam_client.MAX_PHRASES
          and (tmp / "ev_outline.jpg").exists())
    check("the outline log line names the SAM model", "model: sam3" in logs[-1])


def test_slow_sam_never_piles_up(cfg):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "q").mkdir()
    (tmp / "q" / "prompts.txt").write_text("slow\nstove\n")
    entries = [{"ref_yes_path": str(tmp / "q" / "ref-yes.jpg")}]
    logs = []
    out = sam_client.EvidenceOutliner(config=cfg, log_fn=logs.append, max_in_flight=1)
    import time
    t = time.perf_counter()
    first = out(photo(tmp, "a.jpg"), entries)
    second = out(photo(tmp, "b.jpg"), entries)
    check("the outline call returns at once even with a slow SAM", time.perf_counter() - t < 0.5)
    check("one outline runs; the next is skipped while SAM is busy",
          first is not None and second is None and "busy" in logs[-1])
    for _ in range(50):
        if (tmp / "a_outline.jpg").exists():
            break
        time.sleep(0.1)
    time.sleep(0.1)
    check("the running one finishes", (tmp / "a_outline.jpg").exists())
    check("and frees its slot", out(photo(tmp, "c.jpg"), entries) is not None)


def test_stale_temp_files_are_swept(cfg):
    import os
    import time
    tmp = Path(tempfile.mkdtemp())
    stale = tmp / ".old_outline.jpg.1.2.tmp"
    fresh = tmp / ".new_outline.jpg.1.3.tmp"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    (tmp / "q").mkdir()
    (tmp / "q" / "prompts.txt").write_text("stove\n")
    out = sam_client.EvidenceOutliner(config=cfg, log_fn=lambda m: None, background=False)
    out(photo(tmp, "e.jpg"), [{"ref_yes_path": str(tmp / "q" / "ref-yes.jpg")}])
    check("a temp file left by a stopped server is swept", not stale.exists())
    check("one still being written is left alone", fresh.exists())


def test_flag():
    import os
    old = os.environ.pop("TRUSS_EVIDENCE_OUTLINE", None)
    try:
        check("the outline is off by default", not sam_client.outline_enabled())
        os.environ["TRUSS_EVIDENCE_OUTLINE"] = "1"
        check("TRUSS_EVIDENCE_OUTLINE=1 turns it on", sam_client.outline_enabled())
        os.environ["TRUSS_SAM_TIMEOUT_S"] = "nope"
        os.environ["TRUSS_SAM_URL"] = "http://dgx:8090/"
        cfg = sam_client.config_from_env()
        check("config reads the URL and survives a bad timeout",
              cfg.url == "http://dgx:8090" and cfg.timeout_s == sam_client.DEFAULT_TIMEOUT_S)
    finally:
        for k in ("TRUSS_EVIDENCE_OUTLINE", "TRUSS_SAM_TIMEOUT_S", "TRUSS_SAM_URL"):
            os.environ.pop(k, None)
        if old is not None:
            os.environ["TRUSS_EVIDENCE_OUTLINE"] = old


if __name__ == "__main__":
    server, cfg = start_stub()
    test_question_phrases()
    test_read_prompts()
    test_segment_sends_photo_phrases_and_key(cfg)
    test_outline_writes_only_on_a_match(cfg)
    test_errors_are_sam_errors(cfg)
    test_rle_helpers()
    test_outliner_phrases_and_failures(cfg)
    test_check_loop_outline()
    test_outliner_caps_and_names(cfg)
    test_slow_sam_never_piles_up(cfg)
    test_stale_temp_files_are_swept(cfg)
    test_flag()
    server.shutdown()
    print("ALL PASS")
