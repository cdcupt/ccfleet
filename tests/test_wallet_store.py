"""Synthetic credit grants and purchases; no payment service or real machine."""
from __future__ import annotations

import json
import sqlite3

import pytest

from ccfleetd import offers, slots
from ccfleetd.store import (
    InsufficientWalletCredit,
    NoSlotAvailable,
    Store,
    StoreError,
    WalletQuoteChanged,
    WalletReplayMismatch,
)

NOW = 1_700_000_000.0


def accounts(store):
    for who, role in (("alice", "user"), ("bob", "user"), ("admin", "admin")):
        store.add_account(who, "sub-" + who, who + "@example.invalid", role=role,
                          slot_quota=0, now=NOW)
    store.add_user("console-admin", "SYNTHETIC_HASH_NOT_A_PASSWORD", "admin", now=NOW)
    store.add_user("console-owner", "SYNTHETIC_HASH_NOT_A_PASSWORD", "owner", now=NOW)


@pytest.fixture
def store():
    result = Store(":memory:", max_slots_per_machine=8)
    accounts(result)
    yield result
    result.close()


def machine(store, node="node-a", *, cost="8", currency="USD", at=NOW):
    store.add_node(node, "operator", now=at)
    store.set_node_offer(node, host_brand="Host", ip_brand="Exit", ip_type="isp",
                         hosting=offers.Cost(cost, currency), ip=offers.Cost("0", currency))
    slot_id = "slot-" + node
    store.add_slot(slot_id, node, "slot01", now=at)
    store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}], now=at)
    store.insert_heartbeat(node, at, {"node_id": node, "mode": "machine"})
    return slot_id


def grant(store, account="alice", amount="20", key="grant-1", **changes):
    return store.grant_wallet_credit(account, amount, actor_id="admin", reason="Test credit",
        idempotency_key=key, now=NOW, **changes)


def buy(store, account="alice", node="node-a", key="purchase-1", now=NOW, cents=1000):
    return store.purchase_slot(account, node, expected_cents=cents, idempotency_key=key,
                               now=now, max_age_s=90)


def snapshot(store):
    return list(store._conn.iterdump())


def test_no_initial_credit_and_no_conversion_of_manual_payments(store):
    store.record_payment("alice", amount="500", currency="USD", through="2026-01-01",
                         recorded_by="old operator", note="Manual record", now=NOW)
    assert store.wallet_balance("alice") == 0
    assert store.list_wallet_entries("alice") == store.list_slot_purchases("alice") == []
    assert store.list_payments("alice")[0]["amount_minor"] == 50000


def test_grant_requires_real_current_DB_admin_and_replays_once(store):
    entry = grant(store)
    assert grant(store) == entry
    assert store.wallet_balance("alice") == 2000
    assert len(store.list_wallet_entries("alice")) == 1
    assert store.get_account("alice")["slot_quota"] == 0
    store.set_account_role("admin", "user")
    before = snapshot(store)
    with pytest.raises(StoreError, match="database admin"):
        grant(store)
    assert snapshot(store) == before


@pytest.mark.parametrize("kind,actor", [
    ("account", "alice"), ("account", "missing"), ("account", "admin token"),
    ("console_user", "console-owner"), ("console_user", "missing"), ("unknown", "admin"),
])
def test_role_labels_or_nonadmin_actors_cannot_grant(store, kind, actor):
    before = snapshot(store)
    with pytest.raises(StoreError, match="database admin"):
        store.grant_wallet_credit("alice", "10", actor_kind=kind, actor_id=actor,
            reason="Test", idempotency_key="grant-unauthorized", now=NOW)
    assert snapshot(store) == before


def test_console_admin_is_an_explicit_database_actor(store):
    entry = store.grant_wallet_credit("alice", "10.25", actor_kind="console_user",
        actor_id="console-admin", reason="Manual service credit", idempotency_key="console-1", now=NOW)
    assert entry["actor_kind"] == "console_user" and entry["actor_id"] == "console-admin"
    assert store.wallet_balance("alice") == 1025
    assert "SYNTHETIC_HASH" not in json.dumps(entry)


@pytest.mark.parametrize("change", [
    {"account_id": "bob"}, {"amount": "21"}, {"reason": "Different reason"},
    {"actor_kind": "console_user", "actor_id": "console-admin"},
])
def test_grant_request_identity_cannot_be_reused_for_a_different_grant(store, change):
    grant(store)
    values = {"account_id": "alice", "amount": "20", "reason": "Test credit",
              "actor_kind": "account", "actor_id": "admin", "idempotency_key": "grant-1", "now": NOW}
    values.update(change)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        store.grant_wallet_credit(**values)
    assert snapshot(store) == before


def test_paid_purchase_claims_once_without_old_count_quota(store):
    slot_id = machine(store)
    grant(store)
    before_slot = store.get_slot(slot_id)
    order = buy(store)
    assert order["account_id"] == "alice" and order["slot_id"] == slot_id
    assert order["amount_cents"] == 1000 and order["currency"] == "USD"
    assert store.wallet_balance("alice") == 1000
    slot = store.get_slot(slot_id)
    assert slot["state"] == slots.CLAIMING and slot["held_by"] == "alice"
    assert slot["gateway_claim_id"] == order["claim_id"] != before_slot["gateway_claim_id"]
    assert slot["gateway_generation"] == order["claim_generation"] == before_slot["gateway_generation"] + 1
    assert store.get_account("alice")["slot_quota"] == 0
    assert buy(store, now=NOW + 10) == order
    assert store.wallet_balance("alice") == 1000
    assert len(store.list_wallet_entries("alice")) == 2
    assert store.get_slot_purchase("alice", order["id"]) == order
    assert store.get_slot_purchase("bob", order["id"]) is None
    assert store.list_slot_purchases("bob") == []


@pytest.mark.parametrize("fault", [
    "disabled", "reserved", "present", "unknown-present", "stale-slot", "future-slot",
    "stale-machine", "future-machine", "no-heartbeat", "no-price", "non-USD", "unavailable",
    "capacity", "extra-slot", "owner-node", "wrong-quote", "no-balance",
])
def test_failed_purchase_preserves_all_rows_and_has_no_debit(store, fault):
    slot_id = machine(store)
    grant(store)
    cents = 1000
    if fault == "disabled":
        store.set_enabled("node-a", False)
    elif fault == "reserved":
        store._conn.execute("UPDATE nodes SET reserved_for='bob' WHERE id='node-a'")
    elif fault == "present":
        store._conn.execute("UPDATE slots SET present=1")
    elif fault == "unknown-present":
        store._conn.execute("UPDATE slots SET present=NULL")
    elif fault == "stale-slot":
        store._conn.execute("UPDATE slots SET reported_at=?", (NOW - 91,))
    elif fault == "future-slot":
        store._conn.execute("UPDATE slots SET reported_at=?", (NOW + 1,))
    elif fault == "stale-machine":
        store._conn.execute("UPDATE heartbeats SET ts=?", (NOW - 91,))
    elif fault == "future-machine":
        store._conn.execute("UPDATE heartbeats SET ts=?", (NOW + 1,))
    elif fault == "no-heartbeat":
        store._conn.execute("DELETE FROM heartbeats")
    elif fault == "no-price":
        store.clear_node_offer("node-a")
    elif fault == "non-USD":
        store.set_node_offer("node-a", hosting=offers.Cost("8", "EUR"), ip=offers.Cost("0", "EUR"))
    elif fault == "unavailable":
        store.set_slot_quota("bob", 1)
        store.claim_slot("bob", now=NOW)
    elif fault == "capacity":
        store.set_machine_capacity("node-a", 2)
    elif fault == "extra-slot":
        store.set_machine_capacity("node-a", 2)
        store.add_slot("another-slot", "node-a", "slot02", now=NOW)
    elif fault == "owner-node":
        store._conn.execute("UPDATE slots SET kind='owner'")
    elif fault == "wrong-quote":
        cents = 999
    elif fault == "no-balance":
        # A different existing account has no credits; no synthetic debit is inserted.
        before = snapshot(store)
        with pytest.raises(InsufficientWalletCredit):
            buy(store, account="bob")
        assert snapshot(store) == before
        return
    store._conn.commit()
    before = snapshot(store)
    error = WalletQuoteChanged if fault == "wrong-quote" else NoSlotAvailable
    with pytest.raises(error):
        buy(store, cents=cents)
    assert snapshot(store) == before
    assert store.get_slot(slot_id) is not None


@pytest.mark.parametrize("change", [{"account": "bob"}, {"node": "node-b"}, {"cents": 999}])
def test_purchase_request_identity_cannot_cross_account_machine_or_price(store, change):
    machine(store)
    machine(store, "node-b")
    grant(store)
    grant(store, "bob", key="grant-bob")
    buy(store)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        buy(store, **change)
    assert snapshot(store) == before


def test_failure_after_claim_and_debit_rolls_back_both_and_the_order(store):
    slot_id = machine(store)
    grant(store)
    store._conn.execute("CREATE TRIGGER reject_order BEFORE INSERT ON slot_purchases "
                        "BEGIN SELECT RAISE(ABORT, 'synthetic order failure'); END")
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic order failure"):
        buy(store)
    assert snapshot(store) == before
    assert store.get_slot(slot_id)["state"] == slots.FREE
    assert store.wallet_balance("alice") == 2000


@pytest.mark.parametrize("table", ["wallet_entries", "slot_purchases"])
@pytest.mark.parametrize("statement", ["UPDATE {table} SET account_id='bob'", "DELETE FROM {table}"])
def test_credit_and_purchase_records_are_immutable(store, table, statement):
    machine(store)
    grant(store)
    buy(store)
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._conn.execute(statement.format(table=table))
    store._conn.rollback()
    assert snapshot(store) == before


def test_reads_do_not_modify_authority_or_expose_other_accounts(store):
    machine(store)
    grant(store)
    order = buy(store)
    before = snapshot(store)
    assert store.wallet_balance("bob") == 0
    assert store.list_wallet_entries("bob") == store.list_slot_purchases("bob") == []
    assert store.get_slot_purchase("bob", order["id"]) is None
    assert snapshot(store) == before


@pytest.mark.parametrize("change", [
    {"amount": "0.001"}, {"amount": 10.0}, {"reason": ""}, {"reason": "x" * 201},
    {"idempotency_key": ""}, {"idempotency_key": "x" * 129}, {"actor_kind": []},
    {"now": float("nan")}, {"account_id": "missing"},
])
def test_invalid_grants_write_nothing(store, change):
    values = {"account_id": "alice", "amount": "10", "actor_id": "admin", "reason": "Test",
              "idempotency_key": "grant-invalid", "now": NOW}
    values.update(change)
    before = snapshot(store)
    with pytest.raises(StoreError):
        store.grant_wallet_credit(**values)
    assert snapshot(store) == before


def test_purchase_cannot_reuse_a_grant_request_identity(store):
    machine(store)
    grant(store)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        buy(store, key="grant-1")
    assert snapshot(store) == before
