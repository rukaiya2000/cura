"""Clinician login, through Scalekit — the same provider that holds their tool tokens.

Why not Firebase: the point of logging in here is not just "who is this" but *"which
connected accounts may I act through"*. With a separate identity provider you log in, get
a UID, then maintain a mapping from that UID to the Scalekit identifier holding this
doctor's HubSpot and Calendar tokens. That mapping is a table that can drift, and when it
drifts the symptom is the bot writing to the wrong clinician's CRM — precisely the class of
error the rest of this codebase spends its effort preventing.

Logging in through Scalekit removes the mapping entirely: **the authenticated subject *is*
the identifier.** `Session.identifier` is what gets passed to `list_scoped_tools` and
`execute_tool`, with nothing in between to disagree.

Method names and option shapes here were read off the installed SDK
(`scalekit-sdk-python` 2.15.0), not from documentation alone — the `actions` surface had
already differed from expectations once.

Flow, and where each piece lives:

    login()       → build the authorization URL, remember `state`   (browser leaves)
    callback()    → exchange the code, return a Session             (browser returns)
    restore()     → validate a stored token, rebuild the Session    (later requests)
    logout()      → the provider's logout URL
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SCOPES = "openid profile email offline_access"


class AuthError(Exception):
    """Login could not be completed. Never carries a token in its message."""


@dataclass
class Session:
    """An authenticated clinician.

    `identifier` is the value handed to Scalekit's tool APIs. It is derived from the
    verified token rather than from anything the browser sent, which is what stops a
    caller nominating whose records they act on.
    """

    identifier: str
    email: str
    name: str
    access_token: str = field(repr=False, default="")
    refresh_token: str = field(repr=False, default="")
    id_token: str = field(repr=False, default="")
    claims: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace(".", " ").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or self.email[:2].upper()

    def public(self) -> dict:
        """What may safely reach the browser — no tokens, ever."""
        return {"identifier": self.identifier, "email": self.email,
                "name": self.name, "initials": self.initials}


def _client():
    import scalekit

    missing = [k for k in ("SCALEKIT_ENVIRONMENT_URL", "SCALEKIT_CLIENT_ID",
                           "SCALEKIT_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        raise AuthError(f"missing credentials: {', '.join(missing)}")
    return scalekit.client.ScalekitClient(
        env_url=os.environ["SCALEKIT_ENVIRONMENT_URL"],
        client_id=os.environ["SCALEKIT_CLIENT_ID"],
        client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    )


def _subject(claims: dict) -> tuple[str, str, str]:
    """Pull identifier, email and display name out of verified claims — and *only* those.

    **Nothing here may fall back to the exchange response's `user` object.** An earlier
    version did, for the case where the token carried no email claim, and the effect was
    that a token without that claim let the response payload nominate the identifier — so
    an attacker-controlled `user.email` became the value the tool APIs act under. Trusting
    the signature and then quietly reading the unsigned copy is worse than not verifying at
    all, because the code looks careful.

    Email is preferred as the identifier because it is what a human types into the
    Scalekit dashboard when authorising a connector, so the value the doctor connects
    HubSpot under and the value they log in as are the same string. `sub` is the fallback.
    """
    email = (claims.get("email") or "").strip().lower()
    name = (claims.get("name")
            or " ".join(filter(None, [claims.get("given_name"),
                                      claims.get("family_name")])).strip()
            or email or "Clinician")
    identifier = email or claims.get("sub") or ""
    if not identifier:
        raise AuthError("token carried no email or subject — cannot derive an identifier")
    return identifier, email, name


class Auth:
    def __init__(self, *, redirect_uri: str, client=None,
                 scopes: str = DEFAULT_SCOPES) -> None:
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self._client = client
        #: Issued `state` values awaiting their callback. In-memory is fine for a
        #: single-process app; a deployment behind more than one worker needs a shared
        #: store, or the callback lands on a process that never issued the state.
        self._pending: set[str] = set()

    @property
    def client(self):
        if self._client is None:
            self._client = _client()
        return self._client

    # --- leaving ------------------------------------------------------------

    def login_url(self) -> tuple[str, str]:
        """The URL to send the browser to, and the `state` that must come back.

        `state` is generated here and checked on return — without it, anyone can hand the
        callback a code of their choosing and log the victim into an attacker's account.
        """
        from scalekit.common.scalekit import AuthorizationUrlOptions

        state = secrets.token_urlsafe(24)
        options = AuthorizationUrlOptions()
        options.scopes = self.scopes
        options.state = state

        url = self.client.get_authorization_url(self.redirect_uri, options)
        self._pending.add(state)
        return str(url), state

    # --- returning ----------------------------------------------------------

    def callback(self, *, code: str, state: str | None) -> Session:
        """Exchange the code for a session. Rejects a state we did not issue."""
        if state is None or state not in self._pending:
            raise AuthError("unrecognised state — refusing this callback")
        self._pending.discard(state)

        if not code:
            raise AuthError("no authorization code on the callback")

        from scalekit.common.scalekit import CodeAuthenticationOptions

        try:
            result = self.client.authenticate_with_code(
                code, self.redirect_uri, CodeAuthenticationOptions())
        except Exception as e:  # noqa: BLE001 — the provider's failure, reported plainly
            raise AuthError(f"{type(e).__name__}: {e}") from e

        access = _get(result, "access_token") or ""
        if not access:
            raise AuthError("exchange returned no access token")

        # Identity comes from the *verified* token, never from the response's convenience
        # `user` object — that is the difference between trusting a signature and trusting
        # whatever came back over the wire. The response `user` is deliberately not passed
        # anywhere near `_subject`; see the note there.
        claims = self._claims(access)
        identifier, email, name = _subject(claims)

        return Session(
            identifier=identifier, email=email, name=name,
            access_token=access,
            refresh_token=_get(result, "refresh_token") or "",
            id_token=_get(result, "id_token") or "",
            claims=claims,
        )

    # --- later requests -----------------------------------------------------

    def restore(self, access_token: str) -> Session:
        """Rebuild a session from a stored token, or raise if it no longer validates."""
        claims = self._claims(access_token)
        identifier, email, name = _subject(claims)
        return Session(identifier=identifier, email=email, name=name,
                       access_token=access_token, claims=claims)

    def refresh(self, refresh_token: str) -> Session:
        try:
            result = self.client.refresh_access_token(refresh_token)
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"could not refresh: {type(e).__name__}: {e}") from e

        access = _get(result, "access_token") or ""
        if not access:
            raise AuthError("refresh returned no access token")
        session = self.restore(access)
        session.refresh_token = _get(result, "refresh_token") or refresh_token
        return session

    def logout_url(self, session: Session, *, redirect_to: str | None = None) -> str:
        from scalekit.common.scalekit import LogoutUrlOptions

        return str(self.client.get_logout_url(LogoutUrlOptions(
            id_token_hint=session.id_token or None,
            post_logout_redirect_uri=redirect_to,
        )))

    # --- verification -------------------------------------------------------

    def _claims(self, token: str) -> dict:
        """Verify the token and return its claims.

        `validate_access_token_and_get_claims` does both in one call, so there is no
        window in which claims are read from a token that was not checked. Falls back to
        validate-then-decode on SDK versions without it.
        """
        try:
            getter = getattr(self.client, "validate_access_token_and_get_claims", None)
            if getter is not None:
                claims = getter(token)
            else:
                if not self.client.validate_access_token(token):
                    raise AuthError("token failed validation")
                claims = self.client.validate_token(token)
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"token rejected: {type(e).__name__}: {e}") from e

        claims = _as_dict(claims)
        if not claims:
            raise AuthError("token validated but carried no claims")
        return claims


def _get(obj, name):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    for attr in ("to_dict", "_asdict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:  # noqa: BLE001
                pass
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")} \
        if hasattr(obj, "__dict__") else {}
