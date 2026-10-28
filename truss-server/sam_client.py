#!/usr/bin/env python3
"""
SAM on the DGX, from the laptop (work item E).

The DGX runs dgx/sam/sam_service.py: a photo and noun phrases in,
boxes, masks and scores out. Everything that needs an image decoder
happens there, so this side stays standard library only: it sends the
photo's bytes and the phrases, and writes back what comes back.

The use wired here is the evidence outline: when a question fires, a
copy of the kept evidence photo is made with the object outlined,
found by the question's compiled SAM phrases (prompts.txt, written at
plant time, one phrase per line). The evidence photo itself is never
changed; the outline goes beside it as <evidence>_outline.jpg. It is
off unless TRUSS_EVIDENCE_OUTLINE=1, and it runs in the background,
so a slow or missing SAM never delays a spoken answer.

    TRUSS_EVIDENCE_OUTLINE  1 turns the outline on; anything else, off
    TRUSS_SAM_URL           default http://127.0.0.1:8090 (the DGX:
                            http://<dgx>:8090)
    TRUSS_SAM_KEY           optional; the SAM service's own key
                            (setup.sh's SAM_API_KEY). It is sent over
                            plain http on the LAN, so it keeps other
                            machines on the network out and is nothing
                            more; never reuse the Meta key here.
    TRUSS_SAM_TIMEOUT_S     default 15
"""

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8090"
DEFAULT_TIMEOUT_S = 15.0

SamConfig = namedtuple("SamConfig", ["url", "key", "timeout_s"])


class SamError(Exception):
    """A SAM call that gave no usable reply. The message is a plain
    reason, safe to log."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def outline_enabled():
    return os.environ.get("TRUSS_EVIDENCE_OUTLINE", "") == "1"


def config_from_env():
    url = (os.environ.get("TRUSS_SAM_URL") or DEFAULT_URL).rstrip("/")
    key = os.environ.get("TRUSS_SAM_KEY") or None
    try:
        timeout = float(os.environ.get("TRUSS_SAM_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    if not timeout > 0:
        timeout = DEFAULT_TIMEOUT_S
    return SamConfig(url, key, timeout)


def read_prompts(folder):
    """The question's compiled SAM phrases from prompts.txt, one per
    line, or [] when there are none."""
    try:
        text = (Path(folder) / "prompts.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, TypeError):
        return []
    out = []
    for line in text.splitlines():
        phrase = " ".join(line.split())
        if phrase and phrase not in out:
            out.append(phrase)
    return out


def _post(path, payload, config):
    headers = {"Content-Type": "application/json"}
    if config.key:
        headers["Authorization"] = f"Bearer {config.key}"
    req = urllib.request.Request(
        config.url + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with _OPENER.open(req, timeout=config.timeout_s) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("error", "")
        except Exception:
            detail = ""
        if e.code in (401, 403):
            raise SamError("The SAM service refused the key (TRUSS_SAM_KEY).")
        raise SamError(f"The SAM service answered with an error (status {e.code}). {detail}".strip())
    except Exception:
        raise SamError("The SAM service did not answer in time, or at all.")
    try:
        reply = json.loads(text)
    except ValueError:
        raise SamError("The SAM service's reply was not JSON.")
    if not isinstance(reply, dict) or not isinstance(reply.get("union"), dict):
        raise SamError("The SAM service's reply had no union.")
    return reply


def _request(image_path, phrases, threshold, masks):
    data = Path(image_path).read_bytes()
    payload = {
        "image_b64": base64.b64encode(data).decode("ascii"),
        "phrases": list(phrases),
        "masks": masks,
    }
    if threshold is not None:
        payload["threshold"] = threshold
    return payload


def segment(image_path, phrases, threshold=None, masks=True, config=None):
    """SAM's detections for the phrases on one photo: per phrase, and
    their union. See sam_service.py for the reply."""
    config = config or config_from_env()
    return _post("/segment", _request(image_path, phrases, threshold, masks), config)


def has_match(reply):
    """Whether SAM found anything for any of the phrases."""
    union = reply.get("union") if isinstance(reply, dict) else None
    return isinstance(union, dict) and isinstance(union.get("count"), int) and union["count"] > 0


def outline_path(evidence):
    evidence = Path(evidence)
    return evidence.with_name(f"{evidence.stem}_outline.jpg")


def outline(image_path, phrases, dest, threshold=None, config=None):
    """Ask SAM for the photo with the phrases' union outlined and
    write it to dest. Returns the reply (without the image) and
    whether a file was written: nothing is written when nothing
    matched."""
    config = config or config_from_env()
    reply = _post("/outline", _request(image_path, phrases, threshold, False), config)
    b64 = reply.pop("image_b64", None)
    written = False
    if has_match(reply):
        if not isinstance(b64, str) or not b64:
            raise SamError("SAM found a match but sent no outlined photo.")
        try:
            data = base64.b64decode(b64, validate=True)
        except ValueError:
            raise SamError("The outlined photo was not base64.")
        if not data.startswith(b"\xff\xd8"):
            raise SamError("The outlined photo was not a JPEG.")
        dest = Path(dest)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        written = True
    reply["written"] = written
    return reply


# -- run-length masks, for checking SAM's replies without numpy ---------------


def rle_runs(rle):
    """(start, end) flat index spans of the mask's set pixels."""
    pos, on, out = 0, False, []
    for count in rle["counts"]:
        if on and count:
            out.append((pos, pos + count))
        pos += count
        on = not on
    return out


def rle_area(rle):
    return sum(end - start for start, end in rle_runs(rle))


def rle_outside_box(rle, box):
    """How many of the mask's pixels lie outside the box, where a pixel
    belongs to the box when the box covers any part of it (the
    service's own rule)."""
    import math

    h, w = rle["size"]
    x0 = max(0, min(w, math.floor(box[0])))
    y0 = max(0, min(h, math.floor(box[1])))
    x1 = max(0, min(w, math.ceil(box[2])))
    y1 = max(0, min(h, math.ceil(box[3])))
    outside = 0
    for start, end in rle_runs(rle):
        while start < end:
            row, col = divmod(start, w)
            stop = min(end, (row + 1) * w)
            n = stop - start
            if y0 <= row < y1:
                lo, hi = col, col + n
                outside += n - max(0, min(hi, x1) - max(lo, x0))
            else:
                outside += n
            start = stop
    return outside


# -- the evidence outline in the check loop -----------------------------------


class EvidenceOutliner:
    """The check loop's outline step. Called with the kept evidence
    photo and the questions that fired on it; the phrases are the union
    of those questions' prompts.txt. The SAM call runs on a background
    thread, so the call returns at once with where the outline will be
    written, or None when no fired question has phrases."""

    def __init__(self, config=None, log_fn=print, background=True):
        self.config = config
        self.log_fn = log_fn
        self.background = background

    def phrases_for(self, entries):
        phrases = []
        for entry in entries:
            try:
                folder = Path(entry["ref_yes_path"]).parent
            except (KeyError, TypeError):
                continue
            for p in read_prompts(folder):
                if p not in phrases:
                    phrases.append(p)
        return phrases

    def __call__(self, evidence, entries):
        phrases = self.phrases_for(entries)
        if not phrases:
            self.log_fn(f"No outline for {Path(evidence).name}: no prompts.txt for the questions that fired.")
            return None
        dest = outline_path(evidence)
        if self.background:
            threading.Thread(target=self._run, args=(evidence, phrases, dest), daemon=True).start()
        else:
            self._run(evidence, phrases, dest)
        return str(dest)

    def _run(self, evidence, phrases, dest):
        try:
            reply = outline(evidence, phrases, dest, config=self.config or config_from_env())
        except (SamError, OSError) as e:
            self.log_fn(f"No outline for {Path(evidence).name}: {e}")
            return
        ms = reply.get("timing_ms", {}).get("total")
        if reply["written"]:
            self.log_fn(f"Outlined {', '.join(reply['union'].get('phrases_matched', []))} "
                        f"in {Path(dest).name} ({ms} ms on SAM).")
        else:
            self.log_fn(f"No outline for {Path(evidence).name}: SAM found none of {', '.join(phrases)}.")
