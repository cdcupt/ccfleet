"""Fleet-funded OpenRouter access, prepaid holds and generation receipts."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import uuid
from decimal import ROUND_CEILING, Decimal

from . import wallet, wallet_topups

PREFIX = "ccf_or_"
TOKEN = re.compile(r"ccf_or_[A-Za-z0-9_-]{43}\Z")
MULTIPLIER = Decimal("1.05")
SCHEMA = """
CREATE TABLE IF NOT EXISTS openrouter_accounts (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    token_hash TEXT UNIQUE,
    remainder_usd TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS openrouter_requests (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    model TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_checked REAL NOT NULL DEFAULT 0,
    generation_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('held','submitted','settled','released')),
    hold_cents INTEGER NOT NULL,
    cost_usd TEXT,
    debit_cents INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_openrouter_account ON openrouter_requests(account_id,created_at);
"""


class BillingError(ValueError):
    pass


def hold_cents(body_size, max_tokens, rates):
    # Text-only requests. Include serialization, tool templates and model framing.
    prompt, completion = rates
    ceiling = ((body_size * 2 + 4096) * prompt + max_tokens * completion) / 1_000_000
    return max(1, int((ceiling * MULTIPLIER * 100).to_integral_value(rounding=ROUND_CEILING)))


class OpenRouterStore:
    def openrouter_key(self, owner, *, revoke=False):
        token = None if revoke else PREFIX + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest() if token else None
        with self._write_txn() as db:
            self._wallet_account(db, owner)
            db.execute(
                "INSERT INTO openrouter_accounts(account_id, token_hash) VALUES (?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET token_hash=excluded.token_hash",
                (owner, digest),
            )
        return token

    def openrouter_principal(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            row = self._conn.execute(
                "SELECT account_id FROM openrouter_accounts WHERE token_hash=?", (digest,)
            ).fetchone()
        return row[0] if row else None

    def openrouter_reserve(self, owner, token, model, cents):
        wallet.positive_cents(cents)
        with self._write_txn() as db:
            if self.openrouter_principal(token) != owner or wallet_topups.spending_blocked(
                db, owner
            ):
                raise BillingError("OpenRouter spending is unavailable for this account.")
            if self._wallet_balance(db, owner) < cents:
                raise BillingError("Insufficient available Fleet credit for this request.")
            request_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO openrouter_requests(id,account_id,model,created_at,state,"
                "hold_cents) VALUES (?,?,?,?,'held',?)",
                (request_id, owner, model, time.time(), cents),
            )
        return request_id

    def openrouter_submit(self, request_id):
        with self._write_txn() as db:
            if not db.execute(
                "UPDATE openrouter_requests SET state='submitted' WHERE id=? AND state='held'",
                (request_id,),
            ).rowcount:
                raise BillingError("OpenRouter request is no longer reserved.")

    def openrouter_release(self, request_id, *, rejected=False):
        with self._write_txn() as db:
            db.execute(
                "UPDATE openrouter_requests SET state='released' WHERE id=? "
                "AND (state='held' OR (state='submitted' AND ? AND generation_id IS NULL))",
                (request_id, rejected),
            )

    def openrouter_identify(self, request_id, generation, *, verified=False):
        if not isinstance(generation, str) or not re.fullmatch(
            r"(?:gen-|msg_)[a-zA-Z0-9_-]{1,180}", generation
        ):
            raise BillingError("Invalid OpenRouter generation receipt.")
        with self._write_txn() as db:
            if not db.execute(
                "UPDATE openrouter_requests SET generation_id=? WHERE id=? "
                "AND ((state='submitted' AND (generation_id IS NULL "
                "OR generation_id=? OR (? AND substr(generation_id,1,4)='msg_'))) "
                "OR (state='settled' AND generation_id=?))",
                (generation, request_id, generation, verified, generation),
            ).rowcount:
                raise BillingError("Generation receipt does not match the request.")

    def openrouter_settle(self, request_id, generation, cost):
        if (
            not isinstance(cost, (int, Decimal))
            or isinstance(cost, bool)
            or not Decimal(cost).is_finite()
            or not 0 <= cost <= 1_000_000
            or Decimal(cost).as_tuple().exponent < -18
        ):
            raise BillingError("Invalid OpenRouter generation cost.")
        with self._write_txn() as db:
            row = db.execute(
                "SELECT * FROM openrouter_requests WHERE id=?", (request_id,)
            ).fetchone()
            if not row or not generation or row["generation_id"] != generation:
                raise BillingError("Generation receipt does not match the request.")
            if row["state"] == "settled" and Decimal(row["cost_usd"]) == cost:
                return
            if row["state"] != "submitted":
                raise BillingError("Conflicting OpenRouter settlement.")
            owner = row["account_id"]
            carry = db.execute(
                "SELECT remainder_usd FROM openrouter_accounts WHERE account_id=?", (owner,)
            ).fetchone()[0]
            total = Decimal(cost) * MULTIPLIER + Decimal(carry)
            cents = int(total * 100)
            if cents > row["hold_cents"]:
                raise BillingError(
                    "Provider cost exceeded the reserved amount; review the receipt."
                )
            db.execute(
                "UPDATE openrouter_accounts SET remainder_usd=? WHERE account_id=?",
                (str(total - Decimal(cents) / 100), owner),
            )
            db.execute(
                "UPDATE openrouter_requests SET state='settled',cost_usd=?,debit_cents=? "
                "WHERE id=?",
                (str(cost), cents, request_id),
            )

    def openrouter_pending(self, now):
        with self._write_txn() as db:
            # Held means no POST was authorized. A late worker's submit will fail.
            db.execute(
                "UPDATE openrouter_requests SET state='released' "
                "WHERE state='held' AND created_at<?",
                (now - 120,),
            )
            rows = db.execute(
                "SELECT id,generation_id FROM openrouter_requests "
                "WHERE state='submitted' AND generation_id IS NOT NULL "
                "AND created_at<? AND last_checked<? ORDER BY last_checked LIMIT 20",
                (now - 30, now - 60),
            ).fetchall()
            db.executemany(
                "UPDATE openrouter_requests SET last_checked=? WHERE id=?",
                [(now, row["id"]) for row in rows],
            )
            return rows

    def openrouter_receipts(self, owner):
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT id,model,generation_id,state,cost_usd,debit_cents,"
                    "hold_cents,created_at "
                    "FROM openrouter_requests WHERE account_id=? "
                    "ORDER BY created_at DESC LIMIT 50",
                    (owner,),
                )
            ]
