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
- A done question that fires leaves a witness on its record (the
  time and the evidence photo). For the rest of that day it is not
  checked as an ordinary question again; instead, later frames are
  checked under a return prompt, and if she is back at the same
  place about to do it again, one reminder is spoken: that is
  already done, with the witnessed time. The reminder is spoken
  once, and its frame is kept as evidence like any firing.
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


def return_prompt(entry):
    """The check prompt for the redo watch: is she at the same place
    as this question again, about to do the thing again. Built only
    from the question's own words, so it is the same for every frame
    and every checker."""
    return (
        "Is the wearer at the same place as this question again, "
        f"about to do it again: {entry['question']}"
    )


def redo_text(witness):
    """The spoken redo reminder: plain words, and the witnessed time
    in a 12-hour clock, like 8:05 PM."""
    moment = datetime.fromisoformat(witness["at"])
    spoken = moment.strftime("%I:%M %p").lstrip("0")
    return f"That is already done. You did it at {spoken}."


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
            watch = self.store.redo_watch(now=self.now_fn())
        except Exception:
            # A broken question file must never break capturing. Fall
            # back to day 1 behavior: keep the frame, manage nothing.
            return {"managed": False, "fired": [], "speak": [], "errors": 1}
        if not standing and not watch:
            return {"managed": False, "fired": [], "speak": [], "errors": 0}

        # Done questions under check are claimed for the whole check,
        # from before the model call until the witness is written, so
        # the sweep cannot raise a miss for a window that closes while
        # the answer is still on its way.
        claimed = [e["slug"] for e in due if e.get("kind") == "done"]
        self.store.begin_checks(claimed)
        try:
            return self._check_frame(frame_path, due, watch)
        finally:
            self.store.end_checks(claimed)

    def _check_frame(self, frame_path, due, watch):
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

        # The redo watch: done questions holding today's witness are
        # offered the frame under a return prompt, with their own
        # reference looks. One reminder per frame at most. A no, a
        # cannot_tell, or a failed return check stays silent, exactly
        # like an ordinary check that does not fire.
        redo = None
        fired_slugs = {entry["slug"] for entry, _ in fired}
        for entry in watch:
            if entry["slug"] in fired_slugs:
                continue
            prompt_entry = dict(entry)
            prompt_entry["question"] = return_prompt(entry)
            try:
                result = self.check_fn(frame_path, prompt_entry)
                if not isinstance(result, dict):
                    raise TypeError("check returned no verdict")
                verdict = result.get("verdict")
            except Exception:
                result, verdict = {}, None
                errors += 1
            try:
                # The return check is recorded like any other check,
                # so the store's gap before the next one survives a
                # restart and no watched question is return-checked
                # on every frame.
                self.store.mark_checked(entry["slug"], when=now)
            except Exception:
                errors += 1
                continue
            if verdict == "yes":
                try:
                    witness = self.store.witness_today(entry["slug"], now=now)
                except Exception:
                    errors += 1
                    witness = None
                if witness is not None:
                    redo = (entry, witness)
                    break

        speak = []
        evidence = None
        completed_fired = []
        if fired or redo is not None:
            first = fired[0][0] if fired else redo[0]
            try:
                evidence = self._save_evidence(frame_path, first, now)
            except Exception:
                # The frame stays in captures/ as the record, and the
                # answers are still spoken and logged against it.
                errors += 1
                evidence = frame_path
            for entry, result in fired:
                try:
                    if entry.get("kind") == "done":
                        # One store operation records the answered
                        # mark and the witness together. If it fails,
                        # this firing never happened: nothing is
                        # logged or spoken for it, and the question
                        # stays eligible for a later frame.
                        self.store.mark_answered_with_witness(
                            entry["slug"], evidence.name, when=now
                        )
                    else:
                        self.store.mark_answered(entry["slug"], when=now)
                except Exception:
                    # Unmarked means unspoken: the question stays
                    # standing and can fire on a later frame, but this
                    # answer is never spoken twice.
                    errors += 1
                    continue
                try:
                    self._append_log(entry, result, evidence, now)
                except Exception:
                    errors += 1
                completed_fired.append(entry)
                text = speak_text(entry, result)
                speak.append(text)
                if self.speak_fn is not None:
                    try:
                        self.speak_fn(text, entry, evidence)
                    except Exception:
                        errors += 1
            if redo is not None:
                entry, witness = redo
                try:
                    self._append_redo_log(entry, witness, evidence, now)
                except Exception:
                    errors += 1
                try:
                    self.store.mark_redo_spoken(entry["slug"])
                except Exception:
                    # Unmarked means unspoken, as with a firing: the
                    # reminder waits for a later frame rather than
                    # risk being spoken twice.
                    errors += 1
                else:
                    text = redo_text(witness)
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
            "fired": [entry["slug"] for entry in completed_fired],
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
            f"{reason} | evidence: {evidence.name}\n"
        )
        with open(day / "answers.txt", "a") as f:
            f.write(line)

    def _append_redo_log(self, entry, witness, evidence, now):
        day = self._day_dir(now)
        line = (
            f"{now.strftime('%H:%M:%S')} | {entry['question']} | "
            f"redo reminder | {redo_text(witness)} | "
            f"evidence: {evidence.name}\n"
        )
        with open(day / "answers.txt", "a") as f:
            f.write(line)
