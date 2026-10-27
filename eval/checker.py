"""Canonical check implementation for the Mannie eval.

This is kept free of eval-only concerns (no manifest or scorer logic
here) because the product spike will import check() and locate()
later, as is.

    check(frame_path, question_text, mode, ref_yes=None, ref_no=None)
      -> {"verdict": "yes" | "no" | "cannot_tell",
          "reason": "<one sentence>",
          "latency_ms": int,
          "usage": {"spark_input_tokens": int,
                    "spark_output_tokens": int,
                    "sam_images": int}}

check() also accepts three keyword-only arguments that exist only for
mock mode: frame_id, label, and mock_error_rate. The generic and
planted modes ignore them. This is a deliberate addition beyond the
handoff's literal signature, needed because mock mode has to know the
manifest label to flip it; it is called out as an assumption in the
report at the end of the build.

mode generic: one Spark vision call, the frame plus the question.
mode planted: the same call, with ref_yes and ref_no images added and
  the prompt asking for a comparison against the wearer's own
  references.
mode mock: no network. A deterministic stand-in, seeded by frame_id,
  that returns the manifest label flipped at a configurable error
  rate, with fixed fake usage and latency. This proves the runner and
  scorer plumbing without spending any API credits.
"""

import base64
import json
import random
import time
from pathlib import Path

MODEL_ID = "muse-spark-1.3"
MODEL_BASE_URL = "https://api.meta.ai/v1"

SYSTEM_PROMPT_GENERIC = (
    "You are answering one standing visual question about a single photo from "
    "smart glasses, the kind a person would ask before leaving home or going to "
    "bed, such as whether the stove is off. Reply with JSON only, in exactly "
    "this shape: "
    '{"verdict": "yes", "reason": "<one sentence>"} '
    "where verdict is yes, no, or cannot_tell. "
    "Say cannot_tell when the detail that would decide the answer is not "
    "visible in the photo. Never guess."
)

SYSTEM_PROMPT_PLANTED = (
    "You are answering one standing visual question about a single photo from "
    "smart glasses. You are also given two reference photos taken earlier in "
    "the wearer's own home: ref_yes shows what a yes answer looks like there, "
    "ref_no shows what a no answer looks like there. Compare the new photo "
    "against these two references instead of a generic idea of the scene. "
    "Reply with JSON only, in exactly this shape: "
    '{"verdict": "yes", "reason": "<one sentence>"} '
    "where verdict is yes, no, or cannot_tell. "
    "Say cannot_tell when the detail that would decide the answer is not "
    "visible in the photo. Never guess."
)

MOCK_LATENCY_MS = 120
MOCK_USAGE = {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0}


def check(
    frame_path,
    question_text,
    mode,
    ref_yes=None,
    ref_no=None,
    frame_id=None,
    label=None,
    mock_error_rate=0.15,
):
    if mode == "mock":
        return _check_mock(frame_id or str(frame_path), label, mock_error_rate)
    if mode == "generic":
        return _check_spark(frame_path, question_text, ref_yes=None, ref_no=None)
    if mode == "planted":
        if ref_yes is None or ref_no is None:
            raise ValueError("planted mode needs both ref_yes and ref_no images")
        return _check_spark(frame_path, question_text, ref_yes=ref_yes, ref_no=ref_no)
    raise ValueError(f"unknown mode: {mode}")


def _check_mock(seed_value, label, error_rate):
    rng = random.Random(seed_value)
    if label not in ("yes", "no"):
        return {
            "verdict": "cannot_tell",
            "reason": "Mock mode has no manifest label to check against for this frame.",
            "latency_ms": MOCK_LATENCY_MS,
            "usage": dict(MOCK_USAGE),
        }
    flipped = rng.random() < error_rate
    verdict = ("no" if label == "yes" else "yes") if flipped else label
    reason = (
        "Mock mode: a deliberately wrong stand-in verdict, to exercise the scorer."
        if flipped
        else "Mock mode: a stand-in verdict that matches the manifest label."
    )
    return {
        "verdict": verdict,
        "reason": reason,
        "latency_ms": MOCK_LATENCY_MS,
        "usage": dict(MOCK_USAGE),
    }


def _require_api_key():
    import os

    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY is not set.")
    return api_key


def _data_uri(path):
    data = Path(path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _spark_client():
    # Imported lazily so that mock mode, the self-test, and the dummy data
    # tool never need the OpenAI SDK installed.
    from openai import OpenAI

    return OpenAI(base_url=MODEL_BASE_URL, api_key=_require_api_key())


def _check_spark(frame_path, question_text, ref_yes=None, ref_no=None):
    client = _spark_client()

    content = [
        {"type": "text", "text": f"Question: {question_text}"},
        {"type": "image_url", "image_url": {"url": _data_uri(frame_path)}},
    ]
    if ref_yes is not None and ref_no is not None:
        system_prompt = SYSTEM_PROMPT_PLANTED
        content.append({"type": "text", "text": "Reference photo for a yes answer (ref_yes):"})
        content.append({"type": "image_url", "image_url": {"url": _data_uri(ref_yes)}})
        content.append({"type": "text", "text": "Reference photo for a no answer (ref_no):"})
        content.append({"type": "image_url", "image_url": {"url": _data_uri(ref_no)}})
    else:
        system_prompt = SYSTEM_PROMPT_GENERIC

    started = time.monotonic()
    response = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    raw = response.choices[0].message.content
    verdict, reason = _parse_verdict(raw)

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
    output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

    return {
        "verdict": verdict,
        "reason": reason,
        "latency_ms": latency_ms,
        "usage": {
            "spark_input_tokens": input_tokens or 0,
            "spark_output_tokens": output_tokens or 0,
            "sam_images": 0,
        },
    }


def _parse_verdict(raw):
    try:
        parsed = json.loads(raw)
        verdict = parsed.get("verdict")
        reason = parsed.get("reason", "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return "cannot_tell", f"Could not read a JSON verdict from the model reply: {raw!r}"
    if verdict not in ("yes", "no", "cannot_tell"):
        return "cannot_tell", f"Model returned an unexpected verdict, treated as cannot_tell: {verdict!r}"
    return verdict, reason


def locate(frame_path, prompts):
    """SAM 3.1 object grounding, behind a stable interface.

    The SAM 3.1 call shape on the Model API has not been verified yet;
    verifying it is part of the Nov 1 spike, not this build. This
    raises NotImplementedError on purpose rather than guessing an API
    shape. run_eval.py catches this and records sam_images: 0 and
    locate: "skipped" for the frame, instead of failing the run.

    Expected return shape once implemented:
        {"found": bool, "boxes": [...]}
    """
    raise NotImplementedError(
        "locate() is not implemented: the SAM 3.1 call shape on the Model API "
        "is not yet verified. See the Nov 1 spike."
    )
