#!/usr/bin/env python3
"""
The over-the-shoulder guide.

The wearer states a job as steps, each step a question a glance can
settle: "Is the water shut off under the sink?", "Is the wrench on the
cartridge nut?" The guide speaks one step at a time and checks each
photo against the current step only.

- Yes: the step is confirmed, and the next step is spoken.
- No: silence. The wearer is still working on the step.
- Cannot-tell twice in a row on the same step: the step is spoken
  again, plainer, and the wearer can skip it or stop.
- The last step confirmed: a short spoken summary, and one line in
  the day's answer log.
- Stop, at any moment: the guide ends at once and nothing is kept.

The guide keeps no photos: a frame is deleted as soon as it has been
checked, whichever way the check went. Checks are limited to what a
glance can settle (presence, absence, count, position, order), never
safety verdicts; the wearer states the steps, so the wearer owns what
the job is.

One guide runs at a time, in the server's memory. The check function
is injected, as in the check loop, so the whole walk can be exercised
with scripted verdicts and no model key.
"""

from datetime import datetime
from pathlib import Path


def _question(text):
    text = text.strip()
    if not text.endswith("?"):
        text += "?"
    return text


def _fragment(question):
    """A step question as a spoken list item: 'Is the water shut off?'
    becomes 'the water shut off'."""
    text = question.rstrip("?").strip()
    for lead in ("Is ", "Are ", "Does ", "Did ", "Has ", "Have "):
        if text.startswith(lead):
            return text[len(lead):]
    return text


class Guide:
    def __init__(self, steps, title="Guided job", log_dir=None, now_fn=None):
        steps = [_question(s) for s in steps if s and s.strip()]
        if not steps:
            raise ValueError("A guide needs at least one step.")
        self.steps = steps
        self.title = title
        self.log_dir = Path(log_dir) if log_dir else None
        self.now_fn = now_fn or datetime.now
        self.index = 0
        self.streak = 0
        self.state = "active"
        self.outcomes = []

    # -- speaking -----------------------------------------------------------

    def prompt(self):
        return f"Step {self.index + 1} of {len(self.steps)}: {self.steps[self.index]}"

    def start(self):
        return [self.prompt()]

    def _summary(self):
        skipped = [
            _fragment(q)
            for q, outcome in zip(self.steps, self.outcomes)
            if outcome == "skipped"
        ]
        if not skipped:
            return ["All steps done."]
        return [
            f"All steps done: {len(self.steps) - len(skipped)} confirmed, "
            f"{len(skipped)} skipped.",
            "Skipped: " + "; ".join(skipped) + ".",
        ]

    # -- the walk -------------------------------------------------------------

    def process_frame(self, frame_path, check_fn):
        """Check one photo against the current step. The frame is always
        deleted afterwards: the guide keeps no photos."""
        frame_path = Path(frame_path)
        if self.state != "active":
            return {"managed": False, "speak": [], "state": self.state, "errors": 0}
        speak = []
        errors = 0
        try:
            try:
                result = check_fn(frame_path, self.steps[self.index])
                if not isinstance(result, dict):
                    raise TypeError("check returned no verdict")
                verdict = result.get("verdict")
            except Exception:
                result, verdict = {}, None
                errors += 1
            if verdict == "yes":
                speak = self._advance("yes")
            elif verdict == "cannot_tell":
                self.streak += 1
                if self.streak >= 2:
                    self.streak = 0
                    speak = [f"Have another look. {self.steps[self.index]}"]
            # A no, and a failed check, are silent: the step stands.
        finally:
            frame_path.unlink(missing_ok=True)
        return {
            "managed": True,
            "speak": speak,
            "state": self.state,
            "step": self.index + 1 if self.state == "active" else None,
            "errors": errors,
        }

    def skip(self):
        """Move past the current step without a yes, at the wearer's word."""
        if self.state != "active":
            return []
        return self._advance("skipped")

    def stop(self):
        """End at once. Nothing is kept: no log line, no summary."""
        if self.state != "active":
            return []
        self.state = "stopped"
        return ["Stopped."]

    # -- inside -----------------------------------------------------------------

    def _advance(self, outcome):
        self.outcomes.append(outcome)
        self.index += 1
        self.streak = 0
        if self.index >= len(self.steps):
            self.state = "done"
            self._log_done()
            return self._summary()
        return [self.prompt()]

    def _log_done(self):
        if self.log_dir is None:
            return
        now = self.now_fn()
        day = self.log_dir / now.strftime("%Y-%m-%d")
        try:
            day.mkdir(parents=True, exist_ok=True)
            line = (
                f"{now.strftime('%H:%M:%S')} | guided: {self.title} | done | "
                + "; ".join(self.steps)
                + "\n"
            )
            with open(day / "answers.txt", "a") as f:
                f.write(line)
        except Exception:
            # The summary has already been spoken; a failed log line
            # never unmakes a finished job.
            pass
