"""The bot in the meeting, and the words coming back.

The two payloads below are copied **verbatim** from MeetStream's documentation rather than
invented, because the whole risk in this adapter is that our idea of the payload differs
from the real one. A fixture I made up would agree with a parser I made up and prove
nothing. When a real payload is captured, add it here alongside these.

No network anywhere: `MeetStream` takes an injected transport.
"""

import json

import pytest

from skillforge.adapters.meetstream import (
    Binding,
    MeetStream,
    MeetStreamError,
    normalize_lifecycle,
    normalize_transcript,
    verify_signature,
)

BINDING = Binding(
    consultation_id="con-0912",
    patient_id="PT-10482",
    patient_name="Amara Okafor",
    clinician="priya.rao@clinic.test",
    clinician_name="Dr Priya Rao",
    crm_id="hs-contact-88412",
)

#: From docs.meetstream.ai — live transcription webhook, word-level, mid-utterance.
DOC_INTERIM = {
    "bot_id": "8ceabf49-d392-4c04-8e91-bd9601a0df6e",
    "speakerName": "Madan Raj",
    "timestamp": "2026-01-24T17:00:30.354452",
    "new_text": "hear",
    "transcript": "hear",
    "utterance": "",
    "words": [{"word": "hear", "start": 2, "end": 2.08, "confidence": 0.999955,
               "speaker": "Madan Raj", "punctuated_word": "hear",
               "speech_confidence": 0.999955, "word_is_final": False}],
    "end_of_turn": False,
    "turn_is_formatted": False,
    "transcription_mode": "word_level",
    "custom_attributes": {},
}

#: From docs.meetstream.ai — the lifecycle base structure.
DOC_LIFECYCLE = {
    "bot_event": "bot.inmeeting",
    "bot_id": "8ceabf49-d392-4c04-8e91-bd9601a0df6e",
    "bot_status": "in_meeting",
    "message": "Bot joined the meeting",
    "status_code": 200,
    "timestamp": "2026-07-25T09:20:04.100000",
    "custom_attributes": {},
}


def spoken(text, *, speaker="Amara Okafor", final=True, binding=BINDING, **over):
    payload = {**DOC_INTERIM,
               "speakerName": speaker,
               "transcript": text,
               "new_text": (text.split() or [""])[-1],
               "end_of_turn": final,
               "custom_attributes": binding.to_attributes() if binding else {}}
    payload.update(over)
    return payload


# --- the binding, which is the whole point ----------------------------------


def test_the_binding_survives_a_round_trip_through_custom_attributes():
    """MeetStream echoes `custom_attributes` on every event, so a line of speech arrives
    already carrying the patient it belongs to. That is what removes identity resolution
    from the critical path."""
    assert Binding.from_attributes(BINDING.to_attributes()) == BINDING


def test_a_binding_is_namespaced_so_it_cannot_collide():
    attrs = BINDING.to_attributes()
    assert list(attrs) == ["cura_consultation"]
    merged = {**attrs, "someone_elses_key": {"patient_id": "PT-99999"}}
    assert Binding.from_attributes(merged).patient_id == "PT-10482"


@pytest.mark.parametrize("attrs", [None, {}, {"cura_consultation": "not-a-dict"},
                                   {"cura_consultation": {"patient_id": "PT-1"}},
                                   {"other": {"x": 1}}])
def test_an_unrecognised_binding_is_none_not_an_error(attrs):
    """A webhook with no binding is not a fault — it is a bot somebody else created.
    The caller decides what to do; this layer does not raise about it."""
    assert Binding.from_attributes(attrs) is None


# --- transcript normalisation -----------------------------------------------


def test_the_documented_interim_payload_is_ignored():
    """Word-level events stream while somebody is still talking. Acting on them means
    firing on half a sentence — "book her a follow" — and by the time the rest arrives
    the action has already been taken."""
    assert normalize_transcript(DOC_INTERIM) is None


def test_a_completed_turn_becomes_an_utterance():
    u = normalize_transcript(spoken("The morning readings are higher than they were."))

    assert u.text == "The morning readings are higher than they were."
    assert u.speaker == "Amara Okafor"
    assert u.final is True
    assert u.binding.patient_id == "PT-10482"


def test_a_final_turn_uses_the_whole_transcript_not_the_last_fragment():
    """`new_text` carries only the latest word. Using it on a final event silently
    truncates every utterance to one word, and the transcript still *looks* populated."""
    payload = spoken("I have been getting light-headed standing up")
    payload["new_text"] = "up"

    assert normalize_transcript(payload).text.startswith("I have been getting")


def test_an_empty_turn_is_dropped():
    assert normalize_transcript(spoken("")) is None
    assert normalize_transcript(spoken("   ")) is None


@pytest.mark.parametrize("junk", [None, "", [], 0, {"no": "event"}])
def test_junk_does_not_raise(junk):
    """This runs on an unauthenticated-until-verified public endpoint. It must return
    nothing on rubbish rather than throw."""
    assert normalize_transcript(junk) is None


# --- who said it -------------------------------------------------------------


def test_the_clinician_is_recognised_from_the_binding():
    u = normalize_transcript(spoken("How have things been since June?",
                                    speaker="Dr Priya Rao"))
    assert u.role == "clinician"


def test_the_patient_is_recognised_from_the_binding():
    assert normalize_transcript(spoken("Mostly fine.")).role == "patient"


def test_an_unrecognised_speaker_is_never_assumed_to_be_the_patient():
    """A receptionist, interpreter or family member must not have their words filed as
    the patient's. In a clinical record that distinction is not cosmetic."""
    u = normalize_transcript(spoken("She's been taking them every morning.",
                                    speaker="Joseph Okafor"))
    assert u.role == "other"


def test_names_match_case_and_whitespace_insensitively():
    u = normalize_transcript(spoken("Right.", speaker="  dr priya rao "))
    assert u.role == "clinician"


def test_role_is_other_when_nothing_is_bound():
    """No binding means no way to know who anyone is — and guessing is the failure this
    design exists to avoid."""
    u = normalize_transcript(spoken("Anything at all.", binding=None))
    assert u.role == "other"
    assert u.binding is None


# --- lifecycle ---------------------------------------------------------------


def test_the_documented_lifecycle_payload_parses():
    e = normalize_lifecycle(DOC_LIFECYCLE)
    assert e.event == "bot.inmeeting"
    assert e.live is True
    assert e.ended is False


@pytest.mark.parametrize("event, ended, refused", [
    ("bot.stopped", True, False),
    ("bot.done", True, False),
    ("bot.kicked", True, True),
    ("bot.denied", True, True),
    ("bot.notallowed", True, True),
    ("bot.failed", True, True),
    ("bot.joining", False, False),
    ("bot.in_waiting_room", False, False),
])
def test_terminal_events_are_told_apart_from_refusals(event, ended, refused):
    """"The consultation ended" and "the host never let us in" need different words to
    the doctor, so they cannot both just be `ended`."""
    e = normalize_lifecycle({**DOC_LIFECYCLE, "bot_event": event})
    assert e.ended is ended
    assert e.refused is refused


def test_a_lifecycle_event_carries_the_binding_too():
    e = normalize_lifecycle({**DOC_LIFECYCLE,
                             "custom_attributes": BINDING.to_attributes()})
    assert e.binding.consultation_id == "con-0912"


@pytest.mark.parametrize("junk", [None, "", {}, {"bot_id": "x"}])
def test_lifecycle_junk_does_not_raise(junk):
    assert normalize_lifecycle(junk) is None


# --- webhook authenticity ----------------------------------------------------


def test_a_correct_signature_verifies():
    body = json.dumps(DOC_LIFECYCLE).encode()
    import hashlib
    import hmac
    digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    assert verify_signature("s3cret", body, f"sha256={digest}")
    assert verify_signature("s3cret", body, digest), "bare digest should also verify"


def test_a_tampered_body_fails():
    body = json.dumps(DOC_LIFECYCLE).encode()
    import hashlib
    import hmac
    digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    assert not verify_signature("s3cret", body + b" ", f"sha256={digest}")


def test_an_unconfigured_secret_rejects_rather_than_accepts():
    """An endpoint that accepts everything when unconfigured is worse than one that
    accepts nothing: the failure is silent, and it injects lines into patient records."""
    assert not verify_signature("", b"{}", "sha256=whatever")
    assert not verify_signature("s3cret", b"{}", None)


# --- sending the bot ---------------------------------------------------------


def client_recording():
    sent = {}

    def transport(method, path, body):
        sent.update(method=method, path=path, body=body)
        return {"bot_id": "bot-1", "status": "scheduled"}

    return MeetStream(api_key="k", _post=transport), sent


def test_the_bot_carries_its_patient_into_the_meeting():
    ms, sent = client_recording()
    ms.send_bot(meeting_link="https://meet.google.com/abc-defg-hij", binding=BINDING,
                transcript_webhook="https://x.test/hooks/transcript")

    assert sent["path"] == "/bots/create_bot"
    assert Binding.from_attributes(sent["body"]["custom_attributes"]) == BINDING
    assert sent["body"]["live_transcription_required"]["webhook_url"] \
        == "https://x.test/hooks/transcript"


def test_the_bot_does_not_record_video_by_default():
    """A consultation needs the words. Not recording faces is the cheapest possible
    reduction in what has to be protected."""
    ms, sent = client_recording()
    ms.send_bot(meeting_link="https://zoom.us/j/123", binding=BINDING,
                transcript_webhook="https://x.test/h")
    assert sent["body"]["video_required"] is False


def test_a_bot_can_be_scheduled_for_when_the_appointment_starts():
    """So it is booked when the invite is sent, not by someone remembering at 09:20."""
    ms, sent = client_recording()
    ms.send_bot(meeting_link="https://meet.google.com/x", binding=BINDING,
                transcript_webhook="https://x.test/h", join_at="2026-09-05T09:20:00Z")
    assert sent["body"]["join_at"] == "2026-09-05T09:20:00Z"


def test_one_link_field_covers_every_platform():
    """Meet, Zoom and Teams all arrive as `meeting_link`; nothing branches on platform."""
    for link in ("https://meet.google.com/abc-defg-hij",
                 "https://zoom.us/j/98765", "https://teams.microsoft.com/l/meetup-join/x"):
        ms, sent = client_recording()
        ms.send_bot(meeting_link=link, binding=BINDING, transcript_webhook="https://x/h")
        assert sent["body"]["meeting_link"] == link


def test_a_missing_key_is_said_plainly(monkeypatch):
    """The env is cleared explicitly: `MeetStream(api_key="")` falls back to
    MEETSTREAM_API_KEY, and any earlier test that called `load_env()` leaves the real key
    in os.environ for the rest of the session. Without this the test passes or fails
    depending on which other tests ran first."""
    monkeypatch.delenv("MEETSTREAM_API_KEY", raising=False)

    with pytest.raises(MeetStreamError, match="MEETSTREAM_API_KEY"):
        MeetStream(api_key="").send_bot(meeting_link="https://meet.google.com/x",
                                        binding=BINDING, transcript_webhook="https://x/h")


def test_a_missing_meeting_link_is_refused_before_the_call():
    with pytest.raises(MeetStreamError, match="meeting link"):
        MeetStream(api_key="k", _post=lambda *a: {}).send_bot(
            meeting_link="", binding=BINDING, transcript_webhook="https://x/h")


# --- the property the whole design rests on ---------------------------------


def test_a_whole_consultation_arrives_already_bound_to_its_patient():
    """End to end on the shape that matters: every line of a scripted consultation comes
    back attributed and bound, with no step anywhere that infers who anyone is."""
    lines = [
        ("Dr Priya Rao", "Morning Amara — how have things been since June?"),
        ("Amara Okafor", "The morning readings are higher, around 9 or 10."),
        ("Dr Priya Rao", "Any change to how you're taking the ramipril?"),
        ("Amara Okafor", "No, same as before."),
    ]
    utterances = []
    for speaker, text in lines:
        utterances.append(normalize_transcript(spoken(text, speaker=speaker)))
        # interleaved interim events, which must contribute nothing
        assert normalize_transcript(spoken(text[:6], speaker=speaker, final=False)) is None

    assert len(utterances) == 4
    assert [u.role for u in utterances] == ["clinician", "patient", "clinician", "patient"]
    assert {u.binding.patient_id for u in utterances} == {"PT-10482"}
    assert all(u.binding.crm_id == "hs-contact-88412" for u in utterances)


# --- transcription provider --------------------------------------------------


def test_a_transcription_provider_is_always_sent():
    """A webhook URL on its own is refused: "webhook_url is provided but no streaming
    provider found". Something has to actually do the transcription, and MeetStream will
    not pick one for you — observed as a 400 on the first real dispatch."""
    ms, sent = client_recording()
    ms.send_bot(meeting_link="https://meet.google.com/x", binding=BINDING,
                transcript_webhook="https://x.test/h")

    provider = sent["body"]["recording_config"]["transcript"]["provider"]
    assert provider, "no streaming provider in the payload"
    # A *documented* provider. `meetstream_streaming` validates and then transcribes
    # nothing — the bot joined, recording started, and no webhook ever fired.
    assert "deepgram_streaming" in provider
    assert provider["deepgram_streaming"], "the provider needs its own config, not {}"


def test_the_provider_is_configurable():
    ms = MeetStream(api_key="k", provider="deepgram_streaming",
                    _post=lambda m, p, b: b)
    body = ms.send_bot(meeting_link="https://meet.google.com/x", binding=BINDING,
                       transcript_webhook="https://x.test/h")
    assert "deepgram_streaming" in body["recording_config"]["transcript"]["provider"]


def test_an_unknown_provider_is_refused_before_any_call():
    """MeetStream's own error lists the valid names, so a typo should be caught here
    rather than costing a round trip and a confusing 400."""
    with pytest.raises(MeetStreamError, match="unknown transcription provider"):
        MeetStream(api_key="k", provider="whisper_but_made_up")


def test_each_provider_carries_the_config_it_needs():
    """An empty provider object is accepted and then behaves unpredictably. Deepgram wants
    a model; AssemblyAI wants a sample rate."""
    from skillforge.adapters.meetstream import PROVIDER_CONFIG

    assert PROVIDER_CONFIG["deepgram_streaming"]["model"] == "nova-2"
    assert PROVIDER_CONFIG["assemblyai_streaming"]["sample_rate"] == 16000
    # meeting_captions genuinely takes none — it reads the platform's own captions.
    assert PROVIDER_CONFIG["meeting_captions"] == {}


def test_the_default_is_a_documented_provider():
    """Only deepgram and assemblyai are in MeetStream's docs. The rest appear in a
    validation error listing valid names, which is not the same as being wired up."""
    from skillforge.adapters.meetstream import DEFAULT_PROVIDER

    assert DEFAULT_PROVIDER in ("deepgram_streaming", "assemblyai_streaming")


def test_the_dry_run_payload_is_the_one_that_gets_sent():
    """A hand-built copy for display drifts from the real body — the copy in send_bot.py
    had lost `recording_config`, which was the field under investigation at the time."""
    ms, sent = client_recording()
    shown = ms.bot_payload(meeting_link="https://meet.google.com/x", binding=BINDING,
                           transcript_webhook="https://x.test/h")
    ms.send_bot(meeting_link="https://meet.google.com/x", binding=BINDING,
                transcript_webhook="https://x.test/h")

    assert shown == sent["body"]
    assert "recording_config" in shown


# --- pulling, because the webhook never fires --------------------------------


def test_a_pulled_line_needs_no_end_of_turn():
    """A pulled transcript is already settled — there is no partial turn to wait for, and
    requiring `end_of_turn` would discard every line."""
    from skillforge.adapters.meetstream import normalize_pulled

    u = normalize_pulled({"speakerName": "Amara Okafor",
                          "transcript": "The morning readings are higher.",
                          "timestamp": "2026-07-25T09:20:30"}, BINDING)
    assert u.text == "The morning readings are higher."
    assert u.role == "patient"
    assert u.binding.patient_id == "PT-10482"


def test_the_binding_comes_from_the_caller_not_the_entry():
    """A pulled entry carries no custom_attributes — the bot it was fetched for is what
    identifies the consultation."""
    from skillforge.adapters.meetstream import normalize_pulled

    u = normalize_pulled({"text": "Anything.", "speaker": "Dr Priya Rao"}, BINDING)
    assert u.binding.consultation_id == "con-0912"
    assert u.role == "clinician"


@pytest.mark.parametrize("entry, expected", [
    ({"transcript": "one"}, "one"),
    ({"text": "two"}, "two"),
    ({"sentence": "three"}, "three"),
    ({"words": [{"punctuated_word": "four,"}, {"word": "five"}]}, "four, five"),
])
def test_the_text_is_found_whichever_field_carries_it(entry, expected):
    """The pull endpoint has only ever returned []. These field names come from the
    documented webhook payload and the shapes around it — unverified, and isolated here
    so a wrong guess is one function."""
    from skillforge.adapters.meetstream import normalize_pulled

    assert normalize_pulled(entry, BINDING).text == expected


@pytest.mark.parametrize("raw, count", [
    ([{"text": "a"}, {"text": "b"}], 2),
    ({"transcript": [{"text": "a"}]}, 1),
    ({"data": {"utterances": [{"text": "a"}, {"text": "b"}]}}, 2),
    ({}, 0),
    ([], 0),
    (None, 0),
])
def test_envelopes_are_unwrapped(raw, count):
    from skillforge.adapters.meetstream import pulled_entries

    assert len(pulled_entries(raw)) == count


@pytest.mark.parametrize("junk", [None, "", 0, [], {"nothing": "useful"}])
def test_an_unusable_entry_is_dropped(junk):
    from skillforge.adapters.meetstream import normalize_pulled

    assert normalize_pulled(junk, BINDING) is None


#: Captured verbatim from a live Google Meet on 2026-07-25. Not invented, not adapted
#: from the docs — the pull endpoint's actual output. A fixture I wrote myself would have
#: agreed with a parser I wrote myself and proved nothing, which is exactly what happened:
#: the parser built from the documented webhook shape dropped every real line.
LIVE_PULLED = {
    "participant": {
        "id": 100, "name": "Rukaiya Khan",
        "extra_data": {"google_meet": {
            "static_participant_id": "spaces/NTwZoOC1y4YB/devices/232"}},
    },
    "words": [{
        "text": "What?",
        "start_timestamp": {"relative": 0.0, "absolute": "2026-07-25T23:52:49.499000Z"},
        "end_timestamp": {"relative": 5.113, "absolute": "2026-07-25T23:52:54.612000Z"},
    }],
}


def test_the_real_pulled_shape_parses():
    from skillforge.adapters.meetstream import normalize_pulled

    u = normalize_pulled(LIVE_PULLED, BINDING)
    assert u is not None, "the real shape was dropped entirely"
    assert u.text == "What?"
    assert u.speaker == "Rukaiya Khan"
    assert u.at == "2026-07-25T23:52:49.499000Z"


def test_the_speaker_comes_from_the_participant_object():
    """`participant` is an object, not a `speakerName` string. Reading it as a string
    left every line attributed to "Unknown"."""
    from skillforge.adapters.meetstream import normalize_pulled

    assert normalize_pulled(LIVE_PULLED, BINDING).speaker == "Rukaiya Khan"


def test_multiple_words_join_into_a_sentence():
    from skillforge.adapters.meetstream import normalize_pulled

    entry = {**LIVE_PULLED, "words": [
        {"text": "The"}, {"text": "morning"}, {"text": "readings"}, {"text": "are"},
        {"text": "higher."}]}
    assert normalize_pulled(entry, BINDING).text == "The morning readings are higher."


def test_the_word_key_is_text_not_word():
    """MeetStream's webhook payload uses `word`; the pull endpoint uses `text`. Handling
    only the documented one produced empty lines that were then silently dropped."""
    from skillforge.adapters.meetstream import normalize_pulled

    assert normalize_pulled({"participant": {"name": "A"},
                             "words": [{"text": "yes"}]}, BINDING).text == "yes"
