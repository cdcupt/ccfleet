"""Disabled offline DAILY reported-token admission ledger, never a provider adapter.

VerifiedAccount/VerifiedUsage must come from future trusted server/supervisor
boundaries. No route, provider credential, agent execution or shared access is
created here. Reported usage arrives after the turn; an in-flight turn can exceed
its reservation. This is not a hard token, provider-quota or dollar spending cap.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
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
DAILY_REQUESTS = 20
GLOBAL_DAILY_REQUESTS = 100
GLOBAL_DAILY_RUNTIME_S = 1200
DAILY_LEASES = 5
GLOBAL_DAILY_LEASES = 50
DEFAULT_DAILY_TOKENS = 100_000
DEFAULT_GLOBAL_DAILY_TOKENS = 1_000_000
MAX_REPORTED_TOKENS = 10**12
_ACCOUNT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")
_TABLES = {
    "devin_trial_control",
    "devin_trial_sessions",
    "devin_trial_account_days",
    "devin_trial_global_days",
}
_COLUMNS = (
    "trial_id",
    "secret_hash",
    "history_namespace",
    "expires_at",
    "state",
    "steps",
    "reserved_seconds",
    "active_step",
    "step_deadline",
    "active_seconds",
    "utc_day",
    "active_tokens",
)


class TrialError(ValueError):
    """Local configuration/ledger error, never provider or credential text."""


@dataclass(frozen=True)
class VerifiedAccount:
    """Trusted Fleet identity boundary; never deserialize this from visitor JSON."""

    account_id: str
    email_verified: bool = False


@dataclass(frozen=True)
class VerifiedUsage:
    """Future trusted supervisor's COMPLETE final measured usage, not a client claim."""

    reported_tokens: int
    complete: bool = False


@dataclass(frozen=True)
class DailyAllowance:
    utc_day: str
    resets_at: float
    token_limit: int
    reported_tokens: int
    reserved_tokens: int
    available_tokens: int
    global_available_tokens: int
    remaining_requests: int
    paused: bool
    enabled: bool
    hard_generation_stop: bool = False


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
    reserved_tokens: int


@dataclass(frozen=True)
class Decision:
    code: str
    lease: Optional[Lease] = None
    step: Optional[StepPermit] = None


class TrialLedger:
    """Dedicated offline ledger. Daily rollover never clears unresolved work/pause.

    Time and requests are charged at reservation, with no refunds. Tokens reserve
    the WHOLE smaller remaining user/global allowance; complete trusted usage and
    cleanup settle actual tokens, including overshoot. Unknown usage or cleanup
    latches pause and retains reservation. No automatic unpause/reset exists.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        enabled: bool = False,
        clock: Callable[[], float] = time.time,
        daily_token_limit: int = DEFAULT_DAILY_TOKENS,
        global_daily_token_limit: int = DEFAULT_GLOBAL_DAILY_TOKENS,
    ):
        if (
            type(enabled) is not bool
            or not callable(clock)
            or connection.in_transaction
            or any(
                type(v) is not int or not 1 <= v <= 10**9
                for v in (daily_token_limit, global_daily_token_limit)
            )
        ):
            raise TrialError("invalid_trial_configuration")
        self.connection, self.enabled, self.clock = connection, enabled, clock
        self.daily_token_limit, self.global_daily_token_limit = (
            daily_token_limit,
            global_daily_token_limit,
        )
        limits = json.dumps(
            [
                2,
                daily_token_limit,
                global_daily_token_limit,
                DAILY_REQUESTS,
                GLOBAL_DAILY_REQUESTS,
                MAX_RUNTIME_S,
                GLOBAL_DAILY_RUNTIME_S,
                DAILY_LEASES,
                GLOBAL_DAILY_LEASES,
            ],
            separators=(",", ":"),
        )
        connection.execute("PRAGMA busy_timeout=100")
        schema = """
            CREATE TABLE IF NOT EXISTS devin_trial_control (
                id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL CHECK(paused IN (0,1)),
                reason TEXT NOT NULL, last_now REAL NOT NULL, limits TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS devin_trial_account_days (
                account_id TEXT NOT NULL, utc_day INTEGER NOT NULL,
                used_tokens INTEGER NOT NULL CHECK(used_tokens>=0),
                reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>=0),
                requests INTEGER NOT NULL CHECK(requests>=0),
                runtime_seconds INTEGER NOT NULL CHECK(runtime_seconds>=0),
                leases INTEGER NOT NULL CHECK(leases>=0), PRIMARY KEY(account_id,utc_day));
            CREATE TABLE IF NOT EXISTS devin_trial_global_days (
                utc_day INTEGER PRIMARY KEY, used_tokens INTEGER NOT NULL CHECK(used_tokens>=0),
                reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>=0),
                requests INTEGER NOT NULL CHECK(requests>=0),
                runtime_seconds INTEGER NOT NULL CHECK(runtime_seconds>=0),
                leases INTEGER NOT NULL CHECK(leases>=0));
            CREATE TABLE IF NOT EXISTS devin_trial_sessions (
                trial_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, secret_hash TEXT NOT NULL,
                history_namespace TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','closing','closed')),
                steps INTEGER NOT NULL CHECK(steps BETWEEN 0 AND 5),
                reserved_seconds INTEGER NOT NULL CHECK(reserved_seconds BETWEEN 0 AND 120),
                active_step TEXT, step_deadline REAL,
                active_seconds INTEGER CHECK(active_seconds BETWEEN 1 AND 60),
                utc_day INTEGER NOT NULL, active_tokens INTEGER CHECK(active_tokens>0));
            CREATE UNIQUE INDEX IF NOT EXISTS devin_trial_one_active
                ON devin_trial_sessions ((1)) WHERE state IN ('open','closing');
            CREATE UNIQUE INDEX IF NOT EXISTS devin_trial_one_account_active
                ON devin_trial_sessions(account_id) WHERE state IN ('open','closing');
        """
        with self._transaction():
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and tables != _TABLES:
                raise TrialError("dedicated_trial_database_required")
            if tables:
                try:
                    prior = connection.execute(
                        "SELECT limits FROM devin_trial_control WHERE id=1"
                    ).fetchone()
                except sqlite3.DatabaseError:
                    raise TrialError("dedicated_trial_database_required") from None
                if prior is None or prior[0] != limits:
                    raise TrialError("trial_limits_changed")
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO devin_trial_control VALUES(1,0,'',0,?)", (limits,)
            )
            # A racing first constructor can win after the empty-schema check.
            if (
                connection.execute("SELECT limits FROM devin_trial_control WHERE id=1").fetchone()[
                    0
                ]
                != limits
            ):
                raise TrialError("trial_limits_changed")

    @contextlib.contextmanager
    def _transaction(self):
        if self.connection.in_transaction:
            raise TrialError("nested_trial_transaction")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise TrialError("trial_ledger_busy") from None
        try:
            # Keep schema admission and every subsequent write inside the same
            # serialized transaction; another initializer cannot add product
            # tables between the allowlist and our first DDL/write.
            tables = {
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables and tables != _TABLES:
                raise TrialError("dedicated_trial_database_required")
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _pause(self, reason):
        self.connection.execute(
            "UPDATE devin_trial_control SET paused=1,reason=? WHERE id=1", (reason,)
        )

    def _tick(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 10**11:
            raise TrialError("invalid_trial_clock")
        previous = self.connection.execute(
            "SELECT last_now FROM devin_trial_control WHERE id=1"
        ).fetchone()[0]
        if now < previous:
            self._pause("clock_regressed")
            now = previous
        self.connection.execute("UPDATE devin_trial_control SET last_now=? WHERE id=1", (now,))
        row = self.connection.execute(
            "SELECT trial_id,steps,expires_at,active_step,step_deadline "
            "FROM devin_trial_sessions WHERE state IN ('open','closing')"
        ).fetchone()
        if row and ((row[3] is not None and now >= row[4]) or now >= row[2]):
            self.connection.execute(
                "UPDATE devin_trial_sessions SET state=? WHERE trial_id=?",
                ("closing" if row[1] else "closed", row[0]),
            )
            if row[1]:
                self._pause("expired_worker_or_history_cleanup_required")
        return float(now)

    @staticmethod
    def _principal(principal):
        return (
            type(principal) is VerifiedAccount
            and principal.email_verified is True
            and isinstance(principal.account_id, str)
            and _ACCOUNT.fullmatch(principal.account_id) is not None
        )

    def _days(self, account_id, day):
        self.connection.execute(
            "INSERT OR IGNORE INTO devin_trial_account_days VALUES(?,?,0,0,0,0,0)",
            (account_id, day),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO devin_trial_global_days VALUES(?,0,0,0,0,0)", (day,)
        )
        user = self.connection.execute(
            "SELECT used_tokens,reserved_tokens,requests,runtime_seconds,leases "
            "FROM devin_trial_account_days WHERE account_id=? AND utc_day=?",
            (account_id, day),
        ).fetchone()
        total = self.connection.execute(
            "SELECT used_tokens,reserved_tokens,requests,runtime_seconds,leases "
            "FROM devin_trial_global_days WHERE utc_day=?",
            (day,),
        ).fetchone()
        return user, total

    def _gate(self):
        if not self.enabled:
            return "trial_disabled"
        if self.connection.execute("SELECT paused FROM devin_trial_control WHERE id=1").fetchone()[
            0
        ]:
            return "trial_paused"
        return None

    def _row(self, principal, lease):
        if (
            not self._principal(principal)
            or type(lease) is not Lease
            or not isinstance(lease.trial_id, str)
            or not _ID.fullmatch(lease.trial_id)
            or not isinstance(lease.secret, str)
            or not _ID.fullmatch(lease.secret)
        ):
            return None
        row = self.connection.execute(
            "SELECT " + ",".join(_COLUMNS) + " FROM devin_trial_sessions "
            "WHERE trial_id=? AND account_id=?",
            (lease.trial_id, principal.account_id),
        ).fetchone()
        if row:
            row = dict(zip(_COLUMNS, row))
            if (
                lease.history_namespace == row["history_namespace"]
                and lease.expires_at == row["expires_at"]
                and hmac.compare_digest(
                    row["secret_hash"], hashlib.sha256(lease.secret.encode()).hexdigest()
                )
            ):
                return row
        return None

    @staticmethod
    def _permit_matches(row, permit):
        return (
            row is not None
            and type(permit) is StepPermit
            and type(permit.deadline) in (int, float)
            and math.isfinite(permit.deadline)
            and type(permit.reserved_seconds) is int
            and type(permit.reserved_tokens) is int
            and row["trial_id"] == permit.trial_id
            and row["history_namespace"] == permit.history_namespace
            and row["active_step"] == permit.step_id
            and row["step_deadline"] == permit.deadline
            and row["active_seconds"] == permit.reserved_seconds
            and row["active_tokens"] == permit.reserved_tokens
        )

    def allowance(self, principal: VerifiedAccount) -> DailyAllowance:
        if not self._principal(principal):
            raise TrialError("verified_account_required")
        with self._transaction():
            now = self._tick()
            day = int(now // 86400)
            user, total = self._days(principal.account_id, day)
            return DailyAllowance(
                dt.datetime.fromtimestamp(day * 86400, dt.timezone.utc).date().isoformat(),
                float((day + 1) * 86400),
                self.daily_token_limit,
                user[0],
                user[1],
                max(0, self.daily_token_limit - user[0] - user[1]),
                max(0, self.global_daily_token_limit - total[0] - total[1]),
                max(0, DAILY_REQUESTS - user[2]),
                bool(
                    self.connection.execute(
                        "SELECT paused FROM devin_trial_control WHERE id=1"
                    ).fetchone()[0]
                ),
                self.enabled,
            )

    def admit(self, principal: VerifiedAccount, *, lease_seconds: int = MAX_LEASE_S) -> Decision:
        if not self._principal(principal):
            return Decision("verified_account_required")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_LEASE_S:
            return Decision("invalid_lease_seconds")
        with self._transaction():
            now = self._tick()
            if reason := self._gate():
                return Decision(reason)
            day = int(now // 86400)
            user, total = self._days(principal.account_id, day)
            if (
                user[0] + user[1] >= self.daily_token_limit
                or user[2] >= DAILY_REQUESTS
                or user[3] >= MAX_RUNTIME_S
                or user[4] >= DAILY_LEASES
            ):
                return Decision("daily_limit_reached")
            if (
                total[0] + total[1] >= self.global_daily_token_limit
                or total[2] >= GLOBAL_DAILY_REQUESTS
                or total[3] >= GLOBAL_DAILY_RUNTIME_S
                or total[4] >= GLOBAL_DAILY_LEASES
            ):
                return Decision("global_daily_limit_reached")
            if self.connection.execute(
                "SELECT 1 FROM devin_trial_sessions WHERE state IN ('open','closing')"
            ).fetchone():
                return Decision("trial_busy")
            lease = Lease(
                secrets.token_hex(32),
                secrets.token_hex(32),
                "trial-" + secrets.token_hex(32),
                min(now + lease_seconds, float((day + 1) * 86400)),
            )
            self.connection.execute(
                "INSERT INTO devin_trial_sessions"
                " VALUES(?,?,?,?,?,'open',0,0,NULL,NULL,NULL,?,NULL)",
                (
                    lease.trial_id,
                    principal.account_id,
                    hashlib.sha256(lease.secret.encode()).hexdigest(),
                    lease.history_namespace,
                    lease.expires_at,
                    day,
                ),
            )
            self.connection.execute(
                "UPDATE devin_trial_account_days SET leases=leases+1 WHERE"
                " account_id=? AND utc_day=?",
                (principal.account_id, day),
            )
            self.connection.execute(
                "UPDATE devin_trial_global_days SET leases=leases+1 WHERE utc_day=?", (day,)
            )
            return Decision("admitted", lease=lease)

    def start_step(
        self, principal: VerifiedAccount, lease: Lease, *, request: bytes, seconds: int = MAX_STEP_S
    ) -> Decision:
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
            if row["state"] != "open" or row["utc_day"] != int(now // 86400):
                return Decision("trial_closed")
            if row["active_step"] is not None:
                return Decision("step_busy")
            if row["steps"] >= MAX_STEPS or row["reserved_seconds"] >= MAX_RUNTIME_S:
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?",
                    (row["trial_id"],),
                )
                return Decision("trial_limit_reached")
            user, total = self._days(principal.account_id, row["utc_day"])
            tokens = min(
                self.daily_token_limit - user[0] - user[1],
                self.global_daily_token_limit - total[0] - total[1],
            )
            if tokens <= 0 or user[2] >= DAILY_REQUESTS:
                return Decision("daily_limit_reached")
            if total[2] >= GLOBAL_DAILY_REQUESTS:
                return Decision("global_daily_limit_reached")
            if (
                row["reserved_seconds"] + seconds > MAX_RUNTIME_S
                or user[3] + seconds > MAX_RUNTIME_S
                or total[3] + seconds > GLOBAL_DAILY_RUNTIME_S
            ):
                return Decision("runtime_reservation_limit")
            if now + seconds > row["expires_at"]:
                return Decision("lease_time_limit")
            permit = StepPermit(
                row["trial_id"],
                secrets.token_hex(32),
                row["history_namespace"],
                now + seconds,
                seconds,
                tokens,
            )
            self.connection.execute(
                "UPDATE devin_trial_sessions SET steps=steps+1,reserved_seconds=reserved_seconds+?,"
                "active_step=?,step_deadline=?,active_seconds=?,active_tokens=? WHERE trial_id=?",
                (seconds, permit.step_id, permit.deadline, seconds, tokens, row["trial_id"]),
            )
            self.connection.execute(
                "UPDATE devin_trial_account_days SET"
                " reserved_tokens=reserved_tokens+?,requests=requests+1,runtime_seconds"
                "=runtime_seconds+? WHERE account_id=? AND utc_day=?",
                (tokens, seconds, principal.account_id, row["utc_day"]),
            )
            self.connection.execute(
                "UPDATE devin_trial_global_days SET"
                " reserved_tokens=reserved_tokens+?,requests=requests+1,runtime_seconds"
                "=runtime_seconds+? WHERE utc_day=?",
                (tokens, seconds, row["utc_day"]),
            )
            return Decision("step_reserved", step=permit)

    def authorize_step(self, principal: VerifiedAccount, lease: Lease, permit: StepPermit) -> bool:
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            return (
                self._gate() is None
                and self._permit_matches(row, permit)
                and row["state"] == "open"
            )

    def finish_step(
        self,
        principal: VerifiedAccount,
        lease: Lease,
        permit: StepPermit,
        *,
        cleanup_confirmed: bool,
        usage: Optional[VerifiedUsage] = None,
    ) -> Decision:
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            if not self._permit_matches(row, permit):
                return Decision("invalid_step_receipt")
            if cleanup_confirmed is not True:
                self._pause("worker_cleanup_unconfirmed")
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?",
                    (row["trial_id"],),
                )
                return Decision("cleanup_unconfirmed")
            if (
                type(usage) is not VerifiedUsage
                or usage.complete is not True
                or type(usage.reported_tokens) is not int
                or not 0 <= usage.reported_tokens <= MAX_REPORTED_TOKENS
            ):
                self._pause("reported_usage_unaccounted")
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?",
                    (row["trial_id"],),
                )
                return Decision("usage_unaccounted")
            amount, reserved, day = usage.reported_tokens, row["active_tokens"], row["utc_day"]
            self.connection.execute(
                "UPDATE devin_trial_account_days SET"
                " used_tokens=used_tokens+?,reserved_tokens=reserved_tokens-? WHERE acc"
                "ount_id=? AND utc_day=?",
                (amount, reserved, principal.account_id, day),
            )
            self.connection.execute(
                "UPDATE devin_trial_global_days SET"
                " used_tokens=used_tokens+?,reserved_tokens=reserved_tokens-? WHERE utc_day=?",
                (amount, reserved, day),
            )
            self.connection.execute(
                "UPDATE devin_trial_sessions SET"
                " active_step=NULL,step_deadline=NULL,active_seconds=NULL,active_tokens"
                "=NULL WHERE trial_id=?",
                (row["trial_id"],),
            )
            if amount > reserved:
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?",
                    (row["trial_id"],),
                )
                return Decision("step_finished_over_allowance")
            return Decision("step_finished")

    def cancel(self, principal: VerifiedAccount, lease: Lease) -> Decision:
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            if row is None:
                return Decision("invalid_trial_lease")
            if row["state"] != "closed":
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?",
                    (row["trial_id"],),
                )
            return Decision("trial_cancelled")

    def close(
        self, principal: VerifiedAccount, lease: Lease, *, cleanup_confirmed: bool
    ) -> Decision:
        with self._transaction():
            self._tick()
            row = self._row(principal, lease)
            if row is None:
                return Decision("invalid_trial_lease")
            if row["state"] == "closed":
                return Decision("trial_closed")
            if cleanup_confirmed is not True:
                self._pause("trial_cleanup_unconfirmed")
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closing' WHERE trial_id=?",
                    (row["trial_id"],),
                )
                return Decision("cleanup_unconfirmed")
            if row["active_step"] is not None:
                return Decision("step_cleanup_required")
            self.connection.execute(
                "UPDATE devin_trial_sessions SET state='closed' WHERE trial_id=?",
                (row["trial_id"],),
            )
            return Decision("trial_closed")

    def pause(self) -> None:
        with self._transaction():
            self._tick()
            self._pause("operator_pause")
