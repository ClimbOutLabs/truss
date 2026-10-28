#!/usr/bin/env python3
"""
Plant-time compile for the Truss server.

When a question has both of its reference looks, one call goes to the
verdict model with the question's words and the two photos. What comes
back is written into the question's folder as plain text:

    <slug>/prompts.txt   two or three short noun phrases, one per line,
                         for SAM to find the object by ("stove",
                         "pill organizer", "deadbolt")
    <slug>/looks.txt     what tells the yes-look from the no-look, in
                         words, specific to this home, ending with the
                         one detail that decides it

The check reads looks.txt in its single mode (the frame plus these
words instead of the two reference photos). The deciding detail is
what a cannot_tell can then name as missing: "the knobs are not in
view." SAM reads prompts.txt; phrases are kept to one or two plain
words because SAM matches nouns and misses adjectives ("tile" matched
40 of 40 in other participants' tests, "ceramic tile" matched 0).

The model call is injected, so the compile can be tested without a
network and runs unchanged against llama-server on the DGX or the Meta
Model API. The default is ask_json from endpoint.py, the checker
endpoint abstraction, which reads its base URL, model, and key from
the environment.

A failed compile never blocks planting. The question stands with its
two reference looks either way; without looks.txt the check runs on
the frame and the question alone. compile_question never raises: a
missing endpoint, an unreachable model, or an answer that cannot be
used all come back as a result with ok False and a plain reason, and
no half-written file is ever left in the folder.
"""

import json
import queue
import re
import sys
import threading
import uuid
from pathlib import Path

COMPILE_SCHEMA = {
    "type": "object",
    "properties": {
        "prompts": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 3,
        },
        "yes_look": {"type": "string"},
        "no_look": {"type": "string"},
        "decides": {"type": "string"},
    },
    "required": ["prompts", "yes_look", "no_look", "decides"],
    "additionalProperties": False,
}

SCHEMA_NAME = "plant_compile"

SYSTEM_PROMPT = (
    "You are preparing a standing visual question that will be checked "
    "later against photos from smart glasses worn in this person's own "
    "home. You are given the question in the person's own words and two "
    "reference photos taken in that home: the first shows what a yes "
    "answer looks like there, the second shows what a no answer looks "
    "like there. Reply with JSON only, with these fields. "
    "prompts: two or three object names for an object detector to find "
    "the thing the question is about, each one or two plain words, "
    "lowercase, nouns only, no adjectives, no colors, no materials, no "
    'states such as on, off, open or empty. Good: "stove", "pill '
    'organizer", "deadbolt". Bad: "black gas stove", "empty pill '
    'organizer". Name the object itself first, then a part or a close '
    "alternative name. "
    "yes_look: one or two sentences on what the yes photo shows that "
    "matters for the question, naming what is specific to this home. "
    "no_look: the same for the no photo. "
    "decides: one sentence naming the single visible detail that tells "
    "yes from no and where it is on the object, such as the positions "
    "of the knobs on the front of the stove. A later check that cannot "
    "see this detail must answer cannot_tell, so name it plainly. "
    "Describe only what is in the photos. Never guess."
)

# Leading words (articles, states, colors, sizes, materials) dropped
# from a phrase while a noun follows, and state words that are
# dropped outright on their own. A small list, not a parser: it catches the
# modifiers a model most often adds despite being told not to. Nouns
# that double as modifiers (glass, metal) are only dropped in front of
# another word, never on their own.
STATE_WORDS = {
    "the", "a", "an", "my", "her", "his", "their", "this", "that",
    "on", "off", "open", "opened", "closed", "shut", "empty", "full",
    "lit", "unlit", "locked", "unlocked", "filled", "turned",
    "red", "orange", "yellow", "green", "blue", "purple", "pink",
    "black", "white", "gray", "grey", "brown", "silver", "gold",
    "small", "large", "big", "little", "tall", "short", "round",
    "square", "new", "old", "clean", "dirty",
}
MATERIAL_WORDS = {
    "ceramic", "wooden", "wood", "metal", "metallic", "plastic",
    "glass", "steel", "stainless", "iron", "brass", "chrome", "paper",
}

MAX_PROMPTS = 3
MAX_LOOK_CHARS = 400


class CompileError(Exception):
    """A compile that could not be used. The message is the plain
    reason the result carries."""


def clean_prompt(phrase):
    """One SAM noun phrase in its stored form, or None when nothing
    usable is left. Lowercase, letters and inner hyphens or spaces
    only, at most two words, known modifiers dropped."""
    if not isinstance(phrase, str):
        return None
    words = re.sub(r"[^a-z\- ]+", " ", phrase.lower()).split()
    words = [w.strip("-") for w in words if w.strip("-")]
    # Drop leading modifiers while a noun still follows them.
    while len(words) > 1 and words[0] in STATE_WORDS | MATERIAL_WORDS:
        words = words[1:]
    if not words or len(words) > 2:
        return None
    if len(words) == 1 and words[0] in STATE_WORDS:
        return None
    return " ".join(words)


def clean_prompts(phrases):
    """The phrases worth keeping, in the model's order, without
    repeats, at most three."""
    out = []
    if not isinstance(phrases, list):
        return out
    for phrase in phrases:
        cleaned = clean_prompt(phrase)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) == MAX_PROMPTS:
            break
    return out


def _sentence(value, what):
    if not isinstance(value, str):
        raise CompileError(f"The model's {what} was not text, so nothing was compiled.")
    text = " ".join(value.split())
    if not text:
        raise CompileError(f"The model gave no {what}, so nothing was compiled.")
    if len(text) > MAX_LOOK_CHARS:
        text = text[:MAX_LOOK_CHARS].rsplit(" ", 1)[0] + "..."
    return text


def render_looks(answer):
    """looks.txt as plain lines, one per field, each labeled so the
    file reads on its own and drops straight into a check prompt."""
    return (
        f"Yes looks like: {_sentence(answer.get('yes_look'), 'yes look')}\n"
        f"No looks like: {_sentence(answer.get('no_look'), 'no look')}\n"
        f"What decides it: {_sentence(answer.get('decides'), 'deciding detail')}\n"
    )


def build_request(question, ref_yes, ref_no, endpoint):
    """The system prompt and the user content for the compile call,
    as OpenAI chat/completions content parts."""
    content = [
        endpoint.text_part(f"Question: {question}"),
        endpoint.text_part("Reference photo 1: what a yes answer looks like here."),
        endpoint.image_part(str(ref_yes)),
        endpoint.text_part("Reference photo 2: what a no answer looks like here."),
        endpoint.image_part(str(ref_no)),
    ]
    return SYSTEM_PROMPT, content


def _write_text(folder, name, text):
    # A side file moved into place, the same way the store writes
    # watch.json, so a reader never sees half a file.
    path = folder / name
    tmp = folder / f"{name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _load_endpoint():
    try:
        import endpoint
    except ImportError:
        raise CompileError(
            "The checker endpoint (endpoint.py) is not installed, so nothing was compiled."
        )
    return endpoint


def compile_question(folder, endpoint=None):
    """Compile one question folder. Writes prompts.txt and looks.txt
    and returns {"ok": True, "prompts": [...], "looks": "...", "model":
    "..."}; on any
    failure writes nothing and returns {"ok": False, "reason": "..."}.
    Never raises.

    endpoint is any object with ask_json, image_part, and text_part as
    in endpoint.py; it defaults to that module.
    """
    folder = Path(folder)
    try:
        if endpoint is None:
            endpoint = _load_endpoint()
        try:
            question = (folder / "question.txt").read_text().strip()
        except OSError:
            raise CompileError("The question's words could not be read, so nothing was compiled.")
        ref_yes = folder / "ref-yes.jpg"
        ref_no = folder / "ref-no.jpg"
        if not ref_yes.exists() or not ref_no.exists():
            raise CompileError("Both reference looks are needed before a compile.")
        system, content = build_request(question, ref_yes, ref_no, endpoint)
        try:
            answer = endpoint.ask_json(system, content, COMPILE_SCHEMA, SCHEMA_NAME)
        except Exception as e:
            reason = str(e).strip() or "The model call failed"
            raise CompileError(f"{reason.rstrip('.')}, so nothing was compiled.")
        if not isinstance(answer, dict):
            raise CompileError("The model's answer was not a record, so nothing was compiled.")
        prompts = clean_prompts(answer.get("prompts"))
        if not prompts:
            raise CompileError(
                "The model named no usable object for the detector, so nothing was compiled."
            )
        looks = render_looks(answer)
        # looks.txt last: a check that finds looks.txt can count on
        # prompts.txt being from the same compile or an earlier one.
        _write_text(folder, "prompts.txt", "\n".join(prompts) + "\n")
        _write_text(folder, "looks.txt", looks)
        model = answer.get("_model")
        return {
            "ok": True,
            "prompts": prompts,
            "looks": looks,
            "model": model if isinstance(model, str) else "",
        }
    except CompileError as e:
        return {"ok": False, "reason": str(e)}
    except Exception:
        return {"ok": False, "reason": "The compile failed, so nothing was compiled."}


def compiled_prompts(folder):
    """The stored SAM phrases for a question, or [] when there are none."""
    try:
        text = (Path(folder) / "prompts.txt").read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def compiled_looks(folder):
    """The stored looks text for a question, or None when there is none."""
    try:
        text = (Path(folder) / "looks.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


class Compiler:
    """Runs compiles off the request thread, one at a time, in the
    order they were asked for. A compile takes as long as a check
    (seconds on the DGX), and the wearer should hear "planted" without
    waiting for it. One worker means a reference look re-shot while a
    compile is running is compiled again after it, so the last photos
    taken are the ones the files describe."""

    def __init__(self, compile_fn=compile_question, report=print):
        self._compile = compile_fn
        self._report = report
        self._queue = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()

    def submit(self, folder):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, daemon=True)
                self._thread.start()
        self._queue.put(Path(folder))

    def wait(self):
        """Block until every submitted compile has finished. For tests
        and the command line."""
        self._queue.join()

    def _work(self):
        while True:
            folder = self._queue.get()
            try:
                result = self._compile(folder)
                if result.get("ok"):
                    self._report(
                        f"Compiled {folder.name} with {result.get('model') or 'the model'}: "
                        f"prompts {', '.join(result['prompts'])}"
                    )
                else:
                    self._report(f"Compile {folder.name} skipped: {result.get('reason')}")
            except Exception as e:
                self._report(f"Compile {folder.name} failed: {e}")
            finally:
                self._queue.task_done()


def main(argv):
    """Compile planted questions by hand: python3 plant_compile.py [slug ...].
    With no slug, every question folder that has both looks."""
    root = Path(__file__).parent / "questions"
    if argv:
        folders = [root / slug for slug in argv]
    else:
        folders = sorted(
            f for f in root.iterdir()
            if (f / "ref-yes.jpg").exists() and (f / "ref-no.jpg").exists()
        ) if root.exists() else []
    if not folders:
        print("No planted questions to compile.")
        return 1
    failed = 0
    for folder in folders:
        result = compile_question(folder)
        if result["ok"]:
            print(f"{folder.name} ({result['model'] or 'model unnamed'}): {json.dumps(result['prompts'])}")
            print("  " + result["looks"].rstrip().replace("\n", "\n  "))
        else:
            failed += 1
            print(f"{folder.name}: {result['reason']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
