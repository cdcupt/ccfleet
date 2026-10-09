"""Non-secret credential maintenance facts shared by validation and presentation."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

ACCESS_MARGIN_S = 30  # The slot relay refuses credentials this close to expiry.
RENEWAL_WINDOW_S = 10 * 60
LOGIN_WARNING_S = 3 * 86400
MAX_INSTANT = 1e11
RENEWAL_STATES = frozenset({"current", "needed", "renewed", "retrying", "blocked"})
RENEWAL_REASONS = frozenset({
    "account_unbound", "account_transition", "sign_in_pending", "credential_unavailable",
    "expiry_unknown", "native_refresh_unconfirmed", "maintenance_busy",
    "refresh_unavailable", "refresh_expired", "native_login_expired", "native_auth_rejected",
    "native_network_error", "native_rate_limited", "native_timeout", "native_launch_failed",
    "native_probe_busy", "native_refresh_not_due", "native_auth_source", "native_extensions",
})
TRANSITION_REASONS = frozenset({"account_transition", "sign_in_pending", "account_unbound"})
TERMINAL_REASONS = frozenset({"refresh_unavailable", "refresh_expired",
                              "native_login_expired", "native_auth_rejected"})
NATIVE_LOGIN_FAILURES = frozenset({"native_login_expired", "native_auth_rejected"})
NATIVE_CONTEXT_REASONS = frozenset({"native_auth_source", "native_extensions"})
TRANSIENT_REASONS = frozenset({"native_network_error", "native_rate_limited", "native_timeout",
                               "native_launch_failed", "native_probe_busy", "maintenance_busy"})
RENEWAL_OUTCOMES = frozenset({
    "native_refreshed", "native_refresh_not_due", "native_auth_rejected", "native_login_expired",
    "native_network_error", "native_rate_limited", "native_timeout", "native_launch_failed",
    "native_probe_busy", "native_refresh_unconfirmed", "native_auth_source", "native_extensions",
})
RENEWAL_WARNING_CODES = (TERMINAL_REASONS | TRANSIENT_REASONS
                         | {"login_expiring", "native_renewal_pending", "renewal_due"})


def instant(value: Any) -> Optional[float]:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value < MAX_INSTANT or not math.isfinite(value)):
        return None
    return float(value)


def access_expiry(credentials: Mapping[str, Any]) -> Optional[float]:
    value = credentials.get("expires_at")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value < MAX_INSTANT * 1000):
        return None
    return instant(value / 1000)


def renewal_report(value: Any) -> dict[str, Any]:
    """Allowlist enums/timestamps only; never accept a raw error or native output."""
    if not isinstance(value, Mapping):
        return {}
    state, checked = value.get("state"), instant(value.get("checked_at"))
    if not isinstance(state, str) or state not in RENEWAL_STATES or checked is None:
        return {}
    result: dict[str, Any] = {"state": state, "checked_at": checked}
    for field in ("last_attempt_at", "next_attempt_at", "last_success_at"):
        at = instant(value.get(field))
        if at is not None:
            result[field] = at
    reason = value.get("reason")
    if isinstance(reason, str) and reason in RENEWAL_REASONS:
        result["reason"] = reason
    outcome = value.get("outcome")
    if isinstance(outcome, str) and outcome in RENEWAL_OUTCOMES:
        result["outcome"] = outcome
    if isinstance(value.get("probe_started"), bool):
        result["probe_started"] = value["probe_started"]
    return result


def sign_in_reason(credentials: Mapping[str, Any], now: float) -> Optional[str]:
    """Explicit renewal failure evidence; access expiry alone is never revocation.

    Capability facts warn about continued access. A reported unexpired access
    credential does not prove provider acceptance. Explicit native login failure
    is distinct from capability-only warnings. Legacy missing fields establish nothing.
    """
    renewal = renewal_report(credentials.get("renewal"))
    for field in ("reason", "outcome"):
        if renewal.get(field) in TERMINAL_REASONS:
            return renewal[field]
    refresh_expiry = instant(credentials.get("refresh_expires_at"))
    if refresh_expiry is not None and refresh_expiry <= now:
        return "refresh_expired"
    if credentials.get("refresh_available") is False:
        return "refresh_unavailable"
    return None


def renewal_warning(credentials: Mapping[str, Any], now: float) -> Optional[str]:
    """A fixed warning code, independently of whether current access works."""
    terminal = sign_in_reason(credentials, now)
    if terminal:
        return terminal
    refresh_expiry = instant(credentials.get("refresh_expires_at"))
    if refresh_expiry is not None and refresh_expiry <= now + LOGIN_WARNING_S:
        return "login_expiring"
    renewal = renewal_report(credentials.get("renewal"))
    if renewal.get("state") in {"needed", "retrying", "blocked"}:
        reason = renewal.get("reason")
        if reason in TRANSIENT_REASONS:
            return reason
        if reason in NATIVE_CONTEXT_REASONS:
            # Keep the existing device protocol's warning enum; detailed fixed
            # operator guidance is carried by the additive compatibility record.
            return "native_renewal_pending"
        if renewal.get("state") in {"needed", "retrying"}:
            return "native_renewal_pending"
    return None


def recovery_guidance(credentials: Mapping[str, Any], now: float) -> str:
    """Fixed recovery copy only; native output and tokens never enter this channel."""
    if sign_in_reason(credentials, now):
        return "A fresh Claude sign-in is required; automatic renewal cannot recover this login."
    renewal = renewal_report(credentials.get("renewal"))
    if renewal.get("reason") in NATIVE_CONTEXT_REASONS:
        return ("Native maintenance configuration needs operator review. "
                "Your computer pairing and local history are kept.")
    if renewal.get("reason") in TRANSIENT_REASONS:
        return "A temporary native renewal failure is being retried automatically."
    if renewal.get("state") in {"needed", "retrying"}:
        return "Native renewal is not yet confirmed; maintenance will retry automatically."
    return ("Access renewal has not been verified. Access expiry alone does not mean "
            "a revoked login.")


def fresh_observation(credentials: Any, heard: Any, now: float, max_age: float,
                      listening_since: Optional[float] = None) -> bool:
    """A cached heartbeat is not a fresh per-slot credential observation."""
    at = instant(heard)
    if not isinstance(credentials, Mapping) or at is None:
        return False
    observed = renewal_report(credentials.get("renewal")).get("checked_at", at)
    if (now - min(at, observed) > max_age or min(at, observed) > now + 60
            or listening_since and min(at, observed) < listening_since):
        return False
    return (isinstance(credentials.get("present"), bool)
            or isinstance(credentials.get("logged_in"), bool)
            or access_expiry(credentials) is not None
            or bool(renewal_report(credentials.get("renewal"))))
