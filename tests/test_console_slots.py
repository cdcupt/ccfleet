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
from datetime import timedelta

import pytest

from ccfleetd import payments, slots
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
    store = Store(":memory:", max_slots_per_machine=8)
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    store.add_user("ana", hash_password("owner-password"), "owner", "ana", time.time())
    store.add_user("op", hash_password("operator-password"), "admin", "", time.time())

    def call(method, path, form=None, who="admin"):
        creds = {"admin": f"admin:{ADMIN_TOKEN}", "owner": "ana:owner-password",
                 "op": "op:operator-password"}.get(who)
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
    card = slots_card(call("GET", "/admin").body)
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
    page = call("GET", "/admin").body
    for card in (slots_card(page), accounts_card(page)):
        assert "<b>x</b>" not in card and "o&#x27;neil&amp;&lt;b&gt;x" in card
    assert "<img src=x" not in slots_card(page)
    assert "&lt;img src=x" in slots_card(page)


def test_an_owner_login_sees_no_slots_and_nobody(console):
    store, call = console
    shared(store)
    holder(store)
    page = call("GET", "/admin", who="owner").body
    assert 'id="slots"' not in page and 'id="accounts"' not in page
    assert "ana@example.com" not in page and "m1-01" not in page


def test_an_ordinary_owner_node_is_not_listed_as_a_machine(console):
    store, call = console
    store.add_node("laptop", "erik", now=time.time())
    page = call("GET", "/admin").body
    assert "No shared machines yet" in page and "ccfleetd slot add" in page


def test_a_one_slot_machine_is_listed(console):
    """Capacity one is an owner node's default, but one with a slot on it is a
    shared machine: hiding it would hide a slot nobody could take back."""
    store, call = console
    shared(store, node="solo", users=("slot01",))
    assert store.get_node("solo")["capacity"] == 1
    assert 'action="/actions/slot/solo-01/remove"' in slots_card(call("GET", "/admin").body)


def test_a_stuck_slot_says_why(console):
    store, call = console
    shared(store)
    ana = holder(store)
    store.claim_slot(ana["id"], now=time.time() - 3600)
    store.open_alert("m1", "slot_occupied:slot02", "critical",
                     "slot02 is free here but its Linux user exists", time.time())
    page = call("GET", "/admin").body
    assert "setting up for" in slots_card(page)
    assert "slot02 is free here but its Linux user exists" in slots_card(page)


def test_only_a_slot_still_setting_up_is_called_stuck(console):
    """A claim made just now is not stuck, and neither is one made an hour ago
    that the machine finished setting up."""
    store, call = console
    shared(store)
    store.claim_slot(holder(store)["id"], now=time.time())
    assert "setting up for" not in slots_card(call("GET", "/admin").body)
    ready = store.claim_slot(holder(store, email="bo@example.com")["id"],
                             now=time.time() - 3600)
    store.apply_slot_report("m1", [{"unix_user": ready["unix_user"], "present": True,
                                    "provisioned_for": ready["claimed_at"]}],
                            now=time.time())
    assert store.get_slot(ready["id"])["state"] == slots.CLAIMED
    assert "setting up for" not in slots_card(call("GET", "/admin").body)


def test_trouble_shows_on_the_slot_it_is_about_and_no_other(console):
    """Two machines both call their first slot slot01; an alert about one of
    them is not about the other."""
    store, call = console
    shared(store, node="m1")
    shared(store, node="m2")
    store.open_alert("m2", "slot_wipe_failed:slot01", "critical",
                     "wiping slot01 failed: userdel exited 8", time.time())
    assert slots_card(call("GET", "/admin").body).count("wiping slot01 failed") == 1


# -- what the operator can do ------------------------------------------------------

def test_setting_a_machines_capacity(console):
    store, call = console
    shared(store, users=())
    machine_said(store)          # a machine by its own word, before any slot is declared
    reply = call("POST", "/actions/machine/m1/capacity", {"count": "1"})
    assert reply.status == 303 and reply.getheader("Location") == "/admin#slots"
    assert store.get_node("m1")["capacity"] == 1
    assert "0 of 1 declared" in call("GET", "/admin").body


@pytest.mark.parametrize("count", ["2", "5"])
def test_capacity_above_one_is_refused_for_the_reason(console, count):
    """One machine is one slot: claude.ai/code shows a machine by its hostname."""
    store, call = console
    shared(store, users=())
    reply = call("POST", "/actions/machine/m1/capacity", {"count": count})
    assert reply.status == 400 and "one machine is one slot" in reply.body
    assert store.get_node("m1")["capacity"] == 0


@pytest.mark.parametrize("count", ["1", "-1", "two", "", "3.5", " 5", "\u00b2", "99999",
                                   "9" * 40])
def test_a_capacity_that_cannot_be_is_refused_in_words(console, count):
    """Below what is declared, not a number, or no number SQLite could hold:
    a sentence and a way back, never a 500."""
    store, call = console
    shared(store)
    reply = call("POST", "/actions/machine/m1/capacity", {"count": count})
    assert reply.status == 400 and 'href="/admin"' in reply.body
    assert store.get_node("m1")["capacity"] == 2


def test_capacity_for_a_machine_that_is_not_there(console):
    store, call = console
    assert call("POST", "/actions/machine/nowhere/capacity", {"count": "3"}).status == 400


def test_declaring_a_slot(console):
    store, call = console
    shared(store, users=(), capacity=1)
    bad = call("POST", "/actions/machine/m1/slot-add",
               {"slot_id": "m1-04", "unix_user": "Root User"})
    assert bad.status == 400 and store.get_slot("m1-04") is None
    reply = call("POST", "/actions/machine/m1/slot-add",
                 {"slot_id": "m1-03", "unix_user": "slot03"})
    assert reply.status == 303
    assert store.get_slot("m1-03")["state"] == slots.FREE


def test_a_second_slot_on_a_machine_is_refused_for_the_reason(console):
    store, call = console
    shared(store, users=("slot01",))
    reply = call("POST", "/actions/machine/m1/slot-add",
                 {"slot_id": "m1-02", "unix_user": "slot02"})
    assert reply.status == 400 and "one machine is one slot" in reply.body
    assert store.get_slot("m1-02") is None


def test_a_slot_declared_with_stray_spaces_is_still_declared(console):
    """Pasted names carry spaces; the console's other add form forgives them too."""
    store, call = console
    shared(store, users=(), capacity=1)
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
    page = call("GET", "/admin").body
    assert f'action="/actions/slot/{slot["id"]}/reclaim"' in page
    assert f'action="/actions/slot/{slot["id"]}/remove"' not in page
    free = "m1-02" if slot["id"] == "m1-01" else "m1-01"
    assert f'action="/actions/slot/{free}/remove"' in page
    assert f'action="/actions/slot/{free}/reclaim"' not in page
    # Being wiped: nothing to take back and not yet safe to forget.
    store.begin_release(slot["id"])
    page = call("GET", "/admin").body
    assert f'/actions/slot/{slot["id"]}/' not in page


def test_granting_and_reducing_an_allowance(console):
    store, call = console
    ana = holder(store, quota=0)
    reply = call("POST", f"/actions/account/{ana['id']}/allowance", {"count": "3"})
    assert reply.status == 303 and reply.getheader("Location") == "/admin#accounts"
    assert store.get_account(ana["id"])["slot_quota"] == 3
    assert call("POST", f"/actions/account/{ana['id']}/allowance",
                {"count": "-1"}).status == 400
    assert call("POST", "/actions/account/nobody/allowance", {"count": "1"}).status == 400
    assert store.get_account(ana["id"])["slot_quota"] == 3


# -- who may do it --------------------------------------------------------------------

def soon(days=30):
    """A day relative to today as the ledger counts it."""
    return (payments.today(time.time()) + timedelta(days=days)).isoformat()


@pytest.mark.parametrize("path,form", [
    ("/actions/machine/m1/capacity", {"count": "5"}),
    ("/actions/machine/m1/slot-add", {"slot_id": "m1-09", "unix_user": "slot09"}),
    ("/actions/slot/m1-01/reclaim", {"confirm": "m1-01"}),
    ("/actions/slot/m1-02/remove", {}),
    ("/actions/account/ACCOUNT/allowance", {"count": "9"}),
    ("/actions/account/ACCOUNT/payment", {"amount": "30", "currency": "USD",
                                          "through": soon()}),
    ("/actions/payment/PAYMENT/void", {}),
    ("/actions/machine/m1/reserve", {"email": "ana@example.com"}),
    ("/actions/machine/m1/unreserve", {}),
])
def test_an_owner_login_can_do_none_of_it(console, path, form):
    """Their credentials are fine; the action is not theirs. 403, not 401."""
    store, call = console
    shared(store)
    ana = holder(store)
    store.claim_slot(ana["id"], now=time.time())
    paid = store.record_payment(ana["id"], amount="30", currency="USD", through=soon(),
                                recorded_by="op", now=time.time())
    def everything():
        return (store.list_slots(), store.get_account(ana["id"]), store.get_node("m1"),
                store.list_payments())
    before = everything()
    reply = call("POST", path.replace("ACCOUNT", ana["id"]).replace("PAYMENT", str(paid)),
                 form, who="owner")
    assert reply.status == 403
    assert everything() == before


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


# -- payments -------------------------------------------------------------------------

def record(call, account_id, through, amount="30", currency="USD", note="", who="admin"):
    return call("POST", f"/actions/account/{account_id}/payment",
                {"amount": amount, "currency": currency, "through": through, "note": note},
                who=who)


def test_recording_a_payment_from_the_console(console):
    store, call = console
    ana = holder(store)
    reply = record(call, ana["id"], soon(), amount="30.5", currency="cny", note="WeChat")
    assert reply.status == 303 and reply.getheader("Location") == "/admin#accounts"
    [written] = store.list_payments(ana["id"])
    assert (written["amount_minor"], written["currency"], written["paid_through"]) == \
        (3050, "CNY", soon())
    card = accounts_card(call("GET", "/admin").body)
    assert f'paid through <span class="nowrap">{soon()}</span>' in card
    assert "30.50 CNY" in card and "WeChat" in card and "by admin token" in card


def test_the_ledger_says_which_operator_wrote_it(console):
    store, call = console
    ana = holder(store)
    assert record(call, ana["id"], soon(), who="op").status == 303
    assert store.list_payments(ana["id"])[0]["recorded_by"] == "op"


@pytest.mark.parametrize("field,typed", [
    ("amount", "thirty"), ("amount", "-5"), ("amount", "\u00b2"),
    ("currency", "US"), ("currency", "\u00dfd"),
    ("through", "2026-02-30"), ("through", "soon"), ("through", "2090-01-01"),
    ("note", "x" * (payments.NOTE_MAX + 1)),
])
def test_a_payment_the_ledger_cannot_read_is_refused_in_words(console, field, typed):
    store, call = console
    ana = holder(store)
    form = {"amount": "30", "currency": "USD", "through": soon(), "note": "", field: typed}
    reply = record(call, ana["id"], **{("through" if k == "through" else k): v
                                       for k, v in form.items()})
    assert reply.status == 400 and 'href="/admin"' in reply.body
    assert store.list_payments() == []


def test_a_payment_for_nobody_is_refused(console):
    store, call = console
    assert record(call, "nobody", soon()).status == 400
    assert store.list_payments() == []


def test_somebody_with_no_payments_says_so(console):
    store, call = console
    holder(store)
    assert " · no payments" in accounts_card(call("GET", "/admin").body)


def test_lapsed_is_called_out_only_while_it_still_matters(console):
    """Somebody who may still claim, or still holds, and has not paid: that is
    the operator's to act on. Somebody who has left has an ended payment."""
    store, call = console
    shared(store)
    ana = holder(store, quota=1)
    bo = holder(store, email="bo@example.com", quota=1)
    store.claim_slot(bo["id"], now=time.time())
    store.set_slot_quota(bo["id"], 0)                       # holds one, may claim no more
    gone = holder(store, email="cy@example.com", quota=0)
    for account in (ana, bo, gone):
        assert record(call, account["id"], soon(-3)).status == 303
    card = accounts_card(call("GET", "/admin").body)
    day = f'<span class="nowrap">{soon(-3)}</span>'
    called_out = f'<span class="bad-text">lapsed: paid through {day}</span>'
    assert card.count(called_out) == 2
    assert f"paid through {day}, ended" in card
    assert card.index("cy@example.com") < card.index(f"paid through {day}, ended")


def test_paid_up_is_not_lapsed_on_its_last_day(console):
    store, call = console
    ana = holder(store)
    record(call, ana["id"], soon(0))
    card = accounts_card(call("GET", "/admin").body)
    assert f'paid through <span class="nowrap">{soon(0)}</span>' in card
    assert "lapsed:" not in card


def test_voiding_a_payment_from_the_console(console):
    store, call = console
    ana = holder(store)
    record(call, ana["id"], soon(30))
    record(call, ana["id"], soon(60))
    newest, older = store.list_payments(ana["id"])
    reply = call("POST", f"/actions/payment/{newest['id']}/void", {})
    assert reply.status == 303 and reply.getheader("Location") == "/admin#accounts"
    card = accounts_card(call("GET", "/admin").body)
    assert f'paid through <span class="nowrap">{soon(30)}</span>' in card, \
        "a voided payment still counted"
    assert '<span class="pill disabled">voided</span>' in card
    assert f'action="/actions/payment/{newest["id"]}/void"' not in card
    assert f'action="/actions/payment/{older["id"]}/void"' in card
    assert call("POST", f"/actions/payment/{newest['id']}/void", {}).status == 400


@pytest.mark.parametrize("payment_id", ["abc", "\u00b2", "9" * 40, "-1", "404"])
def test_a_payment_that_cannot_be_is_refused_not_crashed(console, payment_id):
    """Quoted the way a browser sends it."""
    store, call = console
    path = f"/actions/payment/{urllib.parse.quote(payment_id)}/void"
    assert call("POST", path, {}).status == 400


def test_the_note_is_shown_not_run(console):
    store, call = console
    ana = holder(store)
    record(call, ana["id"], soon(), note="<script>alert(1)</script>")
    card = accounts_card(call("GET", "/admin").body)
    assert "<script>alert" not in card and "&lt;script&gt;alert(1)" in card


def test_the_form_offers_the_currency_they_paid_in_last(console):
    store, call = console
    ana = holder(store)
    assert 'name="currency" value="USD"' in accounts_card(call("GET", "/admin").body)
    record(call, ana["id"], soon(), currency="CNY")
    assert 'name="currency" value="CNY"' in accounts_card(call("GET", "/admin").body)



# -- versions and reboots ------------------------------------------------------------

def held_slot(store, pin=""):
    """A machine with one slot set up and held, and the machine's pin."""
    shared(store, users=("slot01",))
    if pin:
        store.set_pinned_version("m1", pin)
    slot = store.claim_slot(holder(store)["id"], now=time.time())
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=time.time())
    return slot


def machine_said(store, slot_entry=None, **payload):
    """The machine's latest heartbeat, as the console reads it."""
    body = {"node_id": "m1", "mode": "machine", "slots": [slot_entry] if slot_entry else []}
    body.update(payload)
    store.insert_heartbeat("m1", time.time(), body)


def test_each_slot_shows_the_claude_code_it_runs(console):
    store, call = console
    held_slot(store)
    machine_said(store, {"unix_user": "slot01", "claude": {"version": "2.1.278"}})
    assert "Claude Code 2.1.278" in slots_card(call("GET", "/admin").body)


def test_a_slot_behind_an_exact_pin_is_pending(console):
    store, call = console
    held_slot(store, pin="2.1.300")
    machine_said(store, {"unix_user": "slot01", "claude": {"version": "2.1.278"}})
    assert "update pending" in slots_card(call("GET", "/admin").body)


@pytest.mark.parametrize("pin,running", [("2.1.300", "2.1.300"), ("stable", "2.1.278"),
                                         ("", "2.1.278")])
def test_nothing_is_pending_when_nothing_can_be_behind(console, pin, running):
    """Met exactly, a channel with no number to compare, or no pin at all."""
    store, call = console
    held_slot(store, pin=pin)
    machine_said(store, {"unix_user": "slot01", "claude": {"version": running}})
    assert "update pending" not in slots_card(call("GET", "/admin").body)


def test_a_free_slot_is_never_pending(console):
    """Nobody holds it: its next holder gets whatever is current when they claim."""
    store, call = console
    shared(store, users=("slot01",))
    store.set_pinned_version("m1", "2.1.300")
    machine_said(store, {"unix_user": "slot01", "claude": {"version": "2.1.278"}})
    assert "update pending" not in slots_card(call("GET", "/admin").body)


def test_an_update_that_failed_says_why(console):
    store, call = console
    held_slot(store, pin="2.1.300")
    machine_said(store, {"unix_user": "slot01", "claude": {"version": "2.1.278"},
                         "upgrade": {"to": "2.1.300", "ok": False,
                                     "error": "<b>network</b> down"}})
    card = slots_card(call("GET", "/admin").body)
    assert "Claude Code update to 2.1.300 failed" in card
    assert "&lt;b&gt;network&lt;/b&gt; down" in card and "<b>network</b>" not in card


def test_a_machine_whose_os_wants_a_reboot_says_so(console):
    store, call = console
    held_slot(store)
    machine_said(store, reboot_required=True)
    assert "reboot needed" in slots_card(call("GET", "/admin").body)
    machine_said(store, reboot_required=False)
    assert "reboot needed" not in slots_card(call("GET", "/admin").body)


# -- the Claude account on a slot -------------------------------------------------------

def test_the_console_never_names_a_slots_claude_account(console):
    """The holder's own page says which of their accounts a slot is signed in
    to. Which Claude account a person uses is theirs to see, not the operator's."""
    store, call = console
    held_slot(store)
    machine_said(store, {"unix_user": "slot01", "credentials": {
        "logged_in": True, "subscription_type": "max",
        "email": "first.person@example.org", "refresh_expires_at": 1_800_000_000}})
    page = call("GET", "/admin").body
    assert "first.person" not in page and "example.org" not in page


# -- keeping a machine for one account ----------------------------------------------

def test_keeping_a_machine_for_somebody_from_the_console(console):
    store, call = console
    shared(store)
    ana = holder(store)
    assert "Reserved for" not in slots_card(call("GET", "/admin").body)
    reply = call("POST", "/actions/machine/m1/reserve", {"email": " ana@example.com "})
    assert reply.status == 303 and reply.getheader("Location") == "/admin#slots"
    assert store.get_node("m1")["reserved_for"] == ana["id"]
    assert "Reserved for ana@example.com" in slots_card(call("GET", "/admin").body)


@pytest.mark.parametrize("email", ["ana@exmaple.com", "", "   "])
def test_an_address_nobody_signed_in_with_is_refused_in_words(console, email):
    """A typo keeps the machine for nobody at all — or, worse, for somebody
    else. Refused with a sentence and a way back, and nothing changes."""
    store, call = console
    shared(store)
    holder(store)
    reply = call("POST", "/actions/machine/m1/reserve", {"email": email})
    assert reply.status == 400 and 'href="/admin"' in reply.body
    assert ("ana@exmaple.com" in reply.body) if email.strip() else ("email" in reply.body)
    assert store.get_node("m1")["reserved_for"] is None


def test_opening_a_kept_machine_again_from_the_console(console):
    store, call = console
    shared(store)
    ana = holder(store)
    store.reserve_machine("m1", ana["id"])
    card = slots_card(call("GET", "/admin").body)
    assert 'action="/actions/machine/m1/unreserve"' in card
    reply = call("POST", "/actions/machine/m1/unreserve", {})
    assert reply.status == 303 and reply.getheader("Location") == "/admin#slots"
    assert store.get_node("m1")["reserved_for"] is None
    card = slots_card(call("GET", "/admin").body)
    assert "Reserved for" not in card and "/unreserve" not in card


def test_an_owner_node_cannot_be_kept_from_the_console(console):
    store, call = console
    store.add_node("laptop", "erik", now=time.time())
    holder(store)
    reply = call("POST", "/actions/machine/laptop/reserve", {"email": "ana@example.com"})
    assert reply.status == 400 and "not a shared machine" in reply.body
    assert store.get_node("laptop")["reserved_for"] is None


def test_who_a_machine_is_kept_for_is_shown_not_run(console):
    store, call = console
    shared(store)
    odd = holder(store, email="o'neil&<b>x</b>@example.com")
    store.reserve_machine("m1", odd["id"])
    card = slots_card(call("GET", "/admin").body)
    assert "<b>x</b>" not in card
    assert "Reserved for o&#x27;neil&amp;&lt;b&gt;x&lt;/b&gt;@example.com" in card


def test_a_machine_kept_for_an_account_that_is_gone_still_says_so(console):
    """Accounts are not deleted today, but the page must not fall over, or go
    quiet about a machine nobody can claim, if one ever is."""
    store, call = console
    shared(store)
    with store._lock:
        store._conn.execute("UPDATE nodes SET reserved_for = 'u-gone' WHERE id = 'm1'")
        store._conn.commit()
    reply = call("GET", "/admin")
    assert reply.status == 200
    assert "Reserved for an account that no longer exists" in slots_card(reply.body)


# -- slot model v2: names, own machines, one slot each --------------------------------------

def test_a_held_slot_is_shown_by_its_holders_name(console):
    store, call = console
    held_slot(store)
    card = slots_card(call("GET", "/admin").body)
    assert "ana-1" in card, "the name the holder and claude.ai know it by"
    assert "m1-01" in card, "and the id every command takes"


def test_a_machine_says_when_its_hostname_is_not_its_slots_name_yet(console):
    """The machine renames itself on its next run; until then the console says
    the name it still answers to."""
    store, call = console
    held_slot(store)
    machine_said(store, hostname="m1")
    card = slots_card(call("GET", "/admin").body)
    assert "hostname pending" in card and "ana-1" in card
    machine_said(store, hostname="ana-1")
    assert "hostname pending" not in slots_card(call("GET", "/admin").body)


def test_a_machine_from_before_one_slot_each_is_flagged(console):
    """A database from before may still carry a machine with several slots.
    It answers to its own id, never one holder's name; the operator is told
    to take the extra slots off."""
    store, call = console
    shared(store)                                      # m1 with two slots
    card = slots_card(call("GET", "/admin").body)
    assert "more than one slot" in card
    shared(store, node="m2", users=("slot01",))
    one = slots_card(call("GET", "/admin").body)
    m2 = one[one.index('row-name">m2<'):]
    assert "more than one slot" not in m2[:m2.index("</div>")]


def test_a_machine_that_never_said_its_hostname_is_not_called_pending(console):
    store, call = console
    held_slot(store)
    assert "hostname pending" not in slots_card(call("GET", "/admin").body)


def test_an_owner_node_counted_as_a_slot_is_listed_as_their_own_machine(console):
    store, call = console
    store.add_node("erik-1", "erik", now=time.time())
    erik = holder(store, email="cdcupt@gmail.com")
    store.hold_owner_node("erik-1", erik["id"], now=time.time())
    card = slots_card(call("GET", "/admin").body)
    assert "erik-1" in card and "own machine" in card and "cdcupt@gmail.com" in card
    # Nothing that would act on somebody's own node: no wipe, no forms for slots.
    for action in ("/actions/slot/erik-1/reclaim", "/actions/slot/erik-1/remove",
                   "/actions/machine/erik-1/capacity", "/actions/machine/erik-1/slot-add",
                   "/actions/machine/erik-1/reserve"):
        assert f'action="{action}"' not in card


def test_taking_back_an_owners_node_is_refused_even_by_a_hand_made_form(console):
    store, call = console
    store.add_node("erik-1", "erik", now=time.time())
    erik = holder(store, email="cdcupt@gmail.com")
    store.hold_owner_node("erik-1", erik["id"], now=time.time())
    reply = call("POST", "/actions/slot/erik-1/reclaim", {"confirm": "erik-1"})
    assert reply.status == 400 and "own machine" in reply.body
    assert store.get_slot("erik-1")["state"] == slots.ACTIVE
