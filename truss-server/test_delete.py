#!/usr/bin/env python3
"""
Deleting a question, alerts for stopped questions, and the Desk look photo.

    python3 test_delete.py
"""

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import server as server_module  # noqa: E402
from alerts import AlertStore  # noqa: E402
from questions import QuestionStore  # noqa: E402


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


def _post(base, path, data, ctype="application/json", headers=None):
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": ctype, **(headers or {})})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _get(base, path):
    with urllib.request.urlopen(base + path) as resp:
        return json.loads(resp.read())


def _multipart(data):
    body = (b"--XyZ\r\nContent-Disposition: form-data; name=\"image\"; filename=\"p.jpg\"\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + data + b"\r\n--XyZ--\r\n")
    return body, "multipart/form-data; boundary=XyZ"


def main():
    tmp = Path(tempfile.mkdtemp())
    saved = (server_module.STORE, server_module.ALERTS)
    store = QuestionStore(tmp / "questions")
    alerts = AlertStore(tmp / "alerts")
    server_module.STORE, server_module.ALERTS = store, alerts
    server_module.LATEST.update({"bytes": None, "at": None})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        keep = store.create("Is the stove off?")
        gone = store.create("Did I take my test pills?")
        stopped = store.create("Is the door shut?")
        for slug in (keep, gone, stopped):
            alerts.add({"id": f"a-{slug}", "slug": slug, "date": "2026-11-03", "line": f"About {slug}."})

        listed = {q["slug"] for q in _get(base, "/questions")["questions"]}
        check("all three are listed at first", {keep, gone, stopped} <= listed)
        check("all three alerts are offered at first", len(_get(base, "/alerts")["alerts"]) == 3)

        reply = _post(base, "/questions/delete", json.dumps({"slug": gone}).encode())
        check("delete answers deleted", reply.get("deleted") is True and reply["state"] == "retired")
        _post(base, "/questions/retire", json.dumps({"slug": stopped}).encode())

        listed = {q["slug"] for q in _get(base, "/questions")["questions"]}
        check("a deleted question is not listed", gone not in listed)
        check("a stopped question is still listed (as stopped)", stopped in listed and keep in listed)
        offered = [a["id"] for a in _get(base, "/alerts")["alerts"]]
        check(f"only the live question's alert is offered ({offered})", offered == [f"a-{keep}"])
        check("a deleted question is never due", gone not in {e["slug"] for e in store.due()})
        store.mark_answered(gone)
        check("a check landing after the delete leaves it retired", store.get(gone)["state"] == "retired")

        for bad in (b"{}", json.dumps({"slug": "../x"}).encode(), json.dumps({"slug": "nope"}).encode()):
            try:
                _post(base, "/questions/delete", bad)
                check(f"delete {bad!r} is refused", False)
            except urllib.error.HTTPError as e:
                check(f"delete {bad!r} is refused", e.code in (400, 404))

        # The Desk's look photo: kept as the view at once, with no checks,
        # and not waiting behind a capture that holds the write lock.
        with server_module.WRITE_LOCK:
            body, ctype = _multipart(b"\xff\xd8look")
            reply = _post(base, "/watch/look/photo", body, ctype)
        check("the look photo is kept as the glasses' view", server_module.LATEST["bytes"] == b"\xff\xd8look" and reply["bytes"] == 6)
        body, ctype = _multipart(b"\xff\xd8desk")
        try:
            _post(base, "/watch/look/photo", body, ctype, headers={"Origin": base})
            check("a browser's look photo is refused", False)
        except urllib.error.HTTPError as e:
            check("a browser's look photo is refused", e.code == 403)
        check("and the view is unchanged", server_module.LATEST["bytes"] == b"\xff\xd8look")
    finally:
        httpd.shutdown()
        server_module.STORE, server_module.ALERTS = saved
        server_module.LATEST.update({"bytes": None, "at": None})


if __name__ == "__main__":
    main()
    print("ALL PASS")
