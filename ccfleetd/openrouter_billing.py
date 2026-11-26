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
    wallet_exempt INTEGER NOT NULL DEFAULT 0 CHECK(wallet_exempt IN (0,1)),
    cost_usd TEXT,
    fee_usd TEXT,
    review_note TEXT,
    debit_cents INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_openrouter_account ON openrouter_requests(account_id,created_at);
CREATE INDEX IF NOT EXISTS ix_openrouter_used_model ON openrouter_requests(account_id,model)
    WHERE state='settled';
CREATE TABLE IF NOT EXISTS openrouter_key_requests (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    request_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (account_id, request_hash)
);
"""


class BillingError(ValueError):
    pass


def hold_cents(prompt_tokens, max_tokens, rates):
    prompt, completion = rates
    ceiling = (prompt_tokens * prompt + max_tokens * completion) / 1_000_000
    return int((ceiling * MULTIPLIER * 100).to_integral_value(rounding=ROUND_CEILING))


class OpenRouterStore:
    def openrouter_key(self, owner, *, revoke=False, request_id=None):
        request_hash = None
        if request_id is not None:
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
                raise BillingError("Invalid OpenRouter key request.")
            request_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        token = None if revoke else PREFIX + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest() if token else None
        now = time.time()
        with self._write_txn() as db:
            if request_hash is not None:
                db.execute(
                    "DELETE FROM openrouter_key_requests WHERE account_id=? AND created_at<?",
                    (owner, now - 30 * 86400),
                )
                inserted = db.execute(
                    "INSERT OR IGNORE INTO openrouter_key_requests"
                    "(account_id, request_hash, created_at) VALUES (?,?,?)",
                    (owner, request_hash, now),
                ).rowcount
                if not inserted:
                    return None
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

    def openrouter_reserve(self, owner, token, model, cents, *, admin_account_id=""):
        if type(cents) is not int or not 0 <= cents <= wallet.MAX_CENTS:
            raise BillingError("Invalid OpenRouter credit reservation.")
        with self._write_txn() as db:
            if self.openrouter_principal(token) != owner or wallet_topups.spending_blocked(
                db, owner
            ):
                raise BillingError("OpenRouter spending is unavailable for this account.")
            exempt = owner == admin_account_id and db.execute(
                "SELECT 1 FROM accounts WHERE id=? AND role='admin'", (owner,)
            ).fetchone() is not None
            if exempt:
                cents = 0
            if self._wallet_balance(db, owner) < cents:
                raise BillingError("Insufficient available Fleet credit for this request.")
            request_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO openrouter_requests(id,account_id,model,created_at,state,"
                "hold_cents,wallet_exempt) VALUES (?,?,?,?,'held',?,?)",
                (request_id, owner, model, time.time(), cents, int(exempt)),
            )
        return request_id

    def openrouter_submit(self, request_id, owner, token):
        with self._write_txn() as db:
            if wallet_topups.spending_blocked(db, owner):
                raise BillingError("OpenRouter spending is paused.")
            if not db.execute(
                "UPDATE openrouter_requests SET state='submitted' WHERE id=? "
                "AND account_id=? AND state='held' AND (wallet_exempt=0 OR EXISTS (SELECT 1 "
                "FROM accounts WHERE id=openrouter_requests.account_id AND role='admin')) "
                "AND EXISTS "
                "(SELECT 1 FROM openrouter_accounts WHERE account_id=? AND token_hash=?)",
                (request_id, owner, owner, hashlib.sha256(token.encode()).hexdigest()),
            ).rowcount:
                raise BillingError("OpenRouter request is no longer reserved.")

    def openrouter_release(self, request_id, *, rejected=False):
        with self._write_txn() as db:
            db.execute(
                "UPDATE openrouter_requests SET state='released' WHERE id=? "
                "AND (state='held' OR (state='submitted' AND ? AND generation_id IS NULL))",
                (request_id, rejected),
            )

    def openrouter_void_unbilled(self, request_id, note, before):
        """Release an ambiguous hold only after operator verification of zero cost."""
        reason = wallet.reason(note)
        with self._write_txn() as db:
            if not db.execute(
                "UPDATE openrouter_requests SET state='released', review_note=? "
                "WHERE id=? AND state='submitted' AND created_at<?",
                (reason, request_id, before),
            ).rowcount:
                raise BillingError("No expired, unsettled OpenRouter submission matches.")

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
            if row["wallet_exempt"]:
                db.execute("UPDATE openrouter_requests SET state='settled',cost_usd=?,"
                           "fee_usd='0',debit_cents=0 WHERE id=?", (str(cost), request_id))
                return
            owner = row["account_id"]
            carry = db.execute(
                "SELECT remainder_usd FROM openrouter_accounts WHERE account_id=?", (owner,)
            ).fetchone()[0]
            fee = Decimal(cost) * (MULTIPLIER - 1)
            total = Decimal(cost) + fee + Decimal(carry)
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
                "UPDATE openrouter_requests SET state='settled',cost_usd=?,fee_usd=?,debit_cents=? "
                "WHERE id=?",
                (str(cost), str(fee), cents, request_id),
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

    def openrouter_recent_models(self, owner, offered):
        """Return up to ten enabled models from the most recent settled requests."""
        if not offered:
            return []
        # One indexed last-row lookup per offered model (catalogs have at most 64).
        # A late settlement of an older request must not displace a newer choice.
        placeholders = ",".join("(?)" for _ in offered)
        with self._lock:
            rows = self._conn.execute(
                f"WITH offered(model) AS (VALUES {placeholders}) "
                "SELECT model, (SELECT MAX(r.rowid) FROM openrouter_requests AS r "
                "WHERE r.account_id=? AND r.model=offered.model AND r.state='settled') AS last_use "
                "FROM offered ORDER BY last_use DESC LIMIT 10",
                (*offered, owner),
            ).fetchall()
        return [model for model, last_use in rows if last_use is not None]

    def openrouter_receipts(self, owner):
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT id,model,generation_id,state,cost_usd,fee_usd,debit_cents,"
                    "hold_cents,wallet_exempt,created_at "
                    "FROM openrouter_requests WHERE account_id=? "
                    "ORDER BY created_at DESC LIMIT 50",
                    (owner,),
                )
            ]

    def openrouter_usage_overview(self, owner, *, recent_limit=50, group_limit=60):
        """Return exact all-time account totals, daily-by-model rollups, and recent receipts."""
        recent_cap = max(0, int(recent_limit))
        group_cap = max(0, int(group_limit))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,model,generation_id,state,cost_usd,fee_usd,debit_cents,"
                "hold_cents,wallet_exempt,created_at,"
                "strftime('%Y-%m-%d', created_at, 'unixepoch') AS utc_day "
                "FROM openrouter_requests WHERE account_id=? "
                "ORDER BY created_at DESC, rowid DESC",
                (owner,),
            ).fetchall()

        recent_receipts = []
        grouped = {}
        total_requests = 0
        settled_count = 0
        pending_count = 0
        released_count = 0
        upstream_cost_usd = Decimal(0)
        fee_usd = Decimal(0)
        wallet_debited_cents = 0
        pending_hold_cents = 0

        for row in rows:
            state = row["state"]
            exempt = bool(row["wallet_exempt"])
            debit_c = int(row["debit_cents"] or 0)
            hold_c = int(row["hold_cents"] or 0)
            cost_str = row["cost_usd"]
            fee_str = row["fee_usd"]
            created = row["created_at"]
            model = row["model"]
            day = row["utc_day"] or "—"

            total_requests += 1
            is_settled = state == "settled"
            is_pending = state in ("held", "submitted")
            is_released = state == "released"
            if is_settled:
                settled_count += 1
                if not exempt:
                    wallet_debited_cents += debit_c
            elif is_pending:
                pending_count += 1
                if not exempt:
                    pending_hold_cents += hold_c
            elif is_released:
                released_count += 1

            cost_dec = Decimal(cost_str) if cost_str is not None else None
            fee_dec = (
                Decimal(fee_str)
                if cost_dec is not None and not exempt and fee_str is not None
                else Decimal(0)
            )
            if cost_dec is not None:
                upstream_cost_usd += cost_dec
                if not exempt:
                    fee_usd += fee_dec

            key = (day, model)
            bucket = grouped.get(key)
            if bucket is None:
                bucket = {
                    "day": day,
                    "model": model,
                    "count": 0,
                    "settled_count": 0,
                    "pending_count": 0,
                    "released_count": 0,
                    "exempt_count": 0,
                    "usage_usd": Decimal(0),
                    "paid_usage_usd": Decimal(0),
                    "fee_usd": Decimal(0),
                    "debit_cents": 0,
                    "hold_cents": 0,
                    "first_at": created,
                    "last_at": created,
                    "receipts": [],
                }
                grouped[key] = bucket

            bucket["count"] += 1
            if is_settled:
                bucket["settled_count"] += 1
                if not exempt:
                    bucket["debit_cents"] += debit_c
            elif is_pending:
                bucket["pending_count"] += 1
                if not exempt:
                    bucket["hold_cents"] += hold_c
            elif is_released:
                bucket["released_count"] += 1
            if exempt:
                bucket["exempt_count"] += 1
            if cost_dec is not None:
                bucket["usage_usd"] += cost_dec
                if not exempt:
                    bucket["paid_usage_usd"] += cost_dec
                    bucket["fee_usd"] += fee_dec
            if created is not None:
                if bucket["first_at"] is None or created < bucket["first_at"]:
                    bucket["first_at"] = created
                if bucket["last_at"] is None or created > bucket["last_at"]:
                    bucket["last_at"] = created

            if len(recent_receipts) < recent_cap:
                receipt = {
                    "id": row["id"],
                    "model": model,
                    "generation_id": row["generation_id"],
                    "state": state,
                    "cost_usd": cost_str,
                    "fee_usd": fee_str,
                    "debit_cents": debit_c,
                    "hold_cents": hold_c,
                    "wallet_exempt": int(exempt),
                    "created_at": created,
                }
                recent_receipts.append(receipt)
                bucket["receipts"].append(receipt)

        all_groups = list(grouped.values())
        return {
            "summary": {
                "total_requests": total_requests,
                "settled_count": settled_count,
                "pending_count": pending_count,
                "released_count": released_count,
                "upstream_cost_usd": upstream_cost_usd,
                "fee_usd": fee_usd,
                "wallet_debited_cents": wallet_debited_cents,
                "pending_hold_cents": pending_hold_cents,
            },
            "groups": all_groups[:group_cap],
            "total_groups": len(all_groups),
            "recent_receipts": recent_receipts,
        }
