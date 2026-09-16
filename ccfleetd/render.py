"""Server-rendered fleet dashboard. Every value is HTML-escaped; no scripts."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any, Optional

from .config import Config

LEVEL_ORDER = {"ok": 0, "warn": 1, "critical": 2}

CSS = """
:root{--bg:#f7f8fa;--panel:#fff;--ink:#131820;--muted:#4e5966;--rule:#d7dde5;
--acc:#1747c7;--ok:#157f3b;--ok-bg:#e3f4e8;--warn:#a85b00;--warn-bg:#fbeedb;
--bad:#b42318;--bad-bg:#fbe4e1}
@media (prefers-color-scheme:dark){:root{--bg:#0e1217;--panel:#151b23;--ink:#e7ebf1;
--muted:#a3adba;--rule:#2b3440;--acc:#8fb0ff;--ok:#5ad08a;--ok-bg:#12321f;--warn:#f2b35b;
--warn-bg:#3a2a0e;--bad:#ff8a80;--bad-bg:#3e1714}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:20px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;margin:0 0 18px}
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--rule);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:900px;font-size:14px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--rule);
vertical-align:top;white-space:nowrap}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);background:var(--bg)}
tr:last-child td{border-bottom:0}td.num{font-variant-numeric:tabular-nums}
.pill{display:inline-block;font-size:12px;padding:2px 9px;border-radius:999px;
border:1px solid;font-weight:600}
.ok{color:var(--ok);border-color:var(--ok);background:var(--ok-bg)}
.warn{color:var(--warn);border-color:var(--warn);background:var(--warn-bg)}
.critical{color:var(--bad);border-color:var(--bad);background:var(--bad-bg)}
.muted{color:var(--muted)}h2{font-size:16px;margin:26px 0 8px}
ul.alerts{margin:0;padding-left:18px}ul.alerts li{margin:0 0 4px}
code{font-family:ui-monospace,Menlo,monospace;font-size:13px}
"""


def _age(now: float, ts: Optional[float]) -> str:
    if ts is None:
        return "never"
    seconds = max(0, int(now - ts))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _in(now: float, ts_ms: Optional[float]) -> str:
    if ts_ms is None:
        return "-"
    delta = ts_ms / 1000.0 - now
    return ("in " if delta >= 0 else "") + _age(0, -abs(delta)) + ("" if delta >= 0 else " ago")


def build_rows(nodes: list[Mapping[str, Any]], latest: Mapping[str, Mapping[str, Any]],
               alerts: list[Mapping[str, Any]], now: float) -> list[dict[str, Any]]:
    """Merge node records, latest heartbeats and open alerts into dashboard rows."""
    alerts_by_node: dict[str, list[Mapping[str, Any]]] = {}
    for alert in alerts:
        alerts_by_node.setdefault(alert["node_id"], []).append(alert)
    rows = []
    for node in nodes:
        hb = latest.get(node["id"])
        payload = (hb or {}).get("payload") or {}
        node_alerts = alerts_by_node.get(node["id"], [])
        level = "ok"
        for alert in node_alerts:
            if LEVEL_ORDER.get(alert["level"], 0) > LEVEL_ORDER[level]:
                level = alert["level"]
        creds = payload.get("credentials") or {}
        rows.append({
            "id": node["id"], "owner": node["owner"], "region": node["region"],
            "enabled": node["enabled"], "status": level if node["enabled"] else "disabled",
            "last_seen_ts": (hb or {}).get("ts"),
            "hostname": payload.get("hostname"),
            "claude_version": (payload.get("claude") or {}).get("version"),
            "pinned_version": node["pinned_version"],
            "egress_ip": (payload.get("egress") or {}).get("ip"),
            "disk_used_pct": (payload.get("disk") or {}).get("used_pct"),
            "load1": (payload.get("load") or {}).get("1"),
            "credentials_present": creds.get("present"),
            "credentials_mtime": creds.get("mtime"),
            "token_expires_at": creds.get("expires_at"),
            "subscription_type": creds.get("subscription_type"),
            "remote_control": (payload.get("remote_control") or {}).get("state"),
            "rc_expected": node["rc_expected"],
            "open_alerts": [a["rule"] for a in node_alerts],
        })
    return rows


def _pill(level: str) -> str:
    return f'<span class="pill {escape(level)}">{escape(level)}</span>'


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return '<span class="muted">-</span>'
    if isinstance(value, float):
        return escape(f"{value:.0f}{suffix}") if suffix == "%" else escape(f"{value:.2f}{suffix}")
    return escape(f"{value}{suffix}")


def _row_html(row: Mapping[str, Any], now: float) -> str:
    version = _fmt(row["claude_version"])
    if row["pinned_version"]:
        mark = "" if row["claude_version"] == row["pinned_version"] else " ≠ pinned"
        version += f' <span class="muted">{escape(row["pinned_version"] + mark)}</span>'
    creds = row["credentials_present"]
    cred_text = ("unknown" if creds is None else ("missing" if creds is False else
                 f"refreshed {_age(now, row['credentials_mtime'])} ago"))
    if row["subscription_type"]:
        cred_text += f" ({row['subscription_type']})"
    rc = row["remote_control"] or "-"
    if row["rc_expected"]:
        rc += " (expected)"
    alerts = ", ".join(row["open_alerts"]) or "-"
    return (
        "<tr>"
        f"<td>{_pill(row['status'])}</td>"
        f"<td><strong>{escape(row['id'])}</strong><br><span class=\"muted\">{escape(row['owner'])}"
        f" · {escape(row['region'] or '-')}</span></td>"
        f"<td class=\"num\">{escape(_age(now, row['last_seen_ts']))}</td>"
        f"<td>{version}</td>"
        f"<td><code>{_fmt(row['egress_ip'])}</code></td>"
        f"<td class=\"num\">{_fmt(row['disk_used_pct'], '%')}</td>"
        f"<td class=\"num\">{_fmt(row['load1'])}</td>"
        f"<td>{escape(cred_text)}<br><span class=\"muted\">token "
        f"{escape(_in(now, row['token_expires_at']))}</span></td>"
        f"<td>{escape(rc)}</td>"
        f"<td>{escape(alerts)}</td>"
        "</tr>"
    )


def render_dashboard(rows: list[Mapping[str, Any]], alerts: list[Mapping[str, Any]],
                     now: float, cfg: Config) -> str:
    body_rows = "".join(_row_html(r, now) for r in rows) or (
        '<tr><td colspan="10" class="muted">No nodes yet. Add one with '
        "<code>ccfleetd node add &lt;id&gt; --owner &lt;name&gt;</code>.</td></tr>")
    alert_items = "".join(
        f"<li>{_pill(a['level'])} <strong>{escape(a['node_id'])}</strong> "
        f"{escape(a['rule'])}: {escape(a['message'])} "
        f"<span class=\"muted\">({escape(_age(now, a['opened_at']))} ago)</span></li>"
        for a in alerts) or '<li class="muted">none</li>'
    counts = {"ok": 0, "warn": 0, "critical": 0, "disabled": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = " · ".join(f"{escape(k)} {v}" for k, v in counts.items() if v)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta http-equiv=\"refresh\" content=\"60\"><title>ccfleet</title>"
        f"<style>{CSS}</style></head><body>"
        "<h1>ccfleet</h1>"
        f"<p class=\"sub\">one owner, one account, one node · {summary or 'no nodes'} · "
        f"heartbeat max age {cfg.heartbeat_max_age_s // 60} min · refreshes every minute</p>"
        "<div class=\"wrap\"><table><thead><tr><th>Status</th><th>Node</th><th>Last seen</th>"
        "<th>Claude Code</th><th>Egress IP</th><th>Disk</th><th>Load</th><th>Login</th>"
        "<th>Remote Control</th><th>Open alerts</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
        f"<h2>Open alerts</h2><ul class=\"alerts\">{alert_items}</ul>"
        "</body></html>"
    )
