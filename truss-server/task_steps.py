#!/usr/bin/env python3
"""
Turning a named job into guide steps.

The wearer names a job in plain words ("lock up for the night",
"assemble the bookshelf", "make chicken curry"). One words-only call to
the model (Muse Spark first, through endpoint.ask_json) writes it as a
short list of steps, each a yes/no question a glance through the
glasses can settle. Those steps then run as an ordinary guide (see
guide.py): one at a time, each confirmed before the next is spoken.

No photo is sent: only the job's words.

The guide never makes a safety call by eye, and neither do these
steps. That is judged by intent, not by a list of objects: the writing
call is told to leave out any step whose yes would tell the person
that something is now safe, and then a second, separate words-only
call reads each written step and says whether a yes to it would be
such an all-clear (something that could hurt someone if wrong is off,
out, unplugged, locked, taken, or done inside). Those steps are moved
to left_out, so the wearer hears they are not checked here; they are
the jobs for a standing question with its two reference looks. A
number read off a probe thermometer's display is a reading the person
judges, not an all-clear, so doneness can still be a step. If the
second call fails or leaves a step unjudged, that step is treated as
an all-clear: the guide fails closed.

compose_steps never returns a half-made list: it returns the steps or
raises TaskError with a plain sentence the Desk can show.
"""

MAX_STEPS = 8
MAX_STEP_CHARS = 160
MAX_TASK_CHARS = 200
# Room for Muse Spark's thinking as well as the answer. At 2048 its
# thinking used up the room, the reply was cut off, and the retry was
# cut off too, so no steps were written.
MAX_TOKENS = 16384

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_STEPS,
        },
        "left_out": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "steps", "left_out"],
    "additionalProperties": False,
}

SCHEMA_NAME = "guide_task"

SYSTEM_PROMPT = (
    "You are writing the steps of a job a person is about to do while "
    "wearing smart glasses with a camera. Each step will be checked "
    "against a photo from the glasses before the next step is spoken. "
    "You are given the job in the person's own words. Reply with JSON "
    "only, with these fields. "
    "title: the job in two to five plain words. "
    f"steps: two to {MAX_STEPS} steps in the order they are done. Each "
    "step is one short yes/no question that a single glance at the "
    "work can settle, where yes means the step is done. Ask only about "
    "what a photo shows: something present or gone, a count, a "
    "position, a colour, or a number read off a display or dial. One "
    'visible thing per step. Good: "Are the onions golden brown?", '
    '"Is the shelf level?", "Are all four legs screwed on?", '
    '"Does the thermometer read 74 or more?". Bad: "Did you wash your '
    'hands?" (not visible), "Is everything ready?" (not one thing). '
    "Never write a step whose yes would tell the person that something "
    "which could hurt someone if it were wrong is now safe: switched "
    "off, put out, unplugged, locked, taken, cooked through, or safe in "
    "any other way, whatever the object. Leave those steps out. The one exception is food doneness when a "
    "probe thermometer is part of the job: then the step reads the "
    "number on the thermometer's display, as in the good example. "
    "left_out: the steps you left out for that reason, each in a few "
    "plain words, or an empty list. "
    "Use plain, neutral words. Never guess at details the job does not give."
)

# The second call: each written step judged by intent.
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "all_clear": {"type": "boolean"},
                },
                "required": ["step", "all_clear"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["steps"],
    "additionalProperties": False,
}

CLASSIFY_SCHEMA_NAME = "guide_step_intent"

CLASSIFY_PROMPT = (
    "You check the steps of a guided job before they are used. Each step "
    "is a yes/no question that will be answered from one photo from "
    "smart glasses, and a yes moves the person on. A wrong yes must never "
    "tell someone that something is safe when it is not. You are given "
    "the steps, one per line. For each step, judge its intent, not its "
    "words: would a yes to it tell the person that something which could "
    "hurt someone if it were wrong is now safe? That covers anything "
    "declared off, out, unplugged, shut down, locked, secured, taken, "
    "given, or cooked or safe to eat or use, whatever the object. If so, "
    "all_clear is true. A step that only reads a number off a probe "
    "thermometer's display is a reading the person judges, not an "
    "all-clear: all_clear is false for it, unless it also declares "
    "something else safe. Steps about building, tidying, placing, "
    "counting, or how something looks while the person is still working "
    "are not all-clears. When unsure, all_clear is true. Reply with JSON "
    "only: steps, one entry per given step, in the same order, with the "
    "step copied exactly and all_clear."
)


class TaskError(Exception):
    pass


def judge_steps(steps, ask_json, text_part):
    """Which steps are all-clears, by intent: a list of booleans, one
    per step. Fails closed: a failed call, or a step it leaves
    unjudged, counts as an all-clear."""
    if not steps:
        return []
    try:
        answer = ask_json(
            CLASSIFY_PROMPT,
            [text_part("\n".join(steps))],
            CLASSIFY_SCHEMA,
            CLASSIFY_SCHEMA_NAME,
            max_tokens=MAX_TOKENS,
        )
        judged = {}
        for item in answer.get("steps") or []:
            if isinstance(item, dict) and isinstance(item.get("step"), str):
                key = _clean_step(item["step"])
                verdict = item.get("all_clear")
                # A step judged twice keeps its safer verdict.
                judged[key] = judged.get(key, False) or verdict is not False
    except Exception:
        return [True] * len(steps)
    return [judged.get(step, True) for step in steps]


def _clean_step(step):
    if not isinstance(step, str):
        return ""
    step = " ".join(step.split())
    if not step:
        return ""
    step = step[:MAX_STEP_CHARS].rstrip()
    if not step.endswith("?"):
        step = step.rstrip(".!") + "?"
    return step


def clean_task(task):
    """The job's words, trimmed, or TaskError."""
    if not isinstance(task, str) or not task.strip():
        raise TaskError("Name the job in a few words, like lock up for the night.")
    task = " ".join(task.split())
    if len(task) > MAX_TASK_CHARS:
        raise TaskError(f"Name the job in fewer words (at most {MAX_TASK_CHARS} characters).")
    return task


def compose_steps(task, ask_json, text_part):
    """Ask the model for the job's steps. ask_json and text_part are
    endpoint.ask_json and endpoint.text_part, injected so this can be
    tested without a network. Returns {"title", "steps", "left_out",
    "model"}; raises TaskError with a plain sentence on any failure."""
    task = clean_task(task)
    try:
        answer = ask_json(
            SYSTEM_PROMPT,
            [text_part(f"The job: {task}")],
            TASK_SCHEMA,
            SCHEMA_NAME,
            max_tokens=MAX_TOKENS,
        )
    except Exception as e:
        reason = (str(e).strip() or "The model call failed").rstrip(".")
        raise TaskError(f"{reason}, so no steps were written.")
    if not isinstance(answer, dict):
        raise TaskError("The model's answer was not a list of steps.")

    written = []
    for raw in answer.get("steps") or []:
        step = _clean_step(raw)
        if step and step not in written:
            written.append(step)
    written = written[:MAX_STEPS]
    steps, left_out = [], []
    for step, all_clear in zip(written, judge_steps(written, ask_json, text_part)):
        if all_clear:
            left_out.append(step.rstrip("?"))
        else:
            steps.append(step)
    for raw in answer.get("left_out") or []:
        if isinstance(raw, str) and " ".join(raw.split()):
            left_out.append(" ".join(raw.split())[:MAX_STEP_CHARS])
    if not steps:
        raise TaskError(
            "No step of that job can be checked by eye here. Safety checks "
            "need a standing question with its two looks."
        )
    title = answer.get("title")
    title = " ".join(title.split())[:60] if isinstance(title, str) and title.strip() else task[:60]
    model = answer.get("_model")
    return {
        "title": title,
        "steps": steps,
        "left_out": left_out,
        "model": model if isinstance(model, str) else "",
    }


def left_out_line(left_out):
    """The spoken line naming what the guide will not check, or None."""
    if not left_out:
        return None
    items = "; ".join(s.rstrip(".?") for s in left_out[:3])
    more = f" and {len(left_out) - 3} more" if len(left_out) > 3 else ""
    return f"Not checked in this guide: {items}{more}."
