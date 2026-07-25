"""The practice data and consultation vocabulary the clinician's UI renders.

The same arrangement that worked for the agent core: one shared vocabulary, so the screen
never invents its own idea of what happened. The core does not emit these yet — this is a
scripted instance of the shape it will emit — but defining the shape first means the UI is
built against the real contract rather than a mock that later has to be unpicked.

Three things here carry the product's argument:

`binding` on an appointment happens **when the invite is sent**, before the meeting exists.
Patient identity comes from the calendar invite the clinician issued, never from
recognising a voice — which is why there is no confidence tier anywhere in this file and no
out-of-band check of who is speaking.

`action_refused` with `reason: "wrong_subject"` is the governance beat. The clinician has
permission to write to the other record; what they lack is a consultation about that
patient. Permission is not relevance.

`observed` on a written action is what the record said *after* the write, read back rather
than assumed.

All patient data is synthetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# --- consultation event vocabulary ------------------------------------------

PATIENT_BOUND = "patient_bound"
CONTEXT_FETCHED = "context_fetched"
BOT_JOINED = "bot_joined"
SAID = "said"
NOTE = "note"
ACTION_PROPOSED = "action_proposed"
ACTION_WRITTEN = "action_written"
ACTION_REFUSED = "action_refused"
ACCESSED = "accessed"
CONSULTATION_ENDED = "consultation_ended"
DRAFT_READY = "draft_ready"

CLINICIAN = {"name": "Dr Priya Rao", "role": "General practice", "initials": "PR"}
SURGERY = {"name": "Kirkstall Lane Surgery", "phone": "0113 496 0112"}
TODAY = "Friday 25 July 2026"


@dataclass
class ConsultationLog:
    events: list[dict] = field(default_factory=list)

    def emit(self, at: float, type: str, **payload) -> dict:
        event = {"at": round(at, 2), "type": type, **payload}
        self.events.append(event)
        return event


# --- the practice ------------------------------------------------------------

PATIENTS = [
    {
        "id": "PT-10482", "name": "Amara Okafor", "dob": "1979-03-14", "age": 47,
        "email": "amara.okafor@example.test",
        "nhs": "485 777 3456", "crm_id": "hs-contact-88412",
        "last_seen": "6 weeks ago", "conditions": ["Type 2 diabetes", "Hypertension"],
        "flag": {"text": "HbA1c overdue", "urgency": "attention"},
        "consultations": 14,
    },
    {
        "id": "PT-10920", "name": "Nia Patel", "dob": "1991-11-02", "age": 34,
        "email": "n.patel@example.test",
        "nhs": "612 004 8871", "crm_id": "hs-contact-90233",
        "last_seen": "3 days ago", "conditions": ["Asthma"],
        "flag": {"text": "Called yesterday", "urgency": "info"},
        "consultations": 6,
    },
    {
        "id": "PT-10771", "name": "Tomas Lindqvist", "dob": "1956-06-30", "age": 69,
        "email": "t.lindqvist@example.test",
        "nhs": "223 991 0056", "crm_id": "hs-contact-89044",
        "last_seen": "2 weeks ago", "conditions": ["Atrial fibrillation", "CKD stage 3"],
        "flag": {"text": "INR check due", "urgency": "attention"},
        "consultations": 31,
    },
    {
        "id": "PT-11033", "name": "Grace Mensah", "dob": "2001-01-19", "age": 24,
        "email": "grace.mensah@example.test",
        "nhs": "770 118 2245", "crm_id": None,
        "last_seen": None, "conditions": [],
        "flag": {"text": "New patient — no record", "urgency": "new"},
        "consultations": 0,
    },
    {
        "id": "PT-10218", "name": "Yusuf Demir", "dob": "1984-09-08", "age": 41,
        "email": "y.demir@example.test",
        "nhs": "349 552 1180", "crm_id": "hs-contact-87110",
        "last_seen": "4 months ago", "conditions": ["Eczema"],
        "flag": None, "consultations": 9,
    },
]

SCHEDULE = [
    {"time": "08:40", "patient": "PT-10771", "reason": "Anticoagulation review",
     "status": "done", "duration": "18 min", "written": 2},
    {"time": "09:20", "patient": "PT-10482", "reason": "Follow-up consultation",
     "status": "in_progress", "duration": "14 min",
     "binding": "invite sent 23 Jul · patient bound at scheduling"},
    {"time": "10:00", "patient": "PT-11033", "reason": "First appointment",
     "status": "upcoming", "binding": "invite sent 24 Jul · no record yet"},
    {"time": "10:30", "patient": "PT-10920", "reason": "Asthma review",
     "status": "upcoming", "binding": "invite sent 24 Jul · patient bound at scheduling"},
    {"time": "11:15", "patient": "PT-10218", "reason": "Repeat prescription",
     "status": "upcoming", "binding": "invite not yet sent"},
]

#: The record as it stood before the consultation began — what the bot read.
CONTEXT = {
    "last_seen": "2026-06-12",
    "last_seen_label": "6 weeks ago",
    "conditions": ["Type 2 diabetes (2019)", "Hypertension (2021)"],
    "medications": [
        {"drug": "Metformin", "dose": "1000 mg", "freq": "twice daily"},
        {"drug": "Ramipril", "dose": "5 mg", "freq": "once daily"},
    ],
    "open_items": [
        {"what": "HbA1c overdue", "since": "due 4 weeks ago", "urgency": "attention"},
        {"what": "Retinal screening", "since": "booked 2 Sep 2026", "urgency": "ok"},
    ],
    "allergies": ["Penicillin — rash"],
}

HISTORY = [
    {"date": "12 Jun 2026", "reason": "Diabetes review", "by": "Dr Priya Rao",
     "summary": "HbA1c 58 mmol/mol. Metformin continued at 1000 mg BD. Advised on "
                "morning readings; agreed to review in six weeks."},
    {"date": "04 Apr 2026", "reason": "Blood pressure check", "by": "Dr Priya Rao",
     "summary": "142/88 seated. Ramipril increased to 5 mg OD. No postural symptoms "
                "reported at this visit."},
    {"date": "19 Jan 2026", "reason": "Annual review", "by": "Dr James Whitfield",
     "summary": "Routine annual review. Retinal screening booked. Weight stable."},
]


#: The patient letter, drafted after the consultation and sent to nobody until a human
#: says so. Deliberately a *separate document* from the clinical note: the note is written
#: for the record and reads like it — "?postural hypotension 2° ramipril" is accurate,
#: terse, and frightening to receive. Same consultation, two audiences.
#:
#: Every block carries `sources`: the ids of the utterances it was drawn from. That is
#: what turns approval from "does this look plausible" into "is this what was said" — a
#: question a busy clinician can answer in seconds. A block with **no** source is the
#: interesting case: it is not hidden, it is flagged, because an unsupported sentence in a
#: medical letter is exactly the thing a reviewer must be pointed at rather than trusted
#: to notice. One is left in here on purpose.
LETTER = {
    "subject": "Summary of your appointment — 25 July",
    "greeting": "Dear Amara,",
    "paragraphs": [
        # `kind: courtesy` exempts a block from the unsourced flag. Without it the flag
        # fires on "thank you for coming in", and a warning that goes off on pleasantries
        # is one a busy reader learns to scroll past — which is exactly how the real
        # unsupported claim four blocks down would get through.
        {"id": "p1", "sources": [], "kind": "courtesy",
         "text": "Thank you for coming in this morning. Here is a summary of what we "
                 "talked about, so you have it written down."},
        {"id": "p2", "sources": ["u2", "u5"],
         "text": "Your morning blood sugar readings have gone up since we last met — you "
                 "mentioned they are around 9 to 10 before breakfast. I have requested a "
                 "blood test called HbA1c, which shows how your blood sugar has been over "
                 "the last two to three months."},
        {"id": "p3", "sources": ["u2", "u4"],
         "text": "You also said you have been feeling light-headed when you stand up. That "
                 "can sometimes be related to blood pressure medication, so I would like to "
                 "look at your ramipril dose once the blood test is back. Please keep "
                 "taking it as normal until then."},
        {"id": "p4", "sources": [],
         "text": "Your blood pressure was well controlled today."},
    ],
    "todos": [
        {"id": "t1", "sources": ["u5"],
         "text": "Have your HbA1c blood test at the surgery — no appointment needed, any "
                 "weekday morning before 11am."},
        {"id": "t2", "sources": ["u2", "u5"],
         "text": "Write down your morning readings before breakfast and bring them with "
                 "you next time."},
        {"id": "t3", "sources": ["u2"],
         "text": "Stand up slowly, particularly first thing in the morning."},
    ],
    "closing": {
        "id": "c1", "sources": ["u5"],
        "text": "I have booked your next appointment for Friday 5 September at 9:20am. "
                "A calendar invitation is on its way separately.",
    },
    "sign_off": "Best wishes,\nDr Priya Rao",
    "footer": "This mailbox is not monitored. If your symptoms get worse before your next "
              "appointment, please call the surgery on 0113 496 0112, or 111 outside "
              "opening hours. In an emergency, call 999.",
}


def demo_consultation() -> list[dict]:
    """The 09:20 consultation, as it happened."""
    log = ConsultationLog()
    e = log.emit
    patient = PATIENTS[0]

    e(0.0, PATIENT_BOUND, patient=patient,
      via="calendar invite sent 23 Jul 2026 · Follow-up consultation",
      note="identity established at scheduling time, not inferred from the call")
    e(0.4, ACCESSED, target=patient["crm_id"], what="HubSpot contact", effect="read")
    e(0.9, CONTEXT_FETCHED, context=CONTEXT)
    e(1.2, BOT_JOINED, bot="Cura", meeting="Google Meet · abc-defg-hij")

    e(2.0, SAID, id="u1", who="clinician", name="Dr Rao",
      text="Morning Amara — how have things been since June?")
    e(6.5, SAID, id="u2", who="patient", name="Amara Okafor",
      text="Mostly fine. The morning readings are higher than they were — around 9 or 10 "
           "before breakfast. And I've been getting a bit light-headed standing up.")
    e(14.0, NOTE, section="History", sources=["u2"],
      text="Reports fasting glucose 9–10 mmol/L, risen from previous review. New postural "
           "light-headedness since approximately early July.")
    e(19.0, SAID, id="u3", who="clinician", name="Dr Rao",
      text="Any change to how you're taking the ramipril?")
    e(23.0, SAID, id="u4", who="patient", name="Amara Okafor",
      text="No, same as before. Every morning with breakfast.")
    e(27.0, NOTE, section="Medication", sources=["u3", "u4"],
      text="Ramipril 5 mg OD, adherence reported unchanged. Postural symptoms to be "
           "weighed against antihypertensive dose at follow-up.")
    e(31.0, NOTE, section="Plan", sources=["u5"],
      text="HbA1c requested. Review in six weeks with results. Advised to record morning "
           "readings and to rise slowly from sitting.")

    e(34.0, SAID, id="u5", who="clinician", name="Dr Rao",
      text="Right — bloods done and I'll see you in six weeks. Cura, book her a "
           "follow-up in six weeks and log the HbA1c request.")
    e(36.5, ACTION_PROPOSED, action="schedule_followup_and_log_request",
      summary="Follow-up booked and the outstanding blood test logged",
      steps=[
          {"app": "calendar", "what": "Follow-up consultation · 5 Sep 2026, 09:20"},
          {"app": "hubspot", "what": "Note: HbA1c requested"},
          {"app": "hubspot", "what": "Task: chase HbA1c result"},
      ],
      subject=patient["id"], effects="write", reversible=True)
    e(41.0, ACTION_WRITTEN, action="schedule_followup_and_log_request",
      confirmed_by="Dr Rao", duration_s=2.4,
      observed=[
          {"app": "calendar", "what": "Follow-up consultation",
           "detail": "Fri 5 Sep 2026, 09:20–09:40 · invite sent to Amara"},
          {"app": "hubspot", "what": "Note added", "detail": "note-77219"},
          {"app": "hubspot", "what": "Task created", "detail": "task-4410 · due 29 Aug"},
      ])

    e(48.0, SAID, id="u6", who="clinician", name="Dr Rao",
      text="Oh — and while you're in there, update Mrs Patel's record too, she rang "
           "yesterday.")
    e(49.2, ACTION_REFUSED, action="update_contact", reason="wrong_subject",
      attempted_subject="PT-10920 · Nia Patel",
      bound_subject=f'{patient["id"]} · {patient["name"]}',
      says="This consultation is about Amara Okafor, so I can't write to Nia Patel's "
           "record from here. Open her consultation and I'll do it there.",
      note="Dr Rao has permission to edit that record. What's missing is a consultation "
           "about that patient — permission is not relevance.")

    e(56.0, SAID, id="u7", who="clinician", name="Dr Rao",
      text="Fair enough. See you in September, Amara.")
    e(60.0, CONSULTATION_ENDED,
      summary="Fasting glucose risen to 9–10 mmol/L with new postural light-headedness. "
              "HbA1c requested; antihypertensive dose to be reviewed against postural "
              "symptoms at follow-up.",
      written=3, refused=1, reads=1, duration_label="14 min")

    e(62.0, DRAFT_READY, drafted_by="Cura · claude-opus-5", hold_seconds=20,
      recipient={
          "name": patient["name"], "email": patient["email"],
          "verified_against": patient["crm_id"],
          "note": "the address on the record, not one taken from the conversation",
      },
      letter=LETTER)
    return log.events


def practice() -> dict:
    """Everything the page renders, in one payload."""
    return {
        "clinician": CLINICIAN,
        "today": TODAY,
        "patients": PATIENTS,
        "schedule": SCHEDULE,
        "context": CONTEXT,
        "history": HISTORY,
        "consultation": demo_consultation(),
        "surgery": SURGERY,
        "active_patient": PATIENTS[0]["id"],
    }


def to_json(indent: int | None = None) -> str:
    return json.dumps(practice(), indent=indent)


if __name__ == "__main__":
    print(to_json(indent=2))
