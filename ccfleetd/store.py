"""SQLite-backed storage for nodes, heartbeats and alerts.

Node tokens are never stored in clear text: only a SHA-256 hash is kept, and the
clear token is shown exactly once when the node is created or rotated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
# The owner becomes a unix account name and is interpolated into a command the
# console hands an operator to paste as root, so anything outside this set is a
# command-injection vector, not merely a cosmetic problem.
# Matches what Debian and Ubuntu adduser will actually accept (NAME_REGEX), so a
# name the console approves cannot fail halfway through provisioning. Notably
# that excludes a leading underscore, which adduser refuses without
# --allow-bad-names.
OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Console login names. Same shape as an owner name so the two cannot drift, and
# so a person's login can simply be their owner name.
USERNAME_RE = OWNER_RE
ROLES = ("admin", "owner")
TOKEN_BYTES = 32

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    pinned_version TEXT NOT NULL DEFAULT '',
    rc_expected INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_heartbeats_node_ts ON heartbeats(node_id, ts DESC);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_alerts_node_rule ON alerts(node_id, rule);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    owner TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


class StoreError(ValueError):
    """Raised for invalid identifiers or missing rows."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
        raise StoreError(
            "node id must be 2-40 chars of lowercase letters, digits and hyphens, "
            "starting with a letter or digit"
        )
    return node_id


def _row_to_node(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner": row["owner"],
        "region": row["region"],
        "pinned_version": row["pinned_version"],
        "rc_expected": bool(row["rc_expected"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def _row_to_heartbeat(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "node_id": row["node_id"], "ts": row["ts"],
            "payload": json.loads(row["payload"])}


def _row_to_alert(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "node_id": row["node_id"], "rule": row["rule"],
            "level": row["level"], "message": row["message"],
            "opened_at": row["opened_at"], "closed_at": row["closed_at"]}


class Store:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- nodes -------------------------------------------------------------

    def add_node(self, node_id: str, owner: str, region: str = "",
                 pinned_version: str = "", rc_expected: bool = False,
                 now: float = 0.0) -> str:
        validate_node_id(node_id)
        owner = owner.strip()
        if not OWNER_RE.match(owner):
            raise StoreError(
                "owner must be a valid unix user name: start with a lowercase letter, then "
                "lowercase letters, digits, underscore or hyphen, max 32 characters"
            )
        token = secrets.token_hex(TOKEN_BYTES)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO nodes (id, owner, region, token_hash, pinned_version, "
                    "rc_expected, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    (node_id, owner.strip(), region.strip(), hash_token(token),
                     pinned_version.strip(), int(rc_expected), now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"node {node_id!r} already exists") from exc
            self._conn.commit()
        return token

    def rotate_token(self, node_id: str) -> str:
        token = secrets.token_hex(TOKEN_BYTES)
        self._update_node(node_id, "token_hash", hash_token(token))
        return token

    def set_pinned_version(self, node_id: str, version: str) -> None:
        self._update_node(node_id, "pinned_version", version.strip())

    def set_enabled(self, node_id: str, enabled: bool) -> None:
        self._update_node(node_id, "enabled", int(enabled))

    def set_rc_expected(self, node_id: str, expected: bool) -> None:
        self._update_node(node_id, "rc_expected", int(expected))

    def _update_node(self, node_id: str, column: str, value: Any) -> None:
        if column not in {"token_hash", "pinned_version", "enabled", "rc_expected"}:
            raise StoreError(f"cannot update column {column!r}")
        with self._lock:
            cur = self._conn.execute(f"UPDATE nodes SET {column} = ? WHERE id = ?",  # noqa: S608
                                     (value, node_id))
            self._conn.commit()
        if cur.rowcount == 0:
            raise StoreError(f"unknown node {node_id!r}")

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            self._conn.execute("DELETE FROM heartbeats WHERE node_id = ?", (node_id,))
            self._conn.execute("DELETE FROM alerts WHERE node_id = ?", (node_id,))
            self._conn.commit()
        if cur.rowcount == 0:
            raise StoreError(f"unknown node {node_id!r}")

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def list_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM nodes ORDER BY owner, id").fetchall()
        return [_row_to_node(r) for r in rows]

    def node_for_token(self, token: str) -> Optional[dict[str, Any]]:
        """Return the enabled node owning ``token``, or None."""
        if not token:
            return None
        digest = hash_token(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE token_hash = ? AND enabled = 1", (digest,)
            ).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], digest):
            return None
        return _row_to_node(row)

    # -- heartbeats --------------------------------------------------------

    def insert_heartbeat(self, node_id: str, ts: float, payload: dict[str, Any]) -> int:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO heartbeats (node_id, ts, payload) VALUES (?, ?, ?)",
                (node_id, ts, body),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def recent_heartbeats(self, node_id: str, limit: int = 2) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM heartbeats WHERE node_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (node_id, limit),
            ).fetchall()
        return [_row_to_heartbeat(r) for r in rows]

    def latest_heartbeats(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT h.* FROM heartbeats h JOIN (SELECT node_id, MAX(id) AS max_id "
                "FROM heartbeats GROUP BY node_id) m ON h.id = m.max_id"
            ).fetchall()
        return {r["node_id"]: _row_to_heartbeat(r) for r in rows}

    def prune_heartbeats(self, older_than_ts: float) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM heartbeats WHERE ts < ?", (older_than_ts,))
            self._conn.commit()
        return cur.rowcount

    # -- alerts ------------------------------------------------------------

    def open_alerts(self, node_id: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM alerts WHERE closed_at IS NULL"
        params: tuple[Any, ...] = ()
        if node_id is not None:
            query += " AND node_id = ?"
            params = (node_id,)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY opened_at", params).fetchall()
        return [_row_to_alert(r) for r in rows]

    def open_alert(self, node_id: str, rule: str, level: str, message: str, now: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts (node_id, rule, level, message, opened_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (node_id, rule, level, message, now),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def close_alert(self, alert_id: int, now: float) -> None:
        with self._lock:
            self._conn.execute("UPDATE alerts SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                               (now, alert_id))
            self._conn.commit()

    # -- console accounts -------------------------------------------------

    def add_user(self, username: str, password_hash: str, role: str = "owner",
                 owner: str = "", now: float = 0.0) -> None:
        """A console login. Never takes a plaintext password; hashing is the caller's job."""
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise StoreError(
                "username must start with a lowercase letter, then lowercase letters, "
                "digits, underscore or hyphen, max 32 characters"
            )
        if role not in ROLES:
            raise StoreError(f"role must be one of {', '.join(ROLES)}")
        owner = owner.strip()
        if role == "owner":
            # An owner login that maps to no owner would see an empty dashboard
            # forever, which looks like a broken account rather than an empty one.
            if not owner:
                owner = username
            if not OWNER_RE.match(owner):
                raise StoreError("owner must be a valid unix user name")
        else:
            owner = ""
        if not password_hash:
            raise StoreError("password_hash must not be empty")
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, role, owner, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (username, password_hash, role, owner, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"user {username!r} already exists") from exc
            self._conn.commit()

    def get_user(self, username: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT username, password_hash, role, owner, created_at FROM users "
                "WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        """Without the hashes: nothing that renders or logs should ever hold one."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, role, owner, created_at FROM users "
                "ORDER BY role DESC, username").fetchall()
        return [dict(r) for r in rows]

    def set_password(self, username: str, password_hash: str) -> bool:
        if not password_hash:
            raise StoreError("password_hash must not be empty")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (password_hash, username))
            self._conn.commit()
        return cur.rowcount > 0

    def remove_user(self, username: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._conn.commit()
        return cur.rowcount > 0

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_alert(r) for r in rows]
