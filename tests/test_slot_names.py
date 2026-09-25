"""A slot's name is its holder's to choose, and never made from their address.

The name is the machine's hostname: claude.ai shows it, so Anthropic receives
it (Erik, 2026-09-24). A claim gives a neutral "slot-4821"; the holder renames
it on their page, and nothing of theirs is in it unless they put it there.
"""
from __future__ import annotations

import re
import time
import urllib.parse

import pytest

from ccfleetd import names, slots
from ccfleetd.desired import machine_hostname
from ccfleetd.store import BadName, NameTaken, NotYours, StoreError

from .test_usersite import claimed, machine, report, site  # noqa: F401 (the fixture)

NOW = 1_790_000_000.0


# -- the names themselves ----------------------------------------------------------------

@pytest.mark.parametrize("name", ["ab", "alice", "my-box", "a1", "x" * 30, "pool", "slot",
                                  "pool-a", "slot-x1", "pool-3a", "0-0"])
def test_a_nickname_may_be_any_short_hostname(name):
    assert names.valid_nickname(name)


@pytest.mark.parametrize("name", ["a", "x" * 31, "-ab", "ab-", "Ab", "a b", "a_b", "a.b",
                                  "ab\n", "", "pool-3", "slot-4821", "slot-1", "pool-12",
                                  None, 7])
def test_a_nickname_is_refused_when_it_is_not_one(name):
    assert not names.valid_nickname(name)


def test_a_neutral_name_is_four_random_digits():
    draws = iter([4821])
    assert names.neutral_name(set(), pick=lambda bound: next(draws)) == "slot-4821"
    assert names.neutral_name(set(), pick=lambda bound: 7) == "slot-0007"


def test_a_neutral_name_skips_names_in_use():
    draws = iter([4821, 4821, 12])
    assert names.neutral_name({"slot-4821"}, pick=lambda bound: next(draws)) == "slot-0012"


def test_crowded_neutral_names_take_another_digit():
    taken = {f"slot-{n:04d}" for n in range(10_000)}
    bounds = []

    def pick(bound):
        bounds.append(bound)
        return 0
    assert names.neutral_name(taken, pick=pick) == "slot-00000"
    assert bounds == [10_000] * names.NEUTRAL_TRIES + [100_000]


# -- the store --------------------------------------------------------------------------

def shared(store, node="pool-1"):
    store.add_node(node, "op", now=NOW)
    store.add_slot(node, node, "slot01", now=NOW)
    store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}], now=NOW)


def in_use(store, node="pool-1", account="a1"):
    """A slot claimed and set up, by somebody whose address is plainly theirs."""
    shared(store, node)
    store.add_account(account, f"sub-{account}", f"{account}.person@example.com",
                      slot_quota=1, now=NOW)
    slot = store.claim_slot(account, now=NOW, node_id=node)
    store.apply_slot_report(node, [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=NOW)
    assert store.get_slot(slot["id"])["state"] == slots.CLAIMED
    return store.get_slot(slot["id"])


def test_a_claim_says_nothing_of_the_holders_address(store):
    slot = in_use(store)
    assert re.fullmatch(r"slot-[0-9]{4}", slot["name"])
    rows = store.list_slots(node_id="pool-1", kind=slots.MACHINE_SLOT)
    assert machine_hostname("pool-1", rows) == slot["name"], "the hostname Anthropic sees"


def test_the_holder_renames_their_slot_and_the_machine_follows(store):
    slot = in_use(store)
    assert store.name_slot(slot["id"], "my-box", held_by="a1") == "my-box"
    assert store.get_slot(slot["id"])["name"] == "my-box"
    rows = store.list_slots(node_id="pool-1", kind=slots.MACHINE_SLOT)
    assert machine_hostname("pool-1", rows) == "my-box"


def test_a_slot_keeps_its_own_name_when_given_it_again(store):
    slot = in_use(store)
    store.name_slot(slot["id"], "my-box", held_by="a1")
    assert store.name_slot(slot["id"], "my-box", held_by="a1") == "my-box"


@pytest.mark.parametrize("name", ["my-box", "Bad Name", "pool-9"])
def test_somebody_elses_slot_is_not_theirs_to_name_whatever_they_send(store, name):
    """Who holds it is checked before the name, so a bad name cannot tell a
    stranger that the slot exists."""
    slot = in_use(store)
    store.add_account("b1", "sub-b1", "b@example.com", slot_quota=1, now=NOW)
    with pytest.raises(NotYours):
        store.name_slot(slot["id"], name, held_by="b1")
    assert store.get_slot(slot["id"])["name"] == slot["name"]


@pytest.mark.parametrize("name", ["Ab", "pool-9", "slot-1234", "a", "x" * 31])
def test_a_name_that_is_not_a_nickname_is_refused(store, name):
    slot = in_use(store)
    with pytest.raises(BadName):
        store.name_slot(slot["id"], name, held_by="a1")
    assert store.get_slot(slot["id"])["name"] == slot["name"]


def test_a_name_something_else_answers_to_is_taken(store):
    slot = in_use(store)
    in_use(store, node="pool-2", account="b1")
    store.name_slot("pool-2", "bea", held_by="b1")
    store.add_node("erik-box", "op", now=NOW)
    for taken in ("bea", "erik-box", "pool-2"):
        if names.valid_nickname(taken):
            with pytest.raises(NameTaken):
                store.name_slot(slot["id"], taken, held_by="a1")
    store.add_slot("spare", "erik-box", "slot09", now=NOW)
    with pytest.raises(NameTaken):
        store.name_slot(slot["id"], "spare", held_by="a1")


def test_no_name_is_a_fresh_neutral_one(store):
    slot = in_use(store)
    store.name_slot(slot["id"], "my-box", held_by="a1")
    fresh = store.name_slot(slot["id"], None)
    assert re.fullmatch(r"slot-[0-9]{4}", fresh) and store.get_slot(slot["id"])["name"] == fresh


def test_only_a_machines_slot_in_use_is_named(store):
    shared(store)
    store.add_account("a1", "sub-a1", "a@example.com", slot_quota=2, now=NOW)
    slot = store.claim_slot("a1", now=NOW, node_id="pool-1")
    assert store.get_slot(slot["id"])["state"] == slots.CLAIMING
    with pytest.raises(StoreError):
        store.name_slot(slot["id"], "my-box", held_by="a1")
    store.add_node("erik-1", "erik", now=NOW)
    store.hold_owner_node("erik-1", "a1", now=NOW)
    with pytest.raises(StoreError):
        store.name_slot("erik-1", "my-box", held_by="a1")


# -- the holder's page ------------------------------------------------------------------

def signed_in_with_a_slot(site):  # noqa: F811
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    erik = sign_in(quota=1, handle=None)
    slot = claimed(store, erik)
    return store, erik, store.get_slot(slot["id"])


def test_the_page_offers_a_rename_under_the_name_it_has(site):  # noqa: F811
    store, erik, slot = signed_in_with_a_slot(site)
    page = erik.page()
    assert f'action="/account/slots/{slot["id"]}/rename"' in page
    assert f'placeholder="{slot["name"]}"' in page
    assert "pick anything but your email address" in page


def test_renaming_from_the_page(site):  # noqa: F811
    store, erik, slot = signed_in_with_a_slot(site)
    reply = erik.press(f"/account/slots/{slot['id']}/rename", name=" My-Box ")
    assert reply.status == 303 and "note=renamed" in reply.getheader("Location")
    assert store.get_slot(slot["id"])["name"] == "my-box"


@pytest.mark.parametrize("name,note", [("pool-3", "name-bad"), ("x", "name-bad"),
                                       ("", "name-bad"), ("m1", "name-taken")])
def test_a_rename_that_cannot_be_says_why(site, name, note):  # noqa: F811
    store, erik, slot = signed_in_with_a_slot(site)
    reply = erik.press(f"/account/slots/{slot['id']}/rename", name=name)
    assert reply.status == 303
    assert f"note={note}" in urllib.parse.unquote(reply.getheader("Location"))
    assert store.get_slot(slot["id"])["name"] == slot["name"]


def test_no_rename_before_the_slot_is_set_up_or_on_ones_own_node(site):  # noqa: F811
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    erik = sign_in(quota=2, handle=None)
    assert erik.press("/account/claim").status == 303
    assert "/rename" not in erik.page(), "still setting up"
    store.add_node("erik-1", "erik", now=time.time())
    store.hold_owner_node("erik-1", erik.account["id"], now=time.time())
    report(store, "m1", [])
    page = erik.page()
    assert "/account/slots/erik-1/rename" not in page
