"""Small, content-free account-health response for one authenticated device."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Optional

from . import compatibility
from .credential_health import (
    ACCESS_MARGIN_S,
    NATIVE_LOGIN_FAILURES,
    RENEWAL_WARNING_CODES,
    RENEWAL_WINDOW_S,
    TRANSITION_REASONS,
    access_expiry,
    fresh_observation,
    renewal_report,
    renewal_warning,
    sign_in_reason,
)
from .heartbeat import relay_report

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9]+)?\Z")


def quota_observation(quota: Any, slot: Mapping[str, Any], now: float) -> dict[str, Any]:
    """Only attributed numeric cached readings; no account IDs or reset text."""
    if not isinstance(quota, Mapping):
        return {}
    checked = quota.get("checked_at")
    since = max((value for value in (slot.get("claimed_at"), slot.get("account_switched_at"))
                 if type(value) in (int, float) and math.isfinite(value)), default=0)
    if (type(checked) not in (int, float) or not math.isfinite(checked)
            or not 0 < checked <= now + 60 or checked < since):
        return {}
    clean: dict[str, Any] = {}
    for name in ("session", "week"):
        window = quota.get(name)
        if not isinstance(window, Mapping):
            continue
        used = window.get("used_pct")
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        clean[name] = {"used_pct": used}
        reset = window.get("resets_at")
        if (type(reset) in (int, float) and math.isfinite(reset)
                and checked <= reset <= now + 8 * 24 * 60 * 60):
            clean[name]["resets_at"] = reset
    return {"checked_at": checked, **clean} if clean else {}


def slot_report(slot: Mapping[str, Any], heartbeat: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = (heartbeat or {}).get("payload")
    entries = payload.get("slots") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        return {}
    return next((dict(entry) for entry in entries if isinstance(entry, Mapping)
                 and entry.get("unix_user") == slot.get("unix_user")), {})


def health(slot: Mapping[str, Any], report: Mapping[str, Any], login: Any, *,
           heard: Any, now: float, max_age: float,
           listening_since: Optional[float] = None) -> dict[str, Any]:
    """Describe observations, never claim a heartbeat proves provider acceptance."""
    credentials = report.get("credentials")
    credentials = credentials if isinstance(credentials, Mapping) else {}
    renewal = renewal_report(credentials.get("renewal"))
    expiry = access_expiry(credentials)
    terminal = sign_in_reason(credentials, now)
    result: dict[str, Any] = {"state": slot.get("state") if slot.get("state") in {
        "free", "claiming", "claimed", "active", "releasing"} else "unknown",
        "ready": False, "health": "degraded", "reason": "observation_stale",
        "readiness_source": "reported"}
    if isinstance(heard, (int, float)) and not isinstance(heard, bool) and math.isfinite(heard):
        result["observed_at"] = heard
    if isinstance(login, Mapping) and login.get("state") in {
            "requested", "waiting", "running", "url_ready", "code_sent"}:
        result.update(health="switching", reason="sign_in_pending")
    elif renewal.get("reason") in TRANSITION_REASONS:
        result.update(health="switching", reason="account_transition")
    elif not fresh_observation(credentials, heard, now, max_age, listening_since):
        return result
    elif (credentials.get("bound_fp") and credentials.get("account_fp")
          and credentials["bound_fp"] != credentials["account_fp"]):
        result.update(health="switching", reason="account_mismatch")
    elif not credentials.get("bound_fp") or not credentials.get("account_fp"):
        result.update(health="degraded", reason="account_unbound")
    elif terminal in NATIVE_LOGIN_FAILURES:
        result.update(health="sign_in_required", reason=("not_signed_in"
                      if terminal == "native_auth_rejected" else "credential_expired"))
    elif credentials.get("logged_in") is False or credentials.get("present") is False:
        result.update(health="sign_in_required", reason="not_signed_in")
    elif terminal and (expiry is None or expiry <= now + ACCESS_MARGIN_S):
        result.update(health="sign_in_required", reason="credential_expired")
    elif expiry is None:
        result.update(health="degraded", reason="expiry_unknown")
    elif expiry <= now + ACCESS_MARGIN_S:
        result.update(health="renewal_pending", reason="native_renewal_pending")
    elif credentials.get("logged_in") is True:
        result.update(health="ready", reason="credentials_current", ready=True)
    if result["health"] not in {"switching", "degraded"}:
        warning = renewal_warning(credentials, now)
        if warning in RENEWAL_WARNING_CODES:
            result["renewal_warning"] = warning
        elif expiry is not None and now + ACCESS_MARGIN_S < expiry <= now + RENEWAL_WINDOW_S:
            result["renewal_warning"] = "renewal_due"
    claude = report.get("claude")
    version = claude.get("version") if isinstance(claude, Mapping) else None
    if isinstance(version, str) and VERSION_RE.fullmatch(version):
        result["claude_version"] = version
    # Quota freshness and native credential readiness are distinct observations.
    since = max((value for value in (slot.get("claimed_at"), slot.get("account_switched_at"))
                 if type(value) in (int, float) and math.isfinite(value)), default=0)
    validation = compatibility.observation(
        report.get("compatibility"), claude, credentials, heard=heard, now=now,
        max_age=max_age, since=since, listening_since=listening_since)
    if validation:
        result["compatibility"] = compatibility.public(validation)
    quota = quota_observation(report.get("quota"), slot, now)
    if quota and result["health"] != "switching":
        result["quota"] = quota
    return result


def device_status(store: Any, cfg: Any, device: Mapping[str, Any], now: float) -> dict[str, Any]:
    slot = store.get_slot(device["slot_id"])
    node = store.get_node(device["node_id"])
    if slot is None or node is None:
        raise ValueError("device assignment is unavailable")
    latest = store.recent_heartbeats(device["node_id"], limit=1)
    heartbeat = latest[0] if latest else None
    report = slot_report(slot, heartbeat)
    state = health(slot, report, store.login_for_slot(slot),
                   heard=(heartbeat or {}).get("ts"), now=now,
                   max_age=cfg.heartbeat_max_age_s, listening_since=node.get("listening_since"))
    epoch = json.dumps({"slot": slot["id"], "claim": slot.get("claimed_at"),
                        "account_change": slot.get("account_switched_at")},
                       sort_keys=True, separators=(",", ":")).encode()
    secret = str(cfg.cookie_secret or cfg.admin_token).encode()
    state["account_generation"] = hmac.new(secret, epoch, hashlib.sha256).hexdigest()[:24]
    measurements = relay_report(report.get("relay"), now)
    since = max((value for value in (slot.get("claimed_at"), slot.get("account_switched_at"))
                 if type(value) in (int, float) and math.isfinite(value)), default=0)
    if measurements is not None and measurements["observed_at"] >= since:
        state["relay"] = measurements
    return {"authenticated": True, "device": {"active": True},
            "protocol": 2, "slot": state}
