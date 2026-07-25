"""When the bot speaks, and — far more importantly — when it does not."""

import pytest

from skillforge.core.wake import addressed, reply_to


@pytest.mark.parametrize("line", [
    "Hi Cura, can you hear me?",
    "Cura, are you there?",
    "cura can you hear us",
    "Hey Cura — you there?",
])
def test_it_confirms_it_can_hear(line):
    assert reply_to(line) == "Yes, I can hear you."


def test_it_confirms_it_is_recording_and_what_that_means():
    reply = reply_to("Cura, are you recording this?")
    assert "capturing" in reply
    assert "until you approve" in reply, "the gate is the reassurance worth giving"


def test_it_can_say_what_it_is():
    assert "Cura" in reply_to("Cura, who are you?")


# --- silence, which is the default -------------------------------------------


@pytest.mark.parametrize("line", [
    "The morning readings are higher than they were.",
    "Any change to how you're taking the ramipril?",
    "No, same as before. Every morning with breakfast.",
    "I'll request an HbA1c and see you in six weeks.",
])
def test_an_ordinary_consultation_line_gets_no_reply(line):
    """Almost every line is between a doctor and a patient, and the bot has no business
    in it. Silence is the default, not a fallback."""
    assert reply_to(line) is None


def test_being_named_is_not_enough():
    """A line that names the bot but asks something outside the small set gets no reply.
    Answering "Cura, what was her HbA1c?" would mean volunteering clinical information
    into a live room on the strength of a regex."""
    assert reply_to("Cura, what was her HbA1c last time?") is None
    assert reply_to("Cura, should I increase the ramipril?") is None
    assert addressed("Cura, what was her HbA1c last time?") is True


def test_a_question_without_the_name_is_not_for_the_bot():
    """"Can you hear me?" is what a doctor says to a patient on a bad connection. Without
    the name it is not addressed to the bot, and answering would be an interruption."""
    assert reply_to("Can you hear me?") is None
    assert reply_to("Are you there?") is None


@pytest.mark.parametrize("line", [
    "The curator called about the exhibition.",
    "That's not an accurate reading.",
    "Obscura is the name of the practice.",
])
def test_the_name_matches_as_a_whole_word(line):
    """Otherwise "accurate" wakes it mid-consultation."""
    assert addressed(line) is False
    assert reply_to(line) is None


@pytest.mark.parametrize("junk", ["", "   ", None])
def test_junk_is_silent(junk):
    assert reply_to(junk) is None


def test_it_can_be_told_to_stop():
    reply = reply_to("Cura, stop recording please")
    assert "quiet" in reply
    assert "saved" in reply, "being told to stop must not imply the record was discarded"


def test_common_mishearings_of_the_name_still_work():
    """A transcriber's spelling of a spoken name is a guess. "Kura" and "Cora" are the
    guesses it actually makes."""
    assert reply_to("Kura, can you hear me?") is not None
    assert reply_to("Cora, are you there?") is not None


# --- reading the record back --------------------------------------------------

PATIENT = {
    "id": "PT-10001", "name": "Amara Okafor", "dob": "1979-03-14",
    "conditions": ["Type 2 diabetes", "Hypertension"],
    "medications": ["Metformin 1000 mg BD", "Ramipril 5 mg OD"],
    "allergies": ["Penicillin"],
    "last_seen": "6 weeks ago", "consultations": 14,
}


def ask(text, patient=PATIENT):
    from skillforge.core.wake import record_reply
    return record_reply(text, patient)


def test_it_reads_back_the_medications():
    reply = ask("Cura, what medication is she on?")
    assert "Metformin 1000 mg BD" in reply and "Ramipril 5 mg OD" in reply
    assert "according to the record" in reply, "it must say where this came from"


def test_it_reads_back_allergies_and_conditions():
    assert "Penicillin" in ask("Cura, any allergies?")
    assert "Type 2 diabetes" in ask("Cura, what's her history?")


def test_it_can_say_who_the_consultation_is_with():
    reply = ask("Cura, who is this patient?")
    assert "Amara Okafor" in reply and "PT-10001" in reply


def test_it_says_when_they_were_last_seen():
    assert "6 weeks ago" in ask("Cura, when did we last see her?")


def test_an_empty_field_says_so_rather_than_nothing():
    """Silence would read as "it did not hear me", and the doctor asks again."""
    reply = ask("Cura, any allergies?", {**PATIENT, "allergies": []})
    assert "Nothing is recorded" in reply


# --- the line it will not cross ----------------------------------------------


@pytest.mark.parametrize("line", [
    "Cura, should I increase the ramipril?",
    "Cura, is it safe to stop the metformin?",
    "Cura, what dose would you recommend?",
    "Cura, do you think that's the diabetes?",
    "Cura, should we start her on something else?",
])
def test_it_will_not_advise(line):
    """Reading a record back and giving clinical advice are different products with
    different risk profiles. This is the boundary, and it is enforced by explicit refusal
    rather than by hoping a model stays in its lane."""
    assert ask(line) is None


def test_advice_wins_over_retrieval_when_a_question_is_both():
    """"Should I increase her ramipril" matches the medication rule too. Advice is
    checked first, or the retrieval rule would answer it."""
    assert ask("Cura, should I increase her ramipril dose?") is None
    assert ask("Cura, what medication is she taking?") is not None


def test_it_cannot_be_asked_about_anybody_else():
    """The only record it holds is the one this consultation is bound to. There is no
    lookup to abuse — subject scoping, at the point of speech."""
    reply = ask("Cura, what medication is Mrs Patel on?")
    assert "Amara Okafor" in reply, "it answered about the bound patient, as it must"
    assert "Patel" not in reply


def test_no_record_means_no_answer():
    assert ask("Cura, what medication is she on?", None) is None


def test_it_still_ignores_the_room():
    assert ask("She's been taking them every morning with breakfast.") is None


# --- the record only changes when asked --------------------------------------


@pytest.mark.parametrize("line", [
    "Cura, update her record — she's on 10mg now.",
    "Cura, add that to her notes.",
    "Cura, put that on her record.",
    "Cura, record that she's stopped the metformin.",
])
def test_an_explicit_instruction_is_recognised(line):
    from skillforge.core.wake import update_requested
    assert update_requested(line) is True


@pytest.mark.parametrize("line", [
    "She's on 10mg now.",
    "I've been getting light-headed standing up.",
    "We'll increase the ramipril to 10mg.",
    "Cura, what medication is she on?",
    "Update the prescription when you get a chance.",
])
def test_nothing_else_changes_the_record(line):
    """A patient mentioning a new symptom is captured in the transcript and drafted into
    the note. It does not touch the record until somebody says so — a record that edits
    itself from overheard conversation is one no clinician trusts twice."""
    from skillforge.core.wake import update_requested
    assert update_requested(line) is False


def test_an_instruction_without_the_name_is_not_for_the_bot():
    """"Update her record" said to a receptionist is not an instruction to Cura."""
    from skillforge.core.wake import update_requested
    assert update_requested("Update her record with the new dose.") is False
