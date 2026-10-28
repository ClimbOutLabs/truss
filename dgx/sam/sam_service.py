#!/usr/bin/env python3
"""
SAM on the DGX, as a small local HTTP service (work item E).

One photo and a few noun phrases in; boxes, masks and scores out, per
phrase and as one union across the phrases. The laptop server stays
standard-library only, so everything that needs an image decoder
happens here: decoding the photo, running SAM, and drawing the outline
on the evidence photo.

    GET  /health    {"ok": true, "model": "sam3", ...}; no key needed
    POST /segment   {"image_b64": "...", "phrases": ["stove"],
                     "threshold": 0.5, "masks": true}
                    -> per-phrase detections and the union, each mask
                       as run-length counts (see rle_encode)
    POST /outline   the same request; the reply carries no masks but
                    "image_b64": the photo as a JPEG with the union of
                    all matches outlined, or null when nothing matched

Coordinates are pixels of the upright photo (its EXIF orientation
applied), boxes as [x0, y0, x1, y1]. Each mask is cut to its own box,
as the Meta API does; "inside_box_raw" says how much of SAM's mask was
inside the box before the cut, which the acceptance run checks.

Settings (environment variables, all optional):

    SAM_VERSION       sam3.1 (default) or sam3. Still-image detection
                      is SAM 3 accuracy either way; sam3.1's change is
                      video tracking. setup.sh falls back to sam3 when
                      the sam3.1 checkpoint is refused (SAM_STRICT).
    SAM_CHECKPOINT    path to the .pt file; default: the file setup.sh
                      downloaded under SAM_MODELS_DIR
    SAM_MODELS_DIR    default ~/models/sam
    SAM_DTYPE         bf16 (default) or fp32
    SAM_API_KEY_FILE  when set, POSTs need "Authorization: Bearer <key>"
    SAM_STRICT        1: refuse to start when the checkpoint leaves
                      image-model weights unloaded; 0: warn and start.
                      Default 1 for sam3.1 (a video checkpoint), 0 for
                      sam3 (the upstream loader only warns there too).

Run it through setup.sh (serve, stop, status), not by hand.
"""

import argparse
import base64
import hmac
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

MAX_BODY = 40 * 1024 * 1024  # a 12 MP JPEG, base64'd, with room to spare
MAX_PHRASES = 8
MAX_PHRASE_CHARS = 60
MAX_PIXELS = 50_000_000
DEFAULT_THRESHOLD = 0.5

# The outline: a bright line on a dark halo, so it reads on a white
# stove top and on a dark counter alike.
LINE = (255, 214, 0)
HALO = (20, 20, 20)

CHECKPOINTS = {
    "sam3": ("facebook/sam3", "sam3.pt"),
    "sam3.1": ("facebook/sam3.1", "sam3.1_multiplex.pt"),
}


class RequestError(Exception):
    """A request the service refuses, with the status to answer."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# -- masks --------------------------------------------------------------------


def rle_encode(mask):
    """Run-length counts of a 2-D bool mask, row-major, starting with
    a run of zeros (possibly empty): {"size": [h, w], "counts": [...]}.
    The laptop decodes this with the standard library alone."""
    flat = np.asarray(mask, dtype=bool).ravel()
    h, w = mask.shape
    if flat.size == 0:
        return {"size": [int(h), int(w)], "counts": []}
    change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    edges = np.concatenate(([0], change, [flat.size]))
    counts = np.diff(edges).tolist()
    if flat[0]:
        counts = [0] + counts
    return {"size": [int(h), int(w)], "counts": [int(c) for c in counts]}


def box_pixels(box, h, w):
    """The pixel span [x0, x1) x [y0, y1) a float box covers, clipped
    to the image. A pixel belongs to the box when the box covers any
    part of it."""
    x0, y0, x1, y1 = box
    ix0 = int(max(0, min(w, np.floor(x0))))
    iy0 = int(max(0, min(h, np.floor(y0))))
    ix1 = int(max(0, min(w, np.ceil(x1))))
    iy1 = int(max(0, min(h, np.ceil(y1))))
    return ix0, iy0, max(ix0, ix1), max(iy0, iy1)


def inside_fraction(mask, box):
    """How much of the mask lies inside the box, 0..1 (1 for an empty
    mask: nothing is outside)."""
    total = int(mask.sum())
    if total == 0:
        return 1.0
    h, w = mask.shape
    x0, y0, x1, y1 = box_pixels(box, h, w)
    inside = int(mask[y0:y1, x0:x1].sum())
    return inside / total


def crop_to_box(mask, box):
    h, w = mask.shape
    x0, y0, x1, y1 = box_pixels(box, h, w)
    out = np.zeros_like(mask, dtype=bool)
    out[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
    return out


def _dilate(edge, times):
    out = edge
    for _ in range(times):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def boundary(mask):
    """The mask's own edge pixels: in the mask, with a 4-neighbour
    outside it. Pixels on the photo's border count as edge too, so an
    object cut off by the frame is still closed off."""
    m = np.asarray(mask, dtype=bool)
    inner = m.copy()
    inner[1:, :] &= m[:-1, :]
    inner[:-1, :] &= m[1:, :]
    inner[:, 1:] &= m[:, :-1]
    inner[:, :-1] &= m[:, 1:]
    inner[0, :] = inner[-1, :] = False
    inner[:, 0] = inner[:, -1] = False
    return m & ~inner


def line_width(h, w):
    return max(2, round(min(h, w) / 300))


def draw_outline(image, mask):
    """A copy of the RGB photo with the mask's outline drawn on it."""
    arr = np.array(image.convert("RGB"))
    h, w = mask.shape
    width = line_width(h, w)
    edge = boundary(mask)
    line = _dilate(edge, width - 1)
    halo = _dilate(line, max(1, width // 2))
    arr[halo] = HALO
    arr[line] = LINE
    return Image.fromarray(arr)


def jpeg_b64(image, quality=90):
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# -- requests -----------------------------------------------------------------


def parse_request(body):
    """(image, phrases, threshold, want_masks) from a request body."""
    try:
        req = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise RequestError("The request was not JSON.")
    if not isinstance(req, dict):
        raise RequestError("The request was not a JSON object.")

    raw_phrases = req.get("phrases")
    if not isinstance(raw_phrases, list) or not raw_phrases:
        raise RequestError("phrases must be a non-empty list of noun phrases.")
    phrases = []
    for p in raw_phrases:
        if not isinstance(p, str):
            raise RequestError("Every phrase must be text.")
        p = " ".join(p.split())
        if not p or len(p) > MAX_PHRASE_CHARS:
            raise RequestError(f"Every phrase must be 1 to {MAX_PHRASE_CHARS} characters.")
        if p not in phrases:
            phrases.append(p)
    if len(phrases) > MAX_PHRASES:
        raise RequestError(f"At most {MAX_PHRASES} phrases.")

    threshold = req.get("threshold", DEFAULT_THRESHOLD)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not 0.05 <= threshold <= 0.95:
        raise RequestError("threshold must be a number from 0.05 to 0.95.")

    want_masks = req.get("masks", True)
    if not isinstance(want_masks, bool):
        raise RequestError("masks must be true or false.")

    b64 = req.get("image_b64")
    if not isinstance(b64, str) or not b64:
        raise RequestError("image_b64 is missing.")
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError:
        raise RequestError("image_b64 is not base64.")
    image = decode_image(data)
    return image, phrases, float(threshold), want_masks


def decode_image(data):
    """The photo, upright and RGB. EXIF orientation is applied so the
    coordinates and the outline match what a viewer shows."""
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)
    except Exception:
        raise RequestError("The image could not be decoded.")
    if img.width * img.height > MAX_PIXELS:
        raise RequestError("The image is too large.")
    return img.convert("RGB")


def run(segmenter, image, phrases, threshold, want_masks, outline):
    """The reply for one request. segmenter.segment(image, phrase,
    threshold) gives (boxes Nx4, scores N, masks NxHxW bool) for one
    phrase on the image last passed to segmenter.set_image."""
    if outline:
        want_masks = False
    t0 = time.perf_counter()
    w, h = image.size
    segmenter.set_image(image)
    t_image = time.perf_counter()

    per_phrase = []
    phrase_ms = {}
    union = np.zeros((h, w), dtype=bool)
    matched = []
    for phrase in phrases:
        t = time.perf_counter()
        boxes, scores, masks = segmenter.segment(phrase, threshold)
        detections = []
        for box, score, mask in zip(boxes, scores, masks):
            box = [float(v) for v in box]
            raw = np.asarray(mask, dtype=bool)
            inside = inside_fraction(raw, box)
            cut = crop_to_box(raw, box)
            union |= cut
            det = {
                "box": [round(v, 1) for v in box],
                "score": round(float(score), 4),
                "area": int(cut.sum()),
                "inside_box_raw": round(inside, 4),
            }
            if want_masks:
                det["mask"] = rle_encode(cut)
            detections.append(det)
        detections.sort(key=lambda d: -d["score"])
        if detections:
            matched.append(phrase)
        per_phrase.append({"phrase": phrase, "detections": detections})
        phrase_ms[phrase] = round((time.perf_counter() - t) * 1000, 1)

    count = sum(len(p["detections"]) for p in per_phrase)
    ys, xs = np.nonzero(union)
    union_out = {
        "count": count,
        "phrases_matched": matched,
        "area": int(union.sum()),
        "box": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
        if count and xs.size else None,
    }
    if want_masks:
        union_out["mask"] = rle_encode(union) if count else None

    reply = {
        "model": segmenter.name,
        "width": w,
        "height": h,
        "threshold": threshold,
        "phrases": per_phrase,
        "union": union_out,
    }
    t_draw = time.perf_counter()
    if outline:
        reply["image_b64"] = jpeg_b64(draw_outline(image, union)) if union.any() else None
    t_end = time.perf_counter()
    reply["timing_ms"] = {
        "image": round((t_image - t0) * 1000, 1),
        "phrases": phrase_ms,
        "draw": round((t_end - t_draw) * 1000, 1) if outline else 0.0,
        "total": round((t_end - t0) * 1000, 1),
    }
    return reply


def make_handler(segmenter, api_key=None):
    """The request handler. One request at a time reaches the model:
    the GPU holds one image state, and SAM is sub-second anyway."""
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _send(self, status, obj):
            if status >= 400:
                # The body may be unread; never reuse this connection.
                self.close_connection = True
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True, "model": segmenter.name, "device": segmenter.device})

        def do_POST(self):
            if self.path not in ("/segment", "/outline"):
                return self._send(404, {"error": "not found"})
            if api_key is not None:
                given = self.headers.get("Authorization", "")
                if not hmac.compare_digest(given.encode(), f"Bearer {api_key}".encode()):
                    return self._send(401, {"error": "a valid key is needed"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._send(411, {"error": "Content-Length is needed"})
            if length <= 0 or length > MAX_BODY:
                return self._send(413, {"error": "the request is empty or too large"})
            body = self.rfile.read(length)
            try:
                image, phrases, threshold, want_masks = parse_request(body)
                with lock:
                    reply = run(segmenter, image, phrases, threshold, want_masks,
                                outline=self.path == "/outline")
            except RequestError as e:
                return self._send(e.status, {"error": str(e)})
            except Exception as e:  # the model failed; say so, keep serving
                sys.stderr.write(f"segment failed: {type(e).__name__}: {e}\n")
                return self._send(500, {"error": f"SAM failed: {type(e).__name__}"})
            self._send(200, reply)

    return Handler


# -- the model ----------------------------------------------------------------


class Sam3Segmenter:
    """SAM 3 (or the SAM 3.1 checkpoint) through the image processor in
    github.com/facebookresearch/sam3."""

    def __init__(self, version, checkpoint, dtype="bf16", strict=True):
        import torch
        from sam3.model.sam3_image_processor import Sam3Processor
        from sam3.model_builder import build_sam3_image_model

        if not torch.cuda.is_available():
            raise SystemExit("STOP: torch sees no CUDA device. Rerun ./setup.sh install and read its GPU check.")
        self.torch = torch
        self.name = version
        self.device = torch.cuda.get_device_name(0)
        self.dtype = torch.bfloat16 if dtype == "bf16" else None

        # Built without weights, then loaded here rather than by the
        # builder, so a checkpoint that does not fit the image model
        # (the sam3.1 file is made for the video model) is caught
        # instead of serving half-random weights.
        model = build_sam3_image_model(load_from_HF=False, checkpoint_path=None, device="cuda")
        ckpt = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if "model" in ckpt and isinstance(ckpt["model"], dict):
            ckpt = ckpt["model"]
        # The same key mapping as sam3.model_builder._load_checkpoint.
        image_ckpt = {k.replace("detector.", ""): v for k, v in ckpt.items() if "detector" in k}
        missing, unexpected = model.load_state_dict(image_ckpt, strict=False)
        self.missing = len(missing)
        self.unexpected = len(unexpected)
        print(f"{checkpoint}: {len(image_ckpt)} tensors loaded, "
              f"{self.missing} model weights missing, {self.unexpected} unexpected.", flush=True)
        if self.missing:
            print("  missing, first few:", list(missing)[:8], flush=True)
            if not strict:
                print("  WARNING: starting anyway (SAM_STRICT=0); those weights are random. "
                      "Run the acceptance and read inside_box_raw.", flush=True)
            else:
                raise SystemExit(
                    f"STOP: {self.missing} weights of the image model are not in {checkpoint}. "
                    "If this is the sam3.1 checkpoint, serve SAM_VERSION=sam3 instead (setup.sh does so by itself) "
                    "(SAM_STRICT=0 starts anyway, with those weights random)."
                )
        model.eval()
        self.processor = Sam3Processor(model, device="cuda", confidence_threshold=DEFAULT_THRESHOLD)
        self.state = None

    def _autocast(self):
        if self.dtype is None:
            import contextlib
            return contextlib.nullcontext()
        return self.torch.autocast("cuda", dtype=self.dtype)

    def set_image(self, image):
        with self.torch.inference_mode(), self._autocast():
            self.state = self.processor.set_image(image)

    def segment(self, phrase, threshold):
        self.processor.confidence_threshold = threshold
        with self.torch.inference_mode(), self._autocast():
            self.processor.reset_all_prompts(self.state)
            out = self.processor.set_text_prompt(prompt=phrase, state=self.state)
        boxes = out["boxes"].float().cpu().numpy().reshape(-1, 4)
        scores = out["scores"].float().cpu().numpy().reshape(-1)
        masks = out["masks"].cpu().numpy()
        masks = masks.reshape(masks.shape[0], masks.shape[-2], masks.shape[-1]).astype(bool)
        return boxes, scores, masks


def default_checkpoint(version, models_dir):
    repo, name = CHECKPOINTS[version]
    return Path(models_dir) / version / name


def warm_up(segmenter):
    """One pass on a plain gray image, so the first real photo does not
    pay for CUDA start-up."""
    t = time.perf_counter()
    segmenter.set_image(Image.new("RGB", (1008, 756), (128, 128, 128)))
    segmenter.segment("stove", DEFAULT_THRESHOLD)
    print(f"Warm-up done in {time.perf_counter() - t:.1f}s.", flush=True)


def main():
    ap = argparse.ArgumentParser(description="SAM on the DGX for Truss.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    version = os.environ.get("SAM_VERSION", "sam3.1")
    if version not in CHECKPOINTS:
        raise SystemExit(f"STOP: SAM_VERSION must be one of {', '.join(CHECKPOINTS)}.")
    models_dir = os.environ.get("SAM_MODELS_DIR") or str(Path.home() / "models" / "sam")
    checkpoint = os.environ.get("SAM_CHECKPOINT") or str(default_checkpoint(version, models_dir))
    if not Path(checkpoint).is_file():
        raise SystemExit(f"STOP: no checkpoint at {checkpoint}. Run: ./setup.sh download")
    dtype = os.environ.get("SAM_DTYPE", "bf16")
    if dtype not in ("bf16", "fp32"):
        raise SystemExit("STOP: SAM_DTYPE must be bf16 or fp32.")
    strict = os.environ.get("SAM_STRICT", "1" if version == "sam3.1" else "0") != "0"

    api_key = None
    key_file = os.environ.get("SAM_API_KEY_FILE")
    if key_file:
        api_key = Path(key_file).read_text().strip() or None
        if api_key is None:
            raise SystemExit(f"STOP: {key_file} is empty.")

    segmenter = Sam3Segmenter(version, checkpoint, dtype=dtype, strict=strict)
    warm_up(segmenter)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(segmenter, api_key))
    print(f"SAM ({version}, {dtype}) on {segmenter.device}, listening on {args.host}:{args.port}.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
