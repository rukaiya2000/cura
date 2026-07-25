"""MeetStream — putting the bot in the call, and getting the words back out.

Google Meet, Zoom and Microsoft Teams, from one `meeting_link`; the platform is inferred
from the URL, so nothing here branches on it.

**The keystone: the patient binding rides in `custom_attributes`.** MeetStream echoes that
object back on *every* transcript event and *every* lifecycle event. So a line of speech
does not arrive as anonymous text that something later has to attribute — it arrives
already carrying the consultation and patient it belongs to, fixed when the invite was
sent. That is what removes identity resolution from the critical path entirely, and it is
why `Binding` is a real type rather than a loose dict.

Two payload shapes are parsed, both read off MeetStream's docs rather than guessed:

  * live transcription  →  `live_transcription_required.webhook_url`
  * bot lifecycle       →  `callback_url`

Everything platform-shaped is confined to `normalize_transcript` and
`normalize_lifecycle`. No MeetStream field name appears anywhere else in this codebase, so
if the real payload differs from the documented one, the fix is one function and its test.

The API itself has been exercised: the key authenticates, and a dispatch was refused with
"webhook_url is provided but no streaming provider found", which is why `recording_config`
is now sent. The *webhook payloads* remain unverified — the shapes are documented and the
normalisers are tested against the documented examples verbatim. When a real payload
arrives, capture it as a fixture rather than adjusting code to match a memory of it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

API_ROOT = "https://api.meetstream.ai/api/v1"

#: Transcription backends MeetStream accepts. `meetstream_streaming` is their own, so it
#: is the only one that needs no second account; the rest want their own API key.
PROVIDERS = ("meetstream_streaming", "deepgram_streaming", "assemblyai_streaming",
             "jigsawstack_streaming", "sarvam_streaming", "meeting_captions")
DEFAULT_PROVIDER = "meetstream_streaming"

#: Where our binding lives inside `custom_attributes`. Namespaced so it cannot collide
#: with anything MeetStream or another integration puts alongside it.
BINDING_KEY = "cura_consultation"

#: Lifecycle events that mean the bot is in the room and capturing.
LIVE_EVENTS = frozenset({"bot.inmeeting", "bot.recording"})

#: Terminal events. Exactly one arrives per bot, so a consultation closes on any of them.
ENDED_EVENTS = frozenset({"bot.stopped", "bot.done", "bot.kicked", "bot.denied",
                          "bot.notallowed", "bot.failed"})

#: Terminal events that mean the bot never got to work. Worth distinguishing from a clean
#: finish: "the consultation ended" and "the host never let us in" need different words.
REFUSED_EVENTS = frozenset({"bot.kicked", "bot.denied", "bot.notallowed", "bot.failed"})


class MeetStreamError(Exception):
    """The API refused, or answered with something unusable."""


@dataclass(frozen=True)
class Binding:
    """Which consultation, and which patient, a bot is for.

    Established when the clinician sends the invite — before the meeting exists — and
    carried by MeetStream from that moment on. Nothing infers it from the call.
    """

    consultation_id: str
    patient_id: str
    patient_name: str
    clinician: str                      # the Scalekit identifier the bot acts under
    clinician_name: str = ""
    crm_id: str | None = None

    def to_attributes(self) -> dict:
        return {BINDING_KEY: {
            "consultation_id": self.consultation_id,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "clinician": self.clinician,
            "clinician_name": self.clinician_name,
            "crm_id": self.crm_id,
        }}

    @classmethod
    def from_attributes(cls, attributes: dict | None) -> Binding | None:
        """Rebuild a binding from what came back, or None if it isn't ours.

        Returns None rather than raising: a webhook carrying no binding is not an error,
        it is a bot somebody else created. The caller decides what to do about that.
        """
        if not isinstance(attributes, dict):
            return None
        raw = attributes.get(BINDING_KEY)
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                consultation_id=str(raw["consultation_id"]),
                patient_id=str(raw["patient_id"]),
                patient_name=str(raw.get("patient_name", "")),
                clinician=str(raw["clinician"]),
                clinician_name=str(raw.get("clinician_name", "")),
                crm_id=raw.get("crm_id"),
            )
        except (KeyError, TypeError):
            return None


@dataclass
class Utterance:
    """One complete thing somebody said, bound to a patient."""

    text: str
    speaker: str                        # as the meeting platform labelled them
    role: str                           # "clinician" | "patient" | "other"
    at: str                             # ISO timestamp from MeetStream
    bot_id: str = ""
    binding: Binding | None = None
    final: bool = True

    @property
    def seconds(self) -> float | None:
        """Wall-clock seconds since the epoch, for ordering. None if unparseable."""
        try:
            return datetime.fromisoformat(self.at.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return None


@dataclass
class Lifecycle:
    """Something happened to the bot itself."""

    event: str
    bot_id: str
    message: str = ""
    status_code: int = 200
    at: str = ""
    binding: Binding | None = None

    @property
    def live(self) -> bool:
        return self.event in LIVE_EVENTS

    @property
    def ended(self) -> bool:
        return self.event in ENDED_EVENTS

    @property
    def refused(self) -> bool:
        """The bot never got to work — a host denied it, or it failed."""
        return self.event in REFUSED_EVENTS


# --- the two normalisers ----------------------------------------------------


def normalize_transcript(payload: dict) -> Utterance | None:
    """A live-transcription webhook → one `Utterance`, or None to ignore it.

    **Only complete turns get through.** MeetStream streams word-level events with
    `word_is_final: false` while somebody is still speaking, and `end_of_turn` marks the
    phrase boundary. Acting on interim text would mean the router firing on half a
    sentence — "book her a follow" — which is both wrong and unfixable downstream, because
    by the time the rest arrives the action has already been taken.

    Returning None rather than an incomplete Utterance keeps that decision here, where the
    field names are, instead of leaking `end_of_turn` into the router.
    """
    if not isinstance(payload, dict):
        return None
    if not payload.get("end_of_turn"):
        return None

    # `transcript` is the accumulated turn; `new_text` is only the latest fragment. Using
    # new_text on a final event would silently truncate every utterance to its last word.
    text = (payload.get("transcript") or payload.get("utterance")
            or payload.get("new_text") or "").strip()
    if not text:
        return None

    binding = Binding.from_attributes(payload.get("custom_attributes"))
    speaker = str(payload.get("speakerName") or payload.get("speaker") or "").strip()

    return Utterance(
        text=text,
        speaker=speaker or "Unknown",
        role=_role(speaker, binding),
        at=str(payload.get("timestamp") or ""),
        bot_id=str(payload.get("bot_id") or ""),
        binding=binding,
        final=True,
    )


def normalize_lifecycle(payload: dict) -> Lifecycle | None:
    """A callback webhook → one `Lifecycle`, or None if it isn't one."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("bot_event")
    if not event:
        return None
    return Lifecycle(
        event=str(event),
        bot_id=str(payload.get("bot_id") or ""),
        message=str(payload.get("message") or ""),
        status_code=int(payload.get("status_code") or 200),
        at=str(payload.get("timestamp") or ""),
        binding=Binding.from_attributes(payload.get("custom_attributes")),
    )


def _role(speaker: str, binding: Binding | None) -> str:
    """Clinician, patient, or neither — from the *binding*, never from the voice.

    The meeting platform gives real display names rather than "Speaker 2", so this is a
    string comparison against who we already know is in the room, not recognition. Anyone
    unrecognised is `other` rather than being assumed to be the patient: a receptionist,
    an interpreter or a family member joining must not have their words filed as the
    patient's, and in a clinical record that distinction is not cosmetic.
    """
    if not binding or not speaker:
        return "other"
    name = speaker.strip().casefold()
    if binding.clinician_name and name == binding.clinician_name.strip().casefold():
        return "clinician"
    if binding.patient_name and name == binding.patient_name.strip().casefold():
        return "patient"
    return "other"


# --- webhook authenticity ---------------------------------------------------


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """HMAC-SHA256 over the raw body, compared in constant time.

    The *raw* bytes, not a re-serialised dict: `json.dumps(json.loads(body))` reorders keys
    and changes whitespace, so a re-encoded body fails a signature that was valid.

    With no secret configured this returns False rather than True. An endpoint that accepts
    everything when unconfigured is worse than one that accepts nothing, because the
    failure is silent and the endpoint injects transcript lines into patient records.
    """
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip().removeprefix("sha256="))


# --- the client -------------------------------------------------------------


@dataclass
class MeetStream:
    """The bits of the MeetStream API this product uses."""

    api_key: str = ""
    root: str = API_ROOT
    provider: str = ""
    _post: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("MEETSTREAM_API_KEY", "")
        self.provider = (self.provider
                         or os.environ.get("MEETSTREAM_TRANSCRIPT_PROVIDER")
                         or DEFAULT_PROVIDER)
        if self.provider not in PROVIDERS:
            raise MeetStreamError(
                f"unknown transcription provider {self.provider!r} — "
                f"one of: {', '.join(PROVIDERS)}")

    def send_bot(self, *, meeting_link: str, binding: Binding,
                 transcript_webhook: str, callback_url: str | None = None,
                 bot_name: str = "Cura", join_at: str | None = None,
                 video: bool = False) -> dict:
        """Put a bot in a meeting, carrying its patient with it.

        `join_at` schedules the bot for when the appointment actually starts, so this is
        called once at scheduling time rather than by someone remembering to press a
        button at 09:20.

        `video: False` by default. A consultation needs the words, not the doctor's face,
        and not recording video is the cheapest reduction in what has to be protected.
        """
        if not self.api_key:
            raise MeetStreamError("MEETSTREAM_API_KEY is not set")
        if not meeting_link:
            raise MeetStreamError("a bot needs a meeting link")

        body: dict[str, Any] = {
            "meeting_link": meeting_link,
            "bot_name": bot_name,
            "video_required": video,
            # Echoed back on every event, which is the whole mechanism.
            "custom_attributes": binding.to_attributes(),
            "live_transcription_required": {"webhook_url": transcript_webhook},
            # A webhook URL alone is refused: "webhook_url is provided but no streaming
            # provider found". Something has to actually do the transcription, and
            # MeetStream will not choose for you.
            #
            # `meetstream_streaming` is theirs, so it needs no third-party account. The
            # alternatives (deepgram_streaming, assemblyai_streaming, jigsawstack_streaming,
            # sarvam_streaming) each want their own key, and `meeting_captions` reads the
            # platform's own captions — cheapest, but only as good as the platform's, and
            # unavailable when a host has captions switched off.
            "recording_config": {
                "transcript": {"provider": {self.provider: {}}},
            },
        }
        if callback_url:
            body["callback_url"] = callback_url
        if join_at:
            body["join_at"] = join_at

        return self._request("POST", "/bots/create_bot", body)

    def stop_bot(self, bot_id: str) -> dict:
        return self._request("POST", f"/bots/{bot_id}/leave", {})

    def transcript(self, bot_id: str) -> dict:
        """The post-call transcript, for when live delivery missed something."""
        return self._request("GET", f"/bots/{bot_id}/get_transcript", None)

    # --- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None) -> dict:
        if self._post is not None:            # injected transport, for tests
            return self._post(method, path, body)

        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.root}{path}", data=data, method=method,
            headers={
                # Their scheme is `Token <key>`, not `Bearer`. Sending Bearer gets a 401
                # that reads like a bad key.
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode() or "{}"
        except urllib.error.HTTPError as e:
            detail = (e.read().decode() or "")[:400]
            raise MeetStreamError(f"{e.code} from {path}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise MeetStreamError(f"{type(e).__name__} calling {path}: {e}") from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise MeetStreamError(f"{path} returned non-JSON: {raw[:200]}") from e
