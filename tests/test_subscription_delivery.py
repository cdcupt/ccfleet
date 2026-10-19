"""Lost Checkout replies: real Store/client/signatures with synthetic HTTPS responses only."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import subprocess

import pytest

from ccfleetd import stripe_subscriptions
from ccfleetd.subscription_billing import Billing, SubscriptionError
from tests.test_slot_subscriptions import store as store  # noqa: F401 -- real temporary Store
from tests.test_stripe_subscriptions import flow as flow  # noqa: F401 -- strict client fake HTTP
from tests.test_wallet_store import NOW, snapshot

LATE = NOW + 24 * 3600


@pytest.fixture(autouse=True)
def forbid_real_operations(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Delivery tests must not use real providers, sockets or processes")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def delivery(store, flow):
    client, _old_order, responses, calls = flow
    row = store.reserve_slot_subscription(
        "alice",
        "node-a",
        expected_cents=1000,
        monthly_consent=True,
        idempotency_key="lost-response",
        merchant_id="acct_Merchant",
        livemode=False,
        now=NOW,
        max_age_s=90,
    )
    store.prepare_subscription_customer(
        "alice", merchant_id="acct_Merchant", livemode=False, now=NOW
    )
    store.bind_subscription_customer(
        "alice", merchant_id="acct_Merchant", livemode=False, customer_id="cus_Own", now=NOW
    )
    store.mark_slot_subscription_unknown(row["id"], now=NOW)
    billing = Billing(store, client, sandbox_database=True, portal_configuration_id="bpc_Own")
    order = billing._order(row)

    def amounts(value):
        if isinstance(value, dict):
            return {key: amounts(item) for key, item in value.items()}
        if isinstance(value, list):
            return [amounts(item) for item in value]
        return 1000 if type(value) is int and value == 1234 else value

    for path, value in list(responses.items()):
        responses[path] = amounts(value)
    tags = client._tag(order)
    responses["/v1/customers/cus_Own"]["metadata"]["ccfleet_account_id"] = "alice"
    responses["/v1/subscriptions/sub_Own"]["metadata"] = dict(tags)
    session = responses["/v1/checkout/sessions/cs_test_Own"]
    session.update(
        metadata=dict(tags),
        client_reference_id=row["id"],
        expires_at=order.expires_at,
        status="complete",
        payment_status="paid",
        subscription="sub_Own",
        invoice="in_Own",
    )
    responses["/v1/invoices/in_Own/lines"]["data"][0]["period"] = {
        "start": int(NOW),
        "end": int(NOW + 30 * 86400),
    }
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=LATE)
    store.insert_heartbeat("node-a", LATE, {"node_id": "node-a", "mode": "machine"})
    return billing, row, responses, calls


def notification(billing, row, *, kind="checkout.session.completed", **changes):
    data = {
        "id": "cs_test_Own",
        "metadata": {
            "ccfleet_purpose": stripe_subscriptions.PURPOSE,
            "ccfleet_order_id": row["id"],
        },
    }
    data.update(changes)
    event = {
        "id": "evt_LostReply",
        "object": "event",
        "livemode": False,
        "type": kind,
        "data": {"object": data},
    }
    raw = json.dumps(event, separators=(",", ":")).encode()
    stamp = str(int(LATE))
    signature = hmac.new(
        billing.settings.webhook_secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    return raw, "t=" + stamp + ",v1=" + signature


@pytest.mark.parametrize(
    "kind", ["checkout.session.completed", "checkout.session.async_payment_succeeded"]
)
def test_signed_completion_recovers_exact_paid_session_even_after_create_cache_window(
    delivery, kind
):
    billing, row, _responses, calls = delivery
    assert billing.store.get_slot_subscription(row["id"])["session_id"] is None
    raw, signature = notification(billing, row, kind=kind)
    assert billing.handle_webhook(raw, signature, now=LATE) == "paid"
    current = billing.store.get_slot_subscription(row["id"])
    assert current["session_id"] == "cs_test_Own" and current["subscription_id"] == "sub_Own"
    assert current["expires_at"] == int(NOW) + 3600 and current["claim_id"]
    assert current["period_end_at"] == NOW + 30 * 86400
    assert billing.store.wallet_balance("alice") == 0
    before = snapshot(billing.store)
    assert billing.handle_webhook(raw, signature, now=LATE) == "paid"
    assert snapshot(billing.store) == before
    assert calls and all(method == "GET" for method, _, _, _ in calls)
    assert (
        billing.store._conn.execute("SELECT COUNT(*) FROM slot_subscription_invoices").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "fault",
    [
        "unknown-order",
        "cross-owner",
        "wrong-session",
        "amount",
        "mode",
        "customer",
        "expiry",
        "still-open",
        "unpaid",
    ],
)
def test_event_hint_never_attaches_a_session_before_exact_server_verification(delivery, fault):
    billing, row, responses, calls = delivery
    changes = {}
    value = responses["/v1/checkout/sessions/cs_test_Own"]
    if fault == "unknown-order":
        changes["metadata"] = {
            "ccfleet_purpose": stripe_subscriptions.PURPOSE,
            "ccfleet_order_id": "unknown-local-order",
        }
    elif fault == "cross-owner":
        value["metadata"]["ccfleet_account_id"] = "bob"
    elif fault == "wrong-session":
        value["id"] = "cs_test_Other"
    elif fault == "amount":
        value["amount_total"] = 2000
    elif fault == "mode":
        value["livemode"] = True
    elif fault == "customer":
        value["customer"] = "cus_Foreign"
    elif fault == "expiry":
        value["expires_at"] += 3600
    elif fault == "still-open":
        value["status"], value["payment_status"] = "open", "unpaid"
    else:
        value["payment_status"] = "unpaid"
    before = snapshot(billing.store)
    raw, signature = notification(billing, row, **changes)
    with pytest.raises(SubscriptionError):
        billing.handle_webhook(raw, signature, now=LATE)
    assert snapshot(billing.store) == before
    assert billing.store.get_slot_subscription(row["id"])["session_id"] is None
    assert (
        calls == []
        if fault == "unknown-order"
        else all(method == "GET" for method, _, _, _ in calls)
    )


def test_tampered_notification_cannot_trigger_session_recovery(delivery):
    billing, row, _responses, calls = delivery
    raw, signature = notification(billing, row)
    before = snapshot(billing.store)
    with pytest.raises(SubscriptionError) as failure:
        billing.handle_webhook(raw + b" ", signature, now=LATE)
    assert failure.value.status_code == 400
    assert calls == [] and snapshot(billing.store) == before
