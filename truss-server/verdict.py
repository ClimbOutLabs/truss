#!/usr/bin/env python3
"""
The verdict check: one standing visual question about one frame.

A check is one ask_json() call (see endpoint.py) holding the model to
this schema:

    {"verdict": "yes" | "no" | "cannot_tell",
     "reason":  one sentence naming what in the frame decides it,
     "missing": what would be needed if cannot_tell, else empty}

Two modes:

    single  the current frame only, plus the question's looks.txt (the
            words, written at plant time, for what tells this home's
            yes-look from its no-look) when the question has one. The
            default, until the multi-image test on the DGX passes.
    multi   the yes-look, the no-look and the current frame as three
            images.

A check with no references at all (the guide) runs as single with no
looks in either mode. A multi check with only one of its two
references is a setup error and reads cannot_tell.

The result is the canonical checker shape the loop and the guide
already read (see eval/checker.py), plus "missing" and "model", so the
answer log can name the model that made the call. A failed check never
reads as a verdict and this never raises: every failure is a
cannot_tell whose reason says no check was made.
"""

import time
from pathlib import Path

import endpoint

MODES = ("single", "multi")

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["yes", "no", "cannot_tell"]},
        "reason": {"type": "string"},
        "missing": {"type": "string"},
    },
    "required": ["verdict", "reason", "missing"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You answer one standing visual question about a photo taken from "
    "smart glasses the wearer has on, from their own point of view, in "
    "their own home. The question is the kind a person asks before leaving home "
    "or going to bed, such as whether the stove is off. Describe what you "
    "see before you judge it: the reason is one sentence naming what in "
    "the photo decides the answer. Say cannot_tell whenever the detail "
    "that would decide the answer is not visible, is cut off, or is too "
    "blurred or dark to read, and then say in missing what would need to "
    "be in view; otherwise missing is an empty string. Never guess. A "
    "wrong yes on a safety question is the worst possible answer, so a "
    "yes needs the deciding detail clearly visible."
)

LOOKS_INSTRUCTION = (
    " The wearer planted this question with two reference photos of "
    "their own home. In words, what tells the yes-look from the no-look there "
    "is: {looks}"
)

MULTI_INSTRUCTION = (
    " You are given three photos: first a reference photo taken earlier "
    "in the wearer's home showing what a yes answer looks like there, "
    "then one showing what a no answer looks like there, then the current "
    "photo. Answer about the current photo only, comparing it against "
    "the two references instead of a generic idea of the scene."
)

ZERO_USAGE = {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0}

MAX_TOKENS = 300


def read_looks(folder):
    """The question's looks.txt, stripped, or None when it is missing,
    unreadable or empty."""
    try:
        text = (Path(folder) / "looks.txt").read_text().strip()
    except (OSError, UnicodeDecodeError, TypeError):
        return None
    return text or None


def _request(frame_path, question, mode, ref_yes, ref_no, looks):
    """(system, content) for one check."""
    system = SYSTEM_PROMPT
    if mode == "multi" and ref_yes is not None:
        system += MULTI_INSTRUCTION
        content = [
            endpoint.text_part(f"Question: {question}"),
            endpoint.text_part("Reference photo: what a yes answer looks like here."),
            endpoint.image_part(ref_yes),
            endpoint.text_part("Reference photo: what a no answer looks like here."),
            endpoint.image_part(ref_no),
            endpoint.text_part("The current photo:"),
            endpoint.image_part(frame_path),
        ]
        return system, content
    if looks:
        system += LOOKS_INSTRUCTION.format(looks=looks)
    content = [
        endpoint.text_part(f"Question: {question}"),
        endpoint.image_part(frame_path),
    ]
    return system, content


def _reason_text(value):
    return value.strip() if isinstance(value, str) else ""


def check(
    frame_path,
    question,
    mode="single",
    ref_yes=None,
    ref_no=None,
    looks=None,
    config=None,
):
    """Ask the configured model one visual question about one frame.

    Returns {"verdict", "reason", "missing", "model", "latency_ms",
    "usage"}; see the module docstring. Never raises.
    """
    started = time.monotonic()
    model = None
    missing = ""
    usage = dict(ZERO_USAGE)
    try:
        cfg = config or endpoint.config_from_env()
        model = cfg.model
        if mode not in MODES:
            raise endpoint.EndpointError(f"The check mode {mode!r} is not known.")
        if (ref_yes is None) != (ref_no is None):
            raise endpoint.EndpointError(
                "Only one of the question's two reference looks was given."
            )
        system, content = _request(frame_path, question, mode, ref_yes, ref_no, looks)
        reply = endpoint.ask_json(
            system, content, VERDICT_SCHEMA, "verdict", config=cfg, max_tokens=MAX_TOKENS
        )
        verdict = reply["verdict"]
        reason = _reason_text(reply["reason"])
        missing = _reason_text(reply["missing"]) if verdict == "cannot_tell" else ""
        usage["spark_input_tokens"] = reply["_usage"]["input_tokens"]
        usage["spark_output_tokens"] = reply["_usage"]["output_tokens"]
    except endpoint.EndpointError as e:
        verdict, reason = "cannot_tell", f"{e} So no check was made."
    except Exception:
        verdict, reason = "cannot_tell", "The check failed, so no check was made."
    return {
        "verdict": verdict,
        "reason": reason,
        "missing": missing,
        "model": model,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "usage": usage,
    }
