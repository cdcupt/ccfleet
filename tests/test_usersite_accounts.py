"""The Claude accounts on a slot, as their holder sees and switches them.

Up to three of your own Claude accounts on one slot, one of them active.
Switching is one click with no link or code; adding one is the usual sign-in.
Driven through the real server, from a browser that keeps its cookies, the
way somebody would use it.
"""

from __future__ import annotations

import re
import time

from ccfleetd import slots, usersite
from ccfleetd.store import slot_login_key
from tests.conftest import refresh_of

from . import test_usersite
from .test_usersite import URL, claimed, machine, report

#: The user site served for real, with Google stubbed: the page's own fixture.
site = test_usersite.site

ME = "me@example.com"
WORK = "me@work.example.org"
SPARE = "spare@example.net"


def account(acct_id, email, *, active=False, signed_in=True, plan="max", days=20):
    return {"id": acct_id, "email": email, "plan": plan, "active": active,
            "signed_in": signed_in, "refresh_expires_at": time.time() + days * 86400}


def signed_in_slot(site, accounts, **extra):
    """A slot set up, held, signed in, and reporting these accounts."""
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    say(store, slot, accounts, **extra)
    assert store.get_slot(slot["id"])["state"] == slots.ACTIVE
    return store, erik, slot


def say(store, slot, accounts, logged_in=True, **extra):
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": logged_in, "subscription_type": "max"},
                          "remote_control": {"state": "active"},
                          "accounts": accounts, **extra}])


def slot_card(page):
    """The slot's card alone: the masthead says who is signed in to *us*."""
    return page[page.index('class="card slot"'):page.index('<p class="note">')]


def form_for(page, action):
    """Every form on the page that posts to this slot action, as its HTML."""
    return re.findall(rf'<form[^>]*action="/account/slots/[^"]+/{action}".*?</form>', page,
                      re.S)


# -- what the page shows -----------------------------------------------------------

def test_one_account_shows_its_address_and_room_for_more(site):
    _, erik, _ = signed_in_slot(site, [account("1", ME, active=True)])
    page = erik.page()
    assert ME in page and ">Active</span>" in page
    assert form_for(page, "add-account"), "no way to add a second account"
    assert not form_for(page, "use"), "nothing to switch to, yet a switch is offered"


def test_two_accounts_switch_with_one_click(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True),
                                              account("2", WORK, plan="pro")])
    page = erik.page()
    [use] = form_for(page, "use")
    assert 'name="account" value="2"' in use and "Use this one" in use
    assert "Switching ends anything running in Remote Control right now." in page
    assert "pro plan" in page

    reply = erik.press(f"/account/slots/{slot['id']}/use", account="2")
    assert reply.getheader("Location").startswith("/account?note=switching")
    intent = store.get_account_intent(slot["id"])
    assert (intent["action"], intent["account"], intent["state"]) == ("use", "2", "requested")

    page = erik.page()
    assert f"Switching to {WORK}" in page
    assert not form_for(page, "use"), "a second switch offered while the first is under way"
    assert refresh_of(page) == (4, "/account"), "nobody would see it land"


def test_three_accounts_leave_no_room_for_a_fourth(site):
    _, erik, _ = signed_in_slot(site, [account("1", ME, active=True), account("2", WORK),
                                       account("3", SPARE)])
    page = erik.page()
    assert len(form_for(page, "use")) == 2
    assert not form_for(page, "add-account")


def test_a_lapsed_account_offers_a_fresh_sign_in_not_a_switch(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True),
                                              account("2", WORK, signed_in=False)])
    page = erik.page()
    assert not form_for(page, "use")
    again = [f for f in form_for(page, "signin") if 'value="2"' in f]
    assert again and "Sign in again" in again[0]

    erik.press(f"/account/slots/{slot['id']}/signin", account="2")
    row = store.get_login(slot_login_key(slot["id"]))
    assert row["state"] == "requested" and row["account"] == "2"


def test_how_long_a_saved_sign_in_has_left_is_said(site):
    _, erik, _ = signed_in_slot(site, [account("1", ME, active=True, days=27.5),
                                       account("2", WORK, days=0.5)])
    page = erik.page()
    assert "good for 27 more days" in page
    assert "ends within a day" in page


def test_the_active_account_is_named_where_the_page_says_signed_in(site):
    _, erik, _ = signed_in_slot(site, [account("1", ME), account("2", WORK, active=True)])
    assert f"Signed in as {WORK}" in erik.page()


def test_the_usage_bars_say_whose_they_are(site):
    windows = {"session": {"used_pct": 12}, "week": {"used_pct": 30}}
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True)], quota=windows)
    assert "your Claude account &middot; every device" in erik.page()
    say(store, slot, [account("1", ME, active=True), account("2", WORK)], quota=windows)
    assert "your active Claude account &middot; every device" in erik.page()


def test_a_slot_whose_machine_says_nothing_of_accounts_reads_as_it_always_did(site):
    """An agent from before accounts: the page offers what it offered then."""
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": True, "subscription_type": "max"}}])
    page = erik.page()
    assert "Sign in again" in page and "Signed in · max plan" in page
    assert not form_for(page, "use") and not form_for(page, "add-account")


def test_a_slot_with_nobody_signed_in_any_more_says_so(site):
    """Removing the only account leaves the slot in use but signed out, and
    the page must not keep saying otherwise."""
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True)])
    say(store, slot, [], logged_in=False)
    card = slot_card(erik.page())
    assert "Signed in" not in card
    assert "Not signed in to Claude right now" in card
    assert "Sign in to Claude" in card


# -- what a press does ----------------------------------------------------------------

def test_adding_an_account_is_the_usual_sign_in_aimed_somewhere_new(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True)])
    reply = erik.press(f"/account/slots/{slot['id']}/add-account", email=WORK)
    assert reply.getheader("Location").startswith("/account?note=adding")
    row = store.get_login(slot_login_key(slot["id"]))
    assert (row["state"], row["account"], row["email"]) == ("requested", "new", WORK)
    store.record_login_progress(slot_login_key(slot["id"]), "url_ready", URL, "",
                                time.time(), row["requested_at"])
    assert 'name="code"' in erik.page()


def test_a_fourth_account_cannot_be_started_even_by_hand(site):
    """The button is gone at three; a form sent without it gets no further."""
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True), account("2", WORK),
                                              account("3", SPARE)])
    reply = erik.press(f"/account/slots/{slot['id']}/add-account", email="fourth@example.com")
    assert reply.getheader("Location").startswith("/account?note=not-now")
    assert store.get_login(slot_login_key(slot["id"])) is None


def test_signing_an_account_in_again_prefills_its_own_address(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True),
                                              account("2", WORK, signed_in=False)])
    erik.press(f"/account/slots/{slot['id']}/signin", account="2", email="other@example.com")
    row = store.get_login(slot_login_key(slot["id"]))
    assert (row["account"], row["email"]) == ("2", WORK)


def test_signing_in_again_to_an_account_the_slot_does_not_have_is_refused(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True)])
    reply = erik.press(f"/account/slots/{slot['id']}/signin", account="3")
    assert reply.getheader("Location").startswith("/account?note=not-now")
    assert store.get_login(slot_login_key(slot["id"])) is None


def test_what_cannot_be_used_is_refused_and_nothing_is_asked(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True),
                                              account("2", WORK, signed_in=False)])
    for which in ("1", "2", "3", "x", ""):
        reply = erik.press(f"/account/slots/{slot['id']}/use", account=which)
        assert reply.getheader("Location").startswith("/account?note=not-now"), which
        assert store.get_account_intent(slot["id"]) is None, which


def test_removing_needs_the_box_ticked(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True),
                                              account("2", WORK)])
    [remove] = [f for f in form_for(erik.page(), "forget") if 'value="2"' in f]
    assert 'name="confirm" value="remove"' in remove and "Remove" in remove
    for ticked in ({}, {"confirm": "yes"}, {"confirm": "wipe"}):
        reply = erik.press(f"/account/slots/{slot['id']}/forget", account="2", **ticked)
        assert reply.getheader("Location").startswith("/account?note=confirm-remove")
        assert store.get_account_intent(slot["id"]) is None
    reply = erik.press(f"/account/slots/{slot['id']}/forget", account="2", confirm="remove")
    assert reply.getheader("Location").startswith("/account?note=removing")
    intent = store.get_account_intent(slot["id"])
    assert (intent["action"], intent["account"]) == ("forget", "2")


def test_an_account_the_machine_never_mentioned_cannot_be_removed(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True)])
    reply = erik.press(f"/account/slots/{slot['id']}/forget", account="3", confirm="remove")
    assert reply.getheader("Location").startswith("/account?note=not-now")
    assert store.get_account_intent(slot["id"]) is None


def test_the_only_account_can_be_removed(site):
    """Signing yourself out of your own slot is yours to do."""
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True)])
    erik.press(f"/account/slots/{slot['id']}/forget", account="1", confirm="remove")
    assert store.get_account_intent(slot["id"])["action"] == "forget"


def test_a_failed_switch_is_said_as_text(site):
    store, erik, slot = signed_in_slot(site, [account("1", ME, active=True),
                                              account("2", WORK)])
    erik.press(f"/account/slots/{slot['id']}/use", account="2")
    requested_at = store.get_account_intent(slot["id"])["requested_at"]
    store.record_account_progress(slot["id"], {"state": "failed", "requested_at": requested_at,
                                               "detail": "<b>its sign-in lapsed</b>"},
                                  time.time())
    page = erik.page()
    assert f"Could not switch to {WORK}" in page
    assert "&lt;b&gt;its sign-in lapsed&lt;/b&gt;" in page and "<b>its" not in page
    assert form_for(page, "use"), "after a failure the holder can try again"


def test_an_address_from_the_machine_is_shown_as_text(site):
    """Whatever a machine sends is text on this page, never markup."""
    evil = '"><img src=x onerror=alert(1)>@example.com'
    _, erik, _ = signed_in_slot(site, [account("1", ME, active=True),
                                       account("2", evil)])
    page = erik.page()
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_the_notes_for_accounts_are_the_pages_own(site):
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    for code in ("adding", "switching", "removing", "confirm-remove"):
        assert usersite.NOTES[code][1] in erik.call("GET", f"/account?note={code}").body
