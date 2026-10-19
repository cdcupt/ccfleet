"""Real temporary SQLite subscription transactions; no provider or host access."""

from __future__ import annotations

import socket
import sqlite3
import subprocess

import pytest

from ccfleetd import offers, slots
from ccfleetd.store import (
    NoSlotAvailable,
    Store,
    StoreError,
    SubscriptionCheckoutPending,
    SubscriptionReleaseBlocked,
    WalletReplayMismatch,
)
from tests.test_wallet_concurrency import race
from tests.test_wallet_renewal import deliver
from tests.test_wallet_store import NOW, accounts, buy, grant, machine, snapshot
from tests.test_wallet_topups import hold as topup_hold
from tests.test_wallet_topups import ready as topup_ready
from tests.test_wallet_topups import settle as topup_settle

MERCHANT = "acct_synthetic"
FINANCIAL = ("wallet_entries", "slot_purchases", "wallet_topups", "wallet_topup_sessions",
             "wallet_topup_receipts", "wallet_topup_events", "wallet_topup_review_holds",
             "wallet_topup_review_resolutions")


@pytest.fixture(autouse=True)
def no_provider_or_process(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Only temporary SQLite and synthetic receipts belong in this suite")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def store():
    value = Store(":memory:")
    accounts(value)
    machine(value)
    yield value
    value.close()


def reserve(store, account="alice", node="node-a", key="subscription-one", **changes):
    values = dict(expected_cents=1000, idempotency_key=key, monthly_consent=True, merchant_id=MERCHANT,
                  livemode=False, now=NOW, max_age_s=90)
    values.update(changes)
    return store.reserve_slot_subscription(account, node, **values)


def customer(store, account="alice", identifier="cus_synthetic"):
    store.prepare_subscription_customer(account, merchant_id=MERCHANT, livemode=False, now=NOW)
    return store.bind_subscription_customer(account, merchant_id=MERCHANT, livemode=False,
                                             customer_id=identifier, now=NOW)


def bound(store, **changes):
    order = reserve(store, **changes)
    customer(store)
    store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
                                            expires_at=NOW + 1800, now=NOW)
    return store.bind_slot_subscription(order["id"], session_id="cs_test_synthetic",
        subscription_id="sub_synthetic", customer_id="cus_synthetic", now=NOW)


def paid(store, order, **changes):
    values = dict(invoice_id="in_synthetic", payment_intent_id="pi_synthetic_subscription",
                  subscription_id="sub_synthetic",
                  customer_id="cus_synthetic", merchant_id=MERCHANT, livemode=False,
                  currency="USD", amount_cents=1000, period_start_at=NOW - 100,
                  period_end_at=NOW + 1000, paid=True, event_id="evt_synthetic",
                  body_sha256="a" * 64, now=NOW)
    values.update(changes)
    return store.record_slot_subscription_invoice(order["id"], **values)


def unpaid_expired(order):
    return dict(order_id=order["id"], session_id="cs_test_synthetic", session_status="expired",
                payment_status="unpaid", subscription_id=None, merchant_id=MERCHANT, livemode=False)


def cancellation_proof(order, **changes):
    return {"subscription_id": order["subscription_id"], "customer_id": order["customer_id"],
            "merchant_id": order["merchant_id"], "livemode": bool(order["livemode"]),
            "status": "active", "cancel_at_period_end": True, **changes}


def test_reservation_replays_without_claim_or_credit_and_never_converts_held_slots(store):
    original_slot = store.get_slot("slot-node-a")
    order = reserve(store)
    assert reserve(store) == order and order["slot_id"] == "slot-node-a"
    assert order["status"] == "reserved" and order["amount_cents"] == 1000
    assert order["consent_version"] == "monthly-slot-v1" and order["consented_at"] == NOW
    assert order["billing_interval"] == "month"
    assert order["machine_number"] == store.get_node("node-a")["public_status_number"]
    assert store.get_slot("slot-node-a") == original_slot
    assert store.wallet_balance("alice") == 0
    assert store.list_wallet_entries("alice") == store.list_slot_purchases("alice") == []
    assert store.get_slot_subscription(order["id"], account_id="bob") is None
    assert store.get_slot_subscription("unknown") is None
    assert store.list_slot_subscriptions("alice") == [order]
    assert store.list_slot_subscriptions("bob") == []


def test_omitted_monthly_consent_defaults_to_no_purchase(store):
    before = snapshot(store)
    with pytest.raises(StoreError, match="consent"):
        store.reserve_slot_subscription("alice", "node-a", expected_cents=1000,
            idempotency_key="no-consent", merchant_id=MERCHANT, livemode=False,
            now=NOW, max_age_s=90)
    assert snapshot(store) == before


@pytest.mark.parametrize("change", [dict(account="bob"), dict(node="node-b"),
                                   dict(expected_cents=2000), dict(merchant_id="acct_other"),
                                   dict(livemode=True)])
def test_reservation_request_cannot_be_reused_for_another_purchase(store, change):
    reserve(store)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        reserve(store, **change)
    assert snapshot(store) == before


@pytest.mark.parametrize("change", [dict(expected_cents=True), dict(expected_cents=0),
                                   dict(livemode=0), dict(now=float("nan")),
                                   dict(merchant_id="sk_not_an_account"), dict(max_age_s=0),
                                   dict(monthly_consent=False), dict(monthly_consent=1)])
def test_invalid_reservation_values_do_not_write(store, change):
    before = snapshot(store)
    with pytest.raises(StoreError):
        reserve(store, **change)
    assert snapshot(store) == before


def test_existing_claim_cannot_be_converted_to_subscription(store):
    store.set_slot_quota("alice", 1)
    store.claim_slot("alice", node_id="node-a", now=NOW)
    before = snapshot(store)
    with pytest.raises(NoSlotAvailable):
        reserve(store)
    assert snapshot(store) == before


@pytest.mark.parametrize("state", ["reserved", "checkout_open", "unknown"])
def test_pending_or_unknown_checkout_blocks_all_other_claims_past_local_expiry(store, state):
    order = reserve(store)
    if state != "reserved":
        store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
                                                expires_at=NOW + 1800, now=NOW)
    if state == "unknown":
        store.mark_slot_subscription_unknown(order["id"], now=NOW + 1)
    store.set_slot_quota("bob", 1)
    grant(store, "bob", "10", key="grant-bob")
    for at in (NOW, NOW + 86400):
        before = snapshot(store)
        with pytest.raises(NoSlotAvailable):
            store.claim_slot("bob", node_id="node-a", now=at)
        with pytest.raises(NoSlotAvailable):
            buy(store, "bob", now=at)
        assert snapshot(store) == before


def test_provider_confirmed_expired_unpaid_checkout_releases_only_reservation(store):
    order = reserve(store)
    store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
                                            expires_at=NOW + 1800, now=NOW)
    result = store.close_slot_subscription(order["id"], proof=unpaid_expired(order), now=NOW + 1801)
    assert result["status"] == "closed"
    assert store.close_slot_subscription(order["id"], proof=unpaid_expired(order), now=NOW + 1802) == result
    grant(store, "bob", "10", key="grant-bob")
    assert buy(store, "bob")["account_id"] == "bob"


@pytest.mark.parametrize("change", [dict(session_id="cs_test_other"), dict(session_status="open"),
                                   dict(payment_status="paid"), dict(subscription_id="sub_synthetic"),
                                   dict(merchant_id="acct_other"), dict(livemode=0),
                                   dict(order_id="different")])
def test_untrusted_or_mismatched_expiry_proof_cannot_free_inventory(store, change):
    order = reserve(store)
    store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
                                            expires_at=NOW + 1800, now=NOW)
    proof = {**unpaid_expired(order), **change}
    before = snapshot(store)
    with pytest.raises(StoreError):
        store.close_slot_subscription(order["id"], proof=proof, now=NOW + 1801)
    assert snapshot(store) == before


def test_paid_invoice_claims_exact_slot_and_uses_provider_period_without_wallet_credit(store):
    order = bound(store)
    entitlement = paid(store, order)
    current = store.get_slot(order["slot_id"])
    assert current["held_by"] == "alice" and current["state"] == slots.CLAIMING
    assert entitlement["claim_id"] == current["gateway_claim_id"]
    assert entitlement["period_start_at"] == NOW - 100
    assert entitlement["period_end_at"] == NOW + 1000
    assert store.wallet_balance("alice") == 0
    assert store.list_slot_purchases("alice") == store.list_wallet_entries("alice") == []
    assert paid(store, order, now=NOW + 10) == entitlement
    assert paid(store, order, event_id="evt_redelivery", now=NOW + 20) == entitlement
    assert store._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM slot_subscription_events").fetchone()[0] == 2


def test_reserved_monthly_quote_survives_later_machine_offer_changes(store):
    order = bound(store)
    original_quote = order["offer_snapshot"]
    store.set_node_offer("node-a", hosting=offers.Cost("98", "USD"), ip=offers.Cost("0", "USD"))
    entitlement = paid(store, order)
    assert entitlement["amount_cents"] == 1000
    current = store.get_slot_subscription(order["id"])
    assert current["offer_snapshot"] == original_quote and current["amount_cents"] == 1000


def test_plan_label_and_consent_terms_are_immutable_server_snapshots(store):
    order = reserve(store)
    store._conn.execute("UPDATE nodes SET public_status_number=public_status_number+100 WHERE id='node-a'")
    store._conn.commit()
    assert store.get_slot_subscription(order["id"])["machine_number"] == order["machine_number"]
    for change in ("machine_number=machine_number+1", "amount_cents=amount_cents+1",
                   "consented_at=consented_at+1"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._conn.execute("UPDATE slot_subscription_orders SET " + change)
        store._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._conn.execute("DELETE FROM slot_subscription_orders")
    store._conn.rollback()


@pytest.mark.parametrize("change", [dict(paid=False), dict(paid=1), dict(currency="usd"),
                                   dict(currency="EUR"), dict(amount_cents=999), dict(amount_cents=True),
                                   dict(subscription_id="sub_other"), dict(customer_id="cus_other"),
                                   dict(merchant_id="acct_other"), dict(livemode=True), dict(livemode=0),
                                   dict(period_end_at=NOW - 100), dict(period_start_at=float("nan")),
                                   dict(invoice_id="bad"), dict(body_sha256="bad")])
def test_invoice_requires_exact_paid_scope_and_period_without_partial_claim(store, change):
    order = bound(store)
    before = snapshot(store)
    with pytest.raises(StoreError):
        paid(store, order, **change)
    assert snapshot(store) == before


def test_invoice_replay_cannot_change_period_or_rebind_event(store):
    order = bound(store)
    paid(store, order)
    before = snapshot(store)
    for changes in (dict(period_end_at=NOW + 1001), dict(body_sha256="b" * 64),
                    dict(invoice_id="in_different")):
        with pytest.raises(WalletReplayMismatch):
            paid(store, order, **changes)
        assert snapshot(store) == before


def test_payment_intent_cannot_be_reused_for_another_invoice(store):
    order = bound(store)
    paid(store, order)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch, match="payment intent"):
        paid(store, order, invoice_id="in_other", event_id="evt_other")
    assert snapshot(store) == before


def test_invoice_and_hold_event_identities_cannot_cross_categories(store):
    order = bound(store)
    paid(store, order)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch, match="paid invoice"):
        store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_synthetic",
            event_type="charge.refunded", body_sha256="a" * 64, now=NOW)
    assert snapshot(store) == before
    store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_hold",
        event_type="charge.refunded", body_sha256="a" * 64, now=NOW)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch, match="payment hold"):
        paid(store, order, invoice_id="in_second", payment_intent_id="pi_second", event_id="evt_hold")
    assert snapshot(store) == before


def test_access_uses_containing_paid_interval_including_gaps_and_normal_delivery(store):
    order = bound(store)
    entitlement = paid(store, order, period_start_at=NOW + 100, period_end_at=NOW + 200)
    claim = entitlement["claim_id"]
    def access(at):
        return store.slot_subscription_access("alice", order["slot_id"], claim_id=claim, now=at)
    assert access(NOW) == {"allowed": False, "paid_through": 0.0}
    assert access(NOW + 100) == {"allowed": True, "paid_through": NOW + 200}
    assert access(NOW + 200) == {"allowed": False, "paid_through": 0.0}
    paid(store, order, invoice_id="in_next", payment_intent_id="pi_next", event_id="evt_next",
         period_start_at=NOW + 300, period_end_at=NOW + 400)
    assert access(NOW + 250)["allowed"] is False
    assert access(NOW + 300) == {"allowed": True, "paid_through": NOW + 400}
    deliver(store, order["slot_id"])
    assert store.get_slot(order["slot_id"])["gateway_generation"] != entitlement["claim_generation"]
    assert access(NOW + 300)["allowed"] is True


def test_access_joins_current_claim_period_and_hold_in_one_read_snapshot(store):
    order = bound(store)
    entitlement = paid(store, order)
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        assert store.slot_subscription_access("alice", order["slot_id"],
            claim_id=entitlement["claim_id"], now=NOW)["allowed"] is True
    finally:
        store._conn.set_trace_callback(None)
    assert len(statements) == 1
    assert statements[0].lstrip().startswith("WITH managed")


def test_released_or_reassigned_claim_cannot_gain_new_invoice_period(store):
    order = bound(store)
    first = paid(store, order)
    store.update_slot_subscription_state(order["id"], subscription_id="sub_synthetic",
        status="canceled", cancel_at_period_end=False, now=NOW + 1001)
    store.begin_release(order["slot_id"], held_by="alice", now=NOW + 1001)
    before = snapshot(store)
    with pytest.raises(StoreError):
        paid(store, order, invoice_id="in_late", payment_intent_id="pi_late", event_id="evt_late")
    assert snapshot(store) == before
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=NOW + 1002)
    assert store.get_slot(order["slot_id"])["gateway_claim_id"] != first["claim_id"]
    before = snapshot(store)
    with pytest.raises(StoreError):
        paid(store, order, invoice_id="in_late", payment_intent_id="pi_late", event_id="evt_late")
    assert snapshot(store) == before
    assert paid(store, order) == first


@pytest.mark.parametrize("operation", ["user-release", "admin-takeback", "move", "slot-delete",
                                       "node-delete", "slot-rename", "node-rename"])
def test_active_subscription_blocks_all_release_and_resource_identity_doors(store, operation):
    order = bound(store)
    paid(store, order)
    before = snapshot(store)
    operations = {
        "user-release": lambda: store.begin_release(order["slot_id"], held_by="alice", now=NOW),
        "admin-takeback": lambda: store.begin_release(order["slot_id"], now=NOW),
        "move": lambda: store.move_slot(order["slot_id"], slots.RELEASING, now=NOW),
        "slot-delete": lambda: store.remove_slot(order["slot_id"]),
        "node-delete": lambda: store.remove_node("node-a"),
        "slot-rename": lambda: store.rename_slot(order["slot_id"], "renamed-slot"),
        "node-rename": lambda: store.rename_node("node-a", "renamed-node"),
    }
    with pytest.raises(SubscriptionReleaseBlocked,
                       match="Cancel the monthly subscription first; keep this slot"):
        operations[operation]()
    assert snapshot(store) == before


def test_unknown_binding_stays_blocking_even_with_expired_paid_time(store):
    order = bound(store)
    paid(store, order)
    store._conn.execute("UPDATE slot_subscription_orders SET status='unknown',provider_status='canceled'")
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(StoreError, match="Cancel the monthly subscription first"):
        store.begin_release(order["slot_id"], now=NOW + 1001)
    assert snapshot(store) == before


def test_cancel_at_period_end_does_not_allow_release_or_wipe_until_verified_terminal_and_end(store):
    order = bound(store)
    first = paid(store, order)
    original = store.get_slot(order["slot_id"])
    store.update_slot_subscription_state(order["id"], subscription_id="sub_synthetic",
        status="active", cancel_at_period_end=True, now=NOW)
    with pytest.raises(StoreError):
        store.begin_release(order["slot_id"], now=NOW + 2000)
    store.update_slot_subscription_state(order["id"], subscription_id="sub_synthetic",
        status="canceled", cancel_at_period_end=False, now=NOW + 10)
    with pytest.raises(StoreError):
        store.begin_release(order["slot_id"], now=NOW + 999)
    assert store.get_slot(order["slot_id"]) == original
    assert store.slot_subscription_access("alice", order["slot_id"], claim_id=first["claim_id"],
                                          now=NOW + 999)["allowed"]
    assert store.begin_release(order["slot_id"], now=NOW + 1000)
    assert store.get_slot_subscription(order["id"])["status"] == "active"
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=NOW + 1001)
    assert store.get_slot_subscription(order["id"])["status"] == "closed"
    store.insert_heartbeat("node-a", NOW + 1001, {"node_id": "node-a", "mode": "machine"})
    assert store.get_slot(order["slot_id"])["held_by"] is None
    grant(store, "bob", "10", key="grant-bob-after-release")
    second = buy(store, "bob", now=NOW + 1001)
    assert second["claim_id"] != first["claim_id"]
    before = snapshot(store)
    with pytest.raises(StoreError):
        paid(store, order, invoice_id="in_late_other_claim", payment_intent_id="pi_late_other_claim",
             event_id="evt_late_other_claim", now=NOW + 1002)
    assert snapshot(store) == before
    for operation in (lambda: store.rename_slot(order["slot_id"], "renamed-slot"),
                      lambda: store.rename_node("node-a", "renamed-node"),
                      lambda: store.remove_slot(order["slot_id"])):
        with pytest.raises(StoreError, match="Paid history keeps"):
            operation()


def test_provisioning_timeout_never_wipes_a_nonterminal_subscription(store):
    order = bound(store)
    paid(store, order)
    original_slot = store.get_slot(order["slot_id"])
    assert store.expire_claims(older_than=NOW + 3600) == []
    assert store.get_slot(order["slot_id"]) == original_slot
    current = store.get_slot_subscription(order["id"])
    assert current["cancel_required"] is True and current["payment_review_required"] is True
    assert store._conn.execute("SELECT COUNT(*) FROM slot_subscription_holds").fetchone()[0] == 1
    store.confirm_slot_subscription_cancellation(order["id"], proof=cancellation_proof(current), now=NOW + 1)
    before = snapshot(store)
    assert store.expire_claims(older_than=NOW + 3700) == []
    assert snapshot(store) == before


def test_machine_provision_failure_never_auto_wipes_a_subscription_contract(store):
    order = bound(store)
    paid(store, order)
    current = store.get_slot(order["slot_id"])
    assert store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": True,
        "provision_failed_for": current["claimed_at"]}], now=NOW + 1) == []
    assert store.get_slot(order["slot_id"])["state"] == slots.CLAIMING
    current_order = store.get_slot_subscription(order["id"])
    assert current_order["payment_review_required"] is True and current_order["cancel_required"] is True
    store.update_slot_subscription_state(order["id"], subscription_id="sub_synthetic",
        status="canceled", cancel_at_period_end=False, now=NOW + 1001)
    assert store.expire_claims(older_than=NOW + 3600) == []
    assert store.get_slot(order["slot_id"])["state"] == slots.CLAIMING


@pytest.mark.parametrize("fault", ["disabled", "stale", "offline", "reserved-other", "present"])
def test_verified_paid_invoice_with_unavailable_slot_is_retained_for_review_and_cancellation(store, fault):
    order = bound(store)
    at = NOW
    if fault == "disabled":
        store.set_enabled("node-a", False)
    elif fault == "stale":
        at = NOW + 91
    elif fault == "offline":
        store._conn.execute("DELETE FROM heartbeats")
    elif fault == "reserved-other":
        store._conn.execute("UPDATE nodes SET reserved_for='bob' WHERE id='node-a'")
    else:
        store._conn.execute("UPDATE slots SET present=1 WHERE id='slot-node-a'")
    store._conn.commit()
    original_slot = store.get_slot(order["slot_id"])
    original_finance = financial_snapshot(store)
    receipt = paid(store, order, now=at)
    assert receipt["fulfillment_state"] == "review_required"
    assert receipt["claim_id"] == "" and receipt["claim_generation"] == 0
    assert receipt["amount_cents"] == 1000 and receipt["payment_intent_id"] == "pi_synthetic_subscription"
    assert receipt["period_start_at"] == NOW - 100 and receipt["period_end_at"] == NOW + 1000
    assert store.get_slot(order["slot_id"]) == original_slot
    current = store.get_slot_subscription(order["id"])
    assert current["claim_id"] == "" and current["payment_review_required"] and current["cancel_required"]
    assert store.slot_subscription_for_payment(receipt["payment_intent_id"])["id"] == order["id"]
    assert financial_snapshot(store) == original_finance
    before = snapshot(store)
    assert paid(store, order, now=at + 1) == receipt
    assert snapshot(store) == before


def test_partial_expected_claim_failure_rolls_back_only_claim_then_keeps_exact_paid_receipt(store, monkeypatch):
    order = bound(store)
    original = store.get_slot(order["slot_id"])
    claim = store._claim_slot_in_transaction
    def partially_claim_then_fail(*args, **kwargs):
        claim(*args, **kwargs)
        raise NoSlotAvailable("synthetic expected delivery failure")
    monkeypatch.setattr(store, "_claim_slot_in_transaction", partially_claim_then_fail)
    receipt = paid(store, order)
    assert receipt["fulfillment_state"] == "review_required"
    assert store.get_slot(order["slot_id"]) == original
    assert store.get_slot_subscription(order["id"])["claim_id"] == ""
    monkeypatch.setattr(store, "_claim_slot_in_transaction", claim)
    before = snapshot(store)
    assert paid(store, order) == receipt
    assert snapshot(store) == before


def test_programming_or_sqlite_errors_are_not_silently_converted_to_delivery_review(store, monkeypatch):
    order = bound(store)
    before = snapshot(store)
    def unexpected_failure(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic programming failure")
    monkeypatch.setattr(store, "_claim_slot_in_transaction", unexpected_failure)
    with pytest.raises(sqlite3.OperationalError):
        paid(store, order)
    assert snapshot(store) == before


def test_reconciliation_cursor_reads_all_nonclosed_known_sessions_without_mutation(store):
    order = reserve(store)
    assert store.list_slot_subscription_reconciliation() == []
    store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
        expires_at=NOW + 1800, now=NOW)
    before = snapshot(store)
    assert [row["id"] for row in store.list_slot_subscription_reconciliation()] == [order["id"]]
    assert snapshot(store) == before
    order = bound(store)
    paid(store, order)
    second = second_bound_order(store)
    identifiers = sorted((order["id"], second["id"]))
    before = snapshot(store)
    [first] = store.list_slot_subscription_reconciliation(limit=1)
    [last] = store.list_slot_subscription_reconciliation(limit=1, after_order_id=first["id"])
    assert [first["id"], last["id"]] == identifiers
    assert store.list_slot_subscription_reconciliation(after_order_id=last["id"]) == []
    assert snapshot(store) == before


def test_unknown_session_recovery_requires_an_already_bound_durable_customer(store):
    order = reserve(store)
    store.mark_slot_subscription_unknown(order["id"], now=NOW)
    assert store.list_slot_subscription_reconciliation() == []
    customer(store)
    before = snapshot(store)
    [pending] = store.list_slot_subscription_reconciliation()
    assert pending["id"] == order["id"] and pending["session_id"] is None
    assert snapshot(store) == before


def test_bound_or_paid_subscription_cannot_be_closed_with_checkout_expiry(store):
    order = bound(store)
    for already_paid in (False, True):
        if already_paid:
            paid(store, order)
        before = snapshot(store)
        with pytest.raises(StoreError):
            store.close_slot_subscription(order["id"], proof=unpaid_expired(order), now=NOW + 2000)
        assert snapshot(store) == before


def test_cancellation_state_never_changes_paid_period_or_wipes_slot(store):
    order = bound(store)
    first = paid(store, order)
    original_slot = store.get_slot(order["slot_id"])
    current = store.update_slot_subscription_state(order["id"], subscription_id="sub_synthetic",
        status="active", cancel_at_period_end=True, now=NOW + 1)
    assert current["cancel_at_period_end"] == 1
    assert store.get_slot(order["slot_id"]) == original_slot
    assert store.slot_subscription_access("alice", order["slot_id"], claim_id=first["claim_id"],
                                          now=NOW + 2)["allowed"] is True


def test_payment_hold_pauses_access_and_only_actual_admin_can_resolve(store):
    order = bound(store)
    first = paid(store, order)
    hold = store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_refund",
        event_type="charge.refunded", body_sha256="b" * 64, now=NOW + 1)
    assert store.get_slot_subscription(order["id"])["payment_review_required"] is True
    assert store.list_slot_subscriptions("alice")[0]["payment_review_required"] is True
    assert store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_refund",
        event_type="charge.refunded", body_sha256="b" * 64, now=NOW + 2) == hold
    assert not store.slot_subscription_access("alice", order["slot_id"], claim_id=first["claim_id"], now=NOW + 2)["allowed"]
    values = dict(reason="Reviewed synthetic refund", idempotency_key="resolution", now=NOW + 3)
    before = snapshot(store)
    with pytest.raises(StoreError):
        store.resolve_slot_subscription_hold(hold["id"], actor_id="alice", **values)
    assert snapshot(store) == before
    resolution = store.resolve_slot_subscription_hold(hold["id"], actor_id="admin", **values)
    assert store.get_slot_subscription(order["id"])["payment_review_required"] is False
    assert store.resolve_slot_subscription_hold(hold["id"], actor_id="admin", **values) == resolution
    assert store.slot_subscription_access("alice", order["slot_id"], claim_id=first["claim_id"], now=NOW + 4)["allowed"]
    assert store.wallet_balance("alice") == 0


def test_owned_cancellation_is_durable_idempotent_and_not_a_spend_operation(store):
    order = bound(store)
    paid(store, order)
    store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_review",
        event_type="charge.refunded", body_sha256="c" * 64, now=NOW)
    before = financial_snapshot(store)
    operation = store.prepare_slot_subscription_cancellation("alice", order["id"],
        idempotency_key="cancel-one", now=NOW)
    assert operation["account_id"] == "alice" and operation["subscription_id"] == "sub_synthetic"
    assert operation["customer_id"] == "cus_synthetic" and operation["completed_at"] is None
    assert store.get_slot_subscription(order["id"])["cancel_required"] is True
    unchanged = snapshot(store)
    assert store.prepare_slot_subscription_cancellation("alice", order["id"],
        idempotency_key="cancel-one", now=NOW + 10) == operation
    assert snapshot(store) == unchanged
    assert store.require_slot_subscription_cancellation(order["id"], now=NOW + 20)["cancel_required"]
    assert snapshot(store) == unchanged
    assert financial_snapshot(store) == before
    with pytest.raises(StoreError):
        store.prepare_slot_subscription_cancellation("bob", order["id"],
            idempotency_key="not-owned", now=NOW)


def test_cancellation_confirmation_completes_all_intents_and_identical_rechecks_are_true_noops(store):
    order = bound(store)
    for key in ("cancel-first", "cancel-second"):
        store.prepare_slot_subscription_cancellation("alice", order["id"],
            idempotency_key=key, now=NOW)
    current = store.confirm_slot_subscription_cancellation(order["id"],
        proof=cancellation_proof(order), now=NOW + 1)
    assert current["cancel_required"] is False
    assert store._conn.execute("SELECT COUNT(*) FROM slot_subscription_cancellations "
                               "WHERE completed_at IS NULL").fetchone()[0] == 0
    assert store.pending_slot_subscription_cancellations() == []
    before = snapshot(store)
    assert store.confirm_slot_subscription_cancellation(order["id"],
        proof=cancellation_proof(order), now=NOW + 100) == current
    store.prepare_slot_subscription_cancellation("alice", order["id"],
        idempotency_key="cancel-first", now=NOW + 200)
    assert snapshot(store) == before


@pytest.mark.parametrize("change", [dict(subscription_id="sub_other"), dict(customer_id="cus_other"),
                                   dict(merchant_id="acct_other"), dict(livemode=0),
                                   dict(cancel_at_period_end=1), dict(cancel_at_period_end=False),
                                   dict(status="unknown"), dict(order_id="wrong")])
def test_cancellation_requires_exact_provider_proof_and_keeps_uncertainty_pending(store, change):
    order = bound(store)
    operation = store.prepare_slot_subscription_cancellation("alice", order["id"],
        idempotency_key="cancel-one", now=NOW)
    proof = {**cancellation_proof(order), **change}
    before = snapshot(store)
    with pytest.raises(StoreError):
        store.confirm_slot_subscription_cancellation(order["id"], proof=proof,
            operation_id=operation["id"], now=NOW + 1)
    assert snapshot(store) == before
    assert [row["id"] for row in store.pending_slot_subscription_cancellations()] == [order["id"]]


def test_confirmation_cannot_complete_another_operation_identity(store):
    order = bound(store)
    store.prepare_slot_subscription_cancellation("alice", order["id"],
        idempotency_key="cancel-one", now=NOW)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        store.confirm_slot_subscription_cancellation(order["id"], proof=cancellation_proof(order),
            operation_id="unknown-operation", now=NOW + 1)
    assert snapshot(store) == before


def test_unresolved_hold_remains_monitored_after_confirmed_cancellation_and_reactivation(store):
    order = bound(store)
    paid(store, order)
    hold = store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_review",
        event_type="charge.refunded", body_sha256="c" * 64, now=NOW)
    assert store.get_slot_subscription(order["id"])["cancel_required"] is True
    current = store.confirm_slot_subscription_cancellation(order["id"],
        proof=cancellation_proof(order), now=NOW + 1)
    assert current["cancel_required"] is False
    assert [row["id"] for row in store.pending_slot_subscription_cancellations()] == [order["id"]]
    before = snapshot(store)
    assert store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_review",
        event_type="charge.refunded", body_sha256="c" * 64, now=NOW + 10) == hold
    assert snapshot(store) == before
    store.update_slot_subscription_state(order["id"], subscription_id="sub_synthetic",
        status="active", cancel_at_period_end=False, now=NOW + 20)
    assert store.get_slot_subscription(order["id"])["cancel_required"] is True
    assert store.next_slot_subscription_cancel_attempt(order["id"], now=NOW + 21) == 1
    store.confirm_slot_subscription_cancellation(order["id"], proof=cancellation_proof(order), now=NOW + 22)
    store.resolve_slot_subscription_hold(hold["id"], actor_id="admin", reason="Reviewed synthetic hold",
        idempotency_key="review-resolved", now=NOW + 23)
    assert store.pending_slot_subscription_cancellations() == []


def test_closed_historical_order_with_unresolved_hold_still_requires_payment_processing(store):
    order = bound(store)
    paid(store, order)
    store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_review",
        event_type="charge.refunded", body_sha256="c" * 64, now=NOW)
    proof = {**cancellation_proof(order), "status": "canceled", "cancel_at_period_end": False}
    store.confirm_slot_subscription_cancellation(order["id"], proof=proof, now=NOW + 1000)
    store.begin_release(order["slot_id"], held_by="alice", now=NOW + 1000)
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=NOW + 1001)
    assert store.get_slot_subscription(order["id"])["status"] == "closed"
    assert store.slot_subscription_processing_required()
    assert [row["id"] for row in store.pending_slot_subscription_cancellations()] == [order["id"]]


def test_attempt_versions_never_reuse_prior_success_or_uncertain_attempt_keys(store):
    order = bound(store)
    assert store.next_slot_subscription_cancel_attempt(order["id"], now=NOW) == 1
    assert store.next_slot_subscription_cancel_attempt(order["id"], now=NOW + 1) == 2
    current = store.confirm_slot_subscription_cancellation(order["id"],
        proof=cancellation_proof(order), now=NOW + 2)
    assert current["cancel_required"] is False and current["cancel_attempt_no"] == 2
    assert store.next_slot_subscription_cancel_attempt(order["id"], now=NOW + 3) == 3
    assert store.get_slot_subscription(order["id"])["cancel_required"] is True


def second_bound_order(store):
    machine(store, "node-b")
    order = reserve(store, node="node-b", key="subscription-two")
    store.attach_slot_subscription_session(order["id"], session_id="cs_test_second",
        expires_at=NOW + 1800, now=NOW)
    return store.bind_slot_subscription(order["id"], session_id="cs_test_second",
        subscription_id="sub_second", customer_id="cus_synthetic", now=NOW)


def test_cancellation_nonce_cannot_move_between_owned_orders_and_pending_cursor_is_stable(store):
    first = bound(store)
    paid(store, first)
    second = second_bound_order(store)
    store.prepare_slot_subscription_cancellation("alice", first["id"], idempotency_key="cancel-one", now=NOW)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        store.prepare_slot_subscription_cancellation("alice", second["id"], idempotency_key="cancel-one", now=NOW)
    assert snapshot(store) == before
    store.require_slot_subscription_cancellation(second["id"], now=NOW)
    identifiers = sorted((first["id"], second["id"]))
    [early] = store.pending_slot_subscription_cancellations(limit=1)
    assert early["id"] == identifiers[0]
    [later] = store.pending_slot_subscription_cancellations(limit=1, after_order_id=early["id"])
    assert later["id"] == identifiers[1]
    assert store.pending_slot_subscription_cancellations(after_order_id=later["id"]) == []
    with pytest.raises(WalletReplayMismatch):
        store.confirm_slot_subscription_cancellation(second["id"], proof=cancellation_proof(second),
            operation_id=store._conn.execute("SELECT id FROM slot_subscription_cancellations").fetchone()[0], now=NOW)


@pytest.mark.parametrize("limit", [0, True, -1, 101, 1.0])
def test_pending_cancellation_queue_has_a_bounded_integer_limit(store, limit):
    with pytest.raises(StoreError):
        store.pending_slot_subscription_cancellations(limit=limit)


@pytest.mark.parametrize("state", ["reserved", "unknown", "checkout_open", "bound"])
def test_only_one_unpaid_checkout_per_account_even_with_another_free_machine(store, state):
    machine(store, "node-b")
    order = bound(store) if state == "bound" else reserve(store)
    if state == "unknown":
        store.mark_slot_subscription_unknown(order["id"], now=NOW)
    elif state == "checkout_open":
        store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
            expires_at=NOW + 1800, now=NOW)
    before = snapshot(store)
    with pytest.raises(SubscriptionCheckoutPending):
        reserve(store, node="node-b", key="another-checkout")
    assert snapshot(store) == before
    assert reserve(store)["id"] == order["id"]


def test_paid_plans_do_not_limit_another_monthly_checkout(store):
    first = bound(store)
    paid(store, first)
    second = second_bound_order(store)
    assert second["id"] != first["id"] and second["slot_id"] != first["slot_id"]


def test_customer_identity_is_durable_and_unknown_create_is_not_retried_after_23_hours(store):
    prepared = store.prepare_subscription_customer("alice", merchant_id=MERCHANT, livemode=False, now=NOW)
    assert store.prepare_subscription_customer("alice", merchant_id=MERCHANT, livemode=False, now=NOW + 1) == prepared
    before = snapshot(store)
    with pytest.raises(StoreError, match="unresolved"):
        store.prepare_subscription_customer("alice", merchant_id=MERCHANT, livemode=False, now=NOW + 86400)
    assert snapshot(store) == before
    bound_customer = customer(store)
    assert bound_customer["id"] == prepared["id"]
    assert store.get_subscription_customer("alice", merchant_id=MERCHANT, livemode=False) == bound_customer
    assert store.prepare_subscription_customer("alice", merchant_id=MERCHANT, livemode=False,
                                               now=NOW + 86400) == bound_customer
    store.prepare_subscription_customer("bob", merchant_id=MERCHANT, livemode=False, now=NOW)
    with pytest.raises(StoreError):
        store.bind_subscription_customer("bob", merchant_id=MERCHANT, livemode=False,
                                         customer_id="cus_synthetic", now=NOW)


def test_trusted_provider_lookups_and_display_period_do_not_grant_access(store):
    order = bound(store)
    assert store.slot_subscription_for_session("cs_test_synthetic") == order
    assert store.slot_subscription_for_provider("sub_synthetic") == order
    assert store.slot_subscription_for_invoice("in_synthetic") is None
    assert store.slot_subscription_for_payment("pi_synthetic_subscription") is None
    assert store.slot_subscription_for_session("cs_test_missing") is None
    assert store.slot_subscription_for_provider("sub_missing") is None
    entitlement = paid(store, order, period_start_at=NOW + 100, period_end_at=NOW + 200)
    current = store.get_slot_subscription(order["id"])
    assert current["period_end_at"] == NOW + 200
    assert store.list_slot_subscriptions("alice") == [current]
    assert store.slot_subscription_for_invoice("in_synthetic") == current
    assert store.slot_subscription_for_payment("pi_synthetic_subscription") == current
    assert store.slot_subscription_access("alice", order["slot_id"], claim_id=entitlement["claim_id"],
                                          now=NOW)["allowed"] is False


def test_pending_reconciliation_is_bounded_to_expired_unclaimed_sessions(store):
    order = reserve(store)
    assert store.pending_slot_subscriptions(now=NOW + 2000) == []
    store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
                                            expires_at=NOW + 1800, now=NOW)
    assert store.pending_slot_subscriptions(now=NOW + 1799) == []
    pending = store.pending_slot_subscriptions(now=NOW + 1800, limit=1)
    assert [row["id"] for row in pending] == [order["id"]]
    store.close_slot_subscription(order["id"], proof=unpaid_expired(order), now=NOW + 1801)
    assert store.pending_slot_subscriptions(now=NOW + 2000) == []


def test_processing_gate_is_independent_of_new_enrollment_and_tracks_unknown_contracts(store):
    assert store.slot_subscription_processing_required() is False
    order = reserve(store)
    assert store.slot_subscription_processing_required() is True
    store.mark_slot_subscription_unknown(order["id"], now=NOW + 1)
    assert store.slot_subscription_processing_required() is True
    store.attach_slot_subscription_session(order["id"], session_id="cs_test_synthetic",
                                            expires_at=NOW + 1800, now=NOW)
    store.close_slot_subscription(order["id"], proof=unpaid_expired(order), now=NOW + 1801)
    assert store.slot_subscription_processing_required() is False


@pytest.mark.parametrize("limit", [0, True, -1, 101, 1.0])
def test_subscription_lists_require_bounded_integer_limits(store, limit):
    with pytest.raises(StoreError):
        store.list_slot_subscriptions("alice", limit=limit)
    with pytest.raises(StoreError):
        store.pending_slot_subscriptions(now=NOW, limit=limit)


def database(tmp_path):
    path = str(tmp_path / "subscriptions.db")
    setup = Store(path)
    accounts(setup)
    machine(setup)
    grant(setup, "bob", "10", key="grant-bob")
    setup.set_slot_quota("bob", 1)
    setup.close()
    return path


@pytest.mark.parametrize("competitor", ["subscription", "wallet", "legacy"])
def test_two_connections_cannot_oversell_one_slot_between_purchase_paths(tmp_path, competitor):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    try:
        other = {"subscription": lambda: reserve(b, "bob", key="subscription-bob"),
                 "wallet": lambda: buy(b, "bob"),
                 "legacy": lambda: b.claim_slot("bob", node_id="node-a", now=NOW)}[competitor]
        results, errors = race(lambda: reserve(a), other)
        assert len(results) == len(errors) == 1
        assert isinstance(errors[0], NoSlotAvailable)
        reservations = a._conn.execute("SELECT * FROM slot_subscription_orders WHERE status!='closed'").fetchall()
        held = a.get_slot("slot-node-a")["held_by"]
        assert len(reservations) + int(held is not None) == 1
        if reservations:
            assert a.wallet_balance("bob") == 1000
            assert a.list_slot_purchases("bob") == []
        assert a.wallet_balance("alice") == 0
    finally:
        a.close()
        b.close()


def test_matching_reservation_customer_and_invoice_replays_on_two_connections(tmp_path):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    try:
        orders, errors = race(lambda: reserve(a), lambda: reserve(b))
        assert not errors and orders[0] == orders[1]
        customers, errors = race(
            lambda: a.prepare_subscription_customer("alice", merchant_id=MERCHANT, livemode=False, now=NOW),
            lambda: b.prepare_subscription_customer("alice", merchant_id=MERCHANT, livemode=False, now=NOW))
        assert not errors and customers[0] == customers[1]
        order = bound(a)
        invoices, errors = race(lambda: paid(a, order), lambda: paid(b, order))
        assert not errors and invoices[0] == invoices[1]
        assert a._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0] == 1
        assert a.get_slot(order["slot_id"])["held_by"] == "alice"
        assert a.list_wallet_entries("alice") == []
    finally:
        a.close()
        b.close()


def test_one_unpaid_account_cannot_reserve_two_machines_concurrently(tmp_path):
    path = database(tmp_path)
    setup = Store(path)
    machine(setup, "node-b")
    setup.close()
    a, b = Store(path), Store(path)
    try:
        orders, errors = race(lambda: reserve(a),
                             lambda: reserve(b, node="node-b", key="subscription-two"))
        assert len(orders) == len(errors) == 1
        assert isinstance(errors[0], SubscriptionCheckoutPending)
        assert len(a.list_slot_subscriptions("alice")) == 1
        assert all(slot["state"] == slots.FREE for slot in a.list_slots())
        assert a.list_wallet_entries("alice") == []
    finally:
        a.close()
        b.close()


def test_cancel_operation_and_attempt_versions_are_atomic_across_connections_and_restart(tmp_path):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    try:
        order = bound(a)
        operations, errors = race(
            lambda: a.prepare_slot_subscription_cancellation("alice", order["id"],
                idempotency_key="cancel-one", now=NOW),
            lambda: b.prepare_slot_subscription_cancellation("alice", order["id"],
                idempotency_key="cancel-one", now=NOW))
        assert not errors and operations[0] == operations[1]
        versions, errors = race(
            lambda: a.next_slot_subscription_cancel_attempt(order["id"], now=NOW),
            lambda: b.next_slot_subscription_cancel_attempt(order["id"], now=NOW))
        assert not errors and sorted(versions) == [1, 2]
    finally:
        a.close()
        b.close()
    later = Store(path)
    try:
        assert later.next_slot_subscription_cancel_attempt(order["id"], now=NOW + 10) == 3
        assert later._conn.execute("SELECT COUNT(*) FROM slot_subscription_cancellations").fetchone()[0] == 1
        before = financial_snapshot(later)
        later.confirm_slot_subscription_cancellation(order["id"], proof=cancellation_proof(order), now=NOW + 11)
        assert financial_snapshot(later) == before
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            later._conn.execute("UPDATE slot_subscription_cancellations SET completed_at=NULL")
        later._conn.rollback()
    finally:
        later.close()


def test_duplicate_paid_delivery_failure_on_two_connections_keeps_one_receipt_and_one_hold(tmp_path):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    try:
        order = bound(a)
        a.set_enabled("node-a", False)
        original_slot = a.get_slot(order["slot_id"])
        receipts, errors = race(lambda: paid(a, order), lambda: paid(b, order))
        assert not errors and receipts[0] == receipts[1]
        assert receipts[0]["fulfillment_state"] == "review_required"
        assert a.get_slot(order["slot_id"]) == original_slot
        assert a._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0] == 1
        assert a._conn.execute("SELECT COUNT(*) FROM slot_subscription_holds").fetchone()[0] == 1
        assert a.get_slot_subscription(order["id"])["cancel_required"] is True
        assert a.wallet_balance("bob") == 1000
    finally:
        a.close()
        b.close()


def financial_snapshot(store):
    rows = {table: [tuple(row) for row in store._conn.execute('SELECT * FROM "' + table + '" ORDER BY rowid')]
            for table in FINANCIAL}
    schema = [tuple(row) for row in store._conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN (" + ",".join("?" for _ in FINANCIAL)
        + ") ORDER BY type,name", FINANCIAL)]
    return rows, schema


def test_startup_and_subscription_writes_preserve_nonempty_original_eight_financial_tables(tmp_path):
    path = str(tmp_path / "existing.db")
    setup = Store(path)
    accounts(setup)
    machine(setup)
    machine(setup, "node-b")
    topup = topup_ready(setup)
    topup_settle(setup, topup)
    buy(setup, node="node-b")
    hold = topup_hold(setup)
    setup.resolve_wallet_topup_hold(hold["id"], actor_id="admin", actor_kind="account", reason="Synthetic old history",
                                    idempotency_key="old-resolution", now=NOW)
    before = financial_snapshot(setup)
    assert all(before[0].values())
    setup.close()
    later = Store(path)
    try:
        order = bound(later)
        paid(later, order)
        assert financial_snapshot(later) == before
        for table in ("slot_subscription_invoices", "slot_subscription_events"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                later._conn.execute('UPDATE "' + table + '" SET created_at=created_at+1')
            later._conn.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                later._conn.execute('DELETE FROM "' + table + '"')
            later._conn.rollback()
        assert financial_snapshot(later) == before
    finally:
        later.close()
