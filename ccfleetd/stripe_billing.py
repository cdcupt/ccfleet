"""Hosted Checkout orchestration; no fulfillment from browser return URLs.

Local orders precede remote Sessions. Only authenticated raw webhooks plus
retrieved merchant objects can append paid credit or payment-review holds.
Construction is inert and tests use an injected synthetic checkout client.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from . import stripe_checkout, wallet
from .store import StoreError

PAID_EVENTS = frozenset({"checkout.session.completed", "checkout.session.async_payment_succeeded"})
REVIEW_EVENTS = frozenset({"charge.refunded", "charge.dispute.created", "charge.dispute.closed",
                           "refund.created", "refund.updated"})
# Stripe retains idempotency keys for at least 24 hours. An unresolved create
# stays within a conservative 23-hour window; after that, reconcile manually.
CREATE_RETRY_WINDOW = 23 * 60 * 60


class BillingError(ValueError):
    """Safe HTTP-boundary error; contains no provider body, key or payment URL."""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def _need(condition: Any) -> None:
    if not condition:
        raise BillingError("The payment could not be matched. Please retry later.")


class Billing:
    def __init__(self, store: Any, client: Any, *, sandbox_database: bool = False) -> None:
        if not isinstance(getattr(client, "settings", None), stripe_checkout.Settings):
            raise BillingError("Payment configuration is unavailable.")
        # This flag is a trusted server declaration for an isolated development
        # database, never an HTTP option. Matching test-mode event fields alone
        # must not turn a production user's wallet into test-funded service.
        if (type(sandbox_database) is not bool
                or (client.settings.mode == "test") != sandbox_database):
            raise BillingError("Test payments require an explicitly isolated sandbox database.")
        self.store, self.client = store, client
        self.settings = client.settings

    @staticmethod
    def _order(value: dict[str, Any]) -> stripe_checkout.TopupOrder:
        return stripe_checkout.TopupOrder(value["id"], value["account_id"], value["amount_cents"])

    def _match_order(self, value: dict[str, Any]) -> None:
        _need(value["merchant_id"] == self.settings.expected_account_id
              and type(value["livemode"]) is int
              and value["livemode"] == int(self.settings.livemode)
              and value["currency"] == "USD")

    def start_checkout(self, account_id: str, amount_cents: int, *,
                       idempotency_key: str, now: float) -> stripe_checkout.HostedSession:
        """Create once or recover a persisted Session; never add credit here."""
        if type(amount_cents) is not int or amount_cents not in self.settings.allowed_cents:
            raise BillingError("Choose a supported USD credit amount.", status_code=400)
        try:
            now = wallet.instant(now)
            order = self.store.create_wallet_topup(account_id, amount_cents,
                merchant_id=self.settings.expected_account_id, livemode=self.settings.livemode,
                idempotency_key=idempotency_key, now=now)
            self._match_order(order)
            if self.store.wallet_spending_blocked(account_id):
                raise BillingError("Credit needs payment review before another top-up.")
            bound = self.store.get_wallet_checkout(account_id, order["id"])
            local = self._order(order)
            if bound is not None:
                session = self.client.retrieve_open_session(bound, local)
                _need(session.id == bound)
            else:
                if not 0 <= now - order["created_at"] < CREATE_RETRY_WINDOW:
                    raise BillingError(
                        "This checkout needs operator review before another payment attempt.")
                session = self.client.create_session(local)
                self.store.bind_wallet_checkout(order["id"], session.id, now=now)
            # A review event can arrive while the provider request is in flight.
            # Do not hand out another payment link after that review is known.
            if self.store.wallet_spending_blocked(account_id):
                raise BillingError("Credit needs payment review before another top-up.")
            return session
        except BillingError:
            raise
        except (StoreError, wallet.WalletError, stripe_checkout.StripeCheckoutError,
                sqlite3.DatabaseError):
            raise BillingError("Checkout could not be confirmed. Please retry the same request.") \
                from None

    def _session_order(self, value: dict[str, Any]) -> dict[str, Any] | None:
        session_id = value.get("id")
        _need(isinstance(session_id, str))
        bound = self.store.wallet_topup_for_checkout(session_id)
        metadata = value.get("metadata")
        if metadata is None:
            _need(bound is None)
            return None
        _need(isinstance(metadata, dict))
        own = (metadata.get("integration_identifier") == self.settings.integration_identifier
               or any(isinstance(key, str) and key.startswith("ccfleet_") for key in metadata))
        if not own:
            _need(bound is None)
            return None
        expected = {"integration_identifier": self.settings.integration_identifier,
                    "ccfleet_purpose": stripe_checkout.PURPOSE,
                    "ccfleet_merchant_id": self.settings.expected_account_id,
                    "ccfleet_mode": self.settings.mode}
        _need(all(metadata.get(key) == item for key, item in expected.items()))
        try:
            order_id = wallet.request_id(metadata.get("ccfleet_order_id"))
            account_id = wallet.identity(metadata.get("ccfleet_account_id"))
        except wallet.WalletError:
            raise BillingError("The payment could not be matched. Please retry later.") from None
        order = self.store.get_wallet_topup(account_id, order_id)
        _need(order is not None)
        _need(bound is None or bound["id"] == order["id"])
        self._match_order(order)
        return order

    def handle_webhook(self, raw_body: bytes, signature: str, *, now: float) -> str:
        """Return a static result; caller maps BillingError.status_code to HTTP."""
        try:
            event = stripe_checkout.verify_webhook(raw_body, signature,
                self.settings.webhook_secret, mode=self.settings.mode,
                expected_account_id=self.settings.expected_account_id, now=now)
        except stripe_checkout.StripeCheckoutError:
            raise BillingError("The payment notification could not be verified.",
                               status_code=400) from None
        kind = event["type"]
        if kind not in PAID_EVENTS | REVIEW_EVENTS:
            return "ignored"
        try:
            data = event.get("data")
            _need(isinstance(data, dict) and isinstance(data.get("object"), dict))
            value = data["object"]
            digest = hashlib.sha256(raw_body).hexdigest()
            common = dict(event_id=event["id"], event_type=kind, body_sha256=digest, now=now)
            if kind in PAID_EVENTS:
                order = self._session_order(value)
                if order is None:
                    return "ignored"
                _need(value.get("object") == "checkout.session")
                if value.get("payment_status") == "unpaid":
                    return "ignored"
                _need(value.get("payment_status") == "paid")
                session_id = value.get("id")
                bound = self.store.get_wallet_checkout(order["account_id"], order["id"])
                _need(isinstance(session_id, str) and bound == session_id)
                receipt = self.client.retrieve_paid_session(session_id, self._order(order))
                self.store.settle_wallet_topup(order["id"], receipt.as_dict(), **common)
                return "credited"
            payment_id = value.get("payment_intent")
            expected_object = ("charge" if kind == "charge.refunded" else
                               "dispute" if kind.startswith("charge.dispute.") else "refund")
            _need(value.get("object") == expected_object)
            if isinstance(payment_id, dict):
                _need(payment_id.get("object") == "payment_intent")
                payment_id = payment_id.get("id")
            if payment_id is None:
                # Our hosted flow always has a PaymentIntent; unrelated legacy
                # charges/refunds can legitimately have no PaymentIntent.
                metadata = value.get("metadata")
                _need(metadata is None or isinstance(metadata, dict) and not any(
                    isinstance(key, str) and key.startswith("ccfleet_") for key in metadata)
                    and metadata.get("integration_identifier")
                    != self.settings.integration_identifier)
                return "ignored"
            reference = self.client.retrieve_payment_reference(payment_id)
            if reference is None:
                _need(self.store.wallet_topup_for_payment(payment_id) is None)
                return "ignored"
            verified = reference.as_dict()
            _need(verified.get("payment_intent_id") == payment_id
                  and verified.get("merchant_id") == self.settings.expected_account_id
                  and verified.get("livemode") is self.settings.livemode)
            self.store.hold_wallet_topup(payment_id, reference=verified, **common)
            return "held"
        except BillingError:
            raise
        except (StoreError, wallet.WalletError, stripe_checkout.StripeCheckoutError,
                sqlite3.DatabaseError):
            raise BillingError("The payment could not be confirmed. Please retry later.") from None
