#!/usr/bin/env python3
"""
The action-verb rule for safety wording.

Nothing Truss says about a safety question may tell her to do the act
itself: a missed witness followed by "take your pills" is how the
product would cause the double dose it exists to prevent. The rule is
deliberately stricter than "no imperatives": none of these verbs may
appear anywhere, in any mood or form, so no rewording can slip an
instruction to act past it. "Check" and "look" are the only
directions given; "act" names the act without performing it.

The same scan guards the fixed templates (test_safety.py) and the
look-at phrase a planter types (questions.py), since that phrase is
spoken inside the direction.
"""

import re

ACTION_VERBS = (
    "take", "turn", "feed", "lock", "unlock", "switch", "shut", "close",
    "open", "swallow", "give", "eat", "drink", "light", "pour", "put",
    "press", "push", "pull", "grab", "use", "refill", "fill", "do", "go",
    "finish", "start", "stop", "remember", "forget",
)

_IRREGULAR = {
    "take": ("took", "taken"),
    "feed": ("fed",),
    "give": ("gave", "given"),
    "eat": ("ate", "eaten"),
    "drink": ("drank", "drunk"),
    "light": ("lit",),
    "do": ("did", "done", "does"),
    "go": ("went", "gone", "goes"),
    "forget": ("forgot", "forgotten"),
}


def _forms(verb):
    forms = {verb, verb + "s", verb + "es", verb + "ed", verb + "d", verb + "ing", verb + "en"}
    if verb.endswith("e"):
        forms.add(verb[:-1] + "ing")
    # Doubled final consonant: grabbed, stopping, shutting.
    forms.update({verb + verb[-1] + "ed", verb + verb[-1] + "ing"})
    forms.update(_IRREGULAR.get(verb, ()))
    return forms


FORBIDDEN_WORDS = {form: verb for verb in ACTION_VERBS for form in _forms(verb)}


def action_verbs_in(text):
    """The action verbs in a text, in any form, with {placeholders}
    removed. Returns the verbs found, sorted; empty means clean."""
    bare = re.sub(r"\{[a-z_]+\}", " ", text)
    words = re.findall(r"[a-z]+", bare.lower())
    return sorted({FORBIDDEN_WORDS[w] for w in words if w in FORBIDDEN_WORDS})


# The wearer's words are their own ("my pills"); Mannie says them back
# to them ("your pills").
TO_YOU = {
    "i": "you", "me": "you", "my": "your", "mine": "yours",
    "myself": "yourself", "we": "you", "us": "you", "our": "your",
    "ours": "yours", "i'm": "you're", "i've": "you've", "i'll": "you'll",
    "i'd": "you'd", "we're": "you're", "we've": "you've",
}

# The verb that goes with "I" changes too when it is said back:
# "am I" is "are you", "I was" is "you were".
_WITH_I = {"am": "are", "was": "were"}


def to_you(text):
    """A question in the wearer's own words said back to them: "Did I
    take my pills?" gives "Did you take your pills?". Only pronouns
    and the two verbs that agree with "I" change; every other word,
    and the punctuation, is kept as typed. A question already worded
    another way comes back unchanged."""
    parts = re.split(r"([A-Za-z']+)", text)
    words = [i for i in range(1, len(parts), 2)]
    lower = [parts[i].lower() for i in words]
    out = list(parts)
    for n, i in enumerate(words):
        word = lower[n]
        if word in _WITH_I and (
            (n > 0 and lower[n - 1] == "i") or (n + 1 < len(lower) and lower[n + 1] == "i")
        ):
            new = _WITH_I[word]
        elif word in TO_YOU:
            new = TO_YOU[word]
        else:
            continue
        if parts[i][:1].isupper() and (word != "i" or i == words[0]):
            new = new[:1].upper() + new[1:]
        out[i] = new
    return "".join(out)
