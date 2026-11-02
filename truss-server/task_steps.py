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
steps. The model is told to leave out any step whose yes would be an
all-clear on something that can hurt (a stove, pills, whether food is
cooked through) and to name it in left_out instead; a step about how
done food is becomes a reading off a thermometer's display. A small
word list then catches a safety step the model wrote anyway and moves
it to left_out, so the wearer hears it is not checked here. Those are
the jobs for a standing question with its two reference looks.

compose_steps never returns a half-made list: it returns the steps or
raises TaskError with a plain sentence the Desk can show.
"""

import re

MAX_STEPS = 8
MAX_STEP_CHARS = 160
MAX_TASK_CHARS = 200
MAX_TOKENS = 2048

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
    '"Is the back door bolt across?", "Are all four legs screwed on?", '
    '"Does the thermometer read 74 or more?". Bad: "Did you wash your '
    'hands?" (not visible), "Is everything ready?" (not one thing). '
    "Never write a step whose yes would be an all-clear on something "
    "that can hurt someone: whether a stove, oven or burner is off, "
    "whether pills or medicine were taken or the dose is right, whether "
    "food is cooked through or safe to eat, whether something is safe. "
    "Leave those steps out. The one exception is food doneness when a "
    "probe thermometer is part of the job: then the step reads the "
    "number on the thermometer's display, as in the good example. "
    "left_out: the steps you left out for that reason, each in a few "
    "plain words, or an empty list. "
    "Use plain, neutral words. Never guess at details the job does not give."
)

# A step that reads as a safety all-clear despite the prompt. A plain
# word list, not a classifier: it errs toward leaving a step out,
# which the wearer hears about, over checking it by eye.
SAFETY_WORDS = re.compile(
    r"\b(safe|safely|unsafe|stove|oven|burner|burners|hob|gas|cooked|"
    r"undercooked|raw|pill|pills|medicine|medicines|medication|"
    r"medications|dose|doses|dosage|insulin)\b",
    re.IGNORECASE,
)
# A step that reads a number off a display is a reading, not a
# verdict, so it stays even when it names the food.
READING_WORDS = re.compile(r"\b(thermometer|display|reads?|reading)\b", re.IGNORECASE)


class TaskError(Exception):
    pass


def is_safety_step(step):
    return bool(SAFETY_WORDS.search(step)) and not READING_WORDS.search(step)


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

    steps, left_out = [], []
    for raw in answer.get("steps") or []:
        step = _clean_step(raw)
        if not step or step in steps:
            continue
        if is_safety_step(step):
            left_out.append(step.rstrip("?"))
        else:
            steps.append(step)
    for raw in answer.get("left_out") or []:
        if isinstance(raw, str) and " ".join(raw.split()):
            left_out.append(" ".join(raw.split())[:MAX_STEP_CHARS])
    steps = steps[:MAX_STEPS]
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
