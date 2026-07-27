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

import json

from skillforge.adapters.meetstream import Binding, MeetStream, MeetStreamError
from skillforge.config import get, load_env
from skillforge.ui.serve import consultation_id

DIM, B, GREEN, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[31m", "\033[0m",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting", help="Google Meet, Zoom or Teams link")
    ap.add_argument("--patient", default=None,
                    help="patient id; omit to use your first patient")
    ap.add_argument("--public", help="public base URL MeetStream can reach (ngrok)")
    ap.add_argument("--join-at", help="ISO 8601; schedule instead of joining now")
    # Read from the environment rather than hardcoded, so the name participants see is
    # configured in one place rather than living as a default in a script.
    ap.add_argument("--name", default=None, help="the bot's display name in the call")
    ap.add_argument("--provider", default=None,
                    help="transcription backend: deepgram_streaming (documented, needs a "
                         "Deepgram key in MeetStream), assemblyai_streaming, or "
                         "meeting_captions (no key, but the host must enable captions)")
    ap.add_argument("--dry-run", action="store_true", help="print the payload, send nothing")
    args = ap.parse_args()
    load_env()
    bot_name = args.name or get("MEETSTREAM_BOT_NAME") or "Cura"

    # The doctor's own patients, not the demo fixture — this script used to read the
    # fixture, so it could only ever dispatch a bot for someone who does not exist.
    identifier = get("SKILLFORGE_IDENTIFIER_FULL") or ""
    store = ROOT / "data" / "patients.json"
    everyone = json.loads(store.read_text()) if store.is_file() else {}
    mine = everyone.get(identifier, [])
    patient = next((p for p in mine if p["id"] == args.patient), None) if args.patient \
        else (mine[0] if mine else None)

    if patient is None:
        print(f"{RED}no patient {args.patient or ''}{OFF} for {identifier}. Available:")
        for p in mine:
            print(f"    {p['id']}  {p['name']}")
        if not mine:
            print(f"    {DIM}(none — add one in the app first){OFF}")
        return 1

    binding = Binding(
        consultation_id=consultation_id(identifier, patient["id"]),
        patient_id=patient["id"],
        patient_name=patient["name"],
        clinician=identifier,
        clinician_name=get("SKILLFORGE_CLINICIAN_NAME") or "",
        crm_id=patient.get("crm_id"),
    )

    print(f"\n{B}Bot briefing{OFF}")
    print(f"  patient       {B}{patient['name']}{OFF}  {DIM}{patient['id']} · "
          f"{patient['crm_id'] or 'no CRM record'}{OFF}")
    print(f"  acting as     {identifier or DIM + '(SKILLFORGE_IDENTIFIER_FULL unset)' + OFF}")
    print(f"  consultation  {binding.consultation_id}")
    print(f"  {DIM}This is fixed now. Every transcript line comes back carrying it, so "
          f"nothing\n  downstream has to work out whose consultation it is.{OFF}")

    # `--public` is optional. It only exists so MeetStream can push webhooks, and against
    # this account they have never fired once — not a transcript line, not a lifecycle
    # event, across every bot dispatched. The working route is to pull:
    #
    #     scripts/pull_transcript.py --bot <id> --watch
    #
    # which fetches from MeetStream and posts to this machine directly, so it needs no
    # tunnel, no public address, and nothing of MeetStream's to be reachable inbound.
    if not args.public and not args.dry_run:
        print(f"\n  {AMBER}no --public{OFF} — webhooks cannot reach this machine, so the "
              f"bot will\n  record and deliver nothing by itself. Pull instead once it "
              f"has joined:")
        print(f"    {DIM}.venv/bin/python scripts/pull_transcript.py --bot <id> "
              f"--watch{OFF}\n")

    base = (args.public or "https://webhooks.unreachable.invalid").rstrip("/")
    transcript_hook = f"{base}/hooks/transcript"
    lifecycle_hook = f"{base}/hooks/bot"

    if not get("MEETSTREAM_WEBHOOK_SECRET"):
        print(f"\n  {AMBER}MEETSTREAM_WEBHOOK_SECRET is not set{OFF} — the server will "
              f"reject every\n  webhook it receives. Set it in .env and configure the "
              f"same value in MeetStream.")

    if args.dry_run:
        # The adapter's own payload, not a copy of it.
        payload = MeetStream(api_key="dry-run",
                             provider=args.provider or "").bot_payload(
            meeting_link=args.meeting or "<meeting link>", binding=binding,
            transcript_webhook=transcript_hook, callback_url=lifecycle_hook,
            bot_name=bot_name, join_at=args.join_at)
        print(f"\n{B}POST /api/v1/bots/create_bot{OFF}  {DIM}(dry run — nothing sent){OFF}")
        print(json.dumps(payload, indent=2))
        return 0

    if not args.meeting:
        print(f"\n  {RED}--meeting is required{OFF} unless --dry-run\n")
        return 1

    try:
        result = MeetStream(provider=args.provider or '').send_bot(
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
