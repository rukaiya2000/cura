"""Play a consultation into the running app, without a meeting.

    .venv/bin/python scripts/seed_consultation.py --patient PT-10001
    .venv/bin/python scripts/seed_consultation.py --patient PT-10001 --file mine.txt

Then open the app: the consultation is there, and "Draft the note and letter" works on it.

**It goes through the real webhook.** Each line is signed with MEETSTREAM_WEBHOOK_SECRET
and POSTed to /hooks/transcript exactly as MeetStream would deliver it — same signature
check, same normaliser, same binding, same storage. Nothing is written behind the app's
back, so a demo run exercises the code a real call would and cannot pass while the real
path is broken.

Transcript file format — one line per turn, `speaker: text`:

    Dr Rao: Morning Amara, how have things been since June?
    Amara Okafor: The morning readings are higher, around 9 or 10 before breakfast.

Speakers are matched against the binding, so use the patient's name exactly as it appears
in the app, and the clinician's name for yourself. Anyone else is recorded as `other`,
which is correct — their words must not be filed as the patient's.
"""

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.meetstream import Binding
from skillforge.config import get, load_env

DIM, B, GREEN, BLUE, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;68m", "\033[38;5;179m",
    "\033[31m", "\033[0m",
)

#: A consultation with something in it worth drafting: a symptom the patient volunteers,
#: a medication check, a plan, and one thing said in passing that the letter should not
#: turn into a clinical claim.
SAMPLE = """\
Dr Rao: Morning, how have things been since we last met?
PATIENT: Mostly fine. The morning readings are higher than they were, around 9 or 10 \
before breakfast. And I've been getting a bit light-headed when I stand up.
Dr Rao: Any change to how you're taking the ramipril?
PATIENT: No, same as before. Every morning with breakfast.
Dr Rao: And how are you sleeping?
PATIENT: Not brilliantly, but that's the new flat more than anything.
Dr Rao: Right. I'll request an HbA1c so we can see how things have been over a few \
months, and I'd like to see you again in six weeks with the result. Try to get up slowly \
from sitting in the meantime.
PATIENT: That's fine. Thank you.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--patient", help="patient id, e.g. PT-10001 (default: the first one)")
    ap.add_argument("--file", help="transcript file; omit to use the built-in sample")
    ap.add_argument("--base", default="http://127.0.0.1:8770")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds between lines, to watch it arrive live")
    args = ap.parse_args()
    load_env()

    secret = get("MEETSTREAM_WEBHOOK_SECRET")
    if not secret:
        print(f"{RED}MEETSTREAM_WEBHOOK_SECRET is not set{OFF} — the server will reject "
              f"every line.\n")
        return 1

    patients_file = ROOT / "data" / "patients.json"
    if not patients_file.is_file():
        print(f"{RED}no patients yet{OFF} — sign in and add one with + first.\n")
        return 1
    everyone = json.loads(patients_file.read_text())

    clinician, patient = _pick(everyone, args.patient)
    if patient is None:
        print(f"{RED}no patient {args.patient!r}{OFF}. Available:")
        for who, rows in everyone.items():
            for p in rows:
                print(f"    {p['id']}  {p['name']}  {DIM}({who}){OFF}")
        return 1

    clinician_name = get("SKILLFORGE_CLINICIAN_NAME") or "Dr Rao"
    binding = Binding(
        consultation_id=f"con-{patient['id'].lower()}",
        patient_id=patient["id"], patient_name=patient["name"],
        clinician=clinician, clinician_name=clinician_name,
        crm_id=patient.get("crm_id"),
    )

    raw = Path(args.file).read_text() if args.file else SAMPLE
    turns = _parse(raw, patient=patient["name"], clinician=clinician_name)
    if not turns:
        print(f"{RED}no usable lines{OFF} — expected `Speaker: what they said`.\n")
        return 1

    print(f"\n{B}Playing a consultation into the app{OFF}")
    print(f"  patient       {B}{patient['name']}{OFF}  {DIM}{patient['id']}{OFF}")
    print(f"  as            {clinician}")
    print(f"  {DIM}Signed and posted to /hooks/transcript — the same path MeetStream "
          f"uses.{OFF}\n")

    # The bot arriving, so the room has a status before any speech lands.
    _post(args.base, "/hooks/bot", secret, {
        "bot_event": "bot.inmeeting", "bot_id": "seeded", "status_code": 200,
        "message": "Seeded consultation", "timestamp": _stamp(0),
        "custom_attributes": binding.to_attributes()})

    kept = 0
    for index, (speaker, text) in enumerate(turns):
        # Interim word-level events too, because the app must ignore them and a demo that
        # skips them would not prove that.
        _post(args.base, "/hooks/transcript", secret,
              _turn(binding, speaker, " ".join(text.split()[:2]), index, final=False))
        ok = _post(args.base, "/hooks/transcript", secret,
                   _turn(binding, speaker, text, index, final=True))
        kept += 1 if ok else 0
        colour = BLUE if speaker == clinician_name else OFF
        print(f"  {colour}{speaker}:{OFF} {text[:78]}{'…' if len(text) > 78 else ''}")
        if args.delay:
            time.sleep(args.delay)

    _post(args.base, "/hooks/bot", secret, {
        "bot_event": "bot.stopped", "bot_id": "seeded", "status_code": 200,
        "message": "Consultation ended", "timestamp": _stamp(len(turns)),
        "custom_attributes": binding.to_attributes()})

    print(f"\n  {GREEN}{kept} turns stored{OFF} {DIM}· data/consultations.json{OFF}")
    print(f"\n  Open {args.base} → Consultation → "
          f"{B}Draft the note and letter{OFF}\n")
    return 0


def _pick(everyone: dict, wanted: str | None):
    """The clinician key and patient row for `wanted`, or the first patient there is.

    The configured identifier is tried first. Patient files can hold rows under a previous
    identifier — an opaque `sub` before profile resolution started working — and binding a
    seeded consultation to that key files it under a clinician nobody signs in as, so it
    never appears on screen.
    """
    preferred = get("SKILLFORGE_IDENTIFIER_FULL") or ""
    ordered = ([(preferred, everyone[preferred])] if preferred in everyone else []) + [
        (k, v) for k, v in everyone.items() if k != preferred]
    for clinician, rows in ordered:
        for row in rows:
            if wanted is None or row["id"] == wanted:
                return clinician, row
    first = next(iter(everyone), "")
    return first, None


def _parse(raw: str, *, patient: str, clinician: str) -> list[tuple[str, str]]:
    """`Speaker: text` lines. `PATIENT:` and `DOCTOR:` are aliases for the bound names."""
    turns = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        speaker, _, text = line.partition(":")
        speaker, text = speaker.strip(), text.strip()
        if not text:
            continue
        if speaker.upper() == "PATIENT":
            speaker = patient
        elif speaker.upper() in ("DOCTOR", "CLINICIAN"):
            speaker = clinician
        turns.append((speaker, text))
    return turns


def _stamp(index: int) -> str:
    # Fixed base plus an offset, so a re-run produces the same timestamps and the demo
    # does not drift with the wall clock.
    return (datetime(2026, 7, 25, 9, 20) + timedelta(seconds=index * 12)).isoformat()


def _turn(binding, speaker, text, index, *, final):
    return {
        "bot_id": "seeded", "speakerName": speaker, "timestamp": _stamp(index),
        "transcript": text, "new_text": (text.split() or [""])[-1], "utterance": "",
        "end_of_turn": final, "transcription_mode": "word_level",
        "custom_attributes": binding.to_attributes(),
    }


def _post(base: str, path: str, secret: str, payload: dict) -> bool:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-MeetStream-Signature": "sha256=" + hmac.new(
                     secret.encode(), body, hashlib.sha256).hexdigest()})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read() or b"{}").get("kept", False)
    except urllib.error.HTTPError as e:
        print(f"  {RED}{e.status}{OFF} {path} — {e.read().decode()[:80]}")
    except Exception as e:  # noqa: BLE001
        print(f"  {RED}{type(e).__name__}{OFF} {path} — {e}")
    return False


if __name__ == "__main__":
    raise SystemExit(main())
