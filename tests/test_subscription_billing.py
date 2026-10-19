"""Actual recurring coordinator, raw signatures and Store; synthetic provider only."""

from __future__ import annotations

import socket
import subprocess
from types import SimpleNamespace

import pytest

from ccfleetd import slots, stripe_checkout, stripe_subscriptions, subscription_config
from ccfleetd.config import ConfigError
from ccfleetd.subscription_billing import Billing, SubscriptionError
from tests.test_slot_subscriptions import reserve
from tests.test_slot_subscriptions import store as store
from tests.test_stripe_billing import settings, signed
from tests.test_stripe_config import config as stripe_config_fixture
from tests.test_wallet_store import NOW, snapshot


@pytest.fixture(autouse=True)
def no_real_transports(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Coordinator tests must use only synthetic provider objects")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


class Client:
    def __init__(self):
        self.settings = settings()
        self.calls = []
        self.orders = {}
        self.fail_create = False
        self.fail_expiry = False
        self.fail_cancel = False
        self.cancel_applied_then_timeout = False
        self.before_cancel = None
        self.receipt_changes = {}
        self.invoice_receipts = {}
        self.status = "active"
        self.cancel_at_period_end = False
        self.session_status = "complete"
        self.session_payment_status = "paid"
        self.latest_invoice = "in_synthetic"
        self.fail_recover = False

    def validate_portal_configuration(self, identifier):
        self.calls.append(("portal-config", identifier))

    def create_customer(self, account_id, identity):
        self.calls.append(("create-customer", account_id, identity))
        return "cus_synthetic"

    def retrieve_customer(self, identifier, account_id):
        self.calls.append(("customer", identifier, account_id))
        return identifier

    def create_subscription_session(self, order, customer_id):
        self.calls.append(("create-session", order.id, customer_id))
        self.orders[order.id] = order
        if self.fail_create:
            raise stripe_checkout.StripeCheckoutError("SENSITIVE_PROVIDER_REPLY", uncertain=True)
        return stripe_checkout.HostedSession("cs_test_synthetic", "https://checkout.stripe.com/c/pay/synthetic")

    def retrieve_open_session(self, identifier, order, customer_id):
        self.calls.append(("open-session", identifier, order.id, customer_id))
        return stripe_checkout.HostedSession(identifier, "https://checkout.stripe.com/c/pay/synthetic")

    def recover_subscription_session(self, order, customer_id):
        self.calls.append(("recover-session", order.id, customer_id, order.expires_at))
        if self.fail_recover:
            raise stripe_checkout.StripeCheckoutError("SENSITIVE_RECOVERY_REPLY", uncertain=True)
        return self.retrieve_session("cs_test_synthetic", order, customer_id)

    def retrieve_session(self, identifier, order, customer_id):
        self.calls.append(("session", identifier, order.id, customer_id))
        return {"id": identifier, "status": self.session_status,
                "payment_status": self.session_payment_status, "subscription": "sub_synthetic",
                "invoice": "in_synthetic"}

    def retrieve_subscription(self, identifier, order, customer_id):
        self.calls.append(("subscription", identifier, order.id, customer_id))
        return {"id": identifier, "status": self.status, "cancel_at_period_end": self.cancel_at_period_end,
                "latest_invoice": self.latest_invoice}

    def subscription_reference(self, identifier):
        self.calls.append(("reference", identifier))
        if identifier != "sub_synthetic":
            return None
        return {"metadata": {"ccfleet_order_id": next(iter(self.orders))}}

    def retrieve_paid_invoice(self, identifier, subscription_id, customer_id, order):
        self.calls.append(("paid-invoice", identifier, subscription_id, customer_id))
        return {"order_id": order.id, "account_id": order.account_id, "slot_id": order.slot_id,
                "invoice_id": identifier, "subscription_id": subscription_id, "customer_id": customer_id,
                "merchant_id": self.settings.expected_account_id, "livemode": self.settings.livemode,
                "currency": "USD", "amount_cents": order.amount_cents, "period_start_at": NOW - 100,
                "period_end_at": NOW + 1000, "paid": True, "payment_intent_id": "pi_subscription",
                **self.receipt_changes, **self.invoice_receipts.get(identifier, {})}

    def invoice_subscription(self, identifier):
        self.calls.append(("invoice-reference", identifier))
        return "sub_synthetic" if identifier.startswith("in_") else None

    def expire_unpaid_session(self, identifier, order, customer_id):
        self.calls.append(("expire", identifier, order.id, customer_id))
        if self.fail_expiry:
            raise stripe_checkout.StripeCheckoutError("SENSITIVE_PROVIDER_REPLY")
        return {"order_id": order.id, "session_id": identifier, "session_status": "expired",
                "payment_status": "unpaid", "subscription_id": None,
                "merchant_id": self.settings.expected_account_id, "livemode": self.settings.livemode}

    def review_subscription(self, kind, value):
        self.calls.append(("review", kind))
        return value.get("subscription")

    def create_portal(self, customer_id, account_id, configuration_id):
        self.calls.append(("portal", customer_id, account_id, configuration_id))
        return "https://billing.stripe.com/p/session/synthetic"

    def bound_subscription_identity(self, subscription_id, customer_id):
        self.calls.append(("cancel-read", subscription_id, customer_id))
        return {"id": subscription_id, "status": self.status,
                "cancel_at_period_end": self.cancel_at_period_end}

    def cancel_after_paid_period(self, subscription_id, customer_id, *, operation_key):
        self.calls.append(("cancel-update", subscription_id, customer_id, operation_key))
        if self.before_cancel is not None:
            self.before_cancel()
        if self.fail_cancel:
            raise stripe_checkout.StripeCheckoutError("SENSITIVE_CANCEL_REPLY", uncertain=True)
        self.cancel_at_period_end = True
        if self.cancel_applied_then_timeout:
            raise stripe_checkout.StripeCheckoutError("SENSITIVE_CANCEL_REPLY", uncertain=True)
        return {"subscription_id": subscription_id, "customer_id": customer_id,
                "merchant_id": self.settings.expected_account_id, "livemode": self.settings.livemode,
                "status": self.status, "cancel_at_period_end": True}


@pytest.fixture
def billing(store):
    return Billing(store, Client(), sandbox_database=True, portal_configuration_id="bpc_synthetic")


def start(billing, **changes):
    values = dict(expected_cents=1000, monthly_consent=True, idempotency_key="monthly-one",
                  now=NOW, max_age_s=90)
    values.update(changes)
    return billing.start_checkout("alice", "node-a", **values)


def handle(billing, value, *, kind="checkout.session.completed", identity="evt_monthly", **changes):
    body, signature = signed(billing, value, kind=kind, identity=identity, **changes)
    return billing.handle_webhook(body, signature, now=NOW)


def test_constructor_and_default_customer_portal_do_not_create_provider_objects(store):
    client = Client()
    before = snapshot(store)
    with pytest.raises(SubscriptionError):
        Billing(store, client, portal_configuration_id="bpc_synthetic")
    billing = Billing(store, client, sandbox_database=True, portal_configuration_id="bpc_synthetic")
    with pytest.raises(SubscriptionError):
        billing.portal("alice")
    assert client.calls == [] and snapshot(store) == before


def test_durable_exact_reservation_precedes_provider_checkout_and_reuses_one_session(billing):
    original = billing.client.create_subscription_session

    def create(order, customer_id):
        assert not billing.store._conn.in_transaction
        reserved = billing.store.get_slot_subscription(order.id, account_id="alice")
        assert reserved["status"] == "unknown" and reserved["slot_id"] == order.slot_id
        return original(order, customer_id)

    billing.client.create_subscription_session = create
    first = start(billing)
    assert start(billing, now=NOW + 86400) == first
    assert len([call for call in billing.client.calls if call[0] == "create-session"]) == 1
    assert billing.store.get_slot("slot-node-a")["state"] == slots.FREE
    assert billing.store.wallet_balance("alice") == 0


def test_checkout_requires_explicit_consent_before_provider_or_local_order(billing):
    before = snapshot(billing.store)
    with pytest.raises(SubscriptionError) as error:
        start(billing, monthly_consent=False)
    assert error.value.status_code == 400
    assert billing.client.calls == [] and snapshot(billing.store) == before


def test_unknown_checkout_create_keeps_same_identity_and_safe_retry_expiry(billing):
    billing.client.fail_create = True
    with pytest.raises(SubscriptionError) as error:
        start(billing)
    assert "SENSITIVE" not in str(error.value)
    [order] = billing.store.list_slot_subscriptions("alice")
    assert order["status"] == "unknown" and order["session_id"] is None
    billing.client.fail_create = False
    start(billing, now=NOW + 10)
    creates = [call for call in billing.client.calls if call[0] == "create-session"]
    assert creates[0] == creates[1]


def test_unknown_checkout_only_recovers_original_request_after_fresh_create_window(billing):
    billing.client.fail_create = True
    with pytest.raises(SubscriptionError):
        start(billing)
    for at in (NOW - 1, NOW + 86400):
        with pytest.raises(SubscriptionError):
            start(billing, now=at)
    assert len([call for call in billing.client.calls if call[0] == "create-session"]) == 1
    with pytest.raises(SubscriptionError) as error:
        start(billing, now=NOW + 1800)
    assert error.value.code == "processing-required"
    [order] = billing.store.list_slot_subscriptions("alice")
    assert order["session_id"] == "cs_test_synthetic"
    [recovery] = [call for call in billing.client.calls if call[0] == "recover-session"]
    assert recovery[1] == order["id"] and recovery[-1] == NOW + 3600
    assert len([call for call in billing.client.calls if call[0] == "create-session"]) == 1


def test_signed_payment_and_duplicate_invoice_grant_one_provider_period_only(billing):
    session = start(billing)
    assert handle(billing, {"id": session.id}) == "paid"
    assert handle(billing, {"id": session.id}) == "paid"
    assert handle(billing, {"id": "in_synthetic"}, kind="invoice.paid", identity="evt_invoice") == "paid"
    [order] = billing.store.list_slot_subscriptions("alice")
    assert order["period_end_at"] == NOW + 1000
    assert billing.store._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0] == 1
    assert billing.store.wallet_balance("alice") == 0
    assert billing.store.list_wallet_entries("alice") == billing.store.list_slot_purchases("alice") == []
    assert billing.portal("alice").startswith("https://billing.stripe.com/")


@pytest.mark.parametrize("change", [dict(paid=False), dict(amount_cents=999), dict(customer_id="cus_other"),
                                   dict(merchant_id="acct_other"), dict(livemode=True),
                                   dict(slot_id="another-slot"), dict(account_id="bob")])
def test_mismatched_retrieved_invoice_cannot_claim_or_entitle(billing, change):
    session = start(billing)
    billing.client.receipt_changes = change
    with pytest.raises(SubscriptionError):
        handle(billing, {"id": session.id})
    assert billing.store.get_slot("slot-node-a")["state"] == slots.FREE
    assert billing.store._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0] == 0


@pytest.mark.parametrize("change", [dict(livemode=True), dict(account="acct_other")])
def test_wrong_signed_mode_or_merchant_is_rejected_before_provider_reads(billing, change):
    body, signature = signed(billing, {}, **change)
    before = snapshot(billing.store)
    with pytest.raises(SubscriptionError) as error:
        billing.handle_webhook(body, signature, now=NOW)
    assert error.value.status_code == 400
    assert billing.client.calls == [] and snapshot(billing.store) == before


def test_raw_signature_tampering_does_not_read_provider_or_write_store(billing):
    body, signature = signed(billing, {})
    before = snapshot(billing.store)
    with pytest.raises(SubscriptionError) as error:
        billing.handle_webhook(body + b" ", signature, now=NOW)
    assert error.value.status_code == 400
    assert billing.client.calls == [] and snapshot(billing.store) == before


def test_failed_invoice_and_cancel_at_period_end_keep_existing_paid_time_and_files(billing):
    session = start(billing)
    handle(billing, {"id": session.id})
    original_slot = billing.store.get_slot("slot-node-a")
    billing.client.status = "past_due"
    assert handle(billing, {"id": "in_failed"}, kind="invoice.payment_failed", identity="evt_failed") == "updated"
    billing.client.status, billing.client.cancel_at_period_end = "active", True
    assert handle(billing, {"id": "sub_synthetic"}, kind="customer.subscription.updated", identity="evt_cancel") == "updated"
    [order] = billing.store.list_slot_subscriptions("alice")
    assert order["period_end_at"] == NOW + 1000 and order["cancel_at_period_end"] == 1
    assert billing.store.get_slot("slot-node-a") == original_slot


def test_refund_pauses_subscription_access_without_wallet_credit_or_automatic_wipe(billing):
    session = start(billing)
    handle(billing, {"id": session.id})
    original_slot = billing.store.get_slot("slot-node-a")
    assert handle(billing, {"id": "ch_synthetic", "subscription": "sub_synthetic"},
                  kind="charge.refunded", identity="evt_refund") == "held"
    assert not billing.store.slot_subscription_access("alice", "slot-node-a",
        claim_id=original_slot["gateway_claim_id"], now=NOW)["allowed"]
    assert billing.store.get_slot("slot-node-a") == original_slot
    assert billing.store.wallet_balance("alice") == 0


def test_hold_cancels_future_renewals_before_return_and_duplicate_replay_does_not_rewrite_rows(billing):
    session = start(billing)
    handle(billing, {"id": session.id})
    [order] = billing.store.list_slot_subscriptions("alice")
    def before_provider_write():
        assert not billing.store._conn.in_transaction
        assert billing.store.get_slot_subscription(order["id"])["cancel_required"] is True
    billing.client.before_cancel = before_provider_write
    event = {"id": "ch_synthetic", "subscription": "sub_synthetic"}
    body, signature = signed(billing, event, kind="charge.refunded", identity="evt_hold")
    assert billing.handle_webhook(body, signature, now=NOW) == "held"
    current = billing.store.get_slot_subscription(order["id"])
    assert current["cancel_required"] is False and current["cancel_at_period_end"] == 1
    before = snapshot(billing.store)
    assert billing.handle_webhook(body, signature, now=NOW + 10) == "held"
    assert snapshot(billing.store) == before
    assert len([call for call in billing.client.calls if call[0] == "cancel-update"]) == 1


@pytest.mark.parametrize("applied_before_timeout", [False, True])
def test_uncertain_hold_cancellation_stays_pending_and_retries_from_fresh_provider_state(billing, applied_before_timeout):
    session = start(billing)
    handle(billing, {"id": session.id})
    billing.client.fail_cancel = not applied_before_timeout
    billing.client.cancel_applied_then_timeout = applied_before_timeout
    with pytest.raises(SubscriptionError) as error:
        handle(billing, {"id": "ch_synthetic", "subscription": "sub_synthetic"},
            kind="charge.refunded", identity="evt_hold")
    assert "SENSITIVE" not in str(error.value)
    [order] = billing.store.list_slot_subscriptions("alice")
    assert order["cancel_required"] is True and order["cancel_attempt_no"] == 1
    billing.client.fail_cancel = billing.client.cancel_applied_then_timeout = False
    billing.reconcile_pending(now=NOW + 10)
    current = billing.store.get_slot_subscription(order["id"])
    assert current["cancel_required"] is False
    updates = [call for call in billing.client.calls if call[0] == "cancel-update"]
    assert len(updates) == (1 if applied_before_timeout else 2)
    assert len({call[-1] for call in updates}) == len(updates)
    before = snapshot(billing.store)
    billing.reconcile_pending(now=NOW + 20)
    assert snapshot(billing.store) == before


def test_reactivation_while_held_gets_a_new_durable_cancel_attempt_and_keeps_access_paused(billing):
    session = start(billing)
    handle(billing, {"id": session.id})
    handle(billing, {"id": "ch_synthetic", "subscription": "sub_synthetic"},
        kind="charge.refunded", identity="evt_hold")
    billing.client.cancel_at_period_end = False
    assert handle(billing, {"id": "sub_synthetic"}, kind="customer.subscription.updated",
                  identity="evt_reactivated") == "updated"
    [order] = billing.store.list_slot_subscriptions("alice")
    assert order["cancel_attempt_no"] == 2 and order["cancel_required"] is False
    updates = [call for call in billing.client.calls if call[0] == "cancel-update"]
    assert len(updates) == 2 and updates[0][-1] != updates[1][-1]
    assert not billing.store.slot_subscription_access("alice", order["slot_id"],
        claim_id=order["claim_id"], now=NOW)["allowed"]


def test_own_cancellation_persists_operation_before_provider_and_foreign_owner_is_inert(billing):
    session = start(billing)
    handle(billing, {"id": session.id})
    [order] = billing.store.list_slot_subscriptions("alice")
    before = snapshot(billing.store)
    calls = list(billing.client.calls)
    with pytest.raises(SubscriptionError):
        billing.cancel("bob", order["id"], idempotency_key="not-owned", now=NOW)
    assert snapshot(billing.store) == before and billing.client.calls == calls
    def before_provider_write():
        assert billing.store.get_slot_subscription(order["id"])["cancel_required"] is True
        assert billing.store._conn.execute("SELECT COUNT(*) FROM slot_subscription_cancellations").fetchone()[0] == 1
    billing.client.before_cancel = before_provider_write
    assert billing.cancel("alice", order["id"], idempotency_key="own-cancel", now=NOW)
    before = snapshot(billing.store)
    assert billing.cancel("alice", order["id"], idempotency_key="own-cancel", now=NOW + 10)
    assert snapshot(billing.store) == before
    billing.client.cancel_at_period_end = False
    assert billing.cancel("alice", order["id"], idempotency_key="own-cancel", now=NOW + 20)
    assert billing.store.get_slot_subscription(order["id"])["cancel_attempt_no"] == 2


def test_own_cancellation_timeout_is_honestly_pending_then_monitor_completes_operation(billing):
    session = start(billing)
    handle(billing, {"id": session.id})
    [order] = billing.store.list_slot_subscriptions("alice")
    billing.client.fail_cancel = True
    assert billing.cancel("alice", order["id"], idempotency_key="own-cancel", now=NOW) is False
    assert billing.store.get_slot_subscription(order["id"])["cancel_required"] is True
    billing.client.fail_cancel = False
    billing.reconcile_pending(now=NOW + 10)
    assert billing.store._conn.execute("SELECT COUNT(*) FROM slot_subscription_cancellations "
                                      "WHERE completed_at IS NULL").fetchone()[0] == 0


def test_enrollment_off_existing_held_contract_still_builds_cancellation_service(billing, monkeypatch):
    session = start(billing)
    handle(billing, {"id": session.id})
    [order] = billing.store.list_slot_subscriptions("alice")
    billing.store.hold_slot_subscription(order["id"], subscription_id="sub_synthetic", event_id="evt_hold",
        event_type="charge.refunded", body_sha256="c" * 64, now=NOW)
    config = stripe_config_fixture(stripe_subscriptions_enabled=False, broker_only=False,
        stripe_expected_account_id="acct_synthetic", stripe_subscription_webhook_secret_file="/not-read/signing",
        stripe_subscription_portal_configuration_id="bpc_synthetic")
    def synthetic_secret(path):
        return "rk_test_SYNTHETIC_ONLY" if path == config.stripe_api_key_file else "whsec_SYNTHETIC_ONLY"
    def client_factory(settings):
        billing.client.settings = settings
        return billing.client
    monkeypatch.setattr(subscription_config.stripe_config, "_secret", synthetic_secret)
    monkeypatch.setattr(stripe_subscriptions, "Client", client_factory)
    service = subscription_config.build_billing(config, billing.store)
    assert service is not None
    assert service.cancel("alice", order["id"], idempotency_key="held-off-cancel", now=NOW)
    assert billing.store.get_slot_subscription(order["id"])["cancel_required"] is False


def test_reconciliation_needs_provider_expired_unpaid_proof_and_preserves_unknown(billing):
    start(billing)
    billing.client.session_status, billing.client.session_payment_status = "open", "unpaid"
    billing.client.fail_expiry = True
    assert billing.reconcile_pending(now=NOW + 3601) == 0
    assert billing.store.slot_subscription_processing_required()
    billing.client.fail_expiry = False
    assert billing.reconcile_pending(now=NOW + 3601) == 1
    assert not billing.store.slot_subscription_processing_required()
    assert billing.store.get_slot("slot-node-a")["state"] == slots.FREE


def test_server_reconciliation_recovers_lost_first_paid_webhook_once_without_wallet_credit(billing):
    start(billing)
    [order] = billing.store.list_slot_subscriptions("alice")
    assert not order["claim_id"]
    billing.reconcile_pending(now=NOW + 10)
    current = billing.store.get_slot_subscription(order["id"])
    assert current["claim_id"] and current["period_end_at"] == NOW + 1000
    assert billing.store.get_slot(current["slot_id"])["held_by"] == "alice"
    assert billing.store.wallet_balance("alice") == 0
    assert billing.store._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0] == 1
    before = snapshot(billing.store)
    billing.reconcile_pending(now=NOW + 20)
    assert snapshot(billing.store) == before


def test_server_reconciliation_recovers_only_verified_latest_renewal_and_repeat_is_noop(billing):
    session = start(billing)
    handle(billing, {"id": session.id})
    original_slot = billing.store.get_slot("slot-node-a")
    billing.client.latest_invoice = "in_renewal"
    billing.client.receipt_changes = {"payment_intent_id": "pi_renewal",
        "period_start_at": NOW + 1000, "period_end_at": NOW + 2000}
    billing.reconcile_pending(now=NOW + 10)
    [order] = billing.store.list_slot_subscriptions("alice")
    assert order["period_end_at"] == NOW + 2000
    assert billing.store.get_slot(order["slot_id"]) == original_slot
    assert billing.store._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0] == 2
    assert billing.store.slot_subscription_access("alice", order["slot_id"], claim_id=order["claim_id"],
        now=NOW + 1500) == {"allowed": True, "paid_through": NOW + 2000}
    before = snapshot(billing.store)
    billing.reconcile_pending(now=NOW + 20)
    assert snapshot(billing.store) == before


def test_original_completed_event_replay_after_renewal_still_targets_original_session_invoice(billing):
    session = start(billing)
    body, signature = signed(billing, {"id": session.id}, identity="evt_original_completed")
    assert billing.handle_webhook(body, signature, now=NOW) == "paid"
    billing.client.latest_invoice = "in_renewal"
    billing.client.invoice_receipts["in_renewal"] = {"payment_intent_id": "pi_renewal",
        "period_start_at": NOW + 1000, "period_end_at": NOW + 2000}
    assert handle(billing, {"id": "in_renewal"}, kind="invoice.paid", identity="evt_renewal") == "paid"
    before = snapshot(billing.store)
    assert billing.handle_webhook(body, signature, now=NOW + 10) == "paid"
    assert snapshot(billing.store) == before
    invoices = billing.store._conn.execute("SELECT invoice_id FROM slot_subscription_invoices ORDER BY invoice_id").fetchall()
    assert [row["invoice_id"] for row in invoices] == ["in_renewal", "in_synthetic"]


@pytest.mark.parametrize("outcome", ["paid", "expired"])
def test_unknown_create_recovers_same_cached_session_after_30_minutes_and_never_changes_order(billing, outcome):
    billing.client.fail_create = True
    with pytest.raises(SubscriptionError):
        start(billing)
    [order] = billing.store.list_slot_subscriptions("alice")
    at = NOW + (1801 if outcome == "paid" else 3601)
    if outcome == "paid":
        billing.client.receipt_changes = {"period_end_at": NOW + 7200}
        billing.store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=at)
        billing.store.insert_heartbeat("node-a", at, {"node_id": "node-a", "mode": "machine"})
    else:
        billing.client.session_status, billing.client.session_payment_status = "expired", "unpaid"
    billing.reconcile_pending(now=at)
    current = billing.store.get_slot_subscription(order["id"])
    assert current["session_id"] == "cs_test_synthetic"
    assert current["id"] == order["id"] and current["expires_at"] == NOW + 3600
    [recovery] = [call for call in billing.client.calls if call[0] == "recover-session"]
    assert recovery[1] == order["id"] and recovery[-1] == NOW + 3600
    assert len([call for call in billing.client.calls if call[0] == "create-session"]) == 1
    if outcome == "paid":
        assert current["claim_id"] and current["period_end_at"] == NOW + 7200
    else:
        assert current["status"] == "closed" and not current["claim_id"]
        assert billing.store.get_slot(current["slot_id"])["held_by"] is None


def test_unknown_recovery_uncertainty_preserves_reservation_and_never_posts_after_23_hours(billing):
    billing.client.fail_create = True
    with pytest.raises(SubscriptionError):
        start(billing)
    [order] = billing.store.list_slot_subscriptions("alice")
    billing.client.fail_recover = True
    billing.reconcile_pending(now=NOW + 1801)
    current = billing.store.get_slot_subscription(order["id"])
    assert current["status"] == "unknown" and current["session_id"] is None
    assert len([call for call in billing.client.calls if call[0] == "recover-session"]) == 1
    before = snapshot(billing.store)
    billing.reconcile_pending(now=NOW + 23 * 3600)
    assert snapshot(billing.store) == before
    assert len([call for call in billing.client.calls if call[0] == "recover-session"]) == 1


def test_paid_but_undeliverable_recovery_retains_receipt_holds_slot_and_cancels_future_charges(billing):
    start(billing)
    [order] = billing.store.list_slot_subscriptions("alice")
    original_slot = billing.store.get_slot(order["slot_id"])
    billing.reconcile_pending(now=NOW + 91)
    current = billing.store.get_slot_subscription(order["id"])
    assert current["payment_review_required"] and current["cancel_required"] is False
    assert current["cancel_at_period_end"] == 1
    assert billing.store.get_slot(order["slot_id"]) == original_slot
    [invoice] = billing.store._conn.execute("SELECT * FROM slot_subscription_invoices").fetchall()
    assert invoice["claim_id"] == "" and invoice["amount_cents"] == 1000
    before = snapshot(billing.store)
    billing.reconcile_pending(now=NOW + 100)
    assert snapshot(billing.store) == before


@pytest.mark.parametrize("broker", [False, True])
def test_disabled_fresh_primary_or_broker_never_reads_secrets_or_constructs_client(store, monkeypatch, broker):
    def forbidden(*args, **kwargs):
        pytest.fail("Inactive primary and broker must never read recurring payment credentials")

    monkeypatch.setattr(subscription_config.stripe_config, "_secret", forbidden)
    monkeypatch.setattr(stripe_subscriptions, "Client", forbidden)
    if broker:
        monkeypatch.setattr(store, "slot_subscription_processing_required", forbidden)
    config = SimpleNamespace(broker_only=broker, stripe_subscriptions_enabled=False)
    assert subscription_config.build_billing(config, store) is None


def test_existing_contract_cannot_be_orphaned_by_disabling_enrollment_without_config(store, monkeypatch):
    reserve(store)
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid recurring configuration must fail before protected reads")

    monkeypatch.setattr(subscription_config.stripe_config, "_secret", forbidden)
    with pytest.raises(ConfigError):
        subscription_config.build_billing(SimpleNamespace(broker_only=False,
            stripe_subscriptions_enabled=False), store)
