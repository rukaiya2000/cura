"""Clinician login through Scalekit, against a fake SDK.

No network. What matters here is not that the SDK gets called — it's the three places a
login can be quietly wrong:

* identity taken from a **verified token** rather than from whatever the response's
  convenience `user` object claims;
* `state` checked on the callback, so a code can't be injected;
* tokens never reaching anything browser-facing.
"""

import pytest

from skillforge.adapters.auth import Auth, AuthError, Session

CLAIMS = {"sub": "usr_9912", "email": "Priya.Rao@clinic.test", "name": "Dr Priya Rao"}


class FakeClient:
    """Mirrors the surface read off scalekit-sdk-python 2.15.0."""

    def __init__(self, *, claims=CLAIMS, exchange=None, valid=True, url="https://auth/go"):
        self.claims = claims
        self.exchange = exchange
        self.valid = valid
        self.url = url
        self.seen = {}

    def get_authorization_url(self, redirect_uri, options):
        self.seen["redirect_uri"] = redirect_uri
        self.seen["scopes"] = options.scopes
        self.seen["state"] = options.state
        return f"{self.url}?state={options.state}"

    def authenticate_with_code(self, code, redirect_uri, options):
        self.seen["code"] = code
        if self.exchange is not None:
            return self.exchange
        return {"access_token": "at_live", "refresh_token": "rt_live",
                "id_token": "it_live",
                "user": {"email": "attacker@evil.test", "name": "Someone Else"}}

    def validate_access_token_and_get_claims(self, token):
        if not self.valid:
            raise RuntimeError("expired")
        self.seen["validated"] = token
        return dict(self.claims)

    def refresh_access_token(self, refresh_token):
        self.seen["refreshed"] = refresh_token
        return {"access_token": "at_fresh", "refresh_token": "rt_fresh"}

    def get_logout_url(self, options):
        self.seen["logout"] = options
        return "https://auth/bye"


def auth_for(**kw) -> tuple[Auth, FakeClient]:
    client = FakeClient(**kw)
    return Auth(redirect_uri="http://localhost:8770/auth/callback", client=client), client


def login(auth) -> str:
    _, state = auth.login_url()
    return state


# --- leaving ----------------------------------------------------------------


def test_the_authorization_url_carries_scopes_and_a_state(auth_and_client=None):
    auth, client = auth_for()
    url, state = auth.login_url()

    assert url.startswith("https://auth/go")
    assert client.seen["redirect_uri"] == "http://localhost:8770/auth/callback"
    assert "openid" in client.seen["scopes"] and "offline_access" in client.seen["scopes"]
    assert client.seen["state"] == state
    assert len(state) > 20, "state must be unguessable"


def test_each_login_gets_its_own_state():
    auth, _ = auth_for()
    assert login(auth) != login(auth)


# --- returning: the state check --------------------------------------------


def test_a_state_we_never_issued_is_refused():
    """Without this, anyone can hand the callback a code of their choosing and log the
    victim into an attacker's account."""
    auth, client = auth_for()
    login(auth)

    with pytest.raises(AuthError, match="unrecognised state"):
        auth.callback(code="stolen", state="not-one-of-ours")
    assert "code" not in client.seen, "exchanged a code despite the bad state"


def test_a_missing_state_is_refused():
    auth, _ = auth_for()
    with pytest.raises(AuthError, match="unrecognised state"):
        auth.callback(code="abc", state=None)


def test_a_state_cannot_be_replayed():
    auth, _ = auth_for()
    state = login(auth)
    auth.callback(code="abc", state=state)

    with pytest.raises(AuthError, match="unrecognised state"):
        auth.callback(code="abc", state=state)


def test_a_callback_with_no_code_is_refused():
    auth, _ = auth_for()
    with pytest.raises(AuthError, match="no authorization code"):
        auth.callback(code="", state=login(auth))


# --- returning: identity comes from the verified token ----------------------


def test_identity_comes_from_the_verified_token_not_the_response_user():
    """The fake's exchange response claims to be attacker@evil.test. The verified claims
    say Priya. The session must follow the signature, not the payload."""
    auth, client = auth_for()
    session = auth.callback(code="abc", state=login(auth))

    assert session.email == "priya.rao@clinic.test"
    assert session.identifier == "priya.rao@clinic.test"
    assert "evil" not in session.identifier
    assert client.seen["validated"] == "at_live", "token was never validated"


def test_the_identifier_is_the_email_lowercased():
    """It has to match what the doctor typed into the Scalekit dashboard when authorising
    a connector — same string, or the tool tokens are filed somewhere else."""
    auth, _ = auth_for(claims={**CLAIMS, "email": "Dr.Rao@Clinic.TEST"})
    session = auth.callback(code="abc", state=login(auth))
    assert session.identifier == "dr.rao@clinic.test"


def test_falls_back_to_subject_when_no_email_claim():
    auth, _ = auth_for(claims={"sub": "usr_9912", "name": "Dr Rao"})
    session = auth.callback(code="abc", state=login(auth))
    assert session.identifier == "usr_9912"


def test_a_token_with_neither_email_nor_subject_is_rejected():
    auth, _ = auth_for(claims={"name": "Nobody"})
    with pytest.raises(AuthError, match="cannot derive an identifier"):
        auth.callback(code="abc", state=login(auth))


def test_a_name_is_assembled_from_given_and_family_when_absent():
    auth, _ = auth_for(claims={"sub": "u1", "email": "a@b.test",
                               "given_name": "Priya", "family_name": "Rao"})
    session = auth.callback(code="abc", state=login(auth))
    assert session.name == "Priya Rao"


def test_an_exchange_returning_no_token_is_an_error():
    auth, _ = auth_for(exchange={"refresh_token": "rt"})
    with pytest.raises(AuthError, match="no access token"):
        auth.callback(code="abc", state=login(auth))


def test_a_provider_failure_is_reported_not_swallowed():
    class Boom(FakeClient):
        def authenticate_with_code(self, *a, **k):
            raise ConnectionError("upstream down")

    auth = Auth(redirect_uri="http://localhost/cb", client=Boom())
    with pytest.raises(AuthError, match="ConnectionError"):
        auth.callback(code="abc", state=login(auth))


# --- tokens stay out of anything browser-facing ----------------------------


def test_the_public_view_carries_no_tokens():
    auth, _ = auth_for()
    session = auth.callback(code="abc", state=login(auth))
    public = session.public()

    assert set(public) == {"identifier", "email", "name", "initials"}
    blob = repr(public)
    for secret in ("at_live", "rt_live", "it_live"):
        assert secret not in blob


def test_repr_does_not_leak_tokens():
    """A session lands in logs and tracebacks; the tokens must not go with it."""
    auth, _ = auth_for()
    session = auth.callback(code="abc", state=login(auth))
    text = repr(session)

    for secret in ("at_live", "rt_live", "it_live"):
        assert secret not in text, f"{secret} appears in repr()"
    assert "priya.rao@clinic.test" in text, "repr should still be useful"


def test_initials_are_derived_for_the_avatar():
    assert Session(identifier="a", email="a@b.c", name="Dr Priya Rao").initials == "DP"
    assert Session(identifier="a", email="ab@c.d", name="").initials == "AB"


# --- later requests --------------------------------------------------------


def test_a_stored_token_can_rebuild_a_session():
    auth, client = auth_for()
    session = auth.restore("at_stored")

    assert session.identifier == "priya.rao@clinic.test"
    assert client.seen["validated"] == "at_stored"


def test_an_invalid_stored_token_is_rejected():
    auth, _ = auth_for(valid=False)
    with pytest.raises(AuthError, match="token rejected"):
        auth.restore("at_expired")


def test_refresh_returns_a_session_and_keeps_a_refresh_token():
    auth, client = auth_for()
    session = auth.refresh("rt_old")

    assert client.seen["refreshed"] == "rt_old"
    assert session.access_token == "at_fresh"
    assert session.refresh_token == "rt_fresh"


def test_refresh_keeps_the_old_token_when_the_provider_returns_none():
    class NoRotate(FakeClient):
        def refresh_access_token(self, refresh_token):
            return {"access_token": "at_fresh"}

    auth = Auth(redirect_uri="http://localhost/cb", client=NoRotate())
    assert auth.refresh("rt_keep").refresh_token == "rt_keep"


def test_logout_passes_the_id_token_hint():
    auth, client = auth_for()
    session = auth.callback(code="abc", state=login(auth))
    url = auth.logout_url(session, redirect_to="http://localhost:8770/")

    assert url == "https://auth/bye"
    assert client.seen["logout"].id_token_hint == "it_live"
    assert client.seen["logout"].post_logout_redirect_uri == "http://localhost:8770/"


# --- the reason this is Scalekit and not Firebase --------------------------


def test_the_session_identifier_is_what_the_tool_apis_take():
    """No mapping layer: the authenticated subject *is* the Scalekit identifier, so the
    login identity and the tool tokens cannot drift apart."""
    from skillforge.adapters.scalekit_client import ScalekitScopedClient

    auth, _ = auth_for()
    session = auth.callback(code="abc", state=login(auth))

    calls = []

    class Recording:
        def list_tools(self, identifier):
            calls.append(identifier)
            return []

    ScalekitScopedClient(Recording(), session.identifier).granted_tools()
    assert calls == [session.identifier]


def test_an_unverified_user_payload_can_never_become_the_identifier():
    """Regression. The response's `user` object is attacker-controlled; a token with no
    email claim must fall back to `sub`, never to that payload. An earlier version did,
    and the effect was that an unsigned email chose whose records the bot acts on."""
    for claims, expected in [
        ({"sub": "usr_9912"}, "usr_9912"),
        ({"sub": "usr_9912", "name": "Dr Rao"}, "usr_9912"),
        ({"sub": "usr_9912", "email": ""}, "usr_9912"),
    ]:
        auth, _ = auth_for(claims=claims)
        session = auth.callback(code="abc", state=login(auth))
        assert session.identifier == expected
        assert "evil" not in session.identifier
        assert "evil" not in session.email
        assert "Someone Else" not in session.name
