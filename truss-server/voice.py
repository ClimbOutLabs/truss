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

On Windows, speech is made by Windows' own speech engine (System.Speech,
through PowerShell), also on the computer; TRUSS_VOICE names one of its
voices (for example "Microsoft Zira Desktop"), else the default voice
speaks. On Linux, espeak-ng (or espeak) is used when installed. With
none of these, speak_wav returns None and the server prints the line.

Stdlib only.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

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


# Windows speech: the text and paths come in through the environment,
# never through the command line, so no quoting of what is said can
# break the script. 24 kHz, 16-bit, mono, like the Mac's WAV.
_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($env:TRUSS_TTS_VOICE) { try { $s.SelectVoice($env:TRUSS_TTS_VOICE) } catch { } }
  $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
  $s.SetOutputToWaveFile($env:TRUSS_TTS_OUT, $fmt)
  $s.Speak($env:TRUSS_TTS_TEXT)
} finally { $s.Dispose() }
"""


def _windows_shell():
    return shutil.which("powershell") or shutil.which("pwsh")


def _linux_espeak():
    return shutil.which("espeak-ng") or shutil.which("espeak")


def _read_and_remove(*paths):
    try:
        with open(paths[-1], "rb") as f:
            data = f.read()
    finally:
        for path in paths:
            if os.path.exists(path):
                os.unlink(path)
    return data


def _mac_wav(text):
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp_aiff:
        aiff_path = tmp_aiff.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
        wav_path = tmp_wav.name
    try:
        # Generate AIFF with 'say', in the most natural voice installed.
        # Should that voice fail, the line is still spoken in the
        # system default.
        try:
            subprocess.run(say_command(text, aiff_path), check=True, timeout=20)
        except Exception:
            subprocess.run(["say", "-o", aiff_path, text], check=True, timeout=20)
        # Convert to 16-bit WAV with afconvert. 24 kHz keeps the
        # voice's warmth; 16 kHz made it sound like a phone line.
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@24000", aiff_path, wav_path],
            check=True,
            timeout=10,
        )
    except BaseException:
        _read_and_remove(aiff_path, wav_path)
        raise
    return _read_and_remove(aiff_path, wav_path)


def _windows_wav(text, shell):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
        wav_path = tmp_wav.name
    env = dict(os.environ)
    env.update({
        "TRUSS_TTS_TEXT": text,
        "TRUSS_TTS_OUT": wav_path,
        "TRUSS_TTS_VOICE": os.environ.get("TRUSS_VOICE", "").strip(),
    })
    try:
        subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT],
            check=True, timeout=30, env=env, capture_output=True,
        )
    except BaseException:
        _read_and_remove(wav_path)
        raise
    return _read_and_remove(wav_path)


def _espeak_wav(text, espeak):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
        wav_path = tmp_wav.name
    cmd = [espeak, "-w", wav_path]
    wanted = os.environ.get("TRUSS_VOICE", "").strip()
    if wanted:
        cmd += ["-v", wanted]
    rate = _rate()
    if rate:
        cmd += ["-s", str(rate)]
    try:
        subprocess.run(cmd + ["--", text], check=True, timeout=20, capture_output=True)
    except BaseException:
        _read_and_remove(wav_path)
        raise
    return _read_and_remove(wav_path)


def speak_wav(text, platform=None):
    """The WAV bytes of text spoken on this computer, or None when this
    computer has no speech Truss knows how to use. Raises when the
    speech engine is there but fails."""
    platform = platform or sys.platform
    if platform == "darwin":
        return _mac_wav(text)
    if platform == "win32":
        shell = _windows_shell()
        return _windows_wav(text, shell) if shell else None
    espeak = _linux_espeak()
    return _espeak_wav(text, espeak) if espeak else None


def describe(platform=None):
    """One line for the server's start-up banner."""
    platform = platform or sys.platform
    if platform == "win32":
        if not _windows_shell():
            return "Voice: none (PowerShell not found), spoken lines are printed instead."
        wanted = os.environ.get("TRUSS_VOICE", "").strip()
        return (
            f"Voice: Windows speech, {wanted or 'default voice'} (set TRUSS_VOICE to a "
            "voice from Settings > Time & language > Speech to change)"
        )
    if platform != "darwin":
        espeak = _linux_espeak()
        if not espeak:
            return "Voice: none (install espeak-ng), spoken lines are printed instead."
        return f"Voice: {os.path.basename(espeak)} (set TRUSS_VOICE to an espeak voice to change)"
    voice = chosen_voice()
    if voice:
        return f"Voice: {voice} (set TRUSS_VOICE to change)"
    return (
        "Voice: system default. For a natural voice, add one in System Settings > "
        "Accessibility > Spoken Content > System voice > Manage Voices "
        "(e.g. Ava (Premium)), then restart."
    )
