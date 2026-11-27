"""An owner selects one dedicated slot before choosing a service."""

from collections import Counter
from html import escape
from urllib.parse import urlencode

from . import devin_ui, gateway_ui, home_macs, names, slots, usersite, wallet_access
from .bilingual import label


def tr(en, zh):
    return label(en, zh, inline=True)


def link(url, en, zh, cls="btn"):
    return f'<a class="{cls}" href="{escape(url, quote=True)}">{tr(en, zh)}</a>'


def slot_url(slot_id):
    return home_macs.POOL + "?" + urlencode({"slot": slot_id})


def visible(store, account):
    return [
        s
        for s in store.list_slots(held_by=account["id"])
        if s["state"] in slots.HELD
        and not (
            s.get("private_owner") and not (s.get("home_mac") or account.get("role") == "admin")
        )
    ]


def display(slot):
    return slot.get("home_name") or names.display(slot)


def chooser(held, selected=""):
    options = [
        '<option value="" data-copy-en="Choose a slot" '
        'data-copy-zh="选择槽位">Choose a slot</option>'
    ]
    name_counts = Counter(display(s) for s in held)
    for slot in held:
        value = slot["id"]
        # A neutral stable ID distinguishes Macs that the owner named identically.
        text = display(slot)
        if name_counts[text] > 1:
            text += f" ({value})"
        chosen = " selected" if value == selected else ""
        options.append(
            f'<option value="{escape(value, quote=True)}"{chosen}>{escape(text)}</option>'
        )
    return (
        '<form method="get" action="' + home_macs.POOL + '" class="ds-picker">'
        '<label for="ds-slot">' + tr("Your dedicated slots", "我的专属槽位") + "</label>"
        '<select id="ds-slot" name="slot" required>' + "".join(options) + "</select>"
        '<button class="btn" type="submit">' + tr("Open slot", "打开槽位") + "</button></form>"
    )


def entries(store, cfg, account, slot, now, home=None):
    """No route is offered without the selected holder, claim and service capability."""
    sid = slot["id"]
    owner = account["id"]
    node = store.get_node(slot["node_id"]) or {}
    usable = (
        slot["held_by"] == owner
        and node.get("enabled")
        and node.get("access_host")
        and node.get("ssh_host_key")
        and wallet_access.allowed(store, slot, now=now)
    )
    if slot.get("private_owner"):
        usable = usable and store.slot_inference_allowed(sid, owner)
    gateway = bool(usable and gateway_ui.available(cfg))
    with store._lock:
        latest = store._conn.execute(
            "SELECT ts,payload FROM heartbeats WHERE node_id=? ORDER BY ts DESC,id DESC LIMIT 1",
            (slot["node_id"],),
        ).fetchone()
        if (
            latest
            and max(now - cfg.heartbeat_max_age_s, slot.get("claimed_at") or 0)
            <= latest["ts"]
            <= now
        ):
            import json

            report = usersite._report_for(slot, {"payload": json.loads(latest["payload"])})
        else:
            report = {}
        claude = (
            gateway
            and slot["state"] == slots.ACTIVE
            and (report.get("credentials") or {}).get("logged_in") is True
            and store._gateway_report_ready(
                store._conn,
                slot["node_id"],
                slot["unix_user"],
                private=bool(slot.get("private_owner")),
                now=now,
            )
        )
    if slot.get("home_mac"):
        claude = claude and bool(home and home["tunnel_state"] == "connected")
    native = devin_ui.native_state(store, cfg, account, slot, now=now) if gateway else None
    result = []

    def service(name, ready, unavailable):
        result.append((name, ready, unavailable))

    service(
        "Claude",
        bool(claude),
        (
            "A current connection and Claude sign-in are required.",
            "需要有效连接，并在槽位上登录 Claude。",
        ),
    )
    if cfg.gateway_devin_enabled:
        service(
            "Devin",
            native == "ready",
            ("Choose a hosted slot with Devin enabled.", "请选择已启用 Devin 的托管槽位。"),
        )
    return result


def create_form(slot, csrf, provider):
    fields = {
        "csrf": csrf,
        "slot": slot["id"],
        "claim": slot["gateway_claim_id"],
        "service": provider,
    }
    hidden = "".join(
        f'<input type="hidden" name="{key}" value="{escape(str(v), quote=True)}">'
        for key, v in fields.items()
    )
    return (
        f'<form method="post" action="{home_macs.POOL}/connect">'
        + hidden
        + '<button type="submit" class="btn">'
        + tr("Create connection key", "创建连接密钥")
        + "</button></form>"
    )


def connect(store, cfg, account, session, fields, now):
    if set(fields) != {"csrf", "slot", "claim", "service"}:
        raise home_macs.denied()
    slot = store.get_slot(fields["slot"])
    if not slot or slot["held_by"] != account["id"] or slot["gateway_claim_id"] != fields["claim"]:
        raise home_macs.denied()
    home = (
        home_macs.for_slot(store, account["id"], slot["id"], now) if slot.get("home_mac") else None
    )
    mapping = {"Claude": "anthropic", "Devin": "devin"}
    if fields["service"] not in [
        mapping[name] for name, ready, _ in entries(store, cfg, account, slot, now, home) if ready
    ]:
        raise home_macs.Error(
            409, "This service is unavailable on the selected slot.", "所选槽位上的此服务暂不可用。"
        )
    return gateway_ui.act(
        store,
        cfg,
        account,
        "/account/gateway/create",
        {
            "mode": "priority",
            "provider": fields["service"],
            "name": display(slot)[:60],
            "slot." + slot["id"]: "1",
        },
        now,
        session,
        expected_claim=(slot["id"], fields["claim"]),
    )


def services(store, cfg, account, slot, now, csrf, home):
    sid = slot["id"]
    rows = []
    for name, ready, reason in entries(store, cfg, account, slot, now, home):
        provider = "anthropic" if name == "Claude" else "devin"
        status_pill = (
            '<span class="pill ok">' + tr("Ready", "就绪") + "</span>"
            if ready
            else '<span class="pill disabled">' + tr("Unavailable", "暂不可用") + "</span>"
        )
        actions = (
            '<div class="ds-service-detail">'
            + link(
                f"/account#slot-{sid}-connect-{provider}",
                "Connect my computer",
                "连接我的电脑",
                "btn primary hm-btn-primary",
            )
            + create_form(slot, csrf, provider)
            + "</div>"
            if ready
            else '<div class="ds-service-detail"><p>' + tr(*reason) + "</p></div>"
        )
        rows.append(
            '<section class="ds-service"><div class="ds-service-head"><h3>'
            + name
            + "</h3>"
            + status_pill
            + "</div>"
            + actions
            + "</section>"
        )
    legacy = slot.get("private_owner") and not slot.get("home_mac")
    if cfg.gateway_ready:
        gemini_pill = (
            '<span class="pill assigned">' + tr("Private Mac", "专属 Mac") + "</span>"
            if legacy
            else '<span class="pill disabled">' + tr("Unavailable", "暂不可用") + "</span>"
        )
        gemini = (
            link("/account/gemini", "Open Gemini setup", "打开 Gemini 设置")
            if legacy
            else "<p>"
            + tr(
                "Gemini subscription requires a private Mac configured for Antigravity.",
                "Gemini 订阅需要已配置 Antigravity 的专属 Mac。",
            )
            + "</p>"
        )
        rows.append(
            '<section class="ds-service"><div class="ds-service-head"><h3>Gemini</h3>'
            + gemini_pill
            + '</div><div class="ds-service-detail">'
            + gemini
            + "</div></section>"
        )
        grants = set()
        if not slot.get("home_mac") and wallet_access.allowed(store, slot, now=now):
            for key in store.list_gateway_keys(account["id"]):
                if key["slot_ids"] == [sid] and not key.get("revoked_at"):
                    for grant in store.api_model_targets(key["id"], key["generation"]):
                        if grant["slot_id"] == sid:
                            grants.add(grant["provider"])
        from .api_model_routes import ALIASES, LABELS

        for provider, alias in ALIASES.items():
            if provider in grants:
                api_pill = '<span class="pill ok">' + tr("Granted", "已授权") + "</span>"
                detail = (
                    "<span>"
                    + tr("Use your granted key and model", "使用已授权的密钥及模型")
                    + " <code>"
                    + escape(alias)
                    + "</code></span>"
                    + link("/account#gateway", "Manage connection keys", "管理连接密钥")
                )
            elif legacy and provider == "gemini":
                api_pill = '<span class="pill assigned">' + tr("Configurable", "可配置") + "</span>"
                detail = link("/account/gemini", "Set up the Gemini API", "设置 Gemini API")
            elif slot.get("home_mac"):
                api_pill = (
                    '<span class="pill disabled">' + tr("Unavailable", "暂不可用") + "</span>"
                )
                detail = (
                    "<p>"
                    + tr(
                        "Choose another slot with this API provider configured.",
                        "请选择已配置此 API 服务的其他槽位。",
                    )
                    + "</p>"
                )
            else:
                api_pill = (
                    '<span class="pill disabled">' + tr("Requires grant", "需要授权") + "</span>"
                )
                detail = (
                    "<span>"
                    + tr("Requires a provider grant for this slot.", "需要为此槽位授权。")
                    + "</span>"
                    + link(
                        "/docs/library/docs/public-gateway.html#api-model-picker",
                        "API model setup",
                        "API 模型设置",
                    )
                )
            rows.append(
                '<section class="ds-service"><div class="ds-service-head"><h3>'
                + LABELS[provider]
                + " API</h3>"
                + api_pill
                + '</div><div class="ds-service-detail">'
                + detail
                + "</div></section>"
            )
    return "".join(rows)


def page(store, cfg, account, session, now, identifier="", error=None):
    from . import home_mac_ui

    held = visible(store, account)
    current = next((s for s in held if s["id"] == identifier), None)
    if identifier and not current:
        raise home_macs.denied()
    viewer = usersite.viewer_for(account, session, cfg, store)
    can_add = account["id"] in cfg.home_mac_owners or "*" in cfg.home_mac_owners
    add = (
        link(home_macs.ADD, "Add my Mac", "添加我的 Mac", "btn primary hm-btn-primary")
        if can_add
        else ""
    )
    lead = tr(
        "Select a personal Mac or hosted slot to verify its connection "
        "and choose a compatible service.",
        "选择一台个人 Mac 或托管槽位，核对连接状态并选择兼容的模型服务。",
    )
    contents = (
        '<div class="hm-page"><a class="hm-back" href="/account">'
        '<span aria-hidden="true">←</span>'
        + tr("Back to account", "返回账户")
        + '</a><header class="hm-head ds-head"><div class="hm-head-copy">'
        '<p class="hm-eyebrow">'
        + tr("Slot picker", "槽位选择器")
        + "</p><h1>"
        + tr("Dedicated slots", "专属槽位")
        + f'</h1><p class="hm-lead">{lead}</p></div>'
        + add
        + "</header>"
    )
    if error:
        contents += '<p class="hm-alert" role="alert">' + tr(*error) + "</p>"
    if held:
        contents += chooser(held, identifier)
    else:
        contents += (
            '<section class="hm-card"><p class="hm-copy">'
            + tr(
                "Add your Mac or claim a hosted slot to get started.",
                "添加你的 Mac 或领取托管槽位即可开始。",
            )
            + '</p><div class="hm-actions">'
            + link(
                "/account/available",
                "Browse hosted slots",
                "浏览托管槽位",
                "btn primary hm-btn-primary",
            )
            + "</div></section>"
        )
    if current:
        home = (
            home_macs.for_slot(store, account["id"], identifier, now)
            if current.get("home_mac")
            else None
        )
        kind_badge = (
            '<span class="pill assigned">' + tr("Personal Mac", "个人 Mac") + "</span>"
            if current.get("home_mac")
            else '<span class="pill assigned">' + tr("Hosted slot", "托管槽位") + "</span>"
        )
        contents += (
            '<div class="ds-slot-banner"><h2>'
            + escape(display(current))
            + "</h2>"
            + kind_badge
            + "</div>"
        )
        if home:
            contents += home_mac_ui.connection(home, viewer.csrf)
        contents += (
            '<section class="hm-card" id="ds-services"><h2>'
            + tr("Services on this slot", "此槽位上的服务")
            + "</h2>"
        )
        contents += services(store, cfg, account, current, now, viewer.csrf, home) + "</section>"
    account_links = []
    if cfg.openrouter_models:
        account_links.append(link("/account/openrouter", "OpenRouter", "OpenRouter"))
    if cfg.devin_trial_enabled:
        account_links.append(link("/account/devin-trial", "Devin trial", "Devin 试用"))
    if account_links:
        contents += (
            '<section class="hm-card"><h2>'
            + tr("Account services", "账户服务")
            + '</h2><p class="hm-copy">'
            + tr(
                "These account-level routes do not require selecting a dedicated slot.",
                "以下账户级服务无需绑定单个专属槽位。",
            )
            + '</p><div class="hm-actions">'
            + "".join(account_links)
            + "</div></section>"
        )
    contents += "</div>" + home_mac_ui.script_tag()
    return usersite._shell(
        "Dedicated slots",
        contents,
        extra_css=home_mac_ui.CSS + home_mac_ui.CONNECTION_CSS,
        width="home-machines",
        viewer=viewer,
    )
