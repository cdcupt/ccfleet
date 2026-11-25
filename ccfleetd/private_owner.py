"""Private inference nodes for authorized owners.

Private cost is operator metadata, never an offer or a wallet obligation. The
owner slot keeps its existing no-provision/no-wipe lifecycle.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS agy_hosts (
    node_id TEXT PRIMARY KEY REFERENCES private_owner_nodes(node_id) ON DELETE CASCADE,
    seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agy_pairings (
    token_hash TEXT PRIMARY KEY REFERENCES cli_pairings(token_hash) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agy_devices (
    device_id TEXT PRIMARY KEY REFERENCES cli_devices(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agy_enrollments (
    slot_id TEXT PRIMARY KEY REFERENCES private_owner_nodes(node_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    claim_id TEXT NOT NULL,
    claim_generation INTEGER NOT NULL,
    id TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS private_owner_nodes (
    node_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE,
    monthly_cost_usd_cents INTEGER NOT NULL CHECK (monthly_cost_usd_cents >= 0),
    inference_enabled INTEGER NOT NULL DEFAULT 0 CHECK (inference_enabled IN (0,1)),
    public_usage_enabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

_CURRENT_ENROLLMENT = (
    " FROM agy_enrollments e JOIN slots s ON s.id=e.slot_id AND s.held_by=e.account_id"
    " AND s.gateway_claim_id=e.claim_id AND s.gateway_generation=e.claim_generation "
)

ENROLLMENT_GRANT = (
    "SELECT e.token_hash,e.slot_id,e.account_id,NULL AS expires_at,NULL AS api_model_binding_id,"
    "1 AS workspace" + _CURRENT_ENROLLMENT + "WHERE e.token_hash=?"
)


def _enrollment_id(conn, slot_id: str, owner: str) -> Optional[str]:
    row = conn.execute(
        "SELECT e.id" + _CURRENT_ENROLLMENT + "WHERE e.slot_id=? AND e.account_id=?",
        (slot_id, owner),
    ).fetchone()
    return row["id"] if row else None


def eligible(conn, node_id: str, account_id: str) -> bool:
    """Recheck slot ownership and the legacy admin or active home registration."""
    return (
        conn.execute(
            "SELECT 1 FROM private_owner_nodes p JOIN nodes n ON n.id=p.node_id "
            "JOIN accounts a ON a.id=p.account_id "
            "JOIN slots s ON s.node_id=p.node_id AND s.id=p.node_id "
            "WHERE p.node_id=? AND p.account_id=? AND p.inference_enabled=1 "
            "AND (a.role='admin' OR EXISTS (SELECT 1 FROM home_macs h WHERE h.node_id=p.node_id "
            "AND h.owner_id=p.account_id AND h.removed=0)) AND n.enabled=1 "
            "AND s.kind='owner' AND s.state='active' "
            "AND s.held_by=p.account_id AND s.gateway_claim_id!=''",
            (node_id, account_id),
        ).fetchone()
        is not None
    )


def slot_allowed(conn, slot_id: str, account_id: str) -> bool:
    row = conn.execute("SELECT node_id,kind,held_by FROM slots WHERE id=?", (slot_id,)).fetchone()
    if row is None or row["held_by"] != account_id:
        return False
    private = conn.execute(
        "SELECT 1 FROM private_owner_nodes WHERE node_id=?", (row["node_id"],)
    ).fetchone()
    if private is not None:
        return (
            slot_id == row["node_id"]
            and row["kind"] == "owner"
            and eligible(conn, row["node_id"], account_id)
        )
    return row["kind"] == "machine"


def invalidate(conn, node_id: str) -> None:
    """Rotate grants without touching the owner's Mac, login, history, or money."""
    conn.execute("DELETE FROM agy_enrollments WHERE slot_id=?", (node_id,))
    for row in conn.execute("SELECT id FROM slots WHERE node_id=?", (node_id,)).fetchall():
        slot_id = row["id"]
        conn.execute("DELETE FROM api_model_bindings WHERE claim_id IN "
                     "(SELECT gateway_claim_id FROM slots WHERE id=?)", (slot_id,))
        conn.execute("DELETE FROM cli_pairings WHERE slot_id=?", (slot_id,))
        conn.execute("DELETE FROM cli_devices WHERE slot_id=?", (slot_id,))
        conn.execute(
            "UPDATE slots SET gateway_claim_id=?, gateway_generation=gateway_generation+1 "
            "WHERE id=?",
            (secrets.token_hex(16), slot_id),
        )
        for key in conn.execute("SELECT id,slot_ids FROM gateway_keys WHERE revoked_at IS NULL"):
            try:
                selected = json.loads(key["slot_ids"])
            except (ValueError, TypeError):
                continue
            if isinstance(selected, list) and slot_id in selected:
                conn.execute(
                    "UPDATE gateway_keys SET generation=generation+1 WHERE id=?", (key["id"],)
                )


class PrivateOwnerStoreMixin:
    @staticmethod
    def _require_not_private_node(conn, node_id: str) -> None:
        from .store import StoreError

        if conn.execute("SELECT 1 FROM private_owner_nodes WHERE node_id=?", (node_id,)).fetchone():
            raise StoreError("private owner nodes are managed locally, never rented or reassigned")

    @staticmethod
    def _require_not_private_slot(conn, slot_id: str) -> None:
        row = conn.execute("SELECT node_id FROM slots WHERE id=?", (slot_id,)).fetchone()
        if row:
            PrivateOwnerStoreMixin._require_not_private_node(conn, row["node_id"])

    def private_owner_node(self, node_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM private_owner_nodes WHERE node_id=?", (node_id,)
            ).fetchone()
        return dict(row) if row else None

    def _private_node_annotations(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            private = {
                r["node_id"]: dict(r)
                for r in self._conn.execute("SELECT * FROM private_owner_nodes")
            }
        for node in nodes:
            if node["id"] in private:
                node["private_owner"] = private[node["id"]]
        return nodes

    def _private_slot_annotations(self, slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            private = {
                r["node_id"]: r["active_home"] for r in self._conn.execute(
                    "SELECT p.node_id, h.owner_id AS active_home FROM private_owner_nodes p "
                    "LEFT JOIN home_macs h ON h.node_id=p.node_id AND h.removed=0")
            }
            for slot in slots:
                if slot["node_id"] in private:
                    slot["private_owner"] = True
                    if private[slot["node_id"]] and slot.get("held_by") == private[slot["node_id"]]:
                        slot["home_mac"] = True
                    slot["inference_enabled"] = slot_allowed(
                        self._conn, slot["id"], slot.get("held_by") or ""
                    )
        return slots

    def slot_inference_allowed(self, slot_id: str, account_id: str) -> bool:
        with self._lock:
            return slot_allowed(self._conn, slot_id, account_id)

    def register_private_owner_node(
        self,
        node_id: str,
        account_id: str,
        unix_user: str,
        *,
        monthly_cost_usd_cents: int,
        region: str = "",
        now: float,
    ) -> dict[str, Any]:
        """Atomically register one private, inactive-inference owner node.

        This never converts a rentable node or changes an existing assignment.
        The caller receives its node token once, for private installation.
        """
        from .store import UNIX_USER_RE, StoreError, hash_token, validate_node_id

        validate_node_id(node_id)
        if not isinstance(unix_user, str) or not UNIX_USER_RE.fullmatch(unix_user):
            raise StoreError("a valid native Unix login is required")
        if (
            type(monthly_cost_usd_cents) is not int
            or not 0 <= monthly_cost_usd_cents <= 100_000_000
        ):
            raise StoreError("private monthly USD cost must be nonnegative integer cents")
        if not isinstance(region, str) or len(region) > 100 or any(ord(c) < 32 for c in region):
            raise StoreError("invalid private node region")
        token = secrets.token_hex(32)
        with self._write_txn() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM accounts WHERE id=? AND role='admin'", (account_id,)
                ).fetchone()
                is None
            ):
                raise StoreError("private owner inference requires an administrator account")
            if (
                conn.execute(
                    "SELECT 1 FROM private_owner_nodes WHERE account_id=?", (account_id,)
                ).fetchone()
                is not None
            ):
                raise StoreError("this administrator already has a private owner node")
            if (
                conn.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone()
                or conn.execute(
                    "SELECT 1 FROM slots WHERE id=? OR name=? OR pool_name=?",
                    (node_id, node_id, node_id),
                ).fetchone()
            ):
                raise StoreError("private node identity is already in use")
            conn.execute(
                "INSERT INTO nodes(id,owner,region,token_hash,created_at) VALUES(?,?,?,?,?)",
                (node_id, unix_user, region, hash_token(token), now),
            )
            conn.execute(
                "INSERT INTO slots(id,node_id,unix_user,state,held_by,claimed_at,kind,"
                "gateway_claim_id) VALUES(?,?,?,'active',?,?,'owner',?)",
                (node_id, node_id, unix_user, account_id, now, secrets.token_hex(16)),
            )
            conn.execute(
                "INSERT INTO private_owner_nodes(node_id,account_id,monthly_cost_usd_cents,"
                "created_at,updated_at) VALUES(?,?,?,?,?)",
                (node_id, account_id, monthly_cost_usd_cents, now, now),
            )
        return {"node_id": node_id, "slot_id": node_id, "node_token": token}

    def set_private_owner_inference(
        self, node_id: str, account_id: str, enabled: bool, *, now: float
    ) -> None:
        from .store import StoreError

        if type(enabled) is not bool:
            raise StoreError("private inference enablement must be boolean")
        with self._write_txn() as conn:
            row = conn.execute(
                "SELECT p.*,a.role,s.held_by,s.kind,s.state,n.access_host,n.ssh_host_key "
                "FROM private_owner_nodes p JOIN accounts a ON a.id=p.account_id "
                "JOIN nodes n ON n.id=p.node_id "
                "JOIN slots s ON s.node_id=p.node_id AND s.id=p.node_id "
                "WHERE p.node_id=? AND p.account_id=?",
                (node_id, account_id),
            ).fetchone()
            if (
                row is None
                or row["role"] != "admin"
                or row["held_by"] != account_id
                or row["kind"] != "owner"
                or row["state"] != "active"
            ):
                raise StoreError("private owner authorization is unavailable")
            if enabled and (not row["access_host"] or not row["ssh_host_key"]):
                raise StoreError("configure the private node's pinned SSH endpoint first")
            if bool(row["inference_enabled"]) != enabled:
                invalidate(conn, node_id)
            conn.execute(
                "UPDATE private_owner_nodes SET inference_enabled=?,updated_at=? WHERE node_id=?",
                (int(enabled), now, node_id),
            )

    def antigravity_ready(self, node_id: str, *, now: float) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM agy_hosts WHERE node_id=? "
                                      "AND seen_at BETWEEN ? AND ?", (node_id, now-180, now)
                                      ).fetchone() is not None

    def antigravity_pairing_status(
        self, slot_id: str, account_id: str, *, now: float
    ) -> dict[str, Any]:
        """Summarize this owner's workspace grants independently of Mac availability."""
        with self._lock:
            conn = self._conn
            if not eligible(conn, slot_id, account_id):
                return {"devices": [], "pending_count": 0, "pending_until": None,
                        "enrollment_id": None}
            enrollment_id = _enrollment_id(conn, slot_id, account_id)
            rows = conn.execute(
                "WITH pending AS ("
                " SELECT COUNT(*) AS pending_count, MAX(p.expires_at) AS pending_until"
                " FROM cli_pairings p JOIN agy_pairings w ON w.token_hash=p.token_hash"
                " WHERE p.slot_id=:slot AND p.account_id=:owner AND p.expires_at>:now"
                "), devices AS ("
                " SELECT d.id,d.name,d.created_at,d.last_seen_at FROM cli_devices d"
                " JOIN agy_devices w ON w.device_id=d.id"
                " WHERE d.slot_id=:slot AND d.account_id=:owner AND d.revoked_at IS NULL"
                ") SELECT * FROM pending LEFT JOIN devices ON 1"
                " ORDER BY devices.created_at DESC, devices.name",
                {"slot": slot_id, "owner": account_id, "now": now},
            ).fetchall()
        return {
            "devices": [
                {key: row[key] for key in ("id", "name", "created_at", "last_seen_at")}
                for row in rows if row["id"] is not None
            ],
            "pending_count": rows[0]["pending_count"],
            "pending_until": rows[0]["pending_until"],
            "enrollment_id": enrollment_id,
        }

    @staticmethod
    def _antigravity_owner_slot(conn, owner):
        from .store import StoreError

        slot = conn.execute(
            "SELECT s.id,s.gateway_claim_id,s.gateway_generation FROM private_owner_nodes p "
            "JOIN slots s ON s.id=p.node_id WHERE p.account_id=?", (owner,),
        ).fetchone()
        if slot is None or not eligible(conn, slot["id"], owner):
            raise StoreError("a ready private Mac owned by this account is required")
        return slot

    def set_antigravity_enrollment(self, owner: str, *, expected_id: str, now: float) -> str:
        """Create or replace one reusable code, revealing its secret only on this response."""
        from . import cli_access
        from .store import StoreError

        with self._write_txn() as conn:
            slot = self._antigravity_owner_slot(conn, owner)
            if not self.antigravity_ready(slot["id"], now=now):
                raise StoreError("the Antigravity workspace service is offline")
            if expected_id != (_enrollment_id(conn, slot["id"], owner) or ""):
                raise StoreError("the Gemini enrollment code changed; refresh the page")
            code = cli_access.GEMINI_ENROLL_PREFIX + secrets.token_urlsafe(32)
            conn.execute("DELETE FROM agy_enrollments WHERE slot_id=?", (slot["id"],))
            conn.execute(
                "INSERT INTO agy_enrollments(slot_id,account_id,claim_id,claim_generation,id,"
                "token_hash) VALUES (?,?,?,?,?,?)",
                (slot["id"], owner, slot["gateway_claim_id"], slot["gateway_generation"],
                 secrets.token_hex(16), cli_access.token_hash(code)),
            )
            return code

    def revoke_antigravity_enrollment(self, owner: str, enrollment_id: str) -> None:
        """Stop new registrations without touching any computer's existing credentials."""
        from .store import StoreError

        with self._write_txn() as conn:
            slot = self._antigravity_owner_slot(conn, owner)
            removed = conn.execute(
                "DELETE FROM agy_enrollments WHERE slot_id=? AND account_id=? AND id=?",
                (slot["id"], owner, enrollment_id),
            ).rowcount
            if not removed:
                raise StoreError("the Gemini enrollment code changed; refresh the page")

    def antigravity_pairing(self, owner: str, *, now: float) -> str:
        """Only a private Mac's current administrator may grant an interactive session."""
        from . import cli_access
        from .store import StoreError

        with self._write_txn() as conn:
            slot = self._antigravity_owner_slot(conn, owner)
            if not self.antigravity_ready(slot["id"], now=now):
                raise StoreError("the Antigravity workspace service is offline")
            code = self._mint_cli_pairing(conn, slot["id"], owner, now=now)
            conn.execute("INSERT INTO agy_pairings VALUES (?)", (cli_access.token_hash(code),))
            return code

    def private_owner_access(
        self, node_id: str, gateway_public_key: str = "", *, workspace_at: Optional[float] = None
    ) -> Optional[dict[str, Any]]:
        """Fixed public-key grants only; no remote commands or account management."""
        with self._write_txn() as conn:
            row = conn.execute(
                "SELECT p.account_id,s.id,s.unix_user FROM private_owner_nodes p "
                "JOIN slots s ON s.node_id=p.node_id AND s.id=p.node_id WHERE p.node_id=?",
                (node_id,),
            ).fetchone()
            if row is None:
                return None
            allowed = slot_allowed(conn, row["id"], row["account_id"])
            workspace_keys = []
            if allowed:
                workspace_keys = [r[0] for r in conn.execute(
                    "SELECT d.public_key FROM cli_devices d JOIN agy_devices w ON w.device_id=d.id "
                    "WHERE d.slot_id=? AND d.account_id=? AND d.revoked_at IS NULL "
                    "ORDER BY d.public_key", (row["id"], row["account_id"]))]
            devices = self.cli_public_keys(row["id"]) if allowed else []
            if workspace_at is not None and allowed:
                conn.execute("INSERT INTO agy_hosts VALUES (?,?) ON CONFLICT(node_id) "
                             "DO UPDATE SET seen_at=excluded.seen_at", (node_id, workspace_at))
            else:
                conn.execute("DELETE FROM agy_hosts WHERE node_id=?", (node_id,))
                devices = [key for key in devices if key not in workspace_keys]
                workspace_keys = []
            return {
                **({"workspace_public_keys": workspace_keys} if workspace_keys else {}),
                "version": 1,
                "unix_user": row["unix_user"],
                "enabled": allowed,
                "device_public_keys": devices,
                "gateway_public_keys": self.gateway_public_keys(row["id"], gateway_public_key)
                if allowed and gateway_public_key
                else [],
            }

    def private_owner_quota(self, node_id: str) -> Optional[dict[str, Any]]:
        """One bounded usage-refresh timestamp for this authenticated owner's node."""
        with self._lock:
            row = self._conn.execute(
                "SELECT s.quota_wanted_at FROM private_owner_nodes p "
                "JOIN nodes n ON n.id=p.node_id "
                "JOIN accounts a ON a.id=p.account_id "
                "JOIN slots s ON s.node_id=p.node_id AND s.id=p.node_id "
                "WHERE p.node_id=? AND n.enabled=1 AND p.inference_enabled=1 AND (a.role='admin' "
                "OR EXISTS(SELECT 1 FROM home_macs h WHERE h.node_id=p.node_id AND h.removed=0 "
                "AND h.owner_id=p.account_id)) "
                "AND s.kind='owner' AND s.state='active' AND s.held_by=p.account_id "
                "AND s.gateway_claim_id!=''",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        wanted = row["quota_wanted_at"]
        return ({"requested_at": wanted} if type(wanted) in (int, float)
                and 0 < wanted < 1e11 else {})


def is_private(node: Mapping[str, Any]) -> bool:
    return isinstance(node.get("private_owner"), Mapping)


class OperatorView:
    """Read-only rendering scope; unrelated administrators cannot see this node."""

    def __init__(self, store, account_id: str):
        self.store, self.account_id = store, account_id

    def __getattr__(self, name):
        return getattr(self.store, name)

    def list_nodes(self):
        return [
            node
            for node in self.store.list_nodes()
            if not is_private(node) or node["private_owner"]["account_id"] == self.account_id
        ]

    def list_slots(self, **kwargs):
        visible = {node["id"] for node in self.list_nodes()}
        return [slot for slot in self.store.list_slots(**kwargs) if slot["node_id"] in visible]

    def latest_heartbeats(self):
        visible = {node["id"] for node in self.list_nodes()}
        return {
            key: value for key, value in self.store.latest_heartbeats().items() if key in visible
        }

    def held_slot_count(self, account_id):
        from .slots import HELD

        return sum(slot["state"] in HELD for slot in self.list_slots(held_by=account_id))

    def open_alerts(self, *args, **kwargs):
        visible = {node["id"] for node in self.list_nodes()}
        return [
            alert
            for alert in self.store.open_alerts(*args, **kwargs)
            if alert["node_id"] in visible
        ]
