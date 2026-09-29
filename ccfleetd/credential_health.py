"""Non-secret credential maintenance facts shared by validation and presentation."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

ACCESS_MARGIN_S = 30  # The slot relay refuses credentials this close to expiry.
RENEWAL_WINDOW_S = 10 * 60
MAX_INSTANT = 1e11
RENEWAL_STATES = frozenset({"current", "needed", "renewed", "retrying", "blocked"})
RENEWAL_REASONS = frozenset({
    "account_unbound", "account_transition", "sign_in_pending", "credential_unavailable",
    "expiry_unknown", "native_refresh_unconfirmed", "maintenance_busy",
})
TRANSITION_REASONS = frozenset({"account_transition", "sign_in_pending", "account_unbound"})


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
    return result


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
