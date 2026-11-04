#!/usr/bin/env python3
"""
The Desk's capture strip: GET /captures lists the newest photos the
phone sent, newest first, and GET /captures/photo serves one of them.
Only plain capture names inside the captures folder are ever served.

    python3 test_captures.py
"""

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import server as server_module  # noqa: E402


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")


def _status(port, path):
    try:
        _get(port, path).read()
        return 200
    except urllib.error.HTTPError as e:
        return e.code


def _serve():
    httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _name(second):
    return f"capture_20261103_1200{second:02d}_000000.jpg"


def test_list_and_photo():
    tmp = Path(tempfile.mkdtemp())
    caps = tmp / "captures"
    caps.mkdir()
    for second in range(14):
        (caps / _name(second)).write_bytes(f"jpeg-{second}".encode())
    # Files that are not captures are never listed or served.
    (caps / "notes.txt").write_text("not a photo")
    (caps / "capture_loose.jpg").write_bytes(b"jpeg-loose")
    (caps / "ref-yes.jpg").write_bytes(b"jpeg-ref")
    saved = server_module.CAPTURES_DIR
    server_module.CAPTURES_DIR = caps
    httpd, port = _serve()
    try:
        with _get(port, "/captures") as resp:
            body = json.loads(resp.read())
        items = body["captures"]
        check("GET /captures lists at most 12", len(items) == 12)
        check("newest first", items[0]["name"] == _name(13) and items[-1]["name"] == _name(2))
        check("each item carries its time", items[0]["at"] == "2026-11-03T12:00:13")
        check("only capture names are listed",
              all(i["name"].startswith("capture_2026") for i in items))
        with _get(port, f"/captures/photo?name={_name(13)}") as resp:
            check("a listed photo is served", resp.read() == b"jpeg-13")
            check("as a JPEG", resp.headers["Content-Type"] == "image/jpeg")
        check("a name outside the folder is refused",
              _status(port, "/captures/photo?name=..%2Fserver.py") == 404)
        check("a plant look is not served here",
              _status(port, "/captures/photo?name=ref-yes.jpg") == 404)
        check("a loose capture name is refused",
              _status(port, "/captures/photo?name=capture_loose.jpg") == 404)
        check("a capture that is not there is a 404",
              _status(port, f"/captures/photo?name={_name(59)}") == 404)
        check("no name is a 404", _status(port, "/captures/photo") == 404)
    finally:
        server_module.CAPTURES_DIR = saved
        httpd.shutdown()


class _QuietLoop:
    def process_frame(self, path):
        return {"managed": False, "fired": [], "speak": [], "errors": 0}


def _multipart(data):
    boundary = "xTRUSSx"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"f.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_a_posted_capture_appears():
    from questions import QuestionStore
    tmp = Path(tempfile.mkdtemp())
    caps = tmp / "captures"
    caps.mkdir()
    saved = (server_module.CAPTURES_DIR, server_module.LOOP, server_module.STORE)
    server_module.CAPTURES_DIR = caps
    server_module.LOOP = _QuietLoop()
    server_module.STORE = QuestionStore(tmp / "questions")
    server_module.GUIDE["session"] = None
    httpd, port = _serve()
    try:
        with _get(port, "/captures") as resp:
            check("an empty folder lists nothing", json.loads(resp.read())["captures"] == [])
        body, ctype = _multipart(b"\xff\xd8phone")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/capture", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        urllib.request.urlopen(req).read()
        with _get(port, "/captures") as resp:
            items = json.loads(resp.read())["captures"]
        check("the posted capture is listed", len(items) == 1)
        with _get(port, f"/captures/photo?name={items[0]['name']}") as resp:
            check("and its photo is the posted one", resp.read() == b"\xff\xd8phone")
    finally:
        server_module.CAPTURES_DIR, server_module.LOOP, server_module.STORE = saved
        httpd.shutdown()


if __name__ == "__main__":
    test_list_and_photo()
    test_a_posted_capture_appears()
    print("ALL PASS")
