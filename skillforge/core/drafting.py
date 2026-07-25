"""Turning what was said into a clinical note and a patient letter.

This is the step the approval screen exists for, and its whole value is one property:
**every claim cites the utterances it came from.** Not as documentation — as data. A
sentence that cites nothing is not quietly accepted, it is flagged before a human is asked
to approve it.

Three decisions that look like prompt engineering and are actually safety design:

**Utterances are numbered and the model may only cite those numbers.** A citation is
therefore checkable rather than plausible: a claim referencing `u7` in a nine-line
consultation is verified by looking at line seven. Anything citing an id that does not
exist is treated as citing nothing, because a fabricated citation is worse than an absent
one — it survives review by looking like evidence.

**The note and the letter are drafted together, from the same transcript, as separate
documents.** Generating one from the other compounds error; generating the letter *from
the note* would mean the patient receives a paraphrase of a paraphrase.

**Nothing here writes, sends, or schedules.** The drafter reads a transcript and returns
text. Every effect the letter describes is somebody else's job, gated separately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """\
You are drafting the written record of a medical consultation for the clinician who
conducted it. They will read what you write, edit it, and decide whether to send it. You
are not deciding anything.

Produce two documents from the same transcript.

**The clinical note** goes into the patient's record. Write it the way a doctor writes for
another doctor: terse, precise, standard abbreviations, no reassurance. Sections such as
History, Examination, Medication, Plan — include only those the consultation supports.

**The patient letter** goes to the patient. Same facts, entirely different register: plain
language, second person, no jargon or abbreviation, nothing frightening that the
consultation did not actually raise. Explain what was found, what happens next, and what
they should do. If the clinician mentioned an appointment or a test, say so plainly.

Hard rules:

1. **Every block cites its sources**, as a list of utterance ids from the transcript. Cite
   the ids you actually drew the content from, and only those.
2. **Never invent a citation.** An id that is not in the transcript is worse than no id,
   because it survives review by looking like evidence.
3. If you write something the transcript does not support — a courtesy, an inference, a
   detail you expect but did not hear — give it an **empty** `sources` list. It will be
   flagged for the clinician rather than hidden. Do not manufacture a citation to avoid
   the flag.
4. Greetings, sign-offs and pleasantries carry `"kind": "courtesy"` and empty sources.
   They make no clinical claim, so they are exempt from flagging.
5. **Do not diagnose, prescribe, or give advice the clinician did not give.** You are
   recording what happened, not adding to it.
6. Never state a measurement, dose or date the transcript does not contain.

Write nothing that would alarm a patient who reads it alone, at night, without a doctor
present to explain it.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {
            "type": "array",
            "description": "clinical note sections, in the order they should appear",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "text": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["section", "text", "sources"],
                "additionalProperties": False,
            },
        },
        "letter": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "greeting": {"type": "string"},
                "paragraphs": {"$ref": "#/$defs/blocks"},
                "todos": {"$ref": "#/$defs/blocks"},
                # Named apart because a model handed both will otherwise put the
                # sign-off in `closing` and leave the appointment unsaid — observed on
                # the first real draft.
                "closing": {"allOf": [{"$ref": "#/$defs/block"}], "description":
                            "the last substantive sentence — when they are next seen, "
                            "or what happens next. NOT the sign-off."},
                "sign_off": {"type": "string", "description":
                             "e.g. 'Best wishes,\nDr Rao'. No clinical content."},
            },
            "required": ["subject", "greeting", "paragraphs", "todos", "closing",
                         "sign_off"],
            "additionalProperties": False,
        },
        "summary": {
            "type": "string",
            "description": "one or two sentences for the clinician, not the patient",
        },
    },
    "required": ["note", "letter", "summary"],
    "additionalProperties": False,
    "$defs": {
        "block": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "kind": {"type": "string", "enum": ["clinical", "courtesy"]},
            },
            "required": ["text", "sources", "kind"],
            "additionalProperties": False,
        },
        "blocks": {"type": "array", "items": {"$ref": "#/$defs/block"}},
    },
}


class DraftError(Exception):
    """The draft could not be produced, or came back unusable."""


@dataclass
class Draft:
    """What the approval screen renders. Ids are assigned here, not by the model."""

    note: list[dict] = field(default_factory=list)
    letter: dict = field(default_factory=dict)
    summary: str = ""
    drafted_by: str = MODEL
    #: Citations the model made up. Kept rather than silently dropped: a model inventing
    #: evidence is worth knowing about, and it is invisible once the claim is merely
    #: unflagged.
    invented_citations: list[str] = field(default_factory=list)

    @property
    def blocks(self) -> list[dict]:
        letter = self.letter or {}
        closing = [letter["closing"]] if letter.get("closing") else []
        return [*letter.get("paragraphs", []), *letter.get("todos", []), *closing]

    @property
    def unsupported(self) -> list[dict]:
        """Clinical claims with nothing behind them — what the clinician must look at."""
        return [b for b in self.blocks
                if not b.get("sources") and b.get("kind") != "courtesy"]

    def to_event(self, *, recipient: dict, hold_seconds: int = 20) -> dict:
        """The `draft_ready` event shape the UI already renders."""
        return {
            "at": 0.0, "type": "draft_ready", "drafted_by": self.drafted_by,
            "hold_seconds": hold_seconds, "recipient": recipient,
            "letter": self.letter, "summary": self.summary,
            "invented_citations": self.invented_citations,
        }


def transcript_prompt(said: list[dict], *, patient: str, clinician: str) -> str:
    """The transcript, numbered, plus who is who.

    Numbering happens here rather than being asked of the model, so an id always refers to
    exactly one line and a citation can be checked mechanically.
    """
    lines = [
        f"Consultation between {clinician} (the clinician) and {patient} (the patient).",
        "",
        "Transcript. Cite these ids and no others:",
    ]
    for turn in said:
        who = turn.get("who", "other")
        label = {"clinician": clinician, "patient": patient}.get(who, turn.get("name", "?"))
        lines.append(f"  [{turn['id']}] {label}: {turn['text']}")
    lines += [
        "",
        f"Write the clinical note and the letter to {patient}. Cite the ids above for "
        f"every claim, and leave `sources` empty for anything the transcript does not "
        f"support rather than inventing an id.",
    ]
    return "\n".join(lines)


def validate(payload: dict, known_ids: set[str]) -> Draft:
    """Turn a model response into a Draft, dropping citations that do not exist.

    A citation to an id the transcript does not contain is stripped rather than trusted.
    The claim then has no sources, so it is flagged — which is the correct outcome: the
    model asserted evidence it could not have had.
    """
    invented: list[str] = []

    def clean(block: dict) -> dict:
        cited = [s for s in block.get("sources", []) if isinstance(s, str)]
        real = [s for s in cited if s in known_ids]
        invented.extend(s for s in cited if s not in known_ids)
        return {**block, "sources": real}

    letter = dict(payload.get("letter") or {})
    for key in ("paragraphs", "todos"):
        letter[key] = [clean(b) for b in letter.get(key) or []]
    if letter.get("closing"):
        letter["closing"] = clean(letter["closing"])

    # Ids for the UI, assigned here so they are stable and the model cannot collide them.
    for index, block in enumerate(letter.get("paragraphs", [])):
        block["id"] = f"p{index + 1}"
    for index, block in enumerate(letter.get("todos", [])):
        block["id"] = f"t{index + 1}"
    if letter.get("closing"):
        letter["closing"]["id"] = "c1"

    return Draft(
        note=[clean(s) for s in payload.get("note") or []],
        letter=letter,
        summary=str(payload.get("summary") or ""),
        invented_citations=sorted(set(invented)),
    )


class Drafter(Protocol):
    def draft(self, *, said: list[dict], patient: str, clinician: str) -> Draft: ...


class ClaudeDrafter:
    """Real drafting via the Anthropic SDK, with a guaranteed response shape."""

    def __init__(self, *, model: str = MODEL, max_tokens: int = 16000,
                 effort: str = "high", client=None) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def draft(self, *, said: list[dict], patient: str, clinician: str) -> Draft:
        if not said:
            raise DraftError("nothing was said — there is no consultation to draft from")

        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
            messages=[{"role": "user", "content": transcript_prompt(
                said, patient=patient, clinician=clinician)}],
        ) as stream:
            message = stream.get_final_message()

        if message.stop_reason == "refusal":
            raise DraftError(f"the model declined to draft this: {message.stop_details}")
        if message.stop_reason == "max_tokens":
            raise DraftError(f"draft truncated at max_tokens={self.max_tokens}")

        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            raise DraftError("the model returned no text")

        draft = validate(json.loads(text), {t["id"] for t in said})
        draft.drafted_by = f"Cura · {self.model}"
        return draft
