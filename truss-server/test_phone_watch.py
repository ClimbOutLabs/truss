#!/usr/bin/env python3
"""
The Desk's start and stop for the phone's watching.

    python3 test_phone_watch.py
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

import phone_watch  # noqa: E402
import server as server_module  # noqa: E402


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def test_store():
    clock = Clock()
    desk = phone_watch.WatchDesk(clock)
    check("no request and no phone at first", desk.view() == {"request": None, "phone": None, "look": None})
    reply = desk.report({"watching": False}, "10.0.0.5")
    check("a check-in with no request gets none", reply["request"] is None)

    first = desk.ask(True)["request"]
    check("a start request is on", first["on"] is True and first["age_seconds"] == 0)
    clock.now += timedelta(seconds=7)
    reply = desk.report({"watching": False, "glasses": True, "every": 30}, "10.0.0.5")
    check("the phone sees the request and its age", reply["request"]["id"] == first["id"] and reply["request"]["age_seconds"] == 7)

    second = desk.ask(False)["request"]
    check("each request has a new id", second["id"] != first["id"] and second["on"] is False)

    view = desk.view()
    check("the phone's state is kept", view["phone"]["glasses"] is True and view["phone"]["every"] == 30)
    check("a phone just heard from is reachable", view["phone"]["reachable"] is True)
    clock.now += timedelta(seconds=phone_watch.PHONE_QUIET_SECONDS + 1)
    check("a quiet phone is not reachable", desk.view()["phone"]["reachable"] is False)

    for bad in (None, "yes", 1):
        try:
            desk.ask(bad)
            check(f"ask({bad!r}) is refused", False)
        except ValueError:
            check(f"ask({bad!r}) is refused", True)
    check("a refused ask leaves the request", desk.view()["request"]["id"] == second["id"])
    try:
        desk.report([1], "x")
        check("a non-object check-in is refused", False)
    except ValueError:
        check("a non-object check-in is refused", True)
    desk.report({"watching": "yes", "every": True}, "x")
    p = desk.view()["phone"]
    check("only real true counts and a bool is no cadence", p["watching"] is False and p["every"] is None)
    desk.report({"watching": True, "mode": "live", "off": True}, "x")
    p = desk.view()["phone"]
    check("the capture mode and glasses-off are kept", p["mode"] == "live" and p["off"] is True)
    desk.report({"watching": True, "mode": "video!", "off": "yes"}, "x")
    p = desk.view()["phone"]
    check("an unknown mode and a non-bool off are dropped", p["mode"] is None and p["off"] is False)


def test_start_stays_actionable_while_unmet():
    # An ask the phone has not met stays worth acting on past the
    # fresh minute, up to the cap: check-ins can be blocked for
    # minutes (the phone on the glasses' Wi-Fi, the app asleep), and
    # an ask first seen when old was dropped for good. Past the cap
    # it ages out, so a phone that opens the app much later never
    # starts on an old start.
    check("a start is worth acting on for a minute", phone_watch.FRESH_SECONDS == 60)
    clock = Clock()
    desk = phone_watch.WatchDesk(clock)
    desk.ask(True)
    clock.now += timedelta(seconds=phone_watch.FRESH_SECONDS + 1)
    reply = desk.report({"watching": False}, "x")
    check("an unmet start past a minute is still actionable", reply["request"]["age_seconds"] <= reply["fresh_seconds"])
    clock.now += timedelta(seconds=phone_watch.ACT_CAP_SECONDS)
    reply = desk.report({"watching": False}, "x")
    check("an unmet start past the cap ages out", reply["request"]["age_seconds"] > reply["fresh_seconds"])
    # A met ask needs no widening: the phone is already watching.
    clock2 = Clock()
    desk2 = phone_watch.WatchDesk(clock2)
    desk2.ask(True)
    clock2.now += timedelta(seconds=120)
    reply = desk2.report({"watching": True}, "x")
    check("a met start keeps the plain fresh minute", reply["fresh_seconds"] == phone_watch.FRESH_SECONDS and reply["request"]["age_seconds"] > reply["fresh_seconds"])


def test_look():
    clock = Clock()
    desk = phone_watch.WatchDesk(clock)
    check("no look at first", desk.report({}, "x")["look"] is None)
    first = desk.look()["look"]
    clock.now += timedelta(seconds=5)
    reply = desk.report({"watching": True}, "x")
    check("the phone sees the look and its age", reply["look"]["id"] == first["id"] and reply["look"]["age_seconds"] == 5)
    check("a look leaves start and stop alone", reply["request"] is None)
    second = desk.look()["look"]
    check("each look has a new id", second["id"] != first["id"])
    clock.now += timedelta(seconds=phone_watch.LOOK_FRESH_SECONDS + 1)
    check("a stale look is not offered", desk.report({}, "x")["look"] is None)


def _post(port, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_http():
    server_module.WATCH = phone_watch.WatchDesk()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # Held write lock: the watch routes must not wait behind a capture.
        with server_module.WRITE_LOCK:
            asked = _post(port, "/watch", json.dumps({"on": True}).encode())
            check("POST /watch leaves a start request", asked["request"]["on"] is True)
            reply = _post(port, "/watch/phone", json.dumps({"watching": False, "glasses": True}).encode())
            check("POST /watch/phone returns it", reply["request"]["id"] == asked["request"]["id"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/watch") as resp:
            view = json.loads(resp.read())
        check("GET /watch shows the phone", view["phone"]["glasses"] is True and view["phone"]["reachable"])
        for body in (b"not json", json.dumps({"on": "yes"}).encode()):
            try:
                _post(port, "/watch", body)
                check(f"bad body {body!r} is refused", False)
            except urllib.error.HTTPError as e:
                check(f"bad body {body!r} is refused", e.code == 400)
        check("the start request still stands", server_module.WATCH.view()["request"]["id"] == asked["request"]["id"])
        with server_module.WRITE_LOCK:
            looked = _post(port, "/watch/look", b"{}")
            check("POST /watch/look leaves a look, not waiting behind a capture", looked["look"]["id"])
            reply = _post(port, "/watch/phone", json.dumps({"watching": True}).encode())
            check("the phone's check-in carries the look", reply["look"]["id"] == looked["look"]["id"])
        check("a look leaves the start request standing", server_module.WATCH.view()["request"]["id"] == asked["request"]["id"])
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    test_store()
    test_start_stays_actionable_while_unmet()
    test_look()
    test_http()
    print("ALL PASS")
