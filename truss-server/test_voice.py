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
        ("Did I say I\u2019m home?", "Did you say you're home?"),
        ("Is the US mail here?", "Is the US mail here?"),
    ]:
        check(f"said back: {mine!r} -> {yours!r}", to_you(mine) == yours)


def test_inline_question():
    from alerts import _question_text
    check("an auxiliary first word is lower-cased mid-sentence",
          _question_text({"question": "Did I take my pills"}) == "did you take your pills?")
    check("a name keeps its capital",
          _question_text({"question": "Mia's bowl full?"}) == "Mia's bowl full?")


def test_other_platforms():
    import lan
    import os
    import subprocess
    real_which, real_run = voice.shutil.which, voice.subprocess.run
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        out = kw.get("env", {}).get("TRUSS_TTS_OUT") or cmd[cmd.index("-w") + 1]
        with open(out, "wb") as f:
            f.write(b"RIFFfake")
        return subprocess.CompletedProcess(cmd, 0)

    try:
        voice.shutil.which = lambda name: None
        check("no speech engine on Windows: no audio, no crash",
              voice.speak_wav("hi", platform="win32") is None)
        check("no speech engine on Linux: no audio, no crash",
              voice.speak_wav("hi", platform="linux") is None)
        check("banner says lines are printed when Windows has no speech",
              "printed" in voice.describe(platform="win32"))

        voice.shutil.which = lambda name: "C:/ps.exe" if name == "powershell" else None
        voice.subprocess.run = fake_run
        text = "It's 9:00 \"now\"; $(rm -rf) `x`"
        wav = voice.speak_wav(text, platform="win32")
        cmd, kw = calls[-1]
        check("Windows speech returns the WAV PowerShell wrote", wav == b"RIFFfake")
        check("the spoken text travels in the environment, not the command line",
              kw["env"]["TRUSS_TTS_TEXT"] == text and all(text not in part for part in cmd))
        check("Windows asks for 24 kHz 16-bit mono", "24000" in cmd[-1] and "Mono" in cmd[-1])
        check("the temporary WAV is removed", not os.path.exists(kw["env"]["TRUSS_TTS_OUT"]))

        voice.shutil.which = lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else None
        wav = voice.speak_wav("-dash first", platform="linux")
        cmd, _ = calls[-1]
        check("Linux speech uses espeak-ng, text after --",
              wav == b"RIFFfake" and cmd[0].endswith("espeak-ng") and cmd[-2:] == ["--", "-dash first"])
    finally:
        voice.shutil.which, voice.subprocess.run = real_which, real_run

    saved = os.environ.pop("TRUSS_HOST", None)
    try:
        os.environ["TRUSS_HOST"] = "10.1.2.3"
        check("TRUSS_HOST wins on every platform",
              lan.lan_address("win32") == "10.1.2.3" and lan.lan_address("darwin") == "10.1.2.3")
        del os.environ["TRUSS_HOST"]
        real_mac = lan._mac_en0
        lan._mac_en0 = lambda: "192.0.2.9"
        check("a Mac still uses en0", lan.lan_address("darwin") == "192.0.2.9")
        lan._mac_en0 = real_mac
        got = lan.lan_address("win32")
        check("elsewhere the routed address is an IPv4 address or None",
              got is None or (got.count(".") == 3 and not got.startswith("127.")))
    finally:
        os.environ.pop("TRUSS_HOST", None)
        if saved is not None:
            os.environ["TRUSS_HOST"] = saved


if __name__ == "__main__":
    test_other_platforms()
    test_inline_question()
    test_parse()
    test_pick()
    test_say_command()
    test_to_you()
    print("all voice tests passed")
