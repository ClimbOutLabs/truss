#!/usr/bin/env python3
"""
Tests for the plant-time compile.

Run from the truss-server folder:  python3 test_plant_compile.py
Stdlib only, no network: the model call is a scripted stand-in with
the same three functions endpoint.py exposes. Exits non-zero on the
first failure.
"""

import json
import sys
import tempfile
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import plant_compile  # noqa: E402
from plant_compile import (  # noqa: E402
    COMPILE_SCHEMA,
    Compiler,
    clean_prompt,
    clean_prompts,
    compile_question,
    compiled_looks,
    compiled_prompts,
)
from questions import QuestionStore  # noqa: E402

JPEG_YES = b"\xff\xd8\xff\xe0" + b"yes-look" + b"\xff\xd9"
JPEG_NO = b"\xff\xd8\xff\xe0" + b"no-look" + b"\xff\xd9"

STOVE_ANSWER = {
    "prompts": ["stove", "stove knob"],
    "yes_look": "All four black knobs on the front of the white gas range point straight up.",
    "no_look": "The front left knob is turned toward the burner and its flame is lit.",
    "decides": "The positions of the four knobs on the front of the stove.",
}


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


class FakeEndpoint:
    """The three functions of endpoint.py, scripted. Records every
    call so the request shape can be checked."""

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    @staticmethod
    def text_part(text):
        return {"type": "text", "text": text}

    @staticmethod
    def image_part(path):
        return {"type": "image_url", "image_url": {"url": "file:" + Path(path).name}}

    def ask_json(self, system, content, schema, schema_name, config=None):
        self.calls.append(
            {"system": system, "content": content, "schema": schema, "name": schema_name}
        )
        if self.error is not None:
            raise self.error
        return self.answer


def planted_folder(tmp, text="Is the stove off?"):
    store = QuestionStore(tmp)
    slug = store.create(text)
    store.save_ref(slug, "yes", JPEG_YES)
    store.save_ref(slug, "no", JPEG_NO)
    return Path(tmp) / slug


def test_prompt_cleaning():
    check("plain noun kept", clean_prompt("stove") == "stove")
    check("two-word noun kept", clean_prompt("pill organizer") == "pill organizer")
    check("lowercased", clean_prompt("Deadbolt") == "deadbolt")
    check("material dropped", clean_prompt("ceramic tile") == "tile")
    check("color and material dropped", clean_prompt("black gas stove") == "gas stove")
    check("article dropped", clean_prompt("the stove") == "stove")
    check("state word in front dropped", clean_prompt("empty pill organizer") == "pill organizer")
    check("state word alone dropped", clean_prompt("off") is None)
    check("three nouns refused", clean_prompt("kitchen gas stove knob") is None)
    check("punctuation stripped", clean_prompt(" stove. ") == "stove")
    check("not text refused", clean_prompt(7) is None and clean_prompt(None) is None)
    check("empty refused", clean_prompt("  ") is None)
    check(
        "repeats and overflow trimmed",
        clean_prompts(["stove", "the stove", "knob", "burner", "range"])
        == ["stove", "knob", "burner"],
    )
    check("not a list is nothing", clean_prompts("stove") == [])


def test_compile_writes_both_files():
    with tempfile.TemporaryDirectory() as tmp:
        folder = planted_folder(tmp)
        fake = FakeEndpoint(answer=STOVE_ANSWER)
        result = compile_question(folder, endpoint=fake)
        check("compile ok", result["ok"])
        check("one model call", len(fake.calls) == 1)
        check("prompts.txt exists", (folder / "prompts.txt").exists())
        check("looks.txt exists", (folder / "looks.txt").exists())
        check(
            "prompts.txt is one phrase per line",
            (folder / "prompts.txt").read_text() == "stove\nstove knob\n",
        )
        looks = (folder / "looks.txt").read_text()
        check("looks names the yes look", looks.startswith("Yes looks like: All four black knobs"))
        check("looks names the no look", "\nNo looks like: The front left knob" in looks)
        check(
            "looks ends with the deciding detail",
            looks.rstrip().splitlines()[-1]
            == "What decides it: The positions of the four knobs on the front of the stove.",
        )
        check("readers agree", compiled_prompts(folder) == ["stove", "stove knob"])
        check("looks reader", compiled_looks(folder) == looks.strip())
        check("no side files left", not list(folder.glob("*.tmp")))


def test_request_shape():
    with tempfile.TemporaryDirectory() as tmp:
        folder = planted_folder(tmp)
        fake = FakeEndpoint(answer=STOVE_ANSWER)
        compile_question(folder, endpoint=fake)
        call = fake.calls[0]
        check("strict schema passed", call["schema"] is COMPILE_SCHEMA)
        check("schema name", call["name"] == "plant_compile")
        check(
            "schema closed and all fields required",
            COMPILE_SCHEMA["additionalProperties"] is False
            and set(COMPILE_SCHEMA["required"]) == set(COMPILE_SCHEMA["properties"]),
        )
        check("system prompt asks for nouns", "no adjectives" in call["system"])
        check("system prompt asks for the deciding detail", "cannot_tell" in call["system"])
        content = call["content"]
        check("question first", content[0] == {"type": "text", "text": "Question: Is the stove off?"})
        images = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
        check("yes look then no look", images == ["file:ref-yes.jpg", "file:ref-no.jpg"])
        labels = [p["text"] for p in content if p["type"] == "text"][1:]
        check(
            "each look labeled before its photo",
            "yes" in labels[0] and "no" in labels[1]
            and content.index(content[1]) < content.index(content[2]),
        )


def test_prompts_cleaned_on_the_way_in():
    with tempfile.TemporaryDirectory() as tmp:
        folder = planted_folder(tmp, "Did I take my night pills?")
        answer = dict(STOVE_ANSWER, prompts=["Empty Pill Organizer", "plastic compartment", "pills"])
        result = compile_question(folder, endpoint=FakeEndpoint(answer=answer))
        check("compile ok", result["ok"])
        check(
            "stored phrases have no modifiers",
            compiled_prompts(folder) == ["pill organizer", "compartment", "pills"],
        )


def test_failures_write_nothing():
    cases = [
        ("model unreachable", FakeEndpoint(error=RuntimeError("llama-server did not answer.")), "llama-server did not answer"),
        ("no usable phrase", FakeEndpoint(answer=dict(STOVE_ANSWER, prompts=["off", "big shiny black stove"])), "no usable object"),
        ("looks not text", FakeEndpoint(answer=dict(STOVE_ANSWER, decides=None)), "deciding detail"),
        ("empty looks", FakeEndpoint(answer=dict(STOVE_ANSWER, yes_look="   ")), "yes look"),
        ("answer not a record", FakeEndpoint(answer=["stove"]), "not a record"),
    ]
    for name, fake, words in cases:
        with tempfile.TemporaryDirectory() as tmp:
            folder = planted_folder(tmp)
            result = compile_question(folder, endpoint=fake)
            check(f"{name}: not ok", result["ok"] is False)
            check(f"{name}: plain reason", words in result["reason"])
            check(
                f"{name}: no files",
                not (folder / "prompts.txt").exists() and not (folder / "looks.txt").exists(),
            )


def test_failed_recompile_keeps_the_last_good_files():
    with tempfile.TemporaryDirectory() as tmp:
        folder = planted_folder(tmp)
        compile_question(folder, endpoint=FakeEndpoint(answer=STOVE_ANSWER))
        before = (folder / "looks.txt").read_text()
        result = compile_question(folder, endpoint=FakeEndpoint(error=RuntimeError("timed out")))
        check("recompile failed", not result["ok"])
        check("last good looks kept", (folder / "looks.txt").read_text() == before)
        check("last good prompts kept", compiled_prompts(folder) == ["stove", "stove knob"])


def test_needs_both_looks():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create("Is the door locked?")
        store.save_ref(slug, "yes", JPEG_YES)
        fake = FakeEndpoint(answer=STOVE_ANSWER)
        result = compile_question(Path(tmp) / slug, endpoint=fake)
        check("one look is not enough", not result["ok"] and "Both reference looks" in result["reason"])
        check("no model call made", fake.calls == [])


def test_missing_endpoint_module():
    real = plant_compile._load_endpoint

    def missing():
        raise plant_compile.CompileError(
            "The checker endpoint (endpoint.py) is not installed, so nothing was compiled."
        )

    plant_compile._load_endpoint = missing
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = planted_folder(tmp)
            result = compile_question(folder)
            check("no endpoint is a plain failure", not result["ok"] and "endpoint.py" in result["reason"])
    finally:
        plant_compile._load_endpoint = real


def test_compiler_worker():
    with tempfile.TemporaryDirectory() as tmp:
        folder = planted_folder(tmp)
        lines = []
        fake = FakeEndpoint(answer=STOVE_ANSWER)
        compiler = Compiler(lambda f: compile_question(f, endpoint=fake), report=lines.append)
        compiler.submit(folder)
        compiler.submit(folder)
        compiler.wait()
        check("each submit compiled in turn", len(fake.calls) == 2)
        check("reported", lines and lines[0].startswith("Compiled is-the-stove-off"))

        def boom(f):
            raise RuntimeError("worker crash")

        broken = Compiler(boom, report=lines.append)
        broken.submit(folder)
        broken.wait()
        check("a crash is reported, not fatal", lines[-1].endswith("worker crash"))
        broken.submit(folder)
        broken.wait()
        check("worker keeps going after a crash", lines[-1].endswith("worker crash"))


def multipart(data):
    boundary = "trussboundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
        f"filename=\"x.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def post(url, data, ctype):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def test_planting_compiles():
    import server as server_module

    with tempfile.TemporaryDirectory() as tmp:
        fake = FakeEndpoint(answer=STOVE_ANSWER)
        server_module.STORE = QuestionStore(tmp)
        server_module.COMPILER = Compiler(
            lambda f: compile_question(f, endpoint=fake), report=lambda line: None
        )
        httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            _, body = post(
                base + "/plant/question",
                json.dumps({"text": "Is the stove off?"}).encode(),
                "application/json",
            )
            slug = body["slug"]
            folder = Path(tmp) / slug
            post(f"{base}/plant/ref?slug={slug}&kind=yes", *multipart(JPEG_YES))
            server_module.COMPILER.wait()
            check("one look does not compile", fake.calls == [])
            _, body = post(f"{base}/plant/ref?slug={slug}&kind=no", *multipart(JPEG_NO))
            check("planting answers standing", body["state"] == "standing")
            server_module.COMPILER.wait()
            check("second look compiles", len(fake.calls) == 1)
            check("prompts.txt written by planting", compiled_prompts(folder) == ["stove", "stove knob"])
            check("looks.txt written by planting", "What decides it:" in compiled_looks(folder))

            post(f"{base}/plant/ref?slug={slug}&kind=yes", *multipart(JPEG_YES))
            server_module.COMPILER.wait()
            check("re-shot look on a standing question recompiles", len(fake.calls) == 2)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    test_prompt_cleaning()
    test_compile_writes_both_files()
    test_request_shape()
    test_prompts_cleaned_on_the_way_in()
    test_failures_write_nothing()
    test_failed_recompile_keeps_the_last_good_files()
    test_needs_both_looks()
    test_missing_endpoint_module()
    test_compiler_worker()
    test_planting_compiles()
    print("ALL PASS")
