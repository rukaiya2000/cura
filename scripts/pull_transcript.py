"""Fetch a bot's transcript from MeetStream and feed it into the app.

    .venv/bin/python scripts/pull_transcript.py --bot <bot_id>
    .venv/bin/python scripts/pull_transcript.py --bot <bot_id> --watch

Because the webhook has never fired. MeetStream's own API knows the bot joined and knows
what it heard, but has not delivered a single event to us — not a transcript line, not
even a lifecycle callback. Pulling does not depend on them being able to reach this
machine, which turns out to matter more than latency does.

**Lines are posted through the app's own signed webhook**, exactly as MeetStream would
deliver them. Nothing is written behind the app's back: same signature check, same
binding, same storage, same "Cura, can you hear me" reply path. A transcript arriving this
way is indistinguishable from one that arrived properly, which is the point — the demo
exercises the real code either way.

`--watch` polls every few seconds and posts only what is new, so it can run during a
consultation and the screen fills as people speak.
"""

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.meetstream import (
    Binding,
    MeetStream,
    MeetStreamError,
    normalize_pulled,
    pulled_entries,
)
from skillforge.config import get, load_env

DIM, B, GREEN, BLUE, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;68m", "\033[38;5;179m",
    "\033[31m", "\033[0m",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True, help="bot id from send_bot.py")
    ap.add_argument("--base", default="http://127.0.0.1:8770")
    ap.add_argument("--watch", action="store_true", help="keep polling for new lines")
    ap.add_argument("--every", type=float, default=5.0, help="seconds between polls")
    args = ap.parse_args()
    load_env()

    secret = get("MEETSTREAM_WEBHOOK_SECRET")
    if not secret:
        print(f"{RED}MEETSTREAM_WEBHOOK_SECRET is not set{OFF} — the app will reject "
              f"every line.\n")
        return 1

    client = MeetStream()

    # The binding comes from the bot itself, so a pulled line lands in exactly the
    # consultation the bot was dispatched for. Nothing here guesses.
    try:
        status = client._request("GET", f"/bots/{args.bot}/status", None)
    except MeetStreamError as e:
        print(f"\n  {RED}{e}{OFF}\n")
        return 1

    binding = Binding.from_attributes(status.get("custom_attributes"))
    if binding is None:
        print(f"\n  {RED}that bot carries no Cura binding{OFF} — it was not dispatched "
              f"by this app, so there is no consultation to file its words under.\n")
        return 1

    print(f"\n{B}Pulling the transcript{OFF}")
    print(f"  bot           {args.bot}  {DIM}{status.get('status', '?')}{OFF}")
    print(f"  patient       {B}{binding.patient_name}{OFF}  {DIM}{binding.patient_id}{OFF}")
    print(f"  {DIM}Posted through the app's own webhook — same signature, same "
          f"storage.{OFF}\n")

    seen: set[tuple] = set()
    quiet_rounds = 0
    while True:
        try:
            raw = client.transcript(args.bot)
        except MeetStreamError as e:
            print(f"  {RED}{e}{OFF}")
            return 1

        entries = pulled_entries(raw)
        fresh = 0
        for entry in entries:
            utterance = normalize_pulled(entry, binding)
            if utterance is None:
                continue
            # Dedupe on what was said and when, because polling re-reads the whole
            # transcript each time and a repeated line would be stored twice.
            key = (utterance.speaker, utterance.text, utterance.at)
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            colour = BLUE if utterance.role == "clinician" else OFF
            print(f"  {colour}{utterance.speaker}:{OFF} {utterance.text[:76]}")
            _post(args.base, secret, binding, utterance)

        if not args.watch:
            print(f"\n  {GREEN if seen else AMBER}{len(seen)} line(s) stored{OFF}"
                  f"{'' if seen else '  — MeetStream is holding no transcript yet'}\n")
            return 0

        quiet_rounds = quiet_rounds + 1 if fresh == 0 else 0
        if quiet_rounds == 6:
            print(f"  {DIM}(nothing new for {int(6 * args.every)}s — still watching; "
                  f"ctrl-c to stop){OFF}")
        time.sleep(args.every)


def _post(base: str, secret: str, binding: Binding, utterance) -> None:
    """Deliver one line the way MeetStream would, so the app cannot tell the difference."""
    payload = {
        "bot_id": utterance.bot_id or "pulled",
        "speakerName": utterance.speaker,
        "timestamp": utterance.at,
        "transcript": utterance.text,
        "new_text": (utterance.text.split() or [""])[-1],
        "utterance": "",
        "end_of_turn": True,
        "transcription_mode": "pulled",
        "custom_attributes": binding.to_attributes(),
    }
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base}/hooks/transcript", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-MeetStream-Signature": "sha256=" + hmac.new(
                     secret.encode(), body, hashlib.sha256).hexdigest()})
    try:
        urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as e:
        print(f"     {RED}{e.status}{OFF} {e.read().decode()[:70]}")
    except Exception as e:  # noqa: BLE001
        print(f"     {RED}{type(e).__name__}{OFF} {e}")


if __name__ == "__main__":
    raise SystemExit(main())
