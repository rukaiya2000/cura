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
import sys
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SCOPES = "openid profile email offline_access"

#: Used when a verified token carries no name at all. Named rather than
#: inline because `initials` must recognise it: an avatar reading "C" for
#: "Clinician" looks like a real initial and hides that identity is missing.
FALLBACK_NAME = "Clinician"


def _debug(message: str) -> None:
    """Print a diagnostic when SKILLFORGE_DEBUG_AUTH=1, and nothing otherwise.

    **Claim names only, never values and never tokens.** A login that resolves to the
    wrong identifier still looks like a successful login, so this is the only way to tell
    "the provider omitted the claim" from "we rejected the token" — but a debug switch
    that prints credentials is a worse problem than the one it solves.
    """
    if os.environ.get("SKILLFORGE_DEBUG_AUTH") == "1":
        print(f"[auth] {message}", file=sys.stderr, flush=True)


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
        """Two letters for the avatar, from whatever identity actually arrived.

        An email is split on its local part rather than the whole string: taking the first
        two words of "rukaiyak2000@gmail.com" gives "RC", where the C is from "com" — a
        letter from the domain, which is nobody's initial.
        """
        source = self.name if self.name and self.name != FALLBACK_NAME else ""
        if "@" in source or not source:
            source = (self.email or self.identifier or "").split("@")[0]
        parts = [p for p in source.replace(".", " ").replace("_", " ").split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        letters = "".join(ch for ch in (parts[0] if parts else "") if ch.isalpha())
        return (letters[:2] or "??").upper()

    @property
    def identifies_by_email(self) -> bool:
        """Whether the identifier is an address a connected account is likely filed under.

        When a token carries no email claim the identifier falls back to `sub` — an opaque
        `usr_…` string. The login still works, so nothing looks wrong, but every tool call
        then runs under a name no connected account was created with, and fails at the
        point of acting rather than the point of signing in. The UI surfaces this.
        """
        return "@" in self.identifier

    @property
    def aliases(self) -> list[str]:
        """Every key this person's data might already be filed under.

        The identifier is the email when one can be resolved and the opaque `sub`
        otherwise — so fixing identity resolution *changes* it for an existing user, and
        anything keyed by the old value is orphaned. That happened: two patients were
        added under `usr_…` and vanished from the list when the email started resolving.
        The subject is stable across that change, so it stays as a lookup key even though
        it is no longer the primary one.
        """
        keys = [self.identifier, str(self.claims.get("sub") or ""), self.email]
        return [k for i, k in enumerate(keys) if k and k not in keys[:i]]

    def public(self) -> dict:
        """What may safely reach the browser — no tokens, ever."""
        return {"identifier": self.identifier, "email": self.email,
                "name": self.name, "initials": self.initials,
                "identifies_by_email": self.identifies_by_email}


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
            or email or FALLBACK_NAME)
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

    def login_url(self, *, signup: bool = False) -> tuple[str, str]:
        """The URL to send the browser to, and the `state` that must come back.

        `state` is generated here and checked on return — without it, anyone can hand the
        callback a code of their choosing and log the victim into an attacker's account.

        `signup=True` sets `prompt=create`, which lands on Scalekit's account-creation
        screen instead of its sign-in screen. It is a *hint about which screen to show*,
        not a separate flow: the callback, the token exchange and the session that comes
        back are identical either way, so nothing downstream has to know which button was
        pressed. Treating sign-up as its own pipeline is how the two drift apart.
        """
        from scalekit.common.scalekit import AuthorizationUrlOptions

        state = secrets.token_urlsafe(24)
        options = AuthorizationUrlOptions()
        options.scopes = self.scopes
        options.state = state
        if signup:
            options.prompt = "create"

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
        #
        # Two tokens, both verified, because they carry different things. The access token
        # proves the session; OIDC puts `name` and `email` in the **id token**. Reading
        # identity from the access token alone produced a live session displaying
        # "Clinician" with an opaque `usr_…` identifier — and since that identifier is what
        # every tool call runs under, it silently failed to match the connected account
        # filed under the doctor's email.
        claims = self._claims(access)
        id_token = _get(result, "id_token") or ""
        id_claims = self._id_claims(id_token) if id_token else {}
        if id_claims:
            claims = {**id_claims, **{k: v for k, v in claims.items() if v}}

        # Names of the claims that arrived, never their values. Identity here fails in a
        # way that looks like success — you are signed in, just as the wrong string — so
        # the only way to tell an absent claim from a rejected token is to look.
        # Neither token carries profile claims. Observed against a live Scalekit
        # environment: the id token contains only amr/at_hash/aud/azp/c_hash/client_id/
        # exp/iat/iss/sub, despite `openid profile email` being requested. So the profile
        # is resolved from the directory instead, keyed by the `sub` we just verified.
        #
        # This is *not* the hole closed in `_subject`. That was reading an unsigned
        # payload the browser delivered. This is a server-to-server lookup, authenticated
        # with our own client credentials, keyed by a subject a signature already proved.
        # The browser cannot influence which user is fetched.
        if not claims.get("email"):
            claims = {**claims, **self._profile(str(claims.get("sub") or ""))}

        _debug("exchange returned id_token: %s" % bool(id_token))
        _debug("access-token claims: %s" % sorted(self._claims(access)))
        _debug("id-token claims:     %s" % sorted(id_claims))
        _debug("merged claims:       %s" % sorted(claims))
            # Access-token claims win only where they are actually populated, so a `sub`
            # from the access token stays authoritative while `email` and `name` come from
            # the id token that carries them.
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

    def _profile(self, sub: str) -> dict:
        """Email and name for a verified subject, from Scalekit's user directory.

        Returns `{}` on any failure. Like `_id_claims` this only *enriches* — the access
        token already established who this is, and being unable to look up a display name
        is not a reason to refuse someone entry. The cost of failing is an opaque
        identifier, which the UI already flags.
        """
        if not sub:
            return {}
        try:
            response = self.client.users.get_user(sub)
            user = _get(response[0] if isinstance(response, tuple) else response, "user")
            profile = _get(user, "user_profile")
            found = {
                "email": _get(user, "email") or "",
                "name": _get(profile, "name") or "",
                "given_name": _get(profile, "given_name") or "",
                "family_name": _get(profile, "family_name") or "",
            }
            resolved = {k: v for k, v in found.items() if v}
            _debug(f"directory lookup for sub resolved: {sorted(resolved)}")
            return resolved
        except Exception as e:  # noqa: BLE001 — enrichment only
            _debug(f"directory lookup failed: {type(e).__name__}: {e}")
            return {}

    def _id_claims(self, id_token: str) -> dict:
        """Verify the id token and return its claims — or `{}` if it will not verify.

        Verified, not decoded. Reading an unverified JWT payload for `email` would hand
        an attacker the identifier through the back door, which is exactly the hole closed
        in `_subject`. A token that fails validation contributes nothing rather than
        contributing something unchecked.

        Failure is soft because this only *enriches* identity: the access token has already
        established who this is. Losing a display name is not a reason to fail a login.
        """
        # An id token always carries `aud = client_id`, and the underlying JWT library
        # raises when a token has an audience and the caller supplies none. Validating
        # without it therefore fails every time — which is exactly what happened: a real
        # login showed "Clinician" and an opaque `usr_…` identifier while this silently
        # returned {}. The audience is tried first, then without, because a token that
        # carries no `aud` fails the opposite way.
        for audience in (os.environ.get("SCALEKIT_CLIENT_ID") or None, None):
            try:
                claims = _as_dict(self.client.validate_token(id_token, audience=audience))
                if claims:
                    return claims
            except Exception as e:  # noqa: BLE001 — try the next shape
                _debug(f"id token rejected with audience={audience!r}: "
                       f"{type(e).__name__}: {e}")
                continue
        return {}

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
