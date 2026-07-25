"""The web layer, driven over real HTTP.

A real socket and a real cookie jar, because the things most likely to be wrong here are
things a unit test on the handler would not see: cookie attributes, redirect targets,
and whether an unauthenticated request can reach a file it shouldn't.
"""

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from skillforge.adapters.auth import AuthError, Session
from skillforge.ui.serve import COOKIE, SessionStore, make_server

SESSION = Session(identifier="priya.rao@clinic.test", email="priya.rao@clinic.test",
                  name="Dr Priya Rao", access_token="at_secret",
                  refresh_token="rt_secret", id_token="it_secret")


class FakeAuth:
    """Stands in for the Scalekit-backed Auth, with the same surface."""

    def __init__(self, *, configured=True, fail_callback=None):
        self.configured = configured
        self.fail_callback = fail_callback
        self.states: list[str] = []
        self.seen: dict = {}

    def login_url(self, *, signup=False):
        if not self.configured:
            raise AuthError("missing credentials: SCALEKIT_CLIENT_ID")
        state = f"state-{len(self.states)}"
        self.states.append(state)
        self.seen["signup"] = signup
        return f"https://auth.example/authorize?state={state}", state

    def callback(self, *, code, state):
        self.seen["code"], self.seen["state"] = code, state
        if self.fail_callback:
            raise AuthError(self.fail_callback)
        if state not in self.states:
            raise AuthError("unrecognised state — refusing this callback")
        return SESSION

    def logout_url(self, session, *, redirect_to=None):
        self.seen["logout_redirect"] = redirect_to
        return "https://auth.example/logout"


@pytest.fixture
def site(tmp_path):
    """A running server, plus a client that keeps cookies like a browser."""
    root = tmp_path / "build"
    root.mkdir()
    (root / "consult.html").write_text("<h1>Consultation</h1>")
    (root / "signin.html").write_text("<h1>Sign in</h1><a href='/auth/signup'>Create</a>")
    (root / "events.json").write_text('{"ok": true}')
    (tmp_path / "secret.txt").write_text("must never be served")

    made = {}

    def _make(**kwargs):
        auth = kwargs.pop("auth", None) or FakeAuth()
        store = SessionStore()
        server = make_server(root=root, auth=auth, host="127.0.0.1", port=0,
                            store=store, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), NoRedirect())

        made.update(server=server, auth=auth, store=store, base=base, jar=jar,
                    opener=opener, root=root)
        return made

    yield _make
    if "server" in made:
        made["server"].shutdown()
        made["server"].server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow nothing — the redirect targets are what's under test."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(made, path, *, follow=False):
    opener = (urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(made["jar"])) if follow else made["opener"])
    try:
        return opener.open(made["base"] + path)
    except urllib.error.HTTPError as e:
        return e


def cookie_header(response):
    return response.headers.get("Set-Cookie") or ""


def sign_in(made):
    """Complete a login, leaving the jar holding a session cookie."""
    fetch(made, "/auth/login")
    state = made["auth"].states[-1]
    return fetch(made, f"/auth/callback?code=abc&state={state}")


# --- the gate ---------------------------------------------------------------


def test_an_unauthenticated_request_is_sent_to_login(site):
    made = site()
    r = fetch(made, "/")

    assert r.status == 302
    assert r.headers["Location"] == "/signin"


def test_the_page_itself_is_not_reachable_unauthenticated(site):
    """The redirect must not be cosmetic — the file must not be served."""
    made = site()
    r = fetch(made, "/consult.html")

    assert r.status == 302
    assert b"Consultation" not in (r.read() or b"")


def test_data_files_are_behind_the_gate_too(site):
    made = site()
    assert fetch(made, "/events.json").status == 302


def test_public_paths_can_be_opted_out(site):
    made = site(public_paths=frozenset({"/events.json"}))
    r = fetch(made, "/events.json")
    assert r.status == 200
    assert json.loads(r.read())["ok"] is True


# --- login ------------------------------------------------------------------


def test_login_redirects_to_the_provider(site):
    made = site()
    r = fetch(made, "/auth/login")

    assert r.status == 302
    assert r.headers["Location"].startswith("https://auth.example/authorize")


def test_login_says_so_plainly_when_unconfigured(site):
    """Missing credentials are a deployment problem, not the doctor's. They land on the
    sign-in page, which explains it and disables the buttons — not a bare 503 that can
    only be reloaded."""
    made = site(auth=FakeAuth(configured=False))
    r = fetch(made, "/auth/login")

    assert r.status == 302
    assert r.headers["Location"] == "/signin?unconfigured=1"


def test_an_unconfigured_server_still_refuses_entry(site):
    """Failing open would be the worst possible response to missing credentials."""
    made = site(auth=FakeAuth(configured=False))
    assert fetch(made, "/").status == 302


# --- the callback and the cookie -------------------------------------------


def test_a_successful_callback_sets_a_session_cookie_and_lands_home(site):
    made = site()
    r = sign_in(made)

    assert r.status == 302
    assert r.headers["Location"] == "/"
    assert made["auth"].seen["code"] == "abc"
    assert len(made["store"]) == 1


def test_the_cookie_is_httponly_lax_and_scoped(site):
    made = site()
    header = cookie_header(sign_in(made))

    assert COOKIE in header
    assert "HttpOnly" in header, "script-readable session cookie"
    assert "SameSite=Lax" in header, "Strict would drop the cookie on the callback hop"
    assert "Path=/" in header
    assert "Max-Age=" in header


def test_the_cookie_is_not_marked_secure_over_plain_http(site):
    """Localhost is HTTP; a Secure flag would stop the browser storing it at all, and the
    symptom is a login that loops."""
    made = site()
    assert "Secure" not in cookie_header(sign_in(made))


def test_the_cookie_carries_an_opaque_id_not_a_token(site):
    """A stolen cookie must be worth nothing once the session is dropped."""
    made = site()
    header = cookie_header(sign_in(made))

    for secret in ("at_secret", "rt_secret", "it_secret"):
        assert secret not in header
    assert "priya" not in header.lower(), "cookie leaks who the user is"


def test_a_bad_state_is_rejected_with_no_session_created(site):
    made = site()
    fetch(made, "/auth/login")
    r = fetch(made, "/auth/callback?code=abc&state=forged")

    assert r.status == 302
    assert "/signin?error=" in r.headers["Location"]
    assert len(made["store"]) == 0
    assert COOKIE not in cookie_header(r)


def test_a_declined_sign_in_reports_the_providers_reason(site):
    made = site()
    r = fetch(made, "/auth/callback?error=access_denied")

    assert r.status == 302
    assert "access_denied" in r.headers["Location"]
    assert len(made["store"]) == 0


def test_a_failed_exchange_does_not_create_a_session(site):
    made = site(auth=FakeAuth(fail_callback="token rejected"))
    fetch(made, "/auth/login")
    r = fetch(made, f"/auth/callback?code=abc&state={made['auth'].states[-1]}")

    assert r.status == 302
    assert "/signin?error=" in r.headers["Location"]
    assert len(made["store"]) == 0


# --- signed in --------------------------------------------------------------


def test_the_page_is_served_once_signed_in(site):
    made = site()
    sign_in(made)
    r = fetch(made, "/", follow=True)

    assert r.status == 200
    assert b"Consultation" in r.read()


def test_me_returns_the_public_view_and_no_tokens(site):
    made = site()
    sign_in(made)
    r = fetch(made, "/me", follow=True)

    body = r.read().decode()
    payload = json.loads(body)
    assert payload["identifier"] == "priya.rao@clinic.test"
    assert payload["initials"] == "DP"
    for secret in ("at_secret", "rt_secret", "it_secret"):
        assert secret not in body


def test_me_is_401_when_signed_out(site):
    made = site()
    r = fetch(made, "/me")
    assert r.status == 401
    assert json.loads(r.read())["identifier"] is None


def test_an_unknown_cookie_is_treated_as_signed_out(site):
    made = site()
    request = urllib.request.Request(made["base"] + "/me",
                                     headers={"Cookie": f"{COOKIE}=made-up"})
    try:
        urllib.request.urlopen(request)
        assert False, "a forged cookie was accepted"
    except urllib.error.HTTPError as e:
        assert e.status == 401


def test_an_expired_session_is_treated_as_signed_out(site):
    made = site()
    sign_in(made)
    made["store"].ttl = -1                       # everything is now in the past
    sid = next(iter(made["store"]._rows))
    made["store"]._rows[sid] = (SESSION, 0.0)

    assert made["store"].get(sid) is None
    assert fetch(made, "/").status == 302


# --- logout -----------------------------------------------------------------


def test_logout_drops_the_session_and_clears_the_cookie(site):
    made = site()
    sign_in(made)
    assert len(made["store"]) == 1

    r = fetch(made, "/auth/logout", follow=False)

    assert r.status == 302
    assert len(made["store"]) == 0, "session survived logout server-side"
    assert "Max-Age=0" in cookie_header(r)
    assert r.headers["Location"] == "https://auth.example/logout"


def test_logout_still_works_when_the_provider_call_fails(site):
    """Being unable to reach the provider must not leave someone signed in locally."""
    class Broken(FakeAuth):
        def logout_url(self, session, *, redirect_to=None):
            raise RuntimeError("provider unreachable")

    made = site(auth=Broken())
    sign_in(made)
    r = fetch(made, "/auth/logout")

    assert r.status == 302
    assert r.headers["Location"] == "/signin?signedout=1"
    assert len(made["store"]) == 0


def test_logout_while_signed_out_is_harmless(site):
    made = site()
    r = fetch(made, "/auth/logout")
    assert r.status == 302


# --- static serving ---------------------------------------------------------


@pytest.mark.parametrize("attack", [
    "/../secret.txt",
    "/%2e%2e/secret.txt",
    "/subdir/../../secret.txt",
])
def test_path_traversal_cannot_escape_the_build_directory(site, attack):
    """Custom routing means no inherited traversal protection, so this is explicit."""
    made = site()
    sign_in(made)
    r = fetch(made, attack, follow=True)

    assert r.status in (403, 404), f"{attack} returned {r.status}"
    assert b"must never be served" not in (r.read() or b"")


def test_a_missing_file_is_a_404_not_a_crash(site):
    made = site()
    sign_in(made)
    assert fetch(made, "/nope.js", follow=True).status == 404


def test_the_page_is_served_uncached(site):
    """It changes while open; a cached copy makes the UI look frozen."""
    made = site()
    sign_in(made)
    r = fetch(made, "/", follow=True)
    assert "no-store" in r.headers.get("Cache-Control", "")


# --- the session store on its own -------------------------------------------


def test_ids_are_unguessable_and_unique():
    store = SessionStore()
    ids = {store.put(SESSION) for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) > 30 for i in ids)


def test_a_supplied_store_is_the_one_actually_used(tmp_path):
    """Regression. An empty SessionStore is falsy — it defines __len__ — so `store or
    SessionStore()` handed the server a different store than the caller's, and sessions
    landed somewhere nobody was looking."""
    store = SessionStore()
    server = make_server(root=tmp_path, auth=FakeAuth(), port=0, store=store)
    try:
        assert server.RequestHandlerClass.store is store
    finally:
        server.server_close()


def test_dropping_an_unknown_id_is_harmless():
    store = SessionStore()
    store.drop("nothing")
    store.drop(None)
    assert len(store) == 0


# --- sign in, sign up, sign out as a doctor experiences them -----------------


def test_the_sign_in_screen_is_reachable_signed_out(site):
    """The one page reachable without a session. Served rather than bounced straight to
    the provider — an instant redirect to an unfamiliar domain is how a login looks
    broken."""
    made = site()
    r = fetch(made, "/signin")

    assert r.status == 200
    assert b"Sign in" in r.read()


def test_the_gate_lands_on_the_sign_in_screen(site):
    made = site()
    assert fetch(made, "/consult.html").headers["Location"] == "/signin"


def test_sign_up_asks_the_provider_for_the_create_account_screen(site):
    """`prompt=create` is a hint about which screen to show, not a separate flow."""
    made = site()
    r = fetch(made, "/auth/signup")

    assert r.status == 302
    assert r.headers["Location"].startswith("https://auth.example/authorize")
    assert made["auth"].seen["signup"] is True


def test_sign_in_does_not_ask_for_the_create_account_screen(site):
    made = site()
    fetch(made, "/auth/login")
    assert made["auth"].seen["signup"] is False


def test_a_new_account_comes_back_through_the_same_callback(site):
    """Sign-up and sign-in must converge. If they had separate callbacks the two would
    drift, and the one used less often would be the one that broke."""
    made = site()
    fetch(made, "/auth/signup")
    r = fetch(made, f"/auth/callback?code=abc&state={made['auth'].states[-1]}")

    assert r.status == 302
    assert r.headers["Location"] == "/"
    assert len(made["store"]) == 1


def test_signing_out_says_so_rather_than_looping(site):
    """Landing back on `/` after logout would immediately redirect to sign-in, which
    reads as the sign-out having failed."""
    made = site(auth=type("NoProvider", (FakeAuth,), {
        "logout_url": lambda self, s, **k: (_ for _ in ()).throw(RuntimeError("none"))})())
    sign_in(made)
    r = fetch(made, "/auth/logout")

    assert r.headers["Location"] == "/signin?signedout=1"


def test_the_page_is_reachable_again_after_signing_back_in(site):
    """The whole round trip, because each half passing separately has proven nothing
    about the pair."""
    made = site()
    sign_in(made)
    assert fetch(made, "/", follow=True).status == 200

    fetch(made, "/auth/logout")
    assert fetch(made, "/").status == 302, "still signed in after signing out"

    sign_in(made)
    assert b"Consultation" in fetch(made, "/", follow=True).read()


def test_a_dropped_session_cannot_be_revived_by_the_old_cookie(site):
    """The reason the cookie holds an opaque id: signing out must make the cookie
    worthless, not merely ask the browser to forget it."""
    made = site()
    sign_in(made)
    stolen = next(iter(made["store"]._rows))
    fetch(made, "/auth/logout")

    request = urllib.request.Request(made["base"] + "/me",
                                     headers={"Cookie": f"{COOKIE}={stolen}"})
    try:
        urllib.request.urlopen(request)
        assert False, "a logged-out session id still works"
    except urllib.error.HTTPError as e:
        assert e.status == 401


# --- the meeting webhook -----------------------------------------------------

import hashlib
import hmac

from skillforge.adapters.meetstream import Binding
from skillforge.ui.serve import LiveConsultations

HOOK_BINDING = Binding(consultation_id="con-0912", patient_id="PT-10482",
                       patient_name="Amara Okafor",
                       clinician="priya.rao@clinic.test",
                       clinician_name="Dr Priya Rao", crm_id="hs-contact-88412")
SECRET = "hook-s3cret"


def hooked(made, path, payload, *, secret=SECRET, sign=True):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-MeetStream-Signature"] = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(made["base"] + path, data=body,
                                     headers=headers, method="POST")
    try:
        return urllib.request.urlopen(request)
    except urllib.error.HTTPError as e:
        return e


def turn(text, speaker="Amara Okafor", final=True, binding=HOOK_BINDING):
    return {"bot_id": "bot-1", "speakerName": speaker,
            "timestamp": "2026-07-25T09:20:30.354452",
            "transcript": text, "new_text": text, "utterance": "",
            "end_of_turn": final, "transcription_mode": "word_level",
            "custom_attributes": binding.to_attributes() if binding else {}}


@pytest.fixture
def hooks(site):
    live = LiveConsultations()
    made = site(consultations=live, hook_secret=SECRET)
    made["live"] = live
    return made


def test_a_signed_transcript_turn_is_accepted(hooks):
    r = hooked(hooks, "/hooks/transcript", turn("The morning readings are higher."))

    assert r.status == 200
    assert json.loads(r.read())["kept"] is True
    room = hooks["live"].get("con-0912")
    assert room["said"][0]["text"] == "The morning readings are higher."
    assert room["patient_id"] == "PT-10482"


def test_an_unsigned_webhook_is_refused(hooks):
    """This endpoint is outside the session gate by necessity — MeetStream has no cookie.
    An open endpoint here does not leak data, it *injects* it, into a patient record."""
    r = hooked(hooks, "/hooks/transcript", turn("Injected."), sign=False)

    assert r.status == 401
    assert hooks["live"].rooms == {}


def test_a_wrongly_signed_webhook_is_refused(hooks):
    r = hooked(hooks, "/hooks/transcript", turn("Injected."), secret="wrong-secret")

    assert r.status == 401
    assert hooks["live"].rooms == {}


def test_an_unconfigured_secret_refuses_everything(site):
    """Failing open when unconfigured would be silent, and it is the worst direction."""
    live = LiveConsultations()
    made = site(consultations=live, hook_secret="")
    assert hooked(made, "/hooks/transcript", turn("x")).status == 401
    assert live.rooms == {}


def test_interim_words_are_accepted_but_not_kept(hooks):
    """MeetStream does not retry non-2xx, so an interim event must still get a 2xx —
    it is simply not part of the transcript."""
    r = hooked(hooks, "/hooks/transcript", turn("The morn", final=False))

    assert r.status == 200
    assert json.loads(r.read())["kept"] is False
    assert hooks["live"].rooms == {}


def test_a_turn_with_no_binding_is_dropped(hooks):
    """Nothing guesses which consultation an unbound line belongs to."""
    r = hooked(hooks, "/hooks/transcript", turn("Whose is this?", binding=None))

    assert r.status == 200
    assert hooks["live"].rooms == {}


def test_lifecycle_events_move_the_consultation_status(hooks):
    base = {"bot_id": "bot-1", "timestamp": "2026-07-25T09:20:04",
            "status_code": 200, "message": "",
            "custom_attributes": HOOK_BINDING.to_attributes()}

    hooked(hooks, "/hooks/bot", {**base, "bot_event": "bot.inmeeting"})
    assert hooks["live"].get("con-0912")["status"] == "live"

    hooked(hooks, "/hooks/bot", {**base, "bot_event": "bot.stopped"})
    assert hooks["live"].get("con-0912")["status"] == "ended"


def test_a_refused_bot_is_distinguished_from_a_finished_one(hooks):
    """"The consultation ended" and "the host never let us in" need different words."""
    hooked(hooks, "/hooks/bot", {
        "bot_event": "bot.denied", "bot_id": "bot-1", "status_code": 500,
        "message": "Host rejected the join request",
        "timestamp": "2026-07-25T09:20:04",
        "custom_attributes": HOOK_BINDING.to_attributes()})

    room = hooks["live"].get("con-0912")
    assert room["status"] == "ended"
    assert "rejected" in room["refused_reason"]


def test_an_oversized_body_is_refused_without_reading_it(hooks):
    """A transcript turn is a sentence. Reading an unbounded Content-Length from an
    unauthenticated caller is how a public endpoint becomes a memory-exhaustion bug.

    Refusing *without reading* means the server may close before the client finishes
    sending, so a connection-level error is a pass here too — the point is that the body
    was never consumed, not which layer noticed."""
    body = b"x" * (300 * 1024)
    request = urllib.request.Request(
        hooks["base"] + "/hooks/transcript", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request)
        assert False, "a 300KB 'transcript turn' was accepted"
    except urllib.error.HTTPError as e:
        assert e.status == 413
    except (urllib.error.URLError, ConnectionError, BrokenPipeError):
        pass                            # refused mid-upload, which is the intent
    assert hooks["live"].rooms == {}, "an oversized body still reached the store"


def test_an_unknown_post_route_is_404(hooks):
    assert hooked(hooks, "/hooks/nope", {}).status == 404


# --- reading the transcript back --------------------------------------------


def test_live_transcript_is_behind_the_gate(hooks):
    hooked(hooks, "/hooks/transcript", turn("Private."))
    r = fetch(hooks, "/live")

    assert r.status == 401
    assert b"Private" not in r.read()


def test_a_signed_in_clinician_sees_their_own_consultation(hooks):
    hooked(hooks, "/hooks/transcript", turn("The morning readings are higher."))
    sign_in(hooks)
    r = fetch(hooks, "/live", follow=True)

    rooms = json.loads(r.read())["consultations"]
    assert len(rooms) == 1
    assert rooms[0]["said"][0]["text"] == "The morning readings are higher."


def test_another_clinicians_consultation_is_not_visible(hooks):
    """The binding names whose bot it is. Without this filter one doctor's live
    transcript appears on another's screen."""
    other = Binding(consultation_id="con-9999", patient_id="PT-77777",
                    patient_name="Someone Else", clinician="james@clinic.test",
                    clinician_name="Dr James Whitfield")
    hooked(hooks, "/hooks/transcript", turn("Not yours.", binding=other))
    hooked(hooks, "/hooks/transcript", turn("Yours."))

    sign_in(hooks)                      # signs in as priya.rao@clinic.test
    rooms = json.loads(fetch(hooks, "/live", follow=True).read())["consultations"]

    assert [r["consultation_id"] for r in rooms] == ["con-0912"]
    assert "Not yours." not in json.dumps(rooms)


# --- patients the doctor adds ------------------------------------------------

from skillforge.ui.serve import PatientStore


def post_json(made, path, payload):
    request = urllib.request.Request(
        made["base"] + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    for cookie in made["jar"]:
        request.add_header("Cookie", f"{cookie.name}={cookie.value}")
    try:
        return urllib.request.urlopen(request)
    except urllib.error.HTTPError as e:
        return e


@pytest.fixture
def clinic(site, tmp_path):
    store = PatientStore(path=tmp_path / "patients.json")
    made = site(patients=store)
    made["patients"] = store
    return made


def test_the_patient_list_starts_empty(clinic):
    """No baked-in patients. The first-run state is an empty list, and the UI must not
    quietly fall back to demo people who are not this doctor's."""
    sign_in(clinic)
    r = fetch(clinic, "/patients", follow=True)
    assert json.loads(r.read())["patients"] == []


def test_adding_a_patient_needs_only_a_name(clinic):
    sign_in(clinic)
    r = post_json(clinic, "/patients", {"name": "Amara Okafor"})

    assert r.status == 200
    patient = json.loads(r.read())["patient"]
    assert patient["name"] == "Amara Okafor"
    assert patient["id"].startswith("PT-")


def test_a_new_patient_is_clinically_empty(clinic):
    """Conditions and history are what the consultations write. Asking the doctor to
    type them up front would be asking them to do the product's job."""
    sign_in(clinic)
    patient = json.loads(post_json(clinic, "/patients",
                                   {"name": "Grace Mensah"}).read())["patient"]

    assert patient["conditions"] == []
    assert patient["crm_id"] is None
    assert patient["consultations"] == 0


def test_a_patient_without_a_name_is_refused(clinic):
    sign_in(clinic)
    r = post_json(clinic, "/patients", {"name": "   "})
    assert r.status == 400
    assert "name" in json.loads(r.read())["error"].lower()


def test_a_malformed_email_is_refused(clinic):
    """This address is where a medical summary gets sent, and a typo there is the
    wrong-recipient failure with no undo."""
    sign_in(clinic)
    r = post_json(clinic, "/patients", {"name": "Nia Patel", "email": "not-an-address"})
    assert r.status == 400


def test_adding_a_patient_requires_a_session(clinic):
    r = post_json(clinic, "/patients", {"name": "Nobody"})
    assert r.status == 401
    assert clinic["patients"].rows == {}


def test_patients_survive_a_restart(clinic, tmp_path):
    """A session vanishing on restart is an inconvenience; a patient list vanishing
    means re-typing every record, and nobody uses that twice."""
    sign_in(clinic)
    post_json(clinic, "/patients", {"name": "Tomas Lindqvist"})

    reopened = PatientStore(path=tmp_path / "patients.json")
    assert [p["name"] for p in reopened.list("priya.rao@clinic.test")] \
        == ["Tomas Lindqvist"]


def test_a_corrupt_patients_file_does_not_stop_the_app(tmp_path):
    """Starting empty is recoverable. Refusing to start is not."""
    path = tmp_path / "patients.json"
    path.write_text("{not json")
    assert PatientStore(path=path).list("anyone") == []


def test_one_doctors_patients_are_not_another_doctors(clinic):
    clinic["patients"].add("james@clinic.test", name="Someone Else")
    sign_in(clinic)

    listed = json.loads(fetch(clinic, "/patients", follow=True).read())["patients"]
    assert listed == []


# --- sending the bot ---------------------------------------------------------


def test_sending_a_bot_requires_a_session(clinic):
    r = post_json(clinic, "/bot/send", {"meeting_link": "https://meet.google.com/x",
                                        "patient_id": "PT-10001"})
    assert r.status == 401


def test_a_bot_cannot_be_sent_for_a_patient_who_is_not_yours(clinic):
    """The patient id comes from the request, so it is checked against *this* doctor's
    list rather than trusted."""
    clinic["patients"].add("james@clinic.test", name="Someone Else")
    sign_in(clinic)

    r = post_json(clinic, "/bot/send", {"meeting_link": "https://meet.google.com/x",
                                        "patient_id": "PT-10001"})
    assert r.status == 400
    assert "your list" in json.loads(r.read())["error"]


def test_a_meeting_link_must_look_like_a_url(clinic):
    sign_in(clinic)
    post_json(clinic, "/patients", {"name": "Amara Okafor"})
    r = post_json(clinic, "/bot/send", {"meeting_link": "not a link",
                                        "patient_id": "PT-10001"})
    assert r.status == 400


def test_without_a_public_url_the_reason_is_stated(clinic):
    """MeetStream cannot deliver a transcript to 127.0.0.1. Saying so beats dispatching
    a bot whose words can never come back."""
    sign_in(clinic)
    post_json(clinic, "/patients", {"name": "Amara Okafor"})
    r = post_json(clinic, "/bot/send", {"meeting_link": "https://meet.google.com/abc",
                                        "patient_id": "PT-10001"})

    assert r.status == 503
    assert "tunnel" in json.loads(r.read())["error"].lower()


def test_a_consultation_survives_a_restart(tmp_path):
    """The one kind of data here that cannot be re-typed. A patient list lost to a restart
    is an afternoon; a consultation lost to a restart means the conversation happened and
    nothing remains of it."""
    from skillforge.adapters.meetstream import Utterance
    from skillforge.ui.serve import LiveConsultations

    path = tmp_path / "consultations.json"
    store = LiveConsultations(path=path)
    store.said(Utterance(text="The morning readings are higher.", speaker="Amara Okafor",
                         role="patient", at="2026-07-25T09:20:30", binding=HOOK_BINDING))

    reopened = LiveConsultations(path=path)
    room = reopened.get("con-0912")
    assert room["said"][0]["text"] == "The morning readings are higher."
    assert room["patient_id"] == "PT-10482"


def test_patients_follow_a_changed_identifier(tmp_path):
    """Fixing identity resolution changes the identifier — an opaque sub becomes an email
    — and anything keyed by the old value silently disappears. That happened for real."""
    store = PatientStore(path=tmp_path / "patients.json")
    store.add("usr_9912", name="Sara")
    store.add("usr_9912", name="Tomas")

    found = store.list("priya@clinic.test", aliases=["priya@clinic.test", "usr_9912"])
    assert [p["name"] for p in found] == ["Sara", "Tomas"]
    assert store.list("usr_9912") == [], "rows were copied rather than moved"
    assert [p["id"] for p in found] == ["PT-10001", "PT-10002"], "ids collided on merge"
