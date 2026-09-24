"""A Claude plan, said the way Anthropic sells it (Erik, 2026-09-24).

Claude Code's own words for a plan are "max" and "default_claude_max_20x";
the holder and the operator read "Max 20x" on every page that names it.
"""
from __future__ import annotations

import time

import pytest

from ccfleetd import plans, slots
from ccfleetd.heartbeat import validate_heartbeat

from .test_console_slots import (
    console,  # noqa: F401  (the fixture, by name)
    held_slot,
    holder,
    machine_said,
    own_machine,
    row_of,
    slots_card,
)
from .test_usersite import claimed, machine, report, site  # noqa: F401 (a fixture)


@pytest.mark.parametrize("sub,tier,said", [
    ("max", "default_claude_max_20x", "Max 20x"),
    ("max", "default_claude_max_5x", "Max 5x"),
    ("MAX", "default_claude_max_5x", "Max 5x"),
    ("", "default_claude_max_20x", "Max 20x"),
    (None, "default_claude_max_20x", "Max 20x"),
    ("max", None, "Max"),
    ("max", "default_claude_ai", "Max"),
    ("pro", "default_claude_ai", "Pro"),
    ("pro", "default_claude_max_20x", "Pro"),        # the sign-in's own word for its plan
    ("PRO", None, "Pro"),
    ("team", None, "Team"),
    ("enterprise", None, "Enterprise"),
    ("free", None, "Free"),
    ("claude_plus", None, "claude_plus"),            # one this does not know: as sent
    (None, None, None),
    ("", "", None),
    (" ", None, None),
    (7, 20, None),
])
def test_a_plan_is_said_as_it_is_sold(sub, tier, said):
    assert plans.label(sub, tier) == said


def test_a_slot_keeps_its_plans_tier_through_the_heartbeat():
    """The agent always sent it; the server's list for a slot dropped it."""
    kept = validate_heartbeat({"node_id": "m", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": {"plan": "default_claude_max_20x"}}]}, "m")
    assert kept["slots"][0]["credentials"]["plan"] == "default_claude_max_20x"
    long = validate_heartbeat({"node_id": "m", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": {"plan": "x" * 100}}]}, "m")
    assert long["slots"][0]["credentials"]["plan"] == "x" * 40
    odd = validate_heartbeat({"node_id": "m", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": {"plan": 20}}]}, "m")
    assert odd["slots"][0]["credentials"]["plan"] is None


def signed_in(tier, sub="max"):
    return {"unix_user": "slot01", "present": True,
            "credentials": {"logged_in": True, "subscription_type": sub, "plan": tier}}


def test_the_slots_card_says_each_holders_plan(console):  # noqa: F811
    store, call = console
    held_slot(store)
    machine_said(store, signed_in("default_claude_max_5x"))
    row = row_of(slots_card(call("GET", "/admin").body), "m1")
    assert 'ana@example.com<span class="sub">Max 5x</span>' in row


def test_a_free_slot_says_no_plan(console):  # noqa: F811
    store, call = console
    held_slot(store)
    store.begin_release("m1-01")
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=time.time())
    machine_said(store, signed_in("default_claude_max_5x"))
    row = row_of(slots_card(call("GET", "/admin").body), "m1")
    assert "Max 5x" not in row


def test_an_own_machine_says_its_owners_plan(console):  # noqa: F811
    store, call = console
    own_machine(store)
    store.insert_heartbeat("erik-1", time.time(), {
        "node_id": "erik-1", "credentials": {"present": True, "logged_in": True,
                                             "subscription_type": "max",
                                             "plan": "default_claude_max_20x"}})
    row = row_of(slots_card(call("GET", "/admin").body), "erik-1")
    assert 'cdcupt@gmail.com<span class="sub">Max 20x</span>' in row


def test_the_accounts_card_says_each_persons_plans(console):  # noqa: F811
    store, call = console
    held_slot(store)
    # Another user's report first: each slot's plan is read under its own user.
    machine_said(store, None, slots=[{**signed_in(None, sub="team"), "unix_user": "slot09"},
                                     signed_in("default_claude_max_20x")])
    store.add_node("erik-1", "erik", now=time.time())
    ana = holder(store, quota=2)                  # the same person: one more slot
    store.hold_owner_node("erik-1", ana["id"], now=time.time())
    store.insert_heartbeat("erik-1", time.time(), {
        "node_id": "erik-1", "credentials": {"subscription_type": "pro"}})
    page = call("GET", "/admin").body
    accounts = page[page.index('id="accounts"'):page.index('id="price"')]
    assert "holds 2 (Max 20x, Pro) · " in accounts and "Team" not in accounts


def test_somebody_with_no_plan_says_none(console):  # noqa: F811
    store, call = console
    holder(store, email="bea@example.com")
    page = call("GET", "/admin").body
    accounts = page[page.index('id="accounts"'):page.index('id="price"')]
    assert "holds 0 · " in accounts and "()" not in accounts


def test_a_row_with_no_plan_says_no_plan(console):  # noqa: F811
    store, call = console
    store.add_node("erik-1", "erik", now=time.time())
    store.insert_heartbeat("erik-1", time.time(), {
        "node_id": "erik-1", "credentials": {"present": True, "mtime": time.time() - 600}})
    page = call("GET", "/admin").body
    table = page[page.index("<table"):page.index("</table>")]
    assert ">refreshed 10m ago" in table and "None" not in table


def test_the_fleet_table_says_the_plan_before_the_login(console):  # noqa: F811
    store, call = console
    store.add_node("erik-1", "erik", now=time.time())
    store.insert_heartbeat("erik-1", time.time(), {
        "node_id": "erik-1", "credentials": {"present": True, "subscription_type": "max",
                                             "plan": "default_claude_max_20x",
                                             "mtime": time.time() - 600}})
    page = call("GET", "/admin").body
    table = page[page.index("<table"):page.index("</table>")]
    assert "Max 20x · refreshed 10m ago" in table


def test_the_holders_page_says_their_plan(site):  # noqa: F811
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    report(store, "m1", [{**signed_in("default_claude_max_20x"), "unix_user": slot["unix_user"],
                          "remote_control": {"state": "active"}}])
    assert store.get_slot(slot["id"])["state"] == slots.ACTIVE
    assert " · Max 20x plan" in erik.page()
