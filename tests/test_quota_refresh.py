"""A Refresh for the usage windows, from the holder's page and the console
(Erik, 2026-09-24), and the exact reset moments Claude Code saves."""
from __future__ import annotations

import time
import urllib.parse

import pytest

from ccfleetd import desired, heartbeat, render, slots
from ccfleetd.store import NotYours, StoreError

from .test_console_slots import console, held_slot, machine_said  # noqa: F401 (fixture)
from .test_usersite import claimed, machine, report, site  # noqa: F401 (the fixture)

NOW = 1_790_000_000.0


def active(store, node="m1", holder="a1"):
    store.add_node(node, "op", now=NOW)
    store.add_slot(node, node, "slot01", now=NOW)
    store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}], now=NOW)
    store.add_account(holder, f"sub-{holder}", f"{holder}@example.com", slot_quota=1, now=NOW)
    slot = store.claim_slot(holder, now=NOW, node_id=node)
    store.apply_slot_report(node, [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=NOW)
    store.apply_slot_report(node, [{"unix_user": "slot01", "present": True,
                                    "credentials": {"logged_in": True}}], now=NOW)
    assert store.get_slot(slot["id"])["state"] == slots.ACTIVE
    return slot["id"]


# -- the store --------------------------------------------------------------------------

def test_the_holder_asks_and_it_is_kept(store):
    sid = active(store)
    assert store.request_quota_read(sid, NOW, held_by="a1") is True
    assert store.get_slot(sid)["quota_wanted_at"] == NOW


def test_a_second_press_within_the_minute_asks_nothing_more(store):
    sid = active(store)
    store.request_quota_read(sid, NOW, held_by="a1")
    assert store.request_quota_read(sid, NOW + 59, held_by="a1") is False
    assert store.get_slot(sid)["quota_wanted_at"] == NOW
    assert store.request_quota_read(sid, NOW + 60, held_by="a1") is True
    assert store.get_slot(sid)["quota_wanted_at"] == NOW + 60


def test_somebody_elses_slot_is_not_theirs_to_ask(store):
    sid = active(store)
    store.add_account("b1", "sub-b1", "b@example.com", slot_quota=1, now=NOW)
    with pytest.raises(NotYours):
        store.request_quota_read(sid, NOW, held_by="b1")
    assert store.get_slot(sid)["quota_wanted_at"] is None


def test_only_a_machines_slot_in_use_is_asked(store):
    store.add_node("m1", "op", now=NOW)
    store.add_slot("m1", "m1", "slot01", now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW)
    store.add_account("a1", "sub-a1", "a@example.com", slot_quota=2, now=NOW)
    slot = store.claim_slot("a1", now=NOW, node_id="m1")
    with pytest.raises(StoreError):                  # still setting up
        store.request_quota_read(slot["id"], NOW, held_by="a1")
    store.add_node("erik-1", "erik", now=NOW)
    store.hold_owner_node("erik-1", "a1", now=NOW)
    with pytest.raises(StoreError):                  # somebody's own node
        store.request_quota_read("erik-1", NOW, held_by="a1")


def test_the_request_goes_with_the_slot_when_it_is_freed(store):
    sid = active(store)
    store.request_quota_read(sid, NOW, held_by="a1")
    store.begin_release(sid)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW + 5)
    assert store.get_slot(sid)["state"] == slots.FREE
    assert store.get_slot(sid)["quota_wanted_at"] is None


# -- what the machine is told ---------------------------------------------------------------

def test_the_machine_is_told_of_a_request_for_a_slot_in_use(store):
    sid = active(store)
    store.request_quota_read(sid, NOW, held_by="a1")
    block = desired._slot_block(store.get_slot(sid))
    assert block["quota_wanted_at"] == NOW


@pytest.mark.parametrize("state", [slots.FREE, slots.CLAIMING, slots.RELEASING])
def test_nor_of_one_for_a_slot_nobody_uses(state):
    block = desired._slot_block({"unix_user": "slot01", "state": state,
                                 "quota_wanted_at": NOW})
    assert "quota_wanted_at" not in block


def test_a_request_that_is_no_moment_is_not_passed_on():
    block = desired._slot_block({"unix_user": "slot01", "state": slots.ACTIVE,
                                 "quota_wanted_at": True})
    assert "quota_wanted_at" not in block


# -- what a node may say ------------------------------------------------------------------

@pytest.mark.parametrize("value,kept", [(NOW + 3600, True), (0, False), (-5, False),
                                        (1e12, False), (True, False), ("soon", False)])
def test_a_reset_moment_is_kept_only_when_it_is_one(value, kept):
    got = heartbeat._quota({"session": {"used_pct": 4, "resets": "3pm (UTC)",
                                        "resets_at": value}})
    assert ("resets_at" in got["session"]) is kept
    assert got["session"]["resets"] == "3pm (UTC)"


# -- the pages --------------------------------------------------------------------------

def test_an_exact_moment_is_said_in_the_viewers_zone_without_the_words():
    foot = render._reset_foot("gibberish", NOW - 60, NOW, NOW + 3 * 3600 + 37 * 60)
    assert 'data-local>in 3h 37m</time>' in foot and "gibberish" not in foot


def test_without_one_the_words_are_placed_as_before():
    assert render._reset_foot("gibberish", NOW - 60, NOW, None) == "resets gibberish"
    assert render._reset_foot("", NOW - 60, NOW, True) == ""


def test_an_exact_moment_needs_a_now_to_count_down_from():
    assert render._reset_foot("3pm (UTC)", None, None, NOW) == "resets 3pm (UTC)"


def test_no_refresh_without_a_form_token_or_a_single_slot():
    row = {"slot_id": "m1", "quota_wanted_at": None}
    assert "Refresh" in render._quota_refresh(row, NOW - 60, NOW, "tok")
    assert render._quota_refresh(row, NOW - 60, NOW, "") == "", "an owner login"
    assert render._quota_refresh({"slot_id": None}, NOW - 60, NOW, "tok") == ""


def test_somebodys_own_node_offers_no_refresh(site):  # noqa: F811
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    store.add_node("erik-1", "erik", now=time.time())
    store.hold_owner_node("erik-1", erik.account["id"], now=time.time())
    store.insert_heartbeat("erik-1", time.time(), {
        "node_id": "erik-1", "credentials": {"present": True, "logged_in": True},
        "quota": {"week": {"used_pct": 30}, "checked_at": time.time() - 90}})
    page = erik.page()
    assert "30%" in page and "/account/slots/erik-1/quota" not in page


@pytest.mark.parametrize("wanted,read,reading", [
    (NOW - 10, NOW - 400, True), (NOW - 10, NOW - 5, False), (NOW - 10, NOW - 10, False),
    (NOW - 301, NOW - 400, False), (None, NOW - 400, False), (True, NOW - 400, False),
    (NOW - 10, None, True)])
def test_a_read_is_under_way_until_answered_or_five_minutes_on(wanted, read, reading):
    assert render.quota_reading(wanted, read, NOW) is reading


def test_the_holders_page_offers_a_refresh_and_then_says_it_is_reading(site):  # noqa: F811
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": True},
                          "quota": {"week": {"used_pct": 30}, "checked_at": time.time() - 90}}])
    page = erik.page()
    assert f'action="/account/slots/{slot["id"]}/quota"' in page
    assert "read 1m ago · every five minutes" in page
    assert 'content="60;' in page
    reply = erik.press(f"/account/slots/{slot['id']}/quota")
    assert "note=reading" in urllib.parse.unquote(reply.getheader("Location"))
    page = erik.page()
    assert "reading them again now" in page and f'/{slot["id"]}/quota"' not in page
    assert 'content="4;' in page, "the page comes back soon for the new numbers"


def test_the_console_offers_a_refresh_for_a_slot_in_use(console):  # noqa: F811
    store, call = console
    slot = held_slot(store)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "credentials": {"logged_in": True}}], now=time.time())
    machine_said(store, {"unix_user": "slot01", "present": True,
                         "quota": {"week": {"used_pct": 30}, "checked_at": time.time() - 90}})
    page = call("GET", "/admin").body
    usage = page[page.index('id="usage"'):]
    assert f'action="/actions/slot/{slot["id"]}/quota"' in usage
    reply = call("POST", f"/actions/slot/{slot['id']}/quota", {})
    assert reply.status == 303 and reply.getheader("Location").endswith("#usage")
    assert store.get_slot(slot["id"])["quota_wanted_at"] is not None
    usage = call("GET", "/admin").body
    assert "reading now" in usage[usage.index('id="usage"'):]
