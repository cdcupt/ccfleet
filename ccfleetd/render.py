"""Server-rendered fleet dashboard. Every value is HTML-escaped; no scripts."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from shlex import quote as shq
from typing import Any, Optional

from .config import Config
from .desired import is_channel, is_login_url

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
td.wrap{white-space:normal;min-width:130px}
.pill{display:inline-block;font-size:12px;padding:2px 9px;border-radius:999px;
border:1px solid;font-weight:600}
.ok{color:var(--ok);border-color:var(--ok);background:var(--ok-bg)}
.warn{color:var(--warn);border-color:var(--warn);background:var(--warn-bg)}
.critical{color:var(--bad);border-color:var(--bad);background:var(--bad-bg)}
.muted{color:var(--muted)}h2{font-size:16px;margin:26px 0 8px}
form.inline{display:inline;margin:0}
button,.btn{font:inherit;font-size:12px;padding:4px 10px;border-radius:7px;
border:1px solid var(--rule);background:var(--panel);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--acc);color:var(--acc)}
button.danger:hover{border-color:var(--bad);color:var(--bad)}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc);
font-size:13px;padding:7px 16px}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:10px;
padding:16px 18px;margin:18px 0;max-width:760px}
.card h2{margin:0 0 12px}
label{display:block;font-size:12px;color:var(--muted);margin:0 0 4px}
input[type=text]{font:inherit;font-size:14px;padding:7px 10px;border-radius:7px;
border:1px solid var(--rule);background:var(--bg);color:var(--ink);width:100%;max-width:280px}
.fields{display:flex;flex-wrap:wrap;gap:12px 18px;margin:0 0 14px}
.check{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--ink);margin-top:18px}
.actions{display:flex;gap:5px;flex-wrap:wrap}
.manage-row{display:flex;align-items:center;justify-content:space-between;gap:14px;
flex-wrap:wrap;padding:9px 0;border-bottom:1px solid var(--rule)}
.manage-row:last-of-type{border-bottom:0}
.manage-name{font-weight:600;font-size:14px}
pre{background:var(--bg);border:1px solid var(--rule);border-radius:8px;
padding:12px 14px;overflow-x:auto;font-family:ui-monospace,Menlo,monospace;
font-size:13px;line-height:1.5;margin:0 0 14px;white-space:pre}
.ok-banner{border:1px solid var(--ok);background:var(--ok-bg);color:var(--ok);
border-radius:10px;padding:12px 16px;margin:0 0 18px}
a.back{font-size:13px}
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
            # What the node did about its pin last time it was asked. A silent
            # reconcile is indistinguishable from one that never ran.
            "last_upgrade": ((payload.get("reconcile") or {}).get("upgrade") or None),
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


def _actions_html(row: Mapping[str, Any], csrf: str) -> str:
    """Per-node buttons. Every write is a POST carrying the CSRF token."""
    node = escape(row["id"])
    def form(action: str, label: str, extra: str = "", cls: str = "") -> str:
        return (f'<form class="inline" method="post" action="/actions/node/{node}/{action}">'
                f'<input type="hidden" name="csrf" value="{escape(csrf)}">{extra}'
                f'<button class="{cls}" type="submit">{escape(label)}</button></form>')
    toggle = form("disable", "Disable") if row["enabled"] else form("enable", "Enable")
    rc = form("rc-off", "RC alert off") if row["rc_expected"] else form("rc-on", "RC alert on")
    pin = ""
    if row["claude_version"] and row["claude_version"] != row["pinned_version"]:
        version = escape(str(row["claude_version"]))
        pin = form("pin", f"Pin {row['claude_version']}",
                   f'<input type="hidden" name="version" value="{version}">')
    rotate = form("rotate-token", "New token", cls="danger")
    remove = form("remove", "Remove", '<input type="hidden" name="confirm" value="'
                  + node + '">', cls="danger")
    return f'<div class="actions">{toggle}{rc}{pin}{rotate}{remove}</div>'


def _row_html(row: Mapping[str, Any], now: float) -> str:
    version = _fmt(row["claude_version"])
    if row["pinned_version"]:
        # A channel is never "≠ pinned": tracking it is what the pin asks for.
        mark = ("" if is_channel(row["pinned_version"])
                or row["claude_version"] == row["pinned_version"] else " ≠ pinned")
        version += f' <span class="muted">{escape(row["pinned_version"] + mark)}</span>'
    # A drifted version with no explanation reads as "not tried yet". Say when the
    # node tried and failed, because that is the case an operator must act on.
    upgrade = row.get("last_upgrade") or {}
    if upgrade.get("ok") is False:
        detail = escape(str(upgrade.get("error") or "")[:120])
        version += (f'<br><span class="critical">upgrade to '
                    f'{escape(str(upgrade.get("to") or "?"))} failed</span>'
                    + (f' <span class="muted">{detail}</span>' if detail else ""))
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
        f'<td class="wrap">{escape(alerts)}</td>'
        "</tr>"
    )


def _manage_html(rows: list[Mapping[str, Any]], csrf: str) -> str:
    """Per-node controls, kept out of the status table so neither gets cramped."""
    if not rows:
        return ""
    items = []
    for row in rows:
        node = escape(row["id"])

        def form(action: str, label: str, extra: str = "", cls: str = "", node=node) -> str:
            return (f'<form class="inline" method="post" action="/actions/node/{node}/{action}">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">{extra}'
                    f'<button class="{cls}" type="submit">{escape(label)}</button></form>')

        buttons = [form("disable", "Disable") if row["enabled"] else form("enable", "Enable"),
                   form("rc-off", "RC alert off") if row["rc_expected"] else
                   form("rc-on", "RC alert on")]
        if row["claude_version"] and row["claude_version"] != row["pinned_version"]:
            version = escape(str(row["claude_version"]))
            buttons.append(form("pin", f"Pin {row['claude_version']}",
                                f'<input type="hidden" name="version" value="{version}">'))
        buttons.append(form("rotate-token", "New token", cls="danger"))
        buttons.append(form("remove", "Remove",
                            f'<input type="hidden" name="confirm" value="{node}">', cls="danger"))
        items.append(f'<div class="manage-row"><div class="manage-name">{node}'
                     f'<span class="muted"> · {escape(row["owner"])}</span></div>'
                     f'<div class="actions">{"".join(buttons)}</div></div>')
    return ('<div class="card"><h2>Manage nodes</h2>' + "".join(items) +
            '<p class="muted" style="margin:10px 0 0;font-size:12px">'
            "New token replaces the node's credential immediately, so update the node after. "
            "Remove deletes its history and cannot be undone.</p></div>")


def _add_form(csrf: str) -> str:
    return (
        '<div class="card"><h2>Add a node</h2>'
        '<form method="post" action="/actions/node/add">'
        f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
        '<div class="fields">'
        '<div><label for="node_id">Node name</label>'
        '<input type="text" id="node_id" name="node_id" placeholder="laptop-erik" required '
        'pattern="[a-z0-9][a-z0-9-]{1,39}" title="lowercase letters, digits and hyphens"></div>'
        '<div><label for="owner">Owner</label>'
        '<input type="text" id="owner" name="owner" placeholder="erik" required></div>'
        '<div><label for="region">Region</label>'
        '<input type="text" id="region" name="region" placeholder="us-west"></div>'
        '<label class="check"><input type="checkbox" name="rc_expected" value="1"> '
        'Alert if Remote Control stops</label>'
        '</div>'
        '<button class="primary" type="submit">Add node</button>'
        '</form></div>')


def render_add_result(node_id: str, token: str, cfg: Config, owner: str = "") -> str:
    """Shown once, right after a node is created. This is the only time the token exists."""
    url = cfg.public_url or f"http://127.0.0.1:{cfg.bind_port}"
    steps = (f"CCFLEET_URL={url}\n"
             f"CCFLEET_NODE_ID={node_id}\n"
             f"CCFLEET_NODE_TOKEN={token}")
    # Every value below is quoted before it reaches a command an operator will paste
    # as root. The store validates these too; this is the second line of defence.
    install_cmd = (
        "curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/install.sh \\\n"
        "  | sudo bash -s -- \\\n"
        f"      --server {shq(url)} \\\n"
        f"      --node {shq(node_id)} \\\n"
        f"      --token {shq(token)} \\\n"
        f"      --owner {shq(owner) if owner else '<owner>'}")
    if cfg.bypass_by_default:
        install_cmd += " \\\n      --bypass-permissions"
    bypass_note = (
        "<p class=\"muted\"><strong>This fleet runs without permission prompts.</strong> "
        "<code>--bypass-permissions</code> is in the command above because "
        "<code>CCFLEET_BYPASS_BY_DEFAULT</code> is set on this server. The owner of this node "
        "also has passwordless sudo, so with prompts off nothing stands between a tool call and "
        "root. Drop the flag for a node where that is not wanted.</p>"
        if cfg.bypass_by_default else "")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>ccfleet · {escape(node_id)} added</title><style>{CSS}</style></head><body>"
        f"<h1>{escape(node_id)} added</h1>"
        "<div class=\"ok-banner\">Copy the three lines below now. The token is shown once "
        "and is not stored in readable form.</div>"
        "<div class=\"card\"><h2>1. Run this on a fresh server, as root</h2>"
        f"<pre>{escape(install_cmd)}</pre>"
        "<p class=\"muted\">It hardens the machine, installs Claude Code, starts the agent, and "
        "stops at the sign-in. Remote Control is enabled but not started: that needs a login "
        "which does not exist yet, so the owner starts it in step 2. "
        "Add <code>--ssh-key \"ssh-ed25519 …\"</code> "
        "with the owner's public key, or SSH hardening is skipped so nobody is locked out.</p>"
        f"{bypass_note}"
        "<h2>2. The owner signs in, on that machine</h2>"
        "<pre>claude          # choose the claude.ai login, approve, paste the code back\n"
        "/status         # confirms their account, no base URL, no auth token\n"
        "systemctl --user start claude-remote-control.service   # once, for claude.ai access</pre>"
        "<p class=\"muted\">Nobody else can do this step: a subscription login has to complete "
        "through Anthropic's own flow. Everything before it is the command above.</p>"
        "<h2>If you would rather not paste a token around</h2>"
        f"<pre>{escape(steps)}</pre>"
        "<p class=\"muted\">Those three lines are what the command writes to "
        "<code>~/.config/ccfleet/agent.env</code>; you can place them by hand and run "
        "<code>node/setup-owner.sh</code> instead.</p>"
        "</div>"
        "<p><a class=\"back\" href=\"/\">&larr; back to the fleet</a></p>"
        "</body></html>")


# Signing in, from the console. The server carries a URL back and a code
# forward; the credential itself is written by the CLI on the node and never
# comes near this process.
LOGIN_WORDS = {
    "requested": "Starting on the node\u2026",
    "url_ready": "Open the link, approve, then paste the code below.",
    "code_sent": "Code sent to the node. Waiting for it to finish\u2026",
}


def _signin_html(rows: list[Mapping[str, Any]], csrf: str,
                 logins: Mapping[str, Any]) -> str:
    """One block per node: start a sign-in, or carry the one in flight forward."""
    if not rows:
        return ""
    items = []
    for row in rows:
        node = escape(row["id"])
        login = logins.get(row["id"]) or {}
        state = login.get("state") or ""

        def form(action: str, inner: str, label: str, cls: str = "", node=node) -> str:
            return (f'<form class="inline" method="post" '
                    f'action="/actions/node/{node}/{action}">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
                    f'<button class="{cls}" type="submit">{escape(label)}</button></form>')

        if not state:
            signed_in = row.get("credentials_present")
            status = ("signed in" if signed_in else
                      "not signed in" if signed_in is False else "unknown")
            body = (f'<span class="muted">{escape(status)}</span> '
                    + form("login-start",
                           '<input type="email" name="email" placeholder="email (optional)">',
                           "Sign in"))
        else:
            body = f'<span class="muted">{escape(LOGIN_WORDS.get(state, state))}</span>'
            url = login.get("url") or ""
            # Checked again here: a row written before this rule existed, or by
            # anything but the path above, must still not become a live link.
            if is_login_url(url) and state in ("url_ready", "code_sent"):
                # The node supplied this. It is escaped and its full text is shown,
                # so nobody is asked to trust a link whose target they cannot read.
                body += (f'<div><a href="{escape(url)}" target="_blank" '
                         f'rel="noopener noreferrer">{escape(url)}</a></div>')
            if state == "url_ready":
                body += form("login-code",
                             '<input type="text" name="code" placeholder="paste the code" '
                             'autocomplete="off" required>', "Send code")
            body += " " + form("login-cancel", "", "Cancel", cls="danger")
        items.append(f'<div class="manage-row"><div class="manage-name">{node}</div>'
                     f'<div class="actions">{body}</div></div>')
    return ('<div class="card"><h2>Sign in</h2>' + "".join(items) +
            '<p class="muted" style="margin:10px 0 0;font-size:12px">'
            "Starting a sign-in runs Claude Code's own login on the node. The credential is "
            "written there and never reaches this server; only the verification URL and the "
            "code you paste pass through, and both are discarded when it finishes.</p></div>")


def render_dashboard(rows: list[Mapping[str, Any]], alerts: list[Mapping[str, Any]],
                     now: float, cfg: Config, csrf: str = "", who: Any = None,
                     logins: Optional[Mapping[str, Any]] = None) -> str:
    # who is None for callers that predate per-user accounts, which are all
    # operator-side, so the default is the full-privilege view.
    is_admin = who is None or getattr(who, "is_admin", True)
    empty = ("No nodes yet. Use the form below." if is_admin else
             "No nodes are assigned to you yet. Your operator adds them.")
    body_rows = "".join(_row_html(r, now) for r in rows) or (
        f'<tr><td colspan="10" class="muted">{escape(empty)}</td></tr>')
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
        f"heartbeat max age {cfg.heartbeat_max_age_s // 60} min · refreshes every minute"
        + (f" · signed in as <strong>{escape(str(getattr(who, 'label', '')))}</strong>"
           f"{'' if is_admin else ' · showing only your nodes'}" if who is not None else "")
        + "</p>"
        "<div class=\"wrap\"><table><thead><tr><th>Status</th><th>Node</th><th>Last seen</th>"
        "<th>Claude Code</th><th>Egress IP</th><th>Disk</th><th>Load</th><th>Login</th>"
        "<th>Remote Control</th><th>Open alerts</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
        f"<h2>Open alerts</h2><ul class=\"alerts\">{alert_items}</ul>"
        # Management is the operator's. An owner sees their nodes and nothing to
        # press, which is why they get no CSRF token either: there is no form.
        # The sign-in card belongs to whoever owns the node, admin or not: needing
        # the operator to sign you in would only move the bottleneck.
        + (_signin_html(rows, csrf, logins or {}) if csrf else "")
        + (_manage_html(rows, csrf) + _add_form(csrf) if csrf and is_admin else "")
        + "</body></html>"
    )
