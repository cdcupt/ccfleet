"""The user site: one person, the slots they hold, and what they can do with them.

Driven through the real server with Google stubbed, from a browser that keeps
its cookies, the way somebody would use it. The tests that matter most are the
ones about what a person can reach: only their own slots, only with their own
session's token, and nothing of anybody else's on the page.
"""

from __future__ import annotations

import base64
import http.client
import re
import threading
import time
import urllib.parse
from datetime import timedelta

import pytest

from ccfleetd import oauth, payments, sessions, slots, usersite
from ccfleetd.api import Context, build_server
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.render import LOCAL_TIMES_TAG
from ccfleetd.store import Store, slot_login_key
from tests.conftest import next_load, refresh_of

SECRET = "0123456789abcdef0123456789abcdef"
URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"
TOKEN = "sk-ant-oat01-" + "Q" * 40


def handle_of(email):
    """A handle as an operator might set one from somebody's address, for tests
    that need a slot's name to read; the product never makes one this way."""
    return re.sub(r"[^a-z0-9]+", "-", email.split("@", 1)[0].lower()).strip("-")[:20].strip("-")


class Browser:
    """One person's browser: its own cookie jar, and a way to press buttons."""

    def __init__(self, port):
        self.port = port
        self.jar: dict[str, str] = {}

    def call(self, method, path, form=None, headers=None):
        hdrs = dict(headers or {})
        if self.jar and "Cookie" not in hdrs:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            hdrs["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=hdrs)
        reply = conn.getresponse()
        reply.body = reply.read().decode("utf-8", "replace")
        conn.close()
        for header in reply.headers.get_all("Set-Cookie") or []:
            name, _, value = header.split(";")[0].partition("=")
            if value:
                self.jar[name] = value
            else:
                self.jar.pop(name, None)
        return reply

    def page(self):
        reply = self.call("GET", "/account")
        assert reply.status == 200
        return reply.body

    def token(self):
        found = re.search(r'name="csrf" value="([0-9a-f]{64})"', self.page())
        assert found, "the page carries no form token"
        return found.group(1)

    def press(self, path, **fields):
        return self.call("POST", path, form={"csrf": self.token(), **fields})


@pytest.fixture
def site(monkeypatch):
    yield from serving(monkeypatch)


def serving(monkeypatch, **settings):
    """The user site on a live server, and a way to sign in to it; `settings`
    are more of its configuration, for the tests of a feature it turns on."""
    who = {"sub": "google-erik", "email": "erik@example.com"}
    monkeypatch.setattr(oauth, "exchange_code", lambda **kw: "access-token")
    monkeypatch.setattr(oauth, "fetch_identity", lambda token, **kw: dict(who))
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token="admin-token", public_url="http://127.0.0.1",
                 google_client_id="cid", google_client_secret="secret",
                 cookie_secret=SECRET, cookie_secure=False, **settings)
    store = Store(":memory:", max_slots_per_machine=8)
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def sign_in(sub="google-erik", email="erik@example.com", quota=0, handle=True):
        """Signed in with Google. Their slots are named "<handle>-<n>" after a
        handle set from their address, as the operator can; `handle=None`
        leaves the neutral names a claim gives by default."""
        who.update(sub=sub, email=email)
        browser = Browser(srv.server_address[1])
        start = browser.call("GET", "/auth/google/start?next=/account")
        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(start.getheader("Location")).query)["state"][0]
        assert browser.call("GET", f"/auth/google/callback?state={state}&code=c").status == 303
        account = store.account_by_google_sub(sub)
        store.set_slot_quota(account["id"], quota)
        if handle is not None:
            store.set_account_handle(
                account["id"], handle_of(email) if handle is True else handle)
        browser.account = store.get_account(account["id"])
        return browser

    yield store, sign_in, cfg
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    store.close()


def machine(store, node="m1", users=("slot01", "slot02"), heard=None, region="us-west"):
    """A shared machine that has just reported every slot on it empty."""
    store.add_node(node, "op", region=region, now=time.time())
    store.set_machine_capacity(node, len(users))
    for n, user in enumerate(users, 1):
        store.add_slot(f"{node}-{n:02d}", node, user, now=time.time())
    store.apply_slot_report(node, [{"unix_user": u, "present": False} for u in users],
                            now=time.time() if heard is None else heard)
    store.insert_heartbeat(node, time.time() if heard is None else heard,
                           {"node_id": node, "mode": "machine", "slots": []})


def report(store, node, entries, ts=None):
    """What the machine says next: moves slots, and becomes its latest heartbeat."""
    now = time.time() if ts is None else ts
    store.apply_slot_report(node, entries, now=now)
    store.insert_heartbeat(node, now, {"node_id": node, "mode": "machine", "slots": entries})


def claimed(store, browser, node="m1"):
    """Claim through the page and let the machine finish setting it up."""
    assert browser.press("/account/claim").status == 303
    [slot] = [s for s in store.list_slots(held_by=browser.account["id"])
              if s["state"] == slots.CLAIMING]
    report(store, node, [{"unix_user": slot["unix_user"], "present": True,
                          "provisioned_for": slot["claimed_at"]}])
    return store.get_slot(slot["id"])


# -- the door --------------------------------------------------------------------

def test_a_signed_out_press_goes_back_to_the_page_and_does_nothing(site):
    store, sign_in, _ = site
    machine(store)
    stranger = Browser(sign_in(quota=1).port)
    reply = stranger.call("POST", "/account/claim", form={"csrf": "0" * 64})
    assert reply.status == 303 and reply.getheader("Location") == "/account"
    assert all(s["state"] == slots.FREE for s in store.list_slots())


@pytest.mark.parametrize("token", ["", "0" * 64, "not-hex"])
def test_a_press_without_this_sessions_token_is_refused(site, token):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    reply = erik.call("POST", "/account/claim", form={"csrf": token} if token else {})
    assert reply.status == 403
    assert all(s["state"] == slots.FREE for s in store.list_slots())


def test_somebody_elses_token_is_worth_nothing_here(site):
    """Derived from the session, so one person's page cannot vouch for another's."""
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    ana = sign_in("google-ana", "ana@example.com", quota=1)
    reply = erik.call("POST", "/account/claim", form={"csrf": ana.token()})
    assert reply.status == 403
    assert store.held_slot_count(erik.account["id"]) == 0


# -- claiming ----------------------------------------------------------------------

def test_no_allowance_no_claim_button_and_no_claim(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=0)
    shown = erik.page()
    assert "no slots yet" in shown and "Claim a slot" not in shown
    reply = erik.press("/account/claim")
    assert reply.getheader("Location").startswith("/account?note=no-allowance")
    assert store.held_slot_count(erik.account["id"]) == 0


def test_claiming_a_slot_from_the_page(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    assert "Claim a slot" in erik.page()
    reply = erik.press("/account/claim")
    assert reply.status == 303
    assert reply.getheader("Location").startswith("/account?note=claimed#slot-m1-01")
    [slot] = store.list_slots(held_by=erik.account["id"])
    assert slot["state"] == slots.CLAIMING
    shown = erik.page()
    assert "Setting up" in shown and "m1-01" in shown
    assert "Claim a slot" not in shown, "offered a second claim on an allowance of one"


def test_nothing_free_is_said_as_much(site):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    sign_in("google-ana", "ana@example.com", quota=1).press("/account/claim")
    erik = sign_in(quota=1)
    reply = erik.press("/account/claim")
    assert reply.getheader("Location").startswith("/account?note=no-slot")


def test_a_machine_gone_quiet_is_not_handed_a_claim(site):
    """It would sit in "setting up" for half an hour and then be given up."""
    store, sign_in, cfg = site
    machine(store, heard=time.time() - cfg.heartbeat_max_age_s - 60)
    erik = sign_in(quota=1)
    assert erik.press("/account/claim").getheader("Location").startswith(
        "/account?note=no-slot")


# -- only their own ----------------------------------------------------------------

def test_the_page_shows_their_slots_and_nobody_elses(site):
    store, sign_in, _ = site
    machine(store)
    ana = sign_in("google-ana", "ana@example.com", quota=1)
    ana.press("/account/claim")
    erik = sign_in(quota=1)
    erik.press("/account/claim")
    shown = erik.page()
    assert "m1-02" in shown
    assert "m1-01" not in shown and "ana@example.com" not in shown


def test_a_kept_machine_is_never_mentioned_on_anybodys_page(site):
    """Who a machine is kept for is the operator's business. Somebody turned
    away hears that nothing is free, as at any other time; the person it is kept
    for sees their slot, not the arrangement, and nobody sees anybody's address."""
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    ana = sign_in("google-ana", "ana@example.com", quota=1)
    erik = sign_in(quota=1)
    store.reserve_machine("m1", erik.account["id"])
    assert ana.press("/account/claim").getheader("Location").startswith(
        "/account?note=no-slot")
    claimed(store, erik)
    for browser, other in ((ana, "erik@example.com"), (erik, "ana@example.com")):
        shown = browser.page()
        assert "reserved" not in shown.lower() and "kept for" not in shown.lower()
        assert other not in shown
    assert "m1-01" in erik.page()


@pytest.mark.parametrize("action", usersite.SLOT_ACTIONS)
@pytest.mark.parametrize("fields", [{"confirm": "wipe", "code": "x"}, {}, {"confirm": "no"},
                                    {"code": ""}])
def test_somebody_elses_slot_answers_like_one_that_is_not_there(site, action, fields):
    """Not 403: which slots exist, and who holds them, is not theirs to learn —
    in every shape of request, including the ones a form would refuse."""
    store, sign_in, _ = site
    machine(store)
    ana = sign_in("google-ana", "ana@example.com", quota=1)
    held_by_ana = claimed(store, ana)
    erik = sign_in(quota=1)
    theirs = erik.press(f"/account/slots/{held_by_ana['id']}/{action}", **fields)
    missing = erik.press(f"/account/slots/no-such-slot/{action}", **fields)
    assert theirs.status == missing.status == 404
    assert theirs.body == missing.body
    after = store.get_slot(held_by_ana["id"])
    assert after["state"] == slots.CLAIMED and after["held_by"] == ana.account["id"]
    assert store.get_login(slot_login_key(held_by_ana["id"])) is None


def test_an_action_that_does_not_exist_is_not_found(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    assert erik.press(f"/account/slots/{slot['id']}/shell").status == 404
    assert erik.press("/account/whatever").status == 404


# -- giving it back ----------------------------------------------------------------

def test_giving_a_slot_back_needs_the_box_ticked(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    reply = erik.press(f"/account/slots/{slot['id']}/release")
    assert reply.getheader("Location").startswith("/account?note=confirm")
    assert store.get_slot(slot["id"])["state"] == slots.CLAIMED
    reply = erik.press(f"/account/slots/{slot['id']}/release", confirm="yes")
    assert store.get_slot(slot["id"])["state"] == slots.CLAIMED, "any value ticked it"


def test_giving_a_slot_back(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    erik.press(f"/account/slots/{slot['id']}/signin")
    reply = erik.press(f"/account/slots/{slot['id']}/release", confirm="wipe")
    assert reply.getheader("Location").startswith("/account?note=released")
    assert store.get_slot(slot["id"])["state"] == slots.RELEASING
    assert store.get_login(slot_login_key(slot["id"])) is None, "the sign-in stayed behind"
    shown = erik.page()
    assert "Being wiped" in shown and "Give this slot back" not in shown


def test_a_slot_on_its_way_out_cannot_be_given_back_twice(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    erik.press(f"/account/slots/{slot['id']}/release", confirm="wipe")
    again = erik.press(f"/account/slots/{slot['id']}/release", confirm="wipe")
    assert again.getheader("Location").startswith("/account?note=not-now")


# -- signing in to Claude ----------------------------------------------------------

def test_signing_in_from_the_page(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    key = slot_login_key(slot["id"])
    assert "Sign in to Claude" in erik.page()

    erik.press(f"/account/slots/{slot['id']}/signin", email="me@example.com")
    row = store.get_login(key)
    assert row["state"] == "requested" and row["email"] == "me@example.com"
    assert "Asking the node" in erik.page()

    store.record_login_progress(key, "url_ready", URL, "", time.time(), row["requested_at"])
    shown = erik.page()
    assert f'href="{URL.replace("&", "&amp;")}"' in shown
    assert 'name="code"' in shown
    assert 'http-equiv="refresh"' not in shown, "reloaded while a code was being typed"

    erik.press(f"/account/slots/{slot['id']}/code", code="the-code")
    assert store.get_login(key)["state"] == "code_sent"
    erik.press(f"/account/slots/{slot['id']}/cancel")
    assert store.get_login(key) is None


def test_a_link_that_is_not_a_sign_in_is_never_shown_as_one(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    erik.press(f"/account/slots/{slot['id']}/signin")
    key = slot_login_key(slot["id"])
    with store._lock:                     # as if written before the rule existed
        store._conn.execute("UPDATE logins SET state='url_ready', url=? WHERE node_id=?",
                            ("javascript:alert(document.cookie)", key))
        store._conn.commit()
    shown = erik.page()
    assert "javascript:" not in shown


def test_a_slot_still_being_set_up_cannot_be_signed_into(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    erik.press("/account/claim")
    [slot] = store.list_slots(held_by=erik.account["id"])
    shown = erik.page()
    assert "Sign in to Claude" not in shown
    reply = erik.press(f"/account/slots/{slot['id']}/signin")
    assert reply.getheader("Location").startswith("/account?note=not-now")


def test_a_signed_in_slot_shows_its_windows_and_a_way_in(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": True, "subscription_type": "max"},
                          "remote_control": {"state": "active"},
                          "quota": {"session": {"used_pct": 12}, "week": {"used_pct": 30}},
                          "usage": {"total_tokens": 900, "window_hours": 168,
                                    "by_hour": {"start": time.time() - 167 * 3600,
                                                "tokens": [0] * 160 + [100] * 8}}}])
    assert store.get_slot(slot["id"])["state"] == slots.ACTIVE
    shown = erik.page()
    assert "In use" in shown and "Max plan" in shown
    assert 'href="https://claude.ai/code"' in shown
    assert "900</b> tokens run on this slot itself" in shown
    # The windows are the account's, used anywhere; said under them, so a week
    # at 30% beside 900 tokens here does not read as a mistake.
    assert usersite.ACCOUNT_WIDE in shown
    assert "your Claude account &middot; every device" in shown
    assert "Sign in again" in shown


# -- device tokens -----------------------------------------------------------------

def test_a_device_token_from_the_page(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    key = slot_login_key(slot["id"])
    # Before any token exists the row says it is optional, not a status to act on.
    before = erik.page()
    assert "None yet &middot; optional, for using this account from your own computer" in before
    assert "last issued" not in before
    erik.press(f"/account/slots/{slot['id']}/token")
    row = store.get_login(key)
    assert row["kind"] == "token"
    store.record_login_progress(key, "ready", "", "", time.time(), row["requested_at"],
                                secret=TOKEN)
    assert "Show it" in erik.page()

    shown = erik.press(f"/account/slots/{slot['id']}/token-show")
    assert shown.status == 200 and TOKEN in shown.body
    assert store.get_slot(slot["id"])["device_token_at"] > 0

    erik.press(f"/account/slots/{slot['id']}/token-done")
    assert store.get_login(key) is None
    assert "Nothing to show" in erik.press(f"/account/slots/{slot['id']}/token-show").body
    after = erik.page()
    assert "last issued" in after and "None yet" not in after


def test_one_flow_at_a_time_on_a_slot(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    erik.press(f"/account/slots/{slot['id']}/token")
    shown = erik.page()
    assert "Finish or cancel the device token" in shown
    assert "Sign in to Claude" not in shown


# -- what the page says --------------------------------------------------------------

def test_a_note_is_chosen_by_code_never_written_by_the_link(site):
    """A message a link could write is a message a stranger could put on our
    page, in front of somebody signed in."""
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    evil = erik.call("GET", "/account?note=" + urllib.parse.quote(
        "<script>alert(1)</script>Your account is locked, call +1-555"))
    assert "alert(1)" not in evil.body and "locked" not in evil.body
    # The one script on the page is the site's own, the one its policy allows.
    assert evil.body.count("<script") == 1 and LOCAL_TIMES_TAG in evil.body
    known = erik.call("GET", "/account?note=released")
    assert usersite.NOTES["released"][1] in known.body


def test_the_page_comes_back_soon_only_while_something_moves(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    assert refresh_of(erik.page()) == (60, "/account")
    erik.press("/account/claim")
    assert refresh_of(erik.page()) == (4, "/account"), "setting up, and nobody would see it finish"


def test_after_an_action_the_page_really_comes_back(site):
    """Every action lands on /account?note=…#slot-…. A refresh naming no address
    there is a fragment navigation — the browser scrolls and loads nothing — so
    "Starting the sign-in on your slot…" stood for minutes, the link it was
    waiting for already there for anybody who reloaded by hand."""
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    key = slot_login_key(slot["id"])
    starting = usersite.NOTES["signin"][1]

    at = erik.press(f"/account/slots/{slot['id']}/signin").getheader("Location")
    assert "#" in at, "an action lands on its own card"
    landed = erik.call("GET", urllib.parse.urldefrag(at)[0]).body
    assert starting in landed, "said right after the action"
    assert refresh_of(landed)[0] == usersite.ACTIVE_REFRESH_S, "back soon to see it happen"

    store.record_login_progress(key, "url_ready", URL, "", time.time(),
                                store.get_login(key)["requested_at"])
    then = next_load(at, landed)
    assert then == "/account", "the page came back by itself, to the bare page"
    later = erik.call("GET", then).body
    assert 'name="code"' in later, "showing the link"
    assert starting not in later, "and leaving the note behind"

    at = erik.press(f"/account/slots/{slot['id']}/code", code="the-code").getheader("Location")
    sent = erik.call("GET", urllib.parse.urldefrag(at)[0]).body
    assert refresh_of(sent)[0] == usersite.ACTIVE_REFRESH_S, "a code on its way is moving"
    assert next_load(at, sent) == "/account"


def test_a_machine_nobody_has_heard_from_says_so(site):
    store, sign_in, cfg = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True}],
           ts=time.time() - cfg.heartbeat_max_age_s - 120)
    assert "may be unreachable" in erik.page()


# -- signing out -------------------------------------------------------------------

def test_signing_out_needs_the_pages_own_token(site):
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    refused = erik.call("POST", "/auth/signout", form={"csrf": "0" * 64})
    assert refused.status == 403
    assert "erik@example.com" in erik.page(), "signed out by a request with no token"
    assert erik.press("/auth/signout").status == 303
    assert "Continue with Google" in erik.page()


def test_another_sites_form_cannot_sign_anybody_out(site):
    """SameSite keeps the cookie off a cross-site POST, so it arrives with no
    cookie at all — and must not be answered with one that clears it."""
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    cross_site = Browser(erik.port)            # no cookies: what a cross-site POST sends
    reply = cross_site.call("POST", "/auth/signout", form={})
    assert reply.status == 303
    assert reply.getheader("Set-Cookie") is None
    assert "erik@example.com" in erik.page()


def test_a_cookie_that_names_no_session_is_simply_cleared(site):
    store, sign_in, _ = site
    stale = Browser(sign_in(quota=0).port)
    stale.jar[sessions.COOKIE_NAME] = sessions.sign("expired-long-ago", SECRET)
    reply = stale.call("POST", "/auth/signout", form={})
    assert reply.status == 303
    assert "Max-Age=0" in (reply.getheader("Set-Cookie") or "")


def test_a_token_is_shown_as_text_whatever_a_machine_sent(site):
    """The token rides up from the machine. A machine that has been tampered
    with could send markup instead, and this page is shown to its holder."""
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    erik.press(f"/account/slots/{slot['id']}/token")
    key = slot_login_key(slot["id"])
    store.record_login_progress(key, "ready", "", "", time.time(),
                                store.get_login(key)["requested_at"],
                                secret="<script>steal()</script>")
    shown = erik.press(f"/account/slots/{slot['id']}/token-show").body
    assert "<script>steal()" not in shown
    assert "&lt;script&gt;steal()" in shown


def test_the_way_in_is_offered_only_once_it_is_open(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": True},
                          "remote_control": {"state": "inactive"}}])
    shown = erik.page()
    assert 'href="https://claude.ai/code"' not in shown
    assert "Remote Control is starting" in shown


def test_a_device_token_flow_shows_its_link_and_takes_its_code(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    erik.press(f"/account/slots/{slot['id']}/token")
    key = slot_login_key(slot["id"])
    store.record_login_progress(key, "url_ready", URL, "", time.time(),
                                store.get_login(key)["requested_at"])
    shown = erik.page()
    assert f'href="{URL.replace("&", "&amp;")}"' in shown and 'name="code"' in shown
    erik.press(f"/account/slots/{slot['id']}/code", code="c0de")
    assert store.get_login(key)["state"] == "code_sent"


def test_a_machine_with_nothing_on_record_says_so(site):
    """Heartbeats are kept for a month; a machine silent for longer has none."""
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    claimed(store, erik)
    store.prune_heartbeats(time.time() + 1)
    assert "not heard from yet" in erik.page()



def test_a_page_left_open_across_a_hand_over_acts_on_nothing(site):
    """Erik loads his page; his slot is then given back, wiped, and claimed by
    Ana. The form he still has open must not reach Ana's slot."""
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    stale_token = erik.token()
    store.begin_release(slot["id"])
    report(store, "m1", [{"unix_user": "slot01", "present": False}])
    ana = sign_in("google-ana", "ana@example.com", quota=1)
    claimed(store, ana)
    for action in usersite.SLOT_ACTIONS:
        reply = erik.call("POST", f"/account/slots/{slot['id']}/{action}",
                          form={"csrf": stale_token, "confirm": "wipe", "code": "x"})
        assert reply.status == 404, action
    after = store.get_slot(slot["id"])
    assert after["held_by"] == ana.account["id"] and after["state"] == slots.CLAIMED
    assert store.get_login(slot_login_key(slot["id"])) is None


# -- what they have paid -----------------------------------------------------------

def paid(store, browser, days, **fields):
    through = (payments.today(time.time()) + timedelta(days=days)).isoformat()
    store.record_payment(browser.account["id"], amount=fields.get("amount", "30"),
                         currency="USD", through=through, note=fields.get("note", ""),
                         recorded_by="op", now=time.time())
    return through


def test_the_page_says_how_long_they_are_paid_for_and_nothing_else(site):
    """The day, and only the day: amounts and the operator's notes stay on the console."""
    store, sign_in, _ = site
    browser = sign_in(quota=1)
    through = paid(store, browser, 20, amount="47.5", note="cash via Ana's brother")
    page = browser.page()
    assert f"Paid through <strong>{through}</strong>." in page
    assert "47.50" not in page and "Ana" not in page and "USD" not in page


def test_nothing_recorded_says_nothing(site):
    """Plenty of allowances are arranged without a payment; "none" would read as a debt."""
    store, sign_in, _ = site
    page = sign_in(quota=1).page()
    assert "Paid through" not in page and "paid period" not in page


def test_a_lapsed_period_is_said_plainly_and_changes_nothing_else(site):
    store, sign_in, _ = site
    browser = sign_in(quota=1)
    ended = paid(store, browser, -2)
    page = browser.page()
    assert f"Your paid period ended on {ended}." in page
    assert 'action="/account/claim"' in page, "the ledger is a record, not a gate"


def test_a_voided_payment_is_not_counted(site):
    store, sign_in, _ = site
    browser = sign_in(quota=1)
    kept = paid(store, browser, 10)
    paid(store, browser, 40)
    store.void_payment(store.list_payments(browser.account["id"])[0]["id"], now=time.time())
    assert f"Paid through <strong>{kept}</strong>." in browser.page()


def test_somebody_elses_payment_is_not_theirs(site):
    store, sign_in, _ = site
    ana = sign_in(sub="google-ana", email="ana@example.com", quota=1)
    bo = sign_in(sub="google-bo", email="bo@example.com", quota=1)
    paid(store, ana, 20)
    assert "Paid through" in ana.page() and "Paid through" not in bo.page()


def test_the_sign_in_page_says_what_google_is_asked_for_and_what_is_kept(site):
    """The privacy line is a promise about the scopes, so the two move together:
    ask Google for more and this fails until the page says so."""
    store, _, cfg = site
    assert oauth.SCOPES == "openid email"
    # What is kept is pinned by behaviour in test_sessions: extra fields are dropped.
    page = usersite.page(store, cfg, None, "", time.time())
    assert ("We ask Google for your email address, whether Google has verified it, and "
            "the id it gives your account, which stays the same if the address changes. "
            "From Google we keep only the address and the id.") in page
    assert '<a href="/privacy">What else we keep, and why</a>' in page


# -- the privacy page ----------------------------------------------------------------

def test_the_privacy_page_is_public(site):
    """Google links to it from the sign-in screen; nobody needs an account to read it."""
    _, sign_in, _ = site
    anybody = Browser(sign_in().port)
    anybody.jar.clear()
    reply = anybody.call("GET", "/privacy")
    assert reply.status == 200 and "<h1>Privacy</h1>" in reply.body
    assert "What the operator can see" in reply.body


def test_every_page_of_the_user_site_links_the_policy(site):
    store, sign_in, cfg = site
    assert 'href="/privacy"' in sign_in(quota=1).page()
    assert 'href="/privacy">Privacy</a>' in usersite.page(store, cfg, None, "", time.time())


def test_the_privacy_page_quotes_the_settings_in_force():
    """Every length of time on it comes from the running configuration, so the
    page cannot promise one thing while the server does another."""
    default = usersite.privacy_page(Config())
    assert "It lasts 14 days, or until you sign out" in default
    assert "We keep these reports for 30 days" in default
    assert "are held here for at most 15 minutes" in default
    assert "which lasts 10 minutes" in default
    tuned = usersite.privacy_page(Config(session_ttl_s=36 * 3600, retention_days=45))
    assert "It lasts 36 hours, or until you sign out" in tuned
    assert "We keep these reports for 45 days" in tuned
    assert "We keep these reports for 1 day." in usersite.privacy_page(Config(retention_days=1))


def test_the_privacy_page_follows_the_codes_own_windows(monkeypatch):
    """The sign-in window and the Google cookie are constants in code, not
    settings; the page still reads them rather than repeating the numbers."""
    monkeypatch.setattr(usersite, "LOGIN_MAX_AGE_S", 20 * 60)
    monkeypatch.setattr(oauth, "FLOW_TTL_S", 5 * 60)
    page = usersite.privacy_page(Config())
    assert "are held here for at most 20 minutes" in page and "for at most 20 minutes." in page
    assert "which lasts 5 minutes" in page


def test_the_operators_address_is_published_only_when_they_chose_one():
    unset = usersite.privacy_page(Config())
    assert "mailto:" not in unset and "support address Google shows" in unset
    chosen = usersite.privacy_page(Config(contact_email="help&desk@example.com"))
    assert 'href="mailto:help&amp;desk@example.com"' in chosen
    assert "help&desk@example.com" not in chosen
    assert "support address Google shows" not in chosen


def test_the_privacy_page_says_what_the_operator_can_see():
    """The admission the design insists on: root can read a slot. Softening it
    later is the likeliest way this page goes wrong, so it is pinned."""
    page = usersite.privacy_page(Config())
    assert "their administrators have root" in page
    assert "technically read any slot&#x27;s files, and its Claude credential" in page


def test_the_policy_lists_every_field_kept_about_a_person():
    """A privacy policy that leaves a stored field out is wrong, however kind its
    words. Each phrase stands for a column that holds something about you."""
    page = usersite.privacy_page(Config())
    for kept in ("whether you are an operator",               # accounts.role
                 "when you first signed in",                  # accounts.created_at
                 "when you last visited",                     # accounts.last_seen_at
                 "when a device token was last handed out",   # slots.device_token_at
                 # slots.account_switched_at
                 "when you last moved it to another Claude account",
                 "with when it began and when it ends",       # sessions.created_at/expires_at
                 "when and by whom it was recorded",          # payments.recorded_at/_by
                 "whether it was later voided",               # payments.voided_at
                 "the code you paste",                        # logins.code
                 "the server keeps a matching record",        # oauth_flows
                 # heartbeats: slots[].credentials.email / refresh_expires_at
                 "the email address and plan of the one Claude account",
                 "when that sign-in expires"):
        assert kept in page, kept


def test_the_policy_no_longer_says_the_account_address_stays_on_the_machine():
    """It did say so, and it stopped being true when the holder's page began
    showing which of their accounts a slot is signed in to. A promise the code
    has outgrown is the worst kind of line on this page."""
    page = usersite.privacy_page(Config())
    assert "name and email on your Claude account" not in page
    assert "or the name on your Claude account." in page
    assert "Claude accounts" not in page, "one account per slot, and the page says one"
    assert f"Last updated {usersite.PRIVACY_UPDATED}" in page and \
        usersite.PRIVACY_UPDATED >= "2026-09-23"


# -- the corner of the bar: who is looking -------------------------------------------

PUBLIC = ("/docs", "/docs/guide", "/docs/how-it-works", "/docs/terms", "/privacy")
SIGN_IN = 'href="/account">Sign in</a>'


def corner(page):
    """The bar's corner alone: the part of a page that says who is looking."""
    start = page.index('<div class="topbar-end">')
    return page[start:page.index("</header>", start)]


def test_public_pages_show_who_is_looking(site):
    """Signed in, every page anybody can read shows your initial and your menu;
    signed out, the same spot is the way to sign in."""
    _, sign_in, _ = site
    erik = sign_in()
    anybody = Browser(erik.port)
    for path in PUBLIC:
        mine = corner(erik.call("GET", path).body)
        assert '<details class="usermenu">' in mine, path
        assert "<strong>erik@<wbr>example.com</strong>" in mine and ">E</span>" in mine, path
        # The menu's head repeats the face beside the address, as the mockup has it.
        assert re.search(r'<div class="menu-who"><span class="avatar t\d big" '
                         r'aria-hidden="true">E</span>', mine), path
        assert SIGN_IN not in mine, path
        theirs = corner(anybody.call("GET", path).body)
        assert SIGN_IN in theirs and "usermenu" not in theirs, path


def test_a_session_that_is_no_good_is_nobody(site):
    """A tampered cookie and an expired session both read as nobody: the corner
    offers sign-in, never a menu for a session that no longer counts."""
    store, sign_in, _ = site
    erik = sign_in()
    good = erik.jar[sessions.COOKIE_NAME]
    forged = Browser(erik.port)
    forged.jar[sessions.COOKIE_NAME] = good[:-1] + ("0" if good[-1] != "0" else "1")
    stale = Browser(erik.port)
    expired = store.create_session(erik.account["id"], now=time.time() - 3600, ttl_s=60)
    stale.jar[sessions.COOKIE_NAME] = sessions.sign(expired, SECRET)
    for browser in (forged, stale):
        shown = corner(browser.call("GET", "/docs").body)
        assert SIGN_IN in shown and "usermenu" not in shown


def test_reading_a_public_page_writes_nothing(site):
    """Saying who is looking reads the session; it does not use it. No visit is
    recorded and no expired session is swept by a page anybody can read."""
    store, sign_in, _ = site
    erik = sign_in()
    seen = store.get_account(erik.account["id"])["last_seen_at"]
    stale = Browser(erik.port)
    expired = store.create_session(erik.account["id"], now=time.time() - 3600, ttl_s=60)
    stale.jar[sessions.COOKIE_NAME] = sessions.sign(expired, SECRET)
    before = store._conn.total_changes
    for path in PUBLIC:
        assert erik.call("GET", path).status == 200
        assert stale.call("GET", path).status == 200
    assert store._conn.total_changes == before, "a public page wrote to the database"
    assert store.get_account(erik.account["id"])["last_seen_at"] == seen
    # The page that acts on a session still sweeps an expired one, as before.
    assert "Continue with Google" in stale.call("GET", "/account").body
    assert store._conn.total_changes > before


def test_the_account_page_the_token_page_and_not_found_show_the_menu(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    token_page = erik.press(f"/account/slots/{slot['id']}/token-show")
    missing = erik.press("/account/slots/nobody-01/token")
    assert token_page.status == 200 and missing.status == 404
    for body in (erik.page(), token_page.body, missing.body):
        mine = corner(body)
        assert '<details class="usermenu">' in mine and "erik@example.com" in mine
        assert SIGN_IN not in mine


def test_the_way_in_does_not_offer_itself(site):
    """The sign-in page is the way in: a Sign in button on it would point at itself."""
    _, sign_in, _ = site
    anybody = Browser(sign_in().port)
    door = anybody.call("GET", "/account").body
    assert "Continue with Google" in door and SIGN_IN not in corner(door)


def test_only_an_operator_is_offered_the_console(site):
    store, sign_in, _ = site
    erik = sign_in()
    for path in (*PUBLIC, "/account"):
        mine = corner(erik.call("GET", path).body)
        assert "<span>Console</span>" not in mine, path
        assert '"menu-pill">operator<' not in mine, "the operator pill without the console"
    store.set_account_role(erik.account["id"], "admin")
    for path in (*PUBLIC, "/account"):
        assert ('<a href="/admin"><span>Console</span><span class="menu-pill">operator</span>'
                in corner(erik.call("GET", path).body)), path
    # The console's own corner is the same menu, for the operator it signed in.
    console = erik.call("GET", "/admin")
    assert console.status == 200
    assert "<strong>erik@<wbr>example.com</strong>" in corner(console.body)
    # The admin token is basic auth: no session, so nothing to show or sign out.
    basic = "Basic " + base64.b64encode(b"admin:admin-token").decode()
    token_only = Browser(erik.port).call("GET", "/admin", headers={"Authorization": basic})
    assert token_only.status == 200
    assert '<details class="usermenu">' not in token_only.body


def test_your_slots_carries_this_accounts_own_count(site):
    """How many slots you hold, beside Your slots: your own count, never anybody
    else's, and nothing at all when it is none."""
    store, sign_in, _ = site
    for node in ("m1", "m2", "m3"):
        machine(store, node, users=("slot01",))
    erik = sign_in(quota=2)
    ana = sign_in("google-ana", "ana@example.com", quota=1)
    bo = sign_in("google-bo", "bo@example.com", quota=0)
    for browser in (erik, erik, ana):
        assert browser.press("/account/claim").status == 303
    for browser, count in ((erik, 2), (ana, 1)):
        for path in ("/docs", "/account"):
            mine = corner(browser.call("GET", path).body)
            assert f'<span>Your slots</span><span class="menu-pill">{count}</span>' in mine, path
    empty = corner(bo.call("GET", "/docs").body)
    assert "<span>Your slots</span></a>" in empty and "menu-pill" not in empty


def test_signing_out_from_the_menu_is_this_sessions_post(site):
    """A form carrying this session's token, never a link: a link would let any
    page that can make a browser fetch a URL sign people out."""
    _, sign_in, _ = site
    erik = sign_in()
    mine = corner(erik.call("GET", "/docs/guide").body)
    form = mine[mine.index("<form"):mine.index("</form>")]
    session_id = sessions.unsign(erik.jar[sessions.COOKIE_NAME], SECRET)
    assert '<form method="post" action="/auth/signout">' in form
    assert f'name="csrf" value="{usersite.csrf_for(session_id, SECRET)}"' in form
    assert "/auth/signout" not in mine.replace(form, ""), "signing out is a form, never a link"
    assert erik.call("POST", "/auth/signout", form={}).status == 403
    token = re.search(r'name="csrf" value="([0-9a-f]{64})"', form).group(1)
    assert erik.call("POST", "/auth/signout", form={"csrf": token}).status == 303
    assert SIGN_IN in corner(erik.call("GET", "/docs").body)


def test_the_menu_escapes_the_address():
    from ccfleetd.render import user_menu
    shown = user_menu({"id": "u1", "email": 'x"><img src=y>@e.com'}, "t" * 64,
                      operator=False)
    assert "<img" not in shown
    assert 'aria-label="Account menu for x&quot;&gt;&lt;img src=y&gt;@e.com"' in shown
    assert "<strong>x&quot;&gt;&lt;img src=y&gt;@<wbr>e.com</strong>" in shown


def test_an_avatar_is_the_same_every_time_and_says_whose_it_is():
    from ccfleetd.render import AVATAR_TONES, _initial, _tone
    assert _initial({"email": "erik@example.com"}) == "E"
    assert _initial({"email": "_ops.team@example.com"}) == "O", "the first letter or digit"
    assert _initial({"email": "@example.com"}) == "?" and _initial({}) == "?"
    assert _initial({"handle": "maya", "email": "zed@example.com"}) == "M", "a handle wins"
    assert _tone({"id": "u1"}) == _tone({"id": "u1"})
    tones = {_tone({"id": f"u{n:024x}"}) for n in range(40)}
    assert len(tones) > 1 and tones <= set(range(AVATAR_TONES))
