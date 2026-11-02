#!/usr/bin/env python3
"""
Truss local server.
Handles photo uploads and text-to-speech for the Truss app.
"""

import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from alerts import AlertStore, sweep_missed
import aman_check
from coverage import CheckLog
import endpoint
import sam_client
import verdict
from bedtime import plant_round
from checkloop import CheckLoop
from guide import Guide
import history
from phone_watch import WatchDesk
from plant_compile import Compiler, compile_folder, needs_compile
from questions import QuestionStore, SafetyLooksNeeded
import settings

# Get the Wi-Fi interface address
def get_wifi_address():
    try:
        result = subprocess.run(
            ["ipconfig", "getifaddr", "en0"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        print("Warning: Could not get en0 address, falling back to 0.0.0.0")
        return "0.0.0.0"

# Ensure captures directory exists
CAPTURES_DIR = Path(__file__).parent / "captures"
CAPTURES_DIR.mkdir(exist_ok=True)

# The standing-question store (see questions.py)
STORE = QuestionStore(Path(__file__).parent / "questions")

# Pending missed-window alerts for done questions (see alerts.py).
# An alert leaves this store only when the phone confirms its line
# was spoken; nothing else clears it.
ALERTS = AlertStore(Path(__file__).parent / "alerts")

# The times each done question was actually checked, kept by day
# next to the answer log (see coverage.py). Only the time is kept; the
# photo itself still goes when no question fires on it. Alerts read
# these to say how much of a window was really watched.
# The day folders the check loop keeps answers and evidence in.
LOG_DIR = Path(__file__).parent / "log"
DESK_PAGE = Path(__file__).parent / "desk.html"
CHECKS = CheckLog(LOG_DIR)


# The clock the sweep and the check loop read. Always the wall clock
# in use; tests pin it so they never depend on the time of day.
CLOCK = datetime.now


# One sweep at a time: a sweep checks for an alert and then adds it, so
# two at once (the sweep thread and a request thread) could raise the
# same alert twice.
SWEEP_LOCK = threading.Lock()


def run_sweep(now=None):
    """Raise any missed-window alerts that are due. Idempotent and
    quiet about failure: a sweep that cannot run loses nothing, the
    next one a few seconds later sees the same closed windows."""
    try:
        with SWEEP_LOCK:
            return sweep_missed(STORE, ALERTS, now=now or CLOCK(), checks=CHECKS)
    except Exception as e:
        print(f"Missed-window sweep failed: {e}")
        return []


# Which checker makes the checks. "endpoint" (the default, and the
# submission path) asks the configured OpenAI-compatible endpoint, the
# DGX's llama-server unless TRUSS_BASE_URL says otherwise; see
# endpoint.py and verdict.py. "meta" is accepted as the same thing.
# "aman" asks the owner's own Aman instance instead, a bridge that is
# never used in the demo; see aman_check.py. Any other value is not
# understood and falls back to "endpoint", said out loud here.
CHECKER = os.environ.get("TRUSS_CHECKER", "endpoint")
if CHECKER == "meta":
    CHECKER = "endpoint"
if CHECKER not in ("endpoint", "aman"):
    print("The TRUSS_CHECKER value was not understood; the endpoint checker is being used.")
    CHECKER = "endpoint"

# The endpoint checker's mode: "single" (the frame plus the question's
# looks.txt, the default) or "multi" (yes-look, no-look and frame as
# three images). See verdict.py.
CHECK_MODE = os.environ.get("TRUSS_CHECK_MODE", "single")
if CHECK_MODE not in verdict.MODES:
    print("The TRUSS_CHECK_MODE value was not understood; single mode is being used.")
    CHECK_MODE = "single"

# For a live test session only. TRUSS_PLANT_MINUTES sets how often a
# question planted with no pattern of its own (every planting from the
# app) is checked, instead of every 15 minutes; 0 checks it on every
# capture until it fires. TRUSS_BEDTIME_AFTER sets when the app's
# bedtime round starts each night, instead of 20:00. Left unset, both
# keep their usual values.
PLANT_PATTERN = None
_plant_minutes = os.environ.get("TRUSS_PLANT_MINUTES", "").strip()
if _plant_minutes:
    try:
        _minutes = float(_plant_minutes)
        if not math.isfinite(_minutes) or _minutes < 0:
            raise ValueError
        PLANT_PATTERN = {
            "kind": "interval",
            "minutes": int(_minutes) if _minutes.is_integer() else _minutes,
        }
        print(f"Questions planted from the app are checked every {_plant_minutes} minutes (TRUSS_PLANT_MINUTES).")
    except ValueError:
        PLANT_PATTERN = None
        print("The TRUSS_PLANT_MINUTES value was not understood; app plantings are checked every 15 minutes.")

BEDTIME_AFTER = os.environ.get("TRUSS_BEDTIME_AFTER", "").strip() or "20:00"
if BEDTIME_AFTER != "20:00":
    print(f"The bedtime round starts at {BEDTIME_AFTER} (TRUSS_BEDTIME_AFTER).")

if CHECKER == "aman":
    print("Checks will be made by Aman (TRUSS_CHECKER=aman).")
else:
    _where = endpoint.describe_from_env()
    if _where["model"]:
        _line = f"Checks and plant-time compiles go to {_where['model']} at {_where['base_url']}"
        if _where["fallback_model"]:
            _line += f", falling back to {_where['fallback_model']} at {_where['fallback_base_url']}"
        print(f"{_line}, {CHECK_MODE} mode.")
    if _where["config_error"]:
        # With no model at all, every check reads cannot_tell with this reason.
        print(f"Model setup: {_where['config_error']}")


def production_check(frame_path, entry):
    """The loop's check function, with the question's own reference
    looks. The endpoint checker reads the question's looks.txt from its
    folder in single mode and sends both looks as images in multi
    mode; Aman gets both looks as images."""
    # A question started with one look or none (see /plant/finish) is
    # checked on its words and the frame: the looks go only as a pair.
    both = Path(entry["ref_yes_path"]).is_file() and Path(entry["ref_no_path"]).is_file()
    ref_yes = entry["ref_yes_path"] if both else None
    ref_no = entry["ref_no_path"] if both else None
    if CHECKER == "aman":
        return aman_check.check(
            frame_path,
            entry["question"],
            ref_yes=ref_yes,
            ref_no=ref_no,
        )
    return verdict.check(
        frame_path,
        entry["question"],
        mode=CHECK_MODE,
        ref_yes=ref_yes,
        ref_no=ref_no,
        looks=verdict.read_looks(Path(entry["ref_yes_path"]).parent),
        safety=entry.get("safety") is True,
    )


def guide_check(frame_path, question_text):
    """The guide's check function. Guide steps carry no reference
    looks, so the check is the frame and the step's question alone."""
    if CHECKER == "aman":
        return aman_check.check(frame_path, question_text)
    return verdict.check(frame_path, question_text, mode=CHECK_MODE)


# The refusal (see checkloop.REFUSAL_TEMPLATES): a safety question's
# cannot-tell is spoken ("I can't see the knobs from here") once a
# plain check says the thing itself is in view. TRUSS_REFUSALS=0 keeps
# every cannot-tell silent, as before.
REFUSALS = os.environ.get("TRUSS_REFUSALS", "1").strip() != "0"
if not REFUSALS:
    print("Refusals are off (TRUSS_REFUSALS=0); every cannot-tell stays silent.")

# The evidence outline (see sam_client.py): with TRUSS_EVIDENCE_OUTLINE=1,
# each firing also gets a copy of its evidence photo with the object
# outlined by SAM on the DGX, written beside it in the background.
OUTLINER = None
if sam_client.outline_enabled():
    OUTLINER = sam_client.EvidenceOutliner()
    print(f"Evidence photos will be outlined by SAM at {sam_client.config_from_env().url}.")

# The check loop (see checkloop.py). Spoken answers are returned to the
# app in the /capture response; speaking them is the app's side.
LOOP = CheckLoop(
    STORE,
    production_check,
    Path(__file__).parent / "log",
    now_fn=lambda: CLOCK(),
    checks=CHECKS,
    outline_fn=OUTLINER,
    presence_fn=guide_check if REFUSALS else None,
)

# Plant-time compile (see plant_compile.py). Once a question has both
# reference looks, one model call writes prompts.txt and looks.txt into
# its folder; a question started standing on fewer looks gets
# prompts.txt alone, from its words, so SAM can outline what it fires
# on. Off the request thread, so planting answers at once.
COMPILER = Compiler(lambda folder: compile_folder(folder, lock=STORE.lock))


def compile_if_needed(entry):
    """Queue a compile for a question that is checked (standing or
    answered) and is missing its compiled files."""
    if entry and entry["state"] not in ("retired", "planting"):
        folder = STORE.root / entry["slug"]
        if needs_compile(folder):
            COMPILER.submit(folder)


def compile_waiting():
    """Queue a compile for every checked question missing its compiled
    files: planted while the model was away, before compiles existed,
    or before questions without both looks got their SAM phrases. Run
    once at startup."""
    for entry in STORE.list():
        compile_if_needed(entry)

# When the phone app was last heard from, for GET /status and the
# pre-flight (preflight.py). The app polls GET /alerts about every 30
# seconds while it runs, so a recent poll means the phone can reach
# this laptop; a recent capture means a photo came all the way from
# the glasses. In memory only: a restart forgets both until the next.
APP_SEEN = {"poll": None, "capture": None}

# The server answers each request on its own thread, so a quick read such as
# GET /questions is not stuck behind a capture that takes 40 s checking every
# due question. Requests that change state still run one at a time under this
# lock, exactly as before.
WRITE_LOCK = threading.Lock()


def _seen(kind, address):
    APP_SEEN[kind] = {"at": datetime.now().astimezone(), "from": address}


def status():
    """What GET /status reports: the server's clock (with its UTC
    offset, so the night the clocks change is visible), how checks
    will be made, the live-test settings in force, and when the phone
    was last heard from."""
    now = datetime.now().astimezone()

    def seen(kind):
        item = APP_SEEN[kind]
        if item is None:
            return None
        return {
            "at": item["at"].isoformat(timespec="seconds"),
            "seconds_ago": int((now - item["at"]).total_seconds()),
            "from": item["from"],
        }

    where = endpoint.describe_from_env()
    model, base_url, config_error = where["model"], where["base_url"], where["config_error"]
    return {
        "now": now.isoformat(timespec="seconds"),
        "zone": now.tzname(),
        "checker": CHECKER,
        "model": model,
        "base_url": base_url,
        "config_error": config_error,
        "fallback_model": where["fallback_model"],
        "fallback_base_url": where["fallback_base_url"],
        "meta_resting": where["meta_resting"],
        "check_mode": CHECK_MODE,
        "refusals": REFUSALS,
        "evidence_outline": OUTLINER is not None,
        "sam_url": sam_client.config_from_env().url,
        "plant_minutes": os.environ.get("TRUSS_PLANT_MINUTES", "").strip() or None,
        "bedtime_after": BEDTIME_AFTER,
        "last_poll": seen("poll"),
        "last_capture": seen("capture"),
    }


# The over-the-shoulder guide (see guide.py). One session at a time, in
# memory; while a guide is active, captures go to it instead of the loop.
GUIDE = {"session": None}

def set_checker(value):
    """Switch who answers the checks: "endpoint" (Muse Spark, with its
    fallback) or "aman". Read at every check, so it takes effect on
    the next one."""
    global CHECKER
    CHECKER = "aman" if value == "aman" else "endpoint"


def settings_view():
    out = settings.view()
    out["checker"] = CHECKER
    return out


def test_meta():
    """(ok, text) for the Settings tab's Muse Spark test: one short
    words-only call to Muse Spark alone, never its fallback."""
    try:
        cfg = endpoint.meta_config_from_env()
    except endpoint.EndpointError as e:
        return False, str(e)
    if cfg is None:
        return False, "No Meta Model API key is saved."
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    try:
        reply = endpoint.ask_json(
            "You are a connection test. Answer in one word.",
            [endpoint.text_part("What color is a clear daytime sky?")],
            schema, "connection_test", config=cfg, max_tokens=1024,
        )
    except endpoint.EndpointError as e:
        return False, f"{cfg.model} did not answer: {e}"
    return True, f"{reply['_model']} answered."


def test_aman():
    """(ok, text) for the Settings tab's Aman test: sign in and list
    a few chats. Makes no check and starts no turn."""
    if not (os.environ.get("AMAN_TOKEN") or "").strip():
        return False, "No Aman sign-in token is saved."
    if not (os.environ.get("AMAN_CHECK_SESSION_ID") or "").strip():
        return False, "No Aman check chat is saved."
    try:
        base, token, _session, _timeout = aman_check._config()
        status_code, _text = aman_check._request(
            "GET", f"{base}/api/v1/sessions?limit=5", token, 10
        )
    except aman_check._NoCheck:
        return False, "Aman did not answer at that address."
    if status_code == 200:
        return True, f"Aman at {base} took the sign-in token."
    if status_code in (401, 403):
        return False, "Aman refused the sign-in token. It may have expired."
    return False, f"Aman answered with an error (status {status_code})."


# The glasses' latest view, kept in memory only (never on disk, gone on
# restart), so the Desk can take a question's yes or no look through
# the glasses. Only the newest frame is held.
LATEST = {"bytes": None, "at": None}
LATEST_LOCK = threading.Lock()
# A view older than this is not offered as a look.
LATEST_MAX_AGE_S = 120


def keep_latest(image_data):
    with LATEST_LOCK:
        LATEST["bytes"] = bytes(image_data)
        LATEST["at"] = time.monotonic()


def latest_view():
    """(bytes, seconds_ago), or (None, None) when there is no fresh view."""
    with LATEST_LOCK:
        data, at = LATEST["bytes"], LATEST["at"]
    if data is None:
        return None, None
    age = time.monotonic() - at
    if age > LATEST_MAX_AGE_S:
        return None, None
    return data, age


# The Desk's start and stop for the phone's watching (see phone_watch.py).
WATCH = WatchDesk()

class TrussHandler(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        """Override to customize logging format."""
        # The phone checks in every few seconds; only a failed check-in
        # is worth a line.
        if urlparse(self.path).path == "/watch/phone" and len(args) > 1 and str(args[1]) == "200":
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {format % args}")
    
    def send_json(self, obj, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        """Handle GET requests. Reads run alongside a capture (the stores
        lock their own files); only the guide's status waits its turn,
        since a capture can be changing the guide session."""
        path = urlparse(self.path).path
        if path == "/guide/status":
            with WRITE_LOCK:
                self.handle_guide_status()
            return
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            message = "Truss server is running.\n"
            self.wfile.write(message.encode())
            print(f"GET / -> 200 OK")
        elif path == "/desk":
            self.handle_desk()
        elif path == "/questions":
            self.handle_questions()
        elif path == "/alerts":
            _seen("poll", self.client_address[0])
            self.handle_alerts()
        elif path == "/guide/status":
            self.handle_guide_status()
        elif path == "/answers":
            self.handle_answers()
        elif path == "/evidence":
            self.handle_evidence()
        elif path == "/status":
            self.send_json(status())
        elif path == "/watch":
            self.send_json(WATCH.view())
        elif path == "/settings":
            self.send_json(settings_view())
        elif path == "/latest":
            self.handle_latest()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        """Handle POST requests. Speech runs on its own; every other POST
        changes state and runs one at a time, as it did when the whole
        server was single threaded."""
        path = urlparse(self.path).path
        if path == "/tts":
            self.handle_tts()
            return
        # The watch requests change nothing the checks read, and the
        # phone checks in while its own capture is being checked, so
        # they never wait behind the write lock.
        if path == "/watch":
            self.handle_watch_ask()
            return
        if path == "/watch/phone":
            self.handle_watch_phone()
            return
        # Settings carry keys: saved and tested off the write lock (a
        # test can take seconds), and only from the Desk's own page.
        if path == "/settings":
            self.handle_settings_save()
            return
        if path == "/settings/test":
            self.handle_settings_test()
            return
        with WRITE_LOCK:
            self._do_post(path)

    def _do_post(self, path):
        if path == "/capture":
            self.handle_capture()
        elif path == "/tts":
            self.handle_tts()
        elif path == "/plant/question":
            self.handle_plant_question()
        elif path == "/plant/bedtime":
            self.handle_plant_bedtime()
        elif path == "/alerts/ack":
            self.handle_alerts_ack()
        elif path == "/questions/retire":
            self.handle_questions_retire()
        elif path == "/guide/start":
            self.handle_guide_start()
        elif path == "/guide/skip":
            self.handle_guide_skip()
        elif path == "/guide/stop":
            self.handle_guide_stop()
        elif path == "/plant/ref":
            self.handle_plant_ref()
        elif path == "/plant/finish":
            self.handle_plant_finish()
        else:
            self.send_error(404, "Not Found")

    def handle_latest(self):
        """The glasses' latest view as a JPEG, for taking a look from
        the Desk. 404 when no view came in the last two minutes. The
        X-Seconds-Ago header says how old it is."""
        data, age = latest_view()
        if data is None:
            self.send_error(404, "No view from the glasses in the last two minutes. Start watching on the phone, then try again.")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Seconds-Ago", str(int(age)))
        self.end_headers()
        self.wfile.write(data)

    def handle_desk(self):
        """The Mac page for planting questions, laying out her day,
        reading answers, and sending a saved photo as a capture. One
        static file; everything it does goes through the endpoints
        below, so it changes nothing about how checks are made."""
        try:
            page = DESK_PAGE.read_bytes()
        except OSError:
            self.send_error(404, "The desk page is missing from truss-server.")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)
        print("GET /desk -> 200 OK")

    def handle_questions(self):
        """List the standing questions and their states."""
        today = CLOCK().date().isoformat()
        pending = {a["slug"] for a in ALERTS.pending() if a.get("date") == today}
        entries = []
        for entry in STORE.list():
            item = {
                "slug": entry["slug"],
                "question": entry["question"],
                "kind": entry["kind"],
                "safety": entry["safety"],
                "state": entry["state"],
                "pattern": entry["pattern"],
                "has_ref_yes": entry["has_ref_yes"],
                "has_ref_no": entry["has_ref_no"],
                "answered_at": entry["answered_at"],
            }
            if entry["kind"] == "done":
                try:
                    status = STORE.done_status(entry["slug"], now=CLOCK())
                except Exception:
                    status = {"witness_time": None, "redo_spoken": False}
                item["witness_time"] = status["witness_time"]
                item["redo_spoken"] = status["redo_spoken"]
                item["miss_pending"] = entry["slug"] in pending
            entries.append(item)
        self.send_json({"questions": entries})
        print(f"GET /questions -> {len(entries)} questions")

    def handle_answers(self):
        """The answers logged on one day, newest first: GET
        /answers?day=YYYY-MM-DD, today when no day is given. Read-only;
        each answer names its evidence photo for GET /evidence."""
        query = parse_qs(urlparse(self.path).query)
        day = (query.get("day") or [CLOCK().date().isoformat()])[0]
        try:
            items = history.answers(LOG_DIR, day)
        except ValueError as e:
            self.send_error(400, str(e))
            return
        self.send_json({"day": day, "answers": items})
        print(f"GET /answers -> {day}: {len(items)} answers")

    def handle_evidence(self):
        """One kept photo from a day's log folder: GET
        /evidence?day=YYYY-MM-DD&name=<file>.jpg. Only a plain photo
        name inside that day's folder is ever served."""
        query = parse_qs(urlparse(self.path).query)
        day = (query.get("day") or [""])[0]
        name = (query.get("name") or [""])[0]
        path = history.photo_path(LOG_DIR, day, name)
        if path is None:
            self.send_error(404, "No such photo.")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        print(f"GET /evidence -> {day}/{name} ({len(data)} bytes)")

    def handle_questions_retire(self):
        """Stop watching a question: POST JSON {"slug": ...}. Its folder,
        looks and history stay on disk; it is only never checked again."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(content_length).decode())
        except Exception:
            self.send_error(400, "Body must be JSON with a question slug")
            return
        slug = data.get("slug") if isinstance(data, dict) else None
        # Slugs are the store's own: plain letters, digits and dashes.
        # Anything else could name a folder outside the store.
        if (
            not isinstance(slug, str)
            or not re.fullmatch(r"[a-z0-9-]+", slug)
            or STORE.get(slug) is None
        ):
            self.send_error(404, "No question with that slug.")
            return
        STORE.retire(slug)
        self.send_json({"slug": slug, "state": "retired"})
        print(f"POST /questions/retire -> {slug}")

    def handle_alerts(self):
        """The missed-window alerts still waiting to be spoken. The
        sweep runs first, so a window that closed a moment ago is
        already here on this poll."""
        run_sweep()
        alerts = [
            {"id": a["id"], "line": a["line"]} for a in ALERTS.pending()
        ]
        self.send_json({"alerts": alerts})
        print(f"GET /alerts -> {len(alerts)} pending")

    def _json_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(content_length).decode() or "{}")

    def handle_watch_ask(self):
        """The Desk asks the phone to start or stop watching: POST
        JSON {"on": true|false}. The phone acts on it at its next
        check-in, within a few seconds while its app is open."""
        try:
            data = self._json_body()
            on = data.get("on") if isinstance(data, dict) else None
            view = WATCH.ask(on)
        except (ValueError, UnicodeDecodeError):
            self.send_error(400, 'Body must be JSON like {"on": true}.')
            return
        self.send_json(view)
        print(f"POST /watch -> ask the phone to {'start' if on else 'stop'} watching")

    def _from_desk(self):
        """True for a JSON request from the Desk's own page. A page on
        another site cannot send JSON here without asking first (the
        server never says yes), and a browser names its page's origin,
        which must be this server. Anything else is refused, so no web
        page can change or test the keys."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.send_error(415, "Settings take a JSON body.")
            return False
        # The Host must be an address, not a name: a site whose name
        # was pointed at this Mac would otherwise pass as same-origin.
        host = urlparse(f"//{self.headers.get('Host') or ''}").hostname or ""
        if host != "localhost" and not re.fullmatch(r"[0-9.]+|[0-9a-f:]+", host):
            self.send_error(403, "Open the Desk by the Mac's address to change settings.")
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            o = urlparse(origin)
            if o.netloc.lower() != (self.headers.get("Host") or "").lower():
                self.send_error(403, "Settings can only be changed from the Desk itself.")
                return False
        return True

    def handle_settings_save(self):
        """Save settings from the Desk: POST JSON with any of meta_key,
        aman_base, aman_token, aman_session, checker, clear. Keys are
        never echoed back or printed."""
        if not self._from_desk():
            return
        try:
            data = self._json_body()
        except (ValueError, UnicodeDecodeError):
            self.send_error(400, "Body must be a JSON object.")
            return
        try:
            settings.save(data)
        except settings.SettingsError as e:
            self.send_error(400, str(e))
            return
        if isinstance(data, dict) and data.get("checker") in settings.CHECKERS:
            set_checker(data["checker"])
        names = sorted(k for k in (data if isinstance(data, dict) else {}) if k != "clear")
        print(f"POST /settings -> saved {', '.join(names) or 'nothing'}")
        self.send_json(settings_view())

    def handle_settings_test(self):
        """Try a connection: POST JSON {"what": "meta"} asks Muse Spark
        one words-only question (no photo); {"what": "aman"} signs in
        to Aman and lists its chats. Returns {"ok", "text", "seconds"};
        the text never carries a key."""
        if not self._from_desk():
            return
        try:
            data = self._json_body()
            what = data.get("what") if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            what = None
        if what not in ("meta", "aman"):
            self.send_error(400, 'Body must be JSON like {"what": "meta"}.')
            return
        started = time.monotonic()
        ok, text = test_meta() if what == "meta" else test_aman()
        seconds = round(time.monotonic() - started, 1)
        print(f"POST /settings/test {what} -> {'ok' if ok else 'failed'} ({seconds} s)")
        self.send_json({"ok": ok, "text": text, "seconds": seconds})

    def handle_watch_phone(self):
        """The phone checks in with how its watching stands and gets
        the Desk's latest request back."""
        try:
            data = self._json_body()
            reply = WATCH.report(data, self.client_address[0])
        except (ValueError, UnicodeDecodeError):
            self.send_error(400, "Body must be a JSON object.")
            return
        _seen("poll", self.client_address[0])
        self.send_json(reply)

    def handle_alerts_ack(self):
        """Confirm one alert was spoken: POST JSON {"id": ...}. That
        confirmation is the only thing that ever clears an alert. An
        id that names no pending alert is refused, and nothing
        changes."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
        except Exception:
            self.send_error(400, "Body must be JSON with an alert id")
            return
        alert_id = data.get("id") if isinstance(data, dict) else None
        if not ALERTS.ack(alert_id):
            self.send_error(404, "No pending alert with that id.")
            return
        self.send_json({"acked": alert_id})
        print(f"POST /alerts/ack -> {alert_id}")

    def handle_plant_question(self):
        """Start planting a question: POST JSON {"text": ..., "pattern": ...}.

        An optional "kind" says whether it is a state question or a
        done question; without one it is a state question. An optional
        "safety" (true or false) marks a question where a wrong word
        does harm (left out, a done question is safety and a state
        question is not), and an optional "look_at" names the thing to look at
        in a missed-window alert, like "the {weekday} PM compartment".
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            text = data.get("text", "").strip()
            if not text:
                self.send_error(400, "No question text provided")
                return
            slug = STORE.create(
                text,
                data.get("pattern") or PLANT_PATTERN,
                kind=data.get("kind") or "state",
                safety=data.get("safety"),
                look_at=data.get("look_at"),
            )
            self.send_json({
                "slug": slug,
                "kind": STORE.get(slug)["kind"],
                "safety": STORE.get(slug)["safety"],
                "state": "planting",
                "next": "Show me what yes looks like.",
            })
            print(f"POST /plant/question -> {slug}: {text[:50]}")
        except ValueError as e:
            self.send_error(400, str(e))
        except Exception as e:
            print(f"POST /plant/question -> ERROR: {e}")
            self.send_error(
                500,
                "Something went wrong planting that question. "
                "The error is in the server log.",
            )

    def handle_plant_bedtime(self):
        """Plant the bedtime round: POST JSON, optional "after" (HH:MM)
        and "gap_minutes". The three questions are planted in walk
        order; their reference looks are then shown one question at a
        time through /plant/ref, as for any planting."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        opts = {}
        if length:
            try:
                opts = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "Body must be JSON")
                return
            if not isinstance(opts, dict):
                self.send_error(400, "Body must be a JSON object")
                return
        try:
            planted = plant_round(
                STORE,
                after=opts.get("after", BEDTIME_AFTER),
                gap_minutes=opts.get("gap_minutes", 1),
            )
        except ValueError as e:
            self.send_error(400, str(e))
            return
        except Exception as e:
            print(f"POST /plant/bedtime -> ERROR: {e}")
            self.send_error(
                500,
                "Something went wrong planting the round. "
                "The error is in the server log.",
            )
            return
        slugs = planted["slugs"]
        questions = [
            (STORE.get(slug) or {"question": slug})["question"] for slug in slugs
        ]
        self.send_json(
            {"slugs": slugs, "questions": questions, "notes": planted["notes"]}
        )
        print(f"POST /plant/bedtime -> {slugs}")

    def _guide_body(self):
        """The JSON body of a guide call, or None after a 400 was sent."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "Body must be JSON")
            return None
        if not isinstance(body, dict):
            self.send_error(400, "Body must be a JSON object")
            return None
        return body

    def handle_guide_start(self):
        """Start a guide: POST JSON {"title": ..., "steps": [...]}. Each
        step is a question a glance can settle. The reply carries the
        first step's prompt as a line to speak."""
        body = self._guide_body()
        if body is None:
            return
        session = GUIDE["session"]
        if session is not None and session.state == "active":
            self.send_error(400, "A guide is already running. Stop it first.")
            return
        steps = body.get("steps")
        if not isinstance(steps, list) or not all(
            isinstance(s, str) for s in steps
        ):
            self.send_error(400, "Steps must be a list of step questions.")
            return
        try:
            session = Guide(
                steps,
                title=str(body.get("title") or "Guided job"),
                log_dir=Path(__file__).parent / "log",
            )
        except ValueError as e:
            self.send_error(400, str(e))
            return
        GUIDE["session"] = session
        self.send_json(
            {
                "speak": session.start(),
                "step": 1,
                "of": len(session.steps),
                "title": session.title,
            }
        )
        print(f"POST /guide/start -> {session.title}: {len(session.steps)} steps")

    def handle_guide_skip(self):
        """Skip the current step, at the wearer's word."""
        session = GUIDE["session"]
        if session is None or session.state != "active":
            self.send_error(400, "No guide is running.")
            return
        speak = session.skip()
        self.send_json(
            {"speak": speak, "state": session.state, "step": session.index + 1}
        )
        print(f"POST /guide/skip -> state {session.state}")

    def handle_guide_stop(self):
        """Stop the guide at once. Nothing is kept."""
        session = GUIDE["session"]
        if session is None or session.state != "active":
            self.send_error(400, "No guide is running.")
            return
        speak = session.stop()
        self.send_json({"speak": speak, "state": session.state})
        print("POST /guide/stop -> stopped")

    def handle_guide_status(self):
        """Where the guide stands: step, state, and the current step."""
        session = GUIDE["session"]
        if session is None:
            self.send_json({"active": False, "state": "none"})
            return
        self.send_json(
            {
                "active": session.state == "active",
                "state": session.state,
                "title": session.title,
                "step": session.index + 1 if session.state == "active" else None,
                "of": len(session.steps),
                "current": (
                    session.steps[session.index]
                    if session.state == "active"
                    else None
                ),
            }
        )

    def handle_plant_ref(self):
        """Save a reference look: POST multipart image, ?slug=...&kind=yes|no."""
        try:
            query = parse_qs(urlparse(self.path).query)
            slug = query.get("slug", [""])[0]
            kind = query.get("kind", [""])[0]
            if not slug or kind not in ("yes", "no"):
                self.send_error(400, "Need slug and kind=yes or kind=no")
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                self.send_error(400, "Expected multipart/form-data")
                return
            boundary = content_type.split("boundary=")[-1].encode()
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            image_data = self.extract_multipart_field(body, boundary, b"image")
            if not image_data:
                self.send_error(400, "No image field found")
                return
            state = STORE.save_ref(slug, kind, image_data)
            entry = STORE.get(slug)
            # Both looks exist: compile them, and again whenever a
            # look is re-shot, standing or answered. The re-shot look
            # has already cleared the old compiled files, so a question
            # standing on its yes look alone is compiled again too.
            compile_if_needed(entry)
            if state == "standing":
                name = entry["question"] if entry else slug
                next_step = f"Standing question planted: {name}"
            elif kind == "yes":
                next_step = "Now show me what no looks like."
            else:
                next_step = "Now show me what yes looks like."
            self.send_json({"slug": slug, "state": state, "next": next_step})
            print(f"POST /plant/ref -> {slug} {kind} ({len(image_data)} bytes), state {state}")
        except KeyError as e:
            self.send_error(404, str(e))
        except Exception as e:
            print(f"POST /plant/ref -> ERROR: {e}")
            self.send_error(500, str(e))
    
    def handle_plant_finish(self):
        """Start a planting question standing without the looks it is
        still missing: POST JSON {"slug": ...}. For a question the
        words decide, like a number on a display ("Is my heart rate
        above 160?"), where staging a no look is not possible."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(content_length).decode())
            slug = data.get("slug", "") if isinstance(data, dict) else ""
        except Exception:
            self.send_error(400, "Body must be JSON with a slug")
            return
        if not isinstance(slug, str) or not slug:
            self.send_error(400, "Body must be JSON with a slug")
            return
        try:
            state = STORE.start_standing(slug)
        except SafetyLooksNeeded as e:
            self.send_error(409, str(e))
            return
        except KeyError as e:
            self.send_error(404, str(e))
            return
        except Exception as e:
            print(f"POST /plant/finish -> ERROR: {e}")
            self.send_error(500, str(e))
            return
        entry = STORE.get(slug)
        # Standing on fewer looks: its SAM phrases come from its words.
        compile_if_needed(entry)
        name = entry["question"] if entry else slug
        looks = (
            "both looks" if entry and entry["has_ref_yes"] and entry["has_ref_no"]
            else "the yes look" if entry and entry["has_ref_yes"]
            else "no looks"
        )
        self.send_json({
            "slug": slug,
            "state": state,
            "next": f"Standing question planted: {name}",
        })
        print(f"POST /plant/finish -> {slug} with {looks}, state {state}")

    def handle_capture(self):
        """Handle photo upload."""
        # A capture starting means the wearer is about; it is also a
        # deterministic moment to raise any missed-window alerts,
        # without waiting for the background sweep.
        run_sweep()
        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                self.send_error(400, "Expected multipart/form-data")
                return
            
            # Parse boundary
            boundary = content_type.split("boundary=")[-1].encode()
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            
            # Extract image data
            image_data = self.extract_multipart_field(body, boundary, b"image")
            if not image_data:
                self.send_error(400, "No image field found")
                return
            
            # Save with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"capture_{timestamp}.jpg"
            filepath = CAPTURES_DIR / filename
            
            with open(filepath, "wb") as f:
                f.write(image_data)
            
            file_size = len(image_data)
            _seen("capture", self.client_address[0])
            keep_latest(image_data)

            response = {"bytes": file_size}
            session = GUIDE["session"]
            if session is not None and session.state == "active":
                # A guide is running: the wearer's attention is on the
                # current step, so the frame goes to the guide and the
                # standing questions wait. A guide failure must never
                # break the capture itself.
                try:
                    guide_result = session.process_frame(filepath, guide_check)
                except Exception as e:
                    print(f"POST /capture -> guide failed, frame kept: {e}")
                    guide_result = {"managed": False, "speak": [], "errors": 1}
                response["guide"] = {
                    "state": session.state,
                    "step": session.index + 1 if session.state == "active" else None,
                    "of": len(session.steps),
                }
                if guide_result["managed"]:
                    response["speak"] = guide_result["speak"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                print(f"POST /capture -> saved {filename} ({file_size} bytes)")
                print(
                    f"POST /capture -> guide: state={session.state} "
                    f"speak={len(guide_result['speak'])} errors={guide_result['errors']}"
                )
                return

            # Offer the frame to the check loop. When questions are
            # standing, the loop may move the frame into the answer log
            # as evidence or delete it; when none are standing, the
            # frame stays exactly as day 1 left it. A loop failure must
            # never break the capture itself.
            try:
                loop_result = LOOP.process_frame(filepath)
            except Exception as e:
                print(f"POST /capture -> loop failed, frame kept: {e}")
                loop_result = {"managed": False, "fired": [], "speak": []}
            if loop_result["managed"]:
                response["fired"] = loop_result["fired"]
                response["speak"] = loop_result["speak"]
                if loop_result.get("refused"):
                    response["refused"] = loop_result["refused"]

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

            print(f"POST /capture -> saved {filename} ({file_size} bytes)")
            if loop_result["managed"]:
                print(
                    f"POST /capture -> loop: fired={loop_result['fired']} "
                    f"errors={loop_result['errors']}"
                )
        
        except Exception as e:
            print(f"POST /capture -> ERROR: {e}")
            self.send_error(500, str(e))
    
    def handle_tts(self):
        """Handle text-to-speech."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            
            text = data.get("text", "")
            if not text:
                self.send_error(400, "No text provided")
                return
            
            # Generate speech with macOS 'say' command
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp_aiff:
                aiff_path = tmp_aiff.name
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
                wav_path = tmp_wav.name
            
            try:
                # Generate AIFF with 'say'
                subprocess.run(
                    ["say", "-o", aiff_path, text],
                    check=True,
                    timeout=10
                )
                
                # Convert to 16-bit WAV with afconvert
                subprocess.run(
                    ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", aiff_path, wav_path],
                    check=True,
                    timeout=10
                )
                
                # Read WAV file
                with open(wav_path, "rb") as f:
                    wav_data = f.read()
                
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav_data)))
                self.end_headers()
                self.wfile.write(wav_data)
                
                print(f"POST /tts -> generated {len(wav_data)} bytes for: {text[:50]}...")
            
            finally:
                if os.path.exists(aiff_path):
                    os.unlink(aiff_path)
                if os.path.exists(wav_path):
                    os.unlink(wav_path)
        
        except Exception as e:
            print(f"POST /tts -> ERROR: {e}")
            self.send_error(500, str(e))
    
    def extract_multipart_field(self, body, boundary, field_name):
        """Extract a field from multipart/form-data."""
        parts = body.split(b"--" + boundary)
        for part in parts:
            if field_name in part:
                # Find the data after the headers
                header_end = part.find(b"\r\n\r\n")
                if header_end != -1:
                    data = part[header_end + 4:]
                    # Remove trailing \r\n if present
                    if data.endswith(b"\r\n"):
                        data = data[:-2]
                    return data
        return None

def _sweep_forever():
    """The background half of the missed-window alerts: while the
    server runs, a closed window is noticed within about half a
    minute, whether or not any photo arrives."""
    while True:
        run_sweep()
        time.sleep(30)


def main():
    host = get_wifi_address()
    port = 8792

    # Settings saved from the Desk (see settings.py) go into the
    # environment before anything is asked.
    try:
        saved_checker, applied = settings.apply_saved()
    except settings.SettingsError as e:
        saved_checker, applied = None, []
        print(e)
    if applied:
        print(f"Saved Desk settings used: {', '.join(applied)} ({settings.view()['file']}).")
    if saved_checker:
        set_checker(saved_checker)
    if CHECKER == "aman":
        print("Checks will be made by Aman.")
    else:
        _where = endpoint.describe_from_env()
        if _where["model"]:
            print(f"Checks go to {_where['model']} at {_where['base_url']}.")

    threading.Thread(target=_sweep_forever, daemon=True).start()
    compile_waiting()
    server = ThreadingHTTPServer((host, port), TrussHandler)
    print(f"\nTruss server starting on {host}:{port}")
    print(f"Truss Desk (plant questions from this Mac): http://{host}:{port}/desk")
    print(f"Captures will be saved to: {CAPTURES_DIR.absolute()}")
    print(f"Press Ctrl+C to stop\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()

if __name__ == "__main__":
    main()
