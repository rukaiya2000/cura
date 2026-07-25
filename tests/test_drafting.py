"""Turning a consultation into a note and a letter.

The value of this step is one property, and it is what these tests are about: a claim is
either traceable to something that was said, or it is visibly flagged. There is no third
state, and in particular there is no state where a claim *looks* supported because the
model asserted a citation nobody checked.

No network: `ClaudeDrafter` takes an injected client.
"""

import json

import pytest

from skillforge.core.drafting import (
    RESPONSE_SCHEMA,
    ClaudeDrafter,
    Draft,
    DraftError,
    transcript_prompt,
    validate,
)

SAID = [
    {"id": "u1", "who": "clinician", "name": "Dr Rao",
     "text": "Morning Amara — how have things been since June?"},
    {"id": "u2", "who": "patient", "name": "Amara Okafor",
     "text": "The morning readings are higher, around 9 or 10 before breakfast."},
    {"id": "u3", "who": "clinician", "name": "Dr Rao",
     "text": "Any change to how you're taking the ramipril?"},
    {"id": "u4", "who": "patient", "name": "Amara Okafor", "text": "No, same as before."},
]
KNOWN = {t["id"] for t in SAID}


def block(text, sources, kind="clinical"):
    return {"text": text, "sources": sources, "kind": kind}


def payload(**over):
    base = {
        "note": [{"section": "History", "text": "Fasting glucose 9–10 mmol/L.",
                  "sources": ["u2"]}],
        "letter": {
            "subject": "Summary of your appointment",
            "greeting": "Dear Amara,",
            "paragraphs": [
                block("Thank you for coming in.", [], "courtesy"),
                block("Your morning readings have gone up.", ["u2"]),
            ],
            "todos": [block("Have your blood test.", ["u2"])],
            "closing": block("See you in six weeks.", ["u1"]),
            "sign_off": "Dr Rao",
        },
        "summary": "Fasting glucose risen.",
    }
    base.update(over)
    return base


# --- citations ---------------------------------------------------------------


def test_real_citations_survive():
    draft = validate(payload(), KNOWN)
    assert draft.letter["paragraphs"][1]["sources"] == ["u2"]
    assert draft.note[0]["sources"] == ["u2"]


def test_an_invented_citation_is_stripped_not_trusted():
    """A citation to a line that does not exist is worse than no citation, because it
    survives review by looking like evidence. Stripping it leaves the claim unsourced,
    which is the correct outcome — it gets flagged."""
    p = payload()
    p["letter"]["paragraphs"][1]["sources"] = ["u2", "u99"]
    draft = validate(p, KNOWN)

    assert draft.letter["paragraphs"][1]["sources"] == ["u2"]
    assert "u99" in draft.invented_citations


def test_a_claim_citing_only_invented_ids_becomes_unsupported():
    p = payload()
    p["letter"]["paragraphs"][1]["sources"] = ["u77"]
    draft = validate(p, KNOWN)

    assert draft.letter["paragraphs"][1]["sources"] == []
    assert draft.letter["paragraphs"][1] in draft.unsupported


def test_invented_citations_are_reported_rather_than_silently_dropped():
    """A model inventing evidence is worth knowing about. Once the claim is merely
    unflagged, that fact is invisible."""
    p = payload()
    p["letter"]["todos"][0]["sources"] = ["nope", "also-nope"]
    draft = validate(p, KNOWN)

    assert draft.invented_citations == ["also-nope", "nope"]


def test_a_courtesy_block_is_not_flagged():
    """Otherwise the warning fires on "thank you for coming in", and a warning that goes
    off on pleasantries is one a busy reader learns to scroll past."""
    draft = validate(payload(), KNOWN)
    courtesy = draft.letter["paragraphs"][0]

    assert courtesy["sources"] == []
    assert courtesy not in draft.unsupported


def test_an_unsupported_clinical_claim_is_flagged():
    p = payload()
    p["letter"]["paragraphs"].append(block("Your blood pressure was well controlled.", []))
    draft = validate(p, KNOWN)

    assert [b["text"] for b in draft.unsupported] == \
        ["Your blood pressure was well controlled."]


def test_block_ids_are_assigned_by_us_not_the_model():
    """The UI keys selection and editing on these. A model free to choose them could
    collide two blocks onto one id, and the reviewer would edit the wrong sentence."""
    draft = validate(payload(), KNOWN)

    assert [b["id"] for b in draft.letter["paragraphs"]] == ["p1", "p2"]
    assert [b["id"] for b in draft.letter["todos"]] == ["t1"]
    assert draft.letter["closing"]["id"] == "c1"


def test_a_malformed_response_does_not_explode():
    draft = validate({}, KNOWN)
    assert draft.blocks == []
    assert draft.unsupported == []


# --- the prompt --------------------------------------------------------------


def test_the_prompt_numbers_every_line():
    text = transcript_prompt(SAID, patient="Amara Okafor", clinician="Dr Rao")
    for turn in SAID:
        assert f"[{turn['id']}]" in text
        assert turn["text"] in text


def test_the_prompt_says_who_is_who():
    """Roles come from the binding. Without them the model has to guess which speaker is
    the patient, and a note that attributes the patient's symptoms to the doctor is
    worse than no note."""
    text = transcript_prompt(SAID, patient="Amara Okafor", clinician="Dr Rao")
    assert "Amara Okafor (the patient)" in text
    assert "Dr Rao (the clinician)" in text


def test_the_prompt_forbids_inventing_ids():
    text = transcript_prompt(SAID, patient="A", clinician="B")
    assert "inventing an id" in text


# --- the contract with the model ---------------------------------------------


def test_the_schema_is_closed():
    """An open schema lets the model add fields nothing renders, which then look like
    they were considered and rejected rather than never read."""
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    assert RESPONSE_SCHEMA["$defs"]["block"]["additionalProperties"] is False


def test_sources_are_required_on_every_block():
    """Optional citations become absent citations."""
    assert "sources" in RESPONSE_SCHEMA["$defs"]["block"]["required"]
    assert "sources" in RESPONSE_SCHEMA["properties"]["note"]["items"]["required"]


def test_the_system_prompt_separates_the_two_audiences():
    from skillforge.core.drafting import SYSTEM_PROMPT

    assert "clinical note" in SYSTEM_PROMPT.lower()
    assert "patient letter" in SYSTEM_PROMPT.lower()
    assert "do not diagnose" in SYSTEM_PROMPT.lower()


# --- the client --------------------------------------------------------------


class FakeMessage:
    def __init__(self, text, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.stop_details = {}
        self.content = [type("B", (), {"type": "text", "text": text})()]


class FakeClient:
    def __init__(self, message):
        self.message = message
        self.seen = {}

    class _Stream:
        def __init__(self, outer): self.outer = outer
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def get_final_message(self): return self.outer.message

    @property
    def messages(self):
        outer = self

        class M:
            @staticmethod
            def stream(**kwargs):
                outer.seen.update(kwargs)
                return FakeClient._Stream(outer)
        return M


def test_a_draft_comes_back_validated():
    client = FakeClient(FakeMessage(json.dumps(payload())))
    draft = ClaudeDrafter(client=client).draft(
        said=SAID, patient="Amara Okafor", clinician="Dr Rao")

    assert draft.summary == "Fasting glucose risen."
    assert draft.drafted_by.startswith("Cura ·")
    assert len(draft.unsupported) == 0


def test_an_empty_consultation_is_refused_before_the_call():
    """Drafting a letter from a call in which nothing was said would invent all of it."""
    with pytest.raises(DraftError, match="nothing was said"):
        ClaudeDrafter(client=FakeClient(FakeMessage("{}"))).draft(
            said=[], patient="A", clinician="B")


@pytest.mark.parametrize("reason, expected", [
    ("refusal", "declined"),
    ("max_tokens", "truncated"),
])
def test_refusal_and_truncation_are_surfaced(reason, expected):
    client = FakeClient(FakeMessage("{}", stop_reason=reason))
    with pytest.raises(DraftError, match=expected):
        ClaudeDrafter(client=client).draft(said=SAID, patient="A", clinician="B")


def test_the_request_asks_for_the_guaranteed_shape():
    client = FakeClient(FakeMessage(json.dumps(payload())))
    ClaudeDrafter(client=client).draft(said=SAID, patient="A", clinician="B")

    fmt = client.seen["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is RESPONSE_SCHEMA


def test_the_event_shape_is_what_the_screen_already_renders():
    draft = validate(payload(), KNOWN)
    event = draft.to_event(recipient={"name": "Amara Okafor",
                                      "email": "a@example.test",
                                      "verified_against": "PT-10001"})

    assert event["type"] == "draft_ready"
    assert event["recipient"]["email"] == "a@example.test"
    assert event["letter"]["paragraphs"][0]["id"] == "p1"
    assert event["hold_seconds"] > 0, "a send with no cancel window is not undoable"
