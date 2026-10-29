#!/usr/bin/env python3
"""
Tests for the DGX SAM service (sam_service.py), with a fake model in
place of SAM, so they run anywhere numpy and Pillow are installed (the
service's venv has both). Run:  python3 dgx/sam/test_sam_service.py

The fake finds a "stove" as a filled rectangle and a "knob" as a small
disc that spills one pixel outside its box, so the mask cut and the
inside_box_raw number are exercised. The last tests run the real
handler over HTTP and read it back with the laptop's own client
(truss-server/sam_client.py), so the two sides are tested together.
"""

import base64
import io
import json
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "truss-server"))

import sam_client  # noqa: E402
import sam_service as svc  # noqa: E402


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


class FakeSam:
    name = "fake-sam3"
    device = "cpu"

    def __init__(self):
        self.images = 0

    def set_image(self, image):
        self.images += 1
        self.size = image.size

    def segment(self, phrase, threshold):
        w, h = self.size
        if phrase == "stove":
            m = np.zeros((h, w), bool)
            m[20:60, 30:90] = True
            return np.array([[30, 20, 90, 60]], float), np.array([0.9]), m[None]
        if phrase == "knob":
            yy, xx = np.mgrid[0:h, 0:w]
            m = (yy - 80) ** 2 + (xx - 40) ** 2 <= 36
            # The box is a pixel short on the left, so one column of the
            # disc lies outside it until the cut.
            return np.array([[35, 74, 46, 86]], float), np.array([0.7]), m[None]
        if phrase == "spill":
            # A mask mostly outside its box: what a badly loaded
            # checkpoint looks like. The cut hides it; the raw check must not.
            m = np.zeros((h, w), bool)
            m[10:50, 10:50] = True
            return np.array([[40, 40, 50, 50]], float), np.array([0.8]), m[None]
        if phrase == "explode":
            raise RuntimeError("CUDA out of memory")
        return np.zeros((0, 4)), np.zeros(0), np.zeros((0, h, w), bool)


def jpeg_bytes(w=120, h=100, color=(200, 200, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


def body(phrases, **extra):
    req = {"image_b64": base64.b64encode(jpeg_bytes()).decode(), "phrases": phrases}
    req.update(extra)
    return json.dumps(req).encode()


def decode(rle):
    h, w = rle["size"]
    flat = np.zeros(h * w, bool)
    for a, b in sam_client.rle_runs(rle):
        flat[a:b] = True
    return flat.reshape(h, w)


def test_rle_round_trip():
    rng = np.random.default_rng(3)
    for name, m in [
        ("random", rng.random((17, 23)) > 0.6),
        ("starts on", np.ones((4, 5), bool)),
        ("empty", np.zeros((4, 5), bool)),
    ]:
        rle = svc.rle_encode(m)
        check(f"rle round trip through the laptop's decoder: {name}",
              np.array_equal(decode(rle), m) and sam_client.rle_area(rle) == int(m.sum()))


def test_inside_and_crop():
    m = np.zeros((10, 10), bool)
    m[2:6, 2:6] = True
    check("all inside", svc.inside_fraction(m, [2, 2, 6, 6]) == 1.0)
    check("half inside", svc.inside_fraction(m, [2, 2, 4, 6]) == 0.5)
    cut = svc.crop_to_box(m, [2, 2, 4, 6])
    check("the cut keeps only the box", int(cut.sum()) == 8
          and sam_client.rle_outside_box(svc.rle_encode(cut), [2, 2, 4, 6]) == 0)
    check("an empty mask is inside", svc.inside_fraction(np.zeros((3, 3), bool), [0, 0, 1, 1]) == 1.0)


def test_boundary_and_outline():
    m = np.zeros((40, 40), bool)
    m[10:30, 10:30] = True
    edge = svc.boundary(m)
    check("the boundary is the ring of the square", int(edge.sum()) == 76
          and edge[10, 15] and not edge[15, 15])
    m2 = np.zeros((20, 20), bool)
    m2[:, :8] = True
    check("an object cut off by the frame is closed off at the frame",
          svc.boundary(m2)[0, 3] and svc.boundary(m2)[19, 3] and svc.boundary(m2)[10, 0])
    img = Image.new("RGB", (40, 40), (200, 200, 200))
    out = np.array(svc.draw_outline(img, m))
    check("the line is drawn on the edge", tuple(out[10, 20]) == svc.LINE)
    check("the inside is untouched", tuple(out[20, 20]) == (200, 200, 200))
    check("far outside is untouched", tuple(out[0, 0]) == (200, 200, 200))
    check("the source photo is not changed", np.array(img)[10, 20].tolist() == [200, 200, 200])


def test_parse_refusals():
    good = json.loads(body(["stove"]))
    for name, change in [
        ("not json", None),
        ("no phrases", {"phrases": []}),
        ("phrase not text", {"phrases": [3]}),
        ("phrase too long", {"phrases": ["x" * 61]}),
        ("too many phrases", {"phrases": [f"p{i}" for i in range(9)]}),
        ("threshold out of range", {"threshold": 0.99}),
        ("threshold a bool", {"threshold": True}),
        ("masks not a bool", {"masks": "yes"}),
        ("no image", {"image_b64": ""}),
        ("image not base64", {"image_b64": "@@@"}),
        ("image not a photo", {"image_b64": base64.b64encode(b"hello").decode()}),
    ]:
        raw = b"{" if change is None else json.dumps({**good, **change}).encode()
        try:
            svc.parse_request(raw)
            check(f"refused: {name}", False)
        except svc.RequestError as e:
            check(f"refused: {name}", e.status == 400)
    img, phrases, thr, masks = svc.parse_request(json.dumps({**good, "phrases": [" stove ", "stove", "pill  organizer"]}).encode())
    check("phrases are tidied and deduplicated", phrases == ["stove", "pill organizer"])
    check("defaults", thr == svc.DEFAULT_THRESHOLD and masks is True and img.size == (120, 100))


def test_exif_orientation_is_applied():
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 on display
    Image.new("RGB", (120, 100)).save(buf, format="JPEG", exif=exif)
    img = svc.decode_image(buf.getvalue())
    check("coordinates are of the upright photo", img.size == (100, 120))


def test_run_union():
    fake = FakeSam()
    img = Image.new("RGB", (120, 100), (200, 200, 200))
    r = svc.run(fake, img, ["stove", "knob", "kettle"], 0.5, True, outline=False)
    check("one image pass for all phrases", fake.images == 1)
    by = {p["phrase"]: p["detections"] for p in r["phrases"]}
    check("per-phrase detections", len(by["stove"]) == 1 and len(by["knob"]) == 1 and by["kettle"] == [])
    knob = by["knob"][0]
    check("the knob's raw mask spilled outside its box", knob["inside_box_raw"] < 1.0)
    check("every mask is inside its box after the cut", all(
        sam_client.rle_outside_box(d["mask"], d["box"]) == 0 for ds in by.values() for d in ds))
    u = r["union"]
    check("union counts both and names the phrases", u["count"] == 2
          and u["phrases_matched"] == ["stove", "knob"])
    check("union area is the two masks", u["area"] == by["stove"][0]["area"] + knob["area"]
          and sam_client.rle_area(u["mask"]) == u["area"])
    check("union box spans both", u["box"][0] == 30 and u["box"][1] == 20 and u["box"][3] == 86)
    check("timings", set(r["timing_ms"]) == {"image", "phrases", "draw", "total"}
          and set(r["timing_ms"]["phrases"]) == {"stove", "knob", "kettle"})
    r = svc.run(fake, img, ["kettle"], 0.5, True, outline=True)
    check("outline with no match sends no image", r["image_b64"] is None and r["union"]["box"] is None)


def serve(api_key=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), svc.make_handler(FakeSam(), api_key))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def raw_post(url, data, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_with_the_laptop_client():
    server, url = serve(api_key="lan-key")
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as resp:
            health = json.loads(resp.read())
        check("health needs no key", health["ok"] and health["model"] == "fake-sam3")
        status, _ = raw_post(url + "/segment", body(["stove"]), {"Content-Type": "application/json"})
        check("a POST without the key is refused", status == 401)
        status, _ = raw_post(url + "/segment", body(["stove"]), {"Authorization": "Bearer wrong"})
        check("a wrong key is refused", status == 401)
        status, reply = raw_post(url + "/segment", body(["explode"]), {"Authorization": "Bearer lan-key"})
        check("a model failure is a 500 that names only the error type",
              status == 500 and reply["error"] == "SAM failed: RuntimeError")
        status, _ = raw_post(url + "/nope", b"{}", {"Authorization": "Bearer lan-key"})
        check("unknown paths are 404", status == 404)

        cfg = sam_client.SamConfig(url, "lan-key", 5.0)
        tmp = Path(tempfile.mkdtemp())
        photo = tmp / "210500_is-the-stove-off.jpg"
        photo.write_bytes(jpeg_bytes())
        reply = sam_client.segment(photo, ["stove", "knob"], config=cfg)
        check("the laptop client reads the service's reply", sam_client.has_match(reply)
              and reply["union"]["count"] == 2)
        dest = sam_client.outline_path(photo)
        reply = sam_client.outline(photo, ["stove"], dest, config=cfg)
        check("the outlined photo is written beside the evidence", reply["written"] and dest.exists())
        out = np.array(Image.open(dest))
        line = np.array(svc.LINE)
        check("it shows the outline on the stove's edge",
              np.abs(out[20, 60].astype(int) - line).max() < 60)
        check("and leaves the middle of the stove alone",
              np.abs(out[40, 60].astype(int) - 200).max() < 20)
        check("the evidence photo itself is unchanged", photo.read_bytes() == jpeg_bytes())
    finally:
        server.shutdown()


def test_acceptance_script():
    import os
    import subprocess

    server, url = serve()
    try:
        tmp = Path(tempfile.mkdtemp())
        photos = []
        for i in range(3):
            p = tmp / f"glasses-{i}.jpg"
            p.write_bytes(jpeg_bytes())
            photos.append(str(p))
        env = {**os.environ, "TRUSS_SAM_URL": url}
        run = subprocess.run(
            [sys.executable, str(HERE / "sam_accept.py"), *photos, "--phrases", "stove,knob",
             "--out", str(tmp / "out")],
            env=env, capture_output=True, text=True, timeout=60)
        summary = (tmp / "out" / "summary.md").read_text()
        check("the acceptance run passes on three matching photos", run.returncode == 0 and "**PASS**" in summary)
        check("it writes one outlined photo each", len(list((tmp / "out").glob("*_outline.jpg"))) == 3)
        rows = [json.loads(line) for line in (tmp / "out" / "rows.jsonl").read_text().splitlines()]
        check("each row has a time per frame and masks inside boxes",
              all(r["segment_ms"] >= 0 and r["masks_inside"] for r in rows))
        run = subprocess.run(
            [sys.executable, str(HERE / "sam_accept.py"), *photos[:2], "--phrases", "kettle",
             "--out", str(tmp / "out2")],
            env=env, capture_output=True, text=True, timeout=60)
        summary = (tmp / "out2" / "summary.md").read_text()
        run3 = subprocess.run(
            [sys.executable, str(HERE / "sam_accept.py"), *photos, "--phrases", "spill",
             "--out", str(tmp / "out3")],
            env=env, capture_output=True, text=True, timeout=60)
        summary3 = (tmp / "out3" / "summary.md").read_text()
        check("masks that spill out of their boxes before the cut fail the run",
              run3.returncode == 1 and "FAIL: before the cut" in summary3 and "**FAIL**" in summary3)
        check("two photos with no match fail, saying why", run.returncode == 1
              and "needs three" in summary and "no photo got an outline" in summary)
    finally:
        server.shutdown()


if __name__ == "__main__":
    test_rle_round_trip()
    test_inside_and_crop()
    test_boundary_and_outline()
    test_parse_refusals()
    test_exif_orientation_is_applied()
    test_run_union()
    test_http_with_the_laptop_client()
    test_acceptance_script()
    print("ALL PASS")
