"""Validation of heartbeat payloads posted by node agents.

Only known fields with expected types are kept. Everything else is dropped so a
compromised or buggy agent cannot smuggle large or odd data into the store.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

MAX_STR = 200
# Larger than any real measurement, small enough to stay a float.
MAX_NUMBER = 10 ** 15
# One field genuinely needs more room. A real sign-in URL carries the client id,
# both redirect URIs, the full scope list, a PKCE challenge and the state
# parameter: measured at 496 characters against a live node. Capped at MAX_STR it
# arrives truncated, which is worse than absent — it still looks like a URL.
MAX_URL = 1024
# Usage is counted from local transcripts on the node. What arrives is token
# counts, per-day totals and model names; the transcripts themselves hold
# conversation content and never leave the machine.
MAX_USAGE_DAYS = 31
MAX_USAGE_MODELS = 8
USAGE_COUNTERS = ("total_tokens", "input_tokens", "output_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens", "sessions")


class HeartbeatError(ValueError):
    """Raised when the payload is not a usable heartbeat."""


def _str(value: Any, limit: int = MAX_STR) -> Optional[str]:
    if isinstance(value, str):
        return value[:limit]
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Python integers are arbitrary precision, so an authenticated node can post
    # a 400-digit one. Anything that later does float arithmetic on it — the
    # dashboard's charts, for instance — raises OverflowError and takes the page
    # down, and math.isnan below would raise on it first anyway. Every field
    # here is a measurement, and none of them is legitimately this large.
    if isinstance(value, int) and abs(value) > MAX_NUMBER:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) or value is None else None


def _section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = payload.get(key)
    return section if isinstance(section, Mapping) else {}


def _usage(section: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only, each bounded. A node cannot post an unbounded series here."""
    out: dict[str, Any] = {k: _num(section.get(k)) for k in USAGE_COUNTERS}
    out["window_days"] = _num(section.get("window_days"))
    models = section.get("models")
    out["models"] = ([_str(m, 40) for m in models
                      if isinstance(m, str)][:MAX_USAGE_MODELS]
                     if isinstance(models, list) else [])
    days = section.get("by_day")
    series = []
    if isinstance(days, list):
        for entry in days[-MAX_USAGE_DAYS:]:
            if not isinstance(entry, Mapping):
                continue
            day, tokens = _str(entry.get("day"), 10), _num(entry.get("tokens"))
            if day and tokens is not None:
                series.append({"day": day, "tokens": tokens})
    out["by_day"] = series
    return out


def validate_heartbeat(payload: Any, node_id: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise HeartbeatError("heartbeat body must be a JSON object")
    if payload.get("node_id") != node_id:
        raise HeartbeatError("node_id in body does not match the authenticated node")
    claude = _section(payload, "claude")
    creds = _section(payload, "credentials")
    disk = _section(payload, "disk")
    mem = _section(payload, "mem")
    egress = _section(payload, "egress")
    rc = _section(payload, "remote_control")
    load = _section(payload, "load")
    reconcile = _section(payload, "reconcile")
    upgrade = _section(reconcile, "upgrade")
    login = _section(reconcile, "login")
    usage = _section(payload, "usage")
    # Only carry the section when the agent actually reported one. Emitting a
    # skeleton of Nones makes "has this node ever reconciled?" unanswerable: the
    # dict is truthy, so every node looks like it has.
    has_upgrade = any(upgrade.get(k) is not None
                      for k in ("from", "to", "ok", "error", "ts"))
    # Sign-in progress. The URL is shown to an operator and the detail may quote
    # the CLI, so both are length-capped like every other node-supplied string.
    login_state = _str(login.get("state"))
    result: dict[str, Any] = {
        "node_id": node_id,
        "agent_ts": _num(payload.get("ts")),
        "agent_version": _str(payload.get("agent_version")),
        "hostname": _str(payload.get("hostname")),
        "uptime_s": _num(payload.get("uptime_s")),
        "claude": {"version": _str(claude.get("version")), "path": _str(claude.get("path"))},
        "credentials": {
            "present": _bool_or_none(creds.get("present")),
            "store": _str(creds.get("store")),
            "mtime": _num(creds.get("mtime")),
            "expires_at": _num(creds.get("expires_at")),
            "subscription_type": _str(creds.get("subscription_type")),
            "profile_fetched_at": _num(creds.get("profile_fetched_at")),
            "plan": _str(creds.get("plan")),
            # From `claude auth status`: the CLI's own answer, not an inference
            # from a file existing. Deliberately no email, org name or org id.
            "logged_in": _bool_or_none(creds.get("logged_in")),
            "auth_method": _str(creds.get("auth_method")),
            "api_provider": _str(creds.get("api_provider")),
        },
        "disk": {"used_pct": _num(disk.get("used_pct")), "free_gb": _num(disk.get("free_gb"))},
        "mem": {"used_pct": _num(mem.get("used_pct"))},
        "load": {"1": _num(load.get("1")), "5": _num(load.get("5")), "15": _num(load.get("15"))},
        "egress": {"ip": _str(egress.get("ip")), "source": _str(egress.get("source"))},
        "remote_control": {"state": _str(rc.get("state"))},
        "tmux_sessions": _num(payload.get("tmux_sessions")),
        "usage": _usage(usage),
    }
    if login_state:
        result["reconcile"] = {"login": {
            "state": login_state,
            "url": _str(login.get("url"), MAX_URL),
            "detail": _str(login.get("detail")),
            "requested_at": _num(login.get("requested_at")),
        }}
    if has_upgrade:
        # What the agent did about the last desired state it was handed. Reported
        # one beat late by construction: the agent acts after posting.
        result.setdefault("reconcile", {})["upgrade"] = {
                "from": _str(upgrade.get("from")),
                "to": _str(upgrade.get("to")),
                "ok": _bool_or_none(upgrade.get("ok")),
                "error": _str(upgrade.get("error")),
            "ts": _num(upgrade.get("ts")),
        }
    return result
