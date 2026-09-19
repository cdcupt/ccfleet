"""Validation of heartbeat payloads posted by node agents.

Only known fields with expected types are kept. Everything else is dropped so a
compromised or buggy agent cannot smuggle large or odd data into the store.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

MAX_STR = 200


class HeartbeatError(ValueError):
    """Raised when the payload is not a usable heartbeat."""


def _str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value[:MAX_STR]
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) or value is None else None


def _section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = payload.get(key)
    return section if isinstance(section, Mapping) else {}


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
    upgrade = _section(_section(payload, "reconcile"), "upgrade")
    # Only carry the section when the agent actually reported one. Emitting a
    # skeleton of Nones makes "has this node ever reconciled?" unanswerable: the
    # dict is truthy, so every node looks like it has.
    has_upgrade = any(upgrade.get(k) is not None
                      for k in ("from", "to", "ok", "error", "ts"))
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
        },
        "disk": {"used_pct": _num(disk.get("used_pct")), "free_gb": _num(disk.get("free_gb"))},
        "mem": {"used_pct": _num(mem.get("used_pct"))},
        "load": {"1": _num(load.get("1")), "5": _num(load.get("5")), "15": _num(load.get("15"))},
        "egress": {"ip": _str(egress.get("ip")), "source": _str(egress.get("source"))},
        "remote_control": {"state": _str(rc.get("state"))},
        "tmux_sessions": _num(payload.get("tmux_sessions")),
    }
    if has_upgrade:
        # What the agent did about the last desired state it was handed. Reported
        # one beat late by construction: the agent acts after posting.
        result["reconcile"] = {
            "upgrade": {
                "from": _str(upgrade.get("from")),
                "to": _str(upgrade.get("to")),
                "ok": _bool_or_none(upgrade.get("ok")),
                "error": _str(upgrade.get("error")),
                "ts": _num(upgrade.get("ts")),
            },
        }
    return result
