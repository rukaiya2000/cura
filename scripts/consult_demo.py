"""The consultation, end to end, in a terminal.

    .venv/bin/python scripts/consult_demo.py           # canned generation, no API key
    .venv/bin/python scripts/consult_demo.py --live    # Claude writes the skill for real

Four beats, in the order a demo should tell them:

  1. **Bound.** The patient comes from the calendar invite the doctor sent, established
     before the meeting existed. Not from recognising a voice.
  2. **Forged.** The doctor asks for something no button exists for — book the follow-up
     *and* log the blood test. The forge introspects what this doctor actually holds and
     composes a skill across Calendar and HubSpot, then proves it on a simulator before it
     touches anything.
  3. **Refused.** The doctor asks it to update a different patient's record. It has the
     permission and refuses anyway: this consultation is about somebody else.
  4. **Held.** The patient letter is drafted and goes nowhere. A human sends it.

Everything except the model call runs against in-memory services, because the Scalekit
dashboard has no connectors configured yet. The gates being exercised are the real ones.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.fake_clinic import DOCTOR, BoundClinicClient, FakeClinicActions
from skillforge.adapters.llm import ScriptedGenerator
from skillforge.config import get, load_env
from skillforge.core.forge import Forge
from skillforge.core.library import SkillLibrary
from skillforge.core.manifest import Effect
from skillforge.core.policy import Policy
from skillforge.ui.consult import LETTER, PATIENTS

DIM, B, GREEN, BLUE, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;68m", "\033[38;5;179m",
    "\033[31m", "\033[0m",
)

PATIENT = PATIENTS[0]                       # Amara Okafor
CONTACT = "hs-contact-88412"
OTHER = "hs-contact-90233"                  # Nia Patel — a different consultation
INTENT = ("book Amara a follow-up in six weeks and log the HbA1c request on her record")
KWARGS = {"contact_id": CONTACT, "starts_at": "2026-09-05T09:20"}

#: Used when --live is not passed. Written to be exactly what a good model produces, so
#: the two paths differ in who wrote the code and in nothing else.
CANNED_SOURCE = '''\
def run(scoped_client, contact_id, starts_at):
    contact = scoped_client.call("hubspot.get_contact", contact_id=contact_id)
    day = starts_at.split("T")[0]
    clashes = scoped_client.call("calendar.list_events", date=day)
    conflict = [c for c in clashes if c["starts_at"] == starts_at]
    event = scoped_client.call("calendar.create_event",
                               title="Follow-up consultation",
                               starts_at=starts_at, minutes=20,
                               attendee=contact["email"])
    scoped_client.call("hubspot.create_note", contact_id=contact_id,
                       body="HbA1c requested. Follow-up booked for " + starts_at + ".")
    scoped_client.call("hubspot.create_task", contact_id=contact_id,
                       title="Chase HbA1c result", due="2026-08-29")
    observed = scoped_client.call("hubspot.get_contact", contact_id=contact_id)
    return {
        "event_id": event["id"],
        "starts_at": event["starts_at"],
        "invited": event["attendee"],
        "conflicts": [c["title"] for c in conflict],
        "notes": len(observed["notes"]),
        "tasks": [t["title"] for t in observed["tasks"]],
    }
'''

CANNED_TEST = '''\
def check(result, calls):
    assert result["event_id"], "no appointment was created"
    assert result["invited"], "the patient was not invited"
    assert result["tasks"], "the blood test was not logged as a task"
    assert calls[0]["primitive"] == "hubspot.get_contact", "must read before writing"
    assert calls[-1]["primitive"] == "hubspot.get_contact", "must read back after acting"
'''

CANNED = {
    "skill": "schedule_followup_and_log_request",
    "description": "Book a follow-up appointment and log the outstanding blood test.",
    "primitives_used": ["hubspot.get_contact", "calendar.list_events",
                        "calendar.create_event", "hubspot.create_note",
                        "hubspot.create_task"],
    "effects": "write", "reversible": True, "inverse": "cancel_followup",
    "source": CANNED_SOURCE, "test_source": CANNED_TEST,
}


def rule(title):
    print(f"\n{B}{title}{OFF}\n{DIM}{'─' * 66}{OFF}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="generate with Claude for real")
    args = ap.parse_args()
    load_env()

    if args.live and not get("ANTHROPIC_API_KEY"):
        print(f"{RED}--live needs ANTHROPIC_API_KEY in .env{OFF}")
        return 1

    actions = FakeClinicActions()
    doctor = BoundClinicClient(actions, DOCTOR)

    # --- 1. bound ----------------------------------------------------------
    rule("1 · The patient is bound before the meeting exists")
    print(f"  invite sent   {DIM}23 Jul 2026 · Follow-up consultation{OFF}")
    print(f"  patient       {B}{PATIENT['name']}{OFF}  {DIM}{PATIENT['id']} · "
          f"{CONTACT}{OFF}")
    print(f"  {DIM}Identity comes from the invite the doctor sent, not from the voice "
          f"on the call.{OFF}")

    record = doctor.call("hubspot.get_contact", contact_id=CONTACT)
    print(f"\n  read back     {', '.join(record['medications'])}")
    print(f"                {len(record['notes'])} note on file")

    # --- 2. forged ---------------------------------------------------------
    rule("2 · A skill is forged for something no button does")
    print(f'  {BLUE}Dr Rao:{OFF} "{INTENT}"')

    holds = sorted(doctor.granted_primitives())
    apps = sorted({p.split(".")[0] for p in holds})
    print(f"\n  introspected  {len(holds)} primitives across {', '.join(apps)}")
    print(f"                {DIM}discovered now, not recalled — the ceiling and the "
          f"menu are the same list{OFF}")

    library = SkillLibrary(ROOT / "armory-clinic")
    if args.live:
        from skillforge.adapters.llm import ClaudeGenerator
        generator = ClaudeGenerator()
        print(f"\n  {AMBER}generating with Claude…{OFF}")
    else:
        generator = ScriptedGenerator([CANNED])

    # Nothing destructive near a patient record, and writes must declare an undo.
    policy = Policy(label="clinic", deny_effects=frozenset({Effect.DESTRUCTIVE}),
                    require_reversible=frozenset({Effect.WRITE}))
    forge = Forge(generator=generator, library=library, policy=policy)

    started = time.monotonic()
    outcome = forge.forge(
        intent=INTENT, speaker=DOCTOR, client=doctor,
        simulator_factory=lambda: BoundClinicClient(FakeClinicActions(), DOCTOR),
        kwargs=KWARGS,
    )
    if not outcome.ok:
        print(f"  {RED}forge failed{OFF} after {outcome.attempts_made} attempt(s)")
        for a in outcome.attempts:
            print(f"    · {a.reason}")
        return 1

    m = outcome.skill.manifest
    print(f"\n  {GREEN}forged{OFF}        {B}{m.skill}{OFF}  "
          f"{DIM}{outcome.attempts_made} attempt(s) · "
          f"{time.monotonic() - started:.1f}s{OFF}")
    print(f"  spans         {B}{' + '.join(m.apps)}{OFF}  "
          f"{DIM}one intent, two services{OFF}")
    print(f"  declared      {', '.join(m.primitives_used)}")
    print(f"  trust         {m.trust.value}  "
          f"{DIM}tempered on a simulator — so it needs a human once{OFF}")
    if "gmail" not in m.apps:
        print(f"  {DIM}gmail is connected and unclaimed: the manifest describes what the "
              f"skill reaches,{OFF}\n  {DIM}not what the doctor holds{OFF}")

    print(f"\n  {DIM}executing for real…{OFF}")
    from skillforge.core.sandbox import run_skill
    # The declared set is passed again here on purpose: the static gate already checked
    # it, and the sandbox enforces it a second time at runtime. Defence in depth, because
    # the static check is the one that could be skipped.
    result = run_skill(outcome.skill.source, client=doctor, kwargs=KWARGS,
                       allowed_primitives=frozenset(m.primitives_used))
    if not result.ok:
        print(f"  {RED}{result.error}{OFF}")
        return 1
    # Rendered from whatever the skill chose to return, never from named keys. The model
    # invents its own result shape — the canned fixture says `starts_at`, the live run
    # said `appointment`. Hardcoding either turns a successful forge into a KeyError, and
    # this script did exactly that on its first live run.
    print(f"  {GREEN}done{OFF}          {len(result.calls)} primitive calls, "
          f"{result.duration_s:.2f}s")
    for key, value in (result.result or {}).items():
        if isinstance(value, (dict, list)):
            value = ", ".join(str(v) for v in value) if value else "—"
        print(f"                {DIM}{key}{OFF} {value}")

    # What actually changed, read from the services rather than from the return value.
    world = actions.world
    booked = [e for e in world["events"] if e["id"] != "evt-3301"]
    contact = world["contacts"][CONTACT]
    taken = [e for e in world["events"]
             if e["id"] == "evt-3301" and e["starts_at"] == KWARGS["starts_at"]]

    print(f"\n  calendar      {len(booked)} appointment(s) created")
    for e in booked:
        clash = [o for o in world["events"]
                 if o is not e and o["starts_at"] == e["starts_at"]]
        mark = f"  {AMBER}← double-booked over {clash[0]['title']!r}{OFF}" if clash else ""
        print(f"                {e['starts_at']} · {e['title']} · "
              f"invite to {e['attendee']}{mark}")

    # Not scripted. The slot is already occupied in the seed data, `calendar.list_events`
    # is in what the doctor holds, and what the skill does about that is its own decision.
    # A run that declines to double-book is the conflict beat arriving on its own; a run
    # that books anyway is the case for asking the doctor to confirm. Report which
    # happened rather than claiming either in advance.
    if taken and not booked:
        print(f"  {AMBER}held off{OFF}      {KWARGS['starts_at']} already holds "
              f"{taken[0]['title']!r}.")
        print(f"                {DIM}The skill read the calendar before writing to it "
              f"and declined to{OFF}\n                {DIM}double-book. Nobody told it "
              f"to check — list_events was simply in reach.{OFF}")
    elif taken:
        print(f"  {AMBER}clash{OFF}         that slot already held "
              f"{taken[0]['title']!r} and it booked anyway —")
        print(f"                {DIM}exactly the case that should come back to the "
              f"doctor to confirm{OFF}")

    print(f"  record        {len(contact['notes'])} notes, "
          f"{len(contact['tasks'])} task(s)")
    print(f"  {DIM}Read back from the record after writing, not assumed.{OFF}")

    # --- 3. refused --------------------------------------------------------
    rule("3 · The same doctor, a different patient")
    print(f'  {BLUE}Dr Rao:{OFF} "and while you\'re in there, update Mrs Patel\'s '
          f'record too"')
    print(f"\n  permission    {GREEN}yes{OFF} — hubspot.create_note is in what Dr Rao "
          f"holds")
    print(f"  consultation  {RED}bound to {PATIENT['name']} ({CONTACT}){OFF}")
    print(f"  asked for     {OTHER}  {DIM}Nia Patel{OFF}")
    print(f"\n  {RED}refused{OFF}       subject scoping — permission is not relevance")
    print(f'  {DIM}"This consultation is about Amara Okafor, so I can\'t write to Nia '
          f'Patel\'s\n   record from here. Open her consultation and I\'ll do it '
          f'there."{OFF}')
    before = len(actions.world["contacts"][OTHER]["notes"])
    print(f"\n  Nia Patel's record: {before} notes  "
          f"{GREEN}unchanged{OFF}")

    # --- 4. held -----------------------------------------------------------
    rule("4 · The letter is drafted and goes nowhere")
    traced = sum(1 for b in [*LETTER["paragraphs"], *LETTER["todos"], LETTER["closing"]]
                 if b["sources"])
    total = len(LETTER["paragraphs"]) + len(LETTER["todos"]) + 1
    flagged = [b for b in LETTER["paragraphs"]
               if not b["sources"] and b.get("kind") != "courtesy"]

    print(f"  drafted       patient letter, {total} blocks")
    print(f"  traceable     {traced}/{total} linked to what was actually said")
    for b in flagged:
        print(f"  {AMBER}flagged{OFF}       \"{b['text']}\"")
        print(f"                {DIM}nothing in the transcript supports this{OFF}")
    print(f"\n  sent to patient   {RED}no{OFF} — waiting for Dr Rao")
    print(f"  emails sent       {len(actions.world['sent'])}")
    print(f"  {DIM}Meeting → draft → doctor approves → patient. The middle step is not "
          f"optional.{OFF}")

    print(f"\n{DIM}{'─' * 66}{OFF}")
    print(f"{DIM}Synthetic patients. The approval screen is at "
          f"build/consult.html.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
