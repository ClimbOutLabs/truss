#!/usr/bin/env python3
"""
Pre-flight: everything a recording needs, checked in one go.

Run it on the laptop, from the truss-server folder, with the server
already running and the phone app open:

    python3 preflight.py
    python3 preflight.py --wait-capture 120          # also prove the glasses
    python3 preflight.py --photo refusal.jpg         # rehearse the refusal

It checks, and says how to fix what fails:

    the clock     the laptop's time and zone (the clocks go back 2 AM Nov 1)
    the server    answering, checking with a Meta model on the endpoint
                  (never Aman, never the contributor tier), and no
                  live-test settings left on
    the phone     the app polled the laptop in the last 90 seconds
    the glasses   a photo came from them recently, or, with
                  --wait-capture, one arrives while it waits
    the DGX       llama-server healthy and serving a model; SAM healthy
                  when the evidence outline is on
    the shots     the questions each shot needs are planted, have both
                  looks and their compiled words, and are due at the
                  times the shots are planned for (see --witness-at,
                  --reach-at, --refusal-at, --bedtime-at)

With --photo, one real check is made on that photo against the stove
question, the way the refusal shot will be made: it passes when the
answer is cannot_tell and names the knobs, and prints the line Mannie
would say. Take the photo from where the shot will be, with the knobs
out of view.

Nothing is planted, changed or deleted. Exit status 0 means no FAIL;
WARN lines are worth reading but do not stop a recording.

Stdlib only, like the server.
"""

import argparse
import json
import os
import re
import sys
import time as time_module
import urllib.error
import urllib.request
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import checkloop  # noqa: E402
import endpoint  # noqa: E402
import lan  # noqa: E402
import verdict  # noqa: E402
from bedtime import ROUND, SAFETY as ROUND_SAFETY  # noqa: E402
from questions import QuestionStore  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# The shoot plan for Tue Nov 3 from the brief: the witnessed act about
# 1:00 PM, the stopped reach about 1:40 PM, the bedtime round after
# dark. The refusal is shot on the bedtime round's first pass, from
# the kitchen doorway, unless --refusal-at says otherwise.
DEFAULT_WITNESS_AT = "13:00"
DEFAULT_REACH_AT = "13:40"
DEFAULT_BEDTIME_AT = "20:30"

POLL_FRESH_S = 90
CAPTURE_FRESH_S = 600


def _hm(text):
    try:
        parsed = time.fromisoformat(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a time of day like 13:40: {text!r}")
    return parsed


def _clock(t):
    return datetime.combine(datetime(2000, 1, 1), t).strftime("%I:%M %p").lstrip("0")


def default_server():
    """The address server.py binds: its LAN address (see lan.py) on
    port 8792, or this machine when there is none."""
    return f"http://{lan.lan_address() or '127.0.0.1'}:8792"


def get_json(url, timeout=5):
    """(status, parsed body or None, error text or None). Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        status = e.code
    except Exception as e:
        return None, None, str(getattr(e, "reason", e))
    try:
        return status, json.loads(body.decode("utf-8")), None
    except Exception:
        return status, None, None


# -- the clock -----------------------------------------------------------


def check_clock(now=None):
    now = (now or datetime.now()).astimezone()
    stamp = now.strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " ")
    return [(PASS, "clock", f"{stamp} (UTC{now.strftime('%z')})")]


# -- the server and the phone ----------------------------------------------


def check_server(status):
    """Lines for the server's own report (GET /status)."""
    out = []
    if status is None:
        return [(FAIL, "server", "No answer. Start it: cd truss-server && python3 server.py")]
    out.append((PASS, "server", f"answering, clock {status.get('now')}"))
    checker = status.get("checker")
    model = status.get("model") or ""
    if checker != "endpoint":
        out.append((FAIL, "checker", f"{checker!r}: Aman is never in the demo loop. Unset TRUSS_CHECKER and restart."))
    elif status.get("config_error"):
        out.append((FAIL, "checker", status["config_error"]))
    elif "contributor" in model.lower():
        out.append((FAIL, "model", f"{model}: the contributor tier is never used."))
    elif not re.search(r"glimmer|muse", model, re.IGNORECASE):
        out.append((WARN, "model", f"{model!r} does not look like a Meta model. Every answer line names it."))
    else:
        out.append((PASS, "model", f"{model} at {status.get('base_url')}, {status.get('check_mode')} mode"))
    if status.get("plant_minutes"):
        out.append((WARN, "TRUSS_PLANT_MINUTES", f"is {status['plant_minutes']}, a live-test setting. Unset it for the demo."))
    if status.get("bedtime_after") not in (None, "20:00"):
        out.append((WARN, "TRUSS_BEDTIME_AFTER", f"is {status['bedtime_after']}, a live-test setting. Unset it for the demo."))
    if status.get("refusals") is False:
        out.append((FAIL, "refusals", "off (TRUSS_REFUSALS=0), so the refusal shot cannot happen. Unset it."))
    return out


def check_phone(status):
    if status is None:
        return []
    poll = status.get("last_poll")
    if poll is None:
        return [(FAIL, "phone", "The app has not polled since the server started. Open Truss on the phone, on the same Wi-Fi.")]
    ago = poll.get("seconds_ago", 10**9)
    if ago > POLL_FRESH_S:
        return [(FAIL, "phone", f"Last heard {ago} s ago from {poll.get('from')}. Open Truss on the phone; it polls every 30 s.")]
    return [(PASS, "phone", f"polled {ago} s ago from {poll.get('from')}")]


def check_glasses(status, fetch_status=None, wait_s=0, say=print, sleep=time_module.sleep):
    """A photo from the glasses: recent, or, with wait_s, one taken
    while this waits. The photo goes through the check loop like any
    other, so it may answer a due question; with none due it is just
    deleted."""
    if status is None:
        return []
    before = status.get("last_capture")
    if wait_s > 0 and fetch_status is not None:
        say(f"Waiting up to {wait_s} s: take one photo now (Look now in the app, or the glasses button).")
        deadline = time_module.monotonic() + wait_s
        while time_module.monotonic() < deadline:
            sleep(2)
            current = (fetch_status() or {}).get("last_capture")
            if current is not None and (before is None or current.get("at") != before.get("at")):
                return [(PASS, "glasses", f"a photo arrived from {current.get('from')}")]
        return [(FAIL, "glasses", f"No photo in {wait_s} s. Check the glasses are registered in the app (Settings, Diagnostics) and connected.")]
    if before is None:
        return [(WARN, "glasses", "No photo since the server started. Run again with --wait-capture 120 and take one.")]
    ago = before.get("seconds_ago", 10**9)
    if ago > CAPTURE_FRESH_S:
        return [(WARN, "glasses", f"Last photo {ago // 60} min ago. Run again with --wait-capture 120 and take one.")]
    return [(PASS, "glasses", f"last photo {ago} s ago")]


# -- the DGX -------------------------------------------------------------


def _root(base_url):
    """llama-server's own address: /health lives there, not under /v1."""
    return re.sub(r"/v1/?$", "", base_url.rstrip("/"))


def check_models(status, get=get_json):
    """Lines for where checks go: Muse Spark over the Meta Model API
    when the Meta key is set (Glimmer then only as an optional
    fallback), otherwise llama-server on the DGX."""
    status = status or {}
    base_url = status.get("base_url")
    if not base_url:
        where = endpoint.describe_from_env()
        status = dict(status, **{k: where[k] for k in ("base_url", "model", "fallback_base_url", "fallback_model")})
        base_url = status.get("base_url")
    if base_url and base_url.startswith("https://"):
        out = [(PASS, "Muse Spark", f"{status.get('model')} at {base_url} answers first")]
        if status.get("meta_resting"):
            out.append((WARN, "Muse Spark", "resting after a failure; the fallback answers for now"))
        fallback = status.get("fallback_base_url")
        if not fallback:
            out.append((PASS, "fallback", "none (TRUSS_BASE_URL unset); Glimmer need not run"))
            return out
        return out + check_llama(fallback, get=get)
    return check_llama(base_url, get=get)


def check_llama(base_url, get=get_json):
    if not base_url:
        return [(FAIL, "llama-server", "No endpoint is configured.")]
    if base_url.startswith("https://"):
        return [(WARN, "llama-server", f"{base_url} is not the DGX; the demo checks on Glimmer there.")]
    root = _root(base_url)
    status, body, error = get(f"{root}/health")
    if status is None:
        return [(FAIL, "llama-server", f"No answer at {root} ({error}). On the DGX: ./dgx/bringup.sh serve")]
    if status == 503:
        return [(FAIL, "llama-server", "Still loading the model. Wait, then run this again.")]
    if status != 200:
        return [(FAIL, "llama-server", f"/health answered HTTP {status}. On the DGX: ./dgx/bringup.sh status")]
    status, body, _ = get(f"{base_url.rstrip('/')}/models")
    names = []
    if status == 200 and isinstance(body, dict):
        names = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)]
    return [(PASS, "llama-server", f"healthy at {root}" + (f", serving {', '.join(map(str, names))}" if names else ""))]


def check_sam(status, get=get_json):
    if status is None:
        return []
    url = (status.get("sam_url") or "").rstrip("/")
    on = bool(status.get("evidence_outline"))
    code, body, error = get(f"{url}/health") if url else (None, None, "no address")
    if code == 200:
        model = body.get("model") if isinstance(body, dict) else None
        return [(PASS, "SAM", f"healthy at {url}" + (f", {model}" if model else "") + ("" if on else " (outline is off)"))]
    if on:
        return [(FAIL, "SAM", f"Outline is on but SAM does not answer at {url}. On the DGX: ./dgx/sam/setup.sh serve")]
    return [(WARN, "SAM", f"not answering at {url}; the evidence outline is off, so no shot needs it.")]


# -- the questions each shot needs -------------------------------------------


def _due_at(pattern, at):
    """Whether a pattern can be due at this time of day, and from when."""
    kind = pattern.get("kind")
    if kind == "nightly":
        after = time.fromisoformat(pattern.get("after", "20:00"))
        return at >= after, f"nightly from {_clock(after)}"
    if kind == "window":
        after = time.fromisoformat(pattern["after"])
        until = time.fromisoformat(pattern["until"])
        return after <= at < until, f"window {_clock(after)} to {_clock(until)}"
    if kind == "interval":
        return True, f"every {pattern.get('minutes')} min"
    return False, "unknown pattern"


def _ready(store, entry):
    """Problems that stop a question from being checked well."""
    folder = store.root / entry["slug"]
    problems = []
    if entry["state"] != "standing":
        problems.append(f"is {entry['state']}")
    if not (entry["has_ref_yes"] and entry["has_ref_no"]):
        problems.append("is missing a reference look")
    if not (folder / "looks.txt").is_file():
        problems.append("has no looks.txt (compile it: python3 plant_compile.py " + entry["slug"] + ")")
    return problems


def _norm(text):
    return text.strip().lower().rstrip("?").strip()


def check_shots(store, witness_at, reach_at, bedtime_at, refusal_at=None, now=None):
    out = []
    entries = store.list()
    now = now or datetime.now()

    # The witnessed act and the stopped reach: one safety done question
    # whose window holds both times.
    done = [e for e in entries if e["kind"] == "done" and e["state"] != "retired"]
    if not done:
        out.append((FAIL, "pills (done)", "No done question is planted. Plant one, for example: "
                    "curl -X POST http://<laptop>:8792/plant/question -H 'Content-Type: application/json' "
                    "-d '{\"text\": \"Did I take my night pills?\", \"kind\": \"done\", "
                    "\"look_at\": \"the {weekday} PM compartment\", "
                    f"\"pattern\": {{\"kind\": \"window\", \"after\": \"{_hm_text(witness_at, -15)}\", "
                    f"\"until\": \"{_hm_text(reach_at, 30)}\", \"minutes\": 0}}}}'"))
    else:
        good, bad = [], []
        for entry in done:
            covers_w, desc = _due_at(entry["pattern"], witness_at)
            covers_r, _ = _due_at(entry["pattern"], reach_at)
            problems = _ready(store, entry)
            if not (covers_w and covers_r):
                problems.append(f"has a {desc}, which does not hold both {_clock(witness_at)} and {_clock(reach_at)}")
            if not entry["safety"]:
                problems.append("is not a safety question")
            gap = entry["pattern"].get("minutes", 15)
            if isinstance(gap, (int, float)) and gap > 1:
                problems.append(f"is checked only every {gap} min; the act takes seconds, so plant it with minutes 0")
            witness = store.witness_today(entry["slug"], now=now)
            if witness is not None:
                problems.append("already has today's witness, so it will not witness again until tomorrow")
            if problems:
                bad.append(f"{entry['question']} " + "; ".join(problems))
            else:
                good.append(f"{entry['question']}: {desc}, looks compiled")
        # One good done question is enough; any others only warn.
        out += [(PASS, "pills (done)", text) for text in good]
        out += [(WARN if good else FAIL, "pills (done)", text) for text in bad]

    # The bedtime round: its three questions, in walk order.
    for text in ROUND:
        matches = [e for e in entries if _norm(e["question"]) == _norm(text) and e["state"] != "retired"]
        if not matches:
            out.append((FAIL, "bedtime", f"{text} is not planted. Plant the round from the app (Questions, Bedtime round)."))
            continue
        entry = matches[0]
        problems = _ready(store, entry)
        ok, desc = _due_at(entry["pattern"], bedtime_at)
        if not ok:
            problems.append(f"has a {desc}, so it is not due at {_clock(bedtime_at)}")
        if text in ROUND_SAFETY and not entry["safety"]:
            problems.append("is not a safety question (retire it and plant the round again)")
        if problems:
            out.append((FAIL, "bedtime", f"{text} " + "; ".join(problems)))
        else:
            out.append((PASS, "bedtime", f"{text} {desc}"))

    # The refusal: the stove, a safety question, with the compiled
    # phrase that names the stove, due when the shot is planned.
    stove = [e for e in entries if _norm(e["question"]) == _norm(ROUND[0]) and e["state"] == "standing"]
    when = refusal_at or bedtime_at
    if not stove:
        out.append((FAIL, "refusal", "No standing stove question, so there is nothing to refuse on."))
    else:
        entry = stove[0]
        problems = []
        if not entry["safety"]:
            problems.append("the stove question is not a safety question, so its cannot-tell stays silent")
        if checkloop._thing_for(entry) is None:
            problems.append("it has no prompts.txt, so the stove cannot be confirmed in view")
        ok, desc = _due_at(entry["pattern"], when)
        if not ok:
            problems.append(f"it is a {desc}, so it is not due at {_clock(when)}")
        if problems:
            out.append((FAIL, "refusal", "; ".join(problems)))
        else:
            out.append((PASS, "refusal", f"the stove can refuse at {_clock(when)}"))
    return out


def _hm_text(t, minutes):
    total = max(0, min(23 * 60 + 59, t.hour * 60 + t.minute + minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


# -- the refusal rehearsal ------------------------------------------------------


def rehearse_refusal(store, photo, check=verdict.check):
    """One real check of a photo against the stove question, the way
    the loop makes it: its looks, safety on. Passes on a cannot_tell
    that names the knobs."""
    entries = [e for e in store.list() if _norm(e["question"]) == _norm(ROUND[0])]
    entry = entries[0] if entries else {"question": ROUND[0], "safety": True, "kind": "state"}
    looks = verdict.read_looks(store.root / entry["slug"]) if entries else None
    result = check(Path(photo), entry["question"], looks=looks, safety=True)
    line = checkloop.refusal_text(entry, result) if result.get("verdict") == "cannot_tell" else None
    detail = (
        f"{result.get('verdict')} in {result.get('latency_ms')} ms; reason: {result.get('reason')!r}; "
        f"missing: {result.get('missing')!r}"
    )
    if result.get("failure"):
        return [(FAIL, "refusal rehearsal", f"no check was made ({result['failure']}): {result.get('reason')}")]
    if result.get("verdict") != "cannot_tell":
        return [(FAIL, "refusal rehearsal", detail + ". Move so the knobs are out of view and try again.")]
    if "knob" not in (result.get("missing") or "").lower():
        return [(WARN, "refusal rehearsal", detail + f". Mannie would say: {line}")]
    return [(PASS, "refusal rehearsal", detail + f". Mannie would say: {line}")]


# -- running it ----------------------------------------------------------------


def run(args, say=print):
    lines = []
    lines += check_clock()
    status_url = args.server.rstrip("/") + "/status"

    def fetch_status():
        code, body, _ = get_json(status_url)
        return body if code == 200 and isinstance(body, dict) else None

    status = fetch_status()
    lines += check_server(status)
    lines += check_phone(status)
    lines += check_glasses(status, fetch_status, args.wait_capture, say=say)
    lines += check_models(status)
    lines += check_sam(status)
    store = QuestionStore(Path(args.store))
    lines += check_shots(store, args.witness_at, args.reach_at, args.bedtime_at, args.refusal_at)
    if args.photo:
        lines += rehearse_refusal(store, args.photo)
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check everything a Truss recording needs.")
    parser.add_argument("--server", default=None, help="the Truss server, default http://<en0>:8792")
    parser.add_argument("--store", default=str(Path(__file__).parent / "questions"),
                        help="the question store folder")
    parser.add_argument("--wait-capture", type=int, default=0, metavar="SECONDS",
                        help="wait this long for one photo from the glasses")
    parser.add_argument("--photo", help="a stove photo with the knobs out of view: rehearse the refusal")
    parser.add_argument("--witness-at", type=_hm, default=_hm(DEFAULT_WITNESS_AT))
    parser.add_argument("--reach-at", type=_hm, default=_hm(DEFAULT_REACH_AT))
    parser.add_argument("--bedtime-at", type=_hm, default=_hm(DEFAULT_BEDTIME_AT))
    parser.add_argument("--refusal-at", type=_hm, default=None,
                        help="when the refusal is shot; default is on the bedtime round")
    args = parser.parse_args(argv)
    args.server = args.server or default_server()
    lines = run(args)
    width = max(len(name) for _, name, _ in lines)
    for level, name, detail in lines:
        print(f"{level}  {name.ljust(width)}  {detail}")
    fails = sum(1 for level, _, _ in lines if level == FAIL)
    warns = sum(1 for level, _, _ in lines if level == WARN)
    print()
    if fails:
        print(f"Not ready: {fails} to fix" + (f", {warns} to read." if warns else "."))
        return 1
    print("Ready to record." + (f" {warns} warning(s) to read." if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
