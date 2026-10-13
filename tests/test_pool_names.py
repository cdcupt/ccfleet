"""Empty pool names and anonymous status labels never replace auth identity."""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ccfleetd import names, slots
from ccfleetd.desired import machine_hostname
from ccfleetd.store import PUBLIC_STATUS_SEQUENCE_KEY, SCHEMA, Store, StoreError, hash_token
from tests.test_gateway_access import public_key


def machine(store, node_id, *, slot_id=None, empty=True):
    token = store.add_node(node_id, "operator", now=1.0)
    store.set_node_access(node_id, "192.0.2.1", 22, public_key())
    store.add_slot(slot_id or node_id, node_id, "slot01", now=1.0)
    if empty:
        store.apply_slot_report(node_id, [{"unix_user": "slot01", "present": False}], now=2.0)
    return token


def active(store, node_id="erik-2"):
    if store.get_account("a1") is None:
        store.add_account("a1", "sub-1", "private@example.com", slot_quota=4, now=1.0)
    token = machine(store, node_id)
    store.claim_slot("a1", node_id=node_id, now=3.0)
    store.move_slot(node_id, slots.CLAIMED)
    store.move_slot(node_id, slots.ACTIVE)
    store.name_slot(node_id, "private-box" if node_id == "erik-2" else f"private-{node_id}")
    return token


def legacy_alias(store, slot_id):
    """A migrated slot has no alias until its next eligible empty state."""
    store._conn.execute("UPDATE slots SET pool_name = NULL WHERE id = ?", (slot_id,))
    store._conn.commit()


def test_completed_wipe_changes_display_and_hostname_without_changing_access_identity(store):
    token = active(store)
    legacy_alias(store, "erik-2")
    node = store.get_node("erik-2")
    store.insert_heartbeat("erik-2", 4.0, {"node_id": "erik-2", "hostname": "private-box"})
    pairing = store.request_cli_pairing("erik-2", "a1", now=4.0)
    device = store.register_cli_device(pairing, public_key(8), "laptop", now=4.1)
    endpoint = store.create_gateway_key("a1", "gateway", ["erik-2"], now=4.0)
    key = store.resolve_gateway_key(endpoint["key_token"], now=4.1)
    assert store.gateway_targets(key)
    store.begin_release("erik-2", held_by="a1", named="private-box")
    assert store.resolve_cli_device(device["device_token"], now=5.0) is None
    assert store.gateway_targets(key) == []
    for present in (True, None):
        store.apply_slot_report("erik-2", [{"unix_user": "slot01", "present": present}], now=5.0)
        assert names.display(store.get_slot("erik-2")) == "private-box"
        assert store.get_slot("erik-2")["pool_name"] is None
        assert store.normalize_free_pool_names() == []
    assert store.apply_slot_report("erik-2", [{"unix_user": "slot01", "present": False}], now=6.0)
    released = store.get_slot("erik-2")
    assert released["id"] == released["node_id"] == "erik-2"
    assert released["pool_name"] == names.display(released) == "pool-1"
    assert released["name"] is None and released["held_by"] is None
    assert machine_hostname("erik-2", [released]) == "pool-1"
    assert store.node_for_token(token) == node
    assert store.recent_heartbeats("erik-2")[0]["payload"]["node_id"] == "erik-2"
    store.apply_slot_report("erik-2", [{"unix_user": "slot01", "present": False}], now=7.0)
    assert store.get_slot("erik-2")["pool_name"] == "pool-1"
    reclaimed = store.claim_slot("a1", node_id="erik-2", now=8.0)
    assert reclaimed["pool_name"] == "pool-1" and reclaimed["name"].startswith("slot-")
    assert store.gateway_targets(key) == []


def test_pool_alias_survives_claim_reservation_and_repeat_release(store):
    active(store)
    store.reserve_machine("erik-2", "a1")
    store.begin_release("erik-2")
    store.finish_release("erik-2", now=4.0)
    store.apply_slot_report("erik-2", [{"unix_user": "slot01", "present": False}], now=4.0)
    store.claim_slot("a1", node_id="erik-2", now=5.0)
    store.move_slot("erik-2", slots.CLAIMED)
    store.name_slot("erik-2", "another-name")
    store.begin_release("erik-2")
    store.finish_release("erik-2", now=6.0)
    assert store.get_slot("erik-2")["pool_name"] == "pool-1"
    assert store.get_node("erik-2")["reserved_for"] == "a1"


def test_pool_allocator_skips_internal_ids_held_names_and_persistent_aliases(store):
    store.add_node("pool-1", "operator")
    machine(store, "spare", slot_id="pool-2", empty=False)
    active(store, "other")
    store._conn.execute("UPDATE slots SET name = 'pool-4' WHERE id = 'other'")
    store._conn.commit()
    active(store)
    legacy_alias(store, "erik-2")
    store.begin_release("erik-2")
    store.finish_release("erik-2", now=4.0)
    assert store.get_slot("other")["pool_name"] == "pool-3"
    assert store.get_slot("erik-2")["pool_name"] == "pool-5"


def test_existing_canonical_pool_id_keeps_its_label(store):
    machine(store, "pool-7")
    assert store.get_slot("pool-7")["pool_name"] == "pool-7"


def test_allocated_alias_blocks_new_ids_and_other_account_handles(store):
    active(store)
    machine(store, "another", empty=False)
    for change in (
        lambda: store.add_node("pool-1", "operator"),
        lambda: store.rename_node("another", "pool-1"),
        lambda: store.add_slot("pool-1", "another", "slot02", now=2.0),
        lambda: store.rename_slot("another", "pool-1"),
    ):
        with pytest.raises(StoreError, match="answers"):
            change()
    store.add_account("a2", "sub-2", "other@example.com", slot_quota=1, now=1.0)
    store.set_account_handle("a2", "pool")
    store.apply_slot_report("another", [{"unix_user": "slot01", "present": False}], now=3.0)
    assert store.claim_slot("a2", node_id="another", now=4.0)["name"] == "pool-3"


def test_legacy_normalization_only_touches_confirmed_empty_hosted_slots(store):
    machine(store, "empty")
    legacy_alias(store, "empty")
    machine(store, "unknown", empty=False)
    machine(store, "present", empty=False)
    store.apply_slot_report("present", [{"unix_user": "slot01", "present": True}], now=3.0)
    active(store, "held")
    legacy_alias(store, "held")
    active(store, "releasing")
    legacy_alias(store, "releasing")
    store.begin_release("releasing")
    store.add_node("owner", "operator")
    store.hold_owner_node("owner", "a1", now=3.0)
    before = {row["id"]: row for row in store.list_slots() if row["id"] != "empty"}
    assert store.normalize_free_pool_names() == [{"slot_id": "empty", "pool_name": "pool-1"}]
    assert store.normalize_free_pool_names() == []
    assert {row["id"]: row for row in store.list_slots() if row["id"] != "empty"} == before


def test_two_stores_finishing_wipes_allocate_distinct_aliases(tmp_path):
    path = str(tmp_path / "fleet.db")
    first, second = Store(path), Store(path)
    try:
        tokens = {}
        for node_id in ("erik-1", "erik-2"):
            tokens[node_id] = active(first, node_id)
            legacy_alias(first, node_id)
            first.begin_release(node_id)
        barrier = threading.Barrier(2)

        def finish(store, slot_id):
            barrier.wait(timeout=5)
            return store.finish_release(slot_id, now=10.0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(finish, first, "erik-1"), pool.submit(finish, second, "erik-2")]
            assert all(job.result(timeout=10) for job in jobs)
        assert {row["pool_name"] for row in first.list_slots()} == {"pool-1", "pool-2"}
        for node_id, token in tokens.items():
            assert first.get_slot(node_id)["node_id"] == node_id
            assert first.node_for_token(token)["id"] == node_id
    finally:
        first.close()
        second.close()


def test_failed_free_write_rolls_back_the_holder_and_alias(store):
    active(store)
    legacy_alias(store, "erik-2")
    store.begin_release("erik-2")
    before = store.get_slot("erik-2")
    store._conn.execute("CREATE TRIGGER refuse_pool BEFORE UPDATE OF pool_name ON slots "
                        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    store._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        store.finish_release("erik-2", now=4.0)
    assert store.get_slot("erik-2") == before


def test_public_numbers_migrate_by_creation_and_survive_reopen_rename_delete(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    old_schema = SCHEMA.replace("    public_status_number INTEGER CHECK (public_status_number > 0),\n", "")
    old_schema = old_schema.replace("    pool_name TEXT,\n", "")
    conn.executescript(old_schema)
    for node_id, created in (("older-personal", 1.0), ("newer-personal", 2.0)):
        conn.execute("INSERT INTO nodes (id, owner, token_hash, created_at) VALUES (?,?,?,?)",
                     (node_id, "operator", hash_token(node_id), created))
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.get_node("older-personal")["public_status_number"] == 1
    assert store.get_node("newer-personal")["public_status_number"] == 2
    store.rename_node("older-personal", "renamed-personal")
    assert store.get_node("renamed-personal")["public_status_number"] == 1
    store.remove_node("newer-personal")
    store.close()
    store = Store(path)
    try:
        store.add_node("newer-personal", "operator")
        assert store.get_node("newer-personal")["public_status_number"] == 3
        assert store._conn.execute("SELECT value FROM settings WHERE key = ?",
                                   (PUBLIC_STATUS_SEQUENCE_KEY,)).fetchone()[0] == "3"
    finally:
        store.close()


def test_public_numbers_are_unique_across_concurrent_stores(tmp_path):
    path = str(tmp_path / "fleet.db")
    first, second = Store(path), Store(path)
    try:
        barrier = threading.Barrier(2)

        def add(store, node_id):
            barrier.wait(timeout=5)
            store.add_node(node_id, "operator")

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(add, first, "alice"), pool.submit(add, second, "bob")]
            for job in jobs:
                job.result(timeout=10)
        assert {row["public_status_number"] for row in first.list_nodes()} == {1, 2}
    finally:
        first.close()
        second.close()


def test_corrupt_public_sequence_fails_without_assigning_a_reused_number(store):
    store._conn.execute("UPDATE settings SET value = 'broken' WHERE key = ?",
                        (PUBLIC_STATUS_SEQUENCE_KEY,))
    store._conn.commit()
    with pytest.raises(StoreError, match="sequence"):
        store.add_node("new", "operator")
    assert store.get_node("new") is None


def test_reregistered_node_starts_empty_history_without_changing_group_or_site(store):
    store.add_node("personal-id", "operator")
    old_number = store.get_node("personal-id")["public_status_number"]
    for component, state in (("node:personal-id", "red"), ("machines", "green"), ("site", "yellow")):
        store.count_status(component, state, 2_000_000_000.0, grace=2)
    shared_history = [row for row in store.status_minutes(since_day="1970-01-01")
                      if row["component"] != "node:personal-id"]
    store.remove_node("personal-id")
    store.add_node("personal-id", "operator")
    assert store.get_node("personal-id")["public_status_number"] > old_number
    assert store.status_minutes(since_day="1970-01-01") == shared_history


def test_duplicate_node_insert_preserves_its_history_and_shared_counts(store):
    store.add_node("personal-id", "operator")
    for component in ("node:personal-id", "machines", "site"):
        store.count_status(component, "red", 2_000_000_000.0, grace=2)
    before = store.status_minutes(since_day="1970-01-01")
    node = store.get_node("personal-id")
    with pytest.raises(StoreError, match="already exists"):
        store.add_node("personal-id", "operator")
    assert store.status_minutes(since_day="1970-01-01") == before
    assert store.get_node("personal-id") == node


def test_failed_node_transaction_restores_deleted_orphan_history_and_sequence(store):
    for component in ("node:personal-id", "machines", "site"):
        store.count_status(component, "red", 2_000_000_000.0, grace=2)
    before = store.status_minutes(since_day="1970-01-01")
    store._conn.execute("CREATE TRIGGER refuse_sequence BEFORE UPDATE ON settings "
                        "WHEN NEW.key = 'public_status_number_high_water' "
                        "BEGIN SELECT RAISE(ABORT, 'synthetic sequence failure'); END")
    store._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic sequence failure"):
        store.add_node("personal-id", "operator")
    assert store.get_node("personal-id") is None
    assert store.status_minutes(since_day="1970-01-01") == before
    assert store._conn.execute("SELECT value FROM settings WHERE key = ?",
                               (PUBLIC_STATUS_SEQUENCE_KEY,)).fetchone()[0] == "0"


def test_free_display_uses_only_a_valid_pool_alias():
    assert names.display({"id": "old", "state": "free", "name": "private", "pool_name": "pool-3"}) == "pool-3"
    assert names.display({"id": "old", "state": "free", "pool_name": "private-name"}) == "old"
    assert names.display({"id": "old", "state": "active", "name": None, "pool_name": "pool-3"}) == "old"
