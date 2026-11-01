#!/usr/bin/env python3
"""
Tests for the standing-question store and the planting endpoints.

Run from the truss-server folder:  python3 test_questions.py
Stdlib only. Exits non-zero on the first failure.
"""

import json
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from questions import QuestionStore  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"dummy-jpeg-bytes" + b"\xff\xd9"


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def test_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create("Is the stove off?")
        check("slug from question text", slug == "is-the-stove-off")
        entry = store.get(slug)
        check("new question is planting", entry["state"] == "planting")
        check("no refs yet", not entry["has_ref_yes"] and not entry["has_ref_no"])

        state = store.save_ref(slug, "yes", JPEG)
        check("one ref still planting", state == "planting")
        state = store.save_ref(slug, "no", JPEG)
        check("both refs make it standing", state == "standing")

        lone = store.create("Is my heart rate above 160?")
        store.save_ref(lone, "yes", JPEG)
        check("start_standing with one look", store.start_standing(lone) == "standing")
        check("start_standing leaves a standing one alone", store.start_standing(slug) == "standing")
        store.retire(lone)
        check("start_standing never revives a retired one", store.start_standing(lone) == "retired")
        check(
            "ref files on disk",
            (Path(tmp) / slug / "ref-yes.jpg").read_bytes() == JPEG
            and (Path(tmp) / slug / "ref-no.jpg").read_bytes() == JPEG,
        )

        slug2 = store.create("Is the stove off?")
        check("duplicate text gets a fresh slug", slug2 == "is-the-stove-off-2")

        # Interval due logic, driven by explicit times.
        planted = datetime.now()
        check(
            "fresh interval question not due",
            store.due(now=planted + timedelta(minutes=5)) == [],
        )
        due = store.due(now=planted + timedelta(minutes=16))
        check(
            "interval question due after its minutes",
            [e["slug"] for e in due] == ["is-the-stove-off"],
        )

        store.mark_answered(slug)
        check("interval question retires on answer", store.get(slug)["state"] == "answered")

        slug3 = store.create(
            "Is the front door locked?", {"kind": "nightly", "after": "20:00"}
        )
        store.save_ref(slug3, "yes", JPEG)
        store.save_ref(slug3, "no", JPEG)
        evening = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)
        morning = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        check(
            "nightly question due in the evening",
            any(e["slug"] == slug3 for e in store.due(now=evening)),
        )
        check(
            "nightly question not due in the morning",
            not any(e["slug"] == slug3 for e in store.due(now=morning)),
        )
        store.mark_answered(slug3, when=evening)
        check(
            "nightly question re-arms after answering",
            store.get(slug3)["state"] == "standing",
        )


def multipart(image_bytes):
    boundary = "----truss-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="frame.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + image_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def post(url, body, content_type):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def get_json(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def test_http():
    import server as server_module

    with tempfile.TemporaryDirectory() as tmp:
        server_module.STORE = QuestionStore(tmp)
        httpd = HTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            status, body = post(
                base + "/plant/question",
                json.dumps({"text": "Is the stove off?"}).encode(),
                "application/json",
            )
            check("plant question returns 200", status == 200)
            check("plant question starts planting", body["state"] == "planting")
            check("first prompt asks for the yes look", "yes" in body["next"])
            slug = body["slug"]

            img, ctype = multipart(JPEG)
            status, body = post(
                f"{base}/plant/ref?slug={slug}&kind=yes", img, ctype
            )
            check("yes ref accepted", status == 200 and body["state"] == "planting")
            check("second prompt asks for the no look", "no" in body["next"])

            status, body = post(
                f"{base}/plant/ref?slug={slug}&kind=no", img, ctype
            )
            check("no ref completes planting", status == 200 and body["state"] == "standing")
            check(
                "confirmation names the question",
                body["next"] == "Standing question planted: Is the stove off?",
            )

            listing = get_json(base + "/questions")
            check("list shows one question", len(listing["questions"]) == 1)
            entry = listing["questions"][0]
            check(
                "listed question is standing with both refs",
                entry["state"] == "standing"
                and entry["has_ref_yes"]
                and entry["has_ref_no"],
            )

            status, _ = post(
                f"{base}/plant/ref?slug=nope&kind=yes", img, ctype
            )
            check("bogus slug refused with 404", status == 404)

            # A number on a display: the yes look alone, then start it.
            status, body = post(
                base + "/plant/question",
                json.dumps({"text": "Is my heart rate above 160?"}).encode(),
                "application/json",
            )
            hr = body["slug"]
            post(f"{base}/plant/ref?slug={hr}&kind=yes", img, ctype)
            status, body = post(
                base + "/plant/finish", json.dumps({"slug": hr}).encode(), "application/json"
            )
            check("finish with the yes look alone stands", status == 200 and body["state"] == "standing")
            check("finish names the question",
                  body["next"] == "Standing question planted: Is my heart rate above 160?")

            # No looks at all.
            status, body = post(
                base + "/plant/question",
                json.dumps({"text": "Is the treadmill speed below 5?"}).encode(),
                "application/json",
            )
            bare = body["slug"]
            status, body = post(
                base + "/plant/finish", json.dumps({"slug": bare}).encode(), "application/json"
            )
            check("finish with no looks stands", status == 200 and body["state"] == "standing")

            status, _ = post(
                base + "/plant/finish", json.dumps({"slug": "nope"}).encode(), "application/json"
            )
            check("finish on a bogus slug is 404", status == 404)
            status, _ = post(base + "/plant/finish", b"{}", "application/json")
            check("finish with no slug is 400", status == 400)

            # The check for a one-look question sends no reference looks.
            seen = {}

            def fake_check(frame, question, **kw):
                seen.update(kw)
                return {"verdict": "cannot_tell"}

            real = server_module.verdict.check
            server_module.verdict.check = fake_check
            try:
                server_module.production_check("frame.jpg", server_module.STORE.get(hr))
                check("one-look check sends no refs",
                      seen.get("ref_yes") is None and seen.get("ref_no") is None)
                server_module.production_check("frame.jpg", server_module.STORE.get(slug))
                check("two-look check still sends both refs",
                      seen.get("ref_yes", "").endswith("ref-yes.jpg")
                      and seen.get("ref_no", "").endswith("ref-no.jpg"))
            finally:
                server_module.verdict.check = real
        finally:
            httpd.shutdown()


def test_nightly_regressions():
    """A nightly question must not re-fire all evening: after a check it
    waits out its gap, and after an answer it is done until tomorrow."""
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        slug = store.create(
            "Is the front door locked?", {"kind": "nightly", "after": "20:00"}
        )
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)
        evening = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)

        store.mark_checked(slug, when=evening)
        check(
            "nightly question waits out its gap after a check",
            not any(
                e["slug"] == slug
                for e in store.due(now=evening + timedelta(minutes=5))
            ),
        )
        check(
            "nightly question is due again after the gap",
            any(
                e["slug"] == slug
                for e in store.due(now=evening + timedelta(minutes=20))
            ),
        )

        store.mark_answered(slug, when=evening)
        check(
            "answered nightly question is not due again that evening",
            not any(
                e["slug"] == slug
                for e in store.due(now=evening + timedelta(minutes=30))
            ),
        )
        check(
            "answered nightly question is due the next evening",
            any(
                e["slug"] == slug
                for e in store.due(now=evening + timedelta(days=1))
            ),
        )


def test_retired_stays_retired():
    """A question removed while its check was in flight stays removed when
    the firing lands, for every pattern and for a done question's witness."""
    with tempfile.TemporaryDirectory() as tmp:
        store = QuestionStore(tmp)
        evening = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)
        for text, pattern in (
            ("Is the front door locked?", {"kind": "nightly", "after": "20:00"}),
            ("Is the stove off?", {"kind": "interval", "minutes": 0}),
        ):
            slug = store.create(text, pattern)
            store.save_ref(slug, "yes", JPEG)
            store.save_ref(slug, "no", JPEG)
            store.retire(slug)
            store.mark_answered(slug, when=evening)
            check(f"retired {pattern['kind']} question stays retired after a firing",
                  store.get(slug)["state"] == "retired")
            check(f"retired {pattern['kind']} question is never due again",
                  not any(e["slug"] == slug
                          for e in store.due(now=evening + timedelta(days=1))))
        slug = store.create(
            "Did I take my night pills?",
            {"kind": "window", "after": "21:00", "until": "22:00"},
            kind="done",
        )
        store.save_ref(slug, "yes", JPEG)
        store.save_ref(slug, "no", JPEG)
        store.retire(slug)
        store.mark_answered_with_witness(slug, "x.jpg", when=evening)
        check("retired done question stays retired after a witness",
              store.get(slug)["state"] == "retired")


if __name__ == "__main__":
    test_store()
    test_nightly_regressions()
    test_retired_stays_retired()
    test_http()
    print("ALL PASS")
