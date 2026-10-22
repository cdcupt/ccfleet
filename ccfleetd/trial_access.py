"""Trial-only hashed keys with authenticated owner retrieval, never provider credentials."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from pathlib import Path

from .devin_trial import TrialError, VerifiedAccount

PREFIX = "cct_"
TTL_S = 30 * 86400
_RECOVERABLE_ID = re.compile(r"v1_[0-9a-f]{32}\Z")
_DERIVATION_DOMAIN = b"ccfleet/trial/launch-token/derive-v1"


class AccessKeys:
    def __init__(self, path: Path, *, secret: str, clock=time.time):
        if not isinstance(secret, str) or not secret:
            raise TrialError("trial_key_derivation_secret_required")
        self.path, self.clock = path, clock
        # Domain-separated from browser-cookie signatures and other HMAC uses.
        # The deployment secret stays outside the access database and backups.
        self._derivation_key = hmac.new(secret.encode(), _DERIVATION_DOMAIN,
                                        hashlib.sha256).digest()
        self.identity = (path.stat().st_dev, path.stat().st_ino)
        self.initializing = True
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables - {"trial_access", "trial_api_rate"}:
                raise TrialError("dedicated_trial_key_database_required")
            db.execute(
                "CREATE TABLE IF NOT EXISTS trial_access (id TEXT PRIMARY KEY, "
                "account_id TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, "
                "created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked INTEGER "
                "NOT NULL CHECK(revoked IN (0,1)))"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_trial_key ON "
                "trial_access(account_id) WHERE revoked=0"
            )

            db.execute(
                "CREATE TABLE IF NOT EXISTS trial_api_rate (account_id TEXT PRIMARY KEY, "
                "tokens REAL NOT NULL CHECK(tokens>=0 AND tokens<=3), "
                "sampled_at REAL NOT NULL)"
            )
        self.initializing = False

    @contextlib.contextmanager
    def _db(self):
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or (info.st_dev, info.st_ino) != self.identity
        ):
            raise TrialError("trial_key_database_guard")
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            with connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if (
                    tables - {"trial_access", "trial_api_rate"}
                    or not self.initializing
                    and tables != {"trial_access", "trial_api_rate"}
                ):
                    raise TrialError("dedicated_trial_key_database_required")
                yield connection
        finally:
            connection.close()

    def take_request(self, account_id):
        """Persistent account bucket: burst3, refill40/minute; key rotation is irrelevant."""
        now = self._now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT tokens,sampled_at FROM trial_api_rate WHERE account_id=?", (account_id,)
            ).fetchone()
            if row and now < row[1]:
                return False
            tokens = min(3.0, row[0] + (now - row[1]) * (40 / 60)) if row else 3.0
            accepted = tokens >= 1
            db.execute(
                "INSERT OR REPLACE INTO trial_api_rate VALUES(?,?,?)",
                (account_id, tokens - 1 if accepted else tokens, now),
            )
            return accepted

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 10**11:
            raise TrialError("invalid_trial_clock")
        return now

    def create(self, principal: VerifiedAccount):
        if type(principal) is not VerifiedAccount or principal.email_verified is not True:
            raise TrialError("verified_account_required")
        now, identifier = self._now(), "v1_" + secrets.token_hex(16)
        token = self._derived_token(principal.account_id, identifier)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COUNT(*) FROM trial_access WHERE account_id=? AND created_at>=?",
                (principal.account_id, int(now // 86400) * 86400),
            ).fetchone()[0]
            if count >= 32:
                raise TrialError("trial_key_creation_limit")
            db.execute(
                "UPDATE trial_access SET revoked=1 WHERE account_id=?", (principal.account_id,)
            )
            db.execute(
                "INSERT INTO trial_access VALUES(?,?,?,?,?,0)",
                (
                    identifier,
                    principal.account_id,
                    hashlib.sha256(token.encode()).hexdigest(),
                    now,
                    now + TTL_S,
                ),
            )
        return token

    def _derived_token(self, account_id, identifier):
        message = json.dumps([account_id, identifier], separators=(",", ":")).encode()
        return PREFIX + hmac.new(self._derivation_key, message, hashlib.sha256).hexdigest()

    def launch_key(self, principal: VerifiedAccount):
        """Read the owner's active key without rotation or any allowance/rate work.

        Legacy random keys cannot be recovered. Changing the deployment secret
        also makes retrieval unavailable, while the stored hash keeps existing
        API commands valid until explicit replacement, revocation or expiry.
        """
        if type(principal) is not VerifiedAccount or principal.email_verified is not True:
            raise TrialError("verified_account_required")
        with self._db() as db:
            row = db.execute(
                "SELECT id,token_sha256 FROM trial_access WHERE account_id=? "
                "AND revoked=0 AND expires_at>?",
                (principal.account_id, self._now()),
            ).fetchone()
        if (row is None or not isinstance(row[0], str)
                or not _RECOVERABLE_ID.fullmatch(row[0])
                or not isinstance(row[1], str) or not re.fullmatch(r"[0-9a-f]{64}", row[1])):
            return ""
        candidate = self._derived_token(principal.account_id, row[0])
        digest = hashlib.sha256(candidate.encode()).hexdigest()
        return candidate if hmac.compare_digest(digest, row[1]) else ""

    def resolve(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"cct_[0-9a-f]{64}", token):
            return None
        now = self._now()
        with self._db() as db:
            row = db.execute(
                "SELECT id,account_id,expires_at FROM trial_access "
                "WHERE token_sha256=? AND revoked=0 AND expires_at>?",
                (hashlib.sha256(token.encode()).hexdigest(), now),
            ).fetchone()
        return {"id": row[0], "account_id": row[1], "expires_at": row[2]} if row else None

    def active(self, key):
        now = self._now()
        with self._db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM trial_access WHERE id=? AND account_id=? "
                    "AND revoked=0 AND expires_at>?",
                    (key["id"], key["account_id"], now),
                ).fetchone()
                is not None
            )

    def revoke(self, account_id):
        with self._db() as db:
            db.execute("UPDATE trial_access SET revoked=1 WHERE account_id=?", (account_id,))
