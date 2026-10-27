#!/usr/bin/env python3
"""
The check loop for the Truss server.

The loop is frame-driven: the wearer's glances are the sampling. Every
photo the app sends is offered to each standing question whose pattern
says a check is due, through the injected check function, in planted
mode with the question's own reference looks.

- Verdict yes: the answer is spoken first, the frame is kept as
  evidence, and a plain-text line goes into the day's answer log.
- Verdict no, cannot_tell, or a failed check: silence. Nothing is
  spoken, and nothing about the frame is kept for that question.
- A frame no question fired on is deleted. Kept photos are only ones
  a question fired on; that deletion rule is a product feature.
- When no question is standing, frames are not managed at all and the
  capture path behaves exactly as it did on day 1.

The check function is injected so the loop can be exercised end to end
with scripted verdicts, and so the real checker (eval/checker.py) can
be wired in without the loop knowing anything about models or keys.
"""

import shutil
from datetime import datetime
from pathlib import Path


def speak_text(entry, result):
    """The spoken line for a firing: the question, the answer, and the
    checker's one-sentence observation. Only yes verdicts are spoken."""
    question = entry["question"].strip()
    if not question.endswith("?"):
        question += "?"
    reason = (result.get("reason") or "").strip()
    if reason:
        return f"{question} Yes. {reason}"
    return f"{question} Yes."


class CheckLoop:
    def __init__(self, store, check_fn, log_dir, speak_fn=None, now_fn=None):
        self.store = store
        self.check_fn = check_fn
        self.log_dir = Path(log_dir)
        self.speak_fn = speak_fn
        self.now_fn = now_fn or datetime.now

    def process_frame(self, frame_path):
        frame_path = Path(frame_path)
        standing = [e for e in self.store.list() if e["state"] == "standing"]
        if not standing:
            return {"managed": False, "fired": [], "speak": [], "errors": 0}

        now = self.now_fn()
        fired = []
        errors = 0
        for entry in self.store.due(now=now):
            try:
                result = self.check_fn(frame_path, entry)
            except Exception:
                # A failed check is treated like a cannot_tell: silent,
                # never spoken, and the question is still marked checked
                # so a broken checker cannot hot-loop on every frame.
                errors += 1
                self.store.mark_checked(entry["slug"], when=now)
                continue
            self.store.mark_checked(entry["slug"], when=now)
            if result.get("verdict") == "yes":
                fired.append((entry, result))

        speak = []
        evidence = None
        if fired:
            evidence = self._save_evidence(frame_path, fired[0][0], now)
            for entry, result in fired:
                self._append_log(entry, result, evidence, now)
                text = speak_text(entry, result)
                speak.append(text)
                if self.speak_fn is not None:
                    self.speak_fn(text, entry, evidence)
                self.store.mark_answered(entry["slug"], when=now)
        else:
            frame_path.unlink(missing_ok=True)

        return {
            "managed": True,
            "fired": [entry["slug"] for entry, _ in fired],
            "speak": speak,
            "evidence": str(evidence) if evidence else None,
            "errors": errors,
        }

    # -- evidence and the answer log --------------------------------------

    def _day_dir(self, now):
        day = self.log_dir / now.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        return day

    def _save_evidence(self, frame_path, first_entry, now):
        day = self._day_dir(now)
        name = f"{now.strftime('%H%M%S')}_{first_entry['slug']}.jpg"
        dest = day / name
        shutil.move(str(frame_path), str(dest))
        return dest

    def _append_log(self, entry, result, evidence, now):
        day = self._day_dir(now)
        reason = (result.get("reason") or "").strip()
        line = (
            f"{now.strftime('%H:%M:%S')} | {entry['question']} | yes | "
            f"{reason} | evidence: {evidence.name}\n"
        )
        with open(day / "answers.txt", "a") as f:
            f.write(line)
