"""SQLite-backed storage for nodes, heartbeats and alerts.

Node tokens are never stored in clear text: only a SHA-256 hash is kept, and the
clear token is shown exactly once when the node is created or rotated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from . import slots as slotstates
from .desired import is_login_url

log = logging.getLogger("ccfleetd.store")

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
    created_at REAL NOT NULL,
    -- When a device token was last handed over for this node. The time only;
    -- the credential itself lives in `logins` for the life of the request and
    -- nowhere else.
    device_token_at REAL NOT NULL DEFAULT 0
);
-- One in-flight sign-in per node. A row exists only while a login is being
-- driven from the console; it is deleted when the login finishes, so an absent
-- row is the normal state. The verification code lives here for the seconds
-- between the operator pasting it and the node consuming it, and is cleared as
-- soon as the node reports it was used. No OAuth token ever reaches this table:
-- the credential that results is written on the node and stays there.
CREATE TABLE IF NOT EXISTS logins (
    node_id TEXT PRIMARY KEY,
    requested_at REAL NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'requested',
    url TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    -- 'login' signs the node in; 'token' mints a device credential the owner
    -- carries away. Same dance, different command, so one table drives both.
    kind TEXT NOT NULL DEFAULT 'login',
    -- Only ever set for kind='token', and only between the node reporting it
    -- and the console showing it. Readable until the attempt expires or
    -- somebody says they are done with it. See read_secret().
    secret TEXT NOT NULL DEFAULT ''
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
-- A person who rents slots from us, as Google describes them. Deliberately
-- separate from `users` below, which is the console operator's own basic-auth
-- login and predates all of this: an operator runs machines, an account rents
-- a slot on one, and conflating them behind a role column while a password
-- table also exists helps nobody.
--
-- No password column here, on purpose. Identity comes from Google.
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,                  -- ours, not Google's
    google_sub TEXT NOT NULL UNIQUE,      -- stable even when the email changes
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',    -- 'user' | 'admin'
    -- How many slots this person may hold. Zero until the operator grants
    -- some: a bug that grants nobody anything is a support message, and a bug
    -- that grants everybody a slot is a bill.
    slot_quota INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL DEFAULT 0
);
-- One Linux user on one machine. The unit a person holds.
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    unix_user TEXT NOT NULL,              -- unique on that machine
    state TEXT NOT NULL,                  -- see ccfleetd/slots.py
    held_by TEXT REFERENCES accounts(id), -- NULL only while free
    claimed_at REAL,
    released_at REAL,
    device_token_at REAL NOT NULL DEFAULT 0,
    UNIQUE (node_id, unix_user)
);
CREATE INDEX IF NOT EXISTS ix_slots_state ON slots(state);
CREATE INDEX IF NOT EXISTS ix_slots_held_by ON slots(held_by);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    owner TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


SLOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
UNIX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class StoreError(ValueError):
    """Raised for invalid identifiers or missing rows."""


class QuotaExceeded(StoreError):
    """The account holds as many slots as its allowance permits."""


class NoSlotAvailable(StoreError):
    """Nothing free to hand out right now."""



def _without_secret(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload as it should be kept, which is without the minted token.

    A heartbeat is archived whole for the retention window. A device token
    riding up inside one would therefore outlive the single showing it is
    promised by thirty days, in a second copy nothing points at and
    ``read_secret`` cannot reach. Redacting here rather than at the call site
    makes it a property of storing a heartbeat, not something each caller has
    to remember.
    """
    login = (payload.get("reconcile") or {}).get("login")
    if not isinstance(login, Mapping) or "secret" not in login:
        return dict(payload)
    reconcile = dict(payload["reconcile"])
    reconcile["login"] = {k: v for k, v in login.items() if k != "secret"}
    return {**payload, "reconcile": reconcile}


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
        # A time, never a credential: when a device token was last handed over.
        # Keys are whitelisted here, so a column that is not named is a column
        # that does not exist as far as the rest of the server is concerned —
        # which is why the hash beside it has never leaked.
        "device_token_at": row["device_token_at"],
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
            # CREATE TABLE IF NOT EXISTS does nothing to a table that already
            # exists, so a database written before these columns existed keeps
            # the old shape. Add them here rather than asking anyone to migrate
            # by hand; both are defaulted, so old rows stay valid.
            self._add_missing_columns("nodes", {
                # When a device token was last handed over for this node. The
                # time only — the credential itself lives in `logins` for the
                # life of the request and nowhere else. Without this the console
                # has no memory of a flow that worked, and looks exactly as it
                # did before you started.
                "device_token_at": "REAL NOT NULL DEFAULT 0",
            })
            self._add_missing_columns("nodes", {
                # What a machine needs once it carries more than one person.
                # capacity is declared by the operator, not guessed from RAM:
                # they know what they sold. 1 keeps every existing node behaving
                # exactly as it does now.
                "capacity": "INTEGER NOT NULL DEFAULT 1",
                "tier": "TEXT NOT NULL DEFAULT 'dedicated'",
            })
            self._add_missing_columns("logins", {
                "kind": "TEXT NOT NULL DEFAULT 'login'",
                "secret": "TEXT NOT NULL DEFAULT ''",
            })
            self._conn.commit()

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        """Idempotent ALTER TABLE ADD COLUMN. Called with literals only."""
        have = {row["name"] for row in
                self._conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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

    # -- sign-in, driven from the console ---------------------------------
    #
    # The node does the signing in; the server only carries intent one way and
    # progress the other. It never holds the resulting credential.

    LOGIN_ACTIVE_STATES = ("requested", "url_ready", "code_sent")
    # How far along an attempt is. A node's report is one beat late by
    # construction — it acts after posting — so a report can arrive describing
    # a step the console has already moved past. Applying it would rewind the
    # attempt, and the console's own progress is the part that gets lost.
    # 'ready' belongs here too: it holds a minted token, and a late 'code_sent'
    # landing on it would overwrite the row and destroy the credential. 'done'
    # and 'failed' stay out — they are not progress, they end the attempt, and
    # they must land whatever step it was on.
    LOGIN_ORDER = {"requested": 0, "url_ready": 1, "code_sent": 2, "ready": 3}
    # 'ready' means the node's work is done and a minted token is waiting for
    # someone to collect it. It is not active — the node has nothing left to do
    # — but the row must survive until it is shown, which 'done' does not.
    LOGIN_KINDS = ("login", "token")
    MAX_SECRET = 512
    MAX_LOGIN_CODE = 512
    MAX_LOGIN_URL = 1024

    def request_login(self, node_id: str, email: str, now: float,
                      kind: str = "login") -> None:
        """Ask a node to start a sign-in, replacing any attempt already in flight.

        ``kind`` picks what the node runs: a sign-in for the node itself, or
        ``claude setup-token`` to mint a credential the owner carries to their
        own machine. The dance is identical — a URL out, a code back — so one
        row drives both rather than a second near-copy of this table.
        """
        if kind not in self.LOGIN_KINDS:
            raise StoreError(f"unknown sign-in kind: {kind}")
        if self.get_node(node_id) is None:
            raise StoreError(f"unknown node: {node_id}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO logins (node_id, requested_at, email, state, url, code, "
                "detail, updated_at, kind, secret) "
                "VALUES (?, ?, ?, 'requested', '', '', '', ?, ?, '') "
                "ON CONFLICT(node_id) DO UPDATE SET requested_at=excluded.requested_at, "
                "email=excluded.email, state='requested', url='', code='', detail='', "
                "updated_at=excluded.updated_at, kind=excluded.kind, secret=''",
                (node_id, now, email.strip()[:200], now, kind))
            self._conn.commit()

    def get_login(self, node_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM logins WHERE node_id = ?",
                                     (node_id,)).fetchone()
        return dict(row) if row else None

    def submit_login_code(self, node_id: str, code: str, now: float) -> None:
        """Hand the node the verification code the owner pasted."""
        code = code.strip()[:self.MAX_LOGIN_CODE]
        if not code:
            raise StoreError("the verification code is empty")
        with self._lock:
            changed = self._conn.execute(
                "UPDATE logins SET code = ?, state = 'code_sent', updated_at = ? "
                "WHERE node_id = ? AND state IN ('requested', 'url_ready')",
                (code, now, node_id)).rowcount
            self._conn.commit()
        if not changed:
            raise StoreError("no sign-in is waiting for a code on that node")

    def record_login_progress(self, node_id: str, state: str, url: str,
                              detail: str, now: float,
                              requested_at: Optional[float] = None,
                              secret: str = "") -> None:
        """What the node says is happening. Terminal states delete the row.

        A report is matched against the attempt it belongs to. Without that, a
        `done` or `failed` arriving late from an attempt the owner already
        cancelled would delete the one they have just started — the node posts on
        its own schedule, so overtaking is normal, not exotic.

        The whole decision is made under one lock, and every write is pinned to
        the exact row that decision was made about. Checking outside the lock
        and writing by node id alone left a window: a console request landing
        between the two replaces the row, and a late report then lands on an
        attempt it knows nothing about — planting a stale token under a request
        that has not finished, or deleting one that has only just begun.
        """
        if state not in self.LOGIN_ACTIVE_STATES + ("ready", "done", "failed"):
            return
        with self._lock:
            current = self.get_login(node_id)
            if current is None:
                return
            # Tolerance to match the node's report, which has been through JSON
            # and back; the exact stored value below is what pins the row.
            if (requested_at is not None
                    and abs(float(current["requested_at"]) - float(requested_at)) > 1e-6):
                log.debug("ignoring login progress for a superseded attempt on %s", node_id)
                return
            # Pin every write to the exact row this decision was made about.
            # While the lock above is held this cannot fail — the value is read
            # and written inside it — so no test can reach it, and none pretends
            # to. It is here so that moving or narrowing that lock later fails
            # loudly rather than silently reopening the window this closed.
            pin = (node_id, current["requested_at"])

            # Forward only. The case that bit: the agent reports url_ready,
            # someone pastes a code, and then that already-sent report lands and
            # puts the row back to url_ready — where the desired block no longer
            # carries a code, so the node never receives the one that was typed
            # and the card silently asks for it again. Terminal states are not
            # progress and always apply.
            here = self.LOGIN_ORDER.get(state)
            was = self.LOGIN_ORDER.get(str(current["state"]))
            if here is not None and was is not None and here < was:
                log.debug("ignoring %s for %s; already at %s", state, node_id,
                          current["state"])
                return

            if state == "ready":
                # A minted token. The row has to outlive the node's work, because
                # nobody has seen the token yet — but only until someone does,
                # which is what read_secret is for. The code is cleared in the
                # same statement; it has served its purpose either way.
                clean_secret = secret.strip()[:self.MAX_SECRET]
                if not clean_secret:
                    # A 'ready' with nothing in it is a node reporting success
                    # and losing the one thing that made it useful. Treat it as
                    # failure rather than showing an empty box.
                    self._conn.execute(
                        "UPDATE logins SET state='failed', code='', secret='', "
                        "detail=?, updated_at=? WHERE node_id = ? AND requested_at = ?",
                        ("the node finished but reported no token", now, *pin))
                else:
                    self._conn.execute(
                        "UPDATE logins SET state='ready', code='', secret=?, "
                        "detail='', updated_at=? WHERE node_id = ? AND requested_at = ?",
                        (clean_secret, now, *pin))
                self._conn.commit()
                return

            if state in ("done", "failed"):
                # Nothing useful survives a finished login, and the code must not
                # linger in the database once it has been used.
                self._conn.execute(
                    "DELETE FROM logins WHERE node_id = ? AND requested_at = ?", pin)
                self._conn.commit()
                return

            # A URL that is not a sign-in URL is dropped rather than stored. It
            # would reach an operator as a clickable link, and escaping does not
            # make a javascript: scheme safe.
            clean = url.strip()[:self.MAX_LOGIN_URL]
            if clean and not is_login_url(clean):
                log.warning("node %s offered a login url that is not one; dropping it",
                            node_id)
                clean = ""
            # Once the node says it has typed the code, the copy here has served
            # its only purpose. Clearing it in the same statement means there is
            # no window where a consumed code is still sitting in the database.
            clear_code = state == "code_sent"
            self._conn.execute(
                "UPDATE logins SET state = ?, url = ?, detail = ?, updated_at = ?"
                + (", code = ''" if clear_code else "") +
                " WHERE node_id = ? AND requested_at = ?",
                (state, clean, detail.strip()[:200], now, *pin))
            self._conn.commit()

    def read_secret(self, node_id: str, now: Optional[float] = None) -> str:
        """Return a minted token, for as long as the attempt it belongs to lasts.

        It was shown exactly once and deleted, which turned out to be stricter
        than anything required it to be. The credential already sits here from
        the moment the node reports it until the attempt expires; reading it
        twice inside that window adds no exposure the first read did not.
        Refusing the second only meant a person who needed it on a second
        machine had to mint a whole new one.

        So: readable while the attempt is alive, gone when it expires on the
        ordinary sweep, and gone at once if somebody says they are finished
        with it. The window is what bounds this, not the reading.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT secret, requested_at FROM logins "
                "WHERE node_id = ? AND state = 'ready'", (node_id,)).fetchone()
            if row is None:
                return ""
            # Remember that a token reached somebody: once per attempt, not once
            # per read. Reading the same token again does not make it newly
            # issued — but a later attempt that produces a new one does, and a
            # guard of "only if this has never been set" would have frozen the
            # console's answer at whenever the first one was.
            self._conn.execute(
                "UPDATE nodes SET device_token_at = ? "
                "WHERE id = ? AND device_token_at < ?",
                (now if now is not None else time.time(), node_id,
                 row["requested_at"]))
            self._conn.commit()
        return str(row["secret"])

    def clear_login(self, node_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM logins WHERE node_id = ?", (node_id,))
            self._conn.commit()

    def expire_logins(self, older_than: float) -> int:
        """Drop attempts nobody finished, so a stale code cannot be replayed."""
        with self._lock:
            removed = self._conn.execute("DELETE FROM logins WHERE updated_at < ?",
                                         (older_than,)).rowcount
            self._conn.commit()
        return removed

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
        body = json.dumps(_without_secret(payload), separators=(",", ":"), sort_keys=True)
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

    # -- accounts ----------------------------------------------------------

    def add_account(self, account_id: str, google_sub: str, email: str, *,
                    role: str = "user", slot_quota: int = 0,
                    now: float) -> dict[str, Any]:
        if role not in ("user", "admin"):
            raise StoreError("role must be 'user' or 'admin'")
        if int(slot_quota) < 0:
            raise StoreError("slot quota cannot be negative")
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO accounts (id, google_sub, email, role, "
                    "slot_quota, created_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
                    (account_id, google_sub, email, role, int(slot_quota), now, now))
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"account already exists: {exc}") from exc
            self._conn.commit()
        return self.get_account(account_id)  # type: ignore[return-value]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    def account_by_google_sub(self, google_sub: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE google_sub = ?", (google_sub,)).fetchone()
        return dict(row) if row else None

    def account_by_email(self, email: str) -> dict[str, Any] | None:
        """For the operator, who knows people by address rather than by id.

        Google's subject is what the account is keyed on, because addresses
        change; this is a convenience for the command line, not an identity.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE email = ? ORDER BY created_at LIMIT 1",
                (email,)).fetchone()
        return dict(row) if row else None

    def set_machine_capacity(self, node_id: str, capacity: int) -> bool:
        """How many slots this machine may hold. Declared, never guessed.

        Refuses to drop below the slots already declared on it rather than
        leaving a machine whose own records say it is overfull.
        """
        if int(capacity) < 0:
            raise StoreError("capacity cannot be negative")
        with self._lock:
            have = self._conn.execute(
                "SELECT COUNT(*) AS n FROM slots WHERE node_id = ?",
                (node_id,)).fetchone()["n"]
            if int(capacity) < have:
                raise StoreError(
                    f"{node_id} already has {have} slots declared; remove some "
                    f"before lowering its capacity to {capacity}")
            cur = self._conn.execute(
                "UPDATE nodes SET capacity = ? WHERE id = ?", (int(capacity), node_id))
            self._conn.commit()
        return cur.rowcount > 0

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM accounts ORDER BY email").fetchall()
        return [dict(r) for r in rows]

    def set_slot_quota(self, account_id: str, quota: int) -> bool:
        """Grant or reduce an allowance.

        Reducing it below what somebody already holds is allowed and does
        nothing to those slots: taking one back is a release, a deliberate act
        with a wipe attached, never a side effect of a number changing. All
        this stops is claiming more.
        """
        if int(quota) < 0:
            raise StoreError("slot quota cannot be negative")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE accounts SET slot_quota = ? WHERE id = ?",
                (int(quota), account_id))
            self._conn.commit()
        return cur.rowcount > 0

    def held_slot_count(self, account_id: str) -> int:
        """Slots this account is holding, counting one being wiped.

        A releasing slot still counts. The wipe has not finished, so handing it
        to somebody else now would hand over the files with it.
        """
        marks = ",".join("?" * len(slotstates.HELD))
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM slots WHERE held_by = ? "  # noqa: S608
                f"AND state IN ({marks})",
                (account_id, *sorted(slotstates.HELD))).fetchone()
        return int(row["n"])

    # -- slots ------------------------------------------------------------
    #
    # Releasing is written before claiming, and that order is deliberate. The
    # failure with no recovery is handing somebody a slot that still holds
    # another person's work; it is far easier to get right when the wipe is the
    # thing being designed rather than an afterthought bolted on once claiming
    # already works.

    def add_slot(self, slot_id: str, node_id: str, unix_user: str, *,
                 now: float) -> dict[str, Any]:
        """Declare a slot on a machine. It starts free, holding nobody."""
        if not SLOT_ID_RE.match(slot_id or ""):
            raise StoreError("slot id must be 2-64 chars of lowercase letters, "
                             "digits and hyphens, starting with a letter or digit")
        if not UNIX_USER_RE.match(unix_user or ""):
            raise StoreError("unix user must be a valid Linux login name")
        validate_node_id(node_id)
        with self._lock:
            node = self._conn.execute(
                "SELECT capacity FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if node is None:
                raise StoreError(f"no machine {node_id!r}")
            # Capacity is what the operator declared they sold. Refuse to
            # declare more slots than that rather than discovering it as a
            # machine that will not hold them.
            have = self._conn.execute(
                "SELECT COUNT(*) AS n FROM slots WHERE node_id = ?",
                (node_id,)).fetchone()["n"]
            if have >= int(node["capacity"]):
                raise StoreError(
                    f"{node_id} declares capacity {node['capacity']} and already "
                    f"has {have} slots; raise its capacity first")
            try:
                self._conn.execute(
                    "INSERT INTO slots (id, node_id, unix_user, state, held_by, "
                    "claimed_at, released_at, device_token_at) "
                    "VALUES (?,?,?,?,NULL,NULL,NULL,0)",
                    (slot_id, node_id, unix_user, slotstates.FREE))
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"slot already exists: {exc}") from exc
            self._conn.commit()
        return self.get_slot(slot_id)  # type: ignore[return-value]

    def get_slot(self, slot_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        return dict(row) if row else None

    def list_slots(self, *, node_id: str | None = None,
                   held_by: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if node_id is not None:
            where.append("node_id = ?")
            args.append(node_id)
        if held_by is not None:
            where.append("held_by = ?")
            args.append(held_by)
        if state is not None:
            where.append("state = ?")
            args.append(state)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM slots" + clause + " ORDER BY node_id, unix_user",  # noqa: S608
                args).fetchall()
        return [dict(r) for r in rows]

    def move_slot(self, slot_id: str, to: str) -> bool:
        """Move a slot to `to`, or raise if the lifecycle forbids it.

        The write is pinned to the state that was read, so a move decided
        against a stale read writes nothing rather than overwriting whatever
        happened in between.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None:
                raise StoreError(f"no slot {slot_id!r}")
            frm = row["state"]
            slotstates.check_move(frm, to)
            cur = self._conn.execute(
                "UPDATE slots SET state = ? WHERE id = ? AND state = ?",
                (to, slot_id, frm))
            self._conn.commit()
        return cur.rowcount > 0

    def begin_release(self, slot_id: str) -> bool:
        """Start the wipe. Legal from every state a person can hold."""
        return self.move_slot(slot_id, slotstates.RELEASING)

    def finish_release(self, slot_id: str, *, now: float) -> bool:
        """The wipe finished: the slot is empty and may be given to somebody else.

        This is the only way a slot reaches `free`, and it clears everything
        about the person who held it in the same statement — the holder, when
        they took it, and when they were last handed a device token. A slot
        that came back free still carrying the previous holder's id would read
        as theirs on every page that joins on it.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None:
                raise StoreError(f"no slot {slot_id!r}")
            slotstates.check_move(row["state"], slotstates.FREE)
            cur = self._conn.execute(
                "UPDATE slots SET state = ?, held_by = NULL, claimed_at = NULL, "
                "released_at = ?, device_token_at = 0 WHERE id = ? AND state = ?",
                (slotstates.FREE, now, slot_id, slotstates.RELEASING))
            self._conn.commit()
        return cur.rowcount > 0

    def claim_slot(self, account_id: str, *, now: float,
                   node_id: str | None = None) -> dict[str, Any]:
        """Take a free slot for this account, allowance permitting.

        The allowance is checked against what they already hold in the same
        lock and the same transaction that takes the slot, and the take itself
        is pinned to `state = 'free'`. There is no window where two browser
        tabs each see a free slot and both get one.

        Raises QuotaExceeded when they have no allowance left, and
        NoSlotAvailable when nothing is free — two different answers that the
        page shows differently, so they are two different exceptions rather
        than one absent return value.
        """
        marks = ",".join("?" * len(slotstates.HELD))
        held_states = sorted(slotstates.HELD)
        with self._lock:
            account = self._conn.execute(
                "SELECT slot_quota FROM accounts WHERE id = ?",
                (account_id,)).fetchone()
            if account is None:
                raise StoreError(f"no account {account_id!r}")
            quota = int(account["slot_quota"])
            held = int(self._conn.execute(
                f"SELECT COUNT(*) AS n FROM slots WHERE held_by = ? "  # noqa: S608
                f"AND state IN ({marks})",
                (account_id, *held_states)).fetchone()["n"])
            if held >= quota:
                raise QuotaExceeded(
                    f"account holds {held} of {quota} slots"
                    if quota else "account has no slot allowance")
            args: list[Any] = []
            extra = ""
            if node_id is not None:
                extra = " AND s.node_id = ?"
                args.append(node_id)
            candidate = self._conn.execute(
                "SELECT s.id FROM slots s JOIN nodes n ON n.id = s.node_id "  # noqa: S608
                "WHERE s.state = ? AND n.enabled = 1" + extra +
                " ORDER BY s.node_id, s.unix_user LIMIT 1",
                (slotstates.FREE, *args)).fetchone()
            if candidate is None:
                raise NoSlotAvailable(
                    f"no free slot on {node_id}" if node_id
                    else "no free slot on any enabled machine")
            cur = self._conn.execute(
                "UPDATE slots SET state = ?, held_by = ?, claimed_at = ?, "
                "released_at = NULL WHERE id = ? AND state = ?",
                (slotstates.CLAIMING, account_id, now,
                 candidate["id"], slotstates.FREE))
            if cur.rowcount == 0:
                # Somebody took it between the select and the update. Nothing
                # was written; the caller can ask again.
                self._conn.rollback()
                raise NoSlotAvailable("the free slot was taken; try again")
            self._conn.commit()
        return self.get_slot(candidate["id"])  # type: ignore[return-value]
