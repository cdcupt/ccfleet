"""Narrow hosted USD Checkout adapter; no Store, environment, or fulfillment writes.

API pin: documented current Endive. Checkout's allowed_payment_method_types
filters dynamic eligibility to card; no static payment_method_types is sent.
Alipay remains off pending merchant approval.

References: https://docs.stripe.com/api/versioning
https://docs.stripe.com/api/checkout/sessions/create
https://docs.stripe.com/checkout/fulfillment?payment-ui=stripe-hosted
https://docs.stripe.com/webhooks#verify-manually

Tests use synthetic transport responses, not actual Stripe transactions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from . import wallet

API_VERSION = "2026-09-30.endive"
API_ORIGIN = "https://api.stripe.com"
DEFAULT_AMOUNTS = (1000, 2000, 5000, 10000)
INTEGRATION_IDENTIFIER = "ccfleet-slot-credit-v1-qmxtvzar"
PRODUCT_NAME = "CC Fleet slot service credit"
PURPOSE = "ccfleet_slot_service_credit"
MAX_BODY = 1024 * 1024
TIMEOUT_S = 15
ID = re.compile(r"[A-Za-z0-9_]{1,200}\Z")


class StripeCheckoutError(ValueError):
    """Safe diagnostic: never contains credentials, raw replies or request bodies."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise StripeCheckoutError(message)


def _provider_id(value: Any, prefix: str) -> str:
    _require(isinstance(value, str) and value.startswith(prefix)
             and ID.fullmatch(value) is not None and len(value) > len(prefix),
             "invalid Stripe object identity")
    return value


def _key(value: Any, mode: str) -> None:
    _require(isinstance(value, str) and re.fullmatch(
        r"[sr]k_(test|live)_[A-Za-z0-9_]{8,256}", value) is not None,
        "Stripe server key is not configured")
    _require(value.startswith(("rk_" + mode + "_", "sk_" + mode + "_")),
             "Stripe key mode differs from configured mode")


def _signing_secret(value: Any) -> None:
    _require(isinstance(value, str) and re.fullmatch(
        r"whsec_[A-Za-z0-9_]{8,256}", value) is not None,
        "Stripe webhook signing is not configured")


def _origin(value: Any) -> str:
    _require(isinstance(value, str) and value.isascii()
             and not any(char.isspace() or ord(char) < 32 for char in value)
             and "\\" not in value, "invalid public HTTPS origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname is not None
                 and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parsed.hostname) is not None
                 and parsed.username is None and parsed.password is None and parsed.port is None
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    _require(valid, "invalid public HTTPS origin")
    return "https://" + str(parsed.hostname).lower()


@dataclass(frozen=True)
class Settings:
    mode: str
    expected_account_id: str
    api_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    # One stable public flow tag, never regenerated for an idempotent retry.
    integration_identifier: str = INTEGRATION_IDENTIFIER
    public_origin: str = "https://ccfleet.io"
    allowed_cents: tuple[int, ...] = DEFAULT_AMOUNTS

    def __post_init__(self) -> None:
        _require(isinstance(self.mode, str) and self.mode in {"test", "live"},
                 "explicit Stripe test/live mode required")
        _provider_id(self.expected_account_id, "acct_")
        _key(self.api_key, self.mode)
        _signing_secret(self.webhook_secret)
        _require(isinstance(self.integration_identifier, str) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.integration_identifier) is not None,
            "a stable Fleet integration identifier is required")
        _require(isinstance(self.allowed_cents, tuple) and bool(self.allowed_cents)
                 and all(type(value) is int and value in DEFAULT_AMOUNTS
                         for value in self.allowed_cents)
                 and len(set(self.allowed_cents)) == len(self.allowed_cents),
                 "top-up amounts must be a fixed approved USD preset subset")
        object.__setattr__(self, "public_origin", _origin(self.public_origin))

    @property
    def livemode(self) -> bool:
        return self.mode == "live"


@dataclass(frozen=True)
class TopupOrder:
    id: str
    account_id: str
    amount_cents: int

    def __post_init__(self) -> None:
        try:
            wallet.request_id(self.id)
            wallet.identity(self.account_id)
            wallet.positive_cents(self.amount_cents)
        except wallet.WalletError:
            raise StripeCheckoutError("invalid local top-up order") from None


@dataclass(frozen=True)
class HostedSession:
    id: str
    url: str = field(repr=False)


@dataclass(frozen=True)
class PaidReceipt:
    order_id: str
    account_id: str
    amount_cents: int
    merchant_id: str
    livemode: bool
    session_id: str
    payment_intent_id: str
    currency: str = "USD"
    payment_status: str = "paid"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PaymentReference:
    payment_intent_id: str
    order_id: str
    account_id: str
    amount_cents: int
    merchant_id: str
    livemode: bool
    currency: str = "USD"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_open(request: urllib.request.Request, *, timeout: float):
    """Fixed HTTPS requests only; never follow a redirect or ambient proxy."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(request, timeout=timeout)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StripeCheckoutError("ambiguous JSON object")
        result[key] = value
    return result


def _constant(_value):
    raise StripeCheckoutError("non-finite JSON value")


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_constant)
    except (ValueError, UnicodeDecodeError):
        raise StripeCheckoutError("invalid Stripe JSON response") from None
    _require(isinstance(value, dict), "Stripe response must be a JSON object")
    return value


def _hosted_url(value: Any, session_id: str) -> str:
    _require(isinstance(value, str) and value.isascii() and "\\" not in value
             and not any(char.isspace() or ord(char) < 32 for char in value),
             "invalid Stripe-hosted Checkout URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname == "checkout.stripe.com"
                 and parsed.username is None and parsed.password is None and parsed.port is None
                 and parsed.path in {"/c/pay/" + session_id, "/pay/" + session_id})
    except ValueError:
        valid = False
    _require(valid, "Checkout redirect is not the expected Stripe-hosted session")
    return value


class Client:
    """An explicitly configured client. Construction performs no network I/O."""

    def __init__(self, settings: Settings, *, opener: Callable = _https_open) -> None:
        _require(isinstance(settings, Settings) and callable(opener),
                 "validated Stripe settings and transport required")
        self.settings, self._open = settings, opener

    def _request(self, path: str, *, fields: Optional[Mapping[str, str]] = None,
                 idempotency_key: Optional[str] = None) -> dict[str, Any]:
        method = "POST" if fields is not None else "GET"
        permitted = (path == "/v1/checkout/sessions" if method == "POST" else
                     path == "/v1/account" or re.fullmatch(
                         r"/v1/payment_intents/pi_[A-Za-z0-9_]{1,197}"
                         r"|/v1/checkout/sessions/cs_(test|live)_[A-Za-z0-9_]{1,192}"
                         r"(?:\?expand%5B%5D=line_items)?", path) is not None)
        _require(permitted, "Stripe API operation is outside the Checkout adapter")
        _require(fields is None or not any(key == "payment_method_types"
                 or key.startswith("payment_method_types[") for key in fields),
                 "static Stripe payment methods are not allowed")
        body = urllib.parse.urlencode(fields).encode("ascii") if fields is not None else None
        headers = {"Authorization": "Bearer " + self.settings.api_key,
                   "Accept": "application/json", "Stripe-Version": API_VERSION,
                   "User-Agent": "CCFleetCheckout/1"}
        if fields is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            _require(isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 255,
                     "stable Stripe creation idempotency key required")
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(API_ORIGIN + path, data=body, method=method,
                                         headers=headers)
        try:
            with self._open(request, timeout=TIMEOUT_S) as response:
                _require(type(response.status) is int and 200 <= response.status < 300,
                         "Stripe request was refused")
                _require(response.geturl() == request.full_url,
                         "Stripe API redirect was refused")
                raw = response.read(MAX_BODY + 1)
                _require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BODY,
                         "Stripe response size is invalid")
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise StripeCheckoutError("Stripe HTTP request failed", uncertain=(
                method == "POST" and status >= 500)) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise StripeCheckoutError("Stripe connection could not be confirmed",
                                      uncertain=method == "POST") from None
        except StripeCheckoutError:
            raise StripeCheckoutError("Stripe response could not be confirmed",
                                      uncertain=method == "POST") from None
        try:
            return _json(raw)
        except StripeCheckoutError:
            raise StripeCheckoutError("Stripe response could not be confirmed",
                                      uncertain=method == "POST") from None

    def validate_account(self) -> str:
        account = self._request("/v1/account")
        _require(account.get("object") == "account"
                 and account.get("id") == self.settings.expected_account_id,
                 "Stripe account differs from the configured merchant")
        if self.settings.livemode:
            _require(account.get("charges_enabled") is True,
                     "Stripe live charges are not enabled")
        return self.settings.expected_account_id

    def _order(self, order: TopupOrder) -> None:
        _require(isinstance(order, TopupOrder)
                 and order.amount_cents in self.settings.allowed_cents,
                 "top-up amount is not an approved USD preset")

    def _metadata(self, order: TopupOrder) -> dict[str, str]:
        return {"ccfleet_order_id": order.id, "ccfleet_account_id": order.account_id,
                "ccfleet_merchant_id": self.settings.expected_account_id,
                "ccfleet_mode": self.settings.mode, "ccfleet_purpose": PURPOSE,
                "integration_identifier": self.settings.integration_identifier}

    def _session(self, value: dict[str, Any], order: TopupOrder,
                 session_id: Optional[str] = None) -> str:
        prefix = "cs_live_" if self.settings.livemode else "cs_test_"
        identity = _provider_id(value.get("id"), prefix)
        _require(session_id is None or identity == session_id, "Stripe session identity differs")
        _require(value.get("object") == "checkout.session" and value.get("mode") == "payment"
                 and value.get("ui_mode") == "hosted_page"
                 and value.get("livemode") is self.settings.livemode,
                 "Stripe session mode differs from the configured flow")
        _require(value.get("currency") == "usd"
                 and all(type(value.get(key)) is int and value[key] == order.amount_cents
                         for key in ("amount_total", "amount_subtotal")),
                 "Stripe session amount or currency differs from the local order")
        _require(value.get("client_reference_id") == order.id
                 and isinstance(value.get("metadata"), dict)
                 and all(value["metadata"].get(key) == expected
                         for key, expected in self._metadata(order).items()),
                 "Stripe session is not bound to the expected local account and order")
        _require(value.get("allowed_payment_method_types") == ["card"]
                 and value.get("payment_method_types") == ["card"]
                 and value.get("subscription") is None and value.get("setup_intent") is None,
                 "Stripe session contains an unapproved payment flow")
        tax, totals = value.get("automatic_tax"), value.get("total_details")
        _require(isinstance(tax, dict) and tax.get("enabled") is False
                 and isinstance(totals, dict)
                 and all(type(totals.get(key)) is int and totals[key] == 0
                         for key in ("amount_discount", "amount_shipping", "amount_tax")),
                 "Stripe session contains unapproved tax, shipping or discount changes")
        adaptive = value.get("adaptive_pricing")
        _require(adaptive is None or isinstance(adaptive, dict)
                 and adaptive.get("enabled") is False,
                 "Stripe Adaptive Pricing is not allowed")
        _require(value.get("currency_conversion") is None,
                 "Stripe currency conversion is not allowed")
        presentment = value.get("presentment_details")
        _require(presentment is None or isinstance(presentment, dict)
                 and presentment.get("presentment_currency") == "usd"
                 and type(presentment.get("presentment_amount")) is int
                 and presentment["presentment_amount"] == order.amount_cents,
                 "Stripe presentment differs from the fixed USD order")
        return identity

    def create_session(self, order: TopupOrder) -> HostedSession:
        """Create one hosted card-only session; never charge or credit a ledger here."""
        self._order(order)
        self.validate_account()
        origin = self.settings.public_origin
        fields = {"mode": "payment", "ui_mode": "hosted_page",
            "allowed_payment_method_types[0]": "card", "adaptive_pricing[enabled]": "false",
            "automatic_tax[enabled]": "false", "allow_promotion_codes": "false",
            "integration_identifier": self.settings.integration_identifier,
            "client_reference_id": order.id,
            "success_url": (origin
                            + "/account?checkout=returned&session_id={CHECKOUT_SESSION_ID}#wallet"),
            "cancel_url": origin + "/account?checkout=cancelled#wallet",
            "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(order.amount_cents),
            "line_items[0][price_data][product_data][name]": PRODUCT_NAME}
        for key, value in self._metadata(order).items():
            fields["metadata[" + key + "]"] = value
            fields["payment_intent_data[metadata][" + key + "]"] = value
        digest = hashlib.sha256((self.settings.mode + ":" + self.settings.expected_account_id
                                 + ":" + order.id).encode()).hexdigest()
        session = self._request("/v1/checkout/sessions", fields=fields,
                                idempotency_key="ccfleet-topup-" + digest)
        try:
            return self._open_session(session, order)
        except StripeCheckoutError:
            raise StripeCheckoutError("created Stripe Checkout session could not be confirmed",
                                      uncertain=True) from None

    def _open_session(self, session: dict[str, Any], order: TopupOrder,
                      session_id: Optional[str] = None) -> HostedSession:
        identity = self._session(session, order, session_id)
        _require(session.get("status") == "open" and session.get("payment_status") == "unpaid",
                 "Stripe Checkout session is not open and unpaid")
        return HostedSession(identity, _hosted_url(session.get("url"), identity))

    def retrieve_open_session(self, session_id: str, order: TopupOrder) -> HostedSession:
        """Reuse a bound open Session, even after Stripe's creation-key cache expires."""
        self._order(order)
        prefix = "cs_live_" if self.settings.livemode else "cs_test_"
        _provider_id(session_id, prefix)
        self.validate_account()
        return self._open_session(self._request("/v1/checkout/sessions/" + session_id),
                                  order, session_id)

    def retrieve_payment_reference(self, payment_intent_id: str) -> Optional[PaymentReference]:
        """Map our signed reversal events; absent integration metadata means unrelated."""
        _provider_id(payment_intent_id, "pi_")
        merchant = self.validate_account()
        intent = self._request("/v1/payment_intents/" + payment_intent_id)
        _require(intent.get("object") == "payment_intent" and intent.get("id") == payment_intent_id
                 and intent.get("livemode") is self.settings.livemode,
                 "Stripe PaymentIntent identity or mode differs")
        metadata = intent.get("metadata")
        if metadata is None:
            return None
        _require(isinstance(metadata, dict), "invalid Stripe PaymentIntent metadata")
        own = any(isinstance(key, str) and key.startswith("ccfleet_") for key in metadata)
        if (not own
                and metadata.get("integration_identifier") != self.settings.integration_identifier):
            return None
        try:
            order = TopupOrder(metadata.get("ccfleet_order_id"),
                               metadata.get("ccfleet_account_id"), intent.get("amount"))
        except StripeCheckoutError:
            raise StripeCheckoutError("partial Fleet PaymentIntent metadata") from None
        self._order(order)
        _require(all(metadata.get(key) == expected
                     for key, expected in self._metadata(order).items()),
                 "Fleet PaymentIntent metadata differs from the configured integration")
        _require(intent.get("currency") == "usd" and intent.get("payment_method_types") == ["card"],
                 "Fleet PaymentIntent currency or method differs")
        return PaymentReference(payment_intent_id, order.id, order.account_id,
                                order.amount_cents, merchant, self.settings.livemode)

    def retrieve_paid_session(self, session_id: str, order: TopupOrder) -> PaidReceipt:
        """Read a persisted session ID for webhook settlement; no return-URL credit."""
        self._order(order)
        prefix = "cs_live_" if self.settings.livemode else "cs_test_"
        _provider_id(session_id, prefix)
        merchant = self.validate_account()
        session = self._request("/v1/checkout/sessions/" + session_id + "?expand%5B%5D=line_items")
        self._session(session, order, session_id)
        _require(session.get("status") == "complete" and session.get("payment_status") == "paid",
                 "Stripe payment is not confirmed paid")
        intent = _provider_id(session.get("payment_intent"), "pi_")
        lines = session.get("line_items")
        _require(isinstance(lines, dict) and lines.get("object") == "list"
                 and lines.get("has_more") is False and isinstance(lines.get("data"), list)
                 and len(lines["data"]) == 1, "Stripe line items differ from the top-up order")
        item = lines["data"][0]
        _require(isinstance(item, dict) and type(item.get("quantity")) is int
                 and item["quantity"] == 1 and item.get("currency") == "usd"
                 and all(type(item.get(key)) is int and item[key] == order.amount_cents
                         for key in ("amount_total", "amount_subtotal")),
                 "Stripe line item amount differs from the top-up order")
        price = item.get("price")
        _require(isinstance(price, dict) and price.get("currency") == "usd"
                 and type(price.get("unit_amount")) is int
                 and price["unit_amount"] == order.amount_cents and price.get("recurring") is None,
                 "Stripe price is not the expected one-time USD credit")
        return PaidReceipt(order.id, order.account_id, order.amount_cents, merchant,
                           self.settings.livemode, session_id, intent)


def verify_webhook(raw_body: bytes, signature: str, secret: str, *, mode: str,
                   expected_account_id: Optional[str] = None, now: Optional[float] = None,
                   tolerance: int = 300) -> dict[str, Any]:
    """Authenticate original bytes only. Return data; never fulfill or alter credit."""
    _require(isinstance(raw_body, bytes) and 0 < len(raw_body) <= MAX_BODY,
             "invalid Stripe webhook body")
    _require(isinstance(mode, str) and mode in {"test", "live"},
             "explicit Stripe webhook mode required")
    _signing_secret(secret)
    if expected_account_id is not None:
        _provider_id(expected_account_id, "acct_")
    _require(isinstance(signature, str) and signature.isascii() and 0 < len(signature) <= 4096,
             "invalid Stripe signature header")
    _require(type(tolerance) is int and 1 <= tolerance <= 600,
             "invalid webhook timestamp tolerance")
    clock = time.time() if now is None else now
    _require(type(clock) in (int, float) and math.isfinite(clock) and clock >= 0,
             "invalid webhook verification clock")
    timestamps, signatures = [], []
    for part in signature.split(","):
        key, separator, value = part.strip().partition("=")
        _require(bool(separator) and bool(key), "malformed Stripe signature header")
        if key == "t":
            _require(re.fullmatch(r"[0-9]{1,12}", value) is not None,
                     "invalid Stripe signature timestamp")
            timestamps.append(value)
        elif key == "v1":
            _require(re.fullmatch(r"[0-9a-f]{64}", value) is not None,
                     "invalid Stripe v1 signature")
            signatures.append(value)
    _require(len(timestamps) == 1 and 1 <= len(signatures) <= 16,
             "Stripe signature timestamp or v1 signature is missing or ambiguous")
    _require(abs(clock - int(timestamps[0])) <= tolerance, "Stripe signature timestamp is stale")
    signed = timestamps[0].encode("ascii") + b"." + raw_body
    expected = hmac.new(secret.encode("ascii"), signed, hashlib.sha256).hexdigest()
    _require(any(hmac.compare_digest(expected, value) for value in signatures),
             "Stripe webhook signature verification failed")
    event = _json(raw_body)
    _provider_id(event.get("id"), "evt_")
    _require(event.get("object") == "event" and event.get("livemode") is (mode == "live")
             and isinstance(event.get("type"), str) and re.fullmatch(
                 r"[a-z0-9_.]{1,128}", event["type"]) is not None,
             "Stripe webhook envelope or mode is invalid")
    if event.get("account") is not None:
        _require(expected_account_id is not None and event["account"] == expected_account_id,
                 "Stripe webhook account differs from the configured merchant")
    return event
