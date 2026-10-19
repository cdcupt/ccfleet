"""Paid history freezes resource IDs while names and grandfathered claims stay usable."""
from __future__ import annotations

import pytest

from ccfleetd import slots, wallet_access
from ccfleetd.store import NotYours, StoreError
from tests.test_gateway_end_to_end import public_key
from tests.test_wallet_store import NOW, buy, grant, machine, snapshot
from tests.test_wallet_store import store as store  # noqa: F401 -- imported fixture


def ready_paid(store):
    slot_id = machine(store)
    grant(store)
    purchase = buy(store)
    store.move_slot(slot_id, slots.CLAIMED)
    store.move_slot(slot_id, slots.ACTIVE)
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": True}], now=NOW)
    store.set_node_access("node-a", "192.0.2.10", 2222, public_key())
    key = store.create_gateway_key("alice", "Synthetic connection", [slot_id], now=NOW)
    return slot_id, purchase, key


def entitlement(store, slot_id, owner, now):
    slot = store.get_slot(slot_id)
    try:
        through = store.paid_slot_through(owner, slot_id, claim_id=slot["gateway_claim_id"] or None)
    except NotYours:
        through = "not held"
    return through, wallet_access.allowed(store, slot, now=now)


@pytest.mark.parametrize("operation", ["rename_node", "rename_slot", "remove_node", "remove_slot"])
@pytest.mark.parametrize("state", ["active", "free", "new-legacy-claim"])
def test_paid_resource_rename_and_delete_refuse_atomically_in_every_lifecycle_state(
        store, operation, state):
    slot_id, purchase, key = ready_paid(store)
    owner = "alice"
    if state != "active":
        store.begin_release(slot_id, held_by="alice")
        store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}],
                                now=NOW + 1)
        assert store.get_slot(slot_id)["state"] == slots.FREE
        if state == "new-legacy-claim":
            owner = "bob"
            store.set_slot_quota(owner, 1)
            current = store.claim_slot(owner, node_id="node-a", now=NOW + 2)
            assert current["gateway_claim_id"] != purchase["claim_id"]
            assert store.paid_slot_through(owner, slot_id) is None
    before = snapshot(store)
    access = entitlement(store, slot_id, owner, NOW + 3)
    targets = store.gateway_targets(key)
    args = (("node-a", "renamed-node") if operation == "rename_node" else
            (slot_id, "renamed-slot") if operation == "rename_slot" else
            ("node-a",) if operation == "remove_node" else (slot_id,))
    with pytest.raises(StoreError, match="Paid history.*Disable or retire"):
        getattr(store, operation)(*args)
    assert snapshot(store) == before
    assert entitlement(store, slot_id, owner, NOW + 3) == access
    assert store.gateway_targets(key) == targets
    assert store.get_slot_purchase("alice", purchase["id"]) == purchase
    if state == "active":
        assert access == (purchase["period_end_at"], True)
        assert not wallet_access.allowed(store, store.get_slot(slot_id), now=purchase["period_end_at"])


def test_paid_slot_display_name_changes_without_rewriting_history_or_access(store):
    slot_id, purchase, key = ready_paid(store)
    before_slot = store.get_slot(slot_id)
    before_entries = store.list_wallet_entries("alice")
    before_keys = store.list_gateway_keys("alice")
    before_targets = store.gateway_targets(key)
    assert store.name_slot(slot_id, "my-work-slot", held_by="alice") == "my-work-slot"
    assert store.get_slot(slot_id) == {**before_slot, "name": "my-work-slot"}
    assert store.list_wallet_entries("alice") == before_entries
    assert store.list_gateway_keys("alice") == before_keys
    assert store.gateway_targets(key) == before_targets
    assert store.get_slot_purchase("alice", purchase["id"]) == purchase
    assert entitlement(store, slot_id, "alice", NOW) == (purchase["period_end_at"], True)


def test_credit_grant_without_purchase_preserves_legacy_raw_ID_renames(store):
    slot_id = machine(store)
    grant(store)
    store.set_slot_quota("alice", 1)
    current = store.claim_slot("alice", now=NOW)
    store.move_slot(slot_id, slots.CLAIMED)
    store.move_slot(slot_id, slots.ACTIVE)
    before_entries = store.list_wallet_entries("alice")
    before_slot = store.get_slot(slot_id)
    store.rename_node("node-a", "renamed-node")
    store.rename_slot(slot_id, "renamed-slot")
    renamed = store.get_slot("renamed-slot")
    assert renamed["node_id"] == "renamed-node" and renamed["held_by"] == "alice"
    assert renamed["gateway_claim_id"] == current["gateway_claim_id"]
    assert renamed["gateway_generation"] == before_slot["gateway_generation"]
    assert store.list_wallet_entries("alice") == before_entries
    assert store.list_slot_purchases("alice") == []
    assert entitlement(store, "renamed-slot", "alice", NOW) == (None, True)
