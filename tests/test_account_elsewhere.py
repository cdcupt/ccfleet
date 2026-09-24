"""One Claude account, one node — checked, not trusted: the server's half.

Every node and every slot says which account it is signed in to, as a
fingerprint (a digest of the account's id, never the id). Two live places with
one fingerprint are one account on two nodes, and both are flagged: to the
operator as an alert, to a slot's holder on their card without naming the other
place, which may be somebody else's. And when a slot refuses a sign-in with
another account, its holder is told why.
"""

from __future__ import annotations

import time

import pytest

from ccfleetd import rules, slots, usersite
from ccfleetd.config import Config
from ccfleetd.desired import desired_state
from ccfleetd.heartbeat import validate_heartbeat
from ccfleetd.monitor import LOGIN_MAX_AGE_S, Monitor
from ccfleetd.store import slot_login_key

from . import test_usersite
from .test_monitor import Recorder
from .test_usersite import claimed, machine, report

# The live server and Google sign-in the other page tests use (a fixture).
site = test_usersite.site

NOW = 2_000_000.0
FP = "0123456789abcdef"
OTHER_FP = "fedcba9876543210"


# -- what a node may say -----------------------------------------------------------------

@pytest.mark.parametrize("fp", [FP, OTHER_FP])
def test_a_fingerprint_is_kept_from_a_node_and_from_a_slot(fp):
    node = validate_heartbeat({"node_id": "n", "credentials": {"account_fp": fp}}, "n")
    assert node["credentials"]["account_fp"] == fp
    machine_ = validate_heartbeat({"node_id": "m", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": {"account_fp": fp}}]}, "m")
    assert machine_["slots"][0]["credentials"]["account_fp"] == fp


@pytest.mark.parametrize("fp", ["0123456789ABCDEF", FP[:-1], FP + "0", "g" * 16,
                                "0123-45678-9abcd", 1234567890123456, None, [FP]])
def test_anything_that_is_not_a_fingerprint_is_not_kept(fp):
    node = validate_heartbeat({"node_id": "n", "credentials": {"account_fp": fp}}, "n")
    assert node["credentials"]["account_fp"] is None
    machine_ = validate_heartbeat({"node_id": "m", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": {"account_fp": fp}}]}, "m")
    assert machine_["slots"][0]["credentials"]["account_fp"] is None


# -- where each account is live ----------------------------------------------------------

def beat(ts, **credentials):
    return {"ts": ts, "payload": {"credentials": {"logged_in": True, **credentials}}}


def machine_beat(ts, *entries):
    return {"ts": ts, "payload": {"mode": "machine", "slots": list(entries)}}


def slot_entry(user, **credentials):
    return {"unix_user": user, "credentials": {"logged_in": True, **credentials}}


def fleet():
    """An owner node and a shared machine with one held slot, and their rows."""
    nodes = [{"id": "erik-1", "enabled": True}, {"id": "pool-1", "enabled": True}]
    slot_rows = [{"id": "pool-1-a", "node_id": "pool-1", "unix_user": "slot01",
                  "state": slots.ACTIVE},
                 {"id": "pool-1-b", "node_id": "pool-1", "unix_user": "slot02",
                  "state": slots.FREE}]
    return nodes, slot_rows


def places_of(latest, nodes=None, slot_rows=None, now=NOW):
    nodes_, rows_ = fleet()
    return rules.account_places(nodes or nodes_, latest, slot_rows or rows_, now, Config())


def test_one_account_on_a_node_and_a_slot_is_live_in_both():
    latest = {"erik-1": beat(NOW, account_fp=FP),
              "pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=FP))}
    assert places_of(latest) == {FP: ["erik-1", "pool-1-a"]}


def test_a_node_gone_quiet_is_not_counted():
    stale = NOW - Config().heartbeat_max_age_s - 1
    latest = {"erik-1": beat(stale, account_fp=FP),
              "pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=FP))}
    assert places_of(latest) == {FP: ["pool-1-a"]}


def test_a_disabled_node_is_not_counted():
    nodes = [{"id": "erik-1", "enabled": False}, {"id": "pool-1", "enabled": True}]
    latest = {"erik-1": beat(NOW, account_fp=FP)}
    assert places_of(latest, nodes=nodes) == {}


@pytest.mark.parametrize("credentials", [{"logged_in": False, "account_fp": FP},
                                         {"logged_in": None, "account_fp": FP},
                                         {"account_fp": None}])
def test_a_sign_in_that_does_not_work_or_names_nobody_is_not_counted(credentials):
    latest = {"erik-1": {"ts": NOW, "payload": {"credentials": credentials}},
              "pool-1": machine_beat(NOW, {"unix_user": "slot01", "credentials": credentials})}
    assert places_of(latest) == {}


@pytest.mark.parametrize("state", [slots.FREE, slots.CLAIMING, slots.RELEASING])
def test_a_slot_nobody_holds_is_not_counted(state):
    nodes, rows = fleet()
    rows[0]["state"] = state
    latest = {"pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=FP))}
    assert places_of(latest, slot_rows=rows) == {}


def test_a_slot_the_server_does_not_know_is_not_counted():
    latest = {"pool-1": machine_beat(NOW, slot_entry("slot09", account_fp=FP))}
    assert places_of(latest) == {}


# -- the alert -------------------------------------------------------------------------

def evaluate(node, latest, places, slot_rows=(), names=None):
    return {f.rule: f for f in rules.evaluate(node, latest, None, NOW, Config(), slot_rows,
                                              places, names)}


def named_fleet():
    """The fleet, with its held slot named after its holder, as a claim names it."""
    nodes, rows = fleet()
    rows[0]["name"] = "ana-1"
    return nodes, rows


def test_both_alerts_call_a_held_slot_by_its_holders_name():
    """Erik, 2026-09-24: an alert says a held slot's name, never the slot's id
    or the machine it is on, and names the other place the same way."""
    nodes, rows = named_fleet()
    latest = {"erik-1": beat(NOW, account_fp=FP),
              "pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=FP))}
    places, names = places_of(latest, slot_rows=rows), rules.place_names(rows)
    on_node = evaluate(nodes[0], latest["erik-1"], places, names=names)["account_elsewhere"]
    on_slot = evaluate(nodes[1], latest["pool-1"], places, rows, names)["account_elsewhere:slot01"]
    assert on_node.message.endswith("also signed in on ana-1: one account, one node")
    assert on_slot.message.startswith("the Claude account on ana-1 is also signed in on erik-1")
    assert "pool-1" not in on_node.message + on_slot.message


def test_a_slot_on_another_account_is_called_by_its_holders_name():
    nodes, rows = named_fleet()
    entry = {"unix_user": "slot01", "credentials": {"logged_in": True, "account_fp": OTHER_FP,
                                                    "bound_fp": FP}}
    latest = {"pool-1": machine_beat(NOW, entry)}
    found = evaluate(nodes[1], latest["pool-1"], places_of(latest, slot_rows=rows), rows)
    message = found["account_changed:slot01"].message
    assert message.startswith("ana-1 is signed in to another Claude account")
    assert "pool-1" not in message


def test_a_place_nobody_holds_by_name_keeps_its_id():
    nodes, rows = fleet()
    assert rules.place_names(rows) == {"pool-1-a": "pool-1-a", "pool-1-b": "pool-1-b"}
    assert rules.place_names(named_fleet()[1])["pool-1-a"] == "ana-1"


def test_both_places_are_flagged_each_naming_the_other():
    nodes, rows = fleet()
    latest = {"erik-1": beat(NOW, account_fp=FP),
              "pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=FP))}
    places = places_of(latest)
    on_node = evaluate(nodes[0], latest["erik-1"], places)
    on_machine = evaluate(nodes[1], latest["pool-1"], places, rows)
    assert "pool-1-a" in on_node["account_elsewhere"].message
    assert on_node["account_elsewhere"].level == rules.LEVEL_CRITICAL
    assert "erik-1" in on_machine["account_elsewhere:slot01"].message
    assert FP not in on_node["account_elsewhere"].message, "the fingerprint is not a name"


def test_an_account_live_in_one_place_is_not_flagged():
    nodes, rows = fleet()
    latest = {"erik-1": beat(NOW, account_fp=FP),
              "pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=OTHER_FP))}
    places = places_of(latest)
    assert not any(r.startswith("account_elsewhere")
                   for r in evaluate(nodes[0], latest["erik-1"], places))
    assert not any(r.startswith("account_elsewhere")
                   for r in evaluate(nodes[1], latest["pool-1"], places, rows))


def test_a_place_that_is_not_live_itself_is_not_flagged():
    """A node gone quiet is not where its account is any more, for anybody."""
    nodes, rows = fleet()
    stale = NOW - Config().heartbeat_max_age_s - 1
    latest = {"erik-1": beat(stale, account_fp=FP),
              "pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=FP))}
    places = places_of(latest)
    assert "account_elsewhere" not in evaluate(nodes[0], latest["erik-1"], places)
    assert "account_elsewhere:slot01" not in evaluate(nodes[1], latest["pool-1"], places, rows)


def test_two_slots_on_one_machine_with_one_account_are_both_flagged():
    nodes, rows = fleet()
    rows[1]["state"] = slots.ACTIVE
    latest = {"pool-1": machine_beat(NOW, slot_entry("slot01", account_fp=FP),
                                     slot_entry("slot02", account_fp=FP))}
    found = evaluate(nodes[1], latest["pool-1"], places_of(latest, slot_rows=rows), rows)
    assert "pool-1-b" in found["account_elsewhere:slot01"].message
    assert "pool-1-a" in found["account_elsewhere:slot02"].message


def test_the_alert_opens_on_both_and_closes_when_it_stops(store, cfg):
    store.add_node("erik-1", "erik", now=NOW - 60)
    store.add_node("pool-1", "op", now=NOW - 60)
    store.set_machine_capacity("pool-1", 1)
    store.add_slot("pool-1-a", "pool-1", "slot01", now=NOW - 60)
    store.apply_slot_report("pool-1", [{"unix_user": "slot01", "present": False}], now=NOW - 60)
    store.add_account("a1", "sub-a1", "a1@example.com", slot_quota=1, now=NOW - 60)
    slot = store.claim_slot("a1", now=NOW - 50)
    store.apply_slot_report("pool-1", [{"unix_user": "slot01", "present": True,
                                        "provisioned_for": slot["claimed_at"]}], now=NOW - 40)
    monitor = Monitor(store, cfg, Recorder(), clock=lambda: NOW)
    owner = {"credentials": {"logged_in": True, "account_fp": FP}}
    shared = {"mode": "machine", "slots": [slot_entry("slot01", account_fp=FP)]}

    monitor.record_heartbeat(store.get_node("erik-1"), owner, NOW)
    monitor.record_heartbeat(store.get_node("pool-1"), shared, NOW)
    monitor.check_all(NOW)
    rules_open = {(a["node_id"], a["rule"]) for a in store.open_alerts()}
    assert ("erik-1", "account_elsewhere") in rules_open
    assert ("pool-1", "account_elsewhere:slot01") in rules_open
    # Said by the holder's name, which the claim gave the slot.
    name = store.get_slot("pool-1-a")["name"]
    assert name and name != "pool-1-a"
    said = {a["rule"]: a["message"] for a in store.open_alerts()}
    assert f"also signed in on {name}:" in said["account_elsewhere"]
    assert said["account_elsewhere:slot01"].startswith(f"the Claude account on {name} ")

    owner["credentials"]["logged_in"] = False
    monitor.record_heartbeat(store.get_node("erik-1"), owner, NOW + 1)
    monitor.check_all(NOW + 1)
    assert not [a for a in store.open_alerts() if a["rule"].startswith("account_elsewhere")]


def test_a_bound_fingerprint_is_kept_exactly_as_the_other_is():
    good = validate_heartbeat({"node_id": "m", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": {"bound_fp": FP}}]}, "m")
    assert good["slots"][0]["credentials"]["bound_fp"] == FP
    bad = validate_heartbeat({"node_id": "m", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": {"bound_fp": FP.upper()}}]}, "m")
    assert bad["slots"][0]["credentials"]["bound_fp"] is None


# -- a slot on another account than its own ------------------------------------------------

def changed_findings(state=slots.ACTIVE, **credentials):
    nodes, rows = fleet()
    rows[0]["state"] = state
    entry = {"unix_user": "slot01", "credentials": {"logged_in": True, **credentials}}
    latest = {"pool-1": machine_beat(NOW, entry)}
    return evaluate(nodes[1], latest["pool-1"], places_of(latest, slot_rows=rows), rows)


def test_a_slot_on_another_account_than_its_own_is_flagged():
    found = changed_findings(account_fp=OTHER_FP, bound_fp=FP)
    assert "pool-1-a" in found["account_changed:slot01"].message
    assert found["account_changed:slot01"].level == rules.LEVEL_CRITICAL


@pytest.mark.parametrize("credentials", [
    {"account_fp": FP, "bound_fp": FP},                           # its own
    {"account_fp": OTHER_FP, "bound_fp": None},                   # not bound yet
    {"account_fp": None, "bound_fp": FP},                         # cannot say
    {"account_fp": OTHER_FP, "bound_fp": FP, "logged_in": False},  # not signed in
])
def test_a_slot_on_its_own_account_or_that_cannot_tell_is_not_flagged(credentials):
    assert "account_changed:slot01" not in changed_findings(**credentials)


@pytest.mark.parametrize("state", [slots.FREE, slots.CLAIMING, slots.RELEASING])
def test_a_slot_nobody_holds_is_not_flagged_for_its_account(state):
    assert "account_changed:slot01" not in changed_findings(state, account_fp=OTHER_FP,
                                                            bound_fp=FP)


# -- a refused sign-in, told to its holder -----------------------------------------------

REFUSED = ("this slot stays with the Claude account it was first signed in with; to use "
           "another account, hold another slot")


def active_slot(store):
    store.add_node("m1", "op", now=NOW)
    store.set_machine_capacity("m1", 1)
    store.add_slot("m1-a", "m1", "slot01", now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW)
    store.add_account("a1", "sub-a1", "a1@example.com", slot_quota=1, now=NOW)
    slot = store.claim_slot("a1", now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=NOW + 1)
    return store.get_slot("m1-a")


def test_a_refused_sign_in_is_kept_for_its_holder_with_nothing_else(store):
    slot = active_slot(store)
    key = slot_login_key(slot["id"])
    store.request_slot_login(slot["id"], "", NOW + 2, held_by="a1")
    store.record_login_progress(key, "url_ready", "https://claude.com/x", "", NOW + 3,
                                requested_at=NOW + 2)
    store.submit_slot_login_code(slot["id"], "the-code", NOW + 4, held_by="a1")
    store.record_login_progress(key, "failed", "", REFUSED, NOW + 5, requested_at=NOW + 2)
    row = store.get_login(key)
    assert row["state"] == "failed" and row["detail"] == REFUSED
    assert row["code"] == "" and row["url"] == "" and row["secret"] == ""


def test_a_nodes_own_failed_sign_in_still_goes_away(store):
    store.add_node("erik-1", "erik", now=NOW)
    store.request_login("erik-1", "", NOW)
    store.record_login_progress("erik-1", "failed", "", "whatever", NOW + 1, requested_at=NOW)
    assert store.get_login("erik-1") is None


def test_a_refused_sign_in_is_never_asked_of_the_machine_again(store):
    slot = active_slot(store)
    key = slot_login_key(slot["id"])
    store.request_slot_login(slot["id"], "", NOW + 2, held_by="a1")
    store.record_login_progress(key, "failed", "", REFUSED, NOW + 3, requested_at=NOW + 2)
    rows = store.list_slots(node_id="m1")
    [block] = desired_state(store.get_node("m1"), None, rows,
                            {s["id"]: store.get_login(slot_login_key(s["id"])) for s in rows}
                            )["slots"]
    assert "login" not in block


def test_a_new_sign_in_replaces_the_refused_one_and_the_sweep_takes_it(store):
    slot = active_slot(store)
    key = slot_login_key(slot["id"])
    store.request_slot_login(slot["id"], "", NOW + 2, held_by="a1")
    store.record_login_progress(key, "failed", "", REFUSED, NOW + 3, requested_at=NOW + 2)
    store.request_slot_login(slot["id"], "", NOW + 4, held_by="a1")
    assert store.get_login(key)["state"] == "requested"
    store.record_login_progress(key, "failed", "", REFUSED, NOW + 5, requested_at=NOW + 4)
    store.expire_logins(NOW + 5 + LOGIN_MAX_AGE_S + 1)
    assert store.get_login(key) is None


# -- the holder's page --------------------------------------------------------------------

def in_use(store, browser):
    slot = claimed(store, browser)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": True, "account_fp": FP},
                          "remote_control": {"state": "active"}}])
    return store.get_slot(slot["id"])


def refuse(store, slot, kind="login"):
    key = slot_login_key(slot["id"])
    store.request_slot_login(slot["id"], "", time.time(), kind=kind, held_by=slot["held_by"])
    store.record_login_progress(key, "failed", "", REFUSED, time.time(),
                                requested_at=store.get_login(key)["requested_at"])


def test_the_holder_reads_why_a_sign_in_was_not_kept_and_can_start_again(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    refuse(store, slot)
    page = erik.page()
    assert f"The sign-in was not kept: {REFUSED}." in page
    assert ">Sign in again<" in page and "Cancel" not in page


def test_a_token_that_failed_says_so_and_offers_another(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    refuse(store, slot, kind="token")
    page = erik.page()
    assert "The device token was not made:" in page and ">Get a device token<" in page


def test_the_reason_is_escaped(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    key = slot_login_key(slot["id"])
    store.request_slot_login(slot["id"], "", time.time(), held_by=slot["held_by"])
    store.record_login_progress(key, "failed", "", "<b>bold</b>", time.time(),
                                requested_at=store.get_login(key)["requested_at"])
    page = erik.page()
    assert "<b>bold</b>" not in page and "&lt;b&gt;bold&lt;/b&gt;" in page


def test_the_holder_is_told_their_account_is_live_elsewhere_but_not_where(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    assert usersite.ELSEWHERE not in erik.page()
    store.open_alert("m1", f"account_elsewhere:{slot['unix_user']}", "critical",
                     "the Claude account on m1-01 is also signed in on someone-else-1", NOW)
    page = erik.page()
    assert usersite.ELSEWHERE in page
    assert "someone-else-1" not in page


def test_the_holder_is_told_their_slot_is_on_another_account_than_its_own(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    assert usersite.CHANGED not in erik.page()
    store.open_alert("m1", f"account_changed:{slot['unix_user']}", "critical", "x", NOW)
    page = erik.page()
    assert usersite.CHANGED in page and usersite.ELSEWHERE not in page


def test_another_slots_alert_is_not_this_slots(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    in_use(store, erik)
    store.open_alert("m1", "account_elsewhere:slot99", "critical", "x", NOW)
    assert usersite.ELSEWHERE not in erik.page()


# -- the documents ------------------------------------------------------------------------

def test_the_privacy_page_says_what_the_fingerprint_is_and_is_for():
    page = usersite.privacy_page(Config())
    assert "a fingerprint of that account" in page
    assert "cannot be turned back into the id or your address" in page
    assert "signed in on two machines" in page


def test_the_guide_says_a_slot_keeps_its_account():
    from ccfleetd import customer_docs
    guide = customer_docs.page_for("/docs/guide")(Config())
    assert "and keeps it" in guide and "works with that account only" in guide
