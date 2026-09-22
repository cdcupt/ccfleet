"""Server-rendered fleet dashboard. Every value is HTML-escaped; no scripts."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from shlex import quote as shq
from typing import Any, Optional

from .config import Config
from .desired import is_channel, is_login_url

LEVEL_ORDER = {"ok": 0, "warn": 1, "critical": 2}

# How often the page comes back for more. Fast enough while a sign-in is moving
# that a step finishing on the node shows up almost at once; slow the rest of
# the time, because nothing else here changes minute to minute.
IDLE_REFRESH_S = 60
ACTIVE_REFRESH_S = 4

CSS = """
/* Tokens. Light is the bare :root; dark redefines only the tokens, guarded so an
   explicit light choice still wins. Nothing below hard-codes a colour. */
:root{
--bg:#f2f5f8;--panel:#fff;--inset:#eef2f7;--ink:#0f1620;--muted:#586372;
--rule:#dce3eb;--rule-soft:#e9eef4;--acc:#1d4ed8;--acc-soft:#e7edfc;
--ok:#0f7038;--ok-bg:#e2f3e8;--warn:#94540a;--warn-bg:#fbeedb;
--bad:#a62a1e;--bad-bg:#fce4e1;--off:#6b7684;--off-bg:#eceff3;
--on-acc:#fff;--shadow:0 1px 2px rgba(15,22,32,.05),0 1px 12px rgba(15,22,32,.04);
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#0b0f15;--panel:#131a23;--inset:#1b232e;--ink:#e9edf3;--muted:#96a1b0;
--rule:#232d39;--rule-soft:#1c242f;--acc:#7ea3ff;--acc-soft:#182238;
--ok:#48c97d;--ok-bg:#10301e;--warn:#e6a648;--warn-bg:#33260d;
--bad:#ff9086;--bad-bg:#3a1512;--off:#8b95a3;--off-bg:#1a212b;--on-acc:#0b0f15;
--shadow:0 1px 2px rgba(0,0,0,.4)}}

*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans);
padding:0;-webkit-font-smoothing:antialiased}
.page{max-width:1200px;margin:0 auto;padding-block:26px 44px;padding-left:20px;
padding-right:20px}
code,.mono,td.num,.v{font-family:var(--mono);font-variant-numeric:tabular-nums}

/* Masthead: what this is, then the fleet in one glance. */
.mast{display:flex;align-items:flex-end;justify-content:space-between;gap:16px 24px;
flex-wrap:wrap;margin:0 0 18px}
h1{font-size:27px;line-height:1.1;letter-spacing:-.02em;margin:0;font-weight:680}
h1 .dot{color:var(--acc)}
.sub{color:var(--muted);font-size:13px;margin:5px 0 0}
.sub strong{color:var(--ink);font-weight:600}
.strip{display:grid;grid-template-columns:repeat(4,minmax(74px,1fr));gap:8px;
width:100%;max-width:420px}
.tile{background:var(--panel);border:1px solid var(--rule);border-radius:9px;
padding:8px 10px;border-top:2px solid var(--rule)}
.tile b{display:block;font-family:var(--mono);font-size:20px;line-height:1.15;
font-weight:600;font-variant-numeric:tabular-nums}
.tile span{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
.tile.ok{border-top-color:var(--ok);background:var(--ok-bg)}.tile.ok b{color:var(--ok)}
.tile.warn{border-top-color:var(--warn);background:var(--warn-bg)}
.tile.warn b{color:var(--warn)}
.tile.critical{border-top-color:var(--bad);background:var(--bad-bg)}
.tile.critical b{color:var(--bad)}
.tile.zero b{color:var(--muted);font-weight:400}
.tile.zero{border-top-color:var(--rule)}

h2{font-size:13px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
margin:28px 0 9px;font-weight:650}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:12px;
padding:4px 18px 14px;margin:0;box-shadow:var(--shadow)}
.card h2{margin:14px 0 10px}
.card.form{max-width:760px;margin-top:26px}
.note{color:var(--muted);font-size:12px;line-height:1.5;margin:12px 0 0;
padding-top:11px;border-top:1px solid var(--rule-soft)}
.muted{color:var(--muted)}.small{font-size:12px}

/* The table. A rail on the left edge of each row carries state as form, so a
   glance down the column finds trouble without reading any number. */
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--rule);
border-radius:12px;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;min-width:880px;font-size:14px}
th,td{padding:11px 13px;text-align:left;border-bottom:1px solid var(--rule-soft);
vertical-align:top;white-space:nowrap}
th{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
background:var(--inset);font-weight:650;border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{padding-left:16px}
tbody tr:last-child td{border-bottom:0}
tbody tr{border-left:3px solid transparent}
tbody tr.r-ok{border-left-color:var(--ok)}
tbody tr.r-warn{border-left-color:var(--warn)}
tbody tr.r-critical{border-left-color:var(--bad)}
tbody tr.r-disabled{border-left-color:var(--rule)}
tbody tr.r-disabled td{opacity:.62}
td.wrap{white-space:normal;min-width:130px}
.node-id{font-family:var(--mono);font-weight:600;font-size:14px}
.pill{display:inline-block;font-size:11.5px;padding:2px 9px;border-radius:999px;
border:1px solid;font-weight:600;letter-spacing:.01em}
.pill.ok{color:var(--ok);border-color:var(--ok);background:var(--ok-bg)}
.pill.warn{color:var(--warn);border-color:var(--warn);background:var(--warn-bg)}
.pill.critical{color:var(--bad);border-color:var(--bad);background:var(--bad-bg)}
.pill.disabled{color:var(--off);border-color:var(--rule);background:var(--off-bg)}
.bad-text{color:var(--bad);font-weight:600}

/* Alerts: severity on the edge, rule name in mono, prose in sans. */
.alert{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;padding:11px 0;
border-bottom:1px solid var(--rule-soft)}
.alert:last-of-type{border-bottom:0}
.alert-rule{font-family:var(--mono);font-size:13px;font-weight:600}
.alert-msg{font-size:13.5px;min-width:0;overflow-wrap:anywhere}
.quiet{color:var(--muted);font-size:13.5px;padding:12px 0 4px;margin:0}

/* Controls */
form.inline{display:inline;margin:0}
button,.btn{font:inherit;font-size:12px;padding:5px 11px;border-radius:8px;
border:1px solid var(--rule);background:var(--panel);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--acc);color:var(--acc)}
button.danger:hover{border-color:var(--bad);color:var(--bad)}
button.primary{background:var(--acc);color:var(--on-acc);border-color:var(--acc);
font-size:13px;padding:8px 17px;font-weight:600}
button.primary:hover{filter:brightness(1.08);color:var(--on-acc)}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
a{color:var(--acc)}
label{display:block;font-size:11px;letter-spacing:.04em;text-transform:uppercase;
color:var(--muted);margin:0 0 5px;font-weight:600}
input[type=text],input[type=email]{font:inherit;font-size:14px;padding:8px 11px;
border-radius:8px;border:1px solid var(--rule);background:var(--bg);color:var(--ink);
width:100%;max-width:280px}
input[type=text]:focus,input[type=email]:focus{border-color:var(--acc);outline:none}
.fields{display:flex;flex-wrap:wrap;gap:14px 18px;margin:0 0 16px}
.check{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--ink);
margin-top:20px;text-transform:none;letter-spacing:0}
.actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.row-line{display:flex;align-items:center;justify-content:space-between;gap:14px;
flex-wrap:wrap;padding:11px 0;border-bottom:1px solid var(--rule-soft)}
.row-line:last-of-type{border-bottom:0}
.row-name{font-family:var(--mono);font-weight:600;font-size:13.5px}
.row-line.stacked{display:block}
.row-line.stacked .row-name{margin-bottom:8px}
.login-url{display:block;font-family:var(--mono);font-size:12px;word-break:break-all;
margin:7px 0;line-height:1.45}
.login-say{font-size:13px;margin-right:8px}
pre{background:var(--inset);border:1px solid var(--rule);border-radius:10px;
padding:13px 15px;overflow-x:auto;font-family:var(--mono);font-size:13px;
line-height:1.55;margin:0 0 14px;white-space:pre}
.ok-banner{border:1px solid var(--ok);background:var(--ok-bg);color:var(--ok);
border-radius:12px;padding:13px 17px;margin:0 0 20px;font-weight:500}
a.back{font-size:13px}

/* Usage: who, the two windows, then the trend. */
.usage-row{display:grid;
grid-template-columns:minmax(150px,.9fr) minmax(200px,1fr) minmax(190px,1.1fr);
gap:16px 22px;align-items:start;padding:15px 0;border-bottom:1px solid var(--rule-soft)}
.usage-row:last-of-type{border-bottom:0}
.usage-name{font-family:var(--mono);font-weight:600;font-size:13.5px;min-width:0;
overflow-wrap:anywhere}
.usage-name span{font-family:var(--sans);font-weight:400}
.usage-quota,.usage-spark{min-width:0}
.usage-total{margin-top:9px;font-size:13px}
.usage-total b{font-family:var(--mono);font-size:21px;font-weight:650;
letter-spacing:-.01em;line-height:1.1}
.usage-total span{font-family:var(--sans);font-weight:400}
.usage-meta{font-family:var(--sans);font-weight:400;font-size:12px;margin-top:5px;
line-height:1.45;overflow-wrap:anywhere}
.usage-nums{font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin-top:6px}
svg.spark{display:block;width:100%;height:46px;overflow:visible}
.spark-fill{fill:var(--acc-soft);stroke:none}
.spark-base{stroke:var(--rule);stroke-width:1;vector-effect:non-scaling-stroke}
.spark-line{fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke}
.spark-dot{fill:var(--acc)}

/* One quota window. The bar is the point; the number confirms it. */
.meter{margin:0 0 11px}
.meter:last-child{margin-bottom:2px}
.meter-head{display:flex;justify-content:space-between;align-items:baseline;
font-size:12px;color:var(--muted);margin:0 0 4px}
.meter-pct{font-family:var(--mono);font-weight:700;color:var(--ink);
font-variant-numeric:tabular-nums;font-size:13px}
.meter-track{height:7px;border-radius:99px;background:var(--inset);overflow:hidden;
border:1px solid var(--rule-soft)}
.meter-fill{display:block;height:100%;border-radius:99px;min-width:2px}
.meter-fill.ok{background:var(--acc)}
.meter-fill.warn{background:var(--warn)}
.meter-fill.crit{background:var(--bad)}
.meter-foot{font-size:11px;color:var(--muted);margin-top:3px}

@media (max-width:820px){.usage-row{grid-template-columns:1fr;gap:10px}
.strip{max-width:none}h1{font-size:23px}.page{padding-block:20px 36px}}
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
    days = seconds / 86400
    return f"{days:.1f}d" if days < 10 else f"{days:.0f}d"


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
            "device_token_at": node.get("device_token_at"),
            "credentials_present": creds.get("present"),
            "credentials_mtime": creds.get("mtime"),
            "token_expires_at": creds.get("expires_at"),
            "subscription_type": creds.get("subscription_type"),
            "remote_control": (payload.get("remote_control") or {}).get("state"),
            "rc_expected": node["rc_expected"],
            # What the node did about its pin last time it was asked. A silent
            # reconcile is indistinguishable from one that never ran.
            "last_upgrade": ((payload.get("reconcile") or {}).get("upgrade") or None),
            "usage": payload.get("usage") or {},
            "quota": payload.get("quota") or {},
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
    pinned = row["pinned_version"]
    # Only say the pin when it tells you something. Repeating it beside an equal
    # installed version rendered "2.1.278 2.1.278", which reads as a glitch.
    if pinned and is_channel(pinned):
        version += f' <span class="muted">{escape(pinned)}</span>'
    elif pinned and row["claude_version"] != pinned:
        drift = pinned + " \u2260 pinned"
        version += f' <span class="muted">{escape(drift)}</span>'
    # A drifted version with no explanation reads as "not tried yet". Say when the
    # node tried and failed, because that is the case an operator must act on.
    upgrade = row.get("last_upgrade") or {}
    if upgrade.get("ok") is False:
        detail = escape(str(upgrade.get("error") or "")[:120])
        version += (f'<br><span class="bad-text">upgrade to '
                    f'{escape(str(upgrade.get("to") or "?"))} failed</span>'
                    + (f' <span class="muted">{detail}</span>' if detail else ""))
    creds = row["credentials_present"]
    cred_text = ("unknown" if creds is None else ("missing" if creds is False else
                 f"refreshed {_age(now, row['credentials_mtime'])} ago"))
    if row["subscription_type"]:
        # The plan is a label on the login, not a qualifier on the time. Trailing
        # it read as "refreshed 10m ago (max)", where (max) looks like it modifies
        # the age; leading it reads as what it is.
        cred_text = f"{row['subscription_type']} \u00b7 {cred_text}"
    rc = row["remote_control"] or "-"
    # Nothing is expected of a node that is switched off, so saying so is noise.
    if row["rc_expected"] and row["enabled"]:
        rc += " (expected)"
    return (
        f'<tr class="r-{escape(row["status"])}">'
        f"<td>{_pill(row['status'])}</td>"
        f'<td><span class="node-id">{escape(row["id"])}</span>'
        f"<br><span class=\"muted\">{escape(row['owner'])}"
        f" · {escape(row['region'] or '-')}</span></td>"
        f"<td class=\"num\">{escape(_age(now, row['last_seen_ts']))}</td>"
        f'<td class="v">{version}</td>'
        f"<td><code>{_fmt(row['egress_ip'])}</code></td>"
        f"<td class=\"num\">{_fmt(row['disk_used_pct'], '%')}</td>"
        f"<td class=\"num\">{_fmt(row['load1'])}</td>"
        f"<td>{escape(cred_text)}<br><span class=\"muted\">token "
        f"{escape(_in(now, row['token_expires_at']))}</span></td>"
        f"<td>{escape(rc)}</td>"
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
        items.append(f'<div class="row-line"><div class="row-name">{node}'
                     f'<span class="muted"> · {escape(row["owner"])}</span></div>'
                     f'<div class="actions">{"".join(buttons)}</div></div>')
    return ('<h2 id="manage">Manage nodes</h2><div class="card">' + "".join(items) +
            '<p class="note">'
            "New token replaces the node's credential immediately, so update the node after. "
            "Remove deletes its history and cannot be undone.</p></div>")


def _add_form(csrf: str) -> str:
    return (
        '<h2 id="add-node">Add a node</h2><div class="card form">'
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


def render_token_result(node_id: str, token: str, cfg: Config, owner: str = "") -> str:
    """The minted credential, for as long as the request it belongs to lasts.

    It was minted on the node from the account that node is signed in as and
    rode up on one heartbeat. It stays readable until the request expires or
    somebody says they are done with it, which is what lets a second machine
    have the same token without minting another. Nothing keeps it after that:
    not this server, not the node.
    """
    if not token:
        return (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>ccfleet</title>"
            f"<style>{CSS}</style></head><body><div class=\"page\">"
            "<h1>Nothing to show</h1>"
            "<p class=\"sub\">No token is waiting for this node. Either it was finished "
            "with, or the request expired. Start a new one from the fleet page.</p>"
            "<p><a class=\"back\" href=\"/\">&larr; back to the fleet</a></p>"
            "</div></body></html>")
    who = f" for {escape(owner)}" if owner else ""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ccfleet \u00b7 device token</title>"
        f"<style>{CSS}</style></head><body><div class=\"page\">"
        f"<h1>Device token{who}</h1>"
        "<p class=\"sub\">Minted on <strong>" + escape(node_id) + "</strong>, from the "
        "account that node is signed in as. Good for one year.</p>"
        "<div class=\"ok-banner\">You can come back and show this again while the "
        "request lasts, so a second machine can have the same token. Press "
        "<strong>Done with it</strong> on the fleet page when you have finished, or "
        "leave it and it expires on its own.</div>"
        f"<pre>{escape(token)}</pre>"
        "<div class=\"card\">"
        "<h2>Put it on a machine</h2>"
        "<p>One command, on any Mac or Linux box. It asks for the token and hides "
        "what you paste.</p>"
        "<pre>bash -c \"$(curl -fsSL https://raw.githubusercontent.com/cdcupt/"
        "ccfleet/main/laptop/ccfleet-connect.sh)\"</pre>"
        "<p class=\"muted\">Run through <code>bash -c</code> rather than piped into a "
        "shell, so the prompt can still read from your terminal &mdash; a pipe would "
        "take the keyboard away from it. It installs itself to "
        "<code>~/.local/bin</code> on the way past.</p>"
        "<p class=\"muted\">Then <code>claude</code> works there on that machine\u2019s own "
        "files, with no login. <code>ccfleet-connect --status</code> checks it, "
        "<code>--remove</code> undoes it.</p>"
        "<p class=\"note\">Scope is <code>user:inference</code> only, which is Anthropic\u2019s "
        "limit on long-lived tokens, not ours. So it runs Claude Code and it cannot drive "
        "Remote Control \u2014 that needs a full sign-in, which is what the Sign in card does. "
        "Revoke it from the Claude account it belongs to; there is nothing to revoke here, "
        "because nothing here kept it.</p>"
        "</div>"
        "<p><a class=\"back\" href=\"/\">&larr; back to the fleet</a></p>"
        "</div></body></html>")


# Signing in, from the console. The server carries a URL back and a code
# forward; the credential itself is written by the CLI on the node and never
# comes near this process.
LOGIN_WORDS = {
    "requested": "Asking the node\u2026 it checks in every few minutes, so this can "
                 "take a moment. Leave the page open.",
    "url_ready": "Open the link, approve, then paste the code below.",
    "code_sent": "Code sent to the node. Waiting for it to finish\u2026",
}
# The same three steps, said for the flow that ends in a credential you carry
# away rather than one written on the node.
TOKEN_WORDS = {
    # The node reports on a timer, so it can be a few minutes before it even
    # hears the request. A bare "asking..." with no sense of that reads as
    # broken and gets abandoned, which is exactly what happened.
    "requested": "Asking the node\u2026 it checks in every few minutes, so this can "
                 "take a moment. Leave the page open.",
    "ready_note": "Your token is ready \u2014 show it as often as you need while "
                  "this lasts.",
    "url_ready": "Open the link, approve, then paste the code below.",
    "code_sent": "Code sent. Minting the token\u2026",
    "ready": "Your token is ready.",
}


def _token_html(rows: list[Mapping[str, Any]], csrf: str,
                logins: Mapping[str, Any], now: float) -> str:
    """Mint a credential for a machine that is not a node.

    The node is already signed in, so it can mint one on request. This card is
    how that is asked for and collected without anybody opening a terminal,
    which was the last thing still requiring SSH.
    """
    if not rows:
        return ""
    items = []
    # A flow needing a person goes first; the rest are just buttons.
    ordered = sorted(rows, key=lambda r: not (logins.get(r["id"]) or {}).get("state"))
    for row in ordered:
        node = escape(row["id"])
        login = logins.get(row["id"]) or {}
        # This card owns only the token flow; a sign-in in flight belongs to the
        # card above and must not be shown twice or cancelled from here.
        state = login.get("state") or "" if login.get("kind") == "token" else ""

        def form(action: str, inner: str, label: str, cls: str = "", node=node) -> str:
            return (f'<form class="inline" method="post" '
                    f'action="/actions/node/{node}/{action}">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
                    f'<button class="{cls}" type="submit">{escape(label)}</button></form>')

        if not state:
            # Say if one has been issued before. The flow deletes itself when it
            # finishes, so without this the card after a success is identical to
            # the card before you ever started — and someone reasonably wonders
            # whether anything happened.
            issued = row.get("device_token_at")
            if isinstance(issued, (int, float)) and issued > 0:
                said = (f'<span class="pill ok">last issued {escape(_age(now, issued))} '
                        f'ago</span> ')
                label = "Get another"
            else:
                said = '<span class="muted small">for a laptop, desktop or phone</span> '
                label = "Get a device token"
            body = said + form("token-start", "", label)
        elif state == "ready":
            # Shown as often as you like while the attempt lasts, because a
            # second machine needs the same token and minting another for it
            # is a worse answer than reading this one again.
            body = (f'<span class="pill ok">{escape(TOKEN_WORDS["ready"])}</span> '
                    + form("token-show", "", "Show it", cls="primary")
                    + " " + form("token-done", "", "Done with it"))
        else:
            body = f'<span class="login-say">{escape(TOKEN_WORDS.get(state, state))}</span>'
            url = login.get("url") or ""
            if is_login_url(url) and state in ("url_ready", "code_sent"):
                body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                         f'rel="noopener noreferrer">{escape(url)}</a>')
            if state == "url_ready":
                body += form("login-code",
                             '<input type="text" name="code" placeholder="paste the code" '
                             'autocomplete="off" required>', "Send code")
            body += " " + form("login-cancel", "", "Cancel", cls="danger")
        cls = "row-line stacked" if state and state != "ready" else "row-line"
        items.append(f'<div class="{cls}"><div class="row-name">{node}</div>'
                     f'<div class="actions">{body}</div></div>')
    return ('<h2 id="device-tokens">Device tokens</h2><div class="card">' + "".join(items) +
            '<p class="note">'
            "A device token lets <code>claude</code> run on your own machine, on that "
            "machine's own files, with no login. It is minted on the node from the account "
            "that node is signed in as, and shown here for as long as the request lasts "
            "&mdash; so a second machine can have the same one &mdash; then forgotten. One "
            "year, inference scope &mdash; Anthropic's limit, not ours, which is why it "
            "cannot drive Remote Control.</p></div>")


def _signin_html(rows: list[Mapping[str, Any]], csrf: str,
                 logins: Mapping[str, Any]) -> str:
    """One block per node: start a sign-in, or carry the one in flight forward."""
    if not rows:
        return ""
    # A sign-in in flight is the only row here anyone has to act on. Settled rows
    # are reference; put the work first rather than making someone find it.
    def own(node_id: str) -> Mapping[str, Any]:
        """The sign-in for this node, or nothing if the row is a token flow.

        One table drives both flows, so each card has to say which rows are
        its own. Without this a token in flight showed up here too, with a
        Cancel button that would kill it from either place.
        """
        login = logins.get(node_id) or {}
        return {} if login.get("kind") == "token" else login

    ordered = sorted(rows, key=lambda r: not own(r["id"]).get("state"))
    items = []
    for row in ordered:
        node = escape(row["id"])
        login = own(row["id"])
        state = login.get("state") or ""

        def form(action: str, inner: str, label: str, cls: str = "", node=node) -> str:
            return (f'<form class="inline" method="post" '
                    f'action="/actions/node/{node}/{action}">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
                    f'<button class="{cls}" type="submit">{escape(label)}</button></form>')

        if not state:
            signed_in = row.get("credentials_present")
            # "signed in" beside a button labelled "Sign in" read as a
            # contradiction. Say the state as a state, and let the button say
            # what pressing it would do to that state.
            status, tone, verb = (("signed in", "ok", "Sign in again") if signed_in else
                                  ("not signed in", "warn", "Sign in") if signed_in is False
                                  else ("unknown", "disabled", "Sign in"))
            field = ("" if signed_in else
                     '<input type="email" name="email" placeholder="email (optional)">')
            body = (f'<span class="pill {tone}">{escape(status)}</span> '
                    + form("login-start", field, verb))
        else:
            body = (f'<span class="login-say">'
                    f'{escape(LOGIN_WORDS.get(state, state))}</span>')
            url = login.get("url") or ""
            # Checked again here: a row written before this rule existed, or by
            # anything but the path above, must still not become a live link.
            if is_login_url(url) and state in ("url_ready", "code_sent"):
                # The node supplied this. It is escaped and its full text is shown,
                # so nobody is asked to trust a link whose target they cannot read.
                body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                         f'rel="noopener noreferrer">{escape(url)}</a>')
            if state == "url_ready":
                body += form("login-code",
                             '<input type="text" name="code" placeholder="paste the code" '
                             'autocomplete="off" required>', "Send code")
            body += " " + form("login-cancel", "", "Cancel", cls="danger")
        # A sign-in in flight carries a sentence, a long URL and two controls.
        # Held on one flex line those wrap into a shape nobody designed, so give
        # it a block of its own and keep the one-line form for settled states.
        cls = "row-line stacked" if state else "row-line"
        items.append(f'<div class="{cls}"><div class="row-name">{node}</div>'
                     f'<div class="actions">{body}</div></div>')
    return ('<h2 id="sign-in">Sign in</h2><div class="card">' + "".join(items) +
            '<p class="note">'
            "Starting a sign-in runs Claude Code's own login on the node. The credential is "
            "written there and never reaches this server; only the verification URL and the "
            "code you paste pass through, and both are discarded when it finishes.</p></div>")


def _plural(n: Any, word: str) -> str:
    """"1 sessions" is the sort of thing that makes a page look unfinished."""
    count = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _human_tokens(n: Any) -> str:
    """Token counts run to millions; a raw integer is unreadable at a glance."""
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n <= 0:
        return "0"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= limit:
            trimmed = n / limit
            return f"{trimmed:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def _sparkline(series: list[Mapping[str, Any]], width: int = 240, height: int = 38,
               unit: str = "day") -> str:
    """Tokens over time as a filled area. No script, no library, sized by viewBox."""
    points = [p for p in series if isinstance(p.get("tokens"), (int, float))]
    if not points:
        return '<span class="muted">no activity yet</span>'
    values = [float(p["tokens"]) for p in points]
    if len(values) < 2:
        # One day is not a trend. Drawn, it became a flat line across the whole
        # frame, which reads as a full bar rather than as a single reading.
        return '<span class="muted">one day so far</span>'
    peak = max(values) or 1.0
    step = width / max(len(values) - 1, 1)
    # y is inverted: SVG grows downward, a chart grows upward. A flat series has
    # no shape to show, and normalising it against its own peak would pin it to
    # the top of the frame — the one height that implies a maximum. Sit it in
    # the middle instead, where it reads as "unvarying" and not as "full".
    flat = peak == min(values)
    coords = [(i * step, height - 3 - (0.5 if flat else v / peak) * (height - 8))
              for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"0,{height} " + line + f" {coords[-1][0]:.1f},{height}"
    last_x, last_y = coords[-1]
    label = (f"{len(values)} {unit}(s) of token use, peak {_human_tokens(peak)}, "
             f"latest {_human_tokens(values[-1])}")
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{escape(label)}" preserveAspectRatio="none">'
            # A baseline gives the area something to sit on, so a quiet week
            # reads as low rather than as a chart that failed to draw.
            f'<line class="spark-base" x1="0" y1="{height - 0.5}" '
            f'x2="{width}" y2="{height - 0.5}"/>'
            f'<polygon class="spark-fill" points="{area}"/>'
            f'<polyline class="spark-line" points="{line}"/>'
            f'<circle class="spark-dot" cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5"/></svg>')


def _meter(used: Any, label: str, resets: Any) -> str:
    """One quota window as a labelled bar.

    Colour carries the same meaning as everywhere else on this page: fine,
    getting close, nearly out. A bare number makes you do that comparison
    yourself, every time you look.
    """
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return ""
    pct = max(0.0, min(100.0, float(used)))
    level = "crit" if pct >= 90 else "warn" if pct >= 75 else "ok"
    foot = f"resets {escape(str(resets))}" if resets else ""
    return (f'<div class="meter"><div class="meter-head">'
            f'<span>{escape(label)}</span>'
            f'<span class="meter-pct">{pct:.0f}%</span></div>'
            f'<div class="meter-track"><i class="meter-fill {level}" '
            f'style="width:{pct:.0f}%"></i></div>'
            f'<div class="meter-foot">{foot}</div></div>')


def _quota_html(row: Mapping[str, Any], now: float) -> str:
    """The two windows an owner actually asks about: this session, this week.

    These belong to the Claude account, not to the node: every device signed
    in as that account spends them. Said above the bars, because beside a
    per-node token count they read as if they were the same thing, and the
    count then looks stuck while the bars move.
    """
    quota = row.get("quota") or {}
    session, week = quota.get("session") or {}, quota.get("week") or {}
    if not session and not week:
        return '<p class="muted small">No window reading yet.</p>'
    bars = ('<div class="usage-nums muted">Claude account &middot; every device</div>'
            + _meter(session.get("used_pct"), "5-hour session", session.get("resets"))
            + _meter(week.get("used_pct"), "This week", week.get("resets")))
    checked = quota.get("checked_at")
    if isinstance(checked, (int, float)) and not isinstance(checked, bool):
        bars += f'<p class="muted small">read {escape(_age(now, checked))} ago</p>'
    return bars


def _usage_html(rows: list[Mapping[str, Any]], now: float) -> str:
    """Per-node, per-account token use, counted from transcripts on each node."""
    def reporting(row: Mapping[str, Any]) -> bool:
        return bool((row.get("usage") or {}).get("total_tokens") or (row.get("quota") or {}))

    live = [r for r in rows if reporting(r)]
    if not live:
        return ""
    # A row of dashes says nothing an operator can act on, and four of them bury
    # the two that matter. Name the quiet nodes in one line instead.
    quiet = [r["id"] for r in rows if not reporting(r)]
    tail = ""
    if quiet:
        names = ", ".join(escape(str(q)) for q in quiet[:8])
        more = f" and {len(quiet) - 8} more" if len(quiet) > 8 else ""
        tail = (f'<p class="quiet">No usage reported yet from <span class="mono">{names}'
                f'</span>{more}.</p>')
    items = []
    for row in live:
        usage = row.get("usage") or {}
        total = usage.get("total_tokens") or 0
        cached = usage.get("cache_read_input_tokens") or 0
        share = f"{cached / total * 100:.0f}%" if total else "-"
        spark, caption = _usage_chart(usage)
        # Which model did the work is left out: it is whatever each person
        # chose in their session, and can change turn by turn.
        items.append(
            f'<div class="usage-row"><div class="usage-name">{escape(row["id"])}'
            f'<span class="muted"> &middot; {escape(row["owner"])}</span>'
            f'<div class="usage-total"><b>{escape(_human_tokens(total))}</b>'
            f'<span class="muted"> tokens on this node, last '
            f'{escape(_usage_span(usage))}</span></div>'
            f'<div class="usage-meta muted">'
            f'{escape(_plural(usage.get("sessions") or 0, "session"))} &middot; '
            f'{escape(share)} cached</div></div>'
            f'<div class="usage-quota">{_quota_html(row, now)}</div>'
            f'<div class="usage-spark">{spark}{caption}</div></div>')
    return ('<h2>Usage and quota</h2><div class="card">' + "".join(items) + tail +
            '<p class="note">'
            "Windows come from <code>/usage</code> inside a Claude Code session on the node, "
            "read on a slow schedule &mdash; Claude Code reporting on itself, not a usage "
            "endpoint. Token counts come from the transcripts it writes there. Conversation "
            "content never leaves the node; only counts do.</p></div>")


def _usage_span(usage: Mapping[str, Any]) -> str:
    """How far back the token count reaches, in days: "7 days"."""
    hours, days = usage.get("window_hours"), usage.get("window_days")
    count = int(hours // 24) if hours else int(days) if days else 0
    return _plural(count, "day") if count else "?"


def _usage_chart(usage: Mapping[str, Any]) -> tuple[str, str]:
    """The chart and its caption. Hourly from agents that report it, daily
    from older ones, and a plain sentence when the window holds nothing."""
    hourly = usage.get("by_hour")
    if isinstance(hourly, Mapping) and hourly.get("tokens"):
        if not usage.get("total_tokens"):
            # A flat line along the floor reads as a chart that failed to draw.
            return ('<span class="muted">nothing on this node in the last '
                    f'{escape(_usage_span(usage))}</span>'), ""
        spark = _sparkline([{"tokens": v} for v in hourly["tokens"]], unit="hour")
        unit = "per hour"
    else:
        spark = _sparkline(usage.get("by_day") or [])
        unit = "per day"
    # The caption names the axes of a chart. With no chart drawn it sat under a
    # sentence, captioning nothing.
    caption = (f'<div class="usage-nums muted">tokens {unit}, last '
               f'{escape(_usage_span(usage))}</div>' if "<svg" in spark else "")
    return spark, caption


TILES = (("ok", "healthy"), ("warn", "warning"), ("critical", "critical"),
         ("disabled", "disabled"))


def _strip_html(counts: Mapping[str, int]) -> str:
    """The fleet in one glance, before any detail.

    A count buried in a sentence ("ok 2") makes you read to find out whether
    anything is wrong. Four tiles answer that from across the room, and a zero
    stays quiet rather than competing with the number that matters.
    """
    tiles = []
    for key, label in TILES:
        n = int(counts.get(key) or 0)
        cls = f"tile {key}" if n else "tile zero"
        tiles.append(f'<div class="{cls}"><b>{n}</b><span>{escape(label)}</span></div>')
    return f'<div class="strip">{"".join(tiles)}</div>'


def render_dashboard(rows: list[Mapping[str, Any]], alerts: list[Mapping[str, Any]],
                     now: float, cfg: Config, csrf: str = "", who: Any = None,
                     logins: Optional[Mapping[str, Any]] = None) -> str:
    # who is None for callers that predate per-user accounts, which are all
    # operator-side, so the default is the full-privilege view.
    is_admin = who is None or getattr(who, "is_admin", True)
    empty = ("No nodes yet. Use the form below." if is_admin else
             "No nodes are assigned to you yet. Your operator adds them.")
    body_rows = "".join(_row_html(r, now) for r in rows) or (
        f'<tr><td colspan="9" class="muted">{escape(empty)}</td></tr>')
    alert_items = "".join(
        f'<div class="alert">{_pill(a["level"])}'
        f'<span class="alert-rule">{escape(a["node_id"])} · {escape(a["rule"])}</span>'
        f'<span class="alert-msg">{escape(a["message"])}</span>'
        f'<span class="muted small">{escape(_age(now, a["opened_at"]))} ago</span></div>'
        for a in alerts) or (
        '<p class="quiet">Nothing open. Every node is inside its thresholds.</p>')
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    # An inline favicon keeps the tab recognisable without a second request, and
    # without this page depending on anything it did not render itself.
    icon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='4' fill='%231d4ed8'/%3E"
            "%3Ccircle cx='5' cy='8' r='1.7' fill='white'/%3E"
            "%3Ccircle cx='11' cy='5' r='1.7' fill='white'/%3E"
            "%3Ccircle cx='11' cy='11' r='1.7' fill='white'/%3E%3C/svg%3E")
    # How often to come back, decided by what the page is currently showing.
    #
    # Idle, a minute is plenty. Mid-flow it is not: a step completes on the node
    # in seconds and then sits unseen for the rest of the minute, which reads as
    # nothing happening. And while someone is being asked to paste a code, any
    # reload at all lands mid-typing and throws away what they had — that is the
    # one state where nothing can arrive anyway, so there is nothing to fetch
    # and everything to lose.
    states = {(login or {}).get("state") for login in (logins or {}).values()}
    if "url_ready" in states:
        auto_refresh = ""
    elif states & {"requested", "code_sent"}:
        auto_refresh = f'<meta http-equiv="refresh" content="{ACTIVE_REFRESH_S}">'
    else:
        auto_refresh = f'<meta http-equiv="refresh" content="{IDLE_REFRESH_S}">'
    # Say which of the three the page is doing, so a reload that does not come
    # is a stated choice rather than something that looks broken.
    cadence = ("waiting for you to paste a code" if not auto_refresh else
               "keeping up with a sign-in" if str(ACTIVE_REFRESH_S) in auto_refresh else
               "refreshing itself every minute")
    whoami = ""
    if who is not None:
        whoami = (f" · signed in as <strong>{escape(str(getattr(who, 'label', '')))}</strong>"
                  + ("" if is_admin else " · showing only your nodes"))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{auto_refresh}<title>ccfleet</title>'
        f'<link rel="icon" href="{icon}">'
        f"<style>{CSS}</style></head><body><div class=\"page\">"
        '<header class="mast"><div>'
        '<h1>ccfleet<span class="dot">.</span></h1>'
        '<p class="sub">One owner, one account, one node · heartbeat max age '
        f"{cfg.heartbeat_max_age_s // 60} min · {cadence}{whoami}</p></div>"
        + _strip_html(counts) +
        "</header>"
        '<div class="wrap"><table><thead><tr><th>Status</th><th>Node</th><th>Last seen</th>'
        "<th>Claude Code</th><th>Egress IP</th><th>Disk</th><th>Load</th><th>Login</th>"
        "<th>Remote Control</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
        f'<h2>Open alerts</h2><div class="card">{alert_items}</div>'
        # Management is the operator's. An owner sees their nodes and nothing to
        # press, which is why they get no CSRF token either: there is no form.
        # The sign-in card belongs to whoever owns the node, admin or not: needing
        # the operator to sign you in would only move the bottleneck.
        + _usage_html(rows, now)
        + (_signin_html(rows, csrf, logins or {}) if csrf else "")
        + (_token_html(rows, csrf, logins or {}, now) if csrf else "")
        + (_manage_html(rows, csrf) + _add_form(csrf) if csrf and is_admin else "")
        + "</div></body></html>"
    )


def render_account(account: Optional[Mapping[str, Any]], held: int,
                   cfg: Config) -> str:
    """The page a person sees after signing in with Google.

    Deliberately small. It exists so a session authenticates something real
    rather than being a library nothing calls, and so the state that matters
    most — how many slots you may hold — is the first thing on the page. The
    slots themselves, claiming and releasing, are the user site.
    """
    head = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>ccfleet · your account</title><style>{CSS}</style></head><body>")
    if account is None:
        if not cfg.google_ready:
            # Said plainly rather than showing a button that cannot work.
            return (head + "<h1>ccfleet</h1><div class=\"card\">"
                    "<p>Sign-in is not set up on this server.</p>"
                    "<p class=\"muted\">An operator configures "
                    "<code>CCFLEET_GOOGLE_CLIENT_ID</code>, "
                    "<code>CCFLEET_GOOGLE_CLIENT_SECRET</code> and "
                    "<code>CCFLEET_COOKIE_SECRET</code> to turn it on.</p>"
                    "</div></body></html>")
        return (head + "<h1>ccfleet</h1><div class=\"card\">"
                "<p>Sign in to see the slots you hold.</p>"
                "<p><a class=\"btn\" href=\"/auth/google/start?next=/account\">"
                "Continue with Google</a></p>"
                "<p class=\"muted\">We ask Google for your email address and "
                "nothing else.</p></div></body></html>")

    quota = int(account.get("slot_quota") or 0)
    if quota == 0:
        # The common case for somebody who has just signed up, and the one
        # worth explaining: an empty page with no reason given reads as broken.
        allowance = ("<p><strong>You have no slots yet.</strong></p>"
                     "<p class=\"muted\">Slots are assigned by the operator. "
                     "Once one is assigned to you it appears here.</p>")
    else:
        allowance = (f"<p>You may hold <strong>{quota}</strong> "
                     f"{_plural(quota, 'slot')}, and currently hold "
                     f"<strong>{held}</strong>.</p>")
    return (head +
            "<h1>ccfleet</h1><div class=\"card\">"
            f"<p class=\"muted\">Signed in as {escape(str(account.get('email', '')))}</p>"
            f"{allowance}"
            "<form method=\"post\" action=\"/auth/signout\">"
            "<button class=\"btn\" type=\"submit\">Sign out</button></form>"
            "</div></body></html>")
