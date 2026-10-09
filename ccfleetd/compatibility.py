"""Attributed, content-free hosted upgrade observations and fixed UI copy."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

from ccfleet_agent.compatibility import CHECKS, report

LABELS = {
    "passed": "Update checks passed",
    "pending": "Update checks pending",
    "failed": "Update checks need attention",
    "blocked": "Update checks waiting",
}
OPERATOR_REVIEW_REASONS = frozenset({"native_auth_source", "native_extensions"})
DETAILS = {
    "native_unavailable": "The native client is unavailable. Contact your operator.",
    "native_version_unknown": "The installed client version could not be verified.",
    "native_version_changed": "The installed version changed. Validation will run again.",
    "native_timeout": "A bounded client check timed out. Maintenance will retry.",
    "native_interface_changed": "The updated client interface needs operator review.",
    "native_auth_source": "Native authentication settings do not match the slot login. "
                          "Contact your operator.",
    "native_extensions": "Hosted native extensions need operator review before maintenance checks.",
    "usage_pending": "Waiting for a fresh native usage check.",
    "usage_unavailable": "Native usage validation is unconfirmed. Maintenance will retry.",
    "relay_unavailable": "The installed relay needs operator review.",
    "relay_protocol_mismatch": "Relay protocol validation failed. Contact your operator.",
    "relay_tls_policy": "Encrypted transport validation failed. Contact your operator.",
    "account_unbound": "Waiting for the slot's account binding.",
    "account_transition": "Waiting for account maintenance to finish.",
    "sign_in_pending": "Waiting for sign-in to finish.",
    "credential_unavailable": "Waiting for usable sign-in credentials.",
    "probe_busy": "Another maintenance check is running. Validation will retry.",
    "check_interrupted": "Validation was interrupted. Maintenance will retry.",
}


def clean(value: Any, now: float) -> dict[str, Any]:
    """Allowlisted records only; reject future checks and inconsistent success."""
    out = report(value)
    if not out or out["checked_at"] > now + 60:
        return {}
    for key in ("last_success_at", "usage_observed_at"):
        if out.get(key, 0) > now + 60:
            return {}
    if (out["state"] == "passed"
            and any(out.get("checks", {}).get(key) is not True for key in CHECKS)):
        return {}
    return out


def observation(raw: Any, claude: Any, credentials: Any, *, heard: Any, now: float,
                max_age: float, since: float = 0,
                listening_since: Optional[float] = None) -> dict[str, Any]:
    """A cached check remains attributed while the current heartbeat agrees.

    checked_at is the actual test time, not a timestamp refreshed on every beat.
    Nothing here proves provider acceptance or credentials' future validity.
    """
    if (type(heard) not in (int, float) or not math.isfinite(heard)
            or not now - max_age <= heard <= now + 60
            or listening_since is not None and heard < listening_since):
        return {}
    out = clean(raw, now)
    if not out or out["checked_at"] < since:
        return {}
    native = claude.get("version") if isinstance(claude, Mapping) else None
    if out.get("native_version") != native:
        return {}
    credentials = credentials if isinstance(credentials, Mapping) else {}
    account, bound = credentials.get("account_fp"), credentials.get("bound_fp")
    if account and out.get("account_fp") and out["account_fp"] != account:
        return {}
    if out["state"] == "passed" and (not account or account != bound
                                     or out.get("account_fp") != bound
                                     or out.get("usage_observed_at", 0) < since):
        return {}
    return out


def public(value: Mapping[str, Any]) -> dict[str, Any]:
    """Device/support surfaces never expose the account or runtime digests."""
    return {key: value[key] for key in (
        "state", "reason", "native_version", "checked_at", "next_check_at",
        "last_success_at", "usage_observed_at", "checks") if key in value}


def description(value: Mapping[str, Any]) -> str:
    if value.get("state") == "passed":
        return "Checks passed for the reported client version and account."
    return DETAILS.get(value.get("reason"), "Waiting for confirmed update validation.")
