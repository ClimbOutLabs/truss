#!/usr/bin/env python3
"""
Tests for the spoken voice (voice.py) and for questions said back to
the wearer (wording.to_you).

Run from the truss-server folder:  python3 test_voice.py
Stdlib only. Exits non-zero on the first failure.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import voice  # noqa: E402
from wording import to_you  # noqa: E402

LISTING = """\
Albert              en_US    # Hello! My name is Albert.
Bad News            en_US    # Hello! My name is Bad News.
Samantha            en_US    # Hello! My name is Samantha.
Samantha (Enhanced) en_US    # Hello! My name is Samantha.
Amelie (Premium)    fr_CA    # Bonjour! Je m'appelle Amelie.
Tom (Premium)       en_US    # Hello! My name is Tom.
Zoe (Premium)       en_US    # Hello! My name is Zoe.
"""


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def test_parse():
    voices = voice.parse_voices(LISTING)
    check("every listed voice is read, with names that hold spaces",
          ("Bad News", "en_US") in voices and ("Zoe (Premium)", "en_US") in voices
          and len(voices) == 7)


def test_pick():
    voices = voice.parse_voices(LISTING)
    check("a Premium English voice wins, the warmer one first",
          voice.pick_voice(voices) == "Zoe (Premium)")
    check("a Premium voice in another language is never picked",
          voice.pick_voice([("Amelie (Premium)", "fr_CA")]) is None)
    check("Enhanced is used when there is no Premium",
          voice.pick_voice([("Albert", "en_US"), ("Samantha (Enhanced)", "en_US")])
          == "Samantha (Enhanced)")
    check("only plain voices: the system default",
          voice.pick_voice([("Albert", "en_US"), ("Samantha", "en_US")]) is None)
    check("TRUSS_VOICE wins when installed, in any case",
          voice.pick_voice(voices, "tom (premium)") == "Tom (Premium)")
    check("a TRUSS_VOICE that is not installed falls back to the best one",
          voice.pick_voice(voices, "Nobody") == "Zoe (Premium)")
    check("no listing at all (not a Mac): the system default",
          voice.pick_voice(voice.parse_voices("")) is None)


def test_say_command():
    voice._CHOSEN[:] = ["Zoe (Premium)"]
    try:
        check("the command names the chosen voice",
              voice.say_command("Hi.", "/tmp/x.aiff")
              == ["say", "-v", "Zoe (Premium)", "-o", "/tmp/x.aiff", "Hi."])
        voice._CHOSEN[:] = [None]
        check("with no natural voice the command is as before",
              voice.say_command("Hi.", "/tmp/x.aiff") == ["say", "-o", "/tmp/x.aiff", "Hi."])
    finally:
        voice._CHOSEN.clear()


def test_to_you():
    for mine, yours in [
        ("Did I take my night pills?", "Did you take your night pills?"),
        ("Did i take my test pills 3?", "Did you take your test pills 3?"),
        ("Is my hr below 160", "Is your hr below 160"),
        ("Am I wearing my watch?", "Are you wearing your watch?"),
        ("Was I at the gym?", "Were you at the gym?"),
        ("Did we feed the dog?", "Did you feed the dog?"),
        ("Is the stove off?", "Is the stove off?"),
        ("Is the garage door shut?", "Is the garage door shut?"),
    ]:
        check(f"said back: {mine!r} -> {yours!r}", to_you(mine) == yours)


if __name__ == "__main__":
    test_parse()
    test_pick()
    test_say_command()
    test_to_you()
    print("all voice tests passed")
