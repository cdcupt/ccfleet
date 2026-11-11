"""Owner-attributed cumulative daily usage, independent of machine availability.

Snapshots replace a source/day row; retries never add rolling totals. Nothing
here is a charge, a wallet entry, or a provider credential.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Callable, Optional

from ccfleet_agent import relay_metrics

DAY = relay_metrics.DAY_SECONDS
WINDOW = relay_metrics.WINDOW_DAYS
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_ledger_sources (
    source_key TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    account_switched_at REAL NOT NULL DEFAULT 0,
    bound_fp TEXT NOT NULL,
    private INTEGER NOT NULL CHECK (private IN (0,1)),
    claim_day_authorized INTEGER NOT NULL DEFAULT 0 CHECK (claim_day_authorized IN (0,1)),
    claim_day_proof TEXT NOT NULL DEFAULT '',
    last_observed_at REAL NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0,1))
);
CREATE TABLE IF NOT EXISTS usage_ledger_days (
    source_key TEXT NOT NULL,
    day INTEGER NOT NULL,
    observed_at REAL NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (source_key, day)
);
CREATE INDEX IF NOT EXISTS ix_usage_ledger_window ON usage_ledger_days(day);
"""


def _instant(value):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and 0 <= value <= relay_metrics.MAX_EPOCH
    )


def identity(slot: Mapping[str, Any], bound_fp: str) -> str:
    """Authorization claim IDs may rotate while the same counters continue."""
    fields = [
        slot["node_id"],
        slot["id"],
        format(float(slot["claimed_at"]), ".17g"),
        format(float(slot.get("account_switched_at") or 0), ".17g"),
        bound_fp,
    ]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def _flatten(value, prefix=""):
    result = {}
    for name, item in value.items():
        key = prefix + name
        if isinstance(item, dict):
            result.update(_flatten(item, key + "."))
        elif isinstance(item, list):
            for row in item:
                result.update(
                    _flatten(
                        {k: v for k, v in row.items() if k != "model"},
                        key + "." + row["model"] + ".",
                    )
                )
        elif type(item) is int:
            result[key] = item
    return result


def _dominates(new: dict, old: dict) -> bool:
    previous, current = _flatten(old), _flatten(new)
    return all(current.get(name, 0) >= value for name, value in previous.items())


def _read_day(raw, day, now):
    """Revalidate stored JSON through the same strict wire boundary before aggregation."""
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid saved usage day")
    success = value["success"]
    report = {
        "version": 1,
        "observed_at": now,
        "window_days": WINDOW,
        **dict.fromkeys(relay_metrics.COUNTERS, 0),
        "requests": success,
        "success": success,
        "last_success_at": day * DAY if success else None,
        "latencies": {key: {"count": 0, "mean": 0, "max": 0} for key in relay_metrics.TIMINGS},
        "token_usage": value["token_usage"],
        "usage_days": {str(day): value},
    }
    if "model_usage" in value:
        report["model_usage"] = value["model_usage"]
    return relay_metrics.validate_report(report)["usage_days"][str(day)]


def _entry(payload, slot, bound_fp, *, zero=False):
    """A node cannot attribute counters to an account named in its own payload."""
    if not isinstance(payload, dict) or payload.get("node_id") != slot["node_id"]:
        return None
    expected = "private_owner" if slot["kind"] == "owner" else "machine"
    if payload.get("mode") != expected:
        return None
    rows = payload.get("slots")
    if not isinstance(rows, list):
        return None
    matches = [
        row for row in rows if isinstance(row, dict) and row.get("unix_user") == slot["unix_user"]
    ]
    if len(matches) != 1:
        return None
    row = matches[0]
    creds = row.get("credentials")
    if (
        row.get("present") is not True
        or not isinstance(creds, dict)
        or creds.get("present") is not True
        or creds.get("logged_in") is not True
        or creds.get("bound_fp") != bound_fp
        or creds.get("account_fp") != bound_fp
    ):
        return None
    if slot["kind"] == "machine" and row.get("provisioned_for") != slot["claimed_at"]:
        return None
    if zero:
        report = row.get("relay")
        if (
            not isinstance(report, dict)
            or report.get("requests") != 0
            or report.get("success") != 0
        ):
            return None
        try:
            relay_metrics.validate_report(report)
        except (ValueError, TypeError, OverflowError):
            return None
    return row


class UsageLedgerStoreMixin:
    def set_private_owner_public_usage(
        self, node_id: str, account_id: str, enabled: bool, *, now: float
    ) -> None:
        with self._write_txn() as conn:
            self._usage_consent(conn, node_id, account_id, enabled, now)

    @staticmethod
    def _usage_consent(conn, node_id, account_id, enabled, now):
        from .store import StoreError

        if type(enabled) is not bool or not _instant(now):
            raise StoreError("invalid private usage consent")
        row = conn.execute(
            "SELECT p.account_id,a.role FROM private_owner_nodes p "
            "JOIN accounts a ON a.id=p.account_id WHERE p.node_id=?",
            (node_id,),
        ).fetchone()
        if row is None or row["account_id"] != account_id or row["role"] != "admin":
            raise StoreError("private usage consent requires its administrator owner")
        conn.execute(
            "UPDATE private_owner_nodes SET public_usage_enabled=?,updated_at=? WHERE node_id=?",
            (int(enabled), now, node_id),
        )

    @staticmethod
    def _usage_slot(conn, slot_id, account_id, claimed_at, bound_fp):
        from .store import StoreError

        if not isinstance(bound_fp, str) or re.fullmatch(r"[0-9a-f]{16}", bound_fp) is None:
            raise StoreError("invalid usage account binding")
        row = conn.execute(
            "SELECT s.*,n.enabled AS node_enabled FROM slots s "
            "JOIN nodes n ON n.id=s.node_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if (
            row is None
            or row["held_by"] != account_id
            or row["claimed_at"] != claimed_at
            or row["kind"] not in ("machine", "owner")
            or not _instant(claimed_at)
        ):
            raise StoreError("usage assignment does not match its owner")
        slot = dict(row)
        private = conn.execute(
            "SELECT * FROM private_owner_nodes WHERE node_id=?", (slot["node_id"],)
        ).fetchone()
        if slot["kind"] == "owner" and private is None:
            raise StoreError("ordinary owner nodes do not publish relay usage")
        if private is not None and (
            private["account_id"] != account_id or private["public_usage_enabled"] != 1
        ):
            raise StoreError("private usage publication is not enabled")
        return slot, private is not None

    @staticmethod
    def _usage_archive_evidence(conn, slot, bound_fp, *, zero=False):
        start = max(slot["claimed_at"], slot.get("account_switched_at") or 0)
        order = "ASC" if zero else "DESC"
        rows = conn.execute(
            "SELECT id,ts,payload FROM heartbeats WHERE node_id=? AND ts>=? "
            f"ORDER BY id {order} LIMIT 512",
            (slot["node_id"], start),
        )
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                continue
            entry = _entry(payload, slot, bound_fp)
            if entry is None:
                continue
            if not zero:
                return row["id"]
            try:
                report = relay_metrics.validate_report(entry.get("relay"))
            except (ValueError, TypeError, OverflowError):
                continue
            if not start <= report["observed_at"] <= row["ts"] + 60:
                continue
            # Only the FIRST attributable observation can prove an empty start.
            # A later zero after a reset must not erase a prior positive baseline.
            return row["id"] if report["requests"] == report["success"] == 0 else None
        return None

    @staticmethod
    def _save_usage_days(
        conn,
        slot,
        account_id,
        bound_fp,
        report,
        now,
        *,
        private,
        authorize_claim_day=False,
        proof="",
    ):
        from .store import StoreError

        source_key = identity(slot, bound_fp)
        source = conn.execute(
            "SELECT * FROM usage_ledger_sources WHERE source_key=?", (source_key,)
        ).fetchone()
        if source is not None and source["account_id"] != account_id:
            raise StoreError("recorded usage belongs to its original owner")
        boundary = int(max(slot["claimed_at"], slot.get("account_switched_at") or 0) // DAY)
        authorized = authorize_claim_day or bool(source and source["claim_day_authorized"])
        observed = report["observed_at"]
        today = int(now // DAY)
        days = {
            int(day): snapshot
            for day, snapshot in report["usage_days"].items()
            if today - WINDOW < int(day) <= today
            and (int(day) > boundary or int(day) == boundary and authorized)
        }
        if source is None and not days:
            return {
                "source_key": None,
                "days_changed": 0,
                "claim_day_authorized": authorized,
                "needs_review": False,
            }
        if source is None:
            conn.execute(
                "INSERT INTO usage_ledger_sources(source_key,node_id,slot_id,account_id,"
                "claimed_at,account_switched_at,bound_fp,private,claim_day_authorized,"
                "claim_day_proof,last_observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_key,
                    slot["node_id"],
                    slot["id"],
                    account_id,
                    slot["claimed_at"],
                    slot.get("account_switched_at") or 0,
                    bound_fp,
                    int(private),
                    int(authorized),
                    proof,
                    observed,
                ),
            )
        elif authorize_claim_day:
            conn.execute(
                "UPDATE usage_ledger_sources SET claim_day_authorized=1,claim_day_proof=? "
                "WHERE source_key=?",
                (proof, source_key),
            )
        changed = 0
        conflict = False
        previous = {
            row["day"]: row
            for row in conn.execute(
                "SELECT * FROM usage_ledger_days WHERE source_key=? AND day>=? AND day<=?",
                (source_key, today - WINDOW + 1, today),
            )
        }
        for day, old in previous.items():
            if day not in days and observed > old["observed_at"]:
                conflict = True
        for day, snapshot in days.items():
            old = previous.get(day)
            raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            if old is not None:
                if observed < old["observed_at"]:
                    continue
                if not _dominates(snapshot, json.loads(old["snapshot_json"])):
                    conflict |= observed > old["observed_at"]
                    continue
            conn.execute(
                "INSERT INTO usage_ledger_days VALUES(?,?,?,?) "
                "ON CONFLICT(source_key,day) DO UPDATE SET observed_at=excluded.observed_at,"
                "snapshot_json=excluded.snapshot_json",
                (source_key, day, observed, raw),
            )
            changed += int(old is None or old["snapshot_json"] != raw)
        if source is None or observed >= source["last_observed_at"]:
            conn.execute(
                "UPDATE usage_ledger_sources SET last_observed_at=?,needs_review=? "
                "WHERE source_key=?",
                (observed, int(conflict), source_key),
            )
        return {
            "source_key": source_key,
            "days_changed": changed,
            "claim_day_authorized": authorized,
            "needs_review": conflict,
        }

    def import_usage_days(
        self,
        slot_id: str,
        account_id: str,
        claimed_at: float,
        bound_fp: str,
        report: Mapping[str, Any],
        *,
        now: float,
        include_claim_day: bool = False,
        claim_day_proof: str = "",
    ) -> dict:
        """Operator-only import of retained numeric data; never pretend it was a heartbeat."""
        with self._write_txn() as conn:
            return self._import_usage_days(
                conn,
                slot_id,
                account_id,
                claimed_at,
                bound_fp,
                report,
                now=now,
                include_claim_day=include_claim_day,
                claim_day_proof=claim_day_proof,
            )

    def _import_usage_days(
        self,
        conn,
        slot_id,
        account_id,
        claimed_at,
        bound_fp,
        report,
        *,
        now,
        include_claim_day=False,
        claim_day_proof="",
    ):
        from .store import StoreError

        if not _instant(now) or type(include_claim_day) is not bool:
            raise StoreError("invalid usage import clock or claim-day flag")
        clean = relay_metrics.validate_report(report)
        if "usage_days" not in clean or clean["observed_at"] > now + 60:
            raise StoreError("daily usage evidence is unavailable")
        slot, private = self._usage_slot(conn, slot_id, account_id, claimed_at, bound_fp)
        if self._usage_archive_evidence(conn, slot, bound_fp) is None:
            raise StoreError("matching archived assignment/account evidence is required")
        if include_claim_day and (
            re.fullmatch(r"[0-9a-f]{64}", claim_day_proof or "") is None
            or self._usage_archive_evidence(conn, slot, bound_fp, zero=True) is None
        ):
            raise StoreError("claim-day import needs reviewed proof and a matching zero snapshot")
        return self._save_usage_days(
            conn,
            slot,
            account_id,
            bound_fp,
            clean,
            now,
            private=private,
            authorize_claim_day=include_claim_day,
            proof=claim_day_proof,
        )

    def import_usage_days_batch(
        self,
        imports: list[dict],
        *,
        now: float,
        private_consent: Optional[dict] = None,
        validate: Optional[Callable[[], bool]] = None,
    ) -> list[dict]:
        """Operator cutover: consent, all sources, and the caller's totals check commit together."""
        from .store import StoreError

        if not isinstance(imports, list) or not 1 <= len(imports) <= 32 or not _instant(now):
            raise StoreError("invalid usage import batch")
        required = {"slot_id", "account_id", "claimed_at", "bound_fp", "report"}
        optional = {"include_claim_day", "claim_day_proof"}
        if any(not isinstance(row, dict) or set(row) - optional != required for row in imports):
            raise StoreError("invalid usage import fields")
        if private_consent is not None and (
            not isinstance(private_consent, dict)
            or set(private_consent) != {"node_id", "account_id", "enabled"}
        ):
            raise StoreError("invalid private usage consent fields")
        with self._write_txn() as conn:
            if private_consent is not None:
                self._usage_consent(conn, **private_consent, now=now)
            result = [self._import_usage_days(conn, **row, now=now) for row in imports]
            if validate is not None and (not callable(validate) or validate() is not True):
                raise StoreError("usage import verification did not match")
            return result

    def observe_usage_days(
        self, node_id: str, payload: Mapping[str, Any], *, now: float, max_age_s: float
    ) -> int:
        """Consume only fresh, correctly bound daily snapshots from this node's heartbeat."""
        if not _instant(now) or not _instant(max_age_s) or max_age_s <= 0:
            return 0
        entries = payload.get("slots") if isinstance(payload, Mapping) else None
        if not isinstance(entries, list):
            return 0
        changed = 0
        with self._write_txn() as conn:
            slots = conn.execute(
                "SELECT * FROM slots WHERE node_id=? AND state='active' AND held_by IS NOT NULL",
                (node_id,),
            ).fetchall()
            for row in slots:
                slot = dict(row)
                candidates = [
                    entry
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("unix_user") == slot["unix_user"]
                ]
                if len(candidates) != 1:
                    continue
                fp = (candidates[0].get("credentials") or {}).get("bound_fp")
                try:
                    slot, private = self._usage_slot(
                        conn, slot["id"], slot["held_by"], slot["claimed_at"], fp
                    )
                    if not slot["node_enabled"] or _entry(payload, slot, fp) is None:
                        continue
                    clean = relay_metrics.validate_report(candidates[0].get("relay"))
                    start = max(slot["claimed_at"], slot.get("account_switched_at") or 0)
                    if (
                        "usage_days" not in clean
                        or not max(start, now - max_age_s) <= clean["observed_at"] <= now + 60
                    ):
                        continue
                    zero = self._usage_archive_evidence(conn, slot, fp, zero=True)
                    result = self._save_usage_days(
                        conn,
                        slot,
                        slot["held_by"],
                        fp,
                        clean,
                        now,
                        private=private,
                        authorize_claim_day=zero is not None,
                        proof="heartbeat:" + str(zero) if zero is not None else "",
                    )
                    changed += result["days_changed"]
                except (ValueError, TypeError, OverflowError):
                    continue
        return changed

    def public_usage_ledger(self, now: float) -> dict:
        """Internal source DTO. Public serializers must emit only masked account totals."""
        if not _instant(now):
            raise ValueError("invalid usage ledger clock")
        first, today = int(now // DAY) - WINDOW + 1, int(now // DAY)
        with self._lock:
            sources = self._conn.execute(
                "SELECT u.* FROM usage_ledger_sources u "
                "JOIN accounts a ON a.id=u.account_id LEFT JOIN private_owner_nodes p ON "
                "p.node_id=u.node_id AND p.account_id=u.account_id "
                "WHERE u.private=0 OR (p.public_usage_enabled=1 AND a.role='admin')"
            ).fetchall()
            rows = []
            for source in sources:
                saved = dict(source)
                saved["days"] = []
                for row in self._conn.execute(
                    "SELECT day,snapshot_json FROM usage_ledger_days WHERE source_key=? "
                    "AND day>=? AND day<=? ORDER BY day",
                    (source["source_key"], first, today),
                ):
                    try:
                        saved["days"].append(_read_day(row["snapshot_json"], row["day"], now))
                    except (ValueError, TypeError, KeyError, OverflowError):
                        saved["needs_review"] = 1
                rows.append(saved)
        return {"sources": rows}

    def prune_usage_ledger(self, now: float) -> int:
        if not _instant(now):
            raise ValueError("invalid usage ledger clock")
        with self._write_txn() as conn:
            return conn.execute(
                "DELETE FROM usage_ledger_days WHERE day<?", (int(now // DAY) - WINDOW + 1,)
            ).rowcount
