"""SQLite-backed storage for nodes, heartbeats and alerts.

Node tokens are never stored in clear text: only a SHA-256 hash is kept, and the
clear token is shown exactly once when the node is created or rotated.
"""

from __future__ import annotations

import contextlib
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import claude_versions, names, payments, pricing
from . import sessions as sessionlib
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
    secret TEXT NOT NULL DEFAULT '',
    -- Unused, always ''. Left from a short-lived feature that let a slot hold
    -- several Claude accounts; it broke the one rule this project keeps (one
    -- account, one node) and came out. The column stays so no live database
    -- needs a migration to shed it.
    account TEXT NOT NULL DEFAULT ''
);
-- Unused, and empty, for the same reason as logins.account: it carried
-- switches between a slot's several accounts, which no longer exist. Kept, not
-- dropped, so the schema does not churn; renaming a slot still renames it.
CREATE TABLE IF NOT EXISTS account_intents (
    slot_id TEXT PRIMARY KEY REFERENCES slots(id),
    action TEXT NOT NULL,                     -- 'use' | 'forget'
    account TEXT NOT NULL,                    -- '1' | '2' | '3'
    requested_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'requested',  -- 'requested' | 'failed'
    detail TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
-- A Claude Code update a slot's holder asked for from their page, one per slot:
-- 'pending' until the machine says it installed it or could not, then 'done'
-- or 'failed' so the page can say so, until the sweep clears it. The machine
-- installs; this only carries the ask (see ccfleetd/claude_versions.py).
CREATE TABLE IF NOT EXISTS claude_updates (
    slot_id TEXT PRIMARY KEY REFERENCES slots(id),
    requested_at REAL NOT NULL,
    state TEXT NOT NULL,                     -- 'pending' | 'done' | 'failed'
    to_version TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
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
    last_seen_at REAL NOT NULL DEFAULT 0,
    -- "<handle>-<n>" for their slots, only if the operator set it at their
    -- request; otherwise claims get neutral names ("slot-4821"), never anything
    -- from their address. See ccfleetd/names.py.
    handle TEXT
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
    -- What the machine last said about this slot's Linux user: 1 it exists,
    -- 0 it does not, NULL never reported. A slot is only handed out on a 0,
    -- because free is a claim about the machine and only the machine can
    -- confirm it.
    present INTEGER,
    reported_at REAL,
    -- Its name while somebody holds it ("slot-4821", or what its holder
    -- renamed it to), NULL while free: the name people see, and its machine's
    -- hostname. The id never changes.
    name TEXT,
    -- 'machine': a Linux user the machine agent makes and wipes. 'owner': a
    -- person's own node counted as their slot, a record and nothing more.
    kind TEXT NOT NULL DEFAULT 'machine',
    UNIQUE (node_id, unix_user)
);
CREATE INDEX IF NOT EXISTS ix_slots_state ON slots(state);
CREATE INDEX IF NOT EXISTS ix_slots_held_by ON slots(held_by);
-- What somebody paid, as the operator wrote it down. A record, never an
-- enforcer: nothing about slots or claiming reads this table (see
-- ccfleetd/payments.py). A mistake is voided, not deleted, so the record
-- keeps what was written and when.
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_minor INTEGER NOT NULL,        -- hundredths: 3050 is 30.50
    currency TEXT NOT NULL,               -- a three-letter code, e.g. USD
    paid_through TEXT NOT NULL,           -- YYYY-MM-DD, the last day covered
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    voided_at REAL
);
CREATE INDEX IF NOT EXISTS ix_payments_account ON payments(account_id);
-- Values the operator sets from the console, one row per key. Today that is
-- only "price", the price the public pages show (see ccfleetd/pricing.py): a
-- line people read, never something charged or enforced.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL
);
-- Browser sessions, which the console has never had.
--
-- The primary key is a SHA-256 of the session id, not the id. The cookie
-- carries the id; anyone who reads this table gets hashes they cannot present
-- to us. The node token is stored the same way for the same reason.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    -- Which site the session was made on: 'product' or 'admin'. Honoured
    -- only there — two sites, two sessions — so a product session cookie
    -- carried to the operator's hostname opens nothing.
    site TEXT NOT NULL DEFAULT 'product'
);
CREATE INDEX IF NOT EXISTS ix_sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions(expires_at);
-- A sign-in that has left for Google and not come back. Holds the state that
-- ties the callback to the browser that started it, and the PKCE verifier.
-- Rows are single use and short lived: taking one deletes it, so a callback
-- replayed from a browser history or a proxy log finds nothing.
CREATE TABLE IF NOT EXISTS oauth_flows (
    state TEXT PRIMARY KEY,
    verifier TEXT NOT NULL,
    next_url TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    owner TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
-- The minutes behind the status page (see ccfleetd/status.py): for this site
-- and each machine that counts, how many minutes of each UTC day it was up,
-- degraded or down. Counted once a minute by the serving loop, 90 days kept.
-- Machine-level facts only: no account, holder or name is ever in it.
CREATE TABLE IF NOT EXISTS status_minutes (
    component TEXT NOT NULL,
    day TEXT NOT NULL,
    green INTEGER NOT NULL DEFAULT 0,
    yellow INTEGER NOT NULL DEFAULT 0,
    red INTEGER NOT NULL DEFAULT 0,
    last_minute INTEGER NOT NULL,
    PRIMARY KEY (component, day)
);
CREATE INDEX IF NOT EXISTS ix_status_minutes_day ON status_minutes(day);
"""

#: The states a status minute is counted as, which are its table's columns.
STATUS_COLUMNS = ("green", "yellow", "red")


def _status_day(minute: int) -> str:
    """The UTC date a minute since the epoch falls on (status.utc_day)."""
    return datetime.fromtimestamp(minute * 60, timezone.utc).strftime("%Y-%m-%d")


ACCOUNT_ID_BYTES = 12
SLOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
# Exactly what node/slot-add.sh accepts for --slot. It has to be exactly that:
# the fleet records the name here and the operator provisions it there, so a
# name this accepts and the script refuses is a slot that exists in our records
# and can never exist on the machine.
UNIX_USER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
# A sign-in on a slot shares the `logins` table with a node's own, keyed by
# this prefix and the slot id. A node id cannot contain a colon, so the two can
# never name the same row.
SLOT_LOGIN_PREFIX = "slot:"


def slot_login_key(slot_id: str) -> str:
    return SLOT_LOGIN_PREFIX + slot_id


class StoreError(ValueError):
    """Raised for invalid identifiers or missing rows."""


class QuotaExceeded(StoreError):
    """The account holds as many slots as its allowance permits."""


class NoSlotAvailable(StoreError):
    """Nothing free to hand out right now."""


class NotYours(StoreError):
    """The slot is not held by the account acting on it."""


class BadName(StoreError):
    """Not a name a slot may have (see names.valid_nickname)."""


class NameTaken(StoreError):
    """Another slot or machine already answers to that name."""



def _without_secret(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload as it should be kept, which is without the minted token.

    A heartbeat is archived whole for the retention window. A device token
    riding up inside one would therefore outlive the single showing it is
    promised by thirty days, in a second copy nothing points at and
    ``read_secret`` cannot reach. Redacting here rather than at the call site
    makes it a property of storing a heartbeat, not something each caller has
    to remember.
    """
    kept = dict(payload)
    login = (payload.get("reconcile") or {}).get("login")
    if isinstance(login, Mapping) and "secret" in login:
        reconcile = dict(payload["reconcile"])
        reconcile["login"] = {k: v for k, v in login.items() if k != "secret"}
        kept["reconcile"] = reconcile
    # A shared machine carries each slot's sign-in the same way, one level down.
    slots = payload.get("slots")
    if isinstance(slots, list):
        kept["slots"] = [_slot_without_secret(s) for s in slots]
    return kept


def _slot_without_secret(slot: Any) -> Any:
    login = slot.get("login") if isinstance(slot, Mapping) else None
    if not isinstance(login, Mapping) or "secret" not in login:
        return slot
    return {**slot, "login": {k: v for k, v in login.items() if k != "secret"}}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
        raise StoreError(
            "node id must be 2-40 chars of lowercase letters, digits and hyphens, "
            "starting with a letter or digit"
        )
    return node_id


def validate_slot_id(slot_id: str) -> str:
    if not isinstance(slot_id, str) or not SLOT_ID_RE.match(slot_id):
        raise StoreError("slot id must be 2-64 chars of lowercase letters, "
                         "digits and hyphens, starting with a letter or digit")
    return slot_id


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
        # How many slots the operator declared this machine may hold; one for
        # an ordinary owner node.
        "capacity": row["capacity"],
        # The account this machine's free slots are kept for; None for anybody.
        "reserved_for": row["reserved_for"],
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

    def __init__(self, path: str,
                 max_slots_per_machine: int = slotstates.MAX_SLOTS_PER_MACHINE) -> None:
        # One machine is one slot, held here as well as at the operator's
        # commands, so no way in can declare a second. Raised only by tests
        # of the lifecycle, which is per slot and exercised several at once.
        self._max_slots = int(max_slots_per_machine)
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
            # The account a shared machine is kept for, or NULL for anybody —
            # which every machine written before this column is.
            self._add_missing_columns("nodes", {"reserved_for": "TEXT"})
            self._add_missing_columns("logins", {
                "kind": "TEXT NOT NULL DEFAULT 'login'",
                "secret": "TEXT NOT NULL DEFAULT ''",
            })
            # Unused (see the table): added once, and kept so a database that
            # has it and one that does not look the same from here on.
            self._add_missing_columns("logins", {
                "account": "TEXT NOT NULL DEFAULT ''",
            })
            # NULL on every existing row, which is the point: nothing has
            # confirmed those slots empty, so none is handed out until the
            # machine says so.
            self._add_missing_columns("slots", {
                "present": "INTEGER",
                "reported_at": "REAL",
            })
            # Every session made before two sites existed was made on the only
            # one there was, which is the product's.
            self._add_missing_columns("sessions", {
                "site": "TEXT NOT NULL DEFAULT 'product'",
            })
            # Names come with claims, so every slot written before them has
            # none and shows its id; and every slot before owner slots was a
            # machine's. No handle means neutral names.
            self._add_missing_columns("slots", {
                "name": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'machine'",
            })
            self._add_missing_columns("accounts", {"handle": "TEXT"})
            # The release channel a slot's holder chose on their page: NULL
            # follows the machine's pin, which every slot before this did.
            self._add_missing_columns("slots", {"claude_channel": "TEXT"})
            # When its holder last moved the slot to another Claude account
            # (see request_slot_login): NULL for never, which every slot before
            # this is. The slot's own, so it goes when the slot is freed.
            self._add_missing_columns("slots", {"account_switched_at": "REAL"})
            # A name becomes a hostname: two slots answering to one would be
            # two people's machines under one name in claude.ai. Created here,
            # after the column exists, so an older database gets it too.
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_slots_name "
                               "ON slots(name) WHERE name IS NOT NULL")
            self._conn.commit()

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        """Idempotent ALTER TABLE ADD COLUMN. Called with literals only."""
        have = {row["name"] for row in
                self._conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextlib.contextmanager
    def _write_txn(self):
        """Hold SQLite's write lock across a read-then-write decision.

        The instance lock only serialises threads sharing one Store. Two Stores
        on the same file — `ccfleetd serve` and a `ccfleetd slot` command, say —
        are two connections, and Python's sqlite3 leaves a bare SELECT outside
        any transaction, so both could read "you hold none, here is a free one"
        before either wrote. BEGIN IMMEDIATE takes the write lock up front, so
        the second one waits for the first to finish rather than deciding
        against a read that is already stale.
        """
        with self._lock:
            if self._conn.in_transaction:
                self._conn.commit()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
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
        with self._write_txn() as conn:
            # A machine answers to its id while its slot is free: no slot may
            # already be called that, by id or by its holder's name.
            if conn.execute("SELECT 1 FROM slots WHERE id = ? OR name = ?",
                            (node_id, node_id)).fetchone() is not None:
                raise StoreError(f"a slot already answers to {node_id!r}")
            try:
                conn.execute(
                    "INSERT INTO nodes (id, owner, region, token_hash, pinned_version, "
                    "rc_expected, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    (node_id, owner.strip(), region.strip(), hash_token(token),
                     pinned_version.strip(), int(rc_expected), now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"node {node_id!r} already exists") from exc
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
    # A slot's holder has one more: "switch", moving the slot to another Claude
    # account of theirs (see request_slot_login). A node's own sign-in keeps no
    # account to move from, so a node is never asked for one.
    SLOT_LOGIN_KINDS = LOGIN_KINDS + ("switch",)
    MAX_SECRET = 512
    MAX_LOGIN_CODE = 512
    MAX_LOGIN_URL = 1024
    MAX_LOGIN_DETAIL = 200

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
            self._begin_login(self._conn, node_id, email, now, kind)
            self._conn.commit()

    @staticmethod
    def _begin_login(conn: sqlite3.Connection, key: str, email: str, now: float,
                     kind: str) -> None:
        # account='' on the update too: a row left from when a sign-in could
        # name one of several accounts must not carry that word forward.
        conn.execute(
            "INSERT INTO logins (node_id, requested_at, email, state, url, code, "
            "detail, updated_at, kind, secret) "
            "VALUES (?, ?, ?, 'requested', '', '', '', ?, ?, '') "
            "ON CONFLICT(node_id) DO UPDATE SET requested_at=excluded.requested_at, "
            "email=excluded.email, state='requested', url='', code='', detail='', "
            "updated_at=excluded.updated_at, kind=excluded.kind, secret='', account=''",
            (key, now, email.strip()[:200], now, kind))

    # Only a slot that is set up and held can be signed into: claimed or
    # active. Claiming has no user to sign in as yet, and a slot being given
    # back must not start a login its next holder could inherit.
    SLOT_SIGN_IN_STATES = (slotstates.CLAIMED, slotstates.ACTIVE)

    @staticmethod
    def _held(conn: sqlite3.Connection, slot_id: str,
              held_by: Optional[str]) -> sqlite3.Row:
        """The slot, checked against who is acting on it — inside the caller's
        transaction, so the answer cannot change before the action lands.

        A check made in one request and an action in the next statement leaves
        a gap: released, wiped, freed and claimed by somebody else in between,
        and the stale request acts on the new holder's slot. `held_by` None is
        the operator's side, which acts on any slot.
        """
        row = conn.execute("SELECT state, kind, node_id, held_by, account_switched_at, name "
                           "FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if row is None:
            raise StoreError(f"no slot {slot_id!r}")
        if held_by is not None and row["held_by"] != held_by:
            raise NotYours(f"{slot_id} is not held by this account")
        return row

    @staticmethod
    def _sign_in_key(row: sqlite3.Row, slot_id: str) -> str:
        """Where a slot's sign-in lives. A machine slot's is its own, under
        "slot:<id>". An owner slot's is its node's own row: the owner's own
        agent is what signs in, and it reads its node's row, exactly as it
        does for a sign-in started from the console."""
        if row["kind"] == slotstates.OWNER_SLOT:
            return str(row["node_id"])
        return slot_login_key(slot_id)

    def login_for_slot(self, slot: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """The sign-in in flight on a slot, from wherever that slot keeps it."""
        if slot.get("kind") == slotstates.OWNER_SLOT:
            return self.get_login(str(slot["node_id"]))
        return self.get_login(slot_login_key(str(slot["id"])))

    def request_slot_login(self, slot_id: str, email: str, now: float,
                           kind: str = "login", *, held_by: Optional[str] = None) -> None:
        """Start a sign-in, a device token or a change of account on a slot —
        for its holder.

        The same dance as a node's own, run by the machine as the slot's user.
        The check and the write are one transaction, so a release landing in
        between cannot leave a sign-in hanging off a slot being wiped.

        A change of account ("switch") asks more, in the same transaction: a
        machine's slot in use, and a week since its last change. An owner's
        own node keeps no account to change: a sign-in there is its owner's.
        """
        if kind not in self.SLOT_LOGIN_KINDS:
            raise StoreError(f"unknown sign-in kind: {kind}")
        with self._write_txn() as conn:
            row = self._held(conn, slot_id, held_by)
            if row["state"] not in self.SLOT_SIGN_IN_STATES:
                raise StoreError(f"{slot_id} is {row['state']}; it can be signed "
                                 f"into once it is set up")
            if kind == "switch" and (row["kind"] != slotstates.MACHINE_SLOT
                                     or row["state"] != slotstates.ACTIVE):
                raise StoreError(f"{slot_id} can change its Claude account once it is in use")
            if kind == "switch" and slotstates.switch_wait_until(row["account_switched_at"],
                                                                 now) is not None:
                raise StoreError(f"{slot_id} changed its Claude account less than a week ago")
            self._begin_login(conn, self._sign_in_key(row, slot_id), email, now, kind)

    def get_login(self, node_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM logins WHERE node_id = ?",
                                     (node_id,)).fetchone()
        return dict(row) if row else None

    def submit_login_code(self, node_id: str, code: str, now: float) -> None:
        """Hand the node the verification code the owner pasted."""
        with self._write_txn() as conn:
            self._submit_code(conn, node_id, code, now)

    def submit_slot_login_code(self, slot_id: str, code: str, now: float, *,
                               held_by: Optional[str] = None) -> None:
        """The code a slot's holder pasted, for their slot and nobody else's."""
        with self._write_txn() as conn:
            row = self._held(conn, slot_id, held_by)
            self._submit_code(conn, self._sign_in_key(row, slot_id), code, now)

    def _submit_code(self, conn: sqlite3.Connection, key: str, code: str,
                     now: float) -> None:
        code = code.strip()[:self.MAX_LOGIN_CODE]
        if not code:
            raise StoreError("the verification code is empty")
        changed = conn.execute(
            "UPDATE logins SET code = ?, state = 'code_sent', updated_at = ? "
            "WHERE node_id = ? AND state IN ('requested', 'url_ready')",
            (code, now, key)).rowcount
        if not changed:
            raise StoreError("no sign-in is waiting for a code there")

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

            said = str(detail or "").strip()
            if (state == "done" and current["kind"] == "switch"
                    and said in slotstates.SWITCH_ENDINGS):
                # A change of account, over. Its holder is told how, on their
                # page, as a failed one is: the machine's fixed word stays and
                # nothing else, until the sweep takes it; a done row is never
                # asked of the machine again. The week before the next change
                # starts only when the account really moved. Only a machine's
                # slot is ever asked for one, so the key is always a slot's.
                # The machine says it again until it hears back (see the
                # agent's _to_say); heard once, the week is not moved on.
                if current["state"] == "done":
                    return
                self._conn.execute(
                    "UPDATE logins SET state='done', code='', url='', secret='', "
                    "detail=?, updated_at=? WHERE node_id = ? AND requested_at = ?",
                    (said, now, *pin))
                if said == slotstates.SWITCHED:
                    self._conn.execute("UPDATE slots SET account_switched_at = ? WHERE id = ?",
                                       (now, node_id[len(SLOT_LOGIN_PREFIX):]))
                self._conn.commit()
                return
            if state == "failed" and node_id.startswith(SLOT_LOGIN_PREFIX):
                # A slot's holder is told why, on their own page: above all
                # that a slot keeps its account, and moves only by a change.
                # Only the reason stays — code, link and secret go now — and
                # only until the sweep takes it; the machine is never asked
                # about a failed row again (see desired._login_block).
                self._conn.execute(
                    "UPDATE logins SET state='failed', code='', url='', secret='', "
                    "detail=?, updated_at=? WHERE node_id = ? AND requested_at = ?",
                    (str(detail or "").strip()[:self.MAX_LOGIN_DETAIL], now, *pin))
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
        with self._write_txn() as conn:
            return self._read_secret(conn, node_id, "nodes", node_id, now)

    def read_slot_secret(self, slot_id: str, now: Optional[float] = None, *,
                         held_by: Optional[str] = None) -> str:
        """A token minted on a slot, for as long as its attempt lasts — and
        only for whoever holds the slot as it is read."""
        with self._write_txn() as conn:
            row = self._held(conn, slot_id, held_by)
            if row["kind"] == slotstates.OWNER_SLOT:
                # Noted on the node: a token handed over for the owner's own
                # node, exactly as if they had asked from the console.
                return self._read_secret(conn, row["node_id"], "nodes", row["node_id"], now)
            return self._read_secret(conn, slot_login_key(slot_id), "slots", slot_id, now)

    @staticmethod
    def _read_secret(conn: sqlite3.Connection, key: str, table: str, row_id: str,
                     now: Optional[float]) -> str:
        if table not in ("nodes", "slots"):
            raise StoreError(f"no device-token record on {table!r}")
        row = conn.execute(
            "SELECT secret, requested_at FROM logins "
            "WHERE node_id = ? AND state = 'ready'", (key,)).fetchone()
        if row is None:
            return ""
        # Remember that a token reached somebody: once per attempt, not once
        # per read. Reading the same token again does not make it newly
        # issued — but a later attempt that produces a new one does, and a
        # guard of "only if this has never been set" would have frozen the
        # console's answer at whenever the first one was.
        conn.execute(
            f"UPDATE {table} SET device_token_at = ? "  # noqa: S608 - two literals
            "WHERE id = ? AND device_token_at < ?",
            (now if now is not None else time.time(), row_id, row["requested_at"]))
        return str(row["secret"])

    def clear_login(self, node_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM logins WHERE node_id = ?", (node_id,))
            self._conn.commit()

    def clear_slot_login(self, slot_id: str, *, held_by: Optional[str] = None) -> None:
        """End whatever flow is in flight on a slot, for its holder."""
        with self._write_txn() as conn:
            row = self._held(conn, slot_id, held_by)
            conn.execute("DELETE FROM logins WHERE node_id = ?",
                         (self._sign_in_key(row, slot_id),))

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
        """Forget a machine. Refuses while it still has slots declared on it.

        SQLite does not enforce the foreign key, so deleting the machine would
        leave its slot rows behind — and the id is chosen by the operator, so
        registering a machine under the same name again silently reattaches
        them, holders and lifecycle states and all. Somebody's released slot
        comes back held; somebody's held slot comes back on hardware that is
        not theirs.

        Take the slots off it first, which forces each one through a release
        and so through a wipe.
        """
        with self._write_txn() as conn:
            slot_rows = conn.execute(
                "SELECT id, state, kind FROM slots WHERE node_id = ? ORDER BY unix_user",
                (node_id,)).fetchall()
            if any(r["kind"] == slotstates.OWNER_SLOT for r in slot_rows):
                raise StoreError(
                    f"{node_id} is counted as somebody's slot. Let go of it first: "
                    f"'ccfleetd node hold {node_id} --none'.")
            if slot_rows:
                held = [r["id"] for r in slot_rows
                        if r["state"] in slotstates.HELD]
                detail = (f"{len(held)} of them still held ({', '.join(held)})"
                          if held else "all free")
                raise StoreError(
                    f"{node_id} still has {len(slot_rows)} slots declared on it, "
                    f"{detail}. Remove them first: 'ccfleetd slot remove <id>'.")
            cur = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            conn.execute("DELETE FROM heartbeats WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM alerts WHERE node_id = ?", (node_id,))
            if cur.rowcount == 0:
                raise StoreError(f"unknown node {node_id!r}")

    # Every column that holds a node's id. A rename moves these and nothing
    # else, so the schema test that guards this list fails on any column added
    # later that names a node until it is listed here. `logins.node_id` is the
    # node's own sign-in row; a slot's shares the column as "slot:<id>", which
    # no node id can equal (a node id has no colon), so moving one never
    # touches the other.
    NODE_ID_COLUMNS = (("nodes", "id"), ("slots", "node_id"), ("heartbeats", "node_id"),
                       ("alerts", "node_id"), ("logins", "node_id"))

    def rename_node(self, old: str, new: str) -> None:
        """Give a node — an owner's node or a shared machine — a new id.

        Everything that names it moves in one transaction: its slots, its
        history, its alerts and its own sign-in row. The token hash stays with
        the row, so the box keeps its token and only its CCFLEET_NODE_ID has to
        change. Until the box says the new id its heartbeats are refused — the
        body must name the node the token belongs to — and a refused heartbeat
        changes nothing on a machine, so the gap costs a report or two.

        The new id must be free. What a removed node left behind under it — a
        sign-in row, which can hold a minted token, or history and alerts from
        a hand-edited database — belongs to no node, and is dropped rather than
        adopted. Slots are the exception: they are somebody's, so one still
        naming the new id stops the rename instead of moving onto a machine
        that is not the one it was sold on.
        """
        validate_node_id(new)
        with self._write_txn() as conn:
            if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (old,)).fetchone() is None:
                raise StoreError(f"unknown node {old!r}")
            if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (new,)).fetchone() is not None:
                raise StoreError(f"a node called {new!r} already exists")
            # A machine answers to its id when its slot is free: no slot on
            # another machine may already be called that, by id or by name.
            if conn.execute("SELECT 1 FROM slots WHERE (id = ? OR name = ?) AND node_id != ?",
                            (new, new, old)).fetchone() is not None:
                raise StoreError(f"a slot on another machine already answers to {new!r}")
            stray = [r["id"] for r in conn.execute(
                "SELECT id FROM slots WHERE node_id = ? ORDER BY id", (new,))]
            if stray:
                raise StoreError(
                    f"slots still name {new!r} though no node by that name exists "
                    f"({', '.join(stray)}); they are somebody's, so settle them first")
            # An owner's node counted as their slot is called by the node's id,
            # so the slot is renamed with it. A slot elsewhere already called
            # the new id was refused just above, so nothing is half-done.
            carries = conn.execute(
                "SELECT 1 FROM slots WHERE id = ? AND node_id = ? AND kind = ?",
                (old, old, slotstates.OWNER_SLOT)).fetchone() is not None
            for table in ("logins", "heartbeats", "alerts"):
                conn.execute(f"DELETE FROM {table} WHERE node_id = ?", (new,))  # noqa: S608
            for table, column in self.NODE_ID_COLUMNS:
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",  # noqa: S608
                             (new, old))
            if carries:
                conn.execute("UPDATE slots SET id = ? WHERE id = ? AND kind = ?",
                             (new, old, slotstates.OWNER_SLOT))
                conn.execute("DELETE FROM claude_updates WHERE slot_id = ?", (new,))
                conn.execute("UPDATE claude_updates SET slot_id = ? WHERE slot_id = ?",
                             (new, old))

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

    # -- status minutes (see ccfleetd/status.py) ---------------------------------

    def count_status(self, component: str, state: str, now: float, *, grace: int,
                     gap_state: Optional[str] = None) -> None:
        """Count the minute `now` falls in for `component`, in `state`, once.

        Minutes gone uncounted since its last count, up to `grace` of them, are
        a check running a little late and take `state` too. A longer gap is
        counted as `gap_state` when there is one (the site: its silence was
        its downtime), and left out when not (a machine nobody was watching).
        A minute already counted, or a clock gone back, counts nothing.
        """
        if state not in STATUS_COLUMNS or gap_state not in (None, *STATUS_COLUMNS):
            raise ValueError(f"not a status: {state!r} / {gap_state!r}")
        minute = int(now // 60)
        with self._write_txn() as conn:
            last = conn.execute("SELECT MAX(last_minute) FROM status_minutes "
                                "WHERE component = ?", (component,)).fetchone()[0]
            if last is not None and last >= minute:
                return
            missed = minute - last - 1 if last is not None else 0
            fill = state if missed <= grace else gap_state
            if missed and fill is not None:
                self._add_status(conn, component, fill, last + 1, minute - 1)
            self._add_status(conn, component, state, minute, minute)

    @staticmethod
    def _add_status(conn: sqlite3.Connection, component: str, column: str,
                    first: int, last: int) -> None:
        """Add the minutes first..last, both counted, to `column`, each on the
        UTC day it fell on. `column` is one of STATUS_COLUMNS, checked above.
        Minutes only ever come after the component's last, so each day's
        last minute is simply the newest one added."""
        while first <= last:
            upto = min(last, (first // 1440 + 1) * 1440 - 1)       # this day's last minute
            conn.execute(
                f"INSERT INTO status_minutes (component, day, {column}, last_minute) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(component, day) DO UPDATE "
                f"SET {column} = {column} + excluded.{column}, "
                "last_minute = excluded.last_minute",
                (component, _status_day(first), upto - first + 1, upto))
            first = upto + 1

    def status_minutes(self, since_day: str) -> list[dict[str, Any]]:
        """Every component's counted days from `since_day` (YYYY-MM-DD) on."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT component, day, green, yellow, red FROM status_minutes "
                "WHERE day >= ? ORDER BY component, day", (since_day,)).fetchall()
        return [dict(r) for r in rows]

    def prune_status_minutes(self, before_day: str) -> int:
        """Forget every day before `before_day`."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM status_minutes WHERE day < ?", (before_day,))
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
        if int(capacity) > self._max_slots:
            raise StoreError(f"{slotstates.ONE_SLOT_WHY}; a machine's capacity is at most "
                             f"{self._max_slots}")
        with self._write_txn() as conn:
            have = conn.execute(
                "SELECT COUNT(*) AS n FROM slots WHERE node_id = ?",
                (node_id,)).fetchone()["n"]
            if int(capacity) < have:
                raise StoreError(
                    f"{node_id} already has {have} slots declared; remove some "
                    f"before lowering its capacity to {capacity}")
            cur = conn.execute(
                "UPDATE nodes SET capacity = ? WHERE id = ?", (int(capacity), node_id))
        return cur.rowcount > 0

    def reserve_machine(self, node_id: str, account_id: Optional[str]) -> None:
        """Keep a shared machine's free slots for one account, or, with None,
        open them to anybody again.

        Only the next claim is affected: a slot somebody already holds stays
        theirs, because taking one back is a release, with the wipe it
        implies, and never a side effect of this. A machine is what the
        console lists as one — capacity for more than one slot, or a slot
        declared on it; keeping somebody's own node is refused rather than
        stored as a promise that means nothing.
        """
        with self._write_txn() as conn:
            node = conn.execute(
                "SELECT capacity FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if node is None:
                raise StoreError(f"no machine {node_id!r}")
            if account_id is not None:
                # A machine's slots only: an owner's node counted as their
                # slot has nothing on offer to keep for anybody.
                declared = conn.execute(
                    "SELECT COUNT(*) AS n FROM slots WHERE node_id = ? AND kind = ?",
                    (node_id, slotstates.MACHINE_SLOT)).fetchone()["n"]
                if int(node["capacity"]) <= 1 and not declared:
                    raise StoreError(
                        f"{node_id} is not a shared machine: it has no slots and room "
                        "for one, so there is nothing on it to keep for anybody")
                if conn.execute("SELECT 1 FROM accounts WHERE id = ?",
                                (account_id,)).fetchone() is None:
                    raise StoreError(f"no account {account_id!r}")
            conn.execute("UPDATE nodes SET reserved_for = ? WHERE id = ?",
                         (account_id, node_id))

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

    def set_account_role(self, account_id: str, role: str) -> bool:
        """Make an account an operator, or not. The server's CLI is the only caller."""
        if role not in ("user", "admin"):
            raise StoreError("role must be 'user' or 'admin'")
        with self._write_txn() as conn:
            cur = conn.execute("UPDATE accounts SET role = ? WHERE id = ?", (role, account_id))
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

    # -- payments ---------------------------------------------------------
    #
    # A record, never an enforcer. Nothing that claims or releases a slot calls
    # anything here, and nothing here touches a slot or an allowance.

    def record_payment(self, account_id: str, *, amount: str, currency: str,
                       through: str, note: str = "", recorded_by: str,
                       now: float) -> int:
        """Write down a payment, as typed. Returns its id."""
        try:
            minor = payments.parse_amount(amount)
            code = payments.parse_currency(currency)
            day = payments.parse_through(through, payments.today(now))
            note = payments.check_note(note)
        except payments.PaymentError as exc:
            raise StoreError(str(exc)) from exc
        with self._write_txn() as conn:
            known = conn.execute("SELECT 1 FROM accounts WHERE id = ?",
                                 (account_id,)).fetchone()
            if known is None:
                raise StoreError("no such account")
            cur = conn.execute(
                "INSERT INTO payments (account_id, amount_minor, currency, paid_through, "
                "note, recorded_by, recorded_at) VALUES (?,?,?,?,?,?,?)",
                (account_id, minor, code, day, note, recorded_by, now))
        return int(cur.lastrowid)

    def void_payment(self, payment_id: int, *, now: float) -> None:
        """Mark a payment written down in error. It stays in the record."""
        with self._write_txn() as conn:
            cur = conn.execute(
                "UPDATE payments SET voided_at = ? WHERE id = ? AND voided_at IS NULL",
                (now, payment_id))
            if cur.rowcount == 0:
                raise StoreError(f"no payment {payment_id} left to void")

    def list_payments(self, account_id: str | None = None) -> list[dict[str, Any]]:
        """Newest first, voided ones included."""
        query = "SELECT * FROM payments"
        args: tuple[Any, ...] = ()
        if account_id is not None:
            query += " WHERE account_id = ?"
            args = (account_id,)
        with self._lock:
            rows = self._conn.execute(
                query + " ORDER BY recorded_at DESC, id DESC", args).fetchall()
        return [dict(r) for r in rows]

    # -- the price --------------------------------------------------------
    #
    # Shown on the public pages, never charged. Like the ledger above, nothing
    # that claims or releases a slot reads it.

    def get_price(self) -> Optional[dict[str, Any]]:
        """The price the operator set, with who set it and when; None when there
        is none, or when the stored one no longer passes (a hand edit), so the
        pages fall back to their own words rather than publish it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, updated_at, updated_by FROM settings WHERE key = ?",
                (pricing.SETTING_KEY,)).fetchone()
        price = pricing.from_json(row["value"]) if row is not None else None
        if price is None:
            return None
        return {"price": price, "updated_at": row["updated_at"],
                "updated_by": row["updated_by"]}

    def set_price(self, amount: str, currency: str, *, by: str,
                  now: float) -> pricing.Price:
        """Set the price every public page shows. Checked first: a refused
        price changes nothing."""
        try:
            price = pricing.parse(amount, currency)
        except pricing.PriceError as exc:
            raise StoreError(str(exc)) from exc
        with self._write_txn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (pricing.SETTING_KEY, pricing.to_json(price), now, str(by)[:254]))
        return price

    def clear_price(self) -> bool:
        """Show no price; the pages go back to "agreed with the operator".
        False when there was none."""
        with self._write_txn() as conn:
            cur = conn.execute("DELETE FROM settings WHERE key = ?", (pricing.SETTING_KEY,))
        return cur.rowcount > 0

    # -- Claude Code releases and updates ------------------------------------
    #
    # The numbers Anthropic's channels stood at when last read, and the updates
    # holders asked for. A record of intent: the machine does the installing.

    def get_channel_versions(self) -> dict[str, Any]:
        """The last good read of each channel, checked again on the way out."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?",
                                     (claude_versions.SETTING_KEY,)).fetchone()
        return claude_versions.from_json(row["value"]) if row is not None else {}

    def set_channel_versions(self, record: Mapping[str, Any], *, now: float) -> None:
        with self._write_txn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (claude_versions.SETTING_KEY, claude_versions.to_json(record), now,
                 "release check"))

    def get_claude_update(self, slot_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM claude_updates WHERE slot_id = ?",
                                     (slot_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _choosable(conn: sqlite3.Connection, slot_id: str,
                   held_by: Optional[str]) -> sqlite3.Row:
        """The slot, if its holder may choose its Claude Code right now: set up
        and held, and not under the operator's hold. Checked in the caller's
        transaction, like every other holder action."""
        row = Store._held(conn, slot_id, held_by)
        if row["state"] not in Store.SLOT_SIGN_IN_STATES:
            raise StoreError(f"{slot_id} is {row['state']}; its Claude Code can be chosen "
                             "once it is set up")
        node = conn.execute("SELECT pinned_version FROM nodes WHERE id = ?",
                            (row["node_id"],)).fetchone()
        slot = {"kind": row["kind"], "claude_channel": None}
        if claude_versions.slot_target(slot, dict(node) if node else {}).held:
            raise StoreError(f"{slot_id}'s machine is held at a version by the operator")
        return row

    @staticmethod
    def _set_channel(conn: sqlite3.Connection, row: sqlite3.Row, slot_id: str,
                     channel: str) -> None:
        """A machine slot keeps its own choice; somebody's own node takes it as
        the node's pin, which is theirs to set."""
        if row["kind"] == slotstates.OWNER_SLOT:
            conn.execute("UPDATE nodes SET pinned_version = ? WHERE id = ?",
                         (channel, row["node_id"]))
        else:
            conn.execute("UPDATE slots SET claude_channel = ? WHERE id = ?", (channel, slot_id))

    def request_claude_update(self, slot_id: str, now: float, *, to_version: str = "",
                              held_by: Optional[str] = None) -> None:
        """Move a slot to the latest release now, for its holder.

        The slot switches to the latest channel, so it keeps itself current
        from then on and the daily stable check cannot undo it; and an update
        is asked for, so the machine installs now rather than at its next
        check. One transaction, the holder checked inside it.
        """
        with self._write_txn() as conn:
            row = self._choosable(conn, slot_id, held_by)
            self._set_channel(conn, row, slot_id, "latest")
            conn.execute(
                "INSERT INTO claude_updates (slot_id, requested_at, state, to_version, "
                "detail, updated_at) VALUES (?, ?, 'pending', ?, '', ?) "
                "ON CONFLICT(slot_id) DO UPDATE SET requested_at = excluded.requested_at, "
                "state = 'pending', to_version = excluded.to_version, detail = '', "
                "updated_at = excluded.updated_at",
                (slot_id, now, str(to_version or "")[:40], now))

    def choose_stable(self, slot_id: str, *, held_by: Optional[str] = None) -> None:
        """Back to the stable release, for its holder. The machine moves there
        at its next quiet moment; an update still waiting is withdrawn."""
        with self._write_txn() as conn:
            row = self._choosable(conn, slot_id, held_by)
            self._set_channel(conn, row, slot_id, "stable")
            conn.execute("DELETE FROM claude_updates WHERE slot_id = ?", (slot_id,))

    def record_claude_update(self, slot_id: str, requested_at: Any, state: str,
                             to_version: str, detail: str, now: float) -> bool:
        """What the machine says it did about an update. Only news about the
        update still waiting counts: a report that names another request, one
        already answered, or a state it cannot have, changes nothing."""
        if state not in ("done", "failed"):
            return False
        if not isinstance(requested_at, (int, float)) or isinstance(requested_at, bool):
            return False
        to_version = str(to_version or "")[:40]
        with self._write_txn() as conn:
            cur = conn.execute(
                "UPDATE claude_updates SET state = ?, "
                "to_version = CASE WHEN ? != '' THEN ? ELSE to_version END, "
                "detail = ?, updated_at = ? "
                "WHERE slot_id = ? AND requested_at = ? AND state = 'pending'",
                (state, to_version, to_version, str(detail or "")[:300], now, slot_id,
                 float(requested_at)))
        return cur.rowcount > 0

    #: Said when a machine never answered an update in time.
    UPDATE_TIMED_OUT = "the machine did not answer in time; try again"

    def expire_claude_updates(self, now: float, max_age_s: float) -> int:
        """An update nobody answered becomes a failure the page can say; an
        answered one is forgotten once it has been on the page long enough."""
        with self._write_txn() as conn:
            timed_out = conn.execute(
                "UPDATE claude_updates SET state = 'failed', detail = ?, updated_at = ? "
                "WHERE state = 'pending' AND requested_at < ?",
                (self.UPDATE_TIMED_OUT, now, now - max_age_s)).rowcount
            dropped = conn.execute(
                "DELETE FROM claude_updates WHERE state != 'pending' AND updated_at < ?",
                (now - max_age_s,)).rowcount
        return timed_out + dropped

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
        validate_slot_id(slot_id)
        if not UNIX_USER_RE.match(unix_user or ""):
            raise StoreError("unix user must be a valid Linux login name")
        validate_node_id(node_id)
        with self._write_txn() as conn:
            node = conn.execute(
                "SELECT capacity FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if node is None:
                raise StoreError(f"no machine {node_id!r}")
            # The slot's id is its machine's name while it is free: it may be
            # its own machine's id — the rule, pool-1 on pool-1 — and nothing
            # else that already answers to a name.
            if conn.execute("SELECT 1 FROM slots WHERE name = ?", (slot_id,)).fetchone() or \
                    conn.execute("SELECT 1 FROM nodes WHERE id = ? AND id != ?",
                                 (slot_id, node_id)).fetchone():
                raise StoreError(f"something already answers to {slot_id!r}")
            # Capacity is what the operator declared they sold. Refuse to
            # declare more slots than that rather than discovering it as a
            # machine that will not hold them.
            placed = [r["id"] for r in conn.execute(
                "SELECT id FROM slots WHERE node_id = ? ORDER BY id", (node_id,))]
            if len(placed) >= self._max_slots:
                raise StoreError(f"{node_id} already has its slot ({', '.join(placed)}): "
                                 f"{slotstates.ONE_SLOT_WHY}")
            have = conn.execute(
                "SELECT COUNT(*) AS n FROM slots WHERE node_id = ?",
                (node_id,)).fetchone()["n"]
            if have >= int(node["capacity"]):
                raise StoreError(
                    f"{node_id} declares capacity {node['capacity']} and already "
                    f"has {have} slots; raise its capacity first")
            try:
                conn.execute(
                    "INSERT INTO slots (id, node_id, unix_user, state, held_by, "
                    "claimed_at, released_at, device_token_at) "
                    "VALUES (?,?,?,?,NULL,NULL,NULL,0)",
                    (slot_id, node_id, unix_user, slotstates.FREE))
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"slot already exists: {exc}") from exc
        return self.get_slot(slot_id)  # type: ignore[return-value]

    def remove_slot(self, slot_id: str) -> None:
        """Take a slot off a machine. Only a free slot may go.

        Free is the one state that means the Linux user and its files are gone,
        because only a finished wipe produces it. Deleting the row in any other
        state would drop our record of somebody's account while the account
        itself is still sitting on the machine — the slot stops counting
        against their allowance and nothing is left pointing at the mess.
        """
        with self._write_txn() as conn:
            row = conn.execute(
                "SELECT state, kind, node_id, held_by FROM slots WHERE id = ?",
                (slot_id,)).fetchone()
            if row is None:
                raise StoreError(f"no slot {slot_id!r}")
            if row["kind"] == slotstates.OWNER_SLOT:
                raise StoreError(
                    f"{slot_id} is its holder's own machine, counted as their slot. "
                    f"Let go of it with: ccfleetd node hold {row['node_id']} --none")
            if row["state"] != slotstates.FREE:
                raise StoreError(
                    f"{slot_id} is {row['state']}"
                    + (f", held by {row['held_by']}" if row["held_by"] else "")
                    + ". Release it first; a slot is only safe to forget once "
                      "the wipe has finished.")
            # No `AND state = 'free'` pin here: the state was read inside
            # this write transaction, so nothing can have changed it. A pin
            # would read as a safeguard while being unreachable, which is
            # worse than not having one.
            conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))

    # Every column that holds a slot's id, beside its sign-in row, which
    # `logins` keeps under "slot:<id>" and which moves with it. Guarded by the
    # same schema test as NODE_ID_COLUMNS.
    SLOT_ID_COLUMNS = (("slots", "id"), ("account_intents", "slot_id"),
                       ("claude_updates", "slot_id"))

    def rename_slot(self, old: str, new: str) -> None:
        """Give a slot a new id, in any state — held and in use included.

        The machine never hears a slot's id: it knows each slot by its Linux
        user and each claim by its time, so there is nothing to do on the box.
        The holder, state, claim, sign-in and request about accounts all move
        with it, in one transaction.

        A sign-in or request still filed under the new id belongs to no slot —
        releasing clears both — and is dropped rather than adopted: a sign-in
        row can hold somebody's minted token.
        """
        validate_slot_id(new)
        with self._write_txn() as conn:
            if conn.execute("SELECT 1 FROM slots WHERE id = ?", (old,)).fetchone() is None:
                raise StoreError(f"no slot {old!r}")
            if conn.execute("SELECT 1 FROM slots WHERE id = ?", (new,)).fetchone() is not None:
                raise StoreError(f"a slot called {new!r} already exists")
            # A slot's id is its machine's name while it is free, so nothing
            # else may already answer to it: another slot's holder, or another
            # machine. Its own machine's id, and its own holder's name, may.
            if conn.execute("SELECT 1 FROM slots WHERE name = ? AND id != ?",
                            (new, old)).fetchone() is not None:
                raise StoreError(f"a slot already answers to {new!r}")
            if conn.execute("SELECT 1 FROM nodes WHERE id = ? AND id != "
                            "(SELECT node_id FROM slots WHERE id = ?)",
                            (new, old)).fetchone() is not None:
                raise StoreError(f"another machine already answers to {new!r}")
            conn.execute("DELETE FROM logins WHERE node_id = ?", (slot_login_key(new),))
            conn.execute("DELETE FROM account_intents WHERE slot_id = ?", (new,))
            conn.execute("DELETE FROM claude_updates WHERE slot_id = ?", (new,))
            for table, column in self.SLOT_ID_COLUMNS:
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",  # noqa: S608
                             (new, old))
            conn.execute("UPDATE logins SET node_id = ? WHERE node_id = ?",
                         (slot_login_key(new), slot_login_key(old)))

    def get_slot(self, slot_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        return dict(row) if row else None

    def list_slots(self, *, node_id: str | None = None,
                   held_by: str | None = None,
                   state: str | None = None,
                   kind: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if node_id is not None:
            where.append("node_id = ?")
            args.append(node_id)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
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
            if to == slotstates.FREE:
                # The lifecycle does allow releasing -> free, but not by this
                # door. finish_release clears the holder, the claim time and
                # the device-token mark in the same statement; arriving at free
                # through here would leave a slot that reads as nobody's while
                # still naming the person whose files may still be on it.
                raise slotstates.TransitionError(
                    "a slot reaches free only through finish_release(), which "
                    "clears the holder in the same statement")
            slotstates.check_move(frm, to)
            cur = self._conn.execute(
                "UPDATE slots SET state = ? WHERE id = ? AND state = ?",
                (to, slot_id, frm))
            self._conn.commit()
        return cur.rowcount > 0

    def begin_release(self, slot_id: str, *, held_by: Optional[str] = None,
                      named: Optional[str] = None) -> bool:
        """Start the wipe. Legal from every state a person can hold.

        Whatever sign-in was in flight goes with it: its URL, a code typed in,
        a minted device token waiting to be collected. All of it belongs to the
        person giving the slot back, and none of it may be waiting for the next.

        `named` is the operator's typed confirmation: the name the slot goes by
        (its holder's, else its id), checked in this same transaction. Checked
        before it, a slot freed and claimed by somebody else in between would
        be wiped on the old holder's name.
        """
        # One transaction, both or neither. As two, a crash between them left
        # the slot releasing with the sign-in still in place — and once that
        # slot was wiped, freed and claimed again, the next holder was handed
        # the last one's URL, or read their minted token.
        with self._write_txn() as conn:
            row = self._held(conn, slot_id, held_by)
            shown = row["name"] or slot_id
            if named is not None and named != shown:
                raise StoreError(f"type the slot's name, {shown}, to confirm")
            if row["kind"] == slotstates.OWNER_SLOT:
                # Releasing means wiping, and nothing on somebody's own node is
                # ours to wipe. Letting go of the record is its own command.
                raise StoreError(
                    f"{slot_id} is its holder's own machine: ccfleet never wipes anything "
                    f"on it. To stop counting it as a slot: ccfleetd node hold "
                    f"{row['node_id']} --none")
            slotstates.check_move(row["state"], slotstates.RELEASING)
            conn.execute("UPDATE slots SET state = ? WHERE id = ?",
                         (slotstates.RELEASING, slot_id))
            conn.execute("DELETE FROM logins WHERE node_id = ?",
                         (slot_login_key(slot_id),))
        return True

    def slot_on_machine(self, node_id: str, unix_user: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM slots WHERE node_id = ? AND unix_user = ?",
                (node_id, unix_user)).fetchone()
        return dict(row) if row else None

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
            freed = self._free_released(self._conn, slot_id, now)
            self._conn.commit()
        return freed

    @staticmethod
    def _free_released(conn: sqlite3.Connection, slot_id: str, now: float) -> bool:
        """The one statement that turns a finished wipe into a free slot.

        Shared by every path that frees a slot, so what "free" clears cannot
        differ between them: the holder, when they took it, and when they were
        last handed a device token — all of it in the same statement, pinned
        to `releasing` so a slot in any other state is left untouched.
        """
        # The name goes too: it was the last holder's, and a free slot is
        # called by its id until the next claim names it after somebody else.
        # So does their last change of account: the next holder's week is
        # their own.
        cur = conn.execute(
            "UPDATE slots SET state = ?, held_by = NULL, claimed_at = NULL, "
            "released_at = ?, device_token_at = 0, name = NULL, claude_channel = NULL, "
            "account_switched_at = NULL WHERE id = ? AND state = ?",
            (slotstates.FREE, now, slot_id, slotstates.RELEASING))
        if cur.rowcount > 0:
            # Nor their update: the next holder starts on the machine's pin.
            conn.execute("DELETE FROM claude_updates WHERE slot_id = ?", (slot_id,))
        return cur.rowcount > 0

    def apply_slot_report(self, node_id: str, reports: Any, *,
                          now: float) -> list[dict[str, Any]]:
        """Take in what a machine says about its slots. Returns the moves made.

        Each report is about one Linux user on this machine, and can only ever
        touch the slot declared for that user on this machine — a machine
        cannot move another machine's slots by naming them. What it says is
        recorded first (whether the user exists, and when we heard), and then
        the lifecycle decides whether that is evidence for a move; see
        slots.next_state for what counts as evidence.

        Every move is decided and written inside one write transaction, so a
        person releasing a slot while this runs waits for it rather than being
        overwritten by a report about the moment before.
        """
        by_user: dict[str, Mapping[str, Any]] = {}
        for report in reports if isinstance(reports, (list, tuple)) else ():
            user = report.get("unix_user") if isinstance(report, Mapping) else None
            if isinstance(user, str):
                by_user.setdefault(user, report)
        moved: list[dict[str, Any]] = []
        if not by_user:
            return moved
        with self._write_txn() as conn:
            # Machine slots only. An owner slot is somebody's own node counted
            # as theirs; nothing a report says may wipe-confirm it into free.
            rows = conn.execute(
                "SELECT id, unix_user, state, claimed_at FROM slots "
                "WHERE node_id = ? AND kind = ?",
                (node_id, slotstates.MACHINE_SLOT)).fetchall()
            for row in rows:
                report = by_user.get(row["unix_user"])
                if report is None:
                    continue
                present = report.get("present")
                conn.execute(
                    "UPDATE slots SET present = ?, reported_at = ? WHERE id = ?",
                    (int(present) if isinstance(present, bool) else None, now, row["id"]))
                to = slotstates.next_state(row["state"], row["claimed_at"], report)
                if to is None:
                    continue
                # No state pin on these writes: the state was read inside this
                # write transaction, so nothing can have changed it since.
                if to == slotstates.FREE:
                    self._free_released(conn, row["id"], now)
                else:
                    conn.execute("UPDATE slots SET state = ? WHERE id = ?",
                                 (to, row["id"]))
                moved.append({"slot": row["id"], "from": row["state"], "to": to})
        for move in moved:
            log.info("slot %s: %s -> %s on the machine's report",
                     move["slot"], move["from"], move["to"])
        return moved

    def expire_claims(self, *, older_than: float) -> list[str]:
        """Give up on claims whose provisioning never finished.

        They fail sideways into releasing, never back to free: provisioning
        may have got as far as creating the account before it stalled, and
        only the wipe that follows can say the slot is empty again.
        """
        with self._write_txn() as conn:
            rows = conn.execute(
                "SELECT id FROM slots WHERE state = ? AND claimed_at < ?",
                (slotstates.CLAIMING, older_than)).fetchall()
            for row in rows:
                conn.execute("UPDATE slots SET state = ? WHERE id = ?",
                             (slotstates.RELEASING, row["id"]))
        return [row["id"] for row in rows]

    @staticmethod
    def _taken_names(conn: sqlite3.Connection, but: str = "") -> set[str]:
        """Every name a machine answers to or may: each node's id and each
        slot's id and name, but the slot `but`'s own. The name becomes a
        hostname, and one another machine already answers to would be two
        machines under one name."""
        taken = {r["id"] for r in conn.execute("SELECT id FROM nodes")}
        for row in conn.execute("SELECT id, name FROM slots WHERE id != ?", (but,)):
            taken.add(row["id"])
            if row["name"]:
                taken.add(row["name"])
        return taken

    @classmethod
    def _name_for(cls, conn: sqlite3.Connection, account: sqlite3.Row) -> str:
        """What a slot this account takes is called: a neutral "slot-4821",
        never anything of theirs (Erik, 2026-09-24), which its holder renames
        on their page; "<handle>-<n>" only for a handle the operator set.

        Read in the claim's own write transaction, so two claims cannot both
        pick the same name.
        """
        taken = cls._taken_names(conn)
        handle = account["handle"]
        name = names.next_name(handle, taken) if handle else names.neutral_name(taken)
        if not names.valid_hostname(name):  # pragma: no cover - names.py guarantees it
            raise StoreError(f"{name!r} is not a hostname")
        return name

    def name_slot(self, slot_id: str, name: Optional[str], *,
                  held_by: Optional[str] = None) -> str:
        """Give a machine's slot in use a new name, which is its machine's
        hostname: its holder's own nickname, or with None a fresh neutral one.
        Returns the name.

        Checked in one transaction: the holder (None is the operator's side),
        a machine's slot claimed or in use, and a name nothing else answers
        to. The machine answers to it at its next run, and Remote Control
        restarts under it.
        """
        with self._write_txn() as conn:
            # Whose it is first: to anybody else, a slot answers as a missing
            # one, whatever the name they sent.
            row = self._held(conn, slot_id, held_by)
            if name is not None and not names.valid_nickname(name):
                raise BadName(f"a slot's name is 2-{names.MAX_NICKNAME} lowercase letters, "
                              "digits and inner hyphens, and not pool-<n> or slot-<n>")
            if (row["kind"] != slotstates.MACHINE_SLOT
                    or row["state"] not in (slotstates.CLAIMED, slotstates.ACTIVE)):
                raise StoreError(f"{slot_id} can be named once it is set up")
            taken = self._taken_names(conn, but=slot_id)
            chosen = name if name is not None else names.neutral_name(taken)
            if chosen in taken:
                raise NameTaken(f"{chosen} is taken: another slot or machine answers to it")
            conn.execute("UPDATE slots SET name = ? WHERE id = ?", (chosen, slot_id))
        return chosen

    def set_account_handle(self, account_id: str, handle: Optional[str]) -> None:
        """What this account's slots are named after, "<handle>-<n>", when the
        person asked for it; None goes back to neutral names. Only claims from
        now on are named by it: a slot already named keeps its name."""
        if handle is not None and not names.valid_handle(handle):
            raise StoreError(
                f"a handle is 1-{names.MAX_HANDLE} lowercase letters, digits and inner "
                "hyphens: it starts the hostname of every slot this person holds")
        with self._write_txn() as conn:
            cur = conn.execute("UPDATE accounts SET handle = ? WHERE id = ?",
                               (handle, account_id))
            if cur.rowcount == 0:
                raise StoreError(f"no account {account_id!r}")

    def hold_owner_node(self, node_id: str, account_id: str, *,
                        unix_user: Optional[str] = None, now: float) -> dict[str, Any]:
        """Count somebody's own node as a slot they hold (Erik, 2026-09-23).

        A record and nothing more: the slot is in use from the start, is never
        on offer, is never wiped and never reaches the machine agent, and the
        node goes on exactly as it was. It counts toward the holder's
        allowance like any slot, so the one-account-one-slot bookkeeping sees
        everything a person uses. Holding again updates the one row; handing
        it to somebody else checks their allowance.
        """
        with self._write_txn() as conn:
            node = conn.execute("SELECT owner FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if node is None:
                raise StoreError(f"no node {node_id!r}")
            if conn.execute("SELECT 1 FROM slots WHERE node_id = ? AND kind = ?",
                            (node_id, slotstates.MACHINE_SLOT)).fetchone() is not None:
                raise StoreError(
                    f"{node_id} is a shared machine: its slot is claimed from the page, "
                    "not held")
            account = conn.execute("SELECT email, slot_quota FROM accounts WHERE id = ?",
                                   (account_id,)).fetchone()
            if account is None:
                raise StoreError(f"no account {account_id!r}")
            user = unix_user or node["owner"]
            if not UNIX_USER_RE.match(user or ""):
                raise StoreError(f"{user!r} is not a Linux login; name it with --unix-user")
            existing = conn.execute("SELECT id, held_by FROM slots WHERE node_id = ? AND kind = ?",
                                    (node_id, slotstates.OWNER_SLOT)).fetchone()
            marks = ",".join("?" * len(slotstates.HELD))
            held = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM slots WHERE held_by = ? "  # noqa: S608
                f"AND state IN ({marks}) AND id != ?",
                (account_id, *sorted(slotstates.HELD),
                 existing["id"] if existing else "")).fetchone()["n"])
            quota = int(account["slot_quota"])
            if held + 1 > quota:
                raise QuotaExceeded(
                    f"{account['email']} has an allowance of {quota} and already holds "
                    f"{held}; raise their allowance first: "
                    f"ccfleetd account quota {account['email']} {held + 1}")
            if not existing or existing["held_by"] != account_id:
                # The node's own sign-in row is what an owner slot's page reads:
                # a link, a code or a minted token in it belongs to whoever
                # started it — the last holder, or the console — and never to
                # the account the record now goes to.
                conn.execute("DELETE FROM logins WHERE node_id = ?", (node_id,))
                # Nor an update the last holder asked for.
                conn.execute("DELETE FROM claude_updates WHERE slot_id = ?",
                             (existing["id"] if existing else node_id,))
            if existing:
                conn.execute("UPDATE slots SET held_by = ?, unix_user = ?, state = ? "
                             "WHERE id = ?",
                             (account_id, user, slotstates.ACTIVE, existing["id"]))
                slot_id = existing["id"]
            else:
                if conn.execute("SELECT 1 FROM slots WHERE id = ?",
                                (node_id,)).fetchone() is not None:
                    raise StoreError(f"a slot called {node_id!r} already exists")
                conn.execute(
                    "INSERT INTO slots (id, node_id, unix_user, state, held_by, claimed_at, "
                    "released_at, device_token_at, kind) VALUES (?,?,?,?,?,?,NULL,0,?)",
                    (node_id, node_id, user, slotstates.ACTIVE, account_id, now,
                     slotstates.OWNER_SLOT))
                slot_id = node_id
        return self.get_slot(slot_id)  # type: ignore[return-value]

    def unhold_owner_node(self, node_id: str) -> bool:
        """Stop counting somebody's own node as their slot. Forgets the record
        and nothing else: the node, its history and its own sign-in stay."""
        with self._write_txn() as conn:
            conn.execute("DELETE FROM claude_updates WHERE slot_id IN "
                         "(SELECT id FROM slots WHERE node_id = ? AND kind = ?)",
                         (node_id, slotstates.OWNER_SLOT))
            cur = conn.execute("DELETE FROM slots WHERE node_id = ? AND kind = ?",
                               (node_id, slotstates.OWNER_SLOT))
        return cur.rowcount > 0

    def claim_slot(self, account_id: str, *, now: float,
                   node_id: str | None = None,
                   heard_since: float | None = None) -> dict[str, Any]:
        """Take a free slot for this account, allowance permitting.

        The allowance is checked against what they already hold in the same
        lock and the same transaction that takes the slot, and the take itself
        is pinned to `state = 'free'`. There is no window where two browser
        tabs each see a free slot and both get one.

        Only a slot its machine has reported empty is handed out. Free is a
        claim about the machine — the Linux user does not exist — and a slot
        declared a minute ago, or one whose user somebody created by hand, has
        nothing on the machine's side vouching for it. `heard_since` narrows it
        further to machines that have reported recently, so a claim is not
        handed to a machine that has gone quiet and would leave it waiting out
        the claim timeout.

        A machine kept for somebody hands its free slots to them alone, and
        they are given it before any open machine. Both are read here, in the
        transaction that takes the slot: a reservation read any earlier can be
        stale by the time the slot is taken.

        Raises QuotaExceeded when they have no allowance left, and
        NoSlotAvailable when nothing is free — two different answers that the
        page shows differently, so they are two different exceptions rather
        than one absent return value.
        """
        marks = ",".join("?" * len(slotstates.HELD))
        held_states = sorted(slotstates.HELD)
        with self._write_txn() as conn:
            account = conn.execute(
                "SELECT slot_quota, email, handle FROM accounts WHERE id = ?",
                (account_id,)).fetchone()
            if account is None:
                raise StoreError(f"no account {account_id!r}")
            quota = int(account["slot_quota"])
            held = int(conn.execute(
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
                extra += " AND s.node_id = ?"
                args.append(node_id)
            if heard_since is not None:
                extra += " AND s.reported_at >= ?"
                args.append(heard_since)
            # A machine's slot only: an owner slot is somebody's own node,
            # never on offer, whatever state a stray write left it in.
            candidate = conn.execute(
                "SELECT s.id FROM slots s JOIN nodes n ON n.id = s.node_id "  # noqa: S608
                "WHERE s.state = ? AND s.present = 0 AND n.enabled = 1 AND s.kind = ?"
                " AND (n.reserved_for IS NULL OR n.reserved_for = ?)" + extra +
                " ORDER BY n.reserved_for IS NULL, s.node_id, s.unix_user LIMIT 1",
                (slotstates.FREE, slotstates.MACHINE_SLOT, account_id, *args)).fetchone()
            if candidate is None:
                raise NoSlotAvailable(
                    f"no free slot on {node_id}" if node_id
                    else "no free slot on any enabled machine")
            name = self._name_for(conn, account)
            cur = conn.execute(
                "UPDATE slots SET state = ?, held_by = ?, claimed_at = ?, "
                "released_at = NULL, name = ? WHERE id = ? AND state = ?",
                (slotstates.CLAIMING, account_id, now, name,
                 candidate["id"], slotstates.FREE))
            if cur.rowcount == 0:  # pragma: no cover - the write lock precludes it
                raise NoSlotAvailable("the free slot was taken; try again")
        return self.get_slot(candidate["id"])  # type: ignore[return-value]

    # -- signing in --------------------------------------------------------

    def upsert_account_from_google(self, google_sub: str, email: str, *,
                                   now: float) -> dict[str, Any]:
        """The account for this Google identity, creating it on first sign-in.

        Keyed on the subject, never the address: people change their email and
        Google keeps the subject stable, so matching on address would either
        lock somebody out of their own slots or — worse — hand them somebody
        else's account when an address is reassigned.

        A new account gets no allowance. Registering produces a login and an
        empty page until the operator grants one; that is the economics, and
        the default has to be the safe direction.
        """
        with self._write_txn() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE google_sub = ?",
                (google_sub,)).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE accounts SET email = ?, last_seen_at = ? WHERE id = ?",
                    (email, now, row["id"]))
                updated = dict(row)
                updated.update(email=email, last_seen_at=now)
                return updated
            account_id = "u" + secrets.token_hex(ACCOUNT_ID_BYTES)
            conn.execute(
                "INSERT INTO accounts (id, google_sub, email, role, slot_quota, "
                "created_at, last_seen_at) VALUES (?,?,?,'user',0,?,?)",
                (account_id, google_sub, email, now, now))
            return {"id": account_id, "google_sub": google_sub, "email": email,
                    "role": "user", "slot_quota": 0, "created_at": now,
                    "last_seen_at": now}

    def begin_oauth_flow(self, state: str, verifier: str, *, now: float,
                         ttl_s: int, next_url: str = "") -> None:
        """Remember a sign-in that is about to leave for Google."""
        with self._write_txn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_flows "
                "(state, verifier, next_url, created_at, expires_at) "
                "VALUES (?,?,?,?,?)",
                (state, verifier, next_url, now, now + ttl_s))

    def take_oauth_flow(self, state: str, *, now: float) -> dict[str, Any] | None:
        """Consume a pending sign-in. Returns it once, then never again.

        Single use on purpose. A callback URL lands in browser history, in
        referrer headers and in any proxy log on the way; replaying it must not
        start a second sign-in. Expiry is checked here too, so a state sitting
        in a tab somebody left open overnight is not still good in the morning.
        """
        with self._write_txn() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_flows WHERE state = ?", (state,)).fetchone()
            conn.execute("DELETE FROM oauth_flows WHERE state = ?", (state,))
            if row is None or now >= row["expires_at"]:
                return None
            return dict(row)

    def purge_oauth_flows(self, *, now: float) -> int:
        with self._write_txn() as conn:
            cur = conn.execute(
                "DELETE FROM oauth_flows WHERE expires_at <= ?", (now,))
        return cur.rowcount

    SITES = ("product", "admin")

    def create_session(self, account_id: str, *, now: float,
                       ttl_s: int, site: str = "product") -> str:
        """Start a session and return its id, which is shown to nobody twice.

        Only the hash is kept, so this return value is the only copy. It goes
        straight into the cookie.
        """
        if site not in self.SITES:
            raise StoreError(f"no site {site!r}")
        if self.get_account(account_id) is None:
            raise StoreError(f"no account {account_id!r}")
        session_id = sessionlib.new_session_id()
        with self._write_txn() as conn:
            conn.execute(
                "INSERT INTO sessions (id_hash, account_id, created_at, expires_at, site) "
                "VALUES (?,?,?,?,?)",
                (sessionlib.hash_session_id(session_id), account_id, now,
                 now + ttl_s, site))
        return session_id

    def account_for_session(self, session_id: str, *, now: float,
                            site: Optional[str] = None) -> dict[str, Any] | None:
        """Who this session belongs to, or None if it is unknown or expired.

        An expired row is deleted rather than merely ignored, so a stolen
        cookie stops being useful the first time anybody presents it rather
        than whenever a sweep happens to run. `site` names the site asking:
        a session made on the other one is no session here.
        """
        id_hash = sessionlib.hash_session_id(session_id)
        with self._write_txn() as conn:
            row = conn.execute(
                "SELECT account_id, expires_at, site FROM sessions WHERE id_hash = ?",
                (id_hash,)).fetchone()
            if row is None:
                return None
            if site is not None and row["site"] != site:
                return None
            if now >= row["expires_at"]:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
                return None
            account = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
            if account is None:
                # The account went away while the session did not. Nothing to
                # be signed in as.
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
                return None
            conn.execute("UPDATE accounts SET last_seen_at = ? WHERE id = ?",
                         (now, row["account_id"]))
        return dict(account)

    def peek_session(self, session_id: str, *, now: float,
                     site: Optional[str] = None) -> dict[str, Any] | None:
        """Who this session belongs to, touching nothing.

        For pages anybody may read, which only want to say who is looking: the
        same answer account_for_session gives, but no visit is recorded and no
        expired row is swept. Reading a public page must not write, and the
        sweep still happens the next time the session is used for anything.
        """
        id_hash = sessionlib.hash_session_id(session_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT s.site AS session_site, s.expires_at AS session_expires_at, a.* "
                "FROM sessions s JOIN accounts a ON a.id = s.account_id "
                "WHERE s.id_hash = ?", (id_hash,)).fetchone()
        if row is None or now >= row["session_expires_at"]:
            return None
        if site is not None and row["session_site"] != site:
            return None
        return {k: row[k] for k in row.keys()
                if k not in ("session_site", "session_expires_at")}

    def end_session(self, session_id: str) -> bool:
        with self._write_txn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE id_hash = ?",
                               (sessionlib.hash_session_id(session_id),))
        return cur.rowcount > 0

    def end_all_sessions(self, account_id: str) -> int:
        """Sign somebody out everywhere. What an operator reaches for when an
        account is compromised, and what removing an account must do first."""
        with self._write_txn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE account_id = ?",
                               (account_id,))
        return cur.rowcount

    def purge_sessions(self, *, now: float) -> int:
        with self._write_txn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        return cur.rowcount
