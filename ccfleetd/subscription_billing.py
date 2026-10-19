"""Per-slot recurring rental orchestration; no wallet credit or return-page fulfillment."""

from __future__ import annotations

import hashlib
from typing import Any

from . import stripe_checkout, stripe_subscriptions, wallet
from .store import StoreError, SubscriptionCheckoutPending, WalletQuoteChanged

PAID = {"checkout.session.completed", "checkout.session.async_payment_succeeded", "invoice.paid"}
INVOICES = {"invoice.payment_failed", "invoice.payment_action_required"}
LIFECYCLE = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}
REVIEW = {
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.closed",
    "refund.created",
    "refund.updated",
}
WEBHOOK_EVENTS = tuple(sorted(PAID | INVOICES | LIFECYCLE | REVIEW | {"checkout.session.expired"}))
CREATE_RETRY_WINDOW = 23 * 3600


class SubscriptionError(ValueError):
    def __init__(
        self,
        message: str = "Subscription could not be confirmed.",
        *,
        status_code: int = 503,
        code: str = "unconfirmed",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def need(condition: Any) -> None:
    if not condition:
        raise SubscriptionError()


class Billing:
    def __init__(
        self,
        store: Any,
        client: Any,
        *,
        sandbox_database: bool = False,
        portal_configuration_id: str,
    ):
        if (
            not isinstance(client.settings, stripe_checkout.Settings)
            or type(sandbox_database) is not bool
            or sandbox_database != (client.settings.mode == "test")
        ):
            raise SubscriptionError("Recurring test payments need an isolated sandbox database.")
        stripe_checkout._provider_id(portal_configuration_id, "bpc_")
        self.store, self.client = store, client
        self.settings = client.settings
        self.portal_configuration_id = portal_configuration_id
        self._cancel_cursor = None
        self._reconcile_cursor = None

    @staticmethod
    def _order(row: dict[str, Any]) -> stripe_subscriptions.Order:
        expiry = row.get("expires_at")
        expiry = int(row["created_at"]) + 3600 if expiry is None else int(expiry)
        return stripe_subscriptions.Order(
            row["id"],
            row["account_id"],
            row["slot_id"],
            row["node_id"],
            row["amount_cents"],
            expiry,
            row["machine_number"],
        )

    def _scope(self, row: dict[str, Any]) -> None:
        need(
            row["merchant_id"] == self.settings.expected_account_id
            and type(row["livemode"]) is int
            and row["livemode"] == int(self.settings.livemode)
            and row["currency"] == "USD"
        )

    def _customer(self, account_id: str, now: float) -> str:
        customer = self.store.prepare_subscription_customer(
            account_id,
            merchant_id=self.settings.expected_account_id,
            livemode=self.settings.livemode,
            now=now,
        )
        if customer["customer_id"] is None:
            need(0 <= now - customer["created_at"] < CREATE_RETRY_WINDOW)
            identity = self.client.create_customer(account_id, customer["id"])
            customer = self.store.bind_subscription_customer(
                account_id,
                merchant_id=self.settings.expected_account_id,
                livemode=self.settings.livemode,
                customer_id=identity,
                now=now,
            )
        return self.client.retrieve_customer(customer["customer_id"], account_id)

    def _known_customer(self, row: dict[str, Any]) -> str:
        self._scope(row)
        customer = self.store.get_subscription_customer(
            row["account_id"],
            merchant_id=self.settings.expected_account_id,
            livemode=self.settings.livemode,
        )
        need(customer is not None and customer["customer_id"] is not None)
        return customer["customer_id"]

    def start_checkout(
        self,
        account_id: str,
        node_id: str,
        *,
        expected_cents: int,
        monthly_consent: bool,
        idempotency_key: str,
        now: float,
        max_age_s: float,
    ) -> stripe_checkout.HostedSession:
        if monthly_consent is not True:
            raise SubscriptionError("Confirm the monthly recurring payment.", status_code=400)
        try:
            now = wallet.instant(now)
            self.client.validate_portal_configuration(self.portal_configuration_id)
            row = self.store.reserve_slot_subscription(
                account_id,
                node_id,
                expected_cents=expected_cents,
                idempotency_key=idempotency_key,
                monthly_consent=True,
                merchant_id=self.settings.expected_account_id,
                livemode=self.settings.livemode,
                now=now,
                max_age_s=max_age_s,
            )
            self._scope(row)
            need(
                not row.get("payment_review_required")
                and not self.store.wallet_spending_blocked(account_id)
            )
            customer_id = self._customer(account_id, now)
            local = self._order(row)
            if row.get("session_id"):
                return self.client.retrieve_open_session(row["session_id"], local, customer_id)
            # After the fresh-create window, only the exact original cached
            # request may be recovered; its identity and expiration never move.
            if now - row["created_at"] >= 1800:
                self._recover_unknown_session(row, customer_id, now)
                raise SubscriptionError(status_code=409, code="processing-required")
            need(0 <= now - row["created_at"] < 1800)
            self.store.mark_slot_subscription_unknown(row["id"], now=now)
            session = self.client.create_subscription_session(local, customer_id)
            self.store.attach_slot_subscription_session(
                row["id"], session_id=session.id, expires_at=local.expires_at, now=now
            )
            return session
        except SubscriptionCheckoutPending:
            raise SubscriptionError(status_code=409, code="pending-checkout") from None
        except WalletQuoteChanged:
            raise SubscriptionError(status_code=409, code="quote-changed") from None
        except SubscriptionError:
            raise
        except (StoreError, wallet.WalletError, stripe_checkout.StripeCheckoutError):
            raise SubscriptionError() from None

    def _recover_unknown_session(self, row: dict[str, Any], customer: str, now: float):
        if not 0 <= now - row["created_at"] < CREATE_RETRY_WINDOW:
            raise SubscriptionError(status_code=409, code="processing-required")
        self._scope(row)
        local = self._order(row)
        self.store.mark_slot_subscription_unknown(row["id"], now=now)
        value = self.client.recover_subscription_session(local, customer)
        self.store.attach_slot_subscription_session(
            row["id"], session_id=value["id"], expires_at=local.expires_at, now=now
        )
        return self.store.get_slot_subscription(row["id"])

    def portal(self, account_id: str) -> str:
        try:
            rows = self.store.list_slot_subscriptions(account_id)
            need(
                not any(
                    row.get("payment_review_required") or row.get("cancel_required") for row in rows
                )
            )
            customer = self.store.get_subscription_customer(
                account_id,
                merchant_id=self.settings.expected_account_id,
                livemode=self.settings.livemode,
            )
            need(
                customer is not None
                and customer["customer_id"] is not None
                and any(
                    row.get("subscription_id")
                    for row in self.store.list_slot_subscriptions(account_id)
                )
            )
            return self.client.create_portal(
                customer["customer_id"], account_id, self.portal_configuration_id
            )
        except SubscriptionError:
            raise
        except (StoreError, stripe_checkout.StripeCheckoutError):
            raise SubscriptionError() from None

    def _bind(
        self, row: dict[str, Any], now: float
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        customer = self._known_customer(row)
        need(row.get("session_id") is not None)
        session = self.client.retrieve_session(row["session_id"], self._order(row), customer)
        need(session.get("status") == "complete" and session.get("payment_status") == "paid")
        subscription_id = stripe_checkout._provider_id(session.get("subscription"), "sub_")
        subscription = self.client.retrieve_subscription(
            subscription_id, self._order(row), customer
        )
        row = self.store.bind_slot_subscription(
            row["id"],
            session_id=session["id"],
            subscription_id=subscription_id,
            customer_id=customer,
            now=now,
        )
        return row, subscription, session

    def _subscription_order(self, subscription_id: str, now: float) -> tuple[Any, Any]:
        bound = self.store.slot_subscription_for_provider(subscription_id)
        reference = self.client.subscription_reference(subscription_id)
        if reference is None:
            need(bound is None)
            return None, None
        if bound is None:
            identifier = reference["metadata"].get("ccfleet_order_id")
            need(isinstance(identifier, str))
            bound = self.store.get_slot_subscription(identifier)
            need(bound is not None)
        bound, subscription, _ = self._bind(bound, now)
        need(bound["subscription_id"] == subscription_id)
        return bound, subscription

    def _paid(
        self, row: dict[str, Any], invoice_id: str, event_id: str, digest: str, now: float
    ) -> None:
        receipt = self.client.retrieve_paid_invoice(
            invoice_id, row["subscription_id"], row["customer_id"], self._order(row)
        )
        need(
            receipt["order_id"] == row["id"]
            and receipt["account_id"] == row["account_id"]
            and receipt["slot_id"] == row["slot_id"]
        )
        keys = (
            "invoice_id",
            "subscription_id",
            "customer_id",
            "merchant_id",
            "livemode",
            "currency",
            "amount_cents",
            "period_start_at",
            "period_end_at",
            "paid",
            "payment_intent_id",
        )
        self.store.record_slot_subscription_invoice(
            row["id"],
            **{key: receipt[key] for key in keys},
            event_id=event_id,
            body_sha256=digest,
            now=now,
        )
        refreshed = self.store.get_slot_subscription(row["id"])
        if refreshed.get("payment_review_required"):
            self._cancel_bound(refreshed, now=now)

    def handle_webhook(self, raw_body: bytes, signature: str, *, now: float) -> str:
        try:
            event = stripe_checkout.verify_webhook(
                raw_body,
                signature,
                self.settings.webhook_secret,
                mode=self.settings.mode,
                expected_account_id=self.settings.expected_account_id,
                now=now,
            )
        except stripe_checkout.StripeCheckoutError:
            raise SubscriptionError(
                "Subscription notification signature is invalid.", status_code=400
            ) from None
        try:
            kind = event["type"]
            if kind not in WEBHOOK_EVENTS:
                return "ignored"
            data = event.get("data", {}).get("object")
            need(isinstance(data, dict))
            digest = hashlib.sha256(raw_body).hexdigest()
            if kind.startswith("checkout.session."):
                bound = self.store.slot_subscription_for_session(data.get("id"))
                if bound is None:
                    tags = data.get("metadata")
                    if (
                        not isinstance(tags, dict)
                        or tags.get("ccfleet_purpose") != stripe_subscriptions.PURPOSE
                    ):
                        return "ignored"
                    bound = self.store.get_slot_subscription(tags.get("ccfleet_order_id"))
                    need(bound is not None)
                    if bound.get("session_id") is None:
                        # A lost create response must not orphan a paid contract.
                        # Event metadata is only a lookup hint: retrieve the exact
                        # Session and validate every saved order/customer field.
                        need(
                            kind
                            in {
                                "checkout.session.completed",
                                "checkout.session.async_payment_succeeded",
                            }
                        )
                        self._scope(bound)
                        local = self._order(bound)
                        verified = self.client.retrieve_session(
                            data.get("id"), local, self._known_customer(bound)
                        )
                        need(
                            verified.get("id") == data.get("id")
                            and verified.get("status") == "complete"
                            and verified.get("payment_status") == "paid"
                        )
                        bound = self.store.attach_slot_subscription_session(
                            bound["id"],
                            session_id=verified["id"],
                            expires_at=local.expires_at,
                            now=now,
                        )
                    need(bound.get("session_id") == data.get("id"))
                self._scope(bound)
                if kind == "checkout.session.expired":
                    customer = self._known_customer(bound)
                    proof = self.client.expire_unpaid_session(
                        bound["session_id"], self._order(bound), customer
                    )
                    if proof is not None:
                        self.store.close_slot_subscription(bound["id"], proof=proof, now=now)
                        return "closed"
                    return "ignored"
                row, _, session = self._bind(bound, now)
                # A Checkout event always refers to its own first invoice; the
                # subscription's latest invoice changes on every later renewal.
                invoice_id = stripe_checkout._provider_id(session.get("invoice"), "in_")
                self._paid(row, invoice_id, event["id"], digest, now)
                return "paid"
            if kind in REVIEW:
                subscription_id = self.client.review_subscription(kind, data)
                if subscription_id is None:
                    return "ignored"
                row = self.store.slot_subscription_for_provider(subscription_id)
                if row is None:
                    row, _ = self._subscription_order(subscription_id, now)
                if row is None:
                    return "ignored"
                self._scope(row)
                self.store.hold_slot_subscription(
                    row["id"],
                    subscription_id=subscription_id,
                    event_id=event["id"],
                    event_type=kind,
                    body_sha256=digest,
                    now=now,
                )
                self._cancel_bound(row, now=now)
                return "held"
            if kind.startswith("invoice."):
                subscription_id = self.client.invoice_subscription(data.get("id"))
                if subscription_id is None:
                    return "ignored"
                row, subscription = self._subscription_order(subscription_id, now)
                if row is None:
                    return "ignored"
                if kind == "invoice.paid":
                    self._paid(row, data["id"], event["id"], digest, now)
                    return "paid"
            else:
                row = self.store.slot_subscription_for_provider(data.get("id"))
                if row is not None and row.get("payment_review_required"):
                    self._cancel_bound(row, now=now)
                    return "updated"
                row, subscription = self._subscription_order(data.get("id"), now)
                if row is None:
                    return "ignored"
            self.store.update_slot_subscription_state(
                row["id"],
                subscription_id=row["subscription_id"],
                status=subscription["status"],
                cancel_at_period_end=subscription["cancel_at_period_end"],
                now=now,
            )
            return "updated"
        except SubscriptionError:
            raise
        except (
            StoreError,
            wallet.WalletError,
            stripe_checkout.StripeCheckoutError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            raise SubscriptionError() from None

    def reconcile_pending(self, *, now: float, limit: int = 20) -> int:
        """Recover the verified latest invoice, or close a proved unpaid Session.

        This bounded current-state check does not claim historical invoice backfill.
        No access is inferred from a return URL or a local reservation timeout.
        """
        count = 0
        pending = self.store.pending_slot_subscription_cancellations(
            limit=limit, after_order_id=self._cancel_cursor
        )
        if not pending and self._cancel_cursor is not None:
            self._cancel_cursor = None
            pending = self.store.pending_slot_subscription_cancellations(limit=limit)
        for row in pending:
            self._cancel_cursor = row["id"]
            try:
                self._cancel_bound(row, now=now)
            except (SubscriptionError, StoreError, stripe_checkout.StripeCheckoutError):
                continue
        rows = self.store.list_slot_subscription_reconciliation(
            limit=limit, after_order_id=self._reconcile_cursor
        )
        if not rows and self._reconcile_cursor is not None:
            self._reconcile_cursor = None
            rows = self.store.list_slot_subscription_reconciliation(limit=limit)
        for row in rows:
            self._reconcile_cursor = row["id"]
            try:
                customer = self._known_customer(row)
                if row.get("session_id") is None:
                    row = self._recover_unknown_session(row, customer, now)
                session = self.client.retrieve_session(
                    row["session_id"], self._order(row), customer
                )
                if session.get("status") == "complete" and session.get("payment_status") == "paid":
                    row, subscription, _ = self._bind(row, now)
                    self.store.update_slot_subscription_state(
                        row["id"],
                        subscription_id=row["subscription_id"],
                        status=subscription["status"],
                        cancel_at_period_end=subscription["cancel_at_period_end"],
                        now=now,
                    )
                    invoice = stripe_checkout._provider_id(
                        subscription.get("latest_invoice"), "in_"
                    )
                    digest = hashlib.sha256(
                        (
                            "server-reconcile:"
                            + self.settings.expected_account_id
                            + ":"
                            + self.settings.mode
                            + ":"
                            + row["id"]
                            + ":"
                            + invoice
                        ).encode()
                    ).hexdigest()
                    self._paid(row, invoice, "evt_reconcile_" + digest, digest, now)
                    continue
                if row.get("expires_at") is None or row["expires_at"] > now:
                    continue
                proof = self.client.expire_unpaid_session(
                    row["session_id"], self._order(row), customer
                )
                if proof is not None:
                    self.store.close_slot_subscription(row["id"], proof=proof, now=now)
                    count += 1
            except (SubscriptionError, StoreError, stripe_checkout.StripeCheckoutError):
                # Preserve ambiguous/payable reservations for a later verified attempt.
                continue
        return count

    def _cancel_bound(self, row: dict[str, Any], *, now: float, operation_id=None) -> None:
        self._scope(row)
        need(row.get("subscription_id") is not None and row.get("customer_id") is not None)
        current = self.client.bound_subscription_identity(
            row["subscription_id"], row["customer_id"]
        )
        terminal = {"canceled", "incomplete_expired"}
        if current.get("status") in terminal or current.get("cancel_at_period_end") is True:
            proof = {
                "subscription_id": row["subscription_id"],
                "customer_id": row["customer_id"],
                "merchant_id": self.settings.expected_account_id,
                "livemode": self.settings.livemode,
                "status": current["status"],
                "cancel_at_period_end": current["cancel_at_period_end"],
            }
        else:
            self.store.require_slot_subscription_cancellation(row["id"], now=now)
            attempt = self.store.next_slot_subscription_cancel_attempt(row["id"], now=now)
            proof = self.client.cancel_after_paid_period(
                row["subscription_id"],
                row["customer_id"],
                operation_key=row["id"] + "-cancel-" + str(attempt),
            )
        self.store.confirm_slot_subscription_cancellation(
            row["id"], proof=proof, operation_id=operation_id, now=now
        )

    def cancel(self, account_id: str, order_id: str, *, idempotency_key: str, now: float) -> bool:
        """Save an owned cancellation intent even while enrollment or spending is paused."""
        try:
            row = self.store.get_slot_subscription(order_id, account_id=account_id)
            need(row is not None and row.get("subscription_id") is not None)
            self._scope(row)
            operation = self.store.prepare_slot_subscription_cancellation(
                account_id, order_id, idempotency_key=idempotency_key, now=now
            )
            try:
                self._cancel_bound(row, now=now, operation_id=operation["id"])
                return True
            except (SubscriptionError, StoreError, stripe_checkout.StripeCheckoutError):
                # Intent and retry marker survive uncertain provider state; no false success.
                return False
        except SubscriptionError:
            raise
        except (StoreError, wallet.WalletError):
            raise SubscriptionError(
                "The owned monthly plan could not be confirmed.", status_code=400
            ) from None

    def resume_checkout(
        self, account_id: str, order_id: str, *, now: float, max_age_s: float
    ) -> Any:
        row = self.store.get_slot_subscription(order_id, account_id=account_id)
        need(row is not None and row["status"] != "closed")
        self._scope(row)
        need(
            not row.get("payment_review_required")
            and not self.store.wallet_spending_blocked(account_id)
        )
        if row.get("claim_id") or row.get("subscription_id"):
            return None
        if row.get("session_id") is None:
            if now - row["created_at"] < 1800:
                return self.start_checkout(
                    account_id,
                    row["node_id"],
                    expected_cents=row["amount_cents"],
                    monthly_consent=True,
                    idempotency_key=row["idempotency_key"],
                    now=now,
                    max_age_s=max_age_s,
                )
            row = self._recover_unknown_session(row, self._known_customer(row), now)
        customer = self._known_customer(row)
        value = self.client.retrieve_session(row["session_id"], self._order(row), customer)
        if value.get("status") == "complete":
            return None
        return self.client.retrieve_open_session(row["session_id"], self._order(row), customer)

    def cancel_checkout(
        self, account_id: str, order_id: str, *, idempotency_key: str, now: float
    ) -> str:
        row = self.store.get_slot_subscription(order_id, account_id=account_id)
        need(row is not None)
        self._scope(row)
        if row.get("session_id") is None:
            # An unknown create cannot be proved unpaid from a local timeout.
            try:
                row = self._recover_unknown_session(row, self._known_customer(row), now)
            except (SubscriptionError, StoreError, stripe_checkout.StripeCheckoutError):
                return "pending"
        customer = self._known_customer(row)
        try:
            proof = self.client.expire_unpaid_session(row["session_id"], self._order(row), customer)
        except stripe_checkout.StripeCheckoutError:
            proof = None
        if proof is not None:
            self.store.close_slot_subscription(row["id"], proof=proof, now=now)
            return "closed"
        # A successful payment can win the expiry race. Keep its paid month and
        # cancel future renewal only because this authenticated owner requested it.
        value = self.client.retrieve_session(row["session_id"], self._order(row), customer)
        if value.get("status") == "complete" and value.get("payment_status") == "paid":
            row, _, _ = self._bind(row, now)
            return (
                "paid-cancelled"
                if self.cancel(account_id, row["id"], idempotency_key=idempotency_key, now=now)
                else "pending"
            )
        return "pending"
