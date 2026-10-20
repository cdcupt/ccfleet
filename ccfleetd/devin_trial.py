"""Offline, disabled-by-default trial ledger; no HTTP route or provider adapter.

This module does not authenticate visitors, run Devin, store conversations, or
grant access to an owner's provider account. A future integration must obtain
VerifiedAccount from Fleet's trusted identity boundary and independently prove
licensing, provider spending controls, isolated history and cleanup. A lease is
only a reservation in this separate ledger, never a Fleet API/provider key.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

MAX_LEASE_S = 600
MAX_STEPS = 5
MAX_RUNTIME_S = 120
MAX_STEP_S = 60
MAX_REQUEST_BYTES = 64 * 1024
_ACCOUNT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")
_TABLES = {"devin_trial_control", "devin_trial_sessions"}


class TrialError(ValueError):
    """A local programming, configuration or ledger error; contains no secrets."""


@dataclass(frozen=True)
class VerifiedAccount:
    """Server-derived identity input, not proof supplied by a browser or CLI."""

    account_id: str
    email_verified: bool = False


@dataclass(frozen=True)
class Lease:
    trial_id: str
    secret: str = field(repr=False)
    history_namespace: str
    expires_at: float


@dataclass(frozen=True)
class StepPermit:
    trial_id: str
    step_id: str
    history_namespace: str
    deadline: float
    reserved_seconds: int


@dataclass(frozen=True)
class Decision:
    code: str
    lease: Optional[Lease] = None
    step: Optional[StepPermit] = None


class TrialLedger:
    """A pure admission foundation for a dedicated, otherwise-empty SQLite DB.

    No production Store migration or feature flag imports this class. Enabling
    an instance only permits offline reservations; it cannot execute an agent.
    Runtime is charged conservatively when a step is reserved, with no refund.
    A cleanup failure or deadline overrun permanently pauses this ledger. There
    is deliberately no automatic unpause/reset that could erase one-time use.
    """

    def __init__(self, connection: sqlite3.Connection, *, enabled: bool = False,
                 clock: Callable[[], float] = time.time):
        if type(enabled) is not bool or not callable(clock) or connection.in_transaction:
            raise TrialError("invalid_trial_configuration")
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and tables != _TABLES:
            raise TrialError("dedicated_trial_database_required")
        self.connection, self.enabled, self.clock = connection, enabled, clock
        connection.execute("PRAGMA busy_timeout=100")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS devin_trial_control (
                id INTEGER PRIMARY KEY CHECK (id=1),
                paused INTEGER NOT NULL CHECK (paused IN (0,1)),
                reason TEXT NOT NULL,
                last_now REAL NOT NULL
            );
            INSERT OR IGNORE INTO devin_trial_control VALUES (1,0,'',0);
            CREATE TABLE IF NOT EXISTS devin_trial_sessions (
                trial_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL UNIQUE,
                secret_hash TEXT NOT NULL,
                history_namespace TEXT NOT NULL UNIQUE,
                expires_at REAL NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('open','closing','closed')),
                steps INTEGER NOT NULL CHECK (steps BETWEEN 0 AND 5),
                reserved_seconds INTEGER NOT NULL CHECK (reserved_seconds BETWEEN 0 AND 120),
                active_step TEXT,
                step_deadline REAL,
                active_seconds INTEGER CHECK (active_seconds BETWEEN 1 AND 60)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS devin_trial_one_active
                ON devin_trial_sessions ((1)) WHERE state IN ('open','closing');
        """)

    @contextlib.contextmanager
    def _transaction(self):
        if self.connection.in_transaction:
            raise TrialError("nested_trial_transaction")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise TrialError("trial_ledger_busy") from None
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _pause(self, reason):
        self.connection.execute(
            "UPDATE devin_trial_control SET paused=1,reason=? WHERE id=1", (reason,))

    def _tick(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 10**11:
            raise TrialError("invalid_trial_clock")
        previous = self.connection.execute(
            "SELECT last_now FROM devin_trial_control WHERE id=1").fetchone()[0]
        if now < previous:
            self._pause("clock_regressed")
            now = previous
        self.connection.execute("UPDATE devin_trial_control SET last_now=? WHERE id=1", (now,))
        row = self.connection.execute(
            "SELECT trial_id,steps,expires_at,active_step,step_deadline "
            "FROM devin_trial_sessions WHERE state IN ('open','closing')").fetchone()
        if row and ((row[3] is not None and now >= row[4]) or now >= row[2]):
            # An unused lease created no worker/history. Used/active leases must
            # be reconciled by explicit cleanup; expiration never proves it.
            state = "closing" if row[1] else "closed"
            self.connection.execute(
                "UPDATE devin_trial_sessions SET state=? WHERE trial_id=?", (state, row[0]))
            if row[1]:
                self._pause("expired_worker_or_history_cleanup_required")
        return float(now)

    @staticmethod
    def _principal(principal):
        return (type(principal) is VerifiedAccount and principal.email_verified is True
                and isinstance(principal.account_id, str)
                and _ACCOUNT.fullmatch(principal.account_id) is not None)

    def _row(self, principal, lease):
        if (not self._principal(principal) or type(lease) is not Lease
                or not isinstance(lease.trial_id, str) or not _ID.fullmatch(lease.trial_id)
                or not isinstance(lease.secret, str) or not _ID.fullmatch(lease.secret)):
            return None
        row = self.connection.execute(
            "SELECT trial_id,secret_hash,history_namespace,expires_at,state,steps,"
            "reserved_seconds,active_step,step_deadline,active_seconds FROM devin_trial_sessions "
            "WHERE trial_id=? AND account_id=?", (lease.trial_id, principal.account_id)).fetchone()
        if (row and lease.history_namespace == row[2] and lease.expires_at == row[3]
                and hmac.compare_digest(row[1], hashlib.sha256(lease.secret.encode()).hexdigest())):
            return row
        return None

    @staticmethod
    def _permit_matches(row, permit):
        return (row is not None and type(permit) is StepPermit
                and type(permit.reserved_seconds) is int and row[0] == permit.trial_id
                and row[2] == permit.history_namespace and row[7] == permit.step_id
                and row[8] == permit.deadline and row[9] == permit.reserved_seconds)

    def _gate(self):
        if not self.enabled:
            return "trial_disabled"
        if self.connection.execute(
                "SELECT paused FROM devin_trial_control WHERE id=1").fetchone()[0]:
            return "trial_paused"
        return None

    def admit(self, principal: VerifiedAccount, *, lease_seconds: int = MAX_LEASE_S) -> Decision:
        """Consume one lifetime attempt for this verified Fleet account, never queue."""
        if not self._principal(principal):
            return Decision("verified_account_required")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_LEASE_S:
            return Decision("invalid_lease_seconds")
        with self._transaction():
            now = self._tick()
            if reason := self._gate():
                return Decision(reason)
            if self.connection.execute(
                    "SELECT 1 FROM devin_trial_sessions WHERE account_id=?",
                    (principal.account_id,)).fetchone():
                return Decision("trial_already_used")
            if self.connection.execute(
                    "SELECT 1 FROM devin_trial_sessions WHERE state IN ('open','closing')"
            ).fetchone():
                return Decision("trial_busy")
            lease = Lease(secrets.token_hex(32), secrets.token_hex(32),
                          "trial-" + secrets.token_hex(32), now + lease_seconds)
            self.connection.execute(
                "INSERT INTO devin_trial_sessions VALUES (?,?,?,?,?,'open',0,0,NULL,NULL,NULL)",
                (lease.trial_id, principal.account_id,
                 hashlib.sha256(lease.secret.encode()).hexdigest(),
                 lease.history_namespace, lease.expires_at))
            return Decision("admitted", lease=lease)

    def start_step(self, principal: VerifiedAccount, lease: Lease, *, request: bytes,
                   seconds: int = MAX_STEP_S) -> Decision:
        """Reserve before execution. A future worker must also enforce the deadline."""
        if type(request) is not bytes or not 0 < len(request) <= MAX_REQUEST_BYTES:
            return Decision("request_size_limit")
        if type(seconds) is not int or not 1 <= seconds <= MAX_STEP_S:
            return Decision("step_time_limit")
        with self._transaction():
            now = self._tick()
            if reason := self._gate():
                return Decision(reason)
            row = self._row(principal, lease)
            if row is None:
                return Decision("invalid_trial_lease")
            if row[4] != "open":
                return Decision("trial_closed")
            if row[7] is not None:
                return Decision("step_busy")
            if row[5] >= MAX_STEPS or row[6] >= MAX_RUNTIME_S:
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?", (row[0],))
                return Decision("trial_limit_reached")
            if row[6] + seconds > MAX_RUNTIME_S:
                return Decision("runtime_reservation_limit")
            if now + seconds > row[3]:
                return Decision("lease_time_limit")
            permit = StepPermit(row[0], secrets.token_hex(32), row[2], now + seconds, seconds)
            self.connection.execute(
                "UPDATE devin_trial_sessions SET steps=steps+1,reserved_seconds=reserved_seconds+?,"
                "active_step=?,step_deadline=?,active_seconds=? WHERE trial_id=?",
                (seconds, permit.step_id, permit.deadline, seconds, row[0]))
            return Decision("step_reserved", step=permit)

    def authorize_step(self, principal: VerifiedAccount, lease: Lease,
                       permit: StepPermit) -> bool:
        """A future worker must recheck before/throughout I/O; a permit is not a bearer key."""
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            return (self._gate() is None and self._permit_matches(row, permit) and row[4] == "open")

    def finish_step(self, principal: VerifiedAccount, lease: Lease, permit: StepPermit,
                    *, cleanup_confirmed: bool) -> Decision:
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            if not self._permit_matches(row, permit):
                return Decision("invalid_step_receipt")
            if cleanup_confirmed is not True:
                self._pause("worker_cleanup_unconfirmed")
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?", (row[0],))
                return Decision("cleanup_unconfirmed")
            self.connection.execute(
                "UPDATE devin_trial_sessions SET active_step=NULL,step_deadline=NULL,"
                "active_seconds=NULL "
                "WHERE trial_id=?", (row[0],))
            return Decision("step_finished")

    def cancel(self, principal: VerifiedAccount, lease: Lease) -> Decision:
        """Irreversibly revoke new/ongoing authorization, even while disabled/paused."""
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            if row is None:
                return Decision("invalid_trial_lease")
            if row[4] != "closed":
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?", (row[0],))
            return Decision("trial_cancelled")

    def close(self, principal: VerifiedAccount, lease: Lease,
              *, cleanup_confirmed: bool) -> Decision:
        """A trusted future supervisor attests worker AND history cleanup before reuse."""
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            if row is None:
                return Decision("invalid_trial_lease")
            if row[4] == "closed":
                return Decision("trial_closed")
            if cleanup_confirmed is not True:
                self._pause("trial_cleanup_unconfirmed")
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?", (row[0],))
                return Decision("cleanup_unconfirmed")
            if row[7] is not None:
                return Decision("step_cleanup_required")
            self.connection.execute(
                "UPDATE devin_trial_sessions SET state='closed',active_step=NULL,"
                "step_deadline=NULL,active_seconds=NULL "
                "WHERE trial_id=?", (row[0],))
            return Decision("trial_closed")

    def pause(self) -> None:
        """Local operator kill switch; does not execute, cancel or clean up a provider."""
        with self._transaction():
            self._tick()
            self._pause("operator_pause")
