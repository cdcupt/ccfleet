"""Recurring adapter tests use synthetic HTTP replies, never Stripe accounts or charges."""

from __future__ import annotations

import copy
import json
import urllib.parse

import pytest

from ccfleetd import stripe_checkout as base
from ccfleetd import stripe_subscriptions as recurring
from tests.test_stripe_billing import NOW, signed


class Reply:
    status = 200

    def __init__(self, value, url):
        self.value, self.url = value, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def geturl(self):
        return self.url

    def read(self, maximum):
        raw = json.dumps(self.value).encode()
        assert len(raw) < maximum
        return raw


@pytest.fixture
def flow():
    settings = base.Settings(
        mode="test",
        expected_account_id="acct_Merchant",
        api_key="rk_test_SYNTHETICONLY123456",
        webhook_secret="whsec_SYNTHETICONLY123456",
        public_origin="https://fleet.example.invalid",
    )
    order = recurring.Order("order-1", "account-1", "slot-1", "node-1", 1234, 10000, 7)
    calls = []
    client = recurring.Client(settings, opener=lambda *a, **k: pytest.fail("unconfigured fake"))
    tags = client._tag(order)
    responses = {
        "/v1/account": {"object": "account", "id": settings.expected_account_id},
        "/v1/customers/cus_Own": {
            "object": "customer",
            "id": "cus_Own",
            "livemode": False,
            "metadata": {
                "ccfleet_account_id": order.account_id,
                "ccfleet_merchant_id": settings.expected_account_id,
                "ccfleet_mode": "test",
            },
        },
        "/v1/subscriptions/sub_Own": {
            "object": "subscription",
            "id": "sub_Own",
            "livemode": False,
            "metadata": tags,
            "customer": "cus_Own",
            "currency": "usd",
            "collection_method": "charge_automatically",
            "cancel_at_period_end": False,
            "status": "active",
            "latest_invoice": "in_Own",
            "items": {
                "has_more": False,
                "data": [{"id": "si_Own", "quantity": 1, "price": {"id": "price_Own"}}],
            },
        },
        "/v1/prices/price_Own": {
            "object": "price",
            "id": "price_Own",
            "livemode": False,
            "currency": "usd",
            "unit_amount": 1234,
            "type": "recurring",
            "recurring": {"interval": "month", "interval_count": 1, "usage_type": "licensed"},
        },
        "/v1/invoices/in_Own": {
            "object": "invoice",
            "id": "in_Own",
            "livemode": False,
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": "sub_Own"},
            },
            "customer": "cus_Own",
            "status": "paid",
            "currency": "usd",
            "collection_method": "charge_automatically",
            "billing_reason": "subscription_cycle",
            "amount_paid": 1234,
            "amount_due": 1234,
            "subtotal": 1234,
            "total": 1234,
            "amount_remaining": 0,
        },
        "/v1/invoices/in_Own/lines": {
            "object": "list",
            "has_more": False,
            "data": [
                {
                    "object": "line_item",
                    "amount": 1234,
                    "currency": "usd",
                    "quantity": 1,
                    "period": {"start": 100, "end": 100 + 30 * 86400},
                    "parent": {
                        "type": "subscription_item_details",
                        "subscription_item_details": {
                            "subscription": "sub_Own",
                            "subscription_item": "si_Own",
                            "proration": False,
                        },
                    },
                    "pricing": {"price_details": {"price": "price_Own"}},
                }
            ],
        },
        "/v1/invoice_payments?invoice=in_Own": {
            "object": "list",
            "has_more": False,
            "data": [
                {
                    "object": "invoice_payment",
                    "livemode": False,
                    "invoice": "in_Own",
                    "status": "paid",
                    "currency": "usd",
                    "amount_paid": 1234,
                    "payment": {"type": "payment_intent", "payment_intent": "pi_Own"},
                }
            ],
        },
        "/v1/payment_intents/pi_Own": {
            "object": "payment_intent",
            "livemode": False,
            "id": "pi_Own",
            "status": "succeeded",
            "customer": "cus_Own",
            "currency": "usd",
            "amount": 1234,
            "amount_received": 1234,
            "payment_method_types": ["card"],
        },
    }
    session = {
        "object": "checkout.session",
        "id": "cs_test_Own",
        "livemode": False,
        "metadata": tags,
        "mode": "subscription",
        "ui_mode": "hosted_page",
        "customer": "cus_Own",
        "client_reference_id": order.id,
        "currency": "usd",
        "expires_at": order.expires_at,
        "amount_total": 1234,
        "amount_subtotal": 1234,
        "allowed_payment_method_types": ["card"],
        "payment_method_types": ["card"],
        "automatic_tax": {"enabled": False},
        "total_details": {"amount_tax": 0, "amount_discount": 0, "amount_shipping": 0},
        "status": "open",
        "payment_status": "unpaid",
        "url": "https://checkout.stripe.com/c/pay/cs_test_Own",
    }
    responses["/v1/checkout/sessions"] = session
    responses["/v1/checkout/sessions/cs_test_Own"] = session
    responses["/v1/billing_portal/configurations/bpc_Own"] = {
        "object": "billing_portal.configuration",
        "id": "bpc_Own",
        "active": True,
        "livemode": False,
        "features": {
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "subscription_update": {"enabled": False},
            "payment_method_update": {"enabled": False},
            "customer_update": {"enabled": False},
        },
    }
    responses["/v1/billing_portal/sessions"] = {
        "object": "billing_portal.session",
        "livemode": False,
        "customer": "cus_Own",
        "configuration": "bpc_Own",
        "url": "https://billing.stripe.com/p/session/test_own",
    }

    def transport(request, **kwargs):
        assert kwargs == {"timeout": base.TIMEOUT_S}
        assert request.full_url.startswith(base.API_ORIGIN + "/v1/")
        path = request.full_url[len(base.API_ORIGIN) :]
        fields = (
            None if request.data is None else dict(urllib.parse.parse_qsl(request.data.decode()))
        )
        calls.append((request.method, path, fields, dict(request.header_items())))
        assert path in responses, path
        if request.method == "POST" and path == "/v1/subscriptions/sub_Own":
            responses[path]["cancel_at_period_end"] = True
        return Reply(copy.deepcopy(responses[path]), request.full_url)

    client._open = transport
    return client, order, responses, calls


def test_paid_invoice_has_actual_exact_period_and_succeeded_payment(flow):
    client, order, _, calls = flow
    receipt = client.retrieve_paid_invoice("in_Own", "sub_Own", "cus_Own", order)
    assert receipt["period_start_at"] == 100 and receipt["period_end_at"] == 100 + 30 * 86400
    assert receipt["payment_intent_id"] == "pi_Own" and receipt["paid"] is True
    assert receipt["amount_cents"] == 1234 and receipt["merchant_id"] == "acct_Merchant"
    assert all(method == "GET" for method, _, _, _ in calls)


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("/v1/account", "id", "acct_Other"),
        ("/v1/subscriptions/sub_Own", "customer", "cus_Other"),
        ("/v1/subscriptions/sub_Own", "livemode", True),
        ("/v1/invoices/in_Own", "amount_paid", 1235),
        ("/v1/invoices/in_Own", "currency", "eur"),
        ("/v1/invoices/in_Own", "status", "open"),
        ("/v1/invoices/in_Own", "billing_reason", "manual"),
        ("/v1/payment_intents/pi_Own", "status", "requires_action"),
        ("/v1/payment_intents/pi_Own", "customer", "cus_Other"),
        ("/v1/payment_intents/pi_Own", "amount_received", 0),
        ("/v1/invoices/in_Own/lines", "has_more", True),
        ("/v1/invoice_payments?invoice=in_Own", "data", []),
    ],
)
def test_paid_invoice_mismatches_refuse_without_provider_write(flow, target, field, value):
    client, order, responses, calls = flow
    responses[target][field] = value
    with pytest.raises(base.StripeCheckoutError):
        client.retrieve_paid_invoice("in_Own", "sub_Own", "cus_Own", order)
    assert all(method == "GET" for method, _, _, _ in calls)


def test_monthly_checkout_uses_dynamic_card_filter_fixed_price_and_stable_identity(flow):
    client, order, _, calls = flow
    one = client.create_subscription_session(order, "cus_Own")
    two = client.create_subscription_session(order, "cus_Own")
    assert one == two
    creates = [row for row in calls if row[0] == "POST"]
    assert len(creates) == 2 and creates[0] == creates[1]
    fields = creates[0][2]
    assert fields["mode"] == "subscription"
    assert fields["allowed_payment_method_types[0]"] == "card"
    assert not any(key.startswith("payment_method_types") for key in fields)
    assert fields["line_items[0][price_data][recurring][interval]"] == "month"
    assert fields["line_items[0][price_data][unit_amount]"] == "1234"
    assert (
        fields["line_items[0][price_data][product_data][name]"]
        == "CC Fleet · Machine 7 · monthly slot"
    )
    assert order.id not in fields["line_items[0][price_data][product_data][name]"]
    assert fields["subscription_data[metadata][ccfleet_order_id]"] == order.id


def test_expired_unpaid_proof_only_closes_no_subscription(flow):
    client, order, responses, _ = flow
    value = responses["/v1/checkout/sessions/cs_test_Own"]
    value.update(status="expired", payment_status="unpaid", subscription=None)
    proof = client.expire_unpaid_session("cs_test_Own", order, "cus_Own")
    assert proof["subscription_id"] is None and proof["payment_status"] == "unpaid"
    value.update(subscription="sub_Own")
    assert client.expire_unpaid_session("cs_test_Own", order, "cus_Own") is None


def test_portal_rejects_immediate_cancellation_or_plan_changes(flow):
    client, _, responses, _ = flow
    assert client.create_portal("cus_Own", "account-1", "bpc_Own").startswith(
        "https://billing.stripe.com/"
    )
    config = responses["/v1/billing_portal/configurations/bpc_Own"]
    config["features"]["subscription_cancel"]["mode"] = "immediately"
    with pytest.raises(base.StripeCheckoutError):
        client.create_portal("cus_Own", "account-1", "bpc_Own")


@pytest.mark.parametrize(
    "path,fields",
    [
        ("/v1/subscriptions/sub_Own", {}),
        ("/v1/refunds", {"amount": "1234"}),
        ("https://attacker.invalid", None),
        ("/v1/invoices/in_Own?unapproved=true", None),
    ],
)
def test_unreviewed_provider_operations_never_reach_transport(flow, path, fields):
    client, _, _, calls = flow
    with pytest.raises(base.StripeCheckoutError):
        client._request(path, fields=fields, idempotency_key="synthetic")
    assert not calls


def test_foreign_wallet_review_has_no_subscription_and_is_ignored(flow):
    client, _, responses, calls = flow
    responses["/v1/charges/ch_Wallet"] = {
        "object": "charge",
        "id": "ch_Wallet",
        "livemode": False,
        "payment_intent": "pi_Wallet",
    }
    responses["/v1/payment_intents/pi_Wallet"] = {
        "object": "payment_intent",
        "id": "pi_Wallet",
        "livemode": False,
    }
    path = (
        "/v1/invoice_payments?payment%5Btype%5D=payment_intent"
        "&payment%5Bpayment_intent%5D=pi_Wallet"
    )
    responses[path] = {"object": "list", "has_more": False, "data": []}
    assert client.review_subscription("charge.refunded", {"id": "ch_Wallet"}) is None
    assert all(method == "GET" for method, _, _, _ in calls)
    assert not any("/subscriptions/" in path for _, path, _, _ in calls)


@pytest.mark.parametrize("kind", ["charge.dispute.created", "charge.dispute.closed"])
@pytest.mark.parametrize("through_charge", [False, True])
@pytest.mark.parametrize("allocated", [False, True])
def test_signed_du_dispute_resolves_only_verified_invoice_allocation(
    flow, kind, through_charge, allocated
):
    client, _, responses, calls = flow
    dispute = {
        "id": "du_SYNTHETIC",
        "object": "dispute",
        "livemode": False,
        "charge": "ch_Own",
        "payment_intent": None if through_charge else "pi_Own",
        "reason": "fraudulent",
        "status": "needs_response",
    }
    responses["/v1/disputes/du_SYNTHETIC"] = dispute
    responses["/v1/charges/ch_Own"] = {
        "id": "ch_Own",
        "object": "charge",
        "livemode": False,
        "payment_intent": "pi_Own",
    }
    path = (
        "/v1/invoice_payments?payment%5Btype%5D=payment_intent"
        "&payment%5Bpayment_intent%5D=pi_Own"
    )
    responses[path] = {
        "object": "list",
        "has_more": False,
        "data": [
            {
                "object": "invoice_payment",
                "livemode": False,
                "invoice": "in_Own",
                "payment": {"type": "payment_intent", "payment_intent": "pi_Own"},
            }
        ] if allocated else [],
    }
    body, signature = signed(client, dispute, kind=kind, identity="evt_dispute")
    event = base.verify_webhook(
        body, signature, client.settings.webhook_secret, mode="test",
        expected_account_id=client.settings.expected_account_id, now=NOW,
    )
    assert client.review_subscription(event["type"], event["data"]["object"]) == (
        "sub_Own" if allocated else None
    )
    assert ("GET", "/v1/disputes/du_SYNTHETIC", None) in [call[:3] for call in calls]
    assert any(call[1] == "/v1/charges/ch_Own" for call in calls) is through_charge
    assert any(call[1] == "/v1/invoices/in_Own" for call in calls) is allocated
    assert all(method == "GET" for method, _, _, _ in calls)


@pytest.mark.parametrize("identity", ["dp_SYNTHETIC", "du_", "du_SYNTHETIC/other", "du_SYNTHETIC?expand=x"])
def test_invalid_dispute_ids_and_paths_never_reach_dispute_transport(flow, identity):
    client, _, _, calls = flow
    with pytest.raises(base.StripeCheckoutError):
        client.review_subscription("charge.dispute.created", {"id": identity})
    assert all(path == "/v1/account" for _, path, _, _ in calls)
    calls.clear()
    with pytest.raises(base.StripeCheckoutError):
        client._request("/v1/disputes/" + identity)
    assert calls == []


def test_dispute_response_identity_mismatch_never_traverses_payment_graph(flow):
    client, _, responses, calls = flow
    responses["/v1/disputes/du_SYNTHETIC"] = {
        "object": "dispute", "id": "du_OTHER", "payment_intent": "pi_Own",
    }
    with pytest.raises(base.StripeCheckoutError):
        client.review_subscription("charge.dispute.created", {"id": "du_SYNTHETIC"})
    assert [path for _, path, _, _ in calls] == ["/v1/account", "/v1/disputes/du_SYNTHETIC"]


def test_cancellation_only_stops_future_renewal_and_rechecks_actual_state(flow):
    client, _, responses, calls = flow
    proof = client.cancel_after_paid_period("sub_Own", "cus_Own", operation_key="cancel-one")
    assert proof["cancel_at_period_end"] is True
    creates = [row for row in calls if row[0] == "POST"]
    assert len(creates) == 1
    assert creates[0][2] == {"cancel_at_period_end": "true", "proration_behavior": "none"}
    assert calls[-1][0:2] == ("GET", "/v1/subscriptions/sub_Own")
    client.cancel_after_paid_period("sub_Own", "cus_Own", operation_key="cancel-one")
    assert len([row for row in calls if row[0] == "POST"]) == 1
    # A later explicit Stripe reactivation requires a new durable operation key.
    responses["/v1/subscriptions/sub_Own"]["cancel_at_period_end"] = False
    client.cancel_after_paid_period("sub_Own", "cus_Own", operation_key="cancel-two")
    creates = [row for row in calls if row[0] == "POST"]
    assert len(creates) == 2
    assert creates[0][3]["Idempotency-key"] != creates[1][3]["Idempotency-key"]


def test_defensive_cancel_uses_first_class_binding_if_metadata_was_removed(flow):
    client, _, responses, _ = flow
    responses["/v1/subscriptions/sub_Own"]["metadata"] = {}
    assert (
        client.cancel_after_paid_period("sub_Own", "cus_Own", operation_key="bound-cancel")[
            "cancel_at_period_end"
        ]
        is True
    )


def test_uncertain_cancellation_preserves_uncertain_result_without_false_confirmation(flow):
    client, _, _, calls = flow
    original = client._open

    def timeout(request, **kwargs):
        if request.method == "POST":
            raise TimeoutError("SENSITIVE_PROVIDER_REPLY")
        return original(request, **kwargs)

    client._open = timeout
    with pytest.raises(base.StripeCheckoutError) as failure:
        client.cancel_after_paid_period("sub_Own", "cus_Own", operation_key="cancel-one")
    assert failure.value.uncertain is True and "SENSITIVE" not in str(failure.value)
    assert all(row[0] == "GET" for row in calls)


@pytest.mark.parametrize(
    "fields",
    [
        {"cancel_at_period_end": "false", "proration_behavior": "none"},
        {"cancel_at_period_end": "true", "proration_behavior": "create_prorations"},
        {
            "cancel_at_period_end": "true",
            "proration_behavior": "none",
            "items[0][price]": "price_Other",
        },
    ],
)
def test_cancellation_rejects_every_unapproved_update_before_transport(flow, fields):
    client, _, _, calls = flow
    with pytest.raises(base.StripeCheckoutError):
        client._request("/v1/subscriptions/sub_Own", fields=fields, idempotency_key="synthetic")
    assert calls == []


@pytest.mark.parametrize("change", ["item", "quantity"])
def test_paid_invoice_must_identify_the_single_exact_subscription_item(flow, change):
    client, order, responses, calls = flow
    line = responses["/v1/invoices/in_Own/lines"]["data"][0]
    if change == "item":
        line["parent"]["subscription_item_details"]["subscription_item"] = "si_Other"
    else:
        line["quantity"] = True
    with pytest.raises(base.StripeCheckoutError):
        client.retrieve_paid_invoice("in_Own", "sub_Own", "cus_Own", order)
    assert all(method == "GET" for method, _, _, _ in calls)


@pytest.mark.parametrize(
    "url",
    [
        "https://billing.stripe.com/p/session?secret=test_OPAQUE",
        "https://billing.stripe.com/p/session/test_opaque?secret=test_OPAQUE",
    ],
)
def test_documented_stripe_portal_secret_query_is_accepted_without_rewriting(flow, url):
    client, _, responses, _ = flow
    responses["/v1/billing_portal/sessions"]["url"] = url
    assert client.create_portal("cus_Own", "account-1", "bpc_Own") == url


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.invalid/p/session?secret=test_OPAQUE",
        "https://billing.stripe.com:443/p/session?secret=test_OPAQUE",
        "https://user@billing.stripe.com/p/session?secret=test_OPAQUE",
        "https://billing.stripe.com/p/session?secret=test_OPAQUE#fragment",
        "https://billing.stripe.com/p/session?secret=test_OPAQUE&secret=two",
        "https://billing.stripe.com/p/session?secret=test_OPAQUE&next=evil",
        "https://billing.stripe.com/p/session?secret=",
        "https://billing.stripe.com/p/session?secret=%0aBAD",
        "https://billing.stripe.com/p/session/../../other?secret=test_OPAQUE",
    ],
)
def test_portal_url_rejects_host_scope_duplicate_secret_and_control_mismatches(url):
    with pytest.raises(base.StripeCheckoutError) as failure:
        recurring.portal_url(url)
    assert "OPAQUE" not in str(failure.value)


@pytest.mark.parametrize("state", ["complete", "expired"])
def test_unknown_create_recovery_uses_identical_original_request_and_accepts_closed_state(
    flow, state
):
    client, order, responses, calls = flow
    client.create_subscription_session(order, "cus_Own")
    first = [row for row in calls if row[0] == "POST"][-1]
    value = responses["/v1/checkout/sessions"]
    value.update(status=state, payment_status="paid" if state == "complete" else "unpaid")
    recovered = client.recover_subscription_session(order, "cus_Own")
    assert recovered["status"] == state
    assert [row for row in calls if row[0] == "POST"][-1] == first
