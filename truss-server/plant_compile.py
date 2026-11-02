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

import hashlib
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
    "of the knobs on the front of the stove. Name it plainly: a later "
    "check reads it first. It is the usual way to tell, not the only one: "
    "the question is about the real state of the thing, so a later check "
    "that cannot see this detail may still answer from another clear, "
    "direct sign of the same state, such as a lit flame showing the "
    "stove is on, and otherwise answers cannot_tell. "
    "Describe only what is in the photos. Never guess."
)

# Modifier words dropped from a phrase so only the noun is left:
# articles, states, colors, sizes, materials, fuels, places on the
# object, and times of day. A small list, not a parser: it catches the
# modifiers a model most often adds despite being told not to. State
# words are dropped even on their own; nouns that double as modifiers
# (glass, gas, front) are only dropped in front of another word.
STATE_WORDS = {
    "the", "a", "an", "my", "her", "his", "their", "this", "that",
    "on", "off", "open", "opened", "closed", "shut", "empty", "full",
    "lit", "unlit", "locked", "unlocked", "filled", "turned",
    "red", "orange", "yellow", "green", "blue", "purple", "pink",
    "black", "white", "gray", "grey", "brown", "silver", "gold",
    "small", "large", "big", "little", "tall", "short", "round",
    "square", "new", "old", "clean", "dirty", "hot", "cold", "warm",
}
MODIFIER_WORDS = {
    "ceramic", "wooden", "wood", "metal", "metallic", "plastic",
    "glass", "steel", "stainless", "iron", "brass", "chrome", "paper",
    "gas", "electric", "induction",
    "front", "back", "left", "right", "top", "bottom", "upper", "lower",
    "weekly", "daily", "morning", "evening", "night", "nightly",
}

# Room for Glimmer to think before it answers; a reply cut off at the
# limit is retried by endpoint.py with twice this, never parsed.
MAX_TOKENS = 2048
MIN_PROMPTS = 2
MAX_PROMPTS = 3
MAX_LOOK_CHARS = 400

# Looks text is read into every later check. Words that steer a model
# rather than describe a photo have no place in it, whoever put them
# into the question; a compile whose looks carry them is refused.
STEERING = re.compile(
    r"\b(ignore|disregard|override|instructions?|system|prompt|"
    r"always|never answer|verdict|cannot_tell|json)\b",
    re.IGNORECASE,
)


class CompileError(Exception):
    """A compile that could not be used. The message is the plain
    reason the result carries."""


def clean_prompt(phrase):
    """One SAM noun phrase in its stored form, or None when nothing
    usable is left. Lowercase, letters and inner hyphens or spaces
    only, modifiers dropped, trimmed to the last two words (the head
    noun and the word naming it, as in "pill organizer")."""
    if not isinstance(phrase, str):
        return None
    words = re.sub(r"[^a-z\- ]+", " ", phrase.lower()).split()
    words = [w.strip("-") for w in words if w.strip("-")]
    if not words:
        return None
    head = words[-1]
    words = [w for w in words[:-1] if w not in STATE_WORDS | MODIFIER_WORDS] + [head]
    if head in STATE_WORDS:
        return None
    return " ".join(words[-2:])


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
        # Cutting could drop the qualifier that matters ("except the
        # back left knob"), so an over-long answer is refused whole.
        raise CompileError(f"The model's {what} was too long, so nothing was compiled.")
    if STEERING.search(text):
        raise CompileError(
            f"The model's {what} read as an instruction, not a description, "
            "so nothing was compiled."
        )
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


def looks_fingerprint(folder):
    """A digest of both reference photos, or None when either is
    missing. A compile writes only if the photos it was made from are
    still the ones in the folder."""
    digest = hashlib.sha256()
    for name in ("ref-yes.jpg", "ref-no.jpg"):
        try:
            data = (Path(folder) / name).read_bytes()
        except OSError:
            return None
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _refuse_contributor(endpoint):
    """The compile sends both photos of her home. The contributor tier
    trains on what it is sent, so a compile never runs on it, whatever
    the endpoint is set to. Returns the config to call with, or None
    when the endpoint has no config of its own (ask_json then uses
    its default)."""
    config_from_env = getattr(endpoint, "config_from_env", None)
    if config_from_env is None:
        return None
    config = config_from_env()
    model = getattr(config, "model", "") or ""
    if "contributor" in model.lower():
        raise CompileError(
            f"The model is set to {model}, a tier that trains on what it is "
            "sent. Her photos never go there, so nothing was compiled."
        )
    return config


def compile_question(folder, endpoint=None, lock=None):
    """Compile one question folder. Writes prompts.txt and looks.txt
    and returns {"ok": True, "prompts": [...], "looks": "...", "model":
    "..."}; on any failure writes nothing and returns {"ok": False,
    "reason": "..."}. Never raises.

    endpoint is any object with ask_json, image_part, and text_part as
    in endpoint.py; it defaults to that module. lock is the question
    store's lock: the files are written under it, and only if the
    reference photos are still the ones the model was shown, so a
    look re-shot during the call can never leave a description of the
    old photos behind.
    """
    folder = Path(folder)
    try:
        if endpoint is None:
            endpoint = _load_endpoint()
        config = _refuse_contributor(endpoint)
        try:
            question = (folder / "question.txt").read_text().strip()
        except OSError:
            raise CompileError("The question's words could not be read, so nothing was compiled.")
        ref_yes = folder / "ref-yes.jpg"
        ref_no = folder / "ref-no.jpg"
        with lock or threading.Lock():
            fingerprint = looks_fingerprint(folder)
            if fingerprint is None:
                raise CompileError("Both reference looks are needed before a compile.")
            system, content = build_request(question, ref_yes, ref_no, endpoint)
        try:
            answer = endpoint.ask_json(
                system, content, COMPILE_SCHEMA, SCHEMA_NAME,
                config=config, max_tokens=MAX_TOKENS,
            )
        except Exception as e:
            reason = str(e).strip() or "The model call failed"
            raise CompileError(f"{reason.rstrip('.')}, so nothing was compiled.")
        if not isinstance(answer, dict):
            raise CompileError("The model's answer was not a record, so nothing was compiled.")
        prompts = clean_prompts(answer.get("prompts"))
        if len(prompts) < MIN_PROMPTS:
            raise CompileError(
                "The model named fewer than two usable object names for the "
                "detector, so nothing was compiled."
            )
        looks = render_looks(answer)
        with lock or threading.Lock():
            if looks_fingerprint(folder) != fingerprint:
                raise CompileError(
                    "A reference look changed during the compile, so nothing "
                    "was written; the new look is compiled next."
                )
            # looks.txt last: a check that finds looks.txt can count
            # on prompts.txt being from the same compile.
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
    taken are the ones the files describe.

    A compile that fails is tried again after each delay in
    retry_delays, so a DGX that is down at planting time does not
    leave a question uncompiled for good. A retry is skipped once the
    folder holds a looks.txt for its current photos."""

    RETRY_DELAYS = (30, 120, 600)

    def __init__(self, compile_fn=compile_question, report=print, retry_delays=RETRY_DELAYS):
        self._compile = compile_fn
        self._report = report
        self._retry_delays = tuple(retry_delays)
        self._queue = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()

    def submit(self, folder, attempt=0):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, daemon=True)
                self._thread.start()
        self._queue.put((Path(folder), attempt))

    def wait(self):
        """Block until every submitted compile has finished. For tests
        and the command line. Retries waiting on their delay are not
        counted."""
        self._queue.join()

    def _retry(self, folder, attempt):
        if attempt >= len(self._retry_delays):
            self._report(f"Compile {folder.name}: no retries left; run plant_compile.py by hand.")
            return
        timer = threading.Timer(
            self._retry_delays[attempt], self.submit, args=(folder, attempt + 1)
        )
        timer.daemon = True
        timer.start()

    def _work(self):
        while True:
            folder, attempt = self._queue.get()
            try:
                if attempt and not needs_compile(folder):
                    continue
                result = self._compile(folder)
                if result.get("ok"):
                    self._report(
                        f"Compiled {folder.name} with {result.get('model') or 'the model'}: "
                        f"prompts {', '.join(result['prompts'])}"
                    )
                else:
                    self._report(f"Compile {folder.name} skipped: {result.get('reason')}")
                    self._retry(folder, attempt)
            except Exception as e:
                self._report(f"Compile {folder.name} failed: {e}")
                self._retry(folder, attempt)
            finally:
                self._queue.task_done()


def needs_compile(folder):
    """True when a question folder has both looks and no looks.txt:
    planted while the model was away, or before compiles existed."""
    folder = Path(folder)
    return (
        (folder / "ref-yes.jpg").exists()
        and (folder / "ref-no.jpg").exists()
        and not (folder / "looks.txt").exists()
    )


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
