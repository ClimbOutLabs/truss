#!/usr/bin/env python3
"""
Tests for the over-the-shoulder guide.

Run from the truss-server folder:  python3 test_guide.py
Stdlib only. Exits non-zero on the first failure.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# A Meta key exported in this shell must never send a test's request
# to the real API.
import os  # noqa: E402
for _name in ("TRUSS_META_API_KEY", "TRUSS_META_BASE_URL", "TRUSS_META_MODEL"):
    os.environ.pop(_name, None)

from guide import Guide  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" + b"\xff\xd9"

FAUCET = [
    "Is the water shut off under the sink?",
    "Is the wrench on the cartridge nut?",
    "Is the new cartridge pushed all the way in?",
    "Is the handle back on and tight?",
]


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def verdict(v):
    return {"verdict": v, "reason": "", "latency_ms": 1, "usage": {}}


class ScriptedChecker:
    """One scripted verdict per step, consumed in order."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def __call__(self, frame_path, question_text):
        self.calls.append(question_text)
        script = self.scripts.get(question_text, [])
        if script:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return verdict("cannot_tell")


def rig(steps, scripts, log_dir=None):
    tmp = tempfile.mkdtemp()
    guide = Guide(steps, title="Faucet cartridge", log_dir=log_dir or Path(tmp) / "log")
    captures = Path(tmp) / "captures"
    captures.mkdir()
    return guide, ScriptedChecker(scripts), captures


def frame(captures, name):
    path = captures / name
    path.write_bytes(JPEG)
    return path


def test_start_and_the_walk():
    guide, checker, captures = rig(
        FAUCET,
        {
            FAUCET[0]: [verdict("no"), verdict("yes")],
            FAUCET[1]: [verdict("cannot_tell"), verdict("cannot_tell"), verdict("yes")],
            FAUCET[2]: [verdict("yes")],
            FAUCET[3]: [verdict("yes")],
        },
    )
    check(
        "starting speaks the first step",
        guide.start() == ["Step 1 of 4: Is the water shut off under the sink?"],
    )

    first = guide.process_frame(frame(captures, "1.jpg"), checker)
    check("a no is silent and the step stands", first["speak"] == [] and guide.index == 0)

    second = guide.process_frame(frame(captures, "2.jpg"), checker)
    check(
        "a yes advances and speaks the next step",
        second["speak"] == ["Step 2 of 4: Is the wrench on the cartridge nut?"],
    )

    guide.process_frame(frame(captures, "3.jpg"), checker)
    restate = guide.process_frame(frame(captures, "4.jpg"), checker)
    check(
        "the second cannot-tell in a row restates the step",
        restate["speak"] == ["Let's have another look. Is the wrench on the cartridge nut?"],
    )
    fifth = guide.process_frame(frame(captures, "5.jpg"), checker)
    check("after the restate, a yes advances", fifth["speak"] == [
        "Step 3 of 4: Is the new cartridge pushed all the way in?"
    ])

    guide.process_frame(frame(captures, "6.jpg"), checker)
    done = guide.process_frame(frame(captures, "7.jpg"), checker)
    check("the last step speaks a summary", done["speak"] == ["That's every step. Nicely done."])
    check("guide is done", guide.state == "done")
    check(
        "every step was checked in order",
        checker.calls
        == [FAUCET[0], FAUCET[0], FAUCET[1], FAUCET[1], FAUCET[1], FAUCET[2], FAUCET[3]],
    )
    check("no frames are kept", list(captures.iterdir()) == [])

    after = guide.process_frame(frame(captures, "8.jpg"), checker)
    check("a finished guide does not manage frames", after["managed"] is False)


def test_skip_and_stop():
    guide, checker, captures = rig(
        FAUCET,
        {
            FAUCET[0]: [verdict("yes")],
            FAUCET[1]: [],
            FAUCET[2]: [verdict("yes")],
            FAUCET[3]: [verdict("yes")],
        },
    )
    guide.process_frame(frame(captures, "1.jpg"), checker)
    skipped = guide.skip()
    check(
        "skip speaks the next step",
        skipped == ["Step 3 of 4: Is the new cartridge pushed all the way in?"],
    )
    guide.process_frame(frame(captures, "2.jpg"), checker)
    stopped = guide.stop()
    check("stop speaks once", stopped == ["Okay, I've stopped the guide."])
    check("guide is stopped", guide.state == "stopped")
    check("no log line on stop", not list((Path(captures).parent / "log").rglob("answers.txt")))
    after = guide.process_frame(frame(captures, "3.jpg"), checker)
    check("a stopped guide does not manage frames", after["managed"] is False)


def test_skip_on_last_step_finishes_with_a_note():
    guide, checker, captures = rig([FAUCET[0]], {})
    check("one step guide starts with step one", guide.start() == [
        "Step 1 of 1: Is the water shut off under the sink?"
    ])
    done = guide.skip()
    check(
        "skipping the only step finishes with the skipped note",
        done == ["That's the last step. 1 of the 1 was skipped.", "We skipped: the water shut off under the sink."],
    )
    check("guide is done", guide.state == "done")


def test_checker_failure_is_silent_and_keeps_the_step():
    guide, checker, captures = rig(
        FAUCET,
        {FAUCET[0]: [RuntimeError("model call failed"), verdict("yes")]},
    )
    failed = guide.process_frame(frame(captures, "1.jpg"), checker)
    check("checker failure counted", failed["errors"] == 1)
    check("checker failure is silent", failed["speak"] == [])
    check("checker failure keeps the step", guide.index == 0)
    second = guide.process_frame(frame(captures, "2.jpg"), checker)
    check("the step still advances on its yes", second["speak"] == [
        "Step 2 of 4: Is the wrench on the cartridge nut?"
    ])


def test_done_writes_one_log_line():
    tmp = tempfile.mkdtemp()
    guide, checker, captures = rig(
        FAUCET,
        {step: [verdict("yes")] for step in FAUCET},
        log_dir=Path(tmp) / "log",
    )
    for i, _step in enumerate(FAUCET):
        guide.process_frame(frame(captures, f"{i}.jpg"), checker)
    logs = list((Path(tmp) / "log").rglob("answers.txt"))
    check("one answer log file on done", len(logs) == 1)
    text = logs[0].read_text()
    check(
        "log line names the job and every step",
        "guided: Faucet cartridge | done" in text
        and all(step in text for step in FAUCET),
    )
    check("log line names the model, unknown when the checker does not say",
          text.rstrip().endswith("| model: unknown"))


def test_done_line_names_the_checking_model():
    tmp = tempfile.mkdtemp()
    named = dict(verdict("yes"), model="muse-glimmer-30b")
    guide, checker, captures = rig(
        FAUCET, {step: [named] for step in FAUCET}, log_dir=Path(tmp) / "log"
    )
    for i, _step in enumerate(FAUCET):
        guide.process_frame(frame(captures, f"{i}.jpg"), checker)
    text = next((Path(tmp) / "log").rglob("answers.txt")).read_text()
    check("done line names the model once", text.count("muse-glimmer-30b") == 1
          and text.rstrip().endswith("| model: muse-glimmer-30b"))


def test_empty_steps_refused():
    try:
        Guide([])
        check("empty steps refused", False)
    except ValueError:
        check("empty steps refused", True)


if __name__ == "__main__":
    test_start_and_the_walk()
    test_skip_and_stop()
    test_skip_on_last_step_finishes_with_a_note()
    test_checker_failure_is_silent_and_keeps_the_step()
    test_done_writes_one_log_line()
    test_done_line_names_the_checking_model()
    test_empty_steps_refused()
    print("ALL PASS")
