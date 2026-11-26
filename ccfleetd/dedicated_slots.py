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
        actions = (
            link(f"/account#slot-{sid}-connect-{provider}", "Connect my computer", "连接我的电脑")
            + create_form(slot, csrf, provider)
            if ready
            else "<p>" + tr(*reason) + "</p>"
        )
        rows.append('<section class="ds-service"><h3>' + name + "</h3>" + actions + "</section>")
    legacy = slot.get("private_owner") and not slot.get("home_mac")
    if cfg.gateway_ready:
        gemini = (
            link("/account/gemini", "Open Gemini setup", "打开 Gemini 设置")
            if legacy
            else tr(
                "Gemini subscription requires a private Mac configured for Antigravity.",
                "Gemini 订阅需要已配置 Antigravity 的专属 Mac。",
            )
        )
        rows.append('<section class="ds-service"><h3>Gemini</h3>' + gemini + "</section>")
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
                detail = (
                    tr("Use your granted key and model", "使用已授权的密钥及模型")
                    + " <code>"
                    + escape(alias)
                    + "</code> "
                    + link("/account#gateway", "Manage connection keys", "管理连接密钥")
                )
            elif legacy and provider == "gemini":
                detail = link("/account/gemini", "Set up the Gemini API", "设置 Gemini API")
            elif slot.get("home_mac"):
                detail = tr(
                    "Choose another slot with this API provider configured.",
                    "请选择已配置此 API 服务的其他槽位。",
                )
            else:
                detail = (
                    tr("Requires a provider grant for this slot.", "需要为此槽位授权。")
                    + " "
                    + link(
                        "/docs/library/docs/public-gateway.html#api-model-picker",
                        "API model setup",
                        "API 模型设置",
                    )
                )
            rows.append(
                '<section class="ds-service"><h3>'
                + LABELS[provider]
                + " API</h3>"
                + "<div>"
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
    add = link(home_macs.ADD, "Add my Mac", "添加我的 Mac", "btn primary") if can_add else ""
    contents = (
        '<div class="hm-page"><header class="hm-head ds-head"><h1>'
        + tr("Dedicated slots", "专属槽位")
        + "</h1>"
        + add
        + "</header>"
    )
    if error:
        contents += '<p role="alert">' + tr(*error) + "</p>"
    if held:
        contents += chooser(held, identifier)
    else:
        contents += (
            "<p>"
            + tr(
                "Add your Mac or claim a hosted slot to get started.",
                "添加你的 Mac 或领取托管槽位即可开始。",
            )
            + "</p>"
        )
        contents += link("/account/available", "Browse hosted slots", "浏览托管槽位")
    if current:
        home = (
            home_macs.for_slot(store, account["id"], identifier, now)
            if current.get("home_mac")
            else None
        )
        contents += "<h2>" + escape(display(current)) + "</h2>"
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
            "<section><h2>"
            + tr("Account services", "账户服务")
            + "</h2>"
            + "".join(account_links)
            + "</section>"
        )
    contents += (
        link("/account", "Back to account", "返回账户") + "</div>" + home_mac_ui.script_tag()
    )
    return usersite._shell(
        "Dedicated slots",
        contents,
        extra_css=home_mac_ui.CSS + home_mac_ui.CONNECTION_CSS,
        width="home-machines",
        viewer=viewer,
    )
