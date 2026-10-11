"""API keys select only owned rentals and are revoked throughout a request."""

from __future__ import annotations

import base64
import json

import pytest

from ccfleetd import cli_access, gateway_access, slots
from ccfleetd.store import NotYours, StoreError


def public_key(byte=1):
    kind = b"ssh-ed25519"
    blob = len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


def add_held_slot(store, number, account_id="a1", *, active=True, now=3.0):
    node_id, slot_id = f"m{number}", f"s{number}"
    store.add_node(node_id, "operator", now=1.0)
    store.set_node_access(node_id, f"192.0.2.{number}", 22, public_key(number))
    store.add_slot(slot_id, node_id, "slot01", now=1.0)
    store.apply_slot_report(node_id, [{"unix_user": "slot01", "present": False}], now=2.0)
    slot = store.claim_slot(account_id, node_id=node_id, now=now)
    store.move_slot(slot_id, slots.CLAIMED)
    if active:
        store.move_slot(slot_id, slots.ACTIVE)
    return slot


@pytest.fixture
def owner(store):
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=4, now=1.0)
    store.add_account("a2", "sub-2", "b@example.com", slot_quota=1, now=1.0)
    add_held_slot(store, 1)
    add_held_slot(store, 2)
    add_held_slot(store, 3, "a2")
    return store


def test_gateway_secret_is_once_only_hash_at_rest_and_separate_from_other_tokens(owner):
    made = owner.create_gateway_key("a1", "My laptop", ["s2", "s1"], now=10.0)
    assert gateway_access.valid_token(made["key_token"])
    assert made["mode"] == "sticky" and made["slot_ids"] == ["s2", "s1"]
    stored = json.dumps([dict(row) for row in owner._conn.execute("SELECT * FROM gateway_keys")])
    assert made["key_token"] not in stored
    [listed] = owner.list_gateway_keys("a1")
    assert "token_hash" not in listed and "key_token" not in listed and "claim_bindings" not in listed
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    assert key["last_seen_at"] == 11.0 and "token_hash" not in key
    assert owner.resolve_cli_device(made["key_token"], now=11.0) is None
    code = owner.request_cli_pairing("s1", "a1", now=10.0)
    device = owner.register_cli_device(code, public_key(8), "old laptop", now=10.1)
    for token in (code, device["device_token"], "x" * 64, "ccf_api_" + "x" * 42,
                  made["key_token"] + "\n", None, "ccf_api_" + "界" * 43):
        assert owner.resolve_gateway_key(token, now=11.0) is None


def test_gateway_priority_is_exact_and_forged_descriptor_cannot_broaden_it(owner):
    made = owner.create_gateway_key("a1", "Only second", ["s2"], "priority", now=10.0)
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    [target] = owner.gateway_targets(key)
    assert target["slot_id"] == "s2" and target["access_host"] == "192.0.2.2"
    assert target["ssh_host_key"] == public_key(2)
    assert owner.gateway_targets({**key, "slot_ids": ["s3", "s1"]}) == [target]
    assert owner.gateway_targets({**key, "account_id": "a2"}) == []
    assert owner.gateway_request_is_active(key["id"], key["generation"], "s2", target["claim_generation"])
    assert not owner.gateway_request_is_active(key["id"], key["generation"], "s1", target["claim_generation"])
    own_pool = owner.update_gateway_key(key["id"], "a1", ["s1", "s2"], "sticky")
    assert own_pool["generation"] == key["generation"] + 1
    assert owner.gateway_targets(key) == []
    assert not owner.gateway_request_is_active(key["id"], key["generation"], "s2", target["claim_generation"])
    assert [row["slot_id"] for row in owner.gateway_targets(own_pool)] == ["s1", "s2"]


@pytest.mark.parametrize("selected,mode", [([], "sticky"), (["s1", "s1"], "sticky"),
                                           (["s1"], "random"), (["s1\n"], "sticky"),
                                           ("s1", "sticky"), ([1], "priority")])
def test_selection_validation(owner, selected, mode):
    with pytest.raises(StoreError):
        owner.create_gateway_key("a1", "endpoint", selected, mode, now=10.0)


def test_selection_owner_is_always_enforced_and_does_not_bypass_with_admin_role(owner):
    owner.set_account_role("a1", "admin")
    with pytest.raises(NotYours):
        owner.create_gateway_key("a1", "not mine", ["s3"], now=10.0)
    made = owner.create_gateway_key("a1", "mine", ["s1"], now=10.0)
    with pytest.raises(NotYours):
        owner.update_gateway_key(made["id"], "a2", ["s3"], "sticky")
    with pytest.raises(NotYours):
        owner.update_gateway_key(made["id"], "a1", ["s3"], "sticky")
    assert owner.list_gateway_keys("a2") == []
    assert not owner.revoke_gateway_key(made["id"], "a2", now=11.0)


def test_unready_slot_can_be_selected_but_not_used_and_service_grants_follow_readiness(owner):
    add_held_slot(owner, 4, active=False)
    made = owner.create_gateway_key("a1", "booting", ["s4", "s1"], now=10.0)
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    assert [row["slot_id"] for row in owner.gateway_targets(key)] == ["s1"]
    assert owner.gateway_public_keys("s4", public_key(9)) == []
    owner.move_slot("s4", slots.ACTIVE)
    assert [row["slot_id"] for row in owner.gateway_targets(key)] == ["s4", "s1"]
    assert owner.gateway_public_keys("s4", public_key(9)) == [public_key(9)]
    assert owner.gateway_public_keys("s4", 'command="sh" ' + public_key(9)) == []


def test_key_revocation_closes_requests_and_drops_service_grant(owner):
    made = owner.create_gateway_key("a1", "one", ["s1"], now=10.0)
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    [target] = owner.gateway_targets(key)
    assert owner.gateway_public_keys("s1", public_key(9)) == [public_key(9)]
    assert owner.revoke_gateway_key(key["id"], "a1", now=12.0)
    assert owner.resolve_gateway_key(made["key_token"], now=13.0) is None
    assert not owner.gateway_request_is_active(key["id"], key["generation"], "s1", target["claim_generation"])
    assert owner.gateway_public_keys("s1", public_key(9)) == []
    assert owner.list_gateway_keys("a1") == []


def test_release_and_same_owner_reclaim_never_reuses_old_selection(owner):
    made = owner.create_gateway_key("a1", "old rental", ["s1"], now=10.0)
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    [target] = owner.gateway_targets(key)
    claim_id = owner.get_slot("s1")["gateway_claim_id"]
    owner.begin_release("s1", held_by="a1")
    assert owner.gateway_targets(key) == [] and owner.gateway_public_keys("s1", public_key(9)) == []
    owner.finish_release("s1", now=12.0)
    owner.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=12.0)
    # Even a repeated timestamp is a different rental and cannot revive access.
    owner.claim_slot("a1", node_id="m1", now=target["claimed_at"])
    owner.move_slot("s1", slots.CLAIMED)
    owner.move_slot("s1", slots.ACTIVE)
    assert owner.get_slot("s1")["gateway_claim_id"] != claim_id
    assert owner.gateway_targets(key) == []
    assert not owner.gateway_request_is_active(key["id"], key["generation"], "s1", target["claim_generation"])
    assert owner.gateway_public_keys("s1", public_key(9)) == []
    renewed = owner.update_gateway_key(key["id"], "a1", ["s1"], "sticky")
    assert len(owner.gateway_targets(renewed)) == 1


def test_account_transition_and_node_disable_invalidate_inflight_even_after_reenabled(owner):
    made = owner.create_gateway_key("a1", "one", ["s1"], now=10.0)
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    [target] = owner.gateway_targets(key)
    owner.request_slot_login("s1", "new@example.com", 12.0, "switch", held_by="a1")
    assert owner.gateway_targets(key) == [] and owner.gateway_public_keys("s1", public_key(9)) == []
    owner.clear_slot_login("s1", held_by="a1")
    assert not owner.gateway_request_is_active(key["id"], key["generation"], "s1", target["claim_generation"])
    [next_target] = owner.gateway_targets(key)
    owner.set_enabled("m1", False)
    assert owner.gateway_targets(key) == []
    owner.set_enabled("m1", True)
    assert not owner.gateway_request_is_active(key["id"], key["generation"], "s1", next_target["claim_generation"])
    assert len(owner.gateway_targets(key)) == 1


def test_service_key_never_promotes_an_existing_device_key(owner):
    key = public_key(9)
    made = owner.create_gateway_key("a1", "one", ["s1"], now=10.0)
    pairing = owner.request_cli_pairing("s1", "a1", now=10.0)
    old = owner.register_cli_device(pairing, key, "device", now=10.1)
    assert owner.gateway_public_keys("s1", key) == []
    assert owner.resolve_cli_device(old["device_token"], now=11.0) is not None
    assert cli_access.normalize_public_key(key) == key
    assert owner.resolve_gateway_key(made["key_token"], now=11.0) is not None


@pytest.mark.parametrize("report", [
    {"present": False}, {"credentials": {"logged_in": False}},
    {"credentials": {"present": False}},
    {"credentials": {"bound_fp": "old", "account_fp": "another"}},
])
def test_known_unhealthy_primary_is_excluded_from_targets(owner, report):
    made = owner.create_gateway_key("a1", "two", ["s1", "s2"], now=10.0)
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    owner.insert_heartbeat("m1", 12.0, {"slots": [{"unix_user": "slot01", **report}]})
    assert [row["slot_id"] for row in owner.gateway_targets(key)] == ["s2"]
    assert owner.gateway_public_keys("s1", public_key(9)) == []
    owner.insert_heartbeat("m1", 13.0, {"slots": [{"unix_user": "slot01", "present": True,
                                                "credentials": {"logged_in": True}}]})
    assert [row["slot_id"] for row in owner.gateway_targets(key)] == ["s1", "s2"]


def test_slot_rename_preserves_rental_and_priority_but_invalidates_old_key_snapshots(owner):
    made = owner.create_gateway_key("a1", "two", ["s2", "s1"], now=10.0)
    key = owner.resolve_gateway_key(made["key_token"], now=11.0)
    before = owner.get_slot("s1")
    owner.rename_slot("s1", "renamed-1")
    updated = owner.resolve_gateway_key(made["key_token"], now=12.0)
    assert updated["slot_ids"] == ["s2", "renamed-1"]
    assert updated["generation"] == key["generation"] + 1
    assert owner.get_slot("renamed-1")["gateway_claim_id"] == before["gateway_claim_id"]
    assert owner.gateway_targets(key) == []
    assert [target["slot_id"] for target in owner.gateway_targets(updated)] == ["s2", "renamed-1"]
    assert owner.gateway_public_keys("renamed-1", public_key(9)) == [public_key(9)]


def test_rename_does_not_adopt_an_endpoint_for_a_deleted_target_rental(owner):
    pool = owner.create_gateway_key("a1", "two", ["s1", "s2"], now=10.0)
    stale = owner.create_gateway_key("a1", "second only", ["s2"], now=10.0)
    first_claim = owner.get_slot("s1")["gateway_claim_id"]
    owner.begin_release("s2", held_by="a1")
    owner.finish_release("s2", now=12.0)
    owner.remove_slot("s2")
    owner.rename_slot("s1", "s2")
    updated = owner.resolve_gateway_key(pool["key_token"], now=13.0)
    assert updated["slot_ids"] == ["s2"]
    assert owner.get_slot("s2")["gateway_claim_id"] == first_claim
    assert len(owner.gateway_targets(updated)) == 1
    orphaned = owner.resolve_gateway_key(stale["key_token"], now=13.0)
    assert owner.gateway_targets(orphaned) == []


def test_gateway_selection_migration_failure_rolls_back_the_entire_slot_rename(owner):
    made = owner.create_gateway_key("a1", "one", ["s1"], now=10.0)
    before = owner.resolve_gateway_key(made["key_token"], now=11.0)
    with owner._write_txn() as conn:
        conn.execute("CREATE TRIGGER refuse_gateway BEFORE UPDATE ON gateway_keys "
                     "BEGIN SELECT RAISE(ABORT, 'refused'); END")
    with pytest.raises(Exception, match="refused"):
        owner.rename_slot("s1", "renamed-1")
    assert owner.get_slot("s1") is not None and owner.get_slot("renamed-1") is None
    assert owner.resolve_gateway_key(made["key_token"], now=11.0, touch=False) == before
