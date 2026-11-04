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
- A safety done question whose window closed unwitnessed is owed a
  state check (see alerts.py). Later that day, frames are checked
  under a state prompt: how the thing is right now, not what anyone
  did. The first yes or no that comes with the checker's description
  is spoken once, as that description, and its frame is kept as
  evidence. A cannot_tell, or a verdict with no description behind
  it, is silent, and the check waits for a later frame.
- Every check of a done question that comes back with a verdict is
  written to the check log (see coverage.py), so a missed-window
  alert can say how much of the window was really watched.
- A safety state question (the stove) whose check comes back
  cannot_tell, with the model naming what it lacked, is spoken as a
  refusal ("I can't see the knobs from here") when a plain presence
  check confirms the thing itself is in view; at most once per ten
  minutes per question, with the frame kept as evidence. Without a
  presence check function, every cannot_tell stays silent.
- When no question is standing, frames are not managed at all and the
  capture path behaves exactly as it did on day 1.
- With an outline function (the evidence outline, TRUSS_EVIDENCE_OUTLINE
  in server.py), a firing also asks for a copy of the evidence photo
  with the object outlined. The evidence photo itself is never
  changed, and the outline can never hold up or stop an answer.

The check function is injected so the loop can be exercised end to end
with scripted verdicts, and so the real checker (eval/checker.py) can
be wired in without the loop knowing anything about models or keys.
"""

import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from questions import look_at_for
from wording import TO_YOU, action_verbs_in, to_you


def model_name(result):
    """The model a checker says made the call, for the answer log.
    Every answer line names one; a checker that does not say is logged
    as unknown rather than left out."""
    model = result.get("model") if isinstance(result, dict) else None
    if isinstance(model, str) and model.strip():
        return " ".join(model.split())
    return "unknown"


def _reason_text(result):
    """The checker's one-sentence reason, as text. A model can return a
    reason that is not a string at all; anything that is not text counts
    as no reason, rather than breaking the answer it came with."""
    reason = result.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


def _print_check(entry, verdict, reason, started):
    """One server log line per question checked on a frame, so a live
    test shows what each question saw, not only what fired."""
    seconds = time.monotonic() - started
    try:
        print(
            f"  check {entry.get('slug')}: {verdict or 'no verdict'} "
            f"({seconds:.1f} s) {' '.join(str(reason).split())}"[:400],
            flush=True,
        )
    except Exception:
        pass


def _clock(moment):
    """A time of day as it is spoken, like 8:05 PM."""
    return moment.strftime("%I:%M %p").lstrip("0")


def _spoken_question(entry):
    """The question said back to the wearer: "Did I take my pills?"
    is spoken as "Did you take your pills?"."""
    question = to_you(entry["question"].strip())
    if not question.endswith("?"):
        question += "?"
    return question


# Every sentence the loop speaks for a firing or a redo reminder.
# A firing names the time it was seen, so the bedtime round says when
# each thing was found the way it was, and the checker's reason, so
# the wearer hears what was seen rather than a bare yes. The redo
# reminder names the witnessed time and says it was seen, because "I
# saw you" is what lets them stop fretting. Worded the way a companion
# would say it, not read out like a report.
SPOKEN_TEMPLATES = {
    "state": "{question} Yes, that's how it looked at {time}. {reason}",
    "state_bare": "{question} Yes, that's how it looked at {time}.",
    "done": "{question} Yes, I saw it happen at {time}. {reason}",
    "done_bare": "{question} Yes, I saw it happen at {time}.",
    "redo": "Just so you know, you already {act} at {time}. I saw you.",
    "redo_plain": "Just so you know, that's already done. I saw it at {time}.",
    "next_window": "The next window is at {time}.",
}

# The past tense of the verbs a done question is likely to open with.
# Any other verb takes the regular ending; a question that does not
# open "Did I ..." gets the plain reminder instead.
_PAST = {
    "take": "took", "feed": "fed", "give": "gave", "eat": "ate",
    "drink": "drank", "put": "put", "shut": "shut", "set": "set",
    "get": "got", "make": "made", "leave": "left", "do": "did",
    "go": "went", "run": "ran", "bring": "brought", "pay": "paid",
    "send": "sent", "hang": "hung", "lay": "laid", "stop": "stopped",
    "plug": "plugged", "unplug": "unplugged", "mop": "mopped",
    "water": "watered",
}

# The wearer's words are their own ("my pills"); Mannie says them back
# ("your pills"). See wording.to_you.
_TO_YOU = TO_YOU


# Regular verbs a done question is likely to open with. Only these
# and the verbs in _PAST are said back as an act: the word after
# "Did I" is not always the verb ("Did I already take my pills?"), and
# a guessed past tense ("You alreadied take ...") is worse than the
# plain reminder.
_REGULAR = {
    "lock", "unlock", "close", "turn", "switch", "check", "clean", "wash",
    "brush", "walk", "charge", "pack", "fill", "empty", "dry", "bolt",
    "latch", "load", "unload", "shave", "floss", "measure", "apply",
    "inject", "use", "finish", "start", "remove", "replace", "change",
}


def _past(verb):
    if verb in _PAST:
        return _PAST[verb]
    if verb.endswith("e"):
        return verb + "d"
    if len(verb) > 2 and verb.endswith("y") and verb[-2] not in "aeiou":
        return verb[:-1] + "ied"
    return verb + "ed"


def done_act(question):
    """The act a done question asks about, said back to her in the past
    tense, or None when the question does not open "Did I <verb>":
    "Did I take my night pills?" gives "took your night pills"."""
    match = re.match(r"^\s*did\s+i\s+([a-z]+)\b(.*)$", question, re.IGNORECASE)
    if not match:
        return None
    verb = match.group(1).lower()
    if verb not in _PAST and verb not in _REGULAR:
        return None
    rest = match.group(2).strip().rstrip("?.! ").strip()
    words = [
        _TO_YOU.get(word.lower(), word) for word in rest.split()
    ] if rest else []
    return " ".join([_past(verb)] + words)


def speak_text(entry, result, now=None):
    """The spoken line for a firing: the question, the answer, the time
    it was seen, and the checker's one-sentence observation. Only yes
    verdicts are spoken."""
    reason = _reason_text(result)
    done = entry.get("kind") == "done"
    key = ("done" if done else "state") + ("" if reason else "_bare")
    return SPOKEN_TEMPLATES[key].format(
        question=_spoken_question(entry),
        time=_clock(now or datetime.now()),
        reason=reason,
    )


def return_prompt(entry):
    """The check prompt for the redo watch: is she at the same place
    as this question again, about to do the thing again. Built only
    from the question's own words, so it is the same for every frame
    and every checker."""
    return (
        "Is the wearer at the same place as this question again, "
        f"about to do it again: {entry['question']}"
    )


def redo_text(witness, entry=None, next_at=None):
    """The spoken redo reminder: what she did, in her own words said
    back to her, the witnessed time in a 12-hour clock, and that it
    was seen. "You took your night pills at 9:05 PM. I saw you."
    When the same question has another window later today, its start
    is said too: "The next window opens at 1:00 PM." """
    spoken = _clock(datetime.fromisoformat(witness["at"]))
    act = done_act(entry["question"]) if entry else None
    if act:
        text = SPOKEN_TEMPLATES["redo"].format(act=act, time=spoken)
    else:
        text = SPOKEN_TEMPLATES["redo_plain"].format(time=spoken)
    if next_at is not None:
        text += " " + SPOKEN_TEMPLATES["next_window"].format(time=_clock(next_at))
    return text


def _owed_look_at(entry):
    owed = entry.get("miss_check") or {}
    return look_at_for(entry, owed.get("date"))


def state_prompt(entry):
    """The check prompt for the state check owed after a missed
    window: how things are in this frame now, judged against the
    question's own reference looks. Built only from the question's
    own words and its look-at phrase for the owed day, so a check at
    3 AM asks about the missed day's compartment, not the new day's."""
    target = _owed_look_at(entry)
    focus = (
        f"Look only at {target}. If {target} is not clearly in view, "
        "answer cannot_tell. "
        if target
        else ""
    )
    return (
        f"{focus}Judge only how things are in this frame right now, not "
        "what anyone did earlier. Describe only what is visible, and "
        "never say what anyone should do. "
        f"Does it match the yes look for: {entry['question']}"
    )


# Every sentence the post-miss state check can speak. The checker's
# reason is spoken as it is: a description of the frame, never a yes
# or no on the question. An empty compartment is not proof the pills
# were taken (they may have been dropped or moved), and "still a no"
# would point toward doing it again, so the wearer hears only what is
# there and decides. There is no line for a bare yes or no: a verdict
# with nothing seen behind it is not spoken at all.
MISS_CHECK_TEMPLATES = {
    "reason": (
        "I said I'd tell you once I could see it. You asked: {question} "
        "Here's what I can see right now: {reason}"
    ),
    "reason_look_at": (
        "I said I'd tell you once I could see it. You asked: {question} "
        "Here's what I can see at {look_at} right now: {reason}"
    ),
}


def miss_check_text(entry, result):
    """The spoken state-check result, or None when the checker gave
    no description to speak."""
    reason = _reason_text(result)
    if not reason:
        return None
    question = _spoken_question(entry)
    target = _owed_look_at(entry)
    if target:
        return MISS_CHECK_TEMPLATES["reason_look_at"].format(
            question=question, look_at=target, reason=reason
        )
    return MISS_CHECK_TEMPLATES["reason"].format(question=question, reason=reason)


# The refusal: what Mannie says when a safety question is in front of
# her but the detail that decides it is not ("Is the stove off? I
# can't tell. I can't see the knobs on the front of the stove from
# here."). A wrong all-clear on the stove is the failure Truss is
# designed against, so for a safety question a cannot-tell is worth
# saying out loud, once the thing itself is confirmed in view. No
# refusal ever tells her what to do; test_safety.py scans these too.
REFUSAL_TEMPLATES = {
    "detail": "{question} I can't tell from here. I can't quite see {detail}.",
    "plain": "{question} I can't tell from here, so I won't guess.",
}

# A refusal is spoken at most this often per question, so standing at
# the stove she hears it once, not on every frame.
REFUSAL_REST_MINUTES = 10

# After a presence check finds the thing not in view, the next one for
# that question waits this long.
ABSENT_REST = timedelta(minutes=1)

# Words that mark the checker's "missing" as a sentence about the
# photo ("The knobs need to be in view") rather than the thing itself
# ("the knobs on the front of the stove"). Only a plain noun phrase is
# spoken inside the refusal; anything else gets the plain line.
_NOT_A_THING = {
    "need", "needs", "needed", "must", "should", "would", "could",
    "visible", "view", "is", "are", "be", "been", "show", "shows",
    "shown", "see", "seen", "photo", "image", "frame", "picture",
    "camera", "angle", "closer", "unclear", "not", "no",
}


def refusal_detail(missing):
    """The checker's "missing" as a phrase that fits "I can't see ...
    from here", or None when it is not a short plain noun phrase."""
    if not isinstance(missing, str):
        return None
    text = " ".join(missing.split()).strip().rstrip(".!?;:,").strip()
    # "All of the stove's control knobs, in clear view" names the thing,
    # then where it should be; only the thing is spoken.
    text = re.sub(
        r",?\s*(in (clear |full |plain )?view|clearly visible|visible)$", "", text,
        flags=re.IGNORECASE,
    ).strip()
    if not text:
        return None
    words = re.findall(r"[a-z']+", text.lower())
    if not words or len(words) > 12:
        return None
    if _NOT_A_THING.intersection(words) or action_verbs_in(text):
        return None
    if not re.fullmatch(r"[A-Za-z0-9' ,-]+", text):
        return None
    first, _, rest = text.partition(" ")
    if first[:1].isupper() and first[1:].islower() and first != "I":
        first = first.lower()
    detail = f"{first} {rest}".strip()
    if not detail.lower().startswith(("the ", "a ", "an ", "any ", "all ", "both ", "each ")):
        detail = "the " + detail
    return detail


def refusal_text(entry, result):
    """The spoken refusal for a safety question's cannot-tell."""
    detail = refusal_detail(result.get("missing"))
    question = _spoken_question(entry)
    if detail:
        return REFUSAL_TEMPLATES["detail"].format(question=question, detail=detail)
    return REFUSAL_TEMPLATES["plain"].format(question=question)


def presence_question(thing):
    """The check that tells "the stove is here but its knobs are not"
    from "the stove is not here at all"."""
    return (
        f"Is any part of the {thing} itself in this photo? Answer yes "
        f"if the {thing} is in view at all, even partly or from an "
        f"angle, and no if it is not in the photo."
    )


def _thing_for(entry):
    """The SAM phrase that names the thing itself ("stove"), or None
    when there is none. A phrase naming a part ("stove knob") would
    make the presence check ask about the very detail that is out of
    view, so a whole-thing phrase is preferred; a part is used only
    when nothing else was compiled."""
    try:
        folder = Path(entry["ref_yes_path"]).parent
        lines = (folder / "prompts.txt").read_text(encoding="utf-8").splitlines()
    except (KeyError, TypeError, OSError, UnicodeDecodeError):
        return None
    phrases = [
        line.strip() for line in lines
        if re.fullmatch(r"[a-z][a-z -]{0,40}", line.strip())
    ]
    for phrase in phrases:
        if not _PART_WORDS.intersection(re.findall(r"[a-z]+", phrase)):
            return phrase
    return phrases[0] if phrases else None


# Words that name a part of a thing rather than the thing.
_PART_WORDS = {
    "knob", "knobs", "dial", "dials", "button", "buttons", "switch",
    "switches", "handle", "handles", "burner", "burners", "control",
    "controls", "compartment", "compartments", "lid", "lids", "latch",
    "bolt", "lock", "lever", "flame",
}


def _refusable(entry, result):
    """A model's own cannot-tell on a safety state question that named
    what it lacked. A check that failed (no answer from the model) is
    never spoken as a refusal: that is not her stove being unreadable."""
    return (
        entry.get("kind", "state") == "state"
        and entry.get("safety") is True
        and isinstance(result, dict)
        and result.get("verdict") == "cannot_tell"
        # Any failure means no answer was made.
        and result.get("failure") in (None, "")
        and isinstance(result.get("missing"), str)
        and result["missing"].strip() != ""
    )


class CheckLoop:
    def __init__(self, store, check_fn, log_dir, speak_fn=None, now_fn=None,
                 checks=None, outline_fn=None, presence_fn=None,
                 refusal_minutes=REFUSAL_REST_MINUTES, batch_fn=None):
        self.store = store
        # batch_fn(frame_path, entries) checks several due questions in
        # one model call and returns one result per entry, in order, or
        # None when it cannot (see verdict.check_many). A result whose
        # failure is set is asked again on its own through check_fn, so
        # a batch can only save calls, never lose an answer.
        self.batch_fn = batch_fn
        # presence_fn(frame_path, question_text) is a plain check with
        # no reference looks. With one, a safety question's cannot-tell
        # is spoken as a refusal when the thing itself is in view (see
        # REFUSAL_TEMPLATES). Without one, every cannot-tell is silent.
        self.presence_fn = presence_fn
        self.refusal_rest = timedelta(minutes=refusal_minutes)
        # When each question's last refusal was spoken. In memory: a
        # restart can only mean one more refusal, never a missed answer.
        self._refused = {}
        # When each question's thing was last checked and not in view.
        self._absent = {}
        # The check log (a coverage.CheckLog). Without one, no check
        # is counted as watching, and alerts claim nothing was watched.
        self.checks = checks
        self.check_fn = check_fn
        self.log_dir = Path(log_dir)
        self.speak_fn = speak_fn
        self.now_fn = now_fn or datetime.now
        # outline_fn(evidence, entries) returns where the outlined copy
        # will be written, or None; see sam_client.EvidenceOutliner.
        self.outline_fn = outline_fn

    def process_frame(self, frame_path):
        frame_path = Path(frame_path)
        try:
            standing = [e for e in self.store.list() if e["state"] == "standing"]
            watch = self.store.redo_watch(now=self.now_fn())
            owed = self.store.miss_check_watch(now=self.now_fn())
            # Claimed last, so nothing after it in this block can fail
            # and leak the claim.
            due = self.store.claim_due(now=self.now_fn()) if standing else []
        except Exception:
            # A broken question file must never break capturing. Fall
            # back to day 1 behavior: keep the frame, manage nothing.
            return {"managed": False, "fired": [], "speak": [], "errors": 1}
        if not standing and not watch:
            return {"managed": False, "fired": [], "speak": [], "errors": 0}

        # Done questions under check stay claimed for the whole check,
        # from before the model call until the witness is written, so
        # the sweep cannot raise a miss for a window that closes while
        # the answer is still on its way.
        claimed = [e["slug"] for e in due if e.get("kind") == "done"]
        try:
            return self._check_frame(frame_path, due, watch, owed)
        finally:
            self.store.end_checks(claimed)

    def _check_frame(self, frame_path, due, watch, owed):
        now = self.now_fn()
        fired = []
        refusable = None
        errors = 0
        if not due:
            print("  check: no question is due on this frame (each waits for its gap)", flush=True)
        batched, batch_started = self._batch(frame_path, due)
        for entry in due:
            started = time.monotonic()
            try:
                if entry["slug"] in batched:
                    result, started = batched[entry["slug"]], batch_started
                else:
                    result = self.check_fn(frame_path, entry)
                if not isinstance(result, dict):
                    raise TypeError("check returned no verdict")
                verdict = result.get("verdict")
            except Exception as e:
                # A failed check is treated like a cannot_tell: silent,
                # never spoken, and the question is still marked checked
                # so a broken checker cannot hot-loop on every frame.
                result, verdict = {}, None
                errors += 1
                _print_check(entry, None, f"check failed: {e}", started)
            else:
                _print_check(entry, verdict, _reason_text(result), started)
            if (
                entry.get("kind") == "done"
                and verdict in ("yes", "no", "cannot_tell")
                and not result.get("failure")
                and self.checks is not None
            ):
                # This question was really watched on this frame. A
                # failed check was not, and is never counted: the
                # checker reports an outage as cannot_tell with a
                # failure kind, and that frame was never looked at.
                try:
                    self.checks.record(entry["slug"], when=now)
                except Exception:
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
            elif refusable is None and _refusable(entry, result):
                refusable = (entry, result)

        refusal = self._refusal(frame_path, refusable, now)

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
                    try:
                        next_at = self.store.next_window(entry, now=now)
                    except Exception:
                        errors += 1
                        next_at = None
                    redo = (entry, witness, next_at)
                    break

        # The state check owed after a missed window: a safety done
        # question is offered the frame under a state prompt. A yes or
        # a no is a sighting of the thing and is spoken; a cannot_tell
        # or a failed check stays silent and waits for a later frame.
        seen = []
        for entry in owed:
            if entry["slug"] in fired_slugs:
                continue
            prompt_entry = dict(entry)
            prompt_entry["question"] = state_prompt(entry)
            try:
                result = self.check_fn(frame_path, prompt_entry)
                if not isinstance(result, dict):
                    raise TypeError("check returned no verdict")
                verdict = result.get("verdict")
            except Exception:
                result, verdict = {}, None
                errors += 1
            try:
                self.store.mark_checked(entry["slug"], when=now)
            except Exception:
                errors += 1
                continue
            if verdict in ("yes", "no"):
                # A yes or no with no description behind it is treated
                # as cannot_tell: silent, and the check stays owed.
                text = miss_check_text(entry, result)
                if text is not None:
                    seen.append((entry, verdict, text))

        speak = []
        evidence = None
        outline = None
        completed_fired = []
        if fired or redo is not None or seen or refusal is not None:
            if fired:
                first = fired[0][0]
            elif redo is not None:
                first = redo[0]
            elif seen:
                first = seen[0][0]
            else:
                first = refusal[0]
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
                text = speak_text(entry, result, now)
                speak.append(text)
                if self.speak_fn is not None:
                    try:
                        self.speak_fn(text, entry, evidence)
                    except Exception:
                        errors += 1
            if redo is not None:
                entry, witness, next_at = redo
                try:
                    self._append_redo_log(entry, witness, evidence, now, next_at)
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
                    text = redo_text(witness, entry, next_at)
                    speak.append(text)
                    if self.speak_fn is not None:
                        try:
                            self.speak_fn(text, entry, evidence)
                        except Exception:
                            errors += 1
            for entry, verdict, text in seen:
                try:
                    self._append_miss_check_log(entry, verdict, text, evidence, now)
                except Exception:
                    errors += 1
                try:
                    self.store.mark_miss_check_spoken(entry["slug"])
                except Exception:
                    # Unmarked means unspoken: the check waits for a
                    # later frame rather than risk being spoken twice.
                    errors += 1
                    continue
                speak.append(text)
                if self.speak_fn is not None:
                    try:
                        self.speak_fn(text, entry, evidence)
                    except Exception:
                        errors += 1
            if refusal is not None:
                entry, result, text = refusal
                try:
                    self._append_refusal_log(entry, result, text, evidence, now)
                except Exception:
                    errors += 1
                self._refused[entry["slug"]] = now
                speak.append(text)
                if self.speak_fn is not None:
                    try:
                        self.speak_fn(text, entry, evidence)
                    except Exception:
                        errors += 1
            if self.outline_fn is not None and completed_fired:
                try:
                    outline = self.outline_fn(evidence, completed_fired)
                except Exception:
                    errors += 1
                    outline = None
        else:
            frame_path.unlink(missing_ok=True)

        return {
            "managed": True,
            "fired": [entry["slug"] for entry in completed_fired],
            "refused": [refusal[0]["slug"]] if refusal is not None else [],
            "speak": speak,
            "evidence": str(evidence) if evidence else None,
            "outline": str(outline) if outline else None,
            "errors": errors,
        }

    def _batch(self, frame_path, due):
        """({slug: result}, start time) for the due questions one batch
        call answered. Empty with no batch_fn, fewer than two questions,
        or a batch that failed; those questions are checked one by one.

        A safety question is never batched: it is always asked alone, so
        a misnumbered or blended answer in a batch can never be its
        all-clear."""
        started = time.monotonic()
        due = [e for e in due if e.get("safety") is not True]
        if self.batch_fn is None or len(due) < 2:
            return {}, started
        try:
            results = self.batch_fn(frame_path, due)
        except Exception as e:
            print(f"  check: the one-call check failed ({e}); asking one by one", flush=True)
            return {}, started
        if not isinstance(results, list) or len(results) != len(due):
            return {}, started
        answered = {}
        for entry, result in zip(due, results):
            if isinstance(result, dict) and result.get("failure") in (None, ""):
                answered[entry["slug"]] = result
        if len(answered) < len(due):
            print(
                f"  check: one call answered {len(answered)} of {len(due)}; "
                "the rest are asked one by one",
                flush=True,
            )
        else:
            print(f"  check: {len(due)} questions in one call", flush=True)
        return answered, started

    def _refusal(self, frame_path, refusable, now):
        """(entry, result, text) for a refusal to speak on this frame,
        or None. The cannot-tell is only spoken once a plain presence
        check says the thing itself is in view, so a frame of the
        hallway, where the stove is not, stays silent."""
        if refusable is None or self.presence_fn is None:
            return None
        entry, result = refusable
        last = self._refused.get(entry["slug"])
        if last is not None and timedelta(0) <= now - last < self.refusal_rest:
            return None
        absent = self._absent.get(entry["slug"])
        if absent is not None and timedelta(0) <= now - absent < ABSENT_REST:
            return None
        thing = _thing_for(entry)
        if thing is None:
            return None
        try:
            seen = self.presence_fn(frame_path, presence_question(thing))
        except Exception:
            return None
        if not isinstance(seen, dict) or seen.get("verdict") != "yes":
            # Not in view: no presence check for this question for a
            # minute, so frames away from the stove cost one extra
            # model call a minute at most.
            self._absent[entry["slug"]] = now
            return None
        return entry, result, refusal_text(entry, result)

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
        line += f" | model: {model_name(result)}\n"
        with open(day / "answers.txt", "a") as f:
            f.write(line)

    def _append_redo_log(self, entry, witness, evidence, now, next_at=None):
        """The redo line names the witness photo, the one of her doing
        it, as its evidence, so the History screen shows the moment she
        was seen; the frame she was caught returning in is kept beside
        it and named as "now"."""
        day = self._day_dir(now)
        shown = evidence.name
        witness_frame = witness.get("frame")
        if isinstance(witness_frame, str) and (day / witness_frame).is_file():
            shown = witness_frame
        line = (
            f"{now.strftime('%H:%M:%S')} | {entry['question']} | "
            f"redo reminder | {redo_text(witness, entry, next_at)} | "
            f"evidence: {shown}"
        )
        if shown != evidence.name:
            line += f" | now: {evidence.name}"
        with open(day / "answers.txt", "a") as f:
            f.write(line + "\n")

    def _append_miss_check_log(self, entry, verdict, text, evidence, now):
        day = self._day_dir(now)
        line = (
            f"{now.strftime('%H:%M:%S')} | {entry['question']} | "
            f"state check after a missed window | {verdict} | {text} | "
            f"evidence: {evidence.name}\n"
        )
        with open(day / "answers.txt", "a") as f:
            f.write(line)

    def _append_refusal_log(self, entry, result, text, evidence, now):
        day = self._day_dir(now)
        line = (
            f"{now.strftime('%H:%M:%S')} | {entry['question']} | "
            f"cannot tell | {text} | evidence: {evidence.name} | "
            f"model: {model_name(result)}\n"
        )
        with open(day / "answers.txt", "a") as f:
            f.write(line)
