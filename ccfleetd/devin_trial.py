"""Dedicated DAILY reported-token admission ledger, never a provider adapter.

VerifiedAccount/VerifiedUsage come only from trusted server/worker boundaries.
No provider credential or agent execution lives in this module. Reported usage
arrives after the turn; an in-flight turn can exceed
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
MAX_RUNTIME_S = 180
MAX_STEP_S = 160
MAX_REQUEST_BYTES = 192 * 1024
TURN_RESERVATION_TOKENS = 20_000
DAILY_REQUESTS = 20
GLOBAL_DAILY_REQUESTS = 20
# A longer individual turn does not increase either daily runtime allowance.
DAILY_RUNTIME_S = 1200
GLOBAL_DAILY_RUNTIME_S = 1200
DAILY_LEASES = 20
GLOBAL_DAILY_LEASES = 20
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
    "devin_trial_unmeasured",
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
_SESSION_SCHEMA = """
    CREATE TABLE IF NOT EXISTS devin_trial_sessions (
        trial_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, secret_hash TEXT NOT NULL,
        history_namespace TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','closing','closed')),
        steps INTEGER NOT NULL CHECK(steps BETWEEN 0 AND 5),
        reserved_seconds INTEGER NOT NULL CHECK(reserved_seconds BETWEEN 0 AND 180),
        active_step TEXT, step_deadline REAL,
        active_seconds INTEGER CHECK(active_seconds BETWEEN 1 AND 160),
        utc_day INTEGER NOT NULL, active_tokens INTEGER CHECK(active_tokens>0))
"""
_LEGACY_SESSION_SCHEMA = _SESSION_SCHEMA.replace(
    "reserved_seconds BETWEEN 0 AND 180", "reserved_seconds BETWEEN 0 AND 120"
).replace("active_seconds BETWEEN 1 AND 160", "active_seconds BETWEEN 1 AND 60")
_SESSION_INDEXES = {
    "devin_trial_one_active": """CREATE UNIQUE INDEX IF NOT EXISTS devin_trial_one_active
        ON devin_trial_sessions ((1)) WHERE state IN ('open','closing')""",
    "devin_trial_one_account_active": """CREATE UNIQUE INDEX IF NOT EXISTS
        devin_trial_one_account_active ON devin_trial_sessions(account_id)
        WHERE state IN ('open','closing')""",
}


def _normalized_sql(value):
    return " ".join(value.split()).replace(" IF NOT EXISTS ", " ").rstrip(";")


class TrialError(ValueError):
    """Local configuration/ledger error, never provider or credential text."""


@dataclass(frozen=True)
class VerifiedAccount:
    """Trusted Fleet identity boundary; never deserialize this from visitor JSON."""

    account_id: str
    email_verified: bool = False


@dataclass(frozen=True)
class VerifiedUsage:
    """Trusted supervisor's COMPLETE final measured usage, not a client claim."""

    reported_tokens: int
    complete: bool = False


@dataclass(frozen=True)
class VerifiedCacheReadUnmeasured:
    """Trusted CLEAN completed turn with only the native cache-read meter absent.

    The worker must prove the served model, complete other three integer
    categories, valid reply, child cleanup and entire artifact removal. This is
    never a complete actual-token receipt or an authorization from visitor JSON.
    """

    known_tokens: int
    verified_clean_reply: bool = False


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
    unmeasured_reserved_tokens: int = 0
    partial_reported_tokens: int = 0


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
    at most 20,000 remaining user/global allowance units.
    Complete trusted usage and cleanup settle actual tokens including overshoot.
    Only a verified clean cache-read-missing reply may retain a conservative
    unmeasured hold and close; all other unknown usage/cleanup latches pause.
    No automatic unpause/reset exists.
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
                4,
                TURN_RESERVATION_TOKENS,
                daily_token_limit,
                global_daily_token_limit,
                DAILY_REQUESTS,
                GLOBAL_DAILY_REQUESTS,
                DAILY_RUNTIME_S,
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
            CREATE TABLE IF NOT EXISTS devin_trial_unmeasured (
                step_id TEXT PRIMARY KEY, trial_id TEXT NOT NULL, account_id TEXT NOT NULL,
                utc_day INTEGER NOT NULL, held_tokens INTEGER NOT NULL CHECK(held_tokens>0),
                known_tokens INTEGER NOT NULL CHECK(known_tokens>=0), settled_at REAL NOT NULL);
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
                self._migrate_session_limits()
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(_SESSION_SCHEMA)
            for statement in _SESSION_INDEXES.values():
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

    def _migrate_session_limits(self):
        """Widen only the known time constraints; never rewrite incident state.

        This runs inside the constructor's write transaction. An existing
        deadline/reservation retains its original duration; only later turns
        can use the new bounds. The daily limits record remains unchanged.
        """
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='devin_trial_sessions'"
        ).fetchone()
        actual = _normalized_sql(row[0]) if row and isinstance(row[0], str) else None
        if actual == _normalized_sql(_SESSION_SCHEMA):
            return
        if actual != _normalized_sql(_LEGACY_SESSION_SCHEMA):
            raise TrialError("trial_schema_changed")
        indexes = dict(self.connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='devin_trial_sessions' AND sql IS NOT NULL"
        ))
        if (
            {name: _normalized_sql(sql) for name, sql in indexes.items()}
            != {name: _normalized_sql(sql) for name, sql in _SESSION_INDEXES.items()}
            or self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1"
            ).fetchone()
            or any(self.connection.execute(f"PRAGMA foreign_key_list({name})").fetchone()
                   for name in _TABLES)
        ):
            raise TrialError("trial_schema_changed")
        self.connection.execute(
            "ALTER TABLE devin_trial_sessions RENAME TO devin_trial_sessions_previous"
        )
        self.connection.execute(_SESSION_SCHEMA)
        columns = ",".join(("trial_id", "account_id", *_COLUMNS[1:]))
        self.connection.execute(
            f"INSERT INTO devin_trial_sessions({columns}) "
            f"SELECT {columns} FROM devin_trial_sessions_previous"
        )
        self.connection.execute("DROP TABLE devin_trial_sessions_previous")

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
        # Keep the original incident cause. Repeated allowance/authorization
        # polls of an unresolved expired session must not replace it with a
        # later deadline symptom and hide what needs operator review.
        self.connection.execute(
            "UPDATE devin_trial_control SET paused=1,reason=? WHERE id=1 AND paused=0", (reason,)
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
                False,
                *self.connection.execute(
                    "SELECT COALESCE(SUM(held_tokens),0), "
                    "COALESCE(SUM(known_tokens),0) FROM devin_trial_unmeasured "
                    "WHERE account_id=? AND utc_day=?",
                    (principal.account_id, day),
                ).fetchone(),
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
                or user[3] >= DAILY_RUNTIME_S
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
                TURN_RESERVATION_TOKENS,
                self.daily_token_limit - user[0] - user[1],
                self.global_daily_token_limit - total[0] - total[1],
            )
            if tokens <= 0 or user[2] >= DAILY_REQUESTS:
                return Decision("daily_limit_reached")
            if total[2] >= GLOBAL_DAILY_REQUESTS:
                return Decision("global_daily_limit_reached")
            if (
                row["reserved_seconds"] + seconds > MAX_RUNTIME_S
                or user[3] + seconds > DAILY_RUNTIME_S
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
        usage: Optional[object] = None,
    ) -> Decision:
        with self._transaction():
            instant = self._tick()
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
            if type(usage) is VerifiedCacheReadUnmeasured and (
                usage.verified_clean_reply is True
                and type(usage.known_tokens) is int
                and 0 <= usage.known_tokens <= MAX_REPORTED_TOKENS
            ):
                reserved = row["active_tokens"]
                held = max(reserved, usage.known_tokens)
                extra = held - reserved
                self.connection.execute(
                    "UPDATE devin_trial_account_days SET "
                    "reserved_tokens=reserved_tokens+? WHERE account_id=? AND utc_day=?",
                    (extra, principal.account_id, row["utc_day"]),
                )
                self.connection.execute(
                    "UPDATE devin_trial_global_days SET "
                    "reserved_tokens=reserved_tokens+? WHERE utc_day=?",
                    (extra, row["utc_day"]),
                )
                self.connection.execute(
                    "INSERT INTO devin_trial_unmeasured VALUES(?,?,?,?,?,?,?)",
                    (
                        permit.step_id,
                        row["trial_id"],
                        principal.account_id,
                        row["utc_day"],
                        held,
                        usage.known_tokens,
                        instant,
                    ),
                )
                # Durable accounting is now complete under the conservative
                # policy, but actual cache-read tokens remain explicitly unknown.
                # Close the turn before releasing the global worker; no replay
                # can settle it again and no timer/day rollover deletes its hold.
                self.connection.execute(
                    "UPDATE devin_trial_sessions SET state='closed',"
                    "active_step=NULL,step_deadline=NULL,active_seconds=NULL,active_tokens=NULL "
                    "WHERE trial_id=?",
                    (row["trial_id"],),
                )
                return Decision("step_finished_unmeasured")
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

    def finish_unsent(
        self,
        principal: VerifiedAccount,
        lease: Lease,
        permit: StepPermit,
        *,
        model_started: bool,
        cleanup_confirmed: bool,
    ) -> Decision:
        """Trusted transport proof only: no request-frame write ever began.

        Unlike uncertain sent work, a failed model-free SSH readiness check must
        not consume the global twenty model-turn/time budget. The closed row is
        retained; per-account API throttling separately bounds repeated probes.
        """
        with self._transaction():
            row = self._row(principal, lease)
            if (
                model_started is not False
                or cleanup_confirmed is not True
                or not self._permit_matches(row, permit)
                or row["steps"] != 1
            ):
                return Decision("invalid_unsent_receipt")
            self.connection.execute(
                "UPDATE devin_trial_account_days SET reserved_tokens="
                "reserved_tokens-?,requests=requests-1,runtime_seconds=runtime_seconds-?,"
                "leases=leases-1 WHERE account_id=? AND utc_day=?",
                (row["active_tokens"], row["active_seconds"], principal.account_id, row["utc_day"]),
            )
            self.connection.execute(
                "UPDATE devin_trial_global_days SET reserved_tokens="
                "reserved_tokens-?,requests=requests-1,runtime_seconds=runtime_seconds-?,"
                "leases=leases-1 WHERE utc_day=?",
                (row["active_tokens"], row["active_seconds"], row["utc_day"]),
            )
            self.connection.execute(
                "UPDATE devin_trial_sessions SET state='closed',"
                "active_step=NULL,step_deadline=NULL,active_seconds=NULL,active_tokens=NULL "
                "WHERE trial_id=?",
                (row["trial_id"],),
            )
            self._tick()  # Close the proven unsent work before expiry processing.
            return Decision("step_not_sent")

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
