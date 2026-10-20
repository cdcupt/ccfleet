"""Strict monthly-slot Stripe adapter; construction is inert, tests use fake transports.

Official references: /payments/checkout/build-subscriptions,
/billing/subscriptions/webhooks, /api/invoice-line-item/object.
Existing one-time credit Checkout and its fulfillment are unchanged.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from . import stripe_checkout as base
from . import wallet
from .stripe_checkout import (
    API_ORIGIN,
    API_VERSION,
    MAX_BODY,
    TIMEOUT_S,
    HostedSession,
    StripeCheckoutError,
    _hosted_url,
    _json,
    _provider_id,
    _require,
)

PURPOSE = "ccfleet_monthly_slot"
INTEGRATION_IDENTIFIER = "ccfleet-monthly-slots-v1-jqvprnmt"


def portal_url(value: Any) -> str:
    """Validate Stripe's short-lived portal URL without exposing its secret query."""
    _require(
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and value.isascii()
        and "\\" not in value
        and not re.search(r"[\x00-\x20\x7f]", value),
        "Portal URL is invalid",
    )
    try:
        parsed = urllib.parse.urlsplit(value)
        query = (
            urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True, errors="strict"
            )
            if parsed.query
            else []
        )
        _require(
            parsed.scheme == "https"
            and parsed.hostname == "billing.stripe.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and re.fullmatch(r"/p/session(?:/[A-Za-z0-9_-]+)?", parsed.path) is not None
            and not parsed.fragment
            and (
                not query
                and parsed.path != "/p/session"
                or len(query) == 1
                and query[0][0] == "secret"
                and 0 < len(query[0][1]) <= 4096
                and query[0][1].isascii()
                and not re.search(r"[\x00-\x20\x7f]", query[0][1])
            ),
            "Portal URL is invalid",
        )
    except (ValueError, UnicodeError):
        raise StripeCheckoutError("Portal URL is invalid") from None
    return value


@dataclass(frozen=True)
class Order:
    id: str
    account_id: str
    slot_id: str
    node_id: str
    amount_cents: int
    expires_at: int
    machine_number: int

    def __post_init__(self):
        for value in (self.id, self.account_id, self.slot_id, self.node_id):
            wallet.identity(value)
        wallet.positive_cents(self.amount_cents)
        _require(type(self.expires_at) is int and self.expires_at > 0, "invalid rental expiration")
        _require(
            type(self.machine_number) is int and 0 < self.machine_number < 10**19,
            "invalid public machine number",
        )


class Client(base.Client):
    def _request(
        self,
        path: str,
        *,
        fields: Optional[Mapping[str, str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        method = "POST" if fields is not None else "GET"
        permitted = (
            path in {"/v1/checkout/sessions", "/v1/customers", "/v1/billing_portal/sessions"}
            or re.fullmatch(r"/v1/checkout/sessions/cs_(test|live)_[A-Za-z0-9_]+/expire", path)
            or re.fullmatch(r"/v1/subscriptions/sub_[A-Za-z0-9_]+", path)
            if method == "POST"
            else path == "/v1/account"
            or re.fullmatch(
                r"/v1/invoice_payments[?](?:invoice=in_[A-Za-z0-9_]+|payment%5Btype%5D=payment_intent&payment%5Bpayment_intent%5D=pi_[A-Za-z0-9_]+)",
                path,
            )
            is not None
            or re.fullmatch(
                r"/v1/(?:customers/cus_[A-Za-z0-9_]+|subscriptions/sub_[A-Za-z0-9_]+"
                r"|prices/price_[A-Za-z0-9_]+|invoices/in_[A-Za-z0-9_]+(?:/lines)?"
                r"|checkout/sessions/cs_(test|live)_[A-Za-z0-9_]+(?:/line_items)?"
                r"|billing_portal/configurations/bpc_[A-Za-z0-9_]+"
                r"|payment_intents/pi_[A-Za-z0-9_]+|charges/ch_[A-Za-z0-9_]+"
                r"|refunds/re_[A-Za-z0-9_]+|disputes/du_[A-Za-z0-9_]+)",
                path,
            )
            is not None
        )
        _require(permitted, "Stripe API operation is outside the Checkout adapter")
        if method == "POST" and path == "/v1/checkout/sessions":
            _require(
                fields.get("mode") == "subscription"
                and fields.get("allowed_payment_method_types[0]") == "card"
                and not any(
                    key.startswith("allowed_payment_method_types[")
                    and key != "allowed_payment_method_types[0]"
                    for key in fields
                ),
                "Only card-filtered monthly Checkout is permitted",
            )
        if method == "POST" and path.endswith("/expire"):
            _require(fields == {}, "Checkout expiry cannot change payment details")
        if method == "POST" and path.startswith("/v1/subscriptions/"):
            _require(
                fields == {"cancel_at_period_end": "true", "proration_behavior": "none"},
                "Only end-of-period cancellation is permitted",
            )
        _require(
            fields is None
            or not any(
                key == "payment_method_types" or key.startswith("payment_method_types[")
                for key in fields
            ),
            "static Stripe payment methods are not allowed",
        )
        body = urllib.parse.urlencode(fields).encode("ascii") if fields is not None else None
        headers = {
            "Authorization": "Bearer " + self.settings.api_key,
            "Accept": "application/json",
            "Stripe-Version": API_VERSION,
            "User-Agent": "CCFleetCheckout/1",
        }
        if fields is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            _require(
                isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 255,
                "stable Stripe creation idempotency key required",
            )
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            API_ORIGIN + path, data=body, method=method, headers=headers
        )
        try:
            with self._open(request, timeout=TIMEOUT_S) as response:
                _require(
                    type(response.status) is int and 200 <= response.status < 300,
                    "Stripe request was refused",
                )
                _require(response.geturl() == request.full_url, "Stripe API redirect was refused")
                raw = response.read(MAX_BODY + 1)
                _require(
                    isinstance(raw, bytes) and 0 < len(raw) <= MAX_BODY,
                    "Stripe response size is invalid",
                )
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise StripeCheckoutError(
                "Stripe HTTP request failed", uncertain=(method == "POST" and status >= 500)
            ) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise StripeCheckoutError(
                "Stripe connection could not be confirmed", uncertain=method == "POST"
            ) from None
        except StripeCheckoutError:
            raise StripeCheckoutError(
                "Stripe response could not be confirmed", uncertain=method == "POST"
            ) from None
        try:
            return _json(raw)
        except StripeCheckoutError:
            raise StripeCheckoutError(
                "Stripe response could not be confirmed", uncertain=method == "POST"
            ) from None

    def _tag(self, order: Order) -> dict[str, str]:
        return {
            "integration_identifier": INTEGRATION_IDENTIFIER,
            "ccfleet_purpose": PURPOSE,
            "ccfleet_order_id": order.id,
            "ccfleet_account_id": order.account_id,
            "ccfleet_slot_id": order.slot_id,
            "ccfleet_node_id": order.node_id,
            "ccfleet_merchant_id": self.settings.expected_account_id,
            "ccfleet_mode": self.settings.mode,
        }

    def _mode(self, value: dict[str, Any], object_type: str) -> None:
        _require(
            value.get("object") == object_type and value.get("livemode") is self.settings.livemode,
            "Stripe rental object mode differs",
        )

    def _tags(self, value: dict[str, Any], order: Order) -> None:
        tags = value.get("metadata")
        _require(
            isinstance(tags, dict)
            and all(tags.get(key) == item for key, item in self._tag(order).items()),
            "Stripe rental account or order differs",
        )

    def create_customer(self, account_id: str, operation_id: str) -> str:
        wallet.identity(account_id)
        wallet.request_id(operation_id)
        self.validate_account()
        value = self._request(
            "/v1/customers",
            fields={
                "name": "CC Fleet account",
                "metadata[ccfleet_account_id]": account_id,
                "metadata[ccfleet_merchant_id]": self.settings.expected_account_id,
                "metadata[ccfleet_mode]": self.settings.mode,
            },
            idempotency_key="ccfleet-rental-customer-" + operation_id,
        )
        return self._customer(value, account_id)

    def _customer(self, value: dict[str, Any], account_id: str) -> str:
        self._mode(value, "customer")
        tags = value.get("metadata")
        _require(
            value.get("deleted") is not True
            and isinstance(tags, dict)
            and tags.get("ccfleet_account_id") == account_id
            and tags.get("ccfleet_merchant_id") == self.settings.expected_account_id
            and tags.get("ccfleet_mode") == self.settings.mode,
            "Stripe rental customer differs",
        )
        return _provider_id(value.get("id"), "cus_")

    def retrieve_customer(self, customer_id: str, account_id: str) -> str:
        _provider_id(customer_id, "cus_")
        self.validate_account()
        value = self._customer(self._request("/v1/customers/" + customer_id), account_id)
        _require(value == customer_id, "Stripe customer identity differs")
        return value

    def _price(self, price_id: str, order: Order) -> dict[str, Any]:
        _provider_id(price_id, "price_")
        price = self._request("/v1/prices/" + price_id)
        self._mode(price, "price")
        _require(
            price.get("id") == price_id
            and price.get("currency") == "usd"
            and type(price.get("unit_amount")) is int
            and price["unit_amount"] == order.amount_cents
            and price.get("type") == "recurring"
            and isinstance(price.get("recurring"), dict)
            and price["recurring"].get("interval") == "month"
            and price["recurring"].get("interval_count") == 1
            and price["recurring"].get("usage_type") == "licensed",
            "Monthly rental price differs",
        )
        return price

    def _session(
        self,
        value: dict[str, Any],
        order: Order,
        customer_id: str,
        session_id: Optional[str] = None,
    ) -> str:
        identity = _provider_id(value.get("id"), "cs_" + self.settings.mode + "_")
        self._mode(value, "checkout.session")
        self._tags(value, order)
        _require(session_id is None or identity == session_id, "Rental Checkout identity differs")
        _require(
            value.get("mode") == "subscription"
            and value.get("ui_mode") == "hosted_page"
            and value.get("customer") == customer_id
            and value.get("client_reference_id") == order.id
            and value.get("currency") == "usd"
            and value.get("expires_at") == order.expires_at
            and all(
                type(value.get(key)) is int and value[key] == order.amount_cents
                for key in ("amount_total", "amount_subtotal")
            ),
            "Rental Checkout quote differs",
        )
        _require(
            value.get("allowed_payment_method_types") == ["card"]
            and value.get("payment_method_types") == ["card"]
            and value.get("setup_intent") is None,
            "Rental payment method differs",
        )
        tax, totals = value.get("automatic_tax"), value.get("total_details")
        _require(
            isinstance(tax, dict)
            and tax.get("enabled") is False
            and isinstance(totals, dict)
            and all(
                type(totals.get(key)) is int and totals[key] == 0
                for key in ("amount_tax", "amount_discount", "amount_shipping")
            ),
            "Rental taxes or adjustments differ",
        )
        _require(
            value.get("currency_conversion") is None, "Rental currency conversion is not supported"
        )
        adaptive = value.get("adaptive_pricing")
        _require(
            adaptive is None or isinstance(adaptive, dict) and adaptive.get("enabled") is False,
            "Rental Adaptive Pricing is not supported",
        )
        return identity

    def recover_subscription_session(self, order: Order, customer_id: str) -> dict[str, Any]:
        """Replay the exact original create, including expiry and idempotency key."""
        self.retrieve_customer(customer_id, order.account_id)
        plan_label = f"CC Fleet · Machine {order.machine_number} · monthly slot"
        fields = {
            "mode": "subscription",
            "ui_mode": "hosted_page",
            "customer": customer_id,
            "allowed_payment_method_types[0]": "card",
            "adaptive_pricing[enabled]": "false",
            "automatic_tax[enabled]": "false",
            "allow_promotion_codes": "false",
            "integration_identifier": INTEGRATION_IDENTIFIER,
            "client_reference_id": order.id,
            "expires_at": str(order.expires_at),
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(order.amount_cents),
            "line_items[0][price_data][recurring][interval]": "month",
            "line_items[0][price_data][recurring][interval_count]": "1",
            "line_items[0][price_data][product_data][name]": plan_label,
            "success_url": self.settings.public_origin + "/account?subscription=returned",
            "cancel_url": self.settings.public_origin + "/account?subscription=cancelled",
        }
        for key, value in self._tag(order).items():
            fields["metadata[" + key + "]"] = value
            fields["subscription_data[metadata][" + key + "]"] = value
        key = hashlib.sha256(
            (self.settings.expected_account_id + ":" + self.settings.mode + ":" + order.id).encode()
        ).hexdigest()
        value = self._request(
            "/v1/checkout/sessions",
            fields=fields,
            idempotency_key="ccfleet-monthly-checkout-" + key,
        )
        try:
            self._session(value, order, customer_id)
            _require(
                (value.get("status"), value.get("payment_status"))
                in {("open", "unpaid"), ("complete", "paid"), ("expired", "unpaid")},
                "Rental Checkout state could not be confirmed",
            )
            return value
        except StripeCheckoutError:
            raise StripeCheckoutError(
                "Created rental Checkout could not be confirmed", uncertain=True
            ) from None

    def create_subscription_session(self, order: Order, customer_id: str) -> HostedSession:
        value = self.recover_subscription_session(order, customer_id)
        try:
            _require(
                value.get("status") == "open" and value.get("payment_status") == "unpaid",
                "Rental Checkout is not open",
            )
            return HostedSession(value["id"], _hosted_url(value.get("url"), value["id"]))
        except StripeCheckoutError:
            raise StripeCheckoutError(
                "Created rental Checkout could not be confirmed", uncertain=True
            ) from None

    def retrieve_session(self, session_id: str, order: Order, customer_id: str) -> dict[str, Any]:
        _provider_id(session_id, "cs_" + self.settings.mode + "_")
        self.validate_account()
        value = self._request("/v1/checkout/sessions/" + session_id)
        self._session(value, order, customer_id, session_id)
        return value

    def retrieve_open_session(
        self, session_id: str, order: Order, customer_id: str
    ) -> HostedSession:
        value = self.retrieve_session(session_id, order, customer_id)
        _require(
            value.get("status") == "open" and value.get("payment_status") == "unpaid",
            "Rental Checkout is not open",
        )
        return HostedSession(session_id, _hosted_url(value.get("url"), session_id))

    def retrieve_subscription(
        self, subscription_id: str, order: Order, customer_id: str
    ) -> dict[str, Any]:
        value = self.subscription_reference(subscription_id)
        _require(value is not None, "Known rental subscription lost its ownership metadata")
        self._tags(value, order)
        _require(
            value.get("customer") == customer_id
            and value.get("currency") == "usd"
            and value.get("collection_method") == "charge_automatically"
            and type(value.get("cancel_at_period_end")) is bool,
            "Rental subscription customer or collection differs",
        )
        items = value.get("items")
        _require(
            isinstance(items, dict)
            and items.get("has_more") is False
            and isinstance(items.get("data"), list)
            and len(items["data"]) == 1,
            "Exactly one monthly slot item is required",
        )
        item = items["data"][0]
        _provider_id(item.get("id"), "si_")
        price = item.get("price")
        _require(
            type(item.get("quantity")) is int and item["quantity"] == 1 and isinstance(price, dict),
            "Rental quantity differs",
        )
        self._price(_provider_id(price.get("id"), "price_"), order)
        _require(
            not value.get("discounts")
            and not value.get("pending_update")
            and value.get("pause_collection") is None,
            "Unsupported rental subscription adjustment",
        )
        return value

    def subscription_reference(self, subscription_id: str) -> Optional[dict[str, Any]]:
        _provider_id(subscription_id, "sub_")
        self.validate_account()
        value = self._request("/v1/subscriptions/" + subscription_id)
        self._mode(value, "subscription")
        _require(value.get("id") == subscription_id, "Subscription identity differs")
        tags = value.get("metadata")
        _require(isinstance(tags, dict), "Subscription ownership metadata is invalid")
        own = tags.get("ccfleet_purpose") == PURPOSE or any(
            key.startswith("ccfleet_") for key in tags
        )
        if not own:
            return None
        _require(
            tags.get("ccfleet_purpose") == PURPOSE
            and tags.get("integration_identifier") == INTEGRATION_IDENTIFIER
            and tags.get("ccfleet_merchant_id") == self.settings.expected_account_id
            and tags.get("ccfleet_mode") == self.settings.mode,
            "Subscription ownership differs",
        )
        return value

    @staticmethod
    def _invoice_subscription(value: dict[str, Any]) -> Optional[str]:
        parent = value.get("parent")
        if parent is None:
            return None
        _require(
            isinstance(parent, dict)
            and parent.get("type") == "subscription_details"
            and isinstance(parent.get("subscription_details"), dict),
            "Invoice parent differs",
        )
        return _provider_id(parent["subscription_details"].get("subscription"), "sub_")

    def invoice_subscription(self, invoice_id: str) -> Optional[str]:
        _provider_id(invoice_id, "in_")
        self.validate_account()
        value = self._request("/v1/invoices/" + invoice_id)
        self._mode(value, "invoice")
        _require(value.get("id") == invoice_id, "Invoice identity differs")
        return self._invoice_subscription(value)

    def retrieve_paid_invoice(
        self, invoice_id: str, subscription_id: str, customer_id: str, order: Order
    ) -> dict[str, Any]:
        subscription = self.retrieve_subscription(subscription_id, order, customer_id)
        value = self._request("/v1/invoices/" + _provider_id(invoice_id, "in_"))
        self._mode(value, "invoice")
        _require(
            value.get("id") == invoice_id
            and self._invoice_subscription(value) == subscription_id
            and value.get("customer") == customer_id
            and value.get("status") == "paid"
            and value.get("currency") == "usd"
            and value.get("collection_method") == "charge_automatically"
            and all(
                type(value.get(key)) is int and value[key] == order.amount_cents
                for key in ("amount_paid", "amount_due", "subtotal", "total")
            )
            and value.get("amount_remaining") == 0
            and value.get("billing_reason") in {"subscription_create", "subscription_cycle"},
            "Paid rental invoice differs",
        )
        _require(
            not value.get("total_discount_amounts")
            and not value.get("total_taxes")
            and value.get("pre_payment_credit_notes_amount", 0) == 0
            and value.get("post_payment_credit_notes_amount", 0) == 0,
            "Unsupported rental invoice adjustments",
        )
        payment_intent_id = self._paid_invoice_payment(invoice_id, customer_id, order)
        lines = self._request("/v1/invoices/" + invoice_id + "/lines")
        _require(
            lines.get("object") == "list"
            and lines.get("has_more") is False
            and isinstance(lines.get("data"), list)
            and len(lines["data"]) == 1,
            "Exactly one paid rental invoice line is required",
        )
        line = lines["data"][0]
        period = line.get("period")
        parent = line.get("parent")
        price_id = subscription["items"]["data"][0]["price"]["id"]
        _require(
            line.get("object") == "line_item"
            and type(line.get("amount")) is int
            and line["amount"] == order.amount_cents
            and line.get("currency") == "usd"
            and type(line.get("quantity")) is int
            and line["quantity"] == 1
            and isinstance(period, dict)
            and isinstance(parent, dict)
            and parent.get("type") == "subscription_item_details"
            and isinstance(parent.get("subscription_item_details"), dict)
            and parent["subscription_item_details"].get("subscription") == subscription_id
            and parent["subscription_item_details"].get("subscription_item")
            == subscription["items"]["data"][0]["id"]
            and parent["subscription_item_details"].get("proration") is False,
            "Rental invoice line or period differs",
        )
        pricing = line.get("pricing")
        _require(
            isinstance(pricing, dict)
            and isinstance(pricing.get("price_details"), dict)
            and pricing["price_details"].get("price") == price_id,
            "Paid rental price differs",
        )
        start, end = period.get("start"), period.get("end")
        _require(
            type(start) is int
            and type(end) is int
            and start >= 0
            and 27 * 86400 <= end - start <= 32 * 86400,
            "Invalid monthly paid period",
        )
        return {
            "invoice_id": invoice_id,
            "subscription_id": subscription_id,
            "customer_id": customer_id,
            "order_id": order.id,
            "account_id": order.account_id,
            "slot_id": order.slot_id,
            "payment_intent_id": payment_intent_id,
            "amount_cents": order.amount_cents,
            "currency": "USD",
            "merchant_id": self.settings.expected_account_id,
            "livemode": self.settings.livemode,
            "paid": True,
            "period_start_at": start,
            "period_end_at": end,
        }

    def expire_unpaid_session(
        self, session_id: str, order: Order, customer_id: str
    ) -> Optional[dict[str, Any]]:
        value = self.retrieve_session(session_id, order, customer_id)
        if value.get("status") == "open" and value.get("payment_status") == "unpaid":
            value = self._request(
                "/v1/checkout/sessions/" + session_id + "/expire",
                fields={},
                idempotency_key="ccfleet-monthly-expire-"
                + hashlib.sha256(session_id.encode()).hexdigest(),
            )
            self._session(value, order, customer_id, session_id)
        if (
            value.get("status") != "expired"
            or value.get("payment_status") != "unpaid"
            or value.get("subscription") is not None
        ):
            return None
        return {
            "order_id": order.id,
            "session_id": session_id,
            "session_status": "expired",
            "payment_status": "unpaid",
            "subscription_id": None,
            "merchant_id": self.settings.expected_account_id,
            "livemode": self.settings.livemode,
        }

    def validate_portal_configuration(self, configuration_id: str) -> None:
        self.validate_account()
        _provider_id(configuration_id, "bpc_")
        config = self._request("/v1/billing_portal/configurations/" + configuration_id)
        features = config.get("features")
        _require(
            config.get("object") == "billing_portal.configuration"
            and config.get("id") == configuration_id
            and config.get("active") is True
            and config.get("livemode") is self.settings.livemode
            and isinstance(features, dict)
            and isinstance(features.get("subscription_cancel"), dict)
            and features["subscription_cancel"].get("enabled") is True
            and features["subscription_cancel"].get("mode") == "at_period_end"
            and isinstance(features.get("subscription_update"), dict)
            and features["subscription_update"].get("enabled") is False
            and isinstance(features.get("payment_method_update"), dict)
            and features["payment_method_update"].get("enabled") is False
            and isinstance(features.get("customer_update"), dict)
            and features["customer_update"].get("enabled") is False,
            "Only approved end-of-period cancellation is allowed",
        )

    def create_portal(self, customer_id: str, account_id: str, configuration_id: str) -> str:
        self.retrieve_customer(customer_id, account_id)
        self.validate_portal_configuration(configuration_id)
        value = self._request(
            "/v1/billing_portal/sessions",
            fields={
                "customer": customer_id,
                "configuration": configuration_id,
                "return_url": self.settings.public_origin + "/account",
            },
            idempotency_key="ccfleet-monthly-portal-"
            + hashlib.sha256(
                (account_id + ":" + str(__import__("time").time_ns())).encode()
            ).hexdigest(),
        )
        self._mode(value, "billing_portal.session")
        _require(
            value.get("object") == "billing_portal.session"
            and value.get("customer") == customer_id
            and value.get("configuration") == configuration_id,
            "Portal customer differs",
        )
        return portal_url(value.get("url"))

    def _paid_invoice_payment(self, invoice_id: str, customer_id: str, order: Order) -> str:
        payments = self._request("/v1/invoice_payments?invoice=" + invoice_id)
        _require(
            payments.get("object") == "list"
            and payments.get("has_more") is False
            and isinstance(payments.get("data"), list)
            and len(payments["data"]) == 1,
            "Exactly one Stripe invoice payment is required",
        )
        payment = payments["data"][0]
        self._mode(payment, "invoice_payment")
        reference = payment.get("payment")
        _require(
            payment.get("invoice") == invoice_id
            and payment.get("status") == "paid"
            and payment.get("currency") == "usd"
            and type(payment.get("amount_paid")) is int
            and payment["amount_paid"] == order.amount_cents
            and isinstance(reference, dict)
            and reference.get("type") == "payment_intent",
            "Paid invoice payment differs",
        )
        identity = _provider_id(reference.get("payment_intent"), "pi_")
        intent = self._request("/v1/payment_intents/" + identity)
        self._mode(intent, "payment_intent")
        _require(
            intent.get("id") == identity
            and intent.get("status") == "succeeded"
            and intent.get("customer") == customer_id
            and intent.get("currency") == "usd"
            and intent.get("payment_method_types") == ["card"]
            and all(
                type(intent.get(key)) is int and intent[key] == order.amount_cents
                for key in ("amount", "amount_received")
            ),
            "Recurring payment could not be confirmed",
        )
        return identity

    def review_subscription(self, event_type: str, data: dict[str, Any]) -> Optional[str]:
        self.validate_account()
        if event_type.startswith("charge.dispute."):
            identity = _provider_id(data.get("id"), "du_")
            value = self._request("/v1/disputes/" + identity)
            _require(
                value.get("object") == "dispute" and value.get("id") == identity,
                "Dispute identity differs",
            )
        elif event_type.startswith("refund."):
            identity = _provider_id(data.get("id"), "re_")
            value = self._request("/v1/refunds/" + identity)
            _require(
                value.get("object") == "refund" and value.get("id") == identity,
                "Refund identity differs",
            )
        elif event_type == "charge.refunded":
            identity = _provider_id(data.get("id"), "ch_")
            value = self._request("/v1/charges/" + identity)
            self._mode(value, "charge")
            _require(value.get("id") == identity, "Charge identity differs")
        else:
            raise StripeCheckoutError("Unsupported rental review event")
        intent_id = value.get("payment_intent")
        if intent_id is None and isinstance(value.get("charge"), str):
            charge = self._request("/v1/charges/" + _provider_id(value["charge"], "ch_"))
            self._mode(charge, "charge")
            intent_id = charge.get("payment_intent")
        intent_id = _provider_id(intent_id, "pi_")
        intent = self._request("/v1/payment_intents/" + intent_id)
        self._mode(intent, "payment_intent")
        _require(intent.get("id") == intent_id, "Review payment identity differs")
        path = (
            "/v1/invoice_payments?payment%5Btype%5D=payment_intent"
            + "&payment%5Bpayment_intent%5D="
            + intent_id
        )
        payments = self._request(path)
        _require(
            payments.get("object") == "list"
            and payments.get("has_more") is False
            and isinstance(payments.get("data"), list),
            "Review invoice payment list is incomplete",
        )
        if not payments["data"]:
            return None
        _require(len(payments["data"]) == 1, "Review payment has multiple invoice allocations")
        payment = payments["data"][0]
        self._mode(payment, "invoice_payment")
        _require(
            payment.get("payment") == {"type": "payment_intent", "payment_intent": intent_id},
            "Review invoice payment identity differs",
        )
        return self.invoice_subscription(_provider_id(payment.get("invoice"), "in_"))

    def bound_subscription_identity(self, subscription_id: str, customer_id: str) -> dict[str, Any]:
        """First-class local binding authorizes defensive cancellation even if tags were removed."""
        _provider_id(subscription_id, "sub_")
        _provider_id(customer_id, "cus_")
        self.validate_account()
        value = self._request("/v1/subscriptions/" + subscription_id)
        self._mode(value, "subscription")
        _require(
            value.get("id") == subscription_id
            and value.get("customer") == customer_id
            and type(value.get("cancel_at_period_end")) is bool,
            "Bound subscription identity differs",
        )
        return value

    def cancel_after_paid_period(
        self, subscription_id: str, customer_id: str, *, operation_key: str
    ) -> dict[str, Any]:
        wallet.request_id(operation_key)
        value = self.bound_subscription_identity(subscription_id, customer_id)
        terminal = {"canceled", "incomplete_expired"}
        if value.get("status") not in terminal and value["cancel_at_period_end"] is not True:
            key = hashlib.sha256(
                (
                    self.settings.expected_account_id
                    + ":"
                    + self.settings.mode
                    + ":"
                    + subscription_id
                    + ":"
                    + operation_key
                ).encode()
            ).hexdigest()
            self._request(
                "/v1/subscriptions/" + subscription_id,
                fields={"cancel_at_period_end": "true", "proration_behavior": "none"},
                idempotency_key="ccfleet-monthly-cancel-" + key,
            )
            value = self.bound_subscription_identity(subscription_id, customer_id)
        _require(
            value.get("status") in terminal or value["cancel_at_period_end"] is True,
            "End-of-period cancellation could not be confirmed",
        )
        return {
            "subscription_id": subscription_id,
            "customer_id": customer_id,
            "merchant_id": self.settings.expected_account_id,
            "livemode": self.settings.livemode,
            "status": value["status"],
            "cancel_at_period_end": value["cancel_at_period_end"],
        }
