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
from ..adapters.meetstream import (
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
class LiveConsultations:
    """Transcript arriving from meetings, filed by the consultation it is bound to.

    In memory, like the session store, and for the same reason: a demo restart losing a
    consultation costs nothing, whereas a database here would be the largest thing in the
    repo and would prove nothing the design is claiming.

    What it does insist on is that **every line files itself under the binding it carried**.
    Nothing here looks up which consultation a line "probably" belongs to — the binding was
    fixed when the invite was sent and MeetStream echoes it on every event, so filing is a
    dictionary write rather than a decision. A line with no binding is dropped, because the
    alternative is guessing whose record it belongs in.
    """

    rooms: dict[str, dict] = field(default_factory=dict)

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
            "at": utterance.at, "who": utterance.role,
            "name": utterance.speaker, "text": utterance.text,
        })
        return True

    def lifecycle(self, event) -> bool:
        if event.binding is None:
            return False
        room = self._room(event.binding)
        room["status"] = ("ended" if event.ended else
                          "live" if event.live else event.event)
        if event.refused:
            room["refused_reason"] = event.message or event.event
        return True

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
    hook_secret: str

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
        else:
            event = normalize_lifecycle(payload)
            if event is not None:
                self.consultations.lifecycle(event)

        return self._json({"ok": True, "kept": event is not None})

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
                hook_secret: str = "",
                public_paths: frozenset[str] = frozenset()) -> ThreadingHTTPServer:
    """A server bound to `root`, with login in front of everything but `public_paths`."""
    namespace = {
        "root": Path(root).resolve(),
        "auth": auth,
        "consultations": consultations if consultations is not None else LiveConsultations(),
        "hook_secret": hook_secret or os.environ.get("MEETSTREAM_WEBHOOK_SECRET", ""),
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
