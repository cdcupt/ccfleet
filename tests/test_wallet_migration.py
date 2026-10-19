"""Adding service-credit tables does not reinterpret old payments or rentals."""
import json
import sqlite3

import pytest

from ccfleetd import slots
from ccfleetd.store import Store, StoreError
from tests.test_wallet_store import NOW, accounts, buy, grant, machine

LEGACY = ("accounts", "nodes", "slots", "payments", "cli_devices", "gateway_keys",
          "gateway_provider_bindings", "users")


def legacy_rows(store):
    return {table: sorted([tuple(row) for row in store._conn.execute('SELECT * FROM "' + table + '"')],
                          key=repr) for table in LEGACY}


def test_existing_database_gains_empty_credit_tables_without_converting_any_old_row(tmp_path):
    path = str(tmp_path / "legacy.db")
    original = Store(path)
    accounts(original)
    slot_id = machine(original)
    original.set_slot_quota("alice", 1)
    old_slot = original.claim_slot("alice", now=NOW)
    original.move_slot(slot_id, slots.CLAIMED)
    original.move_slot(slot_id, slots.ACTIVE)
    original.record_payment("alice", amount="500", currency="USD", through="2026-01-01",
                            recorded_by="old admin", note="old manual record", now=NOW)
    before = legacy_rows(original)
    original.close()
    # Simulate the pre-feature schema using only this disposable test database.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE slot_purchases")
        connection.execute("DROP TABLE wallet_entries")
    later = Store(path)
    try:
        assert legacy_rows(later) == before
        assert later.wallet_balance("alice") == 0
        assert later.list_wallet_entries("alice") == later.list_slot_purchases("alice") == []
        assert later.paid_slot_through("alice", slot_id, claim_id=old_slot["gateway_claim_id"]) is None
        assert later.get_slot(slot_id)["state"] == slots.ACTIVE
    finally:
        later.close()


def test_grants_and_new_purchases_do_not_change_existing_manual_payment_or_held_claim(tmp_path):
    store = Store(str(tmp_path / "fleet.db"))
    try:
        accounts(store)
        old_slot_id = machine(store)
        machine(store, "node-b")
        store.set_slot_quota("alice", 1)
        store.claim_slot("alice", node_id="node-a", now=NOW)
        store.record_payment("alice", amount="50", currency="CNY", through="2024-01-01",
            recorded_by="old admin", note="old manual payment", now=NOW)
        old_slot, old_payment = store.get_slot(old_slot_id), store.list_payments("alice")
        grant(store)
        buy(store, node="node-b")
        assert store.get_slot(old_slot_id) == old_slot
        assert store.list_payments("alice") == old_payment
        assert store.get_account("alice")["slot_quota"] == 1
        assert "old manual payment" not in json.dumps(store.list_wallet_entries("alice"))
    finally:
        store.close()


@pytest.mark.parametrize("limit", [0, -1, True, 1.0, 101])
def test_history_reads_have_bounded_integer_limits(tmp_path, limit):
    store = Store(":memory:")
    try:
        accounts(store)
        with pytest.raises(StoreError):
            store.list_wallet_entries("alice", limit=limit)
        with pytest.raises(StoreError):
            store.list_slot_purchases("alice", limit=limit)
    finally:
        store.close()
