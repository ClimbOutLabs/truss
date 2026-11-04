#!/usr/bin/env python3
"""
Which voice Mannie speaks in.

Speech is made on the Mac with its own `say` command, so nothing that
is spoken leaves the laptop. Left to itself, `say` uses the system's
default voice, which on most Macs is one of the older, flatter ones.
macOS also ships natural-sounding voices, free and on-device, marked
"(Premium)" or "(Enhanced)" in `say -v ?`. This picks the best one
installed:

  1. TRUSS_VOICE, when it is set (any name `say -v ?` lists).
  2. A Premium English voice, the warmer ones first.
  3. An Enhanced English voice, in the same order.
  4. Otherwise the system default, as before.

A Premium voice is added once in System Settings > Accessibility >
Spoken Content > System voice > Manage Voices (for example English
(US) > Ava (Premium)). TRUSS_VOICE_RATE sets the words per minute;
left unset, the voice's own pace is used.

Stdlib only.
"""

import os
import re
import subprocess

# Warm, clear voices first. Any other Premium or Enhanced English
# voice still beats the default; this only orders the ones known to
# sound like a person talking.
PREFERRED = (
    "Ava", "Zoe", "Evan", "Nathan", "Joelle", "Noelle", "Jamie",
    "Serena", "Matilda", "Isha", "Samantha", "Allison", "Susan", "Tom",
)

QUALITY = ("Premium", "Enhanced")

_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<lang>[a-z]{2,3}[_-][A-Za-z0-9]+)\s+#")


def parse_voices(listing):
    """(name, language) for each voice in `say -v ?` output."""
    out = []
    for line in listing.splitlines():
        match = _LINE.match(line.strip())
        if match:
            out.append((match.group("name").strip(), match.group("lang")))
    return out


def _quality(name):
    for rank, word in enumerate(QUALITY):
        if f"({word})" in name:
            return rank
    return None


def _preference(name):
    base = name.split(" (")[0].strip()
    try:
        return PREFERRED.index(base)
    except ValueError:
        return len(PREFERRED)


def pick_voice(voices, wanted=None):
    """The voice name to pass to `say -v`, or None for the system
    default. A wanted voice (TRUSS_VOICE) is used when it is listed;
    a name that is not installed falls back to the best one that is,
    so a typo never silences the glasses."""
    names = [name for name, _ in voices]
    if wanted:
        wanted = wanted.strip()
        for name in names:
            if name.lower() == wanted.lower():
                return name
    english = [
        name for name, lang in voices
        if lang.lower().startswith("en") and _quality(name) is not None
    ]
    if not english:
        return None
    english.sort(key=lambda n: (_quality(n), _preference(n), n))
    return english[0]


def _rate():
    try:
        rate = int(os.environ.get("TRUSS_VOICE_RATE", "").strip())
    except ValueError:
        return None
    return rate if 80 <= rate <= 400 else None


_CHOSEN = []


def chosen_voice():
    """The voice this server speaks in, read once from `say -v ?` and
    kept. None means the system default."""
    if not _CHOSEN:
        try:
            listing = subprocess.run(
                ["say", "-v", "?"], capture_output=True, text=True, timeout=10
            ).stdout
        except Exception:
            listing = ""
        _CHOSEN.append(pick_voice(parse_voices(listing), os.environ.get("TRUSS_VOICE")))
    return _CHOSEN[0]


def say_command(text, out_path):
    """The `say` command that writes text, spoken in the chosen voice,
    to out_path."""
    cmd = ["say"]
    voice = chosen_voice()
    if voice:
        cmd += ["-v", voice]
    rate = _rate()
    if rate:
        cmd += ["-r", str(rate)]
    return cmd + ["-o", out_path, text]


def describe():
    """One line for the server's start-up banner."""
    voice = chosen_voice()
    if voice:
        return f"Voice: {voice} (set TRUSS_VOICE to change)"
    return (
        "Voice: system default. For a natural voice, add one in System Settings > "
        "Accessibility > Spoken Content > System voice > Manage Voices "
        "(e.g. Ava (Premium)), then restart."
    )
