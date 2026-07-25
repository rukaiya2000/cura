"""The web layer: login routes, a session cookie, and the consult UI behind them.

Two decisions worth stating, because both are the kind that look arbitrary and are not.

**The cookie holds an opaque session id, never a token.** Putting the access token in the
cookie — even HttpOnly — means the credential travels on every request, sits in browser
storage, and cannot be revoked without waiting for expiry. An opaque id keeps tokens
server-side, makes logout instant and real, and means a stolen cookie is worth nothing once
the session is dropped.

**`SameSite=Lax`, not `Strict`.** The cookie is set during the OAuth callback and must
survive the top-level navigation back to `/`. `Strict` drops it on exactly that hop, and
the symptom is a login that appears to succeed and lands on the login page again.

Demo-grade, deliberately and only where it costs nothing that matters:

* sessions live in memory — they vanish on restart, and a second worker would not see them
* `Secure` is set only when serving over HTTPS, because localhost is HTTP and the flag
  would otherwise stop the cookie being stored at all
* logout is a GET, so it is theoretically CSRF-able. The harm is being logged out.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from ..adapters.auth import Auth, AuthError, Session
from ..core.drafting import ClaudeDrafter, DraftError
from ..core.wake import reply_to
from ..adapters.meetstream import (
    Binding,
    MeetStream,
    MeetStreamError,
    normalize_lifecycle,
    normalize_transcript,
    verify_signature,
)

COOKIE = "cura_session"
SESSION_TTL = 8 * 3600           # a clinic day
INDEX = "consult.html"
SIGNIN = "signin.html"
SIGNIN_ROUTE = "/signin"
TRANSCRIPT_HOOK = "/hooks/transcript"
LIFECYCLE_HOOK = "/hooks/bot"
SEND_BOT_ROUTE = "/bot/send"
PATIENTS_ROUTE = "/patients"
DRAFT_PREFIX = "/consultation/"
#: A transcript turn is a sentence. Anything approaching a megabyte is not one, and
#: reading an unbounded Content-Length from an unauthenticated caller is how a public
#: endpoint becomes a memory exhaustion bug.
MAX_HOOK_BYTES = 256 * 1024


@dataclass
class SessionStore:
    """Opaque id → Session, with an expiry. Tokens stay here and never reach the browser."""

    ttl: int = SESSION_TTL
    _rows: dict[str, tuple[Session, float]] = field(default_factory=dict, repr=False)

    def put(self, session: Session) -> str:
        sid = secrets.token_urlsafe(32)
        self._rows[sid] = (session, time.time() + self.ttl)
        return sid

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        row = self._rows.get(sid)
        if row is None:
            return None
        session, expires = row
        if time.time() > expires:
            self._rows.pop(sid, None)
            return None
        return session

    def drop(self, sid: str | None) -> None:
        if sid:
            self._rows.pop(sid, None)

    def __len__(self) -> int:
        return len(self._rows)


@dataclass
class PatientStore:
    """The doctor's own patients, added through the UI.

    Persisted to a JSON file rather than kept in memory: a session vanishing on restart is
    an inconvenience, but a patient list vanishing means re-typing every record, and
    nobody would use it twice.

    **A new patient starts clinically empty** — name, date of birth and how to reach them,
    nothing else. Conditions, medications and history are not asked for, because they are
    what the consultations are going to write. That is the product working, not a gap in
    the form.

    Patients are filed under the clinician who added them. This is a demo-grade boundary,
    not a claim about multi-tenancy: it stops one doctor's list appearing on another's
    screen, and nothing more.
    """

    path: Path | None = None
    rows: dict[str, list[dict]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path and self.path.is_file():
            try:
                self.rows = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                # A corrupt file must not stop the app booting. Starting empty is
                # recoverable; refusing to start is not.
                self.rows = {}

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.rows, indent=2))

    def adopt(self, clinician: str, aliases: list[str]) -> None:
        """Move rows filed under a previous key for this person onto the current one.

        Identifiers change — an opaque `sub` becomes an email the moment profile
        resolution starts working — and a patient list keyed by the old value simply
        disappears. Migrating on read means the doctor never sees that happen.
        """
        moved = False
        for alias in aliases:
            if alias == clinician or alias not in self.rows:
                continue
            self.rows.setdefault(clinician, []).extend(self.rows.pop(alias))
            moved = True
        if moved:
            # Ids were sequential per clinician, so merging two lists can collide.
            seen, unique = set(), []
            for patient in self.rows.get(clinician, []):
                key = (patient["name"], patient.get("dob", ""))
                if key in seen:
                    continue
                seen.add(key)
                patient["id"] = f"PT-{10000 + len(unique) + 1}"
                unique.append(patient)
            self.rows[clinician] = unique
            self._save()

    def list(self, clinician: str, aliases: list[str] | None = None) -> list[dict]:
        if aliases:
            self.adopt(clinician, aliases)
        return list(self.rows.get(clinician, ()))

    def get(self, clinician: str, patient_id: str) -> dict | None:
        return next((p for p in self.rows.get(clinician, ())
                     if p["id"] == patient_id), None)

    #: Fields a clinician may edit directly. Everything else about a patient is derived
    #: from consultations, and a form that let you type it would be a form that lets you
    #: disagree with the record.
    EDITABLE = ("conditions", "medications", "allergies", "phone")

    def update(self, clinician: str, patient_id: str, changes: dict) -> dict | None:
        patient = self.get(clinician, patient_id)
        if patient is None:
            return None
        for key in self.EDITABLE:
            if key in changes:
                value = changes[key]
                patient[key] = ([v.strip() for v in value if str(v).strip()]
                                if isinstance(value, list) else str(value).strip())
        self._save()
        return patient

    def observe(self, clinician: str, patient_id: str, entries: list[dict]) -> dict | None:
        """Append what a consultation learned, without overwriting what was there.

        Appended rather than replaced, and each entry keeps the consultation it came
        from. A record that silently rewrites itself is one nobody can audit, and "when
        did we first know this?" is a question clinical records exist to answer.
        """
        patient = self.get(clinician, patient_id)
        if patient is None:
            return None
        known = {(n.get("kind"), n.get("text")) for n in patient.setdefault("notes", [])}
        for entry in entries:
            key = (entry.get("kind"), entry.get("text"))
            if key in known or not entry.get("text"):
                continue
            known.add(key)
            patient["notes"].append(entry)
            # Conditions and medications are also surfaced as lists, because that is how
            # a doctor scans them — but the note remains the record of when it was said.
            bucket = {"condition": "conditions", "medication": "medications",
                      "allergy": "allergies"}.get(entry.get("kind"))
            if bucket and entry["text"] not in patient.setdefault(bucket, []):
                patient[bucket].append(entry["text"])
        patient["consultations"] = patient.get("consultations", 0) + 1
        self._save()
        return patient

    def add(self, clinician: str, *, name: str, dob: str = "", email: str = "",
            nhs: str = "") -> dict:
        mine = self.rows.setdefault(clinician, [])
        patient = {
            # Sequential per clinician, so an id is readable in a demo and stable across
            # restarts. A real deployment takes this from the record system.
            "id": f"PT-{10000 + len(mine) + 1}",
            "name": name.strip(),
            "dob": dob.strip(),
            "email": email.strip().lower(),
            "nhs": nhs.strip(),
            "crm_id": None,          # no record until one is created, deliberately
            # The clinical picture. Empty at creation — these are what the consultations
            # fill in — but editable, because a doctor moving an existing patient across
            # should not have to hold three years of history in their head until the next
            # appointment.
            "conditions": [],
            "medications": [],
            "allergies": [],
            "notes": [],
            "consultations": 0,
            "added_by": clinician,
        }
        mine.append(patient)
        self._save()
        return patient


@dataclass
class LiveConsultations:
    """Transcript arriving from meetings, filed by the consultation it is bound to.

    Persisted, like the patient list. A consultation is the record of what was said to a
    patient — what both the letter and the clinical note are derived from — so losing it
    to a restart means the conversation happened and nothing remains of it. It is also
    the one kind of data here that cannot be re-entered by hand.

    What it insists on is that **every line files itself under the binding it carried**.
    Nothing here looks up which consultation a line "probably" belongs to — the binding was
    fixed when the invite was sent and MeetStream echoes it on every event, so filing is a
    dictionary write rather than a decision. A line with no binding is dropped, because the
    alternative is guessing whose record it belongs in.
    """

    path: Path | None = None
    rooms: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path and self.path.is_file():
            try:
                self.rooms = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.rooms = {}

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.rooms, indent=2))

    def _room(self, binding) -> dict:
        return self.rooms.setdefault(binding.consultation_id, {
            "consultation_id": binding.consultation_id,
            "patient_id": binding.patient_id,
            "patient_name": binding.patient_name,
            "crm_id": binding.crm_id,
            "clinician": binding.clinician,
            "said": [],
            "status": "waiting",
        })

    def said(self, utterance) -> bool:
        if utterance.binding is None:
            return False
        room = self._room(utterance.binding)
        room["said"].append({
            # A stable id per line, assigned on arrival. The drafter cites these, and a
            # citation is only checkable if the id it names never moves — so they are
            # allocated once, here, rather than derived from position later.
            "id": f"u{len(room['said']) + 1}",
            "at": utterance.at, "who": utterance.role,
            "name": utterance.speaker, "text": utterance.text,
        })
        self._save()
        return True

    def lifecycle(self, event) -> bool:
        if event.binding is None:
            return False
        room = self._room(event.binding)
        room["status"] = ("ended" if event.ended else
                          "live" if event.live else event.event)
        if event.refused:
            room["refused_reason"] = event.message or event.event
        self._save()
        return True

    def dispatched(self, binding, bot_id: str) -> dict:
        """Register the room the moment a bot is sent, before any webhook arrives.

        Without this the screen shows nothing for the twenty-odd seconds it takes a bot
        to join, and the doctor reasonably concludes the button did not work.
        """
        room = self._room(binding)
        room["bot_id"] = bot_id
        room["status"] = "dispatched"
        self._save()
        return room

    def get(self, consultation_id: str) -> dict | None:
        return self.rooms.get(consultation_id)

    def for_clinician(self, identifier: str) -> list[dict]:
        """Only this doctor's consultations.

        The binding names the clinician the bot acts under, so this is a filter rather
        than a permission check — but it is the filter that stops one doctor's live
        transcript appearing on another's screen.
        """
        return [r for r in self.rooms.values() if r["clinician"] == identifier]


def _safe_path(root: Path, url_path: str) -> Path | None:
    """Resolve a request path inside `root`, or None if it escapes.

    Custom routing means no inherited traversal protection, so this is explicit: resolve
    to canonical form and confirm the result is still under root. Rejects `..`, absolute
    paths, and symlinks pointing outward.
    """
    relative = url_path.lstrip("/") or INDEX
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


class Handler(BaseHTTPRequestHandler):
    server_version = "Cura"

    # injected by make_server
    root: Path
    auth: Auth
    store: SessionStore
    public_paths: frozenset[str]
    consultations: "LiveConsultations"
    patients: "PatientStore"
    hook_secret: str
    public_url: str
    drafter: object

    # --- plumbing ----------------------------------------------------------

    def log_message(self, *args):
        pass

    @property
    def secure(self) -> bool:
        """HTTPS only. On plain HTTP the Secure flag would prevent storage entirely."""
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _sid(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        jar.load(raw)
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def _set_cookie(self, sid: str) -> None:
        parts = [f"{COOKIE}={sid}", "Path=/", "HttpOnly", "SameSite=Lax",
                 f"Max-Age={SESSION_TTL}"]
        if self.secure:
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _clear_cookie(self) -> None:
        self.send_header("Set-Cookie",
                         f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def _redirect(self, to: str, *, sid: str | None = None, clear: bool = False) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        if sid:
            self._set_cookie(sid)
        if clear:
            self._clear_cookie()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, message: str, status: int) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- routes ------------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        route = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)

        if route == "/auth/login":
            return self.start(signup=False)
        if route == "/auth/signup":
            return self.start(signup=True)
        if route == "/auth/callback":
            return self.callback(query)
        if route == "/auth/logout":
            return self.logout()

        session = self.store.get(self._sid())
        if route == "/me":
            return self._json(session.public() if session else {"identifier": None},
                              200 if session else 401)

        # The sign-in screen is the one page reachable signed out. Served rather than
        # redirected to the provider, so someone arriving here sees where they are and
        # chooses sign in or create account — an instant bounce to an unfamiliar domain
        # is how a login looks broken.
        if route == SIGNIN_ROUTE:
            return self.static(f"/{SIGNIN}")

        # Live transcript, for the consultation screen to poll. Behind the gate and
        # scoped to the signed-in clinician — the webhook that fills this is public, but
        # what it collects is a patient conversation and only its own doctor may read it.
        if route == PATIENTS_ROUTE:
            if session is None:
                return self._json({"patients": []}, 401)
            return self._json({"patients": self.patients.list(
                session.identifier, aliases=session.aliases)})

        if route == "/live":
            if session is None:
                return self._json({"consultations": []}, 401)
            return self._json(
                {"consultations": self.consultations.for_clinician(session.identifier)})

        if session is None and route not in self.public_paths:
            return self._redirect(SIGNIN_ROUTE)
        return self.static(url.path)

    # --- webhooks ----------------------------------------------------------

    def do_POST(self):
        """MeetStream delivering transcript and lifecycle events.

        **Deliberately outside the session gate.** MeetStream has no cookie and never
        will; requiring one would mean no transcript ever arrives. Authenticity comes from
        the HMAC signature instead, which is why an unset `MEETSTREAM_WEBHOOK_SECRET`
        rejects rather than waves everything through: an open endpoint here does not leak
        data, it *injects* it, and what it injects ends up in a patient's record.

        MeetStream does not retry non-2xx, so this answers 2xx as soon as the event is
        accepted and does no work the response has to wait for.
        """
        route = urlparse(self.path).path.rstrip("/") or "/"

        # Dispatching a bot is the doctor acting, so unlike the webhooks below it is
        # behind the session gate and carries no signature.
        if route == SEND_BOT_ROUTE:
            return self.send_bot()
        if route == PATIENTS_ROUTE:
            return self.add_patient()
        if route.startswith(DRAFT_PREFIX) and route.endswith("/draft"):
            return self.draft_consultation(
                route[len(DRAFT_PREFIX):-len("/draft")])

        if route not in (TRANSCRIPT_HOOK, LIFECYCLE_HOOK):
            return self._text("Not found", 404)

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_HOOK_BYTES:
            return self._text("Too large", 413)
        body = self.rfile.read(length) if length else b""

        if not verify_signature(self.hook_secret, body,
                                self.headers.get("X-MeetStream-Signature")):
            # No detail in the response: a rejected caller learns only that it was
            # rejected, never which half of the check it failed.
            return self._text("Signature required", 401)

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._text("Bad JSON", 400)

        if route == TRANSCRIPT_HOOK:
            event = normalize_transcript(payload)
            # `None` is the normal case, not an error: word-level interim events stream
            # continuously and only completed turns are ours to keep.
            if event is not None:
                self.consultations.said(event)
                self.maybe_answer(event)
        else:
            event = normalize_lifecycle(payload)
            if event is not None:
                self.consultations.lifecycle(event)

        return self._json({"ok": True, "kept": event is not None})

    def maybe_answer(self, utterance) -> None:
        """Say something back, but only when spoken to by name.

        Runs after the line is stored, never instead of it: a reply that fails must not
        cost the transcript. And it is best-effort — a bot that could not get a word into
        the chat is a small disappointment, whereas a webhook that 500s because of it
        loses the consultation, and MeetStream does not retry.
        """
        reply = reply_to(utterance.text)
        if not reply or not utterance.bot_id:
            return
        try:
            MeetStream().say(utterance.bot_id, reply)
        except Exception:  # noqa: BLE001 — never let speaking break listening
            pass

    def draft_consultation(self, consultation_id: str):
        """Draft the note and the letter from what was actually said.

        Synchronous on purpose. Drafting takes tens of seconds and the honest thing is a
        request that takes tens of seconds — a background job would need a queue, a poll
        and a failure path, all to hide a wait the clinician is expecting anyway.
        """
        session = self.store.get(self._sid())
        if session is None:
            return self._json({"error": "Sign in first."}, 401)

        room = self.consultations.get(consultation_id)
        if room is None:
            return self._json({"error": "No such consultation."}, 404)
        # The binding names whose consultation this is; a doctor may only draft their own.
        if room["clinician"] not in set(session.aliases):
            return self._json({"error": "That is not your consultation."}, 403)
        if not room.get("said"):
            return self._json({"error":
                "Nothing was said in this consultation, so there is nothing to draft."},
                400)

        patient = self.patients.get(session.identifier, room["patient_id"]) or {}
        try:
            draft = self.drafter.draft(
                said=room["said"],
                patient=room.get("patient_name") or "the patient",
                clinician=session.name or "the clinician")
        except DraftError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:  # noqa: BLE001 — the model or the network; say which
            return self._json({"error": f"{type(e).__name__}: {e}"}, 502)

        room["draft"] = draft.to_event(recipient={
            "name": room.get("patient_name", ""),
            "email": patient.get("email", ""),
            "verified_against": patient.get("crm_id") or patient.get("id", ""),
        })
        room["note"] = draft.note
        self.consultations._save()
        return self._json({"ok": True, "consultation_id": consultation_id,
                           "unsupported": len(draft.unsupported),
                           "invented_citations": draft.invented_citations})

    def add_patient(self):
        """Add a patient to the signed-in doctor's list.

        Only a name is required. Asking for conditions and history up front would be
        asking the doctor to type what the consultations are about to write.
        """
        session = self.store.get(self._sid())
        if session is None:
            return self._json({"error": "Sign in first."}, 401)

        length = min(int(self.headers.get("Content-Length") or 0), MAX_HOOK_BYTES)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "Bad request."}, 400)

        name = str(body.get("name") or "").strip()
        if not name:
            return self._json({"error": "A patient needs a name."}, 400)
        if len(name) > 120:
            return self._json({"error": "That name is too long."}, 400)

        email = str(body.get("email") or "").strip()
        if email and "@" not in email:
            # Checked because this address is where a medical summary will be sent, and
            # a typo there is the wrong-recipient failure with no undo.
            return self._json({"error": "That does not look like an email address."}, 400)

        patient = self.patients.add(
            session.identifier, name=name, dob=str(body.get("dob") or ""),
            email=email, nhs=str(body.get("nhs") or ""))
        return self._json({"ok": True, "patient": patient})

    def send_bot(self):
        """Put the bot in a meeting, bound to the patient the doctor picked.

        The binding is built **here, from the session and the chosen patient** — never
        from anything the browser sent beyond the patient id. A request that could name
        its own `clinician` would let one doctor dispatch a bot acting as another.
        """
        session = self.store.get(self._sid())
        if session is None:
            return self._json({"error": "Sign in first."}, 401)

        length = min(int(self.headers.get("Content-Length") or 0), MAX_HOOK_BYTES)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "Bad request."}, 400)

        link = str(body.get("meeting_link") or "").strip()
        patient_id = str(body.get("patient_id") or "").strip()
        if not link.startswith(("https://", "http://")):
            return self._json({"error": "That does not look like a meeting link."}, 400)

        patient = self.patients.get(session.identifier, patient_id)
        if patient is None:
            return self._json({"error": f"No patient {patient_id} on your list."}, 400)

        if not self.public_url:
            return self._json({"error":
                "This server has no public address, so MeetStream cannot deliver the "
                "transcript back. Start a tunnel and restart with --public."}, 503)

        binding = Binding(
            consultation_id=f"con-{patient['id'].lower()}",
            patient_id=patient["id"], patient_name=patient["name"],
            clinician=session.identifier,          # from the session, never the request
            clinician_name=session.name,
            crm_id=patient["crm_id"],
        )
        base = self.public_url.rstrip("/")
        try:
            result = MeetStream().send_bot(
                meeting_link=link, binding=binding,
                transcript_webhook=f"{base}{TRANSCRIPT_HOOK}",
                callback_url=f"{base}{LIFECYCLE_HOOK}",
                bot_name=os.environ.get("MEETSTREAM_BOT_NAME") or "Cura",
            )
        except MeetStreamError as e:
            return self._json({"error": str(e)}, 502)

        # Register the room immediately so the screen has something to show before the
        # first webhook lands — otherwise the doctor gets nothing back for ~20 seconds
        # and reasonably concludes it did not work.
        self.consultations.dispatched(binding, result.get("bot_id", ""))
        return self._json({"ok": True, "bot_id": result.get("bot_id", ""),
                           "consultation_id": binding.consultation_id,
                           "patient_name": patient["name"]})

    def start(self, *, signup: bool):
        """Hand off to the provider. Sign-in and sign-up differ only in which screen
        Scalekit shows; the callback below cannot tell them apart, and must not need to."""
        try:
            url, _state = self.auth.login_url(signup=signup)
        except AuthError:
            # Missing credentials is a deployment problem, not the doctor's. Send them to
            # the sign-in page, which explains it and disables the buttons, rather than a
            # bare 503 they can only reload.
            return self._redirect(f"{SIGNIN_ROUTE}?unconfigured=1")
        return self._redirect(url)

    def callback(self, query: dict):
        if "error" in query:
            # The provider declined. Carry its reason back rather than a generic failure.
            return self._redirect(f"{SIGNIN_ROUTE}?error={quote(query['error'][0])}")
        try:
            session = self.auth.callback(code=(query.get("code") or [""])[0],
                                         state=(query.get("state") or [None])[0])
        except AuthError as e:
            return self._redirect(f"{SIGNIN_ROUTE}?error={quote(str(e))}")
        return self._redirect("/", sid=self.store.put(session))

    def logout(self):
        sid = self._sid()
        session = self.store.get(sid)
        self.store.drop(sid)          # dropped server-side first, so it is gone regardless
        target = f"{SIGNIN_ROUTE}?signedout=1"
        if session is not None:
            try:
                target = self.auth.logout_url(session, redirect_to=self.origin())
            except Exception:  # noqa: BLE001 — a local logout must still succeed
                pass
        return self._redirect(target, clear=True)

    def origin(self) -> str:
        host = self.headers.get("Host") or "localhost"
        return f"{'https' if self.secure else 'http'}://{host}/"

    def static(self, url_path: str):
        target = _safe_path(self.root, url_path)
        if target is None:
            return self._text("Not found", 404)
        if target.is_dir():
            target = target / INDEX
        if not target.is_file():
            return self._text("Not found", 404)

        body = target.read_bytes()
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        # Without an explicit charset a browser decodes UTF-8 bytes as Latin-1, so every
        # `·` became `Â·` and every `—` became `â€"` across the whole app. The pages are
        # UTF-8; say so rather than hoping the browser guesses.
        if kind.startswith("text/") or kind in ("application/javascript",
                                                "application/json"):
            kind = f"{kind}; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # The page and its session data change while open; a cached copy makes the UI
        # look frozen mid-consultation.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)


def make_server(*, root: Path, auth: Auth, host: str = "127.0.0.1", port: int = 8770,
                store: SessionStore | None = None,
                consultations: "LiveConsultations | None" = None,
                patients: "PatientStore | None" = None,
                hook_secret: str = "", public_url: str = "", drafter=None,
                public_paths: frozenset[str] = frozenset()) -> ThreadingHTTPServer:
    """A server bound to `root`, with login in front of everything but `public_paths`."""
    namespace = {
        "root": Path(root).resolve(),
        "auth": auth,
        "consultations": consultations if consultations is not None else LiveConsultations(),
        "patients": patients if patients is not None else PatientStore(),
        "hook_secret": hook_secret or os.environ.get("MEETSTREAM_WEBHOOK_SECRET", ""),
        "public_url": public_url,
        "drafter": drafter if drafter is not None else ClaudeDrafter(),
        # `is None`, not `or` — SessionStore defines __len__, so an empty store is falsy.
        # `store or SessionStore()` silently replaced a caller's fresh store with a second
        # one, and the symptom was sessions that appeared to be created and then weren't
        # there.
        "store": SessionStore() if store is None else store,
        "public_paths": public_paths,
    }
    handler = type("BoundHandler", (Handler,), namespace)
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer((host, port), handler)
