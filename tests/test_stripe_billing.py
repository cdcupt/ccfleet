"""Synthetic orchestration only: no transport, real account or payment."""
import hashlib
import hmac
import json
from dataclasses import replace

import pytest

from ccfleetd import stripe_checkout as checkout
from ccfleetd.stripe_billing import CREATE_RETRY_WINDOW, Billing, BillingError
from tests.test_wallet_store import NOW, snapshot
from tests.test_wallet_store import store as store


def settings(mode="test"):
    return checkout.Settings(mode, "acct_synthetic", "rk_" + mode + "_SYNTHETIC_ONLY",
                             "whsec_SYNTHETIC_ONLY")


class Client:
    def __init__(self, config=None):
        self.settings = config or settings()
        self.calls = []
        self.orders = {}
        self.references = {}
        self.fail_create = False
        self.fail_paid = False
        self.receipt_changes = {}

    def create_session(self, order):
        self.calls.append(("create", order.id))
        self.orders[order.id] = order
        if self.fail_create:
            raise checkout.StripeCheckoutError("SENSITIVE_PROVIDER_REPLY", uncertain=True)
        return checkout.HostedSession("cs_test_" + order.id,
                                     "https://checkout.stripe.com/c/pay/cs_test_" + order.id)

    def retrieve_open_session(self, session_id, order):
        self.calls.append(("open", session_id))
        return checkout.HostedSession(session_id, "https://checkout.stripe.com/c/pay/" + session_id)

    def retrieve_paid_session(self, session_id, order):
        self.calls.append(("paid", session_id))
        if self.fail_paid:
            raise checkout.StripeCheckoutError("SENSITIVE_PROVIDER_REPLY")
        receipt = checkout.PaidReceipt(order.id, order.account_id, order.amount_cents,
            self.settings.expected_account_id, self.settings.livemode, session_id, "pi_synthetic")
        return replace(receipt, **self.receipt_changes)

    def retrieve_payment_reference(self, payment_id):
        self.calls.append(("reference", payment_id))
        return self.references.get(payment_id)


@pytest.fixture
def billing(store):
    return Billing(store, Client(), sandbox_database=True)


def start(billing, *, key="checkout-1", now=NOW):
    return billing.start_checkout("alice", 2000, idempotency_key=key, now=now)


def metadata(billing, order):
    return {"integration_identifier": billing.settings.integration_identifier,
            "ccfleet_order_id": order.id, "ccfleet_account_id": order.account_id,
            "ccfleet_merchant_id": billing.settings.expected_account_id,
            "ccfleet_mode": billing.settings.mode, "ccfleet_purpose": checkout.PURPOSE}


def paid_object(billing, session, *, status="paid"):
    order = next(iter(billing.client.orders.values()))
    return {"id": session.id, "object": "checkout.session", "payment_status": status,
            "metadata": metadata(billing, order)}


def signed(billing, value, *, kind="checkout.session.completed", identity="evt_success", **changes):
    event = {"id": identity, "object": "event", "livemode": billing.settings.livemode,
             "type": kind, "data": {"object": value}}
    event.update(changes)
    body = json.dumps(event, separators=(",", ":")).encode()
    digest = hmac.new(billing.settings.webhook_secret.encode(),
                      str(int(NOW)).encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, "t=" + str(int(NOW)) + ",v1=" + digest


def handle(billing, value, **event):
    body, signature = signed(billing, value, **event)
    return billing.handle_webhook(body, signature, now=NOW)


def reference(billing):
    order = next(iter(billing.client.orders.values()))
    return checkout.PaymentReference("pi_synthetic", order.id, order.account_id,
        order.amount_cents, billing.settings.expected_account_id, billing.settings.livemode)


def test_constructor_is_inert_and_test_mode_cannot_fund_default_database(store):
    client = Client()
    before = snapshot(store)
    with pytest.raises(BillingError, match="isolated sandbox"):
        Billing(store, client)
    with pytest.raises(BillingError):
        Billing(store, client, sandbox_database=1)
    Billing(store, client, sandbox_database=True)
    Billing(store, Client(settings("live")))
    with pytest.raises(BillingError):
        Billing(store, Client(settings("live")), sandbox_database=True)
    assert client.calls == [] and snapshot(store) == before


def test_checkout_commits_order_before_provider_and_reuses_bound_session_after_24_hours(billing):
    original = billing.client.create_session

    def create(order):
        assert not billing.store._conn.in_transaction
        assert billing.store.get_wallet_topup("alice", order.id) is not None
        return original(order)

    billing.client.create_session = create
    first = start(billing)
    assert billing.store.wallet_balance("alice") == 0
    assert start(billing, now=NOW + 25 * 3600) == first
    assert [call[0] for call in billing.client.calls] == ["create", "open"]
    assert billing.store.get_wallet_checkout("alice", next(iter(billing.client.orders))) == first.id


def test_uncertain_create_retries_same_local_order_and_stops_before_key_expiry(billing):
    billing.client.fail_create = True
    with pytest.raises(BillingError) as error:
        start(billing)
    assert "SENSITIVE" not in str(error.value) and error.value.status_code == 503
    first_id = billing.client.calls[0][1]
    billing.client.fail_create = False
    start(billing, now=NOW + 10)
    assert billing.client.calls[:2] == [("create", first_id), ("create", first_id)]
    assert billing.store.wallet_balance("alice") == 0


def test_unresolved_creation_is_not_repeated_after_safe_idempotency_window(billing):
    billing.client.fail_create = True
    with pytest.raises(BillingError):
        start(billing)
    before = snapshot(billing.store)
    for later in (NOW - 1, NOW + CREATE_RETRY_WINDOW, NOW + 2 * 86400):
        with pytest.raises(BillingError):
            start(billing, now=later)
    assert len(billing.client.calls) == 1
    assert snapshot(billing.store) == before


@pytest.mark.parametrize("amount", [True, 0, 1001, "2000"])
def test_bad_credit_amount_cannot_create_remote_or_local_order(billing, amount):
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        billing.start_checkout("alice", amount, idempotency_key="bad", now=NOW)
    assert error.value.status_code == 400
    assert billing.client.calls == [] and snapshot(billing.store) == before


@pytest.mark.parametrize("change", [{"livemode": True}, {"account": "acct_other"}])
def test_wrong_mode_or_merchant_signed_envelope_is_rejected_before_provider_reads(billing, change):
    body, signature = signed(billing, {}, **change)
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        billing.handle_webhook(body, signature, now=NOW)
    assert error.value.status_code == 400
    assert billing.client.calls == [] and snapshot(billing.store) == before


def test_raw_signature_failure_is_inert(billing):
    body, signature = signed(billing, {})
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        billing.handle_webhook(body + b" ", signature, now=NOW)
    assert error.value.status_code == 400
    assert billing.client.calls == [] and snapshot(billing.store) == before


def test_duplicate_and_async_success_events_retrieve_paid_payment_and_credit_once(billing):
    session = start(billing)
    value = paid_object(billing, session)
    assert handle(billing, value) == "credited"
    assert handle(billing, value) == "credited"
    assert handle(billing, value, identity="evt_async",
                  kind="checkout.session.async_payment_succeeded") == "credited"
    assert billing.store.wallet_balance("alice") == 2000
    assert len(billing.store.list_wallet_entries("alice")) == 1
    assert billing.store._conn.execute("SELECT COUNT(*) FROM wallet_topup_events").fetchone()[0] == 2
    assert [call[0] for call in billing.client.calls] == ["create", "paid", "paid", "paid"]


def test_unpaid_completed_event_waits_for_later_verified_success(billing):
    session = start(billing)
    assert handle(billing, paid_object(billing, session, status="unpaid")) == "ignored"
    assert billing.store.wallet_balance("alice") == 0
    assert [call[0] for call in billing.client.calls] == ["create"]
    assert handle(billing, paid_object(billing, session), identity="evt_async",
                  kind="checkout.session.async_payment_succeeded") == "credited"


def test_paid_snapshot_is_not_enough_without_provider_retrieval(billing):
    session = start(billing)
    billing.client.fail_paid = True
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        handle(billing, paid_object(billing, session))
    assert error.value.status_code == 503 and "SENSITIVE" not in str(error.value)
    assert snapshot(billing.store) == before


@pytest.mark.parametrize("change", [
    {"amount_cents": 1000}, {"amount_cents": True}, {"account_id": "bob"},
    {"merchant_id": "acct_other"}, {"livemode": True}, {"payment_status": "unpaid"},
    {"session_id": "cs_test_other"},
])
def test_paid_receipt_mismatch_is_retryable_and_never_credits(billing, change):
    session = start(billing)
    billing.client.receipt_changes = change
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        handle(billing, paid_object(billing, session))
    assert error.value.status_code == 503 and snapshot(billing.store) == before


@pytest.mark.parametrize("metadata_change", [None, {}, {"unrelated": "application"},
    {"ccfleet_order_id": "missing"}, {"integration_identifier": "other"}])
def test_known_session_with_missing_or_partial_own_metadata_retries_instead_of_ignoring(
        billing, metadata_change):
    session = start(billing)
    value = {**paid_object(billing, session), "metadata": metadata_change}
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        handle(billing, value)
    assert error.value.status_code == 503 and snapshot(billing.store) == before


def test_unrelated_events_and_unknown_sessions_are_ignored_without_fulfillment(billing):
    before = snapshot(billing.store)
    assert handle(billing, {}, kind="customer.updated") == "ignored"
    assert handle(billing, {"id": "cs_test_unrelated", "object": "checkout.session",
                           "metadata": {}, "payment_status": "paid"}) == "ignored"
    assert billing.client.calls == [] and snapshot(billing.store) == before


def test_our_unknown_order_or_unbound_checkout_fails_for_retry(billing):
    session = start(billing)
    value = paid_object(billing, session)
    value["metadata"]["ccfleet_order_id"] = "missing"
    with pytest.raises(BillingError) as error:
        handle(billing, value)
    assert error.value.status_code == 503
    value = paid_object(billing, session)
    value["id"] = "cs_test_other"
    with pytest.raises(BillingError):
        handle(billing, value)
    assert billing.store.wallet_balance("alice") == 0


def test_out_of_order_refund_holds_before_paid_receipt_and_remains_held_after_credit(billing):
    session = start(billing)
    billing.client.references["pi_synthetic"] = reference(billing)
    refund = {"id": "re_synthetic", "object": "refund", "payment_intent": "pi_synthetic"}
    assert handle(billing, refund, kind="refund.created", identity="evt_refund") == "held"
    assert billing.store.wallet_spending_blocked("alice")
    assert billing.store.wallet_balance("alice") == 0
    before = snapshot(billing.store)
    with pytest.raises(BillingError):
        start(billing, key="another-topup")
    assert snapshot(billing.store) == before
    assert handle(billing, paid_object(billing, session)) == "credited"
    assert billing.store.wallet_balance("alice") == 2000
    assert billing.store.wallet_spending_blocked("alice")


@pytest.mark.parametrize("changes", [
    {"order_id": "missing"}, {"account_id": "bob"}, {"amount_cents": 1000},
    {"amount_cents": True}, {"merchant_id": "acct_other"}, {"livemode": True},
    {"payment_intent_id": "pi_different"}, {"currency": "EUR"},
])
def test_our_mismatched_payment_reference_is_retryable_without_new_hold(billing, changes):
    start(billing)
    billing.client.references["pi_synthetic"] = replace(reference(billing), **changes)
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        handle(billing, {"object": "dispute", "payment_intent": "pi_synthetic"},
               kind="charge.dispute.created", identity="evt_dispute")
    assert error.value.status_code == 503 and snapshot(billing.store) == before


def test_unrelated_payment_is_ignored_but_known_payment_with_removed_metadata_retries(billing):
    session = start(billing)
    value = {"object": "charge", "payment_intent": "pi_synthetic"}
    assert handle(billing, value, kind="charge.refunded", identity="evt_unrelated") == "ignored"
    handle(billing, paid_object(billing, session))
    before = snapshot(billing.store)
    with pytest.raises(BillingError) as error:
        handle(billing, value, kind="charge.refunded", identity="evt_refund")
    assert error.value.status_code == 503 and snapshot(billing.store) == before


def test_expanded_payment_identity_and_missing_unrelated_legacy_identity(billing):
    start(billing)
    billing.client.references["pi_synthetic"] = reference(billing)
    value = {"object": "dispute", "payment_intent": {"object": "payment_intent",
                                                       "id": "pi_synthetic"}}
    assert handle(billing, value, kind="charge.dispute.created", identity="evt_dispute") == "held"
    assert handle(billing, {"object": "charge", "payment_intent": None},
                  kind="charge.refunded", identity="evt_legacy") == "ignored"


def test_wrong_review_object_or_missing_own_payment_identity_fails_safely(billing):
    start(billing)
    for value in ({"object": "wrong", "payment_intent": "pi_synthetic"},
                  {"object": "charge", "metadata": {"ccfleet_order_id": "known"}}):
        with pytest.raises(BillingError) as error:
            handle(billing, value, kind="charge.refunded", identity="evt_bad")
        assert error.value.status_code == 503
    assert not billing.store.wallet_spending_blocked("alice")


def test_review_arriving_during_create_prevents_returning_payment_link(billing):
    original = billing.client.create_session

    def create(order):
        session = original(order)
        ref = reference(billing).as_dict()
        billing.store.hold_wallet_topup("pi_synthetic", reference=ref, event_id="evt_race",
            event_type="refund.created", body_sha256="a" * 64, now=NOW)
        return session

    billing.client.create_session = create
    with pytest.raises(BillingError, match="payment review"):
        start(billing)
    assert billing.store.wallet_balance("alice") == 0
    assert billing.store.wallet_spending_blocked("alice")
