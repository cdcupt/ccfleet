"""Pure alert rules. ``evaluate`` never touches storage or the network."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from .config import Config

LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str
    message: str


def _get(payload: Mapping[str, Any], *path: str) -> Any:
    cur: Any = payload
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _heartbeat_findings(node: Mapping[str, Any], latest: Optional[Mapping[str, Any]],
                        now: float, cfg: Config) -> list[Finding]:
    if latest is None:
        if now - float(node.get("created_at", now)) > cfg.heartbeat_max_age_s:
            return [Finding("no_heartbeat", LEVEL_CRITICAL, "no heartbeat received yet")]
        return []
    age = now - float(latest["ts"])
    if age > cfg.heartbeat_max_age_s:
        return [Finding("no_heartbeat", LEVEL_CRITICAL, f"last heartbeat {_fmt_age(age)} ago")]
    return []


def _claude_findings(node: Mapping[str, Any], payload: Mapping[str, Any]) -> list[Finding]:
    version = _get(payload, "claude", "version")
    if not version:
        return [Finding("claude_missing", LEVEL_CRITICAL,
                        "claude is not installed or not on PATH for the owner user")]
    pinned = node.get("pinned_version") or ""
    if pinned and version != pinned:
        return [Finding("version_mismatch", LEVEL_WARN,
                        f"claude {version} differs from pinned {pinned}")]
    return []


def _credential_findings(payload: Mapping[str, Any], now: float, cfg: Config) -> list[Finding]:
    present = _get(payload, "credentials", "present")
    if present is None:
        return []
    if present is False:
        return [Finding("credentials_missing", LEVEL_CRITICAL,
                        "no credentials file: the owner needs to run claude and /login")]
    findings: list[Finding] = []
    mtime = _get(payload, "credentials", "mtime")
    if isinstance(mtime, (int, float)) and now - mtime > cfg.token_stale_s:
        findings.append(Finding("token_stale", LEVEL_WARN,
                                f"credentials not refreshed for {_fmt_age(now - mtime)}"))
    expires_ms = _get(payload, "credentials", "expires_at")
    if isinstance(expires_ms, (int, float)):
        expired_for = now - expires_ms / 1000.0
        if expired_for > cfg.token_expired_grace_s:
            findings.append(Finding("token_expired", LEVEL_WARN,
                                    f"access token expired {_fmt_age(expired_for)} ago and was "
                                    "not refreshed; open a session or run /login"))
    return findings


def _disk_findings(payload: Mapping[str, Any], cfg: Config) -> list[Finding]:
    used = _get(payload, "disk", "used_pct")
    if not isinstance(used, (int, float)):
        return []
    if used >= cfg.disk_crit_pct:
        return [Finding("disk_high", LEVEL_CRITICAL, f"disk {used:.0f}% used")]
    if used >= cfg.disk_warn_pct:
        return [Finding("disk_high", LEVEL_WARN, f"disk {used:.0f}% used")]
    return []


def _egress_findings(payload: Mapping[str, Any],
                     previous: Optional[Mapping[str, Any]]) -> list[Finding]:
    if previous is None:
        return []
    current_ip = _get(payload, "egress", "ip")
    previous_ip = _get(previous, "egress", "ip")
    if current_ip and previous_ip and current_ip != previous_ip:
        return [Finding("egress_changed", LEVEL_WARN,
                        f"egress IP changed from {previous_ip} to {current_ip}")]
    return []


def _remote_control_findings(node: Mapping[str, Any],
                             payload: Mapping[str, Any]) -> list[Finding]:
    if not node.get("rc_expected"):
        return []
    state = _get(payload, "remote_control", "state")
    if state != "active":
        return [Finding("remote_control_down", LEVEL_WARN,
                        f"remote-control service is {state or 'unknown'}")]
    return []


def evaluate(node: Mapping[str, Any], latest: Optional[Mapping[str, Any]],
             previous: Optional[Mapping[str, Any]], now: float,
             cfg: Config) -> tuple[Finding, ...]:
    """Return every finding for one node given its latest two heartbeats.

    ``latest`` and ``previous`` are heartbeat rows (``{"ts": ..., "payload": {...}}``).
    """
    findings = _heartbeat_findings(node, latest, now, cfg)
    if latest is None:
        return tuple(findings)
    payload = latest.get("payload") or {}
    prev_payload = (previous or {}).get("payload") if previous else None
    findings += _claude_findings(node, payload)
    findings += _credential_findings(payload, now, cfg)
    findings += _disk_findings(payload, cfg)
    findings += _egress_findings(payload, prev_payload)
    findings += _remote_control_findings(node, payload)
    return tuple(findings)


def worst_level(findings: tuple[Finding, ...]) -> str:
    if any(f.level == LEVEL_CRITICAL for f in findings):
        return LEVEL_CRITICAL
    if findings:
        return LEVEL_WARN
    return "ok"
