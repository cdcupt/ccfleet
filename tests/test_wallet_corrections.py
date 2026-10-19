"""Audited corrections append service credit; they never rewrite access or payments."""
import sqlite3

import pytest

from ccfleetd import wallet
from ccfleetd.store import InsufficientWalletCredit, Store, StoreError, WalletReplayMismatch
from tests.test_wallet_concurrency import database, race
from tests.test_wallet_renewal import deliver, purchased, renew
from tests.test_wallet_store import NOW, buy, grant, machine, snapshot
from tests.test_wallet_store import store as store


def debit(store, amount="5", *, key="debit-1", **changes):
    values = dict(account_id="alice", amount=amount, actor_id="admin", reason="Correct manual credit",
                  idempotency_key=key, now=NOW)
    values.update(changes)
    return store.deduct_wallet_credit(**values)


def credit_return(store, order, amount="5", *, key="return-1", **changes):
    values = dict(account_id="alice", purchase_id=order["id"], amount=amount, actor_id="admin",
                  reason="Explicit service-credit compensation", idempotency_key=key, now=NOW)
    values.update(changes)
    return store.return_purchase_credit(**values)


def test_admin_debit_is_a_separate_immutable_entry_and_replays_after_balance_is_spent(store):
    first = grant(store, amount="10")
    correction = debit(store, "10")
    assert correction["kind"] == "admin_debit" and correction["delta_cents"] == -1000
    assert correction["original_entry_id"] is None
    assert debit(store, "10", now=NOW + 100) == correction
    assert store.wallet_balance("alice") == 0
    assert store.list_wallet_entries("alice") == [correction, first]
    before = snapshot(store)
    with pytest.raises(InsufficientWalletCredit, match="available balance"):
        debit(store, "0.01", key="too-much")
    assert snapshot(store) == before


@pytest.mark.parametrize("operation", ["debit", "return"])
@pytest.mark.parametrize("kind,actor", [
    ("account", "alice"), ("account", "missing"), ("account", "admin token"),
    ("console_user", "console-owner"), ("provider", "stripe:acct_synthetic"),
    ("unknown", "admin"), ([], "admin"),
])
def test_corrections_require_an_actual_current_DB_admin(store, operation, kind, actor):
    machine(store)
    grant(store)
    order = buy(store)
    before = snapshot(store)
    with pytest.raises(StoreError, match="database admin"):
        if operation == "debit":
            debit(store, actor_kind=kind, actor_id=actor)
        else:
            credit_return(store, order, actor_kind=kind, actor_id=actor)
    assert snapshot(store) == before


@pytest.mark.parametrize("operation", ["debit", "return"])
def test_correction_replay_rechecks_admin_role(store, operation):
    machine(store)
    grant(store)
    order = buy(store)
    call = (lambda: debit(store)) if operation == "debit" else (lambda: credit_return(store, order))
    call()
    store.set_account_role("admin", "user")
    before = snapshot(store)
    with pytest.raises(StoreError, match="database admin"):
        call()
    assert snapshot(store) == before


def test_console_admin_can_record_an_explicit_service_credit_return(store):
    _, order = purchased(store)
    entry = credit_return(store, order, actor_kind="console_user", actor_id="console-admin")
    assert entry["actor_kind"] == "console_user" and entry["actor_id"] == "console-admin"


@pytest.mark.parametrize("operation", ["debit", "return"])
@pytest.mark.parametrize("change", [
    {"amount": "0.001"}, {"amount": 10.0}, {"amount": "0"}, {"amount": "-1"},
    {"reason": ""}, {"reason": "x" * 201}, {"reason": "two\nlines"},
    {"idempotency_key": ""}, {"idempotency_key": "x" * 129},
    {"actor_id": " admin"}, {"now": float("nan")}, {"account_id": "missing"},
])
def test_invalid_correction_input_changes_no_rows(store, operation, change):
    machine(store)
    grant(store)
    order = buy(store)
    before = snapshot(store)
    with pytest.raises(StoreError):
        if operation == "debit":
            debit(store, **change)
        else:
            credit_return(store, order, **change)
    assert snapshot(store) == before


@pytest.mark.parametrize("change", [
    {"account_id": "bob"}, {"amount": "4"}, {"reason": "Different correction"},
    {"actor_kind": "console_user", "actor_id": "console-admin"},
])
def test_debit_idempotency_binds_target_amount_reason_and_admin(store, change):
    grant(store)
    debit(store)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        debit(store, **change)
    assert snapshot(store) == before


def test_manual_kinds_cannot_share_one_request_identity(store):
    machine(store)
    grant(store)
    order = buy(store)
    debit(store)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        credit_return(store, order, key="debit-1")
    with pytest.raises(WalletReplayMismatch):
        debit(store, key="grant-1")
    with pytest.raises(WalletReplayMismatch):
        grant(store, key="debit-1")
    assert snapshot(store) == before


def test_partial_returns_are_linked_capped_and_replay_after_the_cap_is_reached(store):
    _, order = purchased(store, amount="10")
    first = credit_return(store, order, "3")
    second = credit_return(store, order, "7", key="return-2")
    assert first["kind"] == second["kind"] == "purchase_return"
    assert first["original_entry_id"] == second["original_entry_id"] == order["wallet_entry_id"]
    assert first["delta_cents"] == 300 and second["delta_cents"] == 700
    assert credit_return(store, order, "3", now=NOW + 1) == first
    assert store.wallet_balance("alice") == 1000
    debit(store, "10")
    before = snapshot(store)
    with pytest.raises(StoreError, match="unreturned amount"):
        credit_return(store, order, "0.01", key="return-3")
    assert snapshot(store) == before


@pytest.mark.parametrize("change", [
    {"amount": "4"}, {"reason": "Different compensation"},
    {"actor_kind": "console_user", "actor_id": "console-admin"},
])
def test_return_idempotency_binds_amount_reason_and_admin(store, change):
    _, order = purchased(store)
    credit_return(store, order)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        credit_return(store, order, **change)
    assert snapshot(store) == before


def test_return_cannot_target_another_account_or_unrelated_purchase(store):
    machine(store)
    machine(store, "node-b")
    grant(store, amount="30")
    first = buy(store)
    second = buy(store, node="node-b", key="purchase-2")
    credit_return(store, first)
    before = snapshot(store)
    with pytest.raises(StoreError, match="matching purchase debit"):
        credit_return(store, first, account_id="bob", key="return-bob")
    with pytest.raises(StoreError, match="matching purchase debit"):
        credit_return(store, first, purchase_id="missing", key="return-missing")
    with pytest.raises(StoreError):
        credit_return(store, first, purchase_id="")
    with pytest.raises(WalletReplayMismatch):
        credit_return(store, second)
    assert snapshot(store) == before


def test_return_does_not_change_original_order_paid_period_slot_or_manual_payment(store):
    slot_id, first = purchased(store)
    renewal = renew(store, slot_id, first["claim_id"])
    store.record_payment("alice", amount="50", currency="CNY", through="2026-01-01",
        recorded_by="old operator", note="Historical record", now=NOW)
    old_orders, old_slot = store.list_slot_purchases("alice"), store.get_slot(slot_id)
    old_payments, old_account = store.list_payments("alice"), store.get_account("alice")
    old_entries = store.list_wallet_entries("alice")
    credit_return(store, first, "10")
    credit_return(store, renewal, "10", key="return-renewal")
    assert store.list_slot_purchases("alice") == old_orders
    assert store.get_slot(slot_id) == old_slot and store.get_account("alice") == old_account
    assert store.list_payments("alice") == old_payments
    assert store.list_wallet_entries("alice")[2:] == old_entries
    assert store.paid_slot_through("alice", slot_id) == renewal["period_end_at"]


def test_manual_return_of_old_rental_cannot_change_the_new_holders_claim(store):
    slot_id, first = purchased(store)
    store.begin_release(slot_id, held_by="alice")
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=NOW + 1)
    store.set_slot_quota("bob", 1)
    later = store.claim_slot("bob", node_id="node-a", now=NOW + 2)
    deliver(store, slot_id, at=NOW + 2)
    before = store.get_slot(slot_id)
    credit_return(store, first, "10")
    assert store.get_slot(slot_id) == before
    assert before["gateway_claim_id"] == later["gateway_claim_id"] != first["claim_id"]
    assert store.paid_slot_through("bob", slot_id) is None
    assert store.wallet_balance("bob") == 0


def test_return_respects_maximum_balance_without_consuming_return_allowance(store):
    machine(store)
    grant(store, amount=wallet.MAX_DOLLARS)
    order = buy(store)
    grant(store, amount="10", key="restore-max")
    before = snapshot(store)
    with pytest.raises(StoreError, match="supported range"):
        credit_return(store, order, "10")
    assert snapshot(store) == before
    debit(store, "10")
    credit_return(store, order, "10")
    assert store.wallet_balance("alice") == wallet.MAX_CENTS


@pytest.mark.parametrize("operation", ["debit", "return"])
def test_insertion_failure_rolls_back_the_entire_manual_correction(store, operation):
    _, order = purchased(store)
    store._conn.execute("CREATE TRIGGER reject_correction BEFORE INSERT ON wallet_entries "
        "WHEN NEW.kind IN ('admin_debit','purchase_return') "
        "BEGIN SELECT RAISE(ABORT, 'synthetic correction failure'); END")
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic correction failure"):
        debit(store) if operation == "debit" else credit_return(store, order)
    assert snapshot(store) == before


@pytest.mark.parametrize("operation", ["debit", "return"])
def test_separate_connections_cannot_exceed_balance_or_original_purchase_cap(tmp_path, operation):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    try:
        order = buy(a) if operation == "return" else None
        calls = [(lambda db=db, key=key: debit(db, "7", key=key)) if operation == "debit"
                 else (lambda db=db, key=key: credit_return(db, order, "7", key=key))
                 for db, key in ((a, "correction-a"), (b, "correction-b"))]
        entries, errors = race(*calls)
        assert len(entries) == len(errors) == 1
        assert isinstance(errors[0], StoreError)
        assert a.wallet_balance("alice") == (300 if operation == "debit" else 700)
        assert len([row for row in a.list_wallet_entries("alice")
                    if row["kind"] in {"admin_debit", "purchase_return"}]) == 1
    finally:
        a.close()
        b.close()


def test_matching_return_on_two_connections_credits_once_and_replays_after_restart(tmp_path):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    try:
        order = buy(a)
        entries, errors = race(lambda: credit_return(a, order, "10"),
                               lambda: credit_return(b, order, "10"))
        assert not errors and entries[0] == entries[1]
        assert a.wallet_balance("alice") == 1000
    finally:
        a.close()
        b.close()
    later = Store(path)
    try:
        assert credit_return(later, order, "10") == entries[0]
        assert later.wallet_balance("alice") == 1000
    finally:
        later.close()


@pytest.mark.parametrize("kind,delta,actor,original,allowed", [
    ("admin_credit", 1, "account", None, True),
    ("admin_debit", -1, "console_user", None, True),
    ("purchase_return", 1, "account", "original", True),
    ("stripe_topup", 1, "provider", None, True),
    ("stripe_reversal", -1, "provider", "original", True),
    ("admin_credit", 1, "provider", None, False),
    ("admin_debit", -1, "provider", None, False),
    ("slot_purchase", -1, "provider", None, False),
    ("purchase_return", 1, "provider", "original", False),
    ("stripe_topup", 1, "account", None, False),
    ("stripe_reversal", -1, "console_user", "original", False),
    ("purchase_return", 1, "account", None, False),
    ("admin_credit", 1, "account", "original", False),
    ("admin_debit", 1, "account", None, False),
    ("purchase_return", -1, "account", "original", False),
    ("stripe_topup", -1, "provider", None, False),
    ("stripe_reversal", 1, "provider", None, False),
])
def test_schema_reserves_provider_actors_and_signed_immutable_operation_kinds(
        store, kind, delta, actor, original, allowed):
    first = grant(store)
    statement = ("INSERT INTO wallet_entries (id,account_id,kind,delta_cents,actor_kind,"
                 "actor_id,reason,idempotency_key,original_entry_id,created_at) "
                 "VALUES ('synthetic','alice',?,?,?,'synthetic actor','Synthetic test',"
                 "'synthetic',?,?)")
    values = (kind, delta, actor, first["id"] if original else None, NOW)
    if allowed:
        store._conn.execute(statement, values)
        store._conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._conn.execute("UPDATE wallet_entries SET delta_cents=2 WHERE id='synthetic'")
        store._conn.rollback()
    else:
        before = snapshot(store)
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(statement, values)
        store._conn.rollback()
        assert snapshot(store) == before


def review_hold(store):
    order = store.create_wallet_topup("alice", 1000, merchant_id="acct_synthetic",
        livemode=False, idempotency_key="held-topup", now=NOW)
    store._conn.execute("INSERT INTO wallet_topup_review_holds "
        "VALUES ('hold','alice',?,'pi_synthetic','evt_synthetic','charge.refunded',?,?)",
        (order["id"], "a" * 64, NOW))
    store._conn.commit()


def test_payment_review_blocks_new_purchases_but_preserves_exact_receipt_replay(store):
    machine(store)
    machine(store, "node-b")
    grant(store, amount="30")
    first = buy(store)
    review_hold(store)
    before = snapshot(store)
    assert buy(store) == first
    with pytest.raises(StoreError, match="payment review before a new purchase"):
        buy(store, node="node-b", key="purchase-2")
    assert snapshot(store) == before


def test_payment_review_blocks_new_renewals_without_pausing_a_paid_period(store):
    slot_id, first = purchased(store, amount="40")
    renewal = renew(store, slot_id, first["claim_id"])
    review_hold(store)
    before = snapshot(store)
    assert renew(store, slot_id, first["claim_id"]) == renewal
    with pytest.raises(StoreError, match="payment review before a new purchase"):
        renew(store, slot_id, first["claim_id"], key="renew-2")
    assert store.paid_slot_through("alice", slot_id) == renewal["period_end_at"]
    assert snapshot(store) == before
