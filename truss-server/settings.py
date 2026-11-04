#!/usr/bin/env python3
"""
Settings saved from the Desk's Settings tab.

The Desk can save the Meta Model API key (Muse Spark), the connection
to Aman (its address, sign-in token and check chat), and which of the
two answers the checks. They are kept in one JSON file on this Mac,
~/.truss/settings.json unless TRUSS_SETTINGS names another, in a
folder only the owner can open (0700) and a file only the owner can
read (0600). The file is never inside the repo.

A saved value is put into this process's environment under the same
name the rest of the server already reads (TRUSS_META_API_KEY,
AMAN_BASE, AMAN_TOKEN, AMAN_CHECK_SESSION_ID), so nothing else changes
how it is set up. A value saved here wins over one exported in the
shell; a value left empty here leaves the shell's value alone.

Secrets (the Meta key and the Aman token) never leave this module
again: view() says only whether each is set and where it came from,
nothing here prints or logs a value, and no error text carries one.

When the Aman address changes, a token saved for the old address is
dropped unless a new one is given in the same save, so a token is
never sent to an address it was not typed for.

Standard library only.
"""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# Field name in the file and the Desk -> environment variable.
ENV = {
    "meta_key": "TRUSS_META_API_KEY",
    "aman_base": "AMAN_BASE",
    "aman_token": "AMAN_TOKEN",
    "aman_session": "AMAN_CHECK_SESSION_ID",
}
SECRETS = ("meta_key", "aman_token")
CHECKERS = ("endpoint", "aman")
DEFAULT_AMAN_BASE = "http://127.0.0.1:8791"
MAX_LEN = 4096


class SettingsError(ValueError):
    """A save that was refused; the text is plain and safe to show."""


def path():
    raw = (os.environ.get("TRUSS_SETTINGS") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".truss" / "settings.json"


# Where each field's value came from: "saved" (this file), "shell"
# (exported before the server started), or None.
_source = {name: None for name in ENV}
_saved = {}


def _shown(p):
    """The path with the home folder as ~, for messages."""
    return str(p).replace(str(Path.home()), "~", 1)


def _read_file():
    p = path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        raise SettingsError(f"The settings file {_shown(p)} could not be read.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise SettingsError(f"The settings file {_shown(p)} is not valid JSON, so it was not used.")
    if not isinstance(data, dict):
        raise SettingsError(f"The settings file {_shown(p)} is not a JSON object, so it was not used.")
    # Tighten a file someone loosened by hand.
    try:
        if p.stat().st_mode & 0o077:
            os.chmod(p, 0o600)
    except OSError:
        pass
    out = {}
    for name in ENV:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()
    if data.get("checker") in CHECKERS:
        out["checker"] = data["checker"]
    return out


def _write_file(data):
    p = path()
    folder = p.parent
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=str(folder))
        try:
            if hasattr(os, "fchmod"):  # not on Windows before Python 3.13
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(p, 0o600)
    except OSError:
        raise SettingsError(f"The settings could not be saved to {_shown(p)}.")


def apply_saved():
    """Read the file and put its values into the environment. Called
    once when the server starts (never on import, so tests never read
    the owner's file). Returns the saved checker, or None, and the
    names of the fields applied."""
    global _saved
    for name, env in ENV.items():
        if (os.environ.get(env) or "").strip():
            _source[name] = "shell"
    _saved = _read_file()
    applied = []
    for name, env in ENV.items():
        if name in _saved:
            os.environ[env] = _saved[name]
            _source[name] = "saved"
            applied.append(name)
    return _saved.get("checker"), applied


def _check_base(value):
    u = urlparse(value)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SettingsError("The Aman address must start with http:// or https:// and name a host.")
    if u.username or u.password:
        raise SettingsError("The Aman address must not carry a user name or password.")
    if u.query or u.fragment:
        raise SettingsError("The Aman address must not carry a query or a fragment.")
    return value.rstrip("/")


def _origin(value):
    u = urlparse(value or DEFAULT_AMAN_BASE)
    port = u.port or (443 if u.scheme == "https" else 80)
    return (u.scheme, (u.hostname or "").lower(), port)


def save(changes):
    """Save the fields named in changes and apply them now.

    changes holds any of meta_key, aman_base, aman_token, aman_session
    (a string; an empty string leaves a secret as it is and clears an
    address or chat id), "checker" ("endpoint" or "aman"), and
    "clear": a list of field names to forget. Returns view()."""
    global _saved
    if not isinstance(changes, dict):
        raise SettingsError("Settings must be a JSON object.")
    current = _read_file()
    new = dict(current)
    clear = changes.get("clear") or []
    if not isinstance(clear, list) or any(c not in ENV for c in clear):
        raise SettingsError("Only these settings can be cleared: " + ", ".join(ENV) + ".")

    for name in ENV:
        if name not in changes:
            continue
        value = changes[name]
        if not isinstance(value, str) or len(value) > MAX_LEN:
            raise SettingsError("Each setting must be text.")
        value = value.strip()
        if any(ch in value for ch in "\r\n\0"):
            raise SettingsError("A setting cannot hold line breaks.")
        if name in SECRETS and not value:
            continue  # an empty secret box keeps what is saved
        if name == "aman_base" and value:
            value = _check_base(value)
        if value:
            new[name] = value
        else:
            new.pop(name, None)

    if "checker" in changes:
        if changes["checker"] not in CHECKERS:
            raise SettingsError("Checks are answered by Muse Spark or by Aman.")
        new["checker"] = changes["checker"]

    for name in clear:
        new.pop(name, None)

    # A token typed for one Aman never goes to another.
    old_base = os.environ.get("AMAN_BASE")
    if "aman_base" in new:
        new_base = new["aman_base"]
    elif _source["aman_base"] == "shell" and "aman_base" not in clear:
        new_base = old_base
    else:
        new_base = None
    if (_origin(new_base) != _origin(old_base)
            and not (changes.get("aman_token") or "").strip()):
        new.pop("aman_token", None)
        if _source["aman_token"] is not None:
            os.environ.pop("AMAN_TOKEN", None)
            _source["aman_token"] = None

    _write_file(new)
    _saved = new
    for name, env in ENV.items():
        if name in new:
            os.environ[env] = new[name]
            _source[name] = "saved"
        elif _source[name] == "saved" or name in clear:
            # Taken out here: forget it in this process too.
            os.environ.pop(env, None)
            _source[name] = None
    return view()


def saved_checker():
    return _saved.get("checker")


def view():
    """What GET /settings shows. Never a secret's value."""
    out = {"file": _shown(path())}
    for name, env in ENV.items():
        value = (os.environ.get(env) or "").strip()
        if name in SECRETS:
            out[name] = {"set": bool(value), "from": _source[name] if value else None}
        else:
            out[name] = {"value": value, "from": _source[name] if value else None}
    if not out["aman_base"]["value"]:
        out["aman_base"]["value"] = DEFAULT_AMAN_BASE
        out["aman_base"]["from"] = "default"
    return out


def reset_for_tests():
    global _saved
    _saved = {}
    for name in ENV:
        _source[name] = None
