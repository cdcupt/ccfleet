"""Persistent Stripe order/credit-once boundary; no network or card data.

Only a server-verified paid receipt reaches settlement. Browser return URLs do
not. Refund/dispute notifications put new spending on hold for manual review;
they never release slots, edit old entries or turn a balance negative.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Any

from . import wallet

ALLOWED_CENTS = (1000, 2000, 5000, 10000)
STRIPE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,199}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_topups (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents IN (1000,2000,5000,10000)),
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    merchant_id TEXT NOT NULL,
    livemode INTEGER NOT NULL CHECK (livemode IN (0,1)),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_topup_sessions (
    order_id TEXT PRIMARY KEY REFERENCES wallet_topups(id),
    session_id TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_topup_receipts (
    order_id TEXT PRIMARY KEY REFERENCES wallet_topups(id),
    session_id TEXT NOT NULL UNIQUE,
    payment_intent_id TEXT NOT NULL UNIQUE,
    wallet_entry_id TEXT NOT NULL UNIQUE REFERENCES wallet_entries(id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_topup_events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES wallet_topups(id),
    event_type TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_topup_review_holds (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    order_id TEXT NOT NULL REFERENCES wallet_topups(id),
    payment_intent_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wallet_topup_hold_payment
ON wallet_topup_review_holds(payment_intent_id, order_id);
CREATE TABLE IF NOT EXISTS wallet_topup_review_resolutions (
    hold_id TEXT PRIMARY KEY REFERENCES wallet_topup_review_holds(id),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('account','console_user')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
"""
for _table in (
    "wallet_topups",
    "wallet_topup_sessions",
    "wallet_topup_receipts",
    "wallet_topup_events",
    "wallet_topup_review_holds",
    "wallet_topup_review_resolutions",
):
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table}
BEGIN SELECT RAISE(ABORT, 'top-up records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS {_table}_no_delete BEFORE DELETE ON {_table}
BEGIN SELECT RAISE(ABORT, 'top-up records are immutable'); END;
"""


def _provider_id(value: Any, prefix: str) -> str:
    if (not isinstance(value, str) or not STRIPE_ID.fullmatch(value)
            or not value.startswith(prefix) or len(value) <= len(prefix)):
        raise wallet.WalletError("a valid provider object identity is required")
    return value


def _event(event_id: str, body_sha256: str) -> None:
    _provider_id(event_id, "evt_")
    if not isinstance(body_sha256, str) or not SHA256.fullmatch(body_sha256):
        raise wallet.WalletError("an exact verified event digest is required")


def spending_blocked(conn: sqlite3.Connection, account_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM wallet_topup_review_holds h "
            "LEFT JOIN wallet_topup_review_resolutions r ON r.hold_id=h.id "
            "WHERE h.account_id=? AND r.hold_id IS NULL LIMIT 1",
            (account_id,),
        ).fetchone()
        is not None
    )


class TopupStoreMixin:
    """Store-owned transactions; deliberately separate from historical payments."""

    def create_wallet_topup(
        self,
        account_id: str,
        amount_cents: int,
        *,
        merchant_id: str,
        livemode: bool,
        idempotency_key: str,
        now: float,
    ) -> dict[str, Any]:
        from .store import StoreError, WalletReplayMismatch

        try:
            if type(amount_cents) is not int or amount_cents not in ALLOWED_CENTS:
                raise wallet.WalletError("choose a supported USD top-up amount")
            _provider_id(merchant_id, "acct_")
            if type(livemode) is not bool:
                raise wallet.WalletError("an explicit provider mode is required")
            wallet.request_id(idempotency_key)
            wallet.instant(now)
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._write_txn() as conn:
            self._wallet_account(conn, account_id)
            expected = dict(
                account_id=account_id,
                amount_cents=amount_cents,
                currency="USD",
                merchant_id=merchant_id,
                livemode=int(livemode),
            )
            previous = conn.execute(
                "SELECT * FROM wallet_topups WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if previous is not None:
                if any(previous[key] != value for key, value in expected.items()):
                    raise WalletReplayMismatch("the top-up identity was already used differently")
                return dict(previous)
            if spending_blocked(conn, account_id):
                raise StoreError("account credit needs payment review before another top-up")
            if self._wallet_balance(conn, account_id) + amount_cents > wallet.MAX_CENTS:
                raise StoreError("the top-up would exceed the supported balance")
            order_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO wallet_topups VALUES (?,?,?,?,?,?,?,?)",
                (
                    order_id,
                    account_id,
                    amount_cents,
                    "USD",
                    merchant_id,
                    int(livemode),
                    idempotency_key,
                    now,
                ),
            )
            return dict(
                conn.execute("SELECT * FROM wallet_topups WHERE id=?", (order_id,)).fetchone()
            )

    def get_wallet_topup(self, account_id: str, order_id: str) -> dict[str, Any] | None:
        from .store import StoreError

        try:
            wallet.identity(order_id)
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._lock:
            self._wallet_account(self._conn, account_id)
            row = self._conn.execute(
                "SELECT * FROM wallet_topups WHERE id=? AND account_id=?", (order_id, account_id)
            ).fetchone()
        return dict(row) if row else None

    def get_wallet_checkout(self, account_id: str, order_id: str) -> str | None:
        """The owner's persisted Session ID, never a reason to create another Session."""
        from .store import StoreError

        try:
            wallet.identity(order_id)
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._lock:
            self._wallet_account(self._conn, account_id)
            row = self._conn.execute(
                "SELECT s.session_id FROM wallet_topup_sessions s "
                "JOIN wallet_topups t ON t.id=s.order_id WHERE t.id=? AND t.account_id=?",
                (order_id, account_id),
            ).fetchone()
        return row[0] if row else None

    def wallet_topup_for_checkout(self, session_id: str) -> dict[str, Any] | None:
        """Trusted webhook lookup; customer routes must use owner-scoped reads."""
        from .store import StoreError

        try:
            _provider_id(session_id, "cs_")
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._lock:
            row = self._conn.execute(
                "SELECT t.* FROM wallet_topups t JOIN wallet_topup_sessions s "
                "ON s.order_id=t.id WHERE s.session_id=?", (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def wallet_topup_for_payment(self, payment_intent_id: str) -> dict[str, Any] | None:
        """Recognize an already-bound payment even if later metadata is removed."""
        from .store import StoreError

        try:
            _provider_id(payment_intent_id, "pi_")
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._lock:
            row = self._conn.execute(
                "SELECT t.* FROM wallet_topups t WHERE t.id IN ("
                "SELECT order_id FROM wallet_topup_receipts WHERE payment_intent_id=? "
                "UNION SELECT order_id FROM wallet_topup_review_holds WHERE payment_intent_id=?)",
                (payment_intent_id, payment_intent_id),
            ).fetchone()
        return dict(row) if row else None

    def bind_wallet_checkout(self, order_id: str, session_id: str, *, now: float) -> None:
        from .store import StoreError, WalletReplayMismatch

        try:
            wallet.identity(order_id)
            _provider_id(session_id, "cs_")
            wallet.instant(now)
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._write_txn() as conn:
            order = conn.execute("SELECT * FROM wallet_topups WHERE id=?", (order_id,)).fetchone()
            if order is None:
                raise StoreError("no such top-up")
            previous = conn.execute(
                "SELECT * FROM wallet_topup_sessions WHERE order_id=?", (order_id,)
            ).fetchone()
            if previous:
                if previous["session_id"] != session_id:
                    raise WalletReplayMismatch(
                        "a different checkout is already bound to this order"
                    )
                return
            collision = conn.execute(
                "SELECT order_id FROM wallet_topup_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if collision:
                raise WalletReplayMismatch("the checkout already belongs to a different top-up")
            conn.execute(
                "INSERT INTO wallet_topup_sessions VALUES (?,?,?)", (order_id, session_id, now)
            )

    def settle_wallet_topup(
        self,
        order_id: str,
        receipt: dict[str, Any],
        *,
        event_id: str,
        event_type: str,
        body_sha256: str,
        now: float,
    ) -> dict[str, Any]:
        """Append credit once after caller verifies signature AND retrieves paid Session.

        Session/PaymentIntent/order uniqueness is independent of event uniqueness:
        completed and async-payment-success events must converge on one credit.
        """
        from .store import StoreError, WalletReplayMismatch

        try:
            wallet.identity(order_id)
            _event(event_id, body_sha256)
            wallet.instant(now)
            if event_type not in {
                "checkout.session.completed",
                "checkout.session.async_payment_succeeded",
            }:
                raise wallet.WalletError("this event does not settle a top-up")
            if not isinstance(receipt, dict):
                raise wallet.WalletError("a verified paid receipt is required")
            _provider_id(receipt.get("session_id"), "cs_")
            _provider_id(receipt.get("payment_intent_id"), "pi_")
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._write_txn() as conn:
            order = conn.execute("SELECT * FROM wallet_topups WHERE id=?", (order_id,)).fetchone()
            if order is None:
                raise StoreError("no such top-up")
            expected = {key: order[key] for key in ("account_id", "amount_cents", "merchant_id")}
            expected.update(
                order_id=order_id,
                currency="USD",
                livemode=bool(order["livemode"]),
                payment_status="paid",
            )
            if (
                any(receipt.get(key) != value for key, value in expected.items())
                or type(receipt.get("livemode")) is not bool
                or type(receipt.get("amount_cents")) is not int
            ):
                raise StoreError("the paid receipt does not match the local USD top-up")
            session = conn.execute(
                "SELECT session_id FROM wallet_topup_sessions WHERE order_id=?", (order_id,)
            ).fetchone()
            if session is None or session[0] != receipt["session_id"]:
                raise StoreError("the paid receipt does not match the bound checkout")
            self._wallet_payment_binding(conn, order_id, receipt["payment_intent_id"])
            event = conn.execute(
                "SELECT * FROM wallet_topup_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if event and (
                event["order_id"] != order_id
                or event["event_type"] != event_type
                or event["body_sha256"] != body_sha256
            ):
                raise WalletReplayMismatch(
                    "the provider event identity was already used differently"
                )
            previous = conn.execute(
                "SELECT * FROM wallet_topup_receipts WHERE order_id=?", (order_id,)
            ).fetchone()
            if previous:
                if (
                    previous["session_id"] != receipt["session_id"]
                    or previous["payment_intent_id"] != receipt["payment_intent_id"]
                ):
                    raise WalletReplayMismatch("a different payment already settled this top-up")
                entry = conn.execute(
                    "SELECT * FROM wallet_entries WHERE id=?", (previous["wallet_entry_id"],)
                ).fetchone()
            else:
                collision = conn.execute(
                    "SELECT order_id FROM wallet_topup_receipts "
                    "WHERE session_id=? OR payment_intent_id=?",
                    (receipt["session_id"], receipt["payment_intent_id"]),
                ).fetchone()
                if collision:
                    raise WalletReplayMismatch("the payment already belongs to a different top-up")
                if (
                    self._wallet_balance(conn, order["account_id"]) + order["amount_cents"]
                    > wallet.MAX_CENTS
                ):
                    raise StoreError(
                        "the paid top-up needs review because it exceeds the balance limit"
                    )
                entry_id = secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO wallet_entries (id,account_id,kind,delta_cents,currency,"
                    "actor_kind,actor_id,reason,idempotency_key,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        order["account_id"],
                        "stripe_topup",
                        order["amount_cents"],
                        "USD",
                        "provider",
                        "stripe:" + order["merchant_id"],
                        "Paid Fleet service credit",
                        # Exactly-once fulfillment is bound by the immutable
                        # receipt in this transaction. Do not expose a future
                        # ledger key derived from a public local order ID.
                        "stripe-topup:" + entry_id,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO wallet_topup_receipts VALUES (?,?,?,?,?)",
                    (order_id, receipt["session_id"], receipt["payment_intent_id"], entry_id, now),
                )
                entry = conn.execute(
                    "SELECT * FROM wallet_entries WHERE id=?", (entry_id,)
                ).fetchone()
            if not event:
                conn.execute(
                    "INSERT INTO wallet_topup_events VALUES (?,?,?,?,?)",
                    (event_id, order_id, event_type, body_sha256, now),
                )
            return dict(entry)

    def hold_wallet_topup(
        self,
        payment_intent_id: str,
        *,
        event_id: str,
        event_type: str,
        body_sha256: str,
        now: float,
        reference: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Hold a verified payment, including before its paid event is delivered.

        The caller retrieves the PaymentIntent through the configured merchant
        and supplies its verified reference. Without it, only an already-settled
        payment can be located. No credit, refund or slot mutation occurs here.
        """
        from .store import StoreError, WalletReplayMismatch

        try:
            _provider_id(payment_intent_id, "pi_")
            _event(event_id, body_sha256)
            wallet.instant(now)
            if event_type not in {
                "charge.refunded",
                "charge.dispute.created",
                "refund.created",
                "refund.updated",
                "charge.dispute.closed",
            }:
                raise wallet.WalletError("this event is not a payment-review event")
            if reference is not None and not isinstance(reference, dict):
                raise wallet.WalletError("a verified payment reference is required")
            if reference is not None:
                wallet.identity(reference.get("order_id"))
                wallet.identity(reference.get("account_id"))
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._write_txn() as conn:
            if reference is None:
                row = conn.execute(
                    "SELECT t.* FROM wallet_topups t JOIN wallet_topup_receipts r "
                    "ON r.order_id=t.id WHERE r.payment_intent_id=?",
                    (payment_intent_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM wallet_topups WHERE id=?",
                                   (reference.get("order_id"),)).fetchone()
            if row is None:
                raise StoreError("payment review needs a matched local top-up; retry later")
            if reference is not None:
                expected_reference = {key: row[key]
                                      for key in ("account_id", "amount_cents", "merchant_id")}
                expected_reference.update(order_id=row["id"], currency="USD",
                    livemode=bool(row["livemode"]), payment_intent_id=payment_intent_id)
                if (any(reference.get(key) != value for key, value in expected_reference.items())
                        or type(reference.get("livemode")) is not bool
                        or type(reference.get("amount_cents")) is not int):
                    raise StoreError("the payment reference does not match the local USD top-up")
            self._wallet_payment_binding(conn, row["id"], payment_intent_id)
            expected = {"order_id": row["id"], "payment_intent_id": payment_intent_id,
                        "event_type": event_type, "body_sha256": body_sha256}
            event = conn.execute("SELECT * FROM wallet_topup_events WHERE event_id=?",
                                 (event_id,)).fetchone()
            if event and any(event[key] != expected[key]
                             for key in ("order_id", "event_type", "body_sha256")):
                raise WalletReplayMismatch(
                    "the provider event identity was already used differently")
            previous = conn.execute(
                "SELECT * FROM wallet_topup_review_holds WHERE event_id=?", (event_id,)
            ).fetchone()
            if previous:
                if any(previous[key] != value for key, value in expected.items()):
                    raise WalletReplayMismatch(
                        "the payment-review event was already used differently"
                    )
                return dict(previous)
            hold_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO wallet_topup_review_holds VALUES (?,?,?,?,?,?,?,?)",
                (hold_id, row["account_id"], row["id"], payment_intent_id,
                 event_id, event_type, body_sha256, now),
            )
            if not event:
                conn.execute("INSERT INTO wallet_topup_events VALUES (?,?,?,?,?)",
                             (event_id, row["id"], event_type, body_sha256, now))
            return dict(
                conn.execute(
                    "SELECT * FROM wallet_topup_review_holds WHERE id=?", (hold_id,)
                ).fetchone()
            )

    @staticmethod
    def _wallet_payment_binding(conn: sqlite3.Connection, order_id: str,
                                payment_intent_id: str) -> None:
        """One payment per order and one order per payment, across holds and receipts."""
        from .store import WalletReplayMismatch

        for table in ("wallet_topup_receipts", "wallet_topup_review_holds"):
            conflict = conn.execute(
                "SELECT 1 FROM " + table + " WHERE "
                "(order_id=? AND payment_intent_id!=?) OR "
                "(payment_intent_id=? AND order_id!=?) LIMIT 1",
                (order_id, payment_intent_id, payment_intent_id, order_id),
            ).fetchone()
            if conflict:
                raise WalletReplayMismatch("the payment reference belongs to a different top-up")

    def wallet_spending_blocked(self, account_id: str) -> bool:
        with self._lock:
            self._wallet_account(self._conn, account_id)
            return spending_blocked(self._conn, account_id)

    def resolve_wallet_topup_hold(
        self,
        hold_id: str,
        *,
        actor_id: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
        now: float,
    ) -> dict[str, Any]:
        """Record a human review after any needed separate audited credit correction.

        This does not request a refund or move money. An operator must reconcile
        the provider outcome and the spendable balance before resolving a hold.
        """
        from .store import StoreError, WalletReplayMismatch

        try:
            wallet.identity(hold_id)
            wallet.identity(actor_id)
            wallet.reason(reason)
            wallet.request_id(idempotency_key)
            wallet.instant(now)
        except wallet.WalletError as exc:
            raise StoreError(str(exc)) from exc
        with self._write_txn() as conn:
            self._wallet_admin(conn, actor_id, actor_kind)
            hold = conn.execute(
                "SELECT * FROM wallet_topup_review_holds WHERE id=?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise StoreError("no such payment-review hold")
            expected = dict(
                hold_id=hold_id, actor_kind=actor_kind, actor_id=actor_id, reason=reason
            )
            previous = conn.execute(
                "SELECT * FROM wallet_topup_review_resolutions "
                "WHERE idempotency_key=? OR hold_id=?",
                (idempotency_key, hold_id),
            ).fetchone()
            if previous:
                if previous["idempotency_key"] != idempotency_key or any(
                    previous[key] != value for key, value in expected.items()
                ):
                    raise WalletReplayMismatch("the review identity was already used differently")
                return dict(previous)
            receipt = conn.execute(
                "SELECT 1 FROM wallet_topup_receipts WHERE order_id=? AND payment_intent_id=?",
                (hold["order_id"], hold["payment_intent_id"]),
            ).fetchone()
            if receipt is None:
                raise StoreError(
                    "wait for the matched payment receipt, then reconcile credit before resolving")
            conn.execute(
                "INSERT INTO wallet_topup_review_resolutions VALUES (?,?,?,?,?,?)",
                (hold_id, actor_kind, actor_id, reason, idempotency_key, now),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM wallet_topup_review_resolutions WHERE hold_id=?", (hold_id,)
                ).fetchone()
            )
