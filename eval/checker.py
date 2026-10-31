"""Canonical check implementation for the Mannie eval.

This is kept free of eval-only concerns (no manifest or scorer logic
here). The model call is the product's own: truss-server/verdict.py
over endpoint.py, so the eval measures exactly what the server runs.

    check(frame_path, question_text, mode, ref_yes=None, ref_no=None,
          looks=None)
      -> {"verdict": "yes" | "no" | "cannot_tell",
          "reason": "<one sentence>",
          "missing": "<what would decide it, when cannot_tell>",
          "model": "<model id>" | None,
          "failure": None | "<why no check was made>",
          "looks": "none" | "text" | "images",
          "latency_ms": int,
          "usage": {"spark_input_tokens": int,
                    "spark_output_tokens": int,
                    "sam_images": int}}

(The usage keys keep their old names so older results files still
score; for Muse Glimmer they count Glimmer's tokens.)

check() also accepts three keyword-only arguments that exist only for
mock mode: frame_id, label, and mock_error_rate. The other modes
ignore them.

The endpoint comes from the same environment the server reads:
TRUSS_BASE_URL (llama-server on the DGX, default
http://127.0.0.1:8080/v1), TRUSS_MODEL (default muse-glimmer-30b),
TRUSS_API_KEY, TRUSS_TIMEOUT_S, TRUSS_MAX_TOKENS.

mode generic: the frame and the question, nothing about her home.
mode planted: the product default. The frame and the question plus
  the question's looks.txt, the words the plant-time compile wrote
  from her two reference photos (verdict.py single mode).
mode planted-photos: the two reference photos and the frame as three
  images (verdict.py multi mode).
mode mock: no network. A deterministic stand-in, seeded by frame_id,
  that returns the manifest label flipped at a configurable error
  rate, with fixed fake usage and latency. This proves the runner and
  scorer plumbing without a model.

A failed check never raises from here and never reads as yes or no:
verdict.py turns every failure into cannot_tell with failure set, so
the scorer can count a timeout apart from the model honestly saying it
cannot see.

locate() asks the SAM service on the DGX (dgx/sam/sam_service.py,
TRUSS_SAM_URL, default http://127.0.0.1:8090) to find the question's
objects in the frame.
"""

import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "truss-server"))

import sam_client  # noqa: E402
import verdict  # noqa: E402

MODES = ("generic", "planted", "planted-photos", "mock")

MOCK_LATENCY_MS = 120
MOCK_USAGE = {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0}


def check(
    frame_path,
    question_text,
    mode,
    ref_yes=None,
    ref_no=None,
    looks=None,
    frame_id=None,
    label=None,
    mock_error_rate=0.15,
    config=None,
):
    if mode == "mock":
        return _check_mock(frame_id or str(frame_path), label, mock_error_rate)
    if mode == "generic":
        return verdict.check(frame_path, question_text, mode="single", config=config)
    if mode == "planted":
        if not looks:
            raise ValueError("planted mode needs the question's looks.txt (run the compile first)")
        return verdict.check(
            frame_path, question_text, mode="single", looks=looks, config=config
        )
    if mode == "planted-photos":
        if ref_yes is None or ref_no is None:
            raise ValueError("planted-photos mode needs both ref_yes and ref_no images")
        return verdict.check(
            frame_path, question_text, mode="multi", ref_yes=ref_yes, ref_no=ref_no,
            config=config,
        )
    raise ValueError(f"unknown mode: {mode}")


def _check_mock(seed_value, label, error_rate):
    rng = random.Random(seed_value)
    base = {
        "missing": "",
        "model": "mock",
        "failure": None,
        "looks": "none",
        "latency_ms": MOCK_LATENCY_MS,
        "usage": dict(MOCK_USAGE),
    }
    if label not in ("yes", "no"):
        return dict(
            base,
            verdict="cannot_tell",
            reason="Mock mode has no yes or no label to check against for this frame.",
        )
    flipped = rng.random() < error_rate
    verdict_ = ("no" if label == "yes" else "yes") if flipped else label
    reason = (
        "Mock mode: a deliberately wrong stand-in verdict, to exercise the scorer."
        if flipped
        else "Mock mode: a stand-in verdict that matches the manifest label."
    )
    return dict(base, verdict=verdict_, reason=reason)


def locate(frame_path, prompts, config=None):
    """SAM on the DGX: does it find the question's objects in the frame?

    Returns {"found": bool, "count": int, "top_score": float | None,
    "phrases": {phrase: count}, "model": str | None, "latency_ms": int,
    "error": None | str}.
    Never raises; an unreachable or failing service comes back as
    found False with error set, so the scorer counts it apart.
    """
    started = time.monotonic()
    out = {"found": False, "count": 0, "top_score": None, "phrases": {}, "model": None,
           "error": None}
    try:
        reply = sam_client.segment(frame_path, list(prompts), masks=False, config=config)
        union = reply.get("union") or {}
        out["count"] = union.get("count", 0) if isinstance(union.get("count"), int) else 0
        out["found"] = sam_client.has_match(reply)
        scores = []
        for entry in reply.get("phrases") or []:
            if not isinstance(entry, dict):
                continue
            detections = entry.get("detections") or []
            out["phrases"][str(entry.get("phrase"))] = len(detections)
            scores.extend(
                d["score"] for d in detections
                if isinstance(d, dict) and isinstance(d.get("score"), (int, float))
            )
        out["model"] = reply.get("model")
        out["top_score"] = max(scores) if scores else None
    except Exception as e:
        out["error"] = str(e) or type(e).__name__
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    return out
