#!/usr/bin/env python3
"""
The Desk's Settings tab: keys saved to a private file outside the repo,
put into the environment the server already reads, never echoed back,
and changed only from the Desk's own page.

    python3 test_settings.py
"""

import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL",
              "AMAN_BASE", "AMAN_TOKEN", "AMAN_CHECK_SESSION_ID"):
    os.environ.pop(_name, None)
_TMP = tempfile.mkdtemp()
os.environ["TRUSS_SETTINGS"] = str(Path(_TMP) / "conf" / "settings.json")

import server as server_module  # noqa: E402
import settings  # noqa: E402

KEY = "test-meta-key-0123456789"
TOKEN = "test-aman-token-abcdef"


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


def fresh():
    p = settings.path()
    if p.exists():
        p.unlink()
    for env in settings.ENV.values():
        os.environ.pop(env, None)
    settings.reset_for_tests()
    server_module.set_checker("endpoint")


def test_save_is_private_and_applied():
    fresh()
    view = settings.save({"meta_key": KEY})
    p = settings.path()
    check("the file is written", p.is_file())
    check("the file is 0600", stat.S_IMODE(p.stat().st_mode) == 0o600)
    check("the folder is 0700", stat.S_IMODE(p.parent.stat().st_mode) == 0o700)
    check("the key is in the environment", os.environ.get("TRUSS_META_API_KEY") == KEY)
    check("the view says set, saved", view["meta_key"] == {"set": True, "from": "saved"})
    check("the view never holds the key", KEY not in json.dumps(view))


def test_empty_secret_keeps_and_clear_forgets():
    fresh()
    settings.save({"meta_key": KEY})
    settings.save({"meta_key": ""})
    check("an empty box keeps the saved key", os.environ.get("TRUSS_META_API_KEY") == KEY)
    settings.save({"clear": ["meta_key"]})
    check("clear takes it out of the environment", "TRUSS_META_API_KEY" not in os.environ)
    check("clear takes it out of the file", "meta_key" not in json.loads(settings.path().read_text()))


def test_token_dropped_on_new_address():
    fresh()
    settings.save({"aman_base": "http://127.0.0.1:8791", "aman_token": TOKEN, "aman_session": "s1"})
    check("the token is set", os.environ.get("AMAN_TOKEN") == TOKEN)
    settings.save({"aman_base": "http://127.0.0.1:8791/", "aman_session": "s1"})
    check("the same address keeps the token", os.environ.get("AMAN_TOKEN") == TOKEN)
    settings.save({"aman_base": "http://192.0.2.9:8791"})
    check("a new address drops the token", "AMAN_TOKEN" not in os.environ)
    check("and out of the file", "aman_token" not in json.loads(settings.path().read_text()))
    settings.save({"aman_base": "http://192.0.2.9:8791", "aman_token": TOKEN})
    check("a token typed with the new address is kept", os.environ.get("AMAN_TOKEN") == TOKEN)


def test_bad_values_refused():
    fresh()
    for bad, why in (({"aman_base": "ftp://x"}, "not http"),
                     ({"aman_base": "http://u:p@host"}, "credentials in the address"),
                     ({"checker": "deepseek"}, "unknown checker"),
                     ({"meta_key": "a\nb"}, "line break"),
                     ({"clear": ["nope"]}, "unknown clear")):
        try:
            settings.save(bad)
            ok = False
        except settings.SettingsError:
            ok = True
        check(f"refused: {why}", ok)


def test_apply_saved_wins_over_shell():
    fresh()
    settings.save({"meta_key": KEY, "checker": "aman"})
    settings.reset_for_tests()
    os.environ["TRUSS_META_API_KEY"] = "from-the-shell"
    checker, applied = settings.apply_saved()
    check("the saved key wins", os.environ.get("TRUSS_META_API_KEY") == KEY)
    check("the saved checker comes back", checker == "aman")
    check("applied names the key", applied == ["meta_key"])


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.TrussHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _post(url, body, headers=None, ctype="application/json"):
    h = {"Content-Type": ctype}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_routes():
    fresh()
    httpd, base = _serve()
    try:
        status, text = _post(f"{base}/settings", {"meta_key": KEY, "checker": "aman"})
        check("POST /settings saves", status == 200)
        check("the reply never holds the key", KEY not in text)
        check("the checker switched", server_module.CHECKER == "aman")
        with urllib.request.urlopen(f"{base}/settings") as r:
            body = r.read().decode()
        check("GET /settings never holds the key", KEY not in body)
        check("GET /settings says it is set", json.loads(body)["meta_key"]["set"] is True)

        status, _ = _post(f"{base}/settings", {"checker": "endpoint"}, {"Origin": "http://evil.example"})
        check("another site's page is refused", status == 403)
        check("and changed nothing", server_module.CHECKER == "aman")
        status, _ = _post(f"{base}/settings", {"checker": "endpoint"}, ctype="text/plain")
        check("a non-JSON post is refused", status == 415)
        status, _ = _post(f"{base}/settings", {"checker": "endpoint"},
                          {"Host": "rebound.example:8792", "Origin": "http://rebound.example:8792"})
        check("a name pointed at this Mac is refused", status == 403)
        status, _ = _post(f"{base}/settings", {"checker": "endpoint"}, {"Origin": base})
        check("the Desk's own page is let through", status == 200 and server_module.CHECKER == "endpoint")

        status, text = _post(f"{base}/settings/test", {"what": "aman"})
        check("the Aman test without a token says so", status == 200 and "token" in json.loads(text)["text"])
        status, _ = _post(f"{base}/settings/test", {"what": "other"})
        check("an unknown test is refused", status == 400)
    finally:
        httpd.shutdown()


def test_meta_test_never_leaks_key():
    fresh()
    # An address nothing listens on: the test fails, and its text must
    # not carry the key.
    os.environ["TRUSS_META_BASE_URL"] = "https://127.0.0.1:9/v1"
    os.environ["TRUSS_META_TIMEOUT_S"] = "2"
    try:
        settings.save({"meta_key": KEY})
        ok, text = server_module.test_meta()
        check("a dead Muse Spark fails the test", ok is False)
        check("the failure text has no key", KEY not in text)
    finally:
        os.environ.pop("TRUSS_META_BASE_URL", None)
        os.environ.pop("TRUSS_META_TIMEOUT_S", None)


if __name__ == "__main__":
    test_save_is_private_and_applied()
    test_empty_secret_keeps_and_clear_forgets()
    test_token_dropped_on_new_address()
    test_bad_values_refused()
    test_apply_saved_wins_over_shell()
    test_http_routes()
    test_meta_test_never_leaks_key()
    fresh()
    print("ALL PASS")
