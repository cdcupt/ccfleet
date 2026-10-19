"""Reserved per-slot subscriptions and immutable paid periods; no provider I/O.

The caller verifies provider objects before passing normalized receipts here.
Local clocks never release a possibly payable reservation. Existing wallet
tables are neither migrated nor used to grant subscription entitlements.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any, Optional

from . import slots, wallet, wallet_topups

CONSENT_VERSION = "monthly-slot-v1"
RELEASE_BLOCKED = (
    "Cancel the monthly subscription first; keep this slot until its paid period ends."
)
TERMINAL = frozenset({"canceled", "incomplete_expired"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_subscription_customers (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    merchant_id TEXT NOT NULL,
    livemode INTEGER NOT NULL CHECK (livemode IN (0,1)),
    customer_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE (account_id, merchant_id, livemode),
    UNIQUE (merchant_id, livemode, customer_id)
);
CREATE TABLE IF NOT EXISTS slot_subscription_orders (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    node_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    machine_number INTEGER NOT NULL CHECK (machine_number > 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    merchant_id TEXT NOT NULL,
    livemode INTEGER NOT NULL CHECK (livemode IN (0,1)),
    offer_snapshot TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    consent_version TEXT NOT NULL CHECK (consent_version = 'monthly-slot-v1'),
    consented_at REAL NOT NULL,
    billing_interval TEXT NOT NULL CHECK (billing_interval = 'month'),
    report_max_age_s REAL NOT NULL CHECK (report_max_age_s > 0 AND report_max_age_s <= 86400),
    status TEXT NOT NULL CHECK (status IN ('reserved','checkout_open','unknown','active','closed')),
    provider_status TEXT NOT NULL DEFAULT '',
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0 CHECK (cancel_at_period_end IN (0,1)),
    cancel_required INTEGER NOT NULL DEFAULT 0 CHECK (cancel_required IN (0,1)),
    cancel_attempt_no INTEGER NOT NULL DEFAULT 0 CHECK (cancel_attempt_no >= 0),
    cancel_checked_at REAL NOT NULL DEFAULT 0,
    claim_id TEXT NOT NULL DEFAULT '',
    claim_generation INTEGER,
    closed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_slot_subscription_open_slot
ON slot_subscription_orders(slot_id) WHERE status != 'closed';
CREATE INDEX IF NOT EXISTS ix_slot_subscription_orders_account
ON slot_subscription_orders(account_id, created_at);
CREATE TABLE IF NOT EXISTS slot_subscription_sessions (
    order_id TEXT PRIMARY KEY REFERENCES slot_subscription_orders(id),
    session_id TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_subscriptions (
    order_id TEXT PRIMARY KEY REFERENCES slot_subscription_orders(id),
    subscription_id TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_subscription_invoices (
    invoice_id TEXT PRIMARY KEY,
    payment_intent_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL REFERENCES slot_subscription_orders(id),
    subscription_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    claim_generation INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    merchant_id TEXT NOT NULL,
    livemode INTEGER NOT NULL CHECK (livemode IN (0,1)),
    period_start_at REAL NOT NULL,
    period_end_at REAL NOT NULL CHECK (period_end_at > period_start_at),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_subscription_events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES slot_subscription_orders(id),
    invoice_id TEXT NOT NULL REFERENCES slot_subscription_invoices(invoice_id),
    body_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_subscription_holds (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES slot_subscription_orders(id),
    subscription_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_subscription_hold_resolutions (
    hold_id TEXT PRIMARY KEY REFERENCES slot_subscription_holds(id),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('account','console_user')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_subscription_cancellations (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    order_id TEXT NOT NULL REFERENCES slot_subscription_orders(id),
    subscription_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_slot_subscription_cancellations_order
ON slot_subscription_cancellations(order_id, completed_at);
"""
for _table in (
    "slot_subscription_sessions",
    "slot_subscriptions",
    "slot_subscription_invoices",
    "slot_subscription_events",
    "slot_subscription_holds",
    "slot_subscription_hold_resolutions",
):
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table}
BEGIN SELECT RAISE(ABORT, 'subscription records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS {_table}_no_delete BEFORE DELETE ON {_table}
BEGIN SELECT RAISE(ABORT, 'subscription records are immutable'); END;
"""


_ORDER_FIXED = (
    "id",
    "account_id",
    "node_id",
    "slot_id",
    "machine_number",
    "amount_cents",
    "currency",
    "merchant_id",
    "livemode",
    "offer_snapshot",
    "idempotency_key",
    "created_at",
    "consent_version",
    "consented_at",
    "billing_interval",
    "report_max_age_s",
)
SCHEMA += (
    "CREATE TRIGGER IF NOT EXISTS slot_subscription_orders_no_rewrite "
    "BEFORE UPDATE ON slot_subscription_orders WHEN "
    + " OR ".join("OLD." + field + " IS NOT NEW." + field for field in _ORDER_FIXED)
    + " OR (OLD.claim_id!='' AND OLD.claim_id IS NOT NEW.claim_id) "
    "OR (OLD.claim_generation IS NOT NULL AND "
    "OLD.claim_generation IS NOT NEW.claim_generation) "
    "BEGIN SELECT RAISE(ABORT, 'subscription order terms are immutable'); END;"
    "CREATE TRIGGER IF NOT EXISTS slot_subscription_orders_no_delete "
    "BEFORE DELETE ON slot_subscription_orders "
    "BEGIN SELECT RAISE(ABORT, 'subscription order terms are immutable'); END;"
)
SCHEMA += (
    "CREATE TRIGGER IF NOT EXISTS slot_subscription_cancellations_no_rewrite "
    "BEFORE UPDATE ON slot_subscription_cancellations WHEN "
    + " OR ".join(
        "OLD." + field + " IS NOT NEW." + field
        for field in (
            "id",
            "account_id",
            "order_id",
            "subscription_id",
            "customer_id",
            "idempotency_key",
            "created_at",
        )
    )
    + " OR (OLD.completed_at IS NOT NULL AND OLD.completed_at IS NOT NEW.completed_at) "
    "BEGIN SELECT RAISE(ABORT, 'subscription cancellation identity is immutable'); END;"
    "CREATE TRIGGER IF NOT EXISTS slot_subscription_cancellations_no_delete "
    "BEFORE DELETE ON slot_subscription_cancellations "
    "BEGIN SELECT RAISE(ABORT, 'subscription cancellation identity is immutable'); END;"
)


def _error(message: str) -> None:
    from .store import StoreError

    raise StoreError(message)


def _check(function, *args):
    from .store import StoreError

    try:
        return function(*args)
    except wallet.WalletError as exc:
        raise StoreError(str(exc)) from exc


def _provider(value: Any, prefix: str) -> str:
    return _check(wallet_topups._provider_id, value, prefix)


def _scope(merchant_id: str, livemode: bool) -> None:
    _provider(merchant_id, "acct_")
    if type(livemode) is not bool:
        _error("an explicit subscription provider mode is required")


def _mismatch(message: str) -> None:
    from .store import WalletReplayMismatch

    raise WalletReplayMismatch(message)


def reservation_blocks(conn: sqlite3.Connection, slot_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM slot_subscription_orders WHERE slot_id=? AND status!='closed' LIMIT 1",
            (slot_id,),
        ).fetchone()
        is not None
    )


def release_allowed(conn: sqlite3.Connection, slot_id: str, now: float) -> bool:
    rows = conn.execute(
        "SELECT o.status,o.provider_status,b.subscription_id,"
        "(SELECT MAX(i.period_end_at) FROM slot_subscription_invoices i "
        "WHERE i.order_id=o.id) AS paid_end FROM slot_subscription_orders o "
        "LEFT JOIN slot_subscriptions b ON b.order_id=o.id "
        "WHERE o.slot_id=? AND o.status!='closed'",
        (slot_id,),
    ).fetchall()
    return all(
        row["status"] != "unknown"
        and row["subscription_id"] is not None
        and row["provider_status"] in TERMINAL
        and (row["paid_end"] is None or now >= row["paid_end"])
        for row in rows
    )


def require_release_allowed(conn: sqlite3.Connection, slot_id: str, now: float) -> None:
    from .store import SubscriptionReleaseBlocked

    if not release_allowed(conn, slot_id, now):
        raise SubscriptionReleaseBlocked(RELEASE_BLOCKED)


def retire_released(conn: sqlite3.Connection, slot_id: str, now: float) -> None:
    """Retire only after a confirmed wipe and an already terminal paid contract."""
    slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    if (
        slot is None
        or slot["state"] != slots.FREE
        or slot["held_by"] is not None
        or slot["present"] != 0
        or slot["gateway_claim_id"]
        or not release_allowed(conn, slot_id, now)
    ):
        return
    conn.execute(
        "UPDATE slot_subscription_orders SET status='closed',"
        "closed_at=COALESCE(closed_at,?) WHERE slot_id=? AND claim_id!='' "
        "AND status!='closed'",
        (now, slot_id),
    )


def _order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM slot_subscription_orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        _error("unknown slot subscription order")
    return row


def _view(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT o.*,s.session_id,s.expires_at,b.subscription_id,b.customer_id,"
        "(SELECT MAX(i.period_end_at) FROM slot_subscription_invoices i "
        "WHERE i.order_id=o.id) AS period_end_at,"
        "EXISTS (SELECT 1 FROM slot_subscription_holds h "
        "LEFT JOIN slot_subscription_hold_resolutions r ON r.hold_id=h.id "
        "WHERE h.order_id=o.id AND r.hold_id IS NULL) AS payment_review_required "
        "FROM slot_subscription_orders o LEFT JOIN slot_subscription_sessions s ON s.order_id=o.id "
        "LEFT JOIN slot_subscriptions b ON b.order_id=o.id WHERE o.id=?",
        (order_id,),
    ).fetchone()
    if row is None:
        _error("unknown slot subscription order")
    value = dict(row)
    value["payment_review_required"] = bool(value["payment_review_required"])
    value["cancel_required"] = bool(value["cancel_required"])
    return value


def _bound_order(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    row = _view(conn, order_id)
    if row["subscription_id"] is None or row["customer_id"] is None:
        _error("a locally bound subscription and customer are required for cancellation")
    return row


def _require_cancellation(conn: sqlite3.Connection, order_id: str, now: float) -> None:
    conn.execute(
        "UPDATE slot_subscription_orders SET cancel_required=1,cancel_checked_at=? "
        "WHERE id=? AND cancel_required=0",
        (now, order_id),
    )


def _invoice_view(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["fulfillment_state"] = "granted" if value["claim_id"] else "review_required"
    return value


def _claim_available(conn: sqlite3.Connection, order: Mapping[str, Any], now: float) -> None:
    """Check service availability without changing the customer's fixed monthly quote."""
    from .store import NoSlotAvailable

    node = conn.execute("SELECT * FROM nodes WHERE id=?", (order["node_id"],)).fetchone()
    declared = conn.execute("SELECT * FROM slots WHERE node_id=?", (order["node_id"],)).fetchall()
    heartbeat = conn.execute(
        "SELECT ts FROM heartbeats WHERE node_id=? ORDER BY id DESC LIMIT 1", (order["node_id"],)
    ).fetchone()
    if (
        node is None
        or node["enabled"] != 1
        or node["capacity"] != 1
        or node["reserved_for"] not in (None, order["account_id"])
        or len(declared) != 1
        or declared[0]["id"] != order["slot_id"]
        or declared[0]["kind"] != slots.MACHINE_SLOT
        or declared[0]["state"] != slots.FREE
        or declared[0]["present"] != 0
        or heartbeat is None
    ):
        raise NoSlotAvailable("the reserved monthly slot cannot currently be delivered")
    for observed in (heartbeat["ts"], declared[0]["reported_at"]):
        if (
            type(observed) not in (int, float)
            or not 0 <= now - observed <= order["report_max_age_s"]
        ):
            raise NoSlotAvailable("the reserved monthly slot has no fresh service report")


def _service_failure_hold(conn: sqlite3.Connection, order: Mapping[str, Any], now: float) -> None:
    existing = conn.execute(
        "SELECT 1 FROM slot_subscription_holds h "
        "LEFT JOIN slot_subscription_hold_resolutions r ON r.hold_id=h.id "
        "WHERE h.order_id=? AND h.event_type='slot.service_unavailable' "
        "AND r.hold_id IS NULL LIMIT 1",
        (order["id"],),
    ).fetchone()
    if existing is None:
        identity = secrets.token_hex(16)
        digest = hashlib.sha256(
            (order["id"] + ":" + order["claim_id"] + ":service").encode()
        ).hexdigest()
        conn.execute(
            "INSERT INTO slot_subscription_holds VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                order["id"],
                order["subscription_id"],
                "internal:service:" + identity,
                "slot.service_unavailable",
                digest,
                now,
            ),
        )
        _require_cancellation(conn, order["id"], now)


def hold_failed_provision(
    conn: sqlite3.Connection, slot_id: str, claim_id: str, now: float
) -> None:
    rows = conn.execute(
        "SELECT id FROM slot_subscription_orders WHERE slot_id=? "
        "AND claim_id=? AND claim_id!='' AND status!='closed'",
        (slot_id, claim_id),
    ).fetchall()
    for row in rows:
        _service_failure_hold(conn, _bound_order(conn, row["id"]), now)


class SlotSubscriptionStoreMixin:
    """All inventory and entitlement decisions share Store's write transaction."""

    def get_subscription_customer(
        self, account_id: str, *, merchant_id: str, livemode: bool
    ) -> Optional[dict[str, Any]]:
        _scope(merchant_id, livemode)
        with self._lock:
            self._wallet_account(self._conn, account_id)
            row = self._conn.execute(
                "SELECT * FROM slot_subscription_customers WHERE "
                "account_id=? AND merchant_id=? AND livemode=?",
                (account_id, merchant_id, int(livemode)),
            ).fetchone()
            return dict(row) if row is not None else None

    def prepare_subscription_customer(
        self, account_id: str, *, merchant_id: str, livemode: bool, now: float
    ) -> dict[str, Any]:
        _scope(merchant_id, livemode)
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            self._wallet_account(conn, account_id)
            row = conn.execute(
                "SELECT * FROM slot_subscription_customers WHERE "
                "account_id=? AND merchant_id=? AND livemode=?",
                (account_id, merchant_id, int(livemode)),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO slot_subscription_customers VALUES (?,?,?,?,NULL,?)",
                    (secrets.token_hex(16), account_id, merchant_id, int(livemode), now),
                )
                row = conn.execute(
                    "SELECT * FROM slot_subscription_customers WHERE "
                    "account_id=? AND merchant_id=? AND livemode=?",
                    (account_id, merchant_id, int(livemode)),
                ).fetchone()
            elif row["customer_id"] is None and not 0 <= now - row["created_at"] <= 23 * 3600:
                _error("customer creation is unresolved; reconcile the existing provider request")
            return dict(row)

    def bind_subscription_customer(
        self, account_id: str, *, merchant_id: str, livemode: bool, customer_id: str, now: float
    ) -> dict[str, Any]:
        _scope(merchant_id, livemode)
        _provider(customer_id, "cus_")
        _check(wallet.instant, now)
        with self._write_txn() as conn:
            self._wallet_account(conn, account_id)
            row = conn.execute(
                "SELECT * FROM slot_subscription_customers WHERE "
                "account_id=? AND merchant_id=? AND livemode=?",
                (account_id, merchant_id, int(livemode)),
            ).fetchone()
            if row is None:
                _error("prepare a durable customer identity before contacting the provider")
            if row["customer_id"] not in (None, customer_id):
                _mismatch("the subscription customer was already bound differently")
            try:
                conn.execute(
                    "UPDATE slot_subscription_customers SET customer_id=? WHERE id=?",
                    (customer_id, row["id"]),
                )
            except sqlite3.IntegrityError as exc:
                from .store import StoreError

                raise StoreError(
                    "this provider customer already belongs to another account"
                ) from exc
            return dict(
                conn.execute(
                    "SELECT * FROM slot_subscription_customers WHERE id=?", (row["id"],)
                ).fetchone()
            )

    def reserve_slot_subscription(
        self,
        account_id: str,
        node_id: str,
        *,
        expected_cents: int,
        idempotency_key: str,
        merchant_id: str,
        livemode: bool,
        now: float,
        max_age_s: float,
        monthly_consent: bool = False,
    ) -> dict[str, Any]:
        from . import offers
        from .store import NoSlotAvailable, SubscriptionCheckoutPending, WalletQuoteChanged

        _check(wallet.identity, node_id)
        _check(wallet.request_id, idempotency_key)
        _check(wallet.positive_cents, expected_cents)
        _scope(merchant_id, livemode)
        if monthly_consent is not True:
            _error("explicit consent to monthly slot billing is required")
        now, max_age_s = _check(wallet.instant, now), _check(wallet.freshness, max_age_s)
        with self._write_txn() as conn:
            self._wallet_account(conn, account_id)
            expected = {
                "account_id": account_id,
                "node_id": node_id,
                "amount_cents": expected_cents,
                "merchant_id": merchant_id,
                "livemode": int(livemode),
            }
            previous = conn.execute(
                "SELECT * FROM slot_subscription_orders WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if previous is not None:
                if any(previous[key] != value for key, value in expected.items()):
                    _mismatch("the subscription request identity was already used differently")
                return _view(conn, previous["id"])
            if (
                conn.execute(
                    "SELECT 1 FROM slot_subscription_orders WHERE account_id=? "
                    "AND status!='closed' AND claim_id='' LIMIT 1",
                    (account_id,),
                ).fetchone()
                is not None
            ):
                raise SubscriptionCheckoutPending(
                    "Finish or cancel your pending monthly checkout before starting another."
                )
            if wallet_topups.spending_blocked(conn, account_id):
                _error("account credit needs payment review before another purchase")
            slot, offer, cents = self._wallet_priced_slot(conn, account_id, node_id, now, max_age_s)
            number = conn.execute(
                "SELECT public_status_number FROM nodes WHERE id=?", (node_id,)
            ).fetchone()[0]
            if type(number) is not int or number <= 0:
                _error("the machine needs a trusted public number before monthly checkout")
            if cents != expected_cents:
                raise WalletQuoteChanged("the slot price changed; review the current monthly quote")
            if (
                slot["state"] != slots.FREE
                or slot["present"] != 0
                or reservation_blocks(conn, slot["id"])
            ):
                raise NoSlotAvailable("the chosen slot is no longer available for subscription")
            order_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO slot_subscription_orders "
                "(id,account_id,node_id,slot_id,machine_number,amount_cents,currency,"
                "merchant_id,livemode,"
                "offer_snapshot,idempotency_key,created_at,consent_version,consented_at,"
                "billing_interval,report_max_age_s,status) "
                "VALUES (?,?,?,?,?,?,'USD',?,?,?,?,?,?,?,'month',?,'reserved')",
                (
                    order_id,
                    account_id,
                    node_id,
                    slot["id"],
                    number,
                    cents,
                    merchant_id,
                    int(livemode),
                    offers.to_json(offer),
                    idempotency_key,
                    now,
                    CONSENT_VERSION,
                    now,
                    max_age_s,
                ),
            )
            return _view(conn, order_id)

    def get_slot_subscription(
        self, order_id: str, *, account_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT account_id FROM slot_subscription_orders WHERE id=?", (order_id,)
            ).fetchone()
            if row is None or account_id is not None and row["account_id"] != account_id:
                return None
            return _view(self._conn, order_id)

    def list_slot_subscriptions(self, account_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            _error("subscription history limit must be an integer from 1 to 100")
        with self._lock:
            self._wallet_account(self._conn, account_id)
            identifiers = self._conn.execute(
                "SELECT id FROM slot_subscription_orders WHERE account_id=? "
                "ORDER BY created_at DESC,id LIMIT ?",
                (account_id, limit),
            ).fetchall()
            return [_view(self._conn, row["id"]) for row in identifiers]

    def _subscription_lookup(
        self, table: str, column: str, identifier: str
    ) -> Optional[dict[str, Any]]:
        if (table, column) not in {
            ("slot_subscription_sessions", "session_id"),
            ("slot_subscriptions", "subscription_id"),
            ("slot_subscription_invoices", "invoice_id"),
            ("slot_subscription_invoices", "payment_intent_id"),
        }:
            _error("unknown subscription lookup")
        with self._lock:
            row = self._conn.execute(
                f"SELECT order_id FROM {table} WHERE {column}=?", (identifier,)
            ).fetchone()
            return _view(self._conn, row["order_id"]) if row is not None else None

    def slot_subscription_for_session(self, session_id: str) -> Optional[dict[str, Any]]:
        _provider(session_id, "cs_")
        return self._subscription_lookup("slot_subscription_sessions", "session_id", session_id)

    def slot_subscription_for_provider(self, subscription_id: str) -> Optional[dict[str, Any]]:
        _provider(subscription_id, "sub_")
        return self._subscription_lookup("slot_subscriptions", "subscription_id", subscription_id)

    def slot_subscription_for_invoice(self, invoice_id: str) -> Optional[dict[str, Any]]:
        _provider(invoice_id, "in_")
        return self._subscription_lookup("slot_subscription_invoices", "invoice_id", invoice_id)

    def slot_subscription_for_payment(self, payment_intent_id: str) -> Optional[dict[str, Any]]:
        _provider(payment_intent_id, "pi_")
        return self._subscription_lookup(
            "slot_subscription_invoices", "payment_intent_id", payment_intent_id
        )

    def pending_slot_subscriptions(self, *, now: float, limit: int = 20) -> list[dict[str, Any]]:
        now = _check(wallet.instant, now)
        if type(limit) is not int or not 1 <= limit <= 100:
            _error("pending subscription limit must be an integer from 1 to 100")
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.id FROM slot_subscription_orders o "
                "JOIN slot_subscription_sessions s ON s.order_id=o.id "
                "WHERE o.status!='closed' AND o.claim_id='' AND s.expires_at<=? "
                "ORDER BY s.expires_at,o.id LIMIT ?",
                (now, limit),
            ).fetchall()
            return [_view(self._conn, row["id"]) for row in rows]

    def list_slot_subscription_reconciliation(
        self, *, limit: int = 20, after_order_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            _error("subscription reconciliation limit must be an integer from 1 to 100")
        if after_order_id is not None:
            _check(wallet.request_id, after_order_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.id FROM slot_subscription_orders o "
                "LEFT JOIN slot_subscription_sessions s ON s.order_id=o.id "
                "LEFT JOIN slot_subscriptions b ON b.order_id=o.id "
                "WHERE o.status!='closed' "
                "AND (s.session_id IS NOT NULL OR b.subscription_id IS NOT NULL "
                "OR (o.status='unknown' AND EXISTS (SELECT 1 FROM slot_subscription_customers c "
                "WHERE c.account_id=o.account_id AND c.merchant_id=o.merchant_id "
                "AND c.livemode=o.livemode AND c.customer_id IS NOT NULL))) "
                "AND (? IS NULL OR o.id>?) ORDER BY o.id LIMIT ?",
                (after_order_id, after_order_id, limit),
            ).fetchall()
            return [_view(self._conn, row["id"]) for row in rows]

    def slot_subscription_processing_required(self) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM slot_subscription_orders o WHERE o.status!='closed' "
                    "OR o.cancel_required=1 OR EXISTS "
                    "(SELECT 1 FROM slot_subscription_cancellations c "
                    "WHERE c.order_id=o.id AND c.completed_at IS NULL) "
                    "OR EXISTS (SELECT 1 FROM slot_subscription_holds h "
                    "LEFT JOIN slot_subscription_hold_resolutions r ON r.hold_id=h.id "
                    "WHERE h.order_id=o.id AND r.hold_id IS NULL) LIMIT 1"
                ).fetchone()
                is not None
            )

    def prepare_slot_subscription_cancellation(
        self, account_id: str, order_id: str, *, idempotency_key: str, now: float
    ) -> dict[str, Any]:
        """Record an owned cancel intent before any provider write, without a spend gate."""
        from .store import NotYours

        _check(wallet.request_id, idempotency_key)
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            self._wallet_account(conn, account_id)
            row = _bound_order(conn, order_id)
            if row["account_id"] != account_id:
                raise NotYours("the monthly subscription is not owned by this account")
            expected = {
                "account_id": account_id,
                "order_id": order_id,
                "subscription_id": row["subscription_id"],
                "customer_id": row["customer_id"],
            }
            previous = conn.execute(
                "SELECT * FROM slot_subscription_cancellations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous is not None:
                if any(previous[key] != value for key, value in expected.items()):
                    _mismatch("the cancellation request identity was already used differently")
                if previous["completed_at"] is None:
                    _require_cancellation(conn, order_id, now)
                return dict(previous)
            operation_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO slot_subscription_cancellations VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    operation_id,
                    account_id,
                    order_id,
                    row["subscription_id"],
                    row["customer_id"],
                    idempotency_key,
                    now,
                ),
            )
            _require_cancellation(conn, order_id, now)
            return dict(
                conn.execute(
                    "SELECT * FROM slot_subscription_cancellations WHERE id=?", (operation_id,)
                ).fetchone()
            )

    def require_slot_subscription_cancellation(
        self, order_id: str, *, now: float
    ) -> dict[str, Any]:
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            _bound_order(conn, order_id)
            _require_cancellation(conn, order_id, now)
            return _view(conn, order_id)

    def next_slot_subscription_cancel_attempt(self, order_id: str, *, now: float) -> int:
        """Trusted caller has just retrieved cancellation=False; reserve a fresh write key."""
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            row = _bound_order(conn, order_id)
            current = row["cancel_attempt_no"]
            if type(current) is not int or not 0 <= current < (1 << 63) - 1:
                _error("subscription cancellation attempt counter is unavailable")
            conn.execute(
                "UPDATE slot_subscription_orders SET cancel_attempt_no=?,cancel_required=1,"
                "cancel_checked_at=? WHERE id=?",
                (current + 1, now, order_id),
            )
            return current + 1

    def confirm_slot_subscription_cancellation(
        self,
        order_id: str,
        *,
        proof: Mapping[str, Any],
        now: float,
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Only a fresh matched provider confirmation completes pending cancel intents."""
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            row = _bound_order(conn, order_id)
            expected = {
                "subscription_id": row["subscription_id"],
                "customer_id": row["customer_id"],
                "merchant_id": row["merchant_id"],
                "livemode": bool(row["livemode"]),
            }
            if (
                not isinstance(proof, Mapping)
                or type(proof.get("livemode")) is not bool
                or type(proof.get("cancel_at_period_end")) is not bool
                or not isinstance(proof.get("status"), str)
                or proof["status"]
                not in {
                    "incomplete",
                    "incomplete_expired",
                    "trialing",
                    "active",
                    "past_due",
                    "canceled",
                    "unpaid",
                    "paused",
                }
                or any(proof.get(key) != value for key, value in expected.items())
                or "order_id" in proof
                and proof["order_id"] != order_id
                or proof["status"] not in TERMINAL
                and proof["cancel_at_period_end"] is not True
            ):
                _error("a matched terminal or end-of-period cancellation confirmation is required")
            if operation_id is not None:
                operation = conn.execute(
                    "SELECT * FROM slot_subscription_cancellations WHERE id=?", (operation_id,)
                ).fetchone()
                if (
                    operation is None
                    or operation["order_id"] != order_id
                    or operation["account_id"] != row["account_id"]
                    or operation["subscription_id"] != row["subscription_id"]
                    or operation["customer_id"] != row["customer_id"]
                ):
                    _mismatch("the cancellation operation does not match this owned subscription")
            if (
                row["cancel_required"]
                or row["provider_status"] != proof["status"]
                or row["cancel_at_period_end"] != int(proof["cancel_at_period_end"])
            ):
                conn.execute(
                    "UPDATE slot_subscription_orders SET cancel_required=0,provider_status=?,"
                    "cancel_at_period_end=?,cancel_checked_at=? WHERE id=?",
                    (proof["status"], int(proof["cancel_at_period_end"]), now, order_id),
                )
            conn.execute(
                "UPDATE slot_subscription_cancellations SET completed_at=COALESCE(completed_at,?) "
                "WHERE order_id=? AND completed_at IS NULL",
                (now, order_id),
            )
            retire_released(conn, row["slot_id"], now)
            return _view(conn, order_id)

    def pending_slot_subscription_cancellations(
        self, *, limit: int = 20, after_order_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            _error("pending cancellation limit must be an integer from 1 to 100")
        if after_order_id is not None:
            _check(wallet.request_id, after_order_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.id FROM slot_subscription_orders o "
                "JOIN slot_subscriptions b ON b.order_id=o.id "
                "WHERE (o.cancel_required=1 OR EXISTS "
                "(SELECT 1 FROM slot_subscription_cancellations c "
                "WHERE c.order_id=o.id AND c.completed_at IS NULL) "
                "OR EXISTS (SELECT 1 FROM slot_subscription_holds h "
                "LEFT JOIN slot_subscription_hold_resolutions r ON r.hold_id=h.id "
                "WHERE h.order_id=o.id AND r.hold_id IS NULL)) "
                "AND (? IS NULL OR o.id>?) ORDER BY o.id LIMIT ?",
                (after_order_id, after_order_id, limit),
            ).fetchall()
            return [_view(self._conn, row["id"]) for row in rows]

    def attach_slot_subscription_session(
        self, order_id: str, *, session_id: str, expires_at: float, now: float
    ) -> dict[str, Any]:
        _provider(session_id, "cs_")
        now, expires_at = _check(wallet.instant, now), _check(wallet.instant, expires_at)
        with self._write_txn() as conn:
            row = _order(conn, order_id)
            previous = conn.execute(
                "SELECT * FROM slot_subscription_sessions WHERE order_id=?", (order_id,)
            ).fetchone()
            if previous is not None:
                if previous["session_id"] != session_id or previous["expires_at"] != expires_at:
                    _mismatch("the subscription checkout was already bound differently")
                return _view(conn, order_id)
            if row["status"] not in {"reserved", "unknown"} or expires_at <= row["created_at"]:
                _error("the subscription reservation cannot accept this checkout")
            if (
                conn.execute(
                    "SELECT 1 FROM slot_subscription_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                is not None
            ):
                _mismatch("the checkout Session already belongs to another subscription order")
            conn.execute(
                "INSERT INTO slot_subscription_sessions VALUES (?,?,?,?)",
                (order_id, session_id, expires_at, now),
            )
            conn.execute(
                "UPDATE slot_subscription_orders SET status='checkout_open' WHERE id=?", (order_id,)
            )
            return _view(conn, order_id)

    def mark_slot_subscription_unknown(self, order_id: str, *, now: float) -> dict[str, Any]:
        _check(wallet.instant, now)
        with self._write_txn() as conn:
            row = _order(conn, order_id)
            if row["status"] in {"reserved", "checkout_open", "unknown"}:
                conn.execute(
                    "UPDATE slot_subscription_orders SET status='unknown' WHERE id=?", (order_id,)
                )
            return _view(conn, order_id)

    def bind_slot_subscription(
        self, order_id: str, *, session_id: str, subscription_id: str, customer_id: str, now: float
    ) -> dict[str, Any]:
        _provider(session_id, "cs_")
        _provider(subscription_id, "sub_")
        _provider(customer_id, "cus_")
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            row = _view(conn, order_id)
            if row["session_id"] != session_id:
                _mismatch("the subscription checkout does not match its reservation")
            if row["subscription_id"] is not None:
                if row["subscription_id"] != subscription_id or row["customer_id"] != customer_id:
                    _mismatch("the slot subscription was already bound differently")
                return row
            if row["status"] == "closed":
                _error("the subscription reservation is closed")
            customer = conn.execute(
                "SELECT customer_id FROM slot_subscription_customers WHERE "
                "account_id=? AND merchant_id=? AND livemode=?",
                (row["account_id"], row["merchant_id"], row["livemode"]),
            ).fetchone()
            if customer is None or customer["customer_id"] != customer_id:
                _mismatch("the provider customer does not belong to this account and mode")
            if (
                conn.execute(
                    "SELECT 1 FROM slot_subscriptions WHERE subscription_id=?", (subscription_id,)
                ).fetchone()
                is not None
            ):
                _mismatch("the provider subscription already belongs to another order")
            conn.execute(
                "INSERT INTO slot_subscriptions VALUES (?,?,?,?)",
                (order_id, subscription_id, customer_id, now),
            )
            return _view(conn, order_id)

    def close_slot_subscription(
        self, order_id: str, *, proof: Mapping[str, Any], now: float
    ) -> dict[str, Any]:
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            row = _view(conn, order_id)
            expected = {
                "order_id": order_id,
                "session_id": row["session_id"],
                "session_status": "expired",
                "payment_status": "unpaid",
                "subscription_id": None,
                "merchant_id": row["merchant_id"],
                "livemode": bool(row["livemode"]),
            }
            if (
                not isinstance(proof, Mapping)
                or row["session_id"] is None
                or type(proof.get("livemode")) is not bool
                or any(proof.get(key) != value for key, value in expected.items())
                or row["subscription_id"] is not None
                or row["claim_id"]
                or conn.execute(
                    "SELECT 1 FROM slot_subscription_invoices WHERE order_id=?", (order_id,)
                ).fetchone()
                is not None
            ):
                _error(
                    "only a matched provider-confirmed unpaid expired checkout "
                    "may free a reservation"
                )
            conn.execute(
                "UPDATE slot_subscription_orders SET status='closed',"
                "closed_at=COALESCE(closed_at,?) "
                "WHERE id=?",
                (now, order_id),
            )
            return _view(conn, order_id)

    def record_slot_subscription_invoice(
        self,
        order_id: str,
        *,
        invoice_id: str,
        payment_intent_id: str,
        subscription_id: str,
        customer_id: str,
        merchant_id: str,
        livemode: bool,
        currency: str,
        amount_cents: int,
        period_start_at: float,
        period_end_at: float,
        paid: bool,
        event_id: str,
        body_sha256: str,
        now: float,
    ) -> dict[str, Any]:
        from .store import NoSlotAvailable

        for value, prefix in (
            (invoice_id, "in_"),
            (payment_intent_id, "pi_"),
            (subscription_id, "sub_"),
            (customer_id, "cus_"),
            (event_id, "evt_"),
        ):
            _provider(value, prefix)
        _scope(merchant_id, livemode)
        _check(wallet_topups._event, event_id, body_sha256)
        _check(wallet.positive_cents, amount_cents)
        start, end = _check(wallet.instant, period_start_at), _check(wallet.instant, period_end_at)
        now = _check(wallet.instant, now)
        if paid is not True or currency != "USD" or end <= start:
            _error("a fully paid USD invoice and exact valid provider period are required")
        with self._write_txn() as conn:
            row = _view(conn, order_id)
            expected = {
                "order_id": order_id,
                "payment_intent_id": payment_intent_id,
                "subscription_id": subscription_id,
                "customer_id": customer_id,
                "merchant_id": merchant_id,
                "livemode": int(livemode),
                "currency": currency,
                "amount_cents": amount_cents,
                "period_start_at": start,
                "period_end_at": end,
            }
            if any(
                row[key] != expected[key]
                for key in (
                    "subscription_id",
                    "customer_id",
                    "merchant_id",
                    "livemode",
                    "currency",
                    "amount_cents",
                )
            ):
                _mismatch("the paid invoice does not match the reserved slot subscription")
            event = conn.execute(
                "SELECT * FROM slot_subscription_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if (
                conn.execute(
                    "SELECT 1 FROM slot_subscription_holds WHERE event_id=?", (event_id,)
                ).fetchone()
                is not None
            ):
                _mismatch("the event identity was already used for a payment hold")
            if event is not None and (
                event["order_id"] != order_id
                or event["invoice_id"] != invoice_id
                or event["body_sha256"] != body_sha256
            ):
                _mismatch("the subscription event identity was already used differently")
            previous = conn.execute(
                "SELECT * FROM slot_subscription_invoices WHERE invoice_id=?", (invoice_id,)
            ).fetchone()
            if previous is not None:
                if any(previous[key] != value for key, value in expected.items()):
                    _mismatch("the invoice identity was already used for a different entitlement")
                if event is None:
                    conn.execute(
                        "INSERT INTO slot_subscription_events VALUES (?,?,?,?,?)",
                        (event_id, order_id, invoice_id, body_sha256, now),
                    )
                return _invoice_view(previous)
            if row["status"] == "closed":
                _error("a closed reservation cannot gain a new paid entitlement")
            if (
                conn.execute(
                    "SELECT 1 FROM slot_subscription_invoices WHERE payment_intent_id=?",
                    (payment_intent_id,),
                ).fetchone()
                is not None
            ):
                _mismatch("the payment intent was already used for another subscription invoice")
            review_required = False
            if not row["claim_id"]:
                conn.execute("SAVEPOINT monthly_first_claim")
                try:
                    _claim_available(conn, row, now)
                    slot_id = self._claim_slot_in_transaction(
                        conn,
                        row["account_id"],
                        now=now,
                        node_id=row["node_id"],
                        heard_since=None,
                        check_quota=False,
                        reservation_id=order_id,
                    )
                    if slot_id != row["slot_id"]:
                        raise NoSlotAvailable("the originally reserved slot no longer exists")
                    slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
                    claim_id, generation = slot["gateway_claim_id"], slot["gateway_generation"]
                    conn.execute(
                        "UPDATE slot_subscription_orders SET status='active',claim_id=?,"
                        "claim_generation=? WHERE id=?",
                        (claim_id, generation, order_id),
                    )
                except NoSlotAvailable:
                    conn.execute("ROLLBACK TO monthly_first_claim")
                    review_required, claim_id, generation = True, "", 0
                finally:
                    conn.execute("RELEASE monthly_first_claim")
            else:
                slot = self._wallet_current_slot(
                    conn, row["account_id"], row["slot_id"], row["claim_id"]
                )
                if slot["state"] == slots.RELEASING:
                    _error("a releasing subscription claim cannot gain a paid entitlement")
                claim_id, generation = row["claim_id"], row["claim_generation"]
            conn.execute(
                "INSERT INTO slot_subscription_invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    invoice_id,
                    payment_intent_id,
                    order_id,
                    subscription_id,
                    customer_id,
                    claim_id,
                    generation,
                    amount_cents,
                    currency,
                    merchant_id,
                    int(livemode),
                    start,
                    end,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO slot_subscription_events VALUES (?,?,?,?,?)",
                (event_id, order_id, invoice_id, body_sha256, now),
            )
            if review_required:
                _service_failure_hold(conn, row, now)
            return _invoice_view(
                conn.execute(
                    "SELECT * FROM slot_subscription_invoices WHERE invoice_id=?", (invoice_id,)
                ).fetchone()
            )

    def slot_subscription_access(
        self, account_id: str, slot_id: str, *, claim_id: str, now: float
    ) -> Optional[dict[str, Any]]:
        from .store import NotYours

        now = _check(wallet.instant, now)
        with self._lock:
            row = self._conn.execute(
                "WITH managed AS (SELECT * FROM slot_subscription_orders "
                "WHERE account_id=? AND slot_id=? AND claim_id=?) "
                "SELECT s.held_by,s.state,s.gateway_claim_id,"
                "(SELECT COUNT(*) FROM managed) AS managed_count,"
                "(SELECT MAX(i.period_end_at) FROM slot_subscription_invoices i "
                "JOIN managed o ON o.id=i.order_id WHERE i.claim_id=s.gateway_claim_id "
                "AND i.period_start_at<=? AND ?<i.period_end_at) AS paid_end,"
                "EXISTS (SELECT 1 FROM slot_subscription_holds h JOIN managed o ON o.id=h.order_id "
                "LEFT JOIN slot_subscription_hold_resolutions r ON r.hold_id=h.id "
                "WHERE r.hold_id IS NULL) AS blocked FROM slots s WHERE s.id=?",
                (account_id, slot_id, claim_id, now, now, slot_id),
            ).fetchone()
            if (
                row is None
                or row["held_by"] != account_id
                or row["state"] not in slots.HELD
                or row["gateway_claim_id"] != claim_id
            ):
                raise NotYours("the slot is not held by this account and claim")
            if row["managed_count"] == 0:
                return None
            if row["managed_count"] != 1:
                _error("the subscription claim has ambiguous ownership")
            return {
                "allowed": row["paid_end"] is not None
                and not row["blocked"]
                and row["state"] != slots.RELEASING,
                "paid_through": float(row["paid_end"]) if row["paid_end"] is not None else 0.0,
            }

    def update_slot_subscription_state(
        self,
        order_id: str,
        *,
        subscription_id: str,
        status: str,
        cancel_at_period_end: bool,
        now: float,
    ) -> dict[str, Any]:
        _provider(subscription_id, "sub_")
        now = _check(wallet.instant, now)
        if (
            status
            not in {
                "incomplete",
                "incomplete_expired",
                "trialing",
                "active",
                "past_due",
                "canceled",
                "unpaid",
                "paused",
            }
            or type(cancel_at_period_end) is not bool
        ):
            _error("an explicit verified subscription state is required")
        with self._write_txn() as conn:
            row = _view(conn, order_id)
            if row["subscription_id"] != subscription_id:
                _mismatch("the subscription state does not match its reserved slot")
            conn.execute(
                "UPDATE slot_subscription_orders SET provider_status=?,"
                "cancel_at_period_end=? WHERE id=?",
                (status, int(cancel_at_period_end), order_id),
            )
            if row["payment_review_required"] and not cancel_at_period_end:
                _require_cancellation(conn, order_id, now)
            retire_released(conn, row["slot_id"], now)
            return _view(conn, order_id)

    def hold_slot_subscription(
        self,
        order_id: str,
        *,
        subscription_id: str,
        event_id: str,
        event_type: str,
        body_sha256: str,
        now: float,
    ) -> dict[str, Any]:
        _provider(subscription_id, "sub_")
        _check(wallet_topups._event, event_id, body_sha256)
        now = _check(wallet.instant, now)
        if event_type not in {
            "charge.refunded",
            "refund.created",
            "refund.updated",
            "charge.dispute.created",
            "charge.dispute.updated",
            "charge.dispute.closed",
        }:
            _error("a verified refund or dispute event is required")
        with self._write_txn() as conn:
            row = _view(conn, order_id)
            if row["subscription_id"] != subscription_id:
                _mismatch("the payment hold does not match this subscription")
            expected = {
                "order_id": order_id,
                "subscription_id": subscription_id,
                "event_type": event_type,
                "body_sha256": body_sha256,
            }
            previous = conn.execute(
                "SELECT * FROM slot_subscription_holds WHERE event_id=?", (event_id,)
            ).fetchone()
            if (
                conn.execute(
                    "SELECT 1 FROM slot_subscription_events WHERE event_id=?", (event_id,)
                ).fetchone()
                is not None
            ):
                _mismatch("the event identity was already used for a paid invoice")
            if previous is not None:
                if any(previous[key] != value for key, value in expected.items()):
                    _mismatch("the payment hold event was already used differently")
                return dict(previous)
            hold_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO slot_subscription_holds VALUES (?,?,?,?,?,?,?)",
                (hold_id, order_id, subscription_id, event_id, event_type, body_sha256, now),
            )
            _require_cancellation(conn, order_id, now)
            return dict(
                conn.execute(
                    "SELECT * FROM slot_subscription_holds WHERE id=?", (hold_id,)
                ).fetchone()
            )

    def resolve_slot_subscription_hold(
        self,
        hold_id: str,
        *,
        actor_id: str,
        actor_kind: str = "account",
        reason: str,
        idempotency_key: str,
        now: float,
    ) -> dict[str, Any]:
        _check(wallet.identity, hold_id)
        _check(wallet.reason, reason)
        _check(wallet.request_id, idempotency_key)
        now = _check(wallet.instant, now)
        with self._write_txn() as conn:
            self._wallet_admin(conn, actor_id, actor_kind)
            if (
                conn.execute(
                    "SELECT 1 FROM slot_subscription_holds WHERE id=?", (hold_id,)
                ).fetchone()
                is None
            ):
                _error("unknown subscription payment hold")
            expected = {
                "hold_id": hold_id,
                "actor_kind": actor_kind,
                "actor_id": actor_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
            }
            previous = conn.execute(
                "SELECT * FROM slot_subscription_hold_resolutions WHERE hold_id=? "
                "OR idempotency_key=?",
                (hold_id, idempotency_key),
            ).fetchone()
            if previous is not None:
                if any(previous[key] != value for key, value in expected.items()):
                    _mismatch("the hold resolution identity was already used differently")
                return dict(previous)
            conn.execute(
                "INSERT INTO slot_subscription_hold_resolutions VALUES (?,?,?,?,?,?)",
                (hold_id, actor_kind, actor_id, reason, idempotency_key, now),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM slot_subscription_hold_resolutions WHERE hold_id=?", (hold_id,)
                ).fetchone()
            )
