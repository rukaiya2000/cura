"""Send the bot into a real meeting, carrying the patient it is for.

    .venv/bin/python scripts/send_bot.py --meeting https://meet.google.com/abc-defg-hij \
                                         --patient PT-10482 --public https://xxxx.ngrok.app

    .venv/bin/python scripts/send_bot.py --dry-run --patient PT-10482    # show the payload

`--public` is the address MeetStream can reach this machine on. It cannot post to
127.0.0.1, so a local run needs a tunnel:

    ngrok http 8770          # then pass the https URL it prints as --public

The binding is fixed here, at scheduling time, and MeetStream echoes it back on every
transcript line and every lifecycle event. That is the whole mechanism: by the time
anybody speaks, which patient this is about was decided and cannot be inferred wrongly.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.meetstream import Binding, MeetStream, MeetStreamError
from skillforge.config import get, load_env
from skillforge.ui.consult import CLINICIAN, PATIENTS

DIM, B, GREEN, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[31m", "\033[0m",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting", help="Google Meet, Zoom or Teams link")
    ap.add_argument("--patient", default="PT-10482", help="patient id from the practice")
    ap.add_argument("--public", help="public base URL MeetStream can reach (ngrok)")
    ap.add_argument("--join-at", help="ISO 8601; schedule instead of joining now")
    # Read from the environment rather than hardcoded, so the name participants see is
    # configured in one place rather than living as a default in a script.
    ap.add_argument("--name", default=None, help="the bot's display name in the call")
    ap.add_argument("--dry-run", action="store_true", help="print the payload, send nothing")
    args = ap.parse_args()
    load_env()
    bot_name = args.name or get("MEETSTREAM_BOT_NAME") or "Cura"

    patient = next((p for p in PATIENTS if p["id"] == args.patient), None)
    if patient is None:
        print(f"{RED}no patient {args.patient}{OFF} — one of: "
              f"{', '.join(p['id'] for p in PATIENTS)}")
        return 1

    identifier = get("SKILLFORGE_IDENTIFIER_FULL") or ""
    binding = Binding(
        # A real consultation id would come from the appointment. Derived here so a
        # re-run for the same patient lands in the same room rather than a new one.
        consultation_id=f"con-{patient['id'].lower()}",
        patient_id=patient["id"],
        patient_name=patient["name"],
        clinician=identifier,
        clinician_name=CLINICIAN["name"],
        crm_id=patient["crm_id"],
    )

    print(f"\n{B}Bot briefing{OFF}")
    print(f"  patient       {B}{patient['name']}{OFF}  {DIM}{patient['id']} · "
          f"{patient['crm_id'] or 'no CRM record'}{OFF}")
    print(f"  acting as     {identifier or DIM + '(SKILLFORGE_IDENTIFIER_FULL unset)' + OFF}")
    print(f"  consultation  {binding.consultation_id}")
    print(f"  {DIM}This is fixed now. Every transcript line comes back carrying it, so "
          f"nothing\n  downstream has to work out whose consultation it is.{OFF}")

    if not args.public and not args.dry_run:
        print(f"\n  {RED}--public is required{OFF} — MeetStream cannot POST to 127.0.0.1.")
        print(f"  {DIM}Run `ngrok http 8770` and pass the https URL it prints.{OFF}\n")
        return 1

    base = (args.public or "https://example.invalid").rstrip("/")
    transcript_hook = f"{base}/hooks/transcript"
    lifecycle_hook = f"{base}/hooks/bot"

    if not get("MEETSTREAM_WEBHOOK_SECRET"):
        print(f"\n  {AMBER}MEETSTREAM_WEBHOOK_SECRET is not set{OFF} — the server will "
              f"reject every\n  webhook it receives. Set it in .env and configure the "
              f"same value in MeetStream.")

    if args.dry_run:
        payload = {
            "meeting_link": args.meeting or "<meeting link>",
            "bot_name": bot_name,
            "video_required": False,
            "custom_attributes": binding.to_attributes(),
            "live_transcription_required": {"webhook_url": transcript_hook},
            "callback_url": lifecycle_hook,
        }
        if args.join_at:
            payload["join_at"] = args.join_at
        print(f"\n{B}POST /api/v1/bots/create_bot{OFF}  {DIM}(dry run — nothing sent){OFF}")
        print(json.dumps(payload, indent=2))
        return 0

    if not args.meeting:
        print(f"\n  {RED}--meeting is required{OFF} unless --dry-run\n")
        return 1

    try:
        result = MeetStream().send_bot(
            meeting_link=args.meeting, binding=binding,
            transcript_webhook=transcript_hook, callback_url=lifecycle_hook,
            bot_name=bot_name, join_at=args.join_at,
        )
    except MeetStreamError as e:
        print(f"\n  {RED}{e}{OFF}\n")
        return 1

    print(f"\n  {GREEN}bot dispatched{OFF}  {DIM}{result.get('bot_id', result)}{OFF}")
    print(f"  transcript →  {transcript_hook}")
    print(f"  lifecycle  →  {lifecycle_hook}")
    print(f"\n  {DIM}Admit it from the meeting host controls if it lands in the lobby. "
          f"Watch\n  the consultation screen — lines appear as they are spoken.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
