"""The holder's page under slot model v2: names, and an owner's own node.

A slot is shown by the name it answers to in claude.ai/code — its holder's —
and an owner's own node, counted as their slot, sits in the same list, built
from the node's own heartbeat. Nothing on that card can wipe it.
"""

from __future__ import annotations

import time

from ccfleetd import slots
from tests.test_usersite import report, site  # noqa: F401  (the fixture, by name)


def one_slot_machine(store, node):
    """A shared machine with its one slot, named after the machine, just
    reported empty."""
    store.add_node(node, "op", region="us-west", now=time.time())
    store.add_slot(node, node, "slot01", now=time.time())
    report(store, node, [{"unix_user": "slot01", "present": False}])


def card(page, slot_id):
    start = page.index(f'id="slot-{slot_id}"')
    return page[start:page.index("</div>", page.index("<h2>", start))]


def whole_card(page, slot_id):
    start = page.index(f'id="slot-{slot_id}"')
    end = page.find('<div class="card slot"', start + 1)
    return page[start:end if end > 0 else len(page)]


# -- names ---------------------------------------------------------------------------

def test_a_claimed_slot_is_shown_by_its_holders_name(site):  # noqa: F811
    store, sign_in, _ = site
    one_slot_machine(store, "pool-1")
    erik = sign_in(quota=1)
    assert erik.press("/account/claim").status == 303
    title = card(erik.page(), "pool-1")
    assert "<h2>erik-1 " in title, "the name claude.ai shows the holder"
    assert "on pool-1" in title, "and the machine it runs on, which is not that name"


def test_the_machine_is_not_named_twice(site):  # noqa: F811
    """erik-2 on erik-2 says it once."""
    store, sign_in, _ = site
    one_slot_machine(store, "erik-2")
    erik = sign_in(quota=1)
    store.reserve_machine("erik-2", erik.account["id"])
    with store._lock:                  # a slot from before names: it has none
        store._conn.execute("UPDATE slots SET name = NULL")
        store._conn.commit()
    erik.press("/account/claim")
    with store._lock:
        store._conn.execute("UPDATE slots SET name = NULL")
        store._conn.commit()
    title = card(erik.page(), "erik-2")
    assert "<h2>erik-2 " in title and "on erik-2" not in title


# -- an owner's own node, counted as their slot ------------------------------------------

def owner_said(store, **creds):
    store.insert_heartbeat("erik-1", time.time(), {
        "node_id": "erik-1", "credentials": {"subscription_type": "max", **creds},
        "remote_control": {"state": "active"},
        "quota": {"session": {"used_pct": 12, "resets": "5pm"},
                  "week": {"used_pct": 40, "resets": "Sep 26"}},
        "usage": {}})


def held_own_node(store, sign_in):
    store.add_node("erik-1", "erik", region="us-west", now=time.time())
    erik = sign_in(quota=1)
    store.hold_owner_node("erik-1", erik.account["id"], now=time.time())
    return erik


def test_an_owners_node_is_their_slot_on_their_page(site):  # noqa: F811
    store, sign_in, _ = site
    erik = held_own_node(store, sign_in)
    owner_said(store, logged_in=True)
    page = erik.page()
    body = whole_card(page, "erik-1")
    assert "<h2>erik-1 " in body and "In use" in body
    assert "your own machine" in body
    assert "max plan" in body and "Remote Control is on" in body
    assert "This week" in body, "the account's usage, from the node's own report"
    assert "Sign in again" in body and "Get a device token" in body


def test_an_owners_node_has_no_give_it_back(site):  # noqa: F811
    """Giving back means wiping, and nothing on somebody's own node is ours to wipe."""
    store, sign_in, _ = site
    erik = held_own_node(store, sign_in)
    owner_said(store, logged_in=True)
    body = whole_card(erik.page(), "erik-1")
    assert "Give this slot back" not in body and "/release" not in body
    reply = erik.press("/account/slots/erik-1/release", confirm="wipe")
    assert reply.status == 303 and "note=not-now" in reply.getheader("Location")
    assert store.get_slot("erik-1")["state"] == slots.ACTIVE


def test_an_owners_node_not_heard_from_claims_nothing(site):  # noqa: F811
    """No report yet: not signed in as far as anybody can say, and no usage."""
    store, sign_in, _ = site
    erik = held_own_node(store, sign_in)
    body = whole_card(erik.page(), "erik-1")
    assert "not heard from yet" in body and "Not signed in" in body
    assert "Signed in" not in body and "Remote Control is on" not in body


def test_an_owners_node_that_is_signed_out_says_so(site):  # noqa: F811
    store, sign_in, _ = site
    erik = held_own_node(store, sign_in)
    owner_said(store, logged_in=False)
    body = whole_card(erik.page(), "erik-1")
    assert "Not signed in" in body and "Sign in to Claude" in body
    assert "Remote Control is on" not in body


def test_signing_in_from_the_page_runs_the_nodes_own_sign_in(site):  # noqa: F811
    store, sign_in, _ = site
    erik = held_own_node(store, sign_in)
    owner_said(store, logged_in=False)
    assert erik.press("/account/slots/erik-1/signin").status == 303
    assert store.get_login("erik-1")["state"] == "requested"
    assert store.get_login("slot:erik-1") is None
    assert "Asking" in whole_card(erik.page(), "erik-1"), "the card follows the node's row"


def test_somebody_else_cannot_sign_in_on_an_owners_node(site):  # noqa: F811
    store, sign_in, _ = site
    held_own_node(store, sign_in)
    other = sign_in(sub="google-ana", email="ana@example.com", quota=1)
    reply = other.press("/account/slots/erik-1/signin")
    assert reply.status == 404
    assert store.get_login("erik-1") is None


def test_a_token_handed_over_for_an_owners_node_is_remembered_on_its_card(site):  # noqa: F811
    store, sign_in, _ = site
    erik = held_own_node(store, sign_in)
    owner_said(store, logged_in=True)
    with store._lock:
        store._conn.execute("UPDATE nodes SET device_token_at = ? WHERE id = 'erik-1'",
                            (time.time() - 3600,))
        store._conn.commit()
    assert "last issued" in whole_card(erik.page(), "erik-1")


# -- what the page says it keeps ---------------------------------------------------------

def test_the_privacy_page_says_a_slot_is_named_after_you(site):  # noqa: F811
    store, sign_in, _ = site
    erik = sign_in()
    page = erik.call("GET", "/privacy").body
    assert "named after you" in page and "before the @" in page


def test_the_docs_say_a_slot_is_a_whole_machine_named_after_you():
    from ccfleetd import customer_docs
    from ccfleetd.config import Config
    for path in ("/docs", "/docs/guide", "/docs/how-it-works"):
        page = customer_docs.page_for(path)(Config())
        assert "a whole machine, named after you" in page, path
    assert "Several slots share a machine" not in customer_docs.page_for(
        "/docs/how-it-works")(Config()), "one machine is one slot now"
