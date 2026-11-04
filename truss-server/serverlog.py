#!/usr/bin/env python3
"""
The server log, and the feed of what Truss says.

Everything the server prints goes through one tidy writer: to the
terminal as before, and to log/server.log, so a test run can be
followed from a second window with

    tail -f truss-server/log/server.log

and the file can be handed over or read by a script. Each line is

    2026-11-03 05:30:45 | capture  | saved capture_20261103_053045.jpg (176142 bytes)

the date and time, a short tag saying what part of the server spoke,
and the message, split by " | " like the answer log.

The check lines are read back, too: check_history() gives one
question's earlier checks on a day, so the next check of that
question can be told what tonight's earlier checks found (see
verdict.py).

The every-few-seconds polls (the phone's check-ins, the Desk's
refreshes, an empty alert poll) are left out, since they buried the
lines that matter. Their failures are kept, and a line repeated many
times in a row is written once with a count. TRUSS_LOG_POLLS=1 keeps
the polls too.

The feed is the other half: every line Truss speaks through the
glasses is synthesized here (POST /tts), so this is where it is kept,
text and audio, for the Desk's Live tab to show and play to anyone
else in the room. It lives in memory only and holds the last few
dozen lines.
"""

import itertools
import os
import re
import sys
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

# An access line from BaseHTTPRequestHandler, as server.py prints it:
#   [2026-11-03 05:30:15] "GET /latest HTTP/1.1" 404 -
ACCESS = re.compile(r'^\[[^\]]+\] "(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+" (?P<code>\d{3})')
# The handler's error line, printed just before the access line above.
ERROR = re.compile(r"^\[[^\]]+\] code (?P<code>\d{3}), message (?P<message>.*)$")
# The handlers' own lines: "POST /capture -> saved ...".
ARROW = re.compile(r"^(?P<method>GET|POST) (?P<path>/\S*) -> (?P<message>.*)$")

# Handler lines that only report a routine poll.
POLL_LINES = (
    re.compile(r"^GET / -> 200 OK$"),
    re.compile(r"^GET /alerts -> 0 pending$"),
    re.compile(r"^GET /questions -> \d+ questions?$"),
    re.compile(r"^GET /desk -> 200 OK$"),
)

# The start of GET /latest's "no view yet" error (see server.py).
NO_VIEW = "No view from the glasses"

# A path's tag is its first part, with a few plainer names.
TAG_NAMES = {"tts": "said", "": "http"}
TAG_WIDTH = 8


def tag_for(path):
    first = path.split("?", 1)[0].strip("/").split("/", 1)[0]
    return TAG_NAMES.get(first, first)[:TAG_WIDTH] or "http"


def tidy(line, keep_polls=False):
    """(tag, message) for one printed line, or None when it is a
    routine poll that is left out."""
    text = line.rstrip()
    if not text:
        return None
    access = ACCESS.match(text)
    if access:
        code = access["code"]
        # The Desk asks for the glasses' latest view every few seconds,
        # and "no view yet" is its ordinary answer, not a failure.
        if not keep_polls and code == "404" and access["path"].split("?", 1)[0] == "/latest":
            return None
        if code.startswith(("2", "3")) and not keep_polls:
            # A success says nothing the handler's own line does not;
            # the rest are the polls.
            return None
        return ("http", f"{access['method']} {access['path']} -> {code}")
    error = ERROR.match(text)
    if error and not keep_polls and error["message"].startswith(NO_VIEW):
        return None
    if error:
        return ("error", f"{error['code']} {error['message']}")
    if not keep_polls and any(p.match(text) for p in POLL_LINES):
        return None
    arrow = ARROW.match(text)
    if arrow:
        return (tag_for(arrow["path"]), arrow["message"])
    stripped = text.strip()
    if stripped.startswith("check"):
        return ("check", stripped[len("check"):].lstrip(" :"))
    if stripped.startswith("Traceback") or text.startswith(" "):
        # A traceback's lines keep their indent, so it still reads as one.
        return ("error", text.rstrip())
    return ("server", stripped)


# Environment variables whose values are keys; never written out.
SECRET_ENV = ("TRUSS_META_API_KEY", "TRUSS_API_KEY", "MODEL_API_KEY", "AMAN_TOKEN", "TRUSS_SAM_KEY")
BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
HIDDEN = "[key hidden]"


def hide_keys(message, environ=None):
    """The message with any key the server holds blanked out."""
    env = os.environ if environ is None else environ
    for name in SECRET_ENV:
        value = (env.get(name) or "").strip()
        if len(value) >= 8 and value in message:
            message = message.replace(value, HIDDEN)
    return BEARER.sub(lambda m: m.group(1) + HIDDEN, message)


class Recent:
    """The last few hundred tidy log lines, newest last, for the Desk's
    Log tab. Each has an id counting up from 1; run changes on each
    start so a page knows its ids no longer line up."""

    def __init__(self, size=300):
        self._items = deque(maxlen=size)
        self._ids = itertools.count(1)
        self.run = uuid.uuid4().hex
        self._lock = threading.Lock()

    def add(self, at, tag, text):
        with self._lock:
            self._items.append({"id": next(self._ids), "at": at, "tag": tag, "text": text})

    def since(self, after=0, limit=300):
        with self._lock:
            fresh = [dict(i) for i in self._items if i["id"] > after]
        return fresh[-limit:]


# The lines GET /log serves; install() points the server's log at it.
RECENT = Recent()


class TidyLog:
    """A stream that takes what the server prints, tidies it line by
    line, and writes each kept line to the terminal and the log file."""

    def __init__(self, console, path=None, keep_polls=None, clock=datetime.now, recent=None):
        self.console = console
        self.recent = recent
        self.path = Path(path) if path else None
        self.keep_polls = (
            os.environ.get("TRUSS_LOG_POLLS", "").strip() == "1"
            if keep_polls is None
            else keep_polls
        )
        self.clock = clock
        self._partial = ""
        self._last = None
        self._repeats = 0
        self._lock = threading.Lock()
        self._file = None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(self.path, "a", buffering=1, encoding="utf-8")
            except OSError:
                self._file = None

    # The parts of a text stream print() and tracebacks use.
    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                self._line(line)
        return len(text)

    def flush(self):
        try:
            self.console.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self.console, "encoding", "utf-8")

    def _line(self, line):
        kept = tidy(line, keep_polls=self.keep_polls)
        if kept is None:
            return
        if kept == self._last:
            self._repeats += 1
            return
        if self._repeats:
            self._emit(self._last[0], f"(the line above, {self._repeats} more times)")
        self._last = kept
        self._repeats = 0
        self._emit(*kept)

    def _emit(self, tag, message):
        message = hide_keys(message)
        stamp = self.clock().strftime("%Y-%m-%d %H:%M:%S")
        out = f"{stamp} | {tag:<{TAG_WIDTH}} | {message}\n"
        if self.recent is not None:
            self.recent.add(stamp, tag, message)
        try:
            self.console.write(out)
            self.console.flush()
        except Exception:
            pass
        if self._file is not None:
            try:
                self._file.write(out)
            except Exception:
                pass


def install(log_dir):
    """Send the server's prints (and its tracebacks) through the tidy
    log, writing log_dir/server.log. Returns the log file's path."""
    path = Path(log_dir) / "server.log"
    out = TidyLog(sys.stdout, path, recent=RECENT)
    sys.stdout = out
    sys.stderr = out
    return path


# One tidy check line, as the loop prints it and _emit writes it: the
# day and the time, the check tag, then "slug: verdict (seconds s)
# reason". A check that failed before the model answered prints "no
# verdict" instead, and a check the endpoint never made gives a reason
# saying so; neither found anything, and neither matches below as a
# result.
_CHECK_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \| check\s*\| (.*)$"
)
_CHECK_REST = re.compile(r"^\([\d.]+ s\) ?(.*)$")

# How much of the log's tail a history read looks through. One
# evening of checks writes far less than this; the bound only keeps a
# long-lived log from being read whole on every check.
_HISTORY_TAIL_BYTES = 512 * 1024


def check_history(path, slug, day, limit=6):
    """One question's earlier checks on one day, read back from the
    server log at path: a list of (time, verdict, reason), oldest
    first, holding at most limit of the most recent. Only the check
    lines this question's own slug heads count, and only the day
    asked about, so another question's checks and an earlier day's
    never appear. A check that found nothing (no verdict, or a reason
    saying no check was made) is left out. A missing or unreadable
    log is no history. Never raises."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _HISTORY_TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace")
    except (OSError, TypeError, ValueError):
        return []
    prefix = f"{slug}: "
    found = []
    for line in text.splitlines():
        matched = _CHECK_LINE.match(line)
        if matched is None or matched.group(1) != day.isoformat():
            continue
        message = matched.group(3)
        if not message.startswith(prefix):
            continue
        verdict_word, _, rest = message[len(prefix):].partition(" ")
        if verdict_word not in ("yes", "no", "cannot_tell"):
            continue
        rest = _CHECK_REST.match(rest)
        if rest is None:
            continue
        reason = rest.group(1).strip()
        if "no check was made" in reason:
            continue
        try:
            at = datetime.fromisoformat(f"{matched.group(1)}T{matched.group(2)}")
        except ValueError:
            continue
        found.append((at, verdict_word, reason))
    if limit is None or limit < 0:
        return found
    return found[-limit:] if limit else []


class Feed:
    """The last lines Truss spoke, with their audio, newest last."""

    def __init__(self, size=40, clock=datetime.now):
        self._items = deque(maxlen=size)
        self._ids = itertools.count(1)
        # A new token each time the server starts, so a page holding an
        # id from before a restart knows its ids no longer line up.
        self.run = uuid.uuid4().hex
        self._lock = threading.Lock()
        self.clock = clock

    def add(self, text, wav=None):
        with self._lock:
            item = {
                "id": next(self._ids),
                "at": self.clock().isoformat(timespec="seconds"),
                "text": text,
                "wav": wav,
            }
            self._items.append(item)
            return item["id"]

    def since(self, after=0):
        """The lines after the given id, without their audio."""
        with self._lock:
            return [
                {"id": i["id"], "at": i["at"], "text": i["text"], "audio": i["wav"] is not None}
                for i in self._items
                if i["id"] > after
            ]

    def audio(self, item_id):
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    return item["wav"]
        return None
