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
