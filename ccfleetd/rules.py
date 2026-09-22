"""Pure alert rules. ``evaluate`` never touches storage or the network."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from . import slots as slotstates
from .config import Config
from .desired import is_channel

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


def _claude_findings(node: Mapping[str, Any], payload: Mapping[str, Any],
                     previous: Optional[Mapping[str, Any]] = None) -> list[Finding]:
    version = _get(payload, "claude", "version")
    if not version:
        # One miss is usually the binary being swapped mid-upgrade, not a broken
        # node: the installer leaves ~/.local/bin/claude a symlink, and a probe
        # that lands while it is being replaced sees nothing. Observed on a fresh
        # node, which raised a critical "claude is not installed" between two
        # heartbeats that both found 2.1.278.
        #
        # So require two in a row. A node that genuinely lost claude still alerts
        # one interval later, and a node that never had it alerts immediately,
        # because there is then no earlier heartbeat that found one.
        if previous is not None and _get(previous, "claude", "version"):
            return []
        return [Finding("claude_missing", LEVEL_CRITICAL,
                        "claude is not installed or not on PATH for the owner user")]
    pinned = node.get("pinned_version") or ""
    # A channel pin is satisfied by definition: the node tracks it, and there is
    # no number to compare. Comparing literally would alert forever.
    if pinned and not is_channel(pinned) and version != pinned:
        return [Finding("version_mismatch", LEVEL_WARN,
                        f"claude {version} differs from pinned {pinned}")]
    return []


def _credential_findings(payload: Mapping[str, Any], now: float, cfg: Config) -> list[Finding]:
    # `claude auth status` is authoritative when the node could ask it; a present
    # credentials file can still hold a login that no longer works.
    logged_in = _get(payload, "credentials", "logged_in")
    present = logged_in if isinstance(logged_in, bool) else _get(payload, "credentials",
                                                                 "present")
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
    elif not isinstance(mtime, (int, float)):
        # No file to stat, which is every macOS machine: Claude Code keeps the
        # credential in the Keychain. The account block gives a profile fetch
        # time instead, and that only advances while the login still works, so a
        # stale one means the same thing a stale mtime means.
        fetched_ms = _get(payload, "credentials", "profile_fetched_at")
        if isinstance(fetched_ms, (int, float)):
            age = now - fetched_ms / 1000.0
            if age > cfg.token_stale_s:
                findings.append(Finding("token_stale", LEVEL_WARN,
                                        f"login not exercised for {_fmt_age(age)}"))
    expires_ms = _get(payload, "credentials", "expires_at")
    if isinstance(expires_ms, (int, float)):
        expired_for = now - expires_ms / 1000.0
        if expired_for > cfg.token_expired_grace_s:
            findings.append(Finding("token_expired", LEVEL_WARN,
                                    f"access token expired {_fmt_age(expired_for)} ago and was "
                                    "not refreshed; open a session or run /login"))
    return findings


WINDOW_WORDS = {"session": "5-hour window", "week": "weekly window"}
# A reading older than this is not evidence about now. The agent refreshes every
# 30 minutes, so two missed refreshes means something is wrong with the read
# rather than with the quota, and alerting on it would be alerting on the wrong
# thing.
QUOTA_MAX_AGE_S = 2 * 60 * 60


def _quota_findings(payload: Mapping[str, Any], now: float, cfg: Config) -> list[Finding]:
    """Warn before a window runs out, not after.

    The console has shown these two numbers since the windows were added, which
    only helps someone already looking at it. This is the half that reaches you.
    """
    quota = payload.get("quota")
    if not isinstance(quota, Mapping):
        return []
    checked = quota.get("checked_at")
    if isinstance(checked, (int, float)) and not isinstance(checked, bool):
        if now - checked > QUOTA_MAX_AGE_S:
            # Stale. Say nothing rather than report an old number as current.
            return []
    findings = []
    for name, words in WINDOW_WORDS.items():
        window = quota.get(name)
        if not isinstance(window, Mapping):
            continue
        used = window.get("used_pct")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        level = (LEVEL_CRITICAL if used >= cfg.quota_crit_pct else
                 LEVEL_WARN if used >= cfg.quota_warn_pct else None)
        if level is None:
            continue
        resets = window.get("resets")
        tail = f", resets {resets}" if isinstance(resets, str) and resets else ""
        # The rule name carries the window, so the two do not collapse into one
        # alert that flaps as whichever is worse changes.
        findings.append(Finding(f"quota_high_{name}", level,
                                f"{words} {used:.0f}% used{tail}"))
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


def _slot_findings(slot_rows: Sequence[Mapping[str, Any]],
                   payload: Mapping[str, Any]) -> list[Finding]:
    """What a shared machine says about its slots that needs the operator.

    Only lifecycle trouble: a wipe that failed, a free slot whose user exists,
    a held slot whose user vanished, provisioning that failed. A holder's own
    login and quota are theirs to see on their page, not the operator's to be
    paged about. Each finding names its slot in the rule, so two slots in
    trouble are two alerts rather than one that flaps between them.
    """
    reports = {r.get("unix_user"): r for r in payload.get("slots") or []
               if isinstance(r, Mapping)}
    findings: list[Finding] = []
    for row in slot_rows:
        user, state = row.get("unix_user"), row.get("state")
        # A slot the machine did not mention has simply not been looked at
        # yet. An empty report says nothing, so every branch below passes it by.
        report = reports.get(user) or {}
        present = report.get("present")
        wipe_error = report.get("wipe_error")
        if state == slotstates.RELEASING and wipe_error:
            findings.append(Finding(
                f"slot_wipe_failed:{user}", LEVEL_CRITICAL,
                f"wiping {user} failed ({wipe_error}); it stays out of the pool "
                f"until a wipe succeeds"))
        elif state == slotstates.FREE and present is True:
            findings.append(Finding(
                f"slot_occupied:{user}", LEVEL_CRITICAL,
                f"{user} is free here but its Linux user exists on the machine, so it "
                f"is not handed out; remove it there with slot-remove.sh --slot {user}"))
        elif state in (slotstates.CLAIMED, slotstates.ACTIVE) and present is False:
            findings.append(Finding(
                f"slot_missing:{user}", LEVEL_CRITICAL,
                f"{user} is held but its Linux user is gone from the machine"))
        provision_error = report.get("provision_error")
        if provision_error and state in (slotstates.CLAIMING, slotstates.RELEASING):
            findings.append(Finding(
                f"slot_provision_failed:{user}", LEVEL_WARN,
                f"setting up {user} failed ({provision_error}); the claim was "
                f"given up and the slot is being wiped"))
    return findings


def evaluate(node: Mapping[str, Any], latest: Optional[Mapping[str, Any]],
             previous: Optional[Mapping[str, Any]], now: float,
             cfg: Config,
             slot_rows: Sequence[Mapping[str, Any]] = ()) -> tuple[Finding, ...]:
    """Return every finding for one node given its latest two heartbeats.

    ``latest`` and ``previous`` are heartbeat rows (``{"ts": ..., "payload": {...}}``).
    ``slot_rows`` are the slots declared on this node, for a shared machine.
    """
    findings = _heartbeat_findings(node, latest, now, cfg)
    if latest is None:
        return tuple(findings)
    payload = latest.get("payload") or {}
    prev_payload = (previous or {}).get("payload") if previous else None
    if payload.get("mode") == slotstates.MACHINE_MODE:
        # A shared machine has no owner login of its own — every login on it is
        # a slot holder's — so the owner-level checks would only ever say
        # "claude missing" and "not signed in" about an account that does not
        # exist. It is judged on the machine and on its slots instead.
        findings += _disk_findings(payload, cfg)
        findings += _egress_findings(payload, prev_payload)
        findings += _slot_findings(slot_rows, payload)
        return tuple(findings)
    findings += _claude_findings(node, payload, prev_payload)
    findings += _credential_findings(payload, now, cfg)
    findings += _disk_findings(payload, cfg)
    findings += _quota_findings(payload, now, cfg)
    findings += _egress_findings(payload, prev_payload)
    findings += _remote_control_findings(node, payload)
    return tuple(findings)


def worst_level(findings: tuple[Finding, ...]) -> str:
    if any(f.level == LEVEL_CRITICAL for f in findings):
        return LEVEL_CRITICAL
    if findings:
        return LEVEL_WARN
    return "ok"
