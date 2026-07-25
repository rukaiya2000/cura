"""Deciding whether the bot was spoken to, and what it should say back.

Almost every line in a consultation is between a doctor and a patient, and the bot has no
business in it. So the default is silence, and this module exists to make the exceptions
narrow and legible rather than clever.

**Addressed by name, or nothing.** No intent classification, no model call, no confidence
score. Somebody says "Cura" and asks a question the bot can answer; otherwise the words
are recorded and that is all. The failure this avoids is a bot that decides a doctor was
talking to it — in a room where the alternative interpretation is a patient being
listened to.

What it will answer divides in two, and the line between them is the important part.

**Presence** — is it there, is it recording, who is it. No clinical content at all.

**Retrieval** — what is already written on this patient's record: their conditions,
medications, allergies, when they were last seen. It reads the record back. It does
**not** interpret it, and it will not answer a question that asks it to: "should I
increase the ramipril" gets silence, because the difference between reading a record and
giving clinical advice is the difference between this product and a much more dangerous
one. That boundary is enforced by refusing anything not matched by an explicit retrieval
rule, rather than by asking a model to stay in its lane.
"""

from __future__ import annotations

import re

#: What the bot answers to. Matched as a whole word so "curator" and "accurate" do not
#: wake it, and case-insensitively because a transcript's capitalisation is the
#: transcriber's guess.
NAME = r"\b(cura|kura|cora)\b"

#: Each rule is (what was asked, what to say). Ordered — the first match wins, so put
#: the specific before the general.
RULES: tuple[tuple[str, str], ...] = (
    (r"\b(can you hear|do you hear|are you (there|listening|on|with us)|you there)\b",
     "Yes, I can hear you."),
    (r"\b(are you recording|recording this|taking notes|getting this|got that)\b",
     "Yes — I'm capturing the consultation. Nothing is sent to anyone until you approve "
     "it."),
    (r"\b(who are you|what are you|introduce yourself)\b",
     "I'm Cura. I keep the note for this consultation and draft the patient's letter for "
     "you to review."),
    (r"\b(stop recording|stop listening|pause|be quiet|shut up)\b",
     "Understood — I'll stay quiet. Everything so far is saved and waiting for you."),
)


#: Questions answered from the patient's own record. Each maps to a field and a way of
#: saying it. Retrieval only — nothing here computes, compares or advises.
#: Prefixes end in `\w*`, not `\b`. `\ballerg\b` cannot match "allergies" — the word
#: carries on — so the rule silently never fired, and the bot answered nothing while
#: looking like it had no opinion.
RECORD_RULES: tuple[tuple[str, str], ...] = (
    (r"\b(medication\w*|medicine\w*|meds|drugs?|prescri\w*|taking|on right now)\b",
     "medications"),
    (r"\ballerg\w*|\breact\w*", "allergies"),
    (r"\b(condition\w*|histor\w*|diagnos\w*|problem\w*|past)\b", "conditions"),
    (r"\b(last (seen|visit\w*|appointment)|when did|previous\w*)\b", "last_seen"),
    (r"\b(who is|who am i|which patient|whose|remind me who|patient)\b", "who"),
)

#: Asked of the bot, these are requests for judgement rather than for the record. Matched
#: first and always refused, so a question that mentions medication *and* asks what to do
#: about it does not get answered by the medication rule.
ADVICE = (r"\b(should|shall|would you|do you think|recommend|advise|is it (safe|ok|okay)|"
          r"what would you|diagnos(e|is)\?|increase|decrease|stop|start|switch|dose)\b")


def _fmt(items) -> str:
    items = [str(i) for i in (items or []) if str(i).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def record_reply(text: str, patient: dict | None) -> str | None:
    """Read back what is on the patient's record, or None to stay silent.

    `patient` is the record for the consultation the bot is *bound to* — never a lookup,
    never a search. The bot cannot be asked about somebody else, because it has no way to
    name them: the only record it holds is the one this consultation is about.
    """
    if not patient or not addressed(text):
        return None
    # Advice is refused before retrieval is attempted, so "Cura, should I increase her
    # ramipril?" cannot be answered by the medication rule that also matches it.
    if re.search(ADVICE, text, re.IGNORECASE):
        return None

    for pattern, field in RECORD_RULES:
        if not re.search(pattern, text, re.IGNORECASE):
            continue
        name = patient.get("name") or "this patient"
        if field == "who":
            bits = [name]
            if patient.get("dob"):
                bits.append(f"date of birth {patient['dob']}")
            if patient.get("id"):
                bits.append(patient["id"])
            return f"This consultation is with {', '.join(bits)}."
        if field == "last_seen":
            seen = patient.get("last_seen")
            count = patient.get("consultations", 0)
            if not seen and not count:
                return f"{name} has no previous consultation on record."
            return (f"{name} was last seen {seen}." if seen
                    else f"{name} has {count} previous consultation"
                         f"{'s' if count != 1 else ''} on record.")
        listed = _fmt(patient.get(field))
        label = {"medications": "on", "allergies": "allergic to",
                 "conditions": "recorded with"}[field]
        if not listed:
            return f"Nothing is recorded for {name} under {field}."
        return f"{name} is {label} {listed}, according to the record."
    return None


#: The doctor explicitly asking for the record to change. Nothing else does — a patient
#: mentioning a new symptom is captured in the transcript and drafted into the note, but
#: it does not touch the record until somebody says so. A record that edits itself from
#: overheard conversation is one no clinician would trust twice.
UPDATE_ASKED = (
    r"\b(update|add (that |this |it )?to|put (that|this|it) (on|in)|"
    r"record (that|this|it)|note (that|this|it) (down|on)|change (her|his|their|the) "
    r"(record|notes?|details))\b")


def update_requested(text: str) -> bool:
    """Did the clinician ask for the record itself to be changed?

    Requires the bot to be addressed *and* an explicit instruction. "She's on 10mg now"
    said to the patient is a fact for the note; "Cura, update her record — she's on 10mg
    now" is an instruction. The difference is the whole reason this function exists.
    """
    return bool(addressed(text) and re.search(UPDATE_ASKED, text or "", re.IGNORECASE))


def addressed(text: str) -> bool:
    """Was the bot named in this line?"""
    return bool(re.search(NAME, text or "", re.IGNORECASE))


def reply_to(text: str) -> str | None:
    """What to say back, or None to stay silent.

    None is the overwhelmingly common answer and the safe one. A line that names the bot
    but asks something outside the small set above gets no reply rather than a guess:
    answering "Cura, what was her HbA1c last time?" would mean volunteering clinical
    information into a live room on the strength of a regex.
    """
    if not addressed(text):
        return None
    for pattern, response in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return response
    return None
