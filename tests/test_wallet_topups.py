"""Local financial boundary tests only; no Stripe account or real charge."""

from __future__ import annotations

import concurrent.futures
import hashlib
import sqlite3
import threading

import pytest

from ccfleetd import wallet_topups
from ccfleetd.store import Store, StoreError, WalletReplayMismatch
from tests.test_wallet_store import NOW, accounts, buy, machine, snapshot

MERCHANT = "acct_synthetic"
DIGEST = hashlib.sha256(b"verified synthetic event body").hexdigest()


@pytest.fixture
def store():
    result = Store(":memory:")
    accounts(result)
    yield result
    result.close()


def order(store, *, key="topup-1", account="alice", cents=2000, mode=False):
    return store.create_wallet_topup(
        account, cents, merchant_id=MERCHANT, livemode=mode, idempotency_key=key, now=NOW
    )


def receipt(value, *, session="cs_test_synthetic", intent="pi_synthetic"):
    return dict(
        order_id=value["id"],
        account_id=value["account_id"],
        amount_cents=value["amount_cents"],
        currency="USD",
        merchant_id=MERCHANT,
        livemode=bool(value["livemode"]),
        payment_status="paid",
        session_id=session,
        payment_intent_id=intent,
    )


def settle(store, value, *, event="evt_synthetic", kind="checkout.session.completed", **changes):
    paid = receipt(value)
    paid.update(changes)
    return store.settle_wallet_topup(
        value["id"], paid, event_id=event, event_type=kind, body_sha256=DIGEST, now=NOW
    )


def ready(store):
    value = order(store)
    store.bind_wallet_checkout(value["id"], "cs_test_synthetic", now=NOW)
    return value


def hold(store, *, event="evt_refund", kind="charge.refunded", **changes):
    return store.hold_wallet_topup(
        "pi_synthetic", event_id=event, event_type=kind, body_sha256=DIGEST, now=NOW, **changes
    )


def test_order_and_checkout_never_add_balance_and_owner_is_private(store):
    value = ready(store)
    assert order(store) == value
    assert store.get_wallet_topup("alice", value["id"]) == value
    assert store.get_wallet_topup("bob", value["id"]) is None
    assert store.wallet_balance("alice") == 0
    assert store.list_wallet_entries("alice") == []


@pytest.mark.parametrize(
    "changes",
    [dict(cents=0), dict(cents=True), dict(cents=1001), dict(mode=1), dict(account="unknown")],
)
def test_invalid_orders_preserve_database(store, changes):
    before = snapshot(store)
    with pytest.raises(StoreError):
        order(store, **changes)
    assert snapshot(store) == before


@pytest.mark.parametrize("changes", [dict(cents=1000), dict(mode=True), dict(account="bob")])
def test_idempotency_key_cannot_be_rebound_to_other_value_owner_or_mode(store, changes):
    order(store)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        order(store, **changes)
    assert snapshot(store) == before


def test_one_credit_for_duplicate_and_different_success_events(store):
    value = ready(store)
    paid = settle(store, value)
    assert settle(store, value) == paid
    assert (
        settle(store, value, event="evt_async", kind="checkout.session.async_payment_succeeded")
        == paid
    )
    assert store.wallet_balance("alice") == 2000
    assert len(store.list_wallet_entries("alice")) == 1
    assert store._conn.execute("SELECT COUNT(*) FROM wallet_topup_events").fetchone()[0] == 2
    assert paid["actor_kind"] == "provider" and paid["kind"] == "stripe_topup"
    assert store.list_payments("alice") == []


@pytest.mark.parametrize(
    "changes",
    [
        dict(amount_cents=1000),
        dict(amount_cents=True),
        dict(livemode=True),
        dict(livemode=0),
        dict(currency="EUR"),
        dict(currency="usd"),
        dict(account_id="bob"),
        dict(merchant_id="acct_other"),
        dict(order_id="unknown"),
        dict(payment_status="unpaid"),
        dict(session_id="cs_other"),
        dict(payment_intent_id="bad"),
    ],
)
def test_mismatched_paid_receipt_cannot_credit_and_rolls_back(store, changes):
    value = ready(store)
    before = snapshot(store)
    with pytest.raises(StoreError):
        settle(store, value, **changes)
    assert snapshot(store) == before


def test_event_reuse_and_payment_intent_reuse_cannot_double_credit(store):
    value = ready(store)
    settle(store, value)
    another = order(store, key="topup-2")
    store.bind_wallet_checkout(another["id"], "cs_test_second", now=NOW)
    before = snapshot(store)
    for event, intent in (("evt_synthetic", "pi_other"), ("evt_other", "pi_synthetic")):
        with pytest.raises(WalletReplayMismatch):
            settle(
                store, another, event=event, session_id="cs_test_second", payment_intent_id=intent
            )
        assert snapshot(store) == before


def test_checkout_binding_is_unique_and_immutable(store):
    first = ready(store)
    second = order(store, key="topup-2")
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        store.bind_wallet_checkout(second["id"], "cs_test_synthetic", now=NOW)
    with pytest.raises(WalletReplayMismatch):
        store.bind_wallet_checkout(first["id"], "cs_other", now=NOW)
    assert snapshot(store) == before


def test_unbound_or_nonpayment_event_cannot_credit(store):
    value = order(store)
    before = snapshot(store)
    with pytest.raises(StoreError):
        settle(store, value)
    with pytest.raises(StoreError):
        settle(store, value, kind="checkout.session.expired")
    assert snapshot(store) == before


def test_refund_hold_blocks_new_spend_without_deleting_access_or_editing_credit(store):
    value = ready(store)
    settle(store, value)
    slot = machine(store)
    paid_slot = buy(store)
    old_slot = store.get_slot(slot)
    review = hold(store)
    assert hold(store) == review
    assert store.wallet_spending_blocked("alice")
    assert not store.wallet_spending_blocked("bob")
    assert store.wallet_balance("alice") == 1000
    assert store.get_slot(slot) == old_slot
    assert store.get_slot_purchase("alice", paid_slot["id"]) == paid_slot
    assert buy(store) == paid_slot  # replay is not new spending
    machine(store, "node-b")
    before = snapshot(store)
    with pytest.raises(StoreError, match="payment review"):
        buy(store, node="node-b", key="new-purchase")
    with pytest.raises(StoreError, match="payment review"):
        order(store, key="new-topup")
    assert snapshot(store) == before


def test_hold_requires_matched_payment_and_only_real_admin_can_resolve(store):
    with pytest.raises(StoreError, match="retry later"):
        hold(store)
    value = ready(store)
    settle(store, value)
    review = hold(store)
    values = dict(
        actor_id="admin",
        actor_kind="account",
        reason="Reviewed provider and balance",
        idempotency_key="resolve-1",
        now=NOW,
    )
    before = snapshot(store)
    with pytest.raises(StoreError, match="database admin"):
        store.resolve_wallet_topup_hold(review["id"], **{**values, "actor_id": "alice"})
    assert snapshot(store) == before
    resolved = store.resolve_wallet_topup_hold(review["id"], **values)
    assert store.resolve_wallet_topup_hold(review["id"], **values) == resolved
    assert not store.wallet_spending_blocked("alice")
    assert store.wallet_balance("alice") == 2000
    store.set_account_role("admin", "user")
    with pytest.raises(StoreError, match="database admin"):
        store.resolve_wallet_topup_hold(review["id"], **values)


@pytest.mark.parametrize(
    "table",
    [
        "wallet_topups",
        "wallet_topup_sessions",
        "wallet_topup_receipts",
        "wallet_topup_events",
        "wallet_topup_review_holds",
        "wallet_topup_review_resolutions",
    ],
)
def test_topup_audit_records_reject_edits_and_deletions(store, table):
    settle(store, ready(store))
    review = hold(store)
    store.resolve_wallet_topup_hold(
        review["id"],
        actor_id="admin",
        actor_kind="account",
        reason="Reviewed",
        idempotency_key="reviewed",
        now=NOW,
    )
    for statement in (f"DELETE FROM {table}", f"UPDATE {table} SET created_at=0"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._conn.execute(statement)
        store._conn.rollback()


def test_two_connections_settle_one_paid_order_once(tmp_path):
    path = str(tmp_path / "wallet.db")
    setup = Store(path)
    accounts(setup)
    value = ready(setup)
    setup.close()

    def attempt(event):
        connection = Store(path)
        try:
            return settle(connection, value, event=event)["id"]
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        identifiers = list(pool.map(attempt, ("evt_concurrent1", "evt_concurrent2")))
    check = Store(path)
    try:
        assert len(set(identifiers)) == 1
        assert check.wallet_balance("alice") == 2000
        assert len(check.list_wallet_entries("alice")) == 1
    finally:
        check.close()


def test_schema_is_additive_and_opening_existing_database_does_not_convert_payments(tmp_path):
    path = str(tmp_path / "legacy.db")
    first = Store(path)
    accounts(first)
    first.record_payment(
        "alice",
        amount="500",
        currency="USD",
        through="2026-01-01",
        recorded_by="legacy",
        note="old record",
        now=NOW,
    )
    old = first.list_payments("alice")
    first.close()
    reopened = Store(path)
    try:
        assert reopened.wallet_balance("alice") == 0
        assert reopened.list_payments("alice") == old
        assert reopened._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert wallet_topups.ALLOWED_CENTS == (1000, 2000, 5000, 10000)
    finally:
        reopened.close()


def test_verified_out_of_order_review_holds_before_credit_and_binds_the_payment(store):
    value = ready(store)
    reference = receipt(value)
    review = hold(store, reference=reference)
    assert review["payment_intent_id"] == "pi_synthetic"
    assert store.wallet_spending_blocked("alice") and store.wallet_balance("alice") == 0
    assert store.wallet_topup_for_payment("pi_synthetic") == value
    assert hold(store, reference=reference) == review
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        settle(store, value, payment_intent_id="pi_different")
    assert snapshot(store) == before
    settle(store, value)
    assert store.wallet_balance("alice") == 2000
    assert store.wallet_spending_blocked("alice")


@pytest.mark.parametrize("changes", [
    dict(order_id="missing"), dict(order_id=[]), dict(account_id="bob"),
    dict(amount_cents=1000), dict(amount_cents=True), dict(currency="EUR"),
    dict(merchant_id="acct_other"), dict(livemode=True), dict(livemode=0),
    dict(payment_intent_id="pi_different"),
])
def test_unsettled_hold_requires_exact_verified_local_reference(store, changes):
    value = ready(store)
    reference = {**receipt(value), **changes}
    before = snapshot(store)
    with pytest.raises(StoreError):
        hold(store, reference=reference)
    assert snapshot(store) == before


def test_a_payment_cannot_be_associated_with_another_held_order(store):
    first = ready(store)
    hold(store, reference=receipt(first))
    second = order(store, account="bob", key="topup-bob")
    store.bind_wallet_checkout(second["id"], "cs_test_second", now=NOW)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        hold(store, event="evt_other", reference=receipt(second))
    with pytest.raises(WalletReplayMismatch):
        settle(store, second, event="evt_other", session_id="cs_test_second")
    assert snapshot(store) == before


def test_one_event_identity_cannot_change_between_payment_and_review(store):
    value = ready(store)
    settle(store, value)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        hold(store, event="evt_synthetic")
    assert snapshot(store) == before
    hold(store, event="evt_review")
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        settle(store, value, event="evt_review")
    assert snapshot(store) == before


def test_public_order_identity_cannot_preoccupy_future_provider_ledger_key(store):
    value = ready(store)
    store.grant_wallet_credit("alice", "1", actor_id="admin", reason="Synthetic key test",
        idempotency_key="stripe-topup:" + value["id"], now=NOW)
    paid = settle(store, value)
    assert paid["idempotency_key"] != "stripe-topup:" + value["id"]
    assert store.wallet_balance("alice") == 2100


def test_checkout_lookup_is_owner_scoped_and_server_binding_reads_are_inert(store):
    value = ready(store)
    assert store.get_wallet_checkout("alice", value["id"]) == "cs_test_synthetic"
    assert store.get_wallet_checkout("bob", value["id"]) is None
    assert store.wallet_topup_for_checkout("cs_test_synthetic") == value
    before = snapshot(store)
    assert store.wallet_topup_for_checkout("cs_test_unknown") is None
    assert store.wallet_topup_for_payment("pi_unknown") is None
    assert snapshot(store) == before


@pytest.mark.parametrize("target", ["wallet_topup_receipts", "wallet_topup_events"])
def test_paid_settlement_failure_rolls_back_credit_and_receipt(store, target):
    value = ready(store)
    store._conn.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON " + target
                        + " BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        settle(store, value)
    assert snapshot(store) == before


def test_event_failure_rolls_back_new_review_hold(store):
    value = ready(store)
    store._conn.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON wallet_topup_events "
                        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        hold(store, reference=receipt(value))
    assert snapshot(store) == before


def test_two_connections_serialize_early_hold_against_conflicting_payment(tmp_path):
    path = str(tmp_path / "early-hold.db")
    setup = Store(path)
    accounts(setup)
    value = ready(setup)
    setup.close()

    def attempt(operation):
        connection = Store(path)
        try:
            if operation == "hold":
                return hold(connection, reference=receipt(value))
            return settle(connection, value, payment_intent_id="pi_different")
        except WalletReplayMismatch as error:
            return error
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("hold", "settle")))
    assert sum(isinstance(value, WalletReplayMismatch) for value in results) == 1
    check = Store(path)
    try:
        if check.wallet_spending_blocked("alice"):
            assert check.wallet_balance("alice") == 0
            assert check.wallet_topup_for_payment("pi_synthetic") == value
            assert check.wallet_topup_for_payment("pi_different") is None
        else:
            assert check.wallet_balance("alice") == 2000
            assert check.wallet_topup_for_payment("pi_different") == value
            assert check.wallet_topup_for_payment("pi_synthetic") is None
    finally:
        check.close()


def test_early_review_cannot_resolve_until_matched_credit_receipt_exists(store):
    value = ready(store)
    review = hold(store, reference=receipt(value))
    values = dict(actor_id="admin", actor_kind="account", reason="Reviewed provider and credit",
                  idempotency_key="review-after-receipt", now=NOW)
    before = snapshot(store)
    with pytest.raises(StoreError, match="wait for the matched payment receipt"):
        store.resolve_wallet_topup_hold(review["id"], **values)
    assert snapshot(store) == before
    settle(store, value)
    store.deduct_wallet_credit("alice", "20", actor_id="admin", reason="Returned top-up review",
                              idempotency_key="reconcile-returned-credit", now=NOW)
    store.resolve_wallet_topup_hold(review["id"], **values)
    assert store.wallet_balance("alice") == 0
    assert not store.wallet_spending_blocked("alice")


def test_resolution_racing_delayed_settlement_stays_held_until_admin_reconciles(tmp_path):
    path = str(tmp_path / "delayed-receipt.db")
    setup = Store(path)
    accounts(setup)
    value = ready(setup)
    review = hold(setup, reference=receipt(value))
    setup.close()
    a, b = Store(path), Store(path)
    entered, release, attempted, done = (threading.Event() for _ in range(4))
    errors, entries = [], []
    original_admin = a._wallet_admin

    def held_admin(*args):
        original_admin(*args)
        entered.set()
        assert release.wait(timeout=5)

    a._wallet_admin = held_admin
    real_connection = b._conn

    class Connection:
        def execute(self, statement, parameters=()):
            if statement == "BEGIN IMMEDIATE":
                attempted.set()
            return real_connection.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(real_connection, name)

    b._conn = Connection()

    def resolve():
        try:
            a.resolve_wallet_topup_hold(review["id"], actor_id="admin", actor_kind="account",
                reason="Review before receipt", idempotency_key="early-resolution", now=NOW)
        except Exception as error:
            errors.append(error)

    def delayed_settlement():
        try:
            entries.append(settle(b, value))
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    try:
        first = threading.Thread(target=resolve)
        second = threading.Thread(target=delayed_settlement)
        first.start()
        assert entered.wait(timeout=5)
        second.start()
        assert attempted.wait(timeout=5)
        assert not done.wait(timeout=0.1)
        release.set()
        for thread in (first, second):
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert len(errors) == len(entries) == 1
        assert isinstance(errors[0], StoreError) and "matched payment receipt" in str(errors[0])
        assert a.wallet_balance("alice") == 2000 and a.wallet_spending_blocked("alice")
        assert a._conn.execute("SELECT COUNT(*) FROM wallet_topup_review_resolutions").fetchone()[0] == 0
    finally:
        release.set()
        a.close()
        b.close()
