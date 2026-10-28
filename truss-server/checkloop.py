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


def _reason_text(result):
    """The checker's one-sentence reason, as text. A model can return a
    reason that is not a string at all; anything that is not text counts
    as no reason, rather than breaking the answer it came with."""
    reason = result.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


def speak_text(entry, result):
    """The spoken line for a firing: the question, the answer, and the
    checker's one-sentence observation. Only yes verdicts are spoken."""
    question = entry["question"].strip()
    if not question.endswith("?"):
        question += "?"
    reason = _reason_text(result)
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
        try:
            standing = [e for e in self.store.list() if e["state"] == "standing"]
            due = self.store.due(now=self.now_fn()) if standing else []
        except Exception:
            # A broken question file must never break capturing. Fall
            # back to day 1 behavior: keep the frame, manage nothing.
            return {"managed": False, "fired": [], "speak": [], "errors": 1}
        if not standing:
            return {"managed": False, "fired": [], "speak": [], "errors": 0}

        now = self.now_fn()
        fired = []
        errors = 0
        for entry in due:
            try:
                result = self.check_fn(frame_path, entry)
                if not isinstance(result, dict):
                    raise TypeError("check returned no verdict")
                verdict = result.get("verdict")
            except Exception:
                # A failed check is treated like a cannot_tell: silent,
                # never spoken, and the question is still marked checked
                # so a broken checker cannot hot-loop on every frame.
                result, verdict = {}, None
                errors += 1
            try:
                self.store.mark_checked(entry["slug"], when=now)
            except Exception:
                # One question's bookkeeping failure skips that
                # question only; it must not discard another question's
                # yes from the same frame.
                errors += 1
                continue
            if verdict == "yes":
                fired.append((entry, result))

        speak = []
        evidence = None
        if fired:
            try:
                evidence = self._save_evidence(frame_path, fired[0][0], now)
            except Exception:
                # The frame stays in captures/ as the record, and the
                # answers are still spoken and logged against it.
                errors += 1
                evidence = frame_path
            for entry, result in fired:
                try:
                    self._append_log(entry, result, evidence, now)
                except Exception:
                    errors += 1
                try:
                    self.store.mark_answered(entry["slug"], when=now)
                except Exception:
                    # Unmarked means unspoken: the question stays
                    # standing and can fire on a later frame, but this
                    # answer is never spoken twice.
                    errors += 1
                    continue
                text = speak_text(entry, result)
                speak.append(text)
                if self.speak_fn is not None:
                    try:
                        self.speak_fn(text, entry, evidence)
                    except Exception:
                        errors += 1
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
        stem = f"{now.strftime('%H%M%S')}_{first_entry['slug']}"
        dest = day / f"{stem}.jpg"
        n = 2
        while dest.exists():
            dest = day / f"{stem}_{n}.jpg"
            n += 1
        shutil.move(str(frame_path), str(dest))
        return dest

    def _append_log(self, entry, result, evidence, now):
        day = self._day_dir(now)
        reason = _reason_text(result)
        line = (
            f"{now.strftime('%H:%M:%S')} | {entry['question']} | yes | "
            f"{reason} | evidence: {evidence.name}"
        )
        # Every answer names the model that made the call, when the
        # checker says which one it was.
        model = result.get("model")
        if isinstance(model, str) and model.strip():
            line += f" | model: {model.strip()}"
        line += "\n"
        with open(day / "answers.txt", "a") as f:
            f.write(line)
