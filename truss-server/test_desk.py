#!/usr/bin/env python3
"""
The desk page: GET /desk serves the one static page, and the page only
calls endpoints the server really has.

    python3 test_desk.py
"""

import re
import urllib.error
import sys
import threading
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


def test_desk_is_served():
    httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/desk") as resp:
            body = resp.read().decode()
            check("GET /desk is HTML", resp.headers["Content-Type"].startswith("text/html"))
            check("GET /desk is the desk page", "<title>Truss Desk</title>" in body)
    finally:
        httpd.shutdown()


def test_page_calls_only_real_endpoints():
    page = (Path(__file__).parent / "desk.html").read_text()
    source = (Path(__file__).parent / "server.py").read_text()
    routes = set(re.findall(r'path == "(/[a-z/]*)"', source))
    called = set(re.findall(r'["`](/[a-z][a-z/]*)[?"`$]', page))
    called -= {"/desk"}
    check(f"the page calls {sorted(called)}", bool(called))
    missing = called - routes
    check(f"every call has a route (missing: {sorted(missing)})", not missing)


def test_latest_view():
    import urllib.error
    server_module.LATEST.update({"bytes": None, "at": None})
    httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/latest")
            check("no view yet is a 404", False)
        except urllib.error.HTTPError as e:
            check("no view yet is a 404", e.code == 404)
        server_module.keep_latest(b"\xff\xd8jpeg")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/latest") as resp:
            check("the latest view is served", resp.read() == b"\xff\xd8jpeg")
            check("as a JPEG", resp.headers["Content-Type"] == "image/jpeg")
            check("with its age", float(resp.headers["X-Seconds-Ago"]) < 1)
        server_module.LATEST["at"] -= server_module.LATEST_MAX_AGE_S + 1
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/latest")
            check("a stale view is a 404", False)
        except urllib.error.HTTPError as e:
            check("a stale view is a 404", e.code == 404)
    finally:
        server_module.LATEST.update({"bytes": None, "at": None})
        httpd.shutdown()


def _multipart(data):
    boundary = "xTRUSSx"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"f.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class _QuietLoop:
    def process_frame(self, path):
        return {"managed": False, "fired": [], "speak": [], "errors": 0}


def test_replay_is_not_the_glasses_view():
    import tempfile
    from questions import QuestionStore
    tmp = Path(tempfile.mkdtemp())
    saved = (server_module.CAPTURES_DIR, server_module.LOOP, server_module.STORE)
    server_module.CAPTURES_DIR = tmp / "captures"
    server_module.CAPTURES_DIR.mkdir()
    server_module.LOOP = _QuietLoop()
    server_module.STORE = QuestionStore(tmp / "questions")
    server_module.GUIDE["session"] = None
    server_module.LATEST.update({"bytes": None, "at": None})
    httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body, ctype = _multipart(b"\xff\xd8replayed")
        req = urllib.request.Request(f"{base}/capture", data=body, method="POST",
                                     headers={"Content-Type": ctype, "Origin": base})
        urllib.request.urlopen(req).read()
        check("a photo replayed from the Desk is not kept as the view", server_module.LATEST["bytes"] is None)
        body, ctype = _multipart(b"\xff\xd8phone")
        req = urllib.request.Request(f"{base}/capture", data=body, method="POST",
                                     headers={"Content-Type": ctype})
        urllib.request.urlopen(req).read()
        check("the phone's frame is kept as the view", server_module.LATEST["bytes"] == b"\xff\xd8phone")

    finally:
        server_module.CAPTURES_DIR, server_module.LOOP, server_module.STORE = saved
        server_module.LATEST.update({"bytes": None, "at": None})

def test_status_shows_a_check_in_flight():
    """While a slow check runs, GET /status answers at once and names
    it; once it finishes, the line goes. The check's result and any
    error pass through the wrapper untouched."""
    import json
    from http.server import ThreadingHTTPServer
    started, release = threading.Event(), threading.Event()

    def slow(frame, entry):
        started.set()
        release.wait(10)
        return {"verdict": "yes"}

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    out = {}
    worker = threading.Thread(
        target=lambda: out.update(r=server_module._checking(slow)("f.jpg", {"question": "Did I take my pill?"})))
    try:
        check("nothing is being checked at first", server_module.checking_view() is None)
        worker.start()
        started.wait(5)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as resp:
            body = json.loads(resp.read())
        check("GET /status answers during a check", body["checking"] is not None)
        check("it names the question", body["checking"]["question"] == "Did I take my pill?")
        release.set()
        worker.join(5)
        check("the result passes through", out.get("r") == {"verdict": "yes"})
        check("the line goes when the check ends", server_module.checking_view() is None)

        def boom(frame, entry):
            raise RuntimeError("down")
        try:
            server_module._checking(boom)("f.jpg", "Is step one done?")
            check("an error passes through", False)
        except RuntimeError:
            check("an error passes through", True)
        check("and the line goes after an error", server_module.checking_view() is None)
    finally:
        release.set()
        httpd.shutdown()


if __name__ == "__main__":
    test_desk_is_served()
    test_page_calls_only_real_endpoints()
    test_latest_view()
    test_replay_is_not_the_glasses_view()
    test_status_shows_a_check_in_flight()
    print("ALL PASS")
