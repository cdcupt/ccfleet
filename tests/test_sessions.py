"""Signing in: the cookie, the Google flow, and what the database keeps.

The console has never had a session — it uses HTTP basic auth. Everything here
exists because that is not acceptable for something the public signs into.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse

import pytest

from ccfleetd import oauth, sessions
from ccfleetd.store import Store, StoreError

SECRET = "0123456789abcdef0123456789abcdef"
NOW = 1_700_000_000.0


@pytest.fixture
def store():
    st = Store(":memory:")
    st.add_account("a1", "google-123", "erik@example.com", now=NOW)
    yield st
    st.close()


# -- the cookie ---------------------------------------------------------------

def test_a_signed_cookie_survives_a_round_trip():
    sid = sessions.new_session_id()
    assert sessions.unsign(sessions.sign(sid, SECRET), SECRET) == sid


def test_a_forged_session_id_fails_at_the_door():
    """Without a signature the cookie is just a string the browser writes, and
    the only thing standing between a guess and a session is a table lookup."""
    real = sessions.new_session_id()
    forged = sessions.new_session_id()
    _, _, mac = sessions.sign(real, SECRET).partition(".")
    with pytest.raises(sessions.SessionError):
        sessions.unsign(f"{forged}.{mac}", SECRET)


def test_a_cookie_signed_with_another_key_is_refused():
    """Rotating the secret has to sign everybody out; that is what makes it
    worth doing when one leaks."""
    value = sessions.sign(sessions.new_session_id(), SECRET)
    with pytest.raises(sessions.SessionError):
        sessions.unsign(value, "a-different-secret-entirely")


@pytest.mark.parametrize("value", ["", "no-dot", ".onlymac", "a.b.c", "x."])
def test_a_malformed_cookie_is_refused_rather_than_parsed(value):
    with pytest.raises(sessions.SessionError):
        sessions.unsign(value, SECRET)


def test_signing_without_a_secret_is_refused_rather_than_done_badly():
    """An empty key would produce a perfectly valid-looking signature that
    every deployment which forgot to configure one could also produce."""
    with pytest.raises(sessions.SessionError):
        sessions.sign("abc", "")
    with pytest.raises(sessions.SessionError):
        sessions.unsign("abc.def", "")


def test_the_cookie_carries_the_attributes_that_make_it_safe():
    header = sessions.cookie_header("v", ttl_s=60)
    assert "HttpOnly" in header, "script could read the session"
    assert "SameSite=Lax" in header, "it would ride along on a cross-site POST"
    assert "Secure" in header
    assert "Max-Age=60" in header
    assert "Path=/" in header


def test_the_insecure_cookie_is_only_for_plain_http():
    assert "Secure" not in sessions.cookie_header("v", ttl_s=60, secure=False)


def test_signing_out_matches_the_attributes_it_was_set_with():
    """A browser keeps the original cookie unless the clearing one matches, and
    signing out then appears to do nothing at all."""
    setting = sessions.cookie_header("v", ttl_s=60)
    clearing = sessions.clearing_header()
    for attribute in ("Path=/", "HttpOnly", "SameSite=Lax", "Secure"):
        assert attribute in setting and attribute in clearing
    assert "Max-Age=0" in clearing


def test_our_cookie_is_found_among_other_peoples():
    header = f"_ga=GA1.2.3; {sessions.COOKIE_NAME}=the-value; other=x"
    assert sessions.read_cookie(header) == "the-value"


@pytest.mark.parametrize("header", [None, "", "other=x", f"{sessions.COOKIE_NAME}="])
def test_no_cookie_reads_as_no_cookie(header):
    assert sessions.read_cookie(header) is None


def test_the_database_never_holds_the_session_id_itself():
    sid = sessions.new_session_id()
    assert sessions.hash_session_id(sid) != sid
    assert sessions.hash_session_id(sid) == hashlib.sha256(sid.encode()).hexdigest()


# -- the Google flow ----------------------------------------------------------

def test_the_authorize_url_asks_for_nothing_beyond_identity():
    """An unused permission is still a permission somebody granted us."""
    url = oauth.authorize_url(client_id="cid", redirect_uri="https://x/cb",
                              state="st", verifier="v" * 43)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["scope"] == ["openid email"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["st"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["prompt"] == ["select_account"]
    # The hash, not the secret. Checking the *method* is not enough: a URL that
    # says S256 and carries the verifier itself leaks it into browser history
    # and referrers, which is precisely what PKCE exists to prevent — and no
    # name check catches it, because the value is simply in the wrong field.
    assert query["code_challenge"] == [oauth.challenge_for("v" * 43)]
    assert query["code_challenge"] != ["v" * 43]
    assert "v" * 43 not in url, "the verifier must not leave the server"


def test_the_pkce_challenge_is_the_hash_and_not_the_verifier():
    verifier = "v" * 43
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert oauth.challenge_for(verifier) == expected
    assert oauth.challenge_for(verifier) != verifier
    assert "=" not in oauth.challenge_for(verifier), "b64url must be unpadded"


def test_states_and_verifiers_are_not_guessable():
    assert len({oauth.new_state() for _ in range(200)}) == 200
    assert len({oauth.new_verifier() for _ in range(200)}) == 200
    assert len(oauth.new_state()) >= 32


def test_sign_in_is_refused_rather_than_offered_unconfigured():
    with pytest.raises(oauth.OAuthError):
        oauth.authorize_url(client_id="", redirect_uri="https://x/cb",
                            state="s", verifier="v")
    with pytest.raises(oauth.OAuthError):
        oauth.authorize_url(client_id="cid", redirect_uri="", state="s", verifier="v")


class _Reply:
    def __init__(self, payload, record=None):
        self._payload = payload if isinstance(payload, bytes) else json.dumps(
            payload).encode()
        self.record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _opener(payload, seen=None):
    def open_it(request, timeout=None):
        if seen is not None:
            seen.append(request)
        return _Reply(payload)
    return open_it


def test_exchanging_a_code_sends_the_verifier_and_returns_the_token():
    seen = []
    token = oauth.exchange_code(
        code="the-code", verifier="the-verifier", client_id="cid",
        client_secret="secret", redirect_uri="https://x/cb",
        opener=_opener({"access_token": "at-123"}, seen))
    assert token == "at-123"
    sent = urllib.parse.parse_qs(seen[0].data.decode())
    assert sent["code"] == ["the-code"]
    assert sent["code_verifier"] == ["the-verifier"]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["client_secret"] == ["secret"]
    assert seen[0].full_url == oauth.TOKEN_ENDPOINT


def test_a_reply_with_no_token_is_an_error_not_an_empty_string():
    with pytest.raises(oauth.OAuthError, match="no access token"):
        oauth.exchange_code(code="c", verifier="v", client_id="i",
                            client_secret="s", redirect_uri="r",
                            opener=_opener({"error": "invalid_grant"}))


def test_an_empty_code_never_reaches_google():
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code(code="", verifier="v", client_id="i",
                            client_secret="s", redirect_uri="r",
                            opener=_opener({"access_token": "x"}))


def test_googles_own_reason_survives_into_the_error():
    """"invalid_grant" and "redirect_uri_mismatch" are the two that actually
    happen, and neither is guessable from a bare 400."""
    def fails(request, timeout=None):
        raise urllib.error.HTTPError(
            "u", 400, "Bad Request", {},
            __import__("io").BytesIO(b'{"error":"redirect_uri_mismatch"}'))
    with pytest.raises(oauth.OAuthError, match="redirect_uri_mismatch"):
        oauth.exchange_code(code="c", verifier="v", client_id="i",
                            client_secret="s", redirect_uri="r", opener=fails)


def test_something_that_is_not_json_is_not_treated_as_an_identity():
    with pytest.raises(oauth.OAuthError, match="not JSON"):
        oauth.exchange_code(code="c", verifier="v", client_id="i",
                            client_secret="s", redirect_uri="r",
                            opener=_opener(b"<html>a proxy login page</html>"))


def test_the_identity_comes_back_with_the_address_lowercased():
    seen = []
    who = oauth.fetch_identity("at-123", opener=_opener(
        {"sub": "google-123", "email": "Erik@Example.COM",
         "email_verified": True}, seen))
    assert who == {"sub": "google-123", "email": "erik@example.com"}
    assert seen[0].get_header("Authorization") == "Bearer at-123"


def test_an_unverified_address_is_refused():
    """Google hands back addresses people have not proved they own. The
    operator grants allowances by address, so registering as somebody else's
    is not a cosmetic problem."""
    with pytest.raises(oauth.OAuthError, match="not verified|has not verified"):
        oauth.fetch_identity("at", opener=_opener(
            {"sub": "s", "email": "someone@example.com", "email_verified": False}))
    with pytest.raises(oauth.OAuthError):
        oauth.fetch_identity("at", opener=_opener(
            {"sub": "s", "email": "someone@example.com"}))


@pytest.mark.parametrize("payload", [
    {"email": "a@b.c", "email_verified": True},          # no subject
    {"sub": "", "email": "a@b.c", "email_verified": True},
    {"sub": "s", "email_verified": True},                 # no address
    {"sub": "s", "email": "not-an-address", "email_verified": True},
])
def test_an_identity_missing_what_we_key_on_is_refused(payload):
    with pytest.raises(oauth.OAuthError):
        oauth.fetch_identity("at", opener=_opener(payload))


# -- what the database keeps --------------------------------------------------

def test_a_pending_sign_in_can_be_taken_exactly_once(store):
    """A callback URL lands in browser history, in referrers and in any proxy
    log on the way. Replaying it must not start a second sign-in."""
    store.begin_oauth_flow("st", "verifier", now=NOW, ttl_s=600, next_url="/slots")
    first = store.take_oauth_flow("st", now=NOW + 1)
    assert first["verifier"] == "verifier" and first["next_url"] == "/slots"
    assert store.take_oauth_flow("st", now=NOW + 2) is None


def test_a_sign_in_left_in_an_open_tab_goes_stale(store):
    store.begin_oauth_flow("st", "v", now=NOW, ttl_s=600)
    assert store.take_oauth_flow("st", now=NOW + 601) is None
    # and is gone, not merely refused
    assert store.take_oauth_flow("st", now=NOW) is None


def test_a_callback_for_a_flow_we_never_started(store):
    assert store.take_oauth_flow("invented", now=NOW) is None


def test_stale_flows_are_swept(store):
    store.begin_oauth_flow("old", "v", now=NOW, ttl_s=60)
    store.begin_oauth_flow("new", "v", now=NOW, ttl_s=6000)
    assert store.purge_oauth_flows(now=NOW + 600) == 1
    assert store.take_oauth_flow("new", now=NOW + 600) is not None


def test_a_session_is_stored_as_a_hash_and_nowhere_as_itself(store):
    sid = store.create_session("a1", now=NOW, ttl_s=3600)
    with store._lock:
        rows = store._conn.execute("SELECT * FROM sessions").fetchall()
    assert len(rows) == 1
    assert rows[0]["id_hash"] == sessions.hash_session_id(sid)
    assert sid not in str(tuple(rows[0]))


def test_a_session_names_its_account_and_marks_it_seen(store):
    sid = store.create_session("a1", now=NOW, ttl_s=3600)
    account = store.account_for_session(sid, now=NOW + 5)
    assert account["id"] == "a1" and account["email"] == "erik@example.com"
    assert store.get_account("a1")["last_seen_at"] == NOW + 5


def test_an_expired_session_is_deleted_the_first_time_it_is_presented(store):
    """Not merely ignored until a sweep happens to run: a stolen cookie should
    stop being useful the moment anybody tries it."""
    sid = store.create_session("a1", now=NOW, ttl_s=60)
    assert store.account_for_session(sid, now=NOW + 61) is None
    with store._lock:
        assert store._conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_an_invented_cookie_names_nobody(store):
    assert store.account_for_session(sessions.new_session_id(), now=NOW) is None


def test_a_session_whose_account_is_gone_signs_nobody_in(store):
    sid = store.create_session("a1", now=NOW, ttl_s=3600)
    with store._lock:
        store._conn.execute("DELETE FROM accounts WHERE id = 'a1'")
        store._conn.commit()
    assert store.account_for_session(sid, now=NOW + 1) is None
    with store._lock:
        assert store._conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_a_session_for_nobody_is_refused(store):
    with pytest.raises(StoreError):
        store.create_session("ghost", now=NOW, ttl_s=60)


def test_signing_out_ends_that_session_and_only_that_one(store):
    here = store.create_session("a1", now=NOW, ttl_s=3600)
    elsewhere = store.create_session("a1", now=NOW, ttl_s=3600)
    assert store.end_session(here) is True
    assert store.account_for_session(here, now=NOW) is None
    assert store.account_for_session(elsewhere, now=NOW)["id"] == "a1"


def test_signing_out_everywhere_is_available_for_when_it_is_needed(store):
    ids = [store.create_session("a1", now=NOW, ttl_s=3600) for _ in range(3)]
    assert store.end_all_sessions("a1") == 3
    assert all(store.account_for_session(i, now=NOW) is None for i in ids)


def test_ending_a_session_that_is_already_gone(store):
    assert store.end_session(sessions.new_session_id()) is False


def test_expired_sessions_are_swept(store):
    store.create_session("a1", now=NOW, ttl_s=60)
    kept = store.create_session("a1", now=NOW, ttl_s=6000)
    assert store.purge_sessions(now=NOW + 600) == 1
    assert store.account_for_session(kept, now=NOW + 600)["id"] == "a1"


# -- the routes ---------------------------------------------------------------

@pytest.mark.parametrize("raw,kept", [
    ("/slots", "/slots"),
    ("/slots?tab=1", "/slots?tab=1"),
    ("", ""),
    ("//evil.example/take", ""),        # protocol-relative: the browser leaves
    ("https://evil.example", ""),
    ("http://evil.example", ""),
    ("evil.example", ""),
    ("/\\evil.example", ""),
    ("/ok\nLocation: https://evil.example", ""),
])
def test_sign_in_will_not_send_anybody_off_site(raw, kept):
    """An open redirect on a sign-in route is how a link that genuinely starts
    at our domain finishes somewhere else with the person already signed in."""
    from ccfleetd.api import _safe_next
    assert _safe_next(raw) == kept


@pytest.fixture
def signin(monkeypatch):
    """A server with Google sign-in configured and Google itself stubbed.

    Only the two network calls are replaced. Everything else — the state, the
    single-use flow, the cookie, the account — is the real thing.
    """
    import http.client
    import threading

    from ccfleetd import oauth as oauth_mod
    from ccfleetd.api import Context, build_server
    from ccfleetd.config import Config
    from ccfleetd.monitor import Monitor
    from ccfleetd.notify import LogNotifier

    monkeypatch.setattr(oauth_mod, "exchange_code", lambda **kw: "access-token")
    monkeypatch.setattr(oauth_mod, "fetch_identity",
                        lambda token, **kw: {"sub": "google-9",
                                             "email": "erik@example.com"})
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token="admin-token", public_url="http://127.0.0.1",
                 google_client_id="cid", google_client_secret="secret",
                 cookie_secret=SECRET, cookie_secure=False)
    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    jar: dict[str, str] = {}

    def call(method, path, headers=None, *, browser=True):
        """One request from a browser that keeps its cookies.

        `browser=False` is a *different* browser: same server, no jar. That is
        the whole of the login-CSRF test — the attacker's callback URL opened
        by somebody who did not start the flow.
        """
        hdrs = dict(headers or {})
        if browser and jar and "Cookie" not in hdrs:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                          timeout=10)
        conn.request(method, path, headers=hdrs)
        reply = conn.getresponse()
        reply.read()
        conn.close()
        if browser:
            for header in reply.headers.get_all("Set-Cookie") or []:
                name, _, value = header.split(";")[0].partition("=")
                if value:
                    jar[name] = value
                else:
                    jar.pop(name, None)
        return reply

    yield store, call, jar

    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    store.close()


def _state_from(reply):
    location = reply.getheader("Location")
    assert location.startswith(oauth.AUTH_ENDPOINT), location
    return urllib.parse.parse_qs(
        urllib.parse.urlparse(location).query)["state"][0]


def test_signing_in_creates_an_account_that_can_claim_nothing(signin):
    """The whole flow with Google stubbed: start, callback, session cookie, and
    an account with no allowance until the operator grants one."""
    store, call, _jar = signin
    state = _state_from(call("GET", "/auth/google/start?next=/slots"))

    back = call("GET", f"/auth/google/callback?state={state}&code=abc")
    assert back.status == 303
    assert back.getheader("Location") == "/slots"
    cookie = back.getheader("Set-Cookie")
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie, "a Secure cookie over http is dropped"

    account = store.account_by_google_sub("google-9")
    assert account["email"] == "erik@example.com"
    assert account["slot_quota"] == 0, "signing up must not grant anything"

    # That callback is spent. Replaying it is how a link out of browser history
    # or a proxy log would otherwise start a second sign-in.
    replay = call("GET", f"/auth/google/callback?state={state}&code=abc")
    assert replay.status == 400
    # It may clear the half-finished flow, but it must not hand out a session.
    assert sessions.COOKIE_NAME not in (replay.getheader("Set-Cookie") or "")
    assert len(store.list_accounts()) == 1

    value = cookie.split(";")[0].split("=", 1)[1]
    session_id = sessions.unsign(value, SECRET)
    assert store.account_for_session(session_id, now=NOW)["id"] == account["id"]

    out = call("POST", "/auth/signout",
               {"Cookie": f"{sessions.COOKIE_NAME}={value}",
                "Content-Length": "0"})
    assert out.status == 303
    assert "Max-Age=0" in out.getheader("Set-Cookie")
    assert store.account_for_session(session_id, now=NOW) is None


def test_signing_in_twice_is_the_same_person(signin):
    """Keyed on Google's subject, so a second sign-in finds the account rather
    than making another — and an allowance already granted survives it."""
    store, call, _jar = signin
    for _ in range(2):
        state = _state_from(call("GET", "/auth/google/start"))
        assert call("GET",
                    f"/auth/google/callback?state={state}&code=x").status == 303
        store.set_slot_quota(store.list_accounts()[0]["id"], 3)
    accounts = store.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]["slot_quota"] == 3, "signing in again reset the allowance"


@pytest.mark.parametrize("path", [
    "/auth/google/callback?code=abc",                  # no state at all
    "/auth/google/callback?state=invented&code=abc",   # a state we never issued
])
def test_a_callback_nobody_started_signs_nobody_in(signin, path):
    store, call, _jar = signin
    reply = call("GET", path)
    assert reply.status == 400
    assert sessions.COOKIE_NAME not in (reply.getheader("Set-Cookie") or "")
    assert store.list_accounts() == []


def test_an_off_site_next_is_dropped_rather_than_followed(signin):
    store, call, _jar = signin
    state = _state_from(call("GET", "/auth/google/start?next=" +
                             urllib.parse.quote("https://evil.example/take")))
    back = call("GET", f"/auth/google/callback?state={state}&code=abc")
    assert back.getheader("Location") == "/", "sent the browser off-site"


def test_sign_in_is_not_offered_when_it_is_not_configured():
    """Rather than a button that fails on the callback, which reads as the
    product being broken."""
    import http.client
    import threading

    from ccfleetd.api import Context, build_server
    from ccfleetd.config import Config
    from ccfleetd.monitor import Monitor
    from ccfleetd.notify import LogNotifier

    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token="t")
    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/auth/google/start", "/auth/google/callback?state=x"):
            conn = http.client.HTTPConnection("127.0.0.1",
                                              srv.server_address[1], timeout=10)
            conn.request("GET", path)
            reply = conn.getresponse()
            reply.read()
            conn.close()
            assert reply.status == 503, path
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        store.close()


# -- what an account is keyed on ----------------------------------------------

def test_changing_your_google_address_keeps_your_account(store):
    """People change their email; Google keeps the subject stable. Matching on
    the address would give somebody a fresh, empty account — and their slots
    would still be held by the old one."""
    first = store.upsert_account_from_google("google-1", "old@example.com", now=NOW)
    store.set_slot_quota(first["id"], 4)
    again = store.upsert_account_from_google("google-1", "new@example.com",
                                             now=NOW + 10)
    assert again["id"] == first["id"], "a changed address made a second account"
    assert again["email"] == "new@example.com"
    assert store.get_account(first["id"])["slot_quota"] == 4
    assert len(store.list_accounts()) == 2  # a1 from the fixture, plus this one


def test_an_address_someone_else_now_owns_is_not_their_account(store):
    """The other direction, and the one that matters: corporate domains hand
    addresses on. Keyed on the address, the next holder of erik@ would sign in
    to Erik's account and whatever slots it holds."""
    mine = store.upsert_account_from_google("google-1", "shared@example.com", now=NOW)
    store.set_slot_quota(mine["id"], 4)
    theirs = store.upsert_account_from_google("google-2", "shared@example.com",
                                              now=NOW + 10)
    assert theirs["id"] != mine["id"], "handed over somebody else's account"
    assert theirs["slot_quota"] == 0, "and their allowance with it"


def test_a_callback_opened_in_another_browser_signs_nobody_in(signin):
    """Login CSRF, and the reason `state` alone is not enough. Held only on the
    server it proves the flow was started by *somebody*: an attacker starts
    one, completes it as themselves, and sends the victim the callback URL —
    which would sign the victim into the attacker's account, where the attacker
    can then read whatever the victim does. The state has to be in the browser
    too, and has to match."""
    store, call, jar = signin
    state = _state_from(call("GET", "/auth/google/start"))
    assert sessions.FLOW_COOKIE_NAME in jar, "the flow was never tied to a browser"

    # The victim's browser: no flow of its own.
    victim = call("GET", f"/auth/google/callback?state={state}&code=abc",
                  browser=False)
    assert victim.status == 400
    assert victim.getheader("Set-Cookie") is not None
    assert sessions.COOKIE_NAME not in victim.getheader("Set-Cookie")
    assert store.list_accounts() == [], "signed somebody in"


def test_a_callback_from_a_different_flow_is_refused(signin):
    """The other shape: the victim has a sign-in of their own in progress, so a
    cookie is present — it simply is not the one this callback belongs to."""
    store, call, jar = signin
    stolen = _state_from(call("GET", "/auth/google/start"))
    # A second start replaces the cookie, as another browser's would.
    mine = _state_from(call("GET", "/auth/google/start"))
    assert stolen != mine

    reply = call("GET", f"/auth/google/callback?state={stolen}&code=abc")
    assert reply.status == 400
    assert store.list_accounts() == []


def test_a_tampered_flow_cookie_is_refused(signin):
    store, call, jar = signin
    state = _state_from(call("GET", "/auth/google/start"))
    forged = sessions.sign(state, "some-other-secret")
    reply = call("GET", f"/auth/google/callback?state={state}&code=abc",
                 {"Cookie": f"{sessions.FLOW_COOKIE_NAME}={forged}"},
                 browser=False)
    assert reply.status == 400
    assert store.list_accounts() == []


def test_a_refused_sign_in_clears_the_half_finished_flow(signin):
    """So the next attempt starts clean rather than against a spent state."""
    store, call, jar = signin
    _state_from(call("GET", "/auth/google/start"))
    call("GET", "/auth/google/callback?state=invented&code=abc")
    assert sessions.FLOW_COOKIE_NAME not in jar
