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
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..adapters.auth import Auth, AuthError, Session

COOKIE = "forge_session"
SESSION_TTL = 8 * 3600           # a clinic day
INDEX = "consult.html"


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
    server_version = "Forge"

    # injected by make_server
    root: Path
    auth: Auth
    store: SessionStore
    public_paths: frozenset[str]

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
            return self.login()
        if route == "/auth/callback":
            return self.callback(query)
        if route == "/auth/logout":
            return self.logout()

        session = self.store.get(self._sid())
        if route == "/me":
            return self._json(session.public() if session else {"identifier": None},
                              200 if session else 401)

        if session is None and route not in self.public_paths:
            return self._redirect("/auth/login")
        return self.static(url.path)

    def login(self):
        try:
            url, _state = self.auth.login_url()
        except AuthError as e:
            return self._text(
                f"Login is not configured: {e}\n\n"
                "Set SCALEKIT_ENVIRONMENT_URL, SCALEKIT_CLIENT_ID and "
                "SCALEKIT_CLIENT_SECRET in .env, then restart.", 503)
        return self._redirect(url)

    def callback(self, query: dict):
        if "error" in query:
            # The provider declined. Show its reason rather than a generic failure.
            return self._text(f"Sign-in was declined: {query['error'][0]}", 400)
        try:
            session = self.auth.callback(code=(query.get("code") or [""])[0],
                                         state=(query.get("state") or [None])[0])
        except AuthError as e:
            return self._text(f"Sign-in failed: {e}", 400)
        return self._redirect("/", sid=self.store.put(session))

    def logout(self):
        sid = self._sid()
        session = self.store.get(sid)
        self.store.drop(sid)          # dropped server-side first, so it is gone regardless
        target = "/"
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
                public_paths: frozenset[str] = frozenset()) -> ThreadingHTTPServer:
    """A server bound to `root`, with login in front of everything but `public_paths`."""
    namespace = {
        "root": Path(root).resolve(),
        "auth": auth,
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
