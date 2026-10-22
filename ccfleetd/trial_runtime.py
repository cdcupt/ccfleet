"""Trusted public-trial admission. No slot ownership, wallet, or product DB writes."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import stat
import threading
import time
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from ccfleet_agent import devin_native, devin_relay

from . import devin_trial as ledger
from . import native_preflight
from .config import ConfigError
from .gateway_providers import native_client_request
from .gateway_transport import GatewayTransportError
from .trial_access import AccessKeys
from .trial_worker_transport import MODEL, TrialAttempt, protected_json

log = logging.getLogger(__name__)


class TrialUnavailable(ValueError):
    def __init__(self, code, status=503, *, request_error=None):
        self.code, self.status = code, status
        self.request_error = request_error
        super().__init__(code)


def principal(account):
    # Google addresses are verified at OAuth admission. Basic/admin identities
    # and arbitrary JSON/email strings cannot become trial principals.
    if not isinstance(account, dict) or not account.get("google_sub"):
        raise TrialUnavailable("verified_fleet_account_required", 403)
    return ledger.VerifiedAccount(account["id"], True)


def validate(cfg):
    if type(cfg.devin_trial_enabled) is not bool:
        raise ConfigError("Devin trial opt-in must be an explicit boolean")
    if cfg.devin_trial_enabled is not True:
        return False
    if cfg.broker_only:
        return False
    origin = urllib.parse.urlsplit(cfg.public_url)
    if (
        not cfg.google_ready
        or not cfg.cookie_secret
        or origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path
        or not all(
            os.path.isabs(value)
            for value in (
                cfg.devin_trial_directory,
                cfg.devin_trial_worker_file,
                cfg.devin_trial_ssh_key_file,
            )
        )
    ):
        raise ConfigError(
            "Devin trial requires Google sign-in, HTTPS and dedicated protected paths"
        )
    if Path(cfg.devin_trial_directory).resolve() == Path(cfg.db_path).resolve().parent:
        raise ConfigError("Devin trial requires a separate database directory")
    return True


def build(cfg, store):
    if not validate(cfg):
        return None  # Disabled is strictly no filesystem, credential or network work.
    return TrialRuntime(cfg, store)


class TrialRuntime:
    def __init__(self, cfg, store, *, transport=TrialAttempt, clock=time.time):
        self.cfg, self.store, self.clock, self.transport = cfg, store, clock, transport
        self.lock = threading.RLock()
        self.readiness_lock = threading.Lock()
        self.directory = Path(cfg.devin_trial_directory)
        info = self.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or self.directory.is_symlink()
        ):
            raise ConfigError("Devin trial directory must be an existing private owned directory")
        self.target, self.worker_sha = protected_json(cfg.devin_trial_worker_file)
        # Pre-create through protected exclusive descriptors; never follow a
        # product DB symlink or silently chmod an existing unrelated file.
        self.database_identities = {}
        for name in ("allowance.sqlite3", "access.sqlite3"):
            path = self.directory / name
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ConfigError("Devin trial database protection is invalid")
            self.database_identities[name] = (info.st_dev, info.st_ino)
        self.keys = AccessKeys(self.directory / "access.sqlite3", secret=cfg.cookie_secret,
                               clock=clock)
        with self._ledger():
            pass

    @contextlib.contextmanager
    def _ledger(self):
        with self.lock:
            path = self.directory / "allowance.sqlite3"
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != self.database_identities[path.name]
            ):
                raise ledger.TrialError("trial_database_guard")
            db = sqlite3.connect(path, timeout=1)
            try:
                yield ledger.TrialLedger(db, enabled=True, clock=self.clock)
            finally:
                db.close()

    def allowance(self, account):
        with self._ledger() as book:
            return asdict(book.allowance(principal(account)))

    def admit_api(self, token):
        key = self.keys.resolve(token)
        if key is None:
            raise TrialUnavailable("trial_key_expired_or_revoked", 401)
        who = principal(self.store.get_account(key["account_id"]))
        if not self.keys.take_request(who.account_id):
            raise TrialUnavailable("trial_request_rate_limit", 429)

    def create_key(self, account):
        # A key only identifies its owner; creating one never admits a model
        # turn. Keep setup/replacement available during operator review while
        # turn() continues to enforce the ledger's global execution pause.
        return self.keys.create(principal(account))

    def launch_key(self, account):
        return self.keys.launch_key(principal(account))

    def revoke(self, account):
        self.keys.revoke(principal(account).account_id)

    def _identity_active(self, key, who):
        try:
            account = self.store.get_account(key["account_id"])
            if principal(account) != who or not self.keys.active(key):
                return False
            if protected_json(self.cfg.devin_trial_worker_file)[1] != self.worker_sha:
                return False
            return True
        except Exception:
            return False

    def _active(self, key, who, lease, step):
        try:
            if not self._identity_active(key, who):
                return False
            with self._ledger() as book:
                return book.authorize_step(who, lease, step)
        except Exception:
            return False

    def turn(self, token, raw):
        key = self.keys.resolve(token)
        if key is None:
            raise TrialUnavailable("trial_key_expired_or_revoked", 401)
        who = principal(self.store.get_account(key["account_id"]))
        if not isinstance(raw, bytes) or not raw:
            raise TrialUnavailable("invalid_trial_request_framing", 400)
        if len(raw) > ledger.MAX_REQUEST_BYTES:
            raise TrialUnavailable("trial_context_too_large", 400)
        try:
            body = devin_native.mcp._decode(raw)
            body = native_client_request(body)
            clean = native_preflight.validate(body, request_models=[MODEL], effort="max")
        except native_preflight.RequestRejected as error:
            raise TrialUnavailable(error.code, error.status, request_error=error) from None
        except (ValueError, TypeError, RecursionError):
            raise TrialUnavailable("trial_requires_supported_opus_5_5_request", 400) from None
        forwarded = devin_native._encode(clean)
        # Reject an existing safety pause before contacting the worker. SSH
        # readiness itself is model-free and must not consume the execution
        # permit while the remote connection/policy is still being checked.
        with self._ledger() as book:
            if book.allowance(who).paused:
                raise TrialUnavailable("trial_paused", 429)
        attempt = self.transport(
            self.target, SimpleNamespace(gateway_ssh_key_path=self.cfg.devin_trial_ssh_key_file)
        )
        readiness_locked = False
        try:
            prepare = getattr(attempt, "prepare", None)
            if prepare is not None:
                readiness_locked = self.readiness_lock.acquire(blocking=False)
                if not readiness_locked:
                    raise TrialUnavailable("trial_busy", 429)
                prepare(lambda: self._identity_active(key, who))
            with self._ledger() as book:
                decision = book.admit(who, lease_seconds=180)
                if decision.lease is None:
                    raise TrialUnavailable(decision.code, 429)
                lease = decision.lease
                decision = book.start_step(who, lease, request=forwarded, seconds=ledger.MAX_STEP_S)
                if decision.step is None:
                    book.close(who, lease, cleanup_confirmed=True)
                    raise TrialUnavailable(decision.code, 429)
                step = decision.step
        except Exception as error:
            try:
                attempt.close()
            finally:
                if readiness_locked:
                    self.readiness_lock.release()
            if isinstance(error, (TrialUnavailable, ledger.TrialError)):
                raise
            status = error.status if isinstance(error, GatewayTransportError) else None
            reason = {
                403: "authorization_changed",
                429: "worker_busy",
                503: "worker_not_ready",
                504: "readiness_timeout",
            }.get(status, "transport_or_policy_failed")
            # Only fixed phase reasons, never exception text, keys or request
            # values. Readiness has not written a model request frame.
            log.warning("Devin trial readiness failed: reason=%s", reason)
            if status == 429:
                raise TrialUnavailable("trial_busy", 429) from None
            raise TrialUnavailable("trial_worker_unavailable") from None
        cleanup, usage, result = False, None, None
        try:
            result = attempt.perform(forwarded, lambda: self._active(key, who, lease, step))
            cleanup, usage = self._receipt(result)
            if type(usage) is ledger.VerifiedCacheReadUnmeasured:
                try:
                    self._message(result, clean)
                except Exception:
                    usage = None

        except Exception:
            # Before the request-frame write no native turn exists; after it,
            # uncertainty must retain reservation and latch operator review.
            if not attempt.model_started:
                cleanup, usage = True, ledger.VerifiedUsage(0, True)
        finally:
            try:
                attempt.close()
                with self._ledger() as book:
                    if attempt.model_started is False and cleanup is True:
                        settled = book.finish_unsent(
                            who, lease, step, model_started=False, cleanup_confirmed=True
                        )
                    else:
                        settled = book.finish_step(
                            who, lease, step, cleanup_confirmed=cleanup, usage=usage
                        )
                    if settled.code in {
                        "step_finished",
                        "step_finished_over_allowance",
                        "step_finished_unmeasured",
                        "step_not_sent",
                    }:
                        book.close(who, lease, cleanup_confirmed=True)
            finally:
                if readiness_locked:
                    self.readiness_lock.release()
        if result is None or usage is None or not cleanup:
            raise TrialUnavailable("trial_result_unconfirmed_do_not_retry")
        if result["error"] is not None:
            raise TrialUnavailable("trial_native_request_failed")
        # A revoked caller receives no buffered reply even if execution settled.
        if not self.keys.active(key):
            raise TrialUnavailable("trial_key_revoked", 403)
        return self._message(result, clean), clean.get("stream", False)

    @staticmethod
    def _message(result, clean):
        message = result["message"]
        fields = {"id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage"}
        expected_usage = {
            "input_tokens": result["usage"]["input_tokens"],
            "output_tokens": result["usage"]["output_tokens"],
            "cache_read_input_tokens": result["usage"]["cache_read_tokens"],
            "cache_creation_input_tokens": result["usage"]["cache_creation_tokens"],
        }
        expected_usage = {key: value for key, value in expected_usage.items() if value is not None}
        if (
            not isinstance(message, dict)
            or set(message) != fields
            or message["usage"] != expected_usage
            or message.get("type") != "message"
            or message.get("model") != MODEL
            or message.get("role") != "assistant"
            or message.get("stop_sequence") is not None
            or len(json.dumps(message).encode()) > 1024 * 1024
        ):
            raise TrialUnavailable("trial_result_invalid")
        try:
            safe = devin_relay._message(
                {
                    "mode": "native_agent_compatibility",
                    "contract": "native-agent-v1",
                    "native_cleanup_confirmed": True,
                    "prompt_requests": 1,
                    "raw_Messages_semantic_parity": False,
                    "model": MODEL,
                    "content": message["content"],
                    "stop_reason": message["stop_reason"],
                    "accounting": {"usage": result["usage"]},
                },
                SimpleNamespace(data={"model_uid": MODEL}),
                clean,
            )
        except (ValueError, TypeError, KeyError):
            raise TrialUnavailable("trial_result_invalid") from None
        return safe

    @staticmethod
    def _receipt(value):
        if (
            not isinstance(value, dict)
            or set(value)
            != {"version", "cleanup_confirmed", "metering", "usage", "message", "error"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["cleanup_confirmed"]) is not bool
            or value["error"] not in {None, "native_request_failed"}
        ):
            raise TrialUnavailable("trial_receipt_invalid")
        counts = value["usage"]
        if (
            value.get("metering") == "cache_read_unmeasured"
            and value["cleanup_confirmed"] is True
            and value["error"] is None
            and isinstance(counts, dict)
            and set(counts)
            == {"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"}
            and counts["cache_read_tokens"] is None
            and all(
                type(item) is int and 0 <= item <= 10**12
                for key, item in counts.items()
                if key != "cache_read_tokens"
            )
            and sum(item for item in counts.values() if item is not None)
            <= ledger.MAX_REPORTED_TOKENS
        ):
            known = sum(item for item in counts.values() if item is not None)
            return True, ledger.VerifiedCacheReadUnmeasured(known, True)
        if value.get("metering") != "complete":
            return value["cleanup_confirmed"], None

        if (
            not isinstance(counts, dict)
            or set(counts)
            != {"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"}
            or any(type(n) is not int or not 0 <= n <= 10**12 for n in counts.values())
            or sum(counts.values()) > ledger.MAX_REPORTED_TOKENS
        ):
            return value["cleanup_confirmed"], None
        return value["cleanup_confirmed"], ledger.VerifiedUsage(sum(counts.values()), True)
