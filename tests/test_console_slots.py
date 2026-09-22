"""The console's half of slots: machines, slots, and the people who hold them.

The operator's alone. The tests that matter most are about reach: an owner's
console login sees none of it and can do none of it, taking a slot back needs
the slot's id typed out, and there is no action here that acts as a user.
"""

from __future__ import annotations

import base64
import http.client
import threading
import time
import urllib.parse

import pytest

from ccfleetd import slots
from ccfleetd.api import Context, build_server, csrf_token
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.passwords import hash_password
from ccfleetd.store import Store, slot_login_key

ADMIN_TOKEN = "admin-token-long-enough-to-pass"


@pytest.fixture
def console():
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN)
    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    store.add_user("ana", hash_password("owner-password"), "owner", "ana", time.time())

    def call(method, path, form=None, who="admin"):
        creds = {"admin": f"admin:{ADMIN_TOKEN}", "owner": "ana:owner-password"}.get(who)
        headers = {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()} \
            if creds else {}
        body = None
        if form is not None:
            body = urllib.parse.urlencode({"csrf": csrf_token(cfg), **form}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers)
        reply = conn.getresponse()
        reply.body = reply.read().decode("utf-8", "replace")
        conn.close()
        return reply

    yield store, call
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    store.close()


def shared(store, node="m1", users=("slot01", "slot02"), capacity=None):
    store.add_node(node, "op", now=time.time())
    store.set_machine_capacity(node, capacity or len(users))
    for n, user in enumerate(users, 1):
        store.add_slot(f"{node}-{n:02d}", node, user, now=time.time())
    store.apply_slot_report(node, [{"unix_user": u, "present": False} for u in users],
                            now=time.time())


def slots_card(page):
    """The Slots card alone; the Alerts card above it lists every alert anyway."""
    return page[page.index('id="slots"'):page.index('id="accounts"')]


def accounts_card(page):
    return page[page.index('id="accounts"'):]


def holder(store, email="ana@example.com", quota=1):
    account = store.upsert_account_from_google(f"sub-{email}", email, now=time.time())
    store.set_slot_quota(account["id"], quota)
    return account


# -- what the operator sees ------------------------------------------------------

def test_the_operator_sees_every_slot_and_who_holds_it(console):
    store, call = console
    shared(store)
    ana = holder(store)
    store.claim_slot(ana["id"], now=time.time())
    card = slots_card(call("GET", "/").body)
    assert "m1-01" in card and "m1-02" in card
    assert "ana@example.com" in card
    assert ">claiming<" in card and ">free<" in card


def test_what_people_and_machines_wrote_is_shown_not_run(console):
    """An address is whatever Google says it is, and a wipe error is whatever
    the machine said. Both reach the operator's page as text."""
    store, call = console
    shared(store)
    odd = holder(store, email="o'neil&<b>x</b>@example.com")
    store.claim_slot(odd["id"], now=time.time())
    store.open_alert("m1", "slot_wipe_failed:slot02", "critical",
                     "wipe failed: <img src=x onerror=alert(1)>", time.time())
    page = call("GET", "/").body
    for card in (slots_card(page), accounts_card(page)):
        assert "<b>x</b>" not in card and "o&#x27;neil&amp;&lt;b&gt;x" in card
    assert "<img src=x" not in slots_card(page)
    assert "&lt;img src=x" in slots_card(page)


def test_an_owner_login_sees_no_slots_and_nobody(console):
    store, call = console
    shared(store)
    holder(store)
    page = call("GET", "/", who="owner").body
    assert 'id="slots"' not in page and 'id="accounts"' not in page
    assert "ana@example.com" not in page and "m1-01" not in page


def test_an_ordinary_owner_node_is_not_listed_as_a_machine(console):
    store, call = console
    store.add_node("laptop", "erik", now=time.time())
    page = call("GET", "/").body
    assert "No shared machines yet" in page and "ccfleetd slot capacity" in page


def test_a_one_slot_machine_is_listed(console):
    """Capacity one is an owner node's default, but one with a slot on it is a
    shared machine: hiding it would hide a slot nobody could take back."""
    store, call = console
    shared(store, node="solo", users=("slot01",))
    assert store.get_node("solo")["capacity"] == 1
    assert 'action="/actions/slot/solo-01/remove"' in slots_card(call("GET", "/").body)


def test_a_stuck_slot_says_why(console):
    store, call = console
    shared(store)
    ana = holder(store)
    store.claim_slot(ana["id"], now=time.time() - 3600)
    store.open_alert("m1", "slot_occupied:slot02", "critical",
                     "slot02 is free here but its Linux user exists", time.time())
    page = call("GET", "/").body
    assert "setting up for" in slots_card(page)
    assert "slot02 is free here but its Linux user exists" in slots_card(page)


def test_only_a_slot_still_setting_up_is_called_stuck(console):
    """A claim made just now is not stuck, and neither is one made an hour ago
    that the machine finished setting up."""
    store, call = console
    shared(store)
    store.claim_slot(holder(store)["id"], now=time.time())
    assert "setting up for" not in slots_card(call("GET", "/").body)
    ready = store.claim_slot(holder(store, email="bo@example.com")["id"],
                             now=time.time() - 3600)
    store.apply_slot_report("m1", [{"unix_user": ready["unix_user"], "present": True,
                                    "provisioned_for": ready["claimed_at"]}],
                            now=time.time())
    assert store.get_slot(ready["id"])["state"] == slots.CLAIMED
    assert "setting up for" not in slots_card(call("GET", "/").body)


def test_trouble_shows_on_the_slot_it_is_about_and_no_other(console):
    """Two machines both call their first slot slot01; an alert about one of
    them is not about the other."""
    store, call = console
    shared(store, node="m1")
    shared(store, node="m2")
    store.open_alert("m2", "slot_wipe_failed:slot01", "critical",
                     "wiping slot01 failed: userdel exited 8", time.time())
    assert slots_card(call("GET", "/").body).count("wiping slot01 failed") == 1


# -- what the operator can do ------------------------------------------------------

def test_setting_a_machines_capacity(console):
    store, call = console
    shared(store)
    reply = call("POST", "/actions/machine/m1/capacity", {"count": "5"})
    assert reply.status == 303 and reply.getheader("Location") == "/#slots"
    assert store.get_node("m1")["capacity"] == 5
    assert "2 of 5 declared" in call("GET", "/").body


@pytest.mark.parametrize("count", ["1", "-1", "two", "", "3.5", " 5", "\u00b2", "99999",
                                   "9" * 40])
def test_a_capacity_that_cannot_be_is_refused_in_words(console, count):
    """Below what is declared, not a number, or no number SQLite could hold:
    a sentence and a way back, never a 500."""
    store, call = console
    shared(store)
    reply = call("POST", "/actions/machine/m1/capacity", {"count": count})
    assert reply.status == 400 and 'href="/"' in reply.body
    assert store.get_node("m1")["capacity"] == 2


def test_capacity_for_a_machine_that_is_not_there(console):
    store, call = console
    assert call("POST", "/actions/machine/nowhere/capacity", {"count": "3"}).status == 400


def test_declaring_a_slot(console):
    store, call = console
    shared(store, capacity=3)
    reply = call("POST", "/actions/machine/m1/slot-add",
                 {"slot_id": "m1-03", "unix_user": "slot03"})
    assert reply.status == 303
    assert store.get_slot("m1-03")["state"] == slots.FREE
    bad = call("POST", "/actions/machine/m1/slot-add",
               {"slot_id": "m1-04", "unix_user": "Root User"})
    assert bad.status == 400 and store.get_slot("m1-04") is None


def test_a_slot_declared_with_stray_spaces_is_still_declared(console):
    """Pasted names carry spaces; the console's other add form forgives them too."""
    store, call = console
    shared(store, capacity=3)
    reply = call("POST", "/actions/machine/m1/slot-add",
                 {"slot_id": " m1-03 ", "unix_user": " slot03\t"})
    assert reply.status == 303 and store.get_slot("m1-03")["unix_user"] == "slot03"


def test_taking_a_slot_back_needs_its_id_typed(console):
    store, call = console
    shared(store)
    ana = holder(store)
    slot = store.claim_slot(ana["id"], now=time.time())
    for typed in ("yes", "", f" {slot['id']}", slot["id"].upper()):
        refused = call("POST", f"/actions/slot/{slot['id']}/reclaim", {"confirm": typed})
        assert refused.status == 400
    assert store.get_slot(slot["id"])["state"] == slots.CLAIMING
    taken = call("POST", f"/actions/slot/{slot['id']}/reclaim", {"confirm": slot["id"]})
    assert taken.status == 303
    assert store.get_slot(slot["id"])["state"] == slots.RELEASING


def test_taking_back_a_signed_in_slot_takes_its_sign_in_too(console):
    store, call = console
    shared(store)
    ana = holder(store)
    slot = store.claim_slot(ana["id"], now=time.time())
    store.apply_slot_report("m1", [{"unix_user": slot["unix_user"], "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=time.time())
    store.request_slot_login(slot["id"], "", time.time(), kind="token")
    call("POST", f"/actions/slot/{slot['id']}/reclaim", {"confirm": slot["id"]})
    assert store.get_login(slot_login_key(slot["id"])) is None


def test_taking_back_a_slot_already_on_its_way_out_is_refused_not_crashed(console):
    store, call = console
    shared(store)
    ana = holder(store)
    slot = store.claim_slot(ana["id"], now=time.time())
    store.begin_release(slot["id"])
    reply = call("POST", f"/actions/slot/{slot['id']}/reclaim", {"confirm": slot["id"]})
    assert reply.status == 400


def test_removing_only_a_free_slot(console):
    """Free means the wipe finished, so there is nothing to lose and nothing to type."""
    store, call = console
    shared(store)
    ana = holder(store)
    slot = store.claim_slot(ana["id"], now=time.time())
    assert call("POST", f"/actions/slot/{slot['id']}/remove", {}).status == 400
    assert store.get_slot(slot["id"]) is not None
    assert call("POST", "/actions/slot/m1-02/remove", {}).status == 303
    assert store.get_slot("m1-02") is None


def test_the_page_offers_take_back_on_a_held_slot_and_remove_on_a_free_one(console):
    store, call = console
    shared(store)
    slot = store.claim_slot(holder(store)["id"], now=time.time())
    page = call("GET", "/").body
    assert f'action="/actions/slot/{slot["id"]}/reclaim"' in page
    assert f'action="/actions/slot/{slot["id"]}/remove"' not in page
    free = "m1-02" if slot["id"] == "m1-01" else "m1-01"
    assert f'action="/actions/slot/{free}/remove"' in page
    assert f'action="/actions/slot/{free}/reclaim"' not in page
    # Being wiped: nothing to take back and not yet safe to forget.
    store.begin_release(slot["id"])
    page = call("GET", "/").body
    assert f'/actions/slot/{slot["id"]}/' not in page


def test_granting_and_reducing_an_allowance(console):
    store, call = console
    ana = holder(store, quota=0)
    reply = call("POST", f"/actions/account/{ana['id']}/allowance", {"count": "3"})
    assert reply.status == 303 and reply.getheader("Location") == "/#accounts"
    assert store.get_account(ana["id"])["slot_quota"] == 3
    assert call("POST", f"/actions/account/{ana['id']}/allowance",
                {"count": "-1"}).status == 400
    assert call("POST", "/actions/account/nobody/allowance", {"count": "1"}).status == 400
    assert store.get_account(ana["id"])["slot_quota"] == 3


# -- who may do it --------------------------------------------------------------------

@pytest.mark.parametrize("path,form", [
    ("/actions/machine/m1/capacity", {"count": "5"}),
    ("/actions/machine/m1/slot-add", {"slot_id": "m1-09", "unix_user": "slot09"}),
    ("/actions/slot/m1-01/reclaim", {"confirm": "m1-01"}),
    ("/actions/slot/m1-02/remove", {}),
    ("/actions/account/ACCOUNT/allowance", {"count": "9"}),
])
def test_an_owner_login_can_do_none_of_it(console, path, form):
    """Their credentials are fine; the action is not theirs. 403, not 401."""
    store, call = console
    shared(store)
    ana = holder(store)
    store.claim_slot(ana["id"], now=time.time())
    before = (store.list_slots(), store.get_account(ana["id"]), store.get_node("m1"))
    reply = call("POST", path.replace("ACCOUNT", ana["id"]), form, who="owner")
    assert reply.status == 403
    assert (store.list_slots(), store.get_account(ana["id"]), store.get_node("m1")) == before


def test_nobody_signed_in_can_do_any_of_it(console):
    store, call = console
    shared(store)
    assert call("POST", "/actions/machine/m1/capacity", {"count": "5"}, who=None).status == 401


def test_there_is_no_action_that_acts_as_a_user(console):
    """No sign-in on somebody's behalf, no code typed for them, no token read
    for them. The operator takes slots back; they do not stand in for anybody."""
    store, call = console
    shared(store)
    for action in ("signin", "code", "token", "token-show", "login-start", "cancel"):
        assert call("POST", f"/actions/slot/m1-01/{action}", {"code": "x"}).status == 404


def test_an_unknown_kind_of_thing_is_not_found(console):
    store, call = console
    assert call("POST", "/actions/machine/m1/explode", {}).status == 404
