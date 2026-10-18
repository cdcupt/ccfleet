"""Own-account HTTPS endpoint controls. Secrets appear only on the creation response."""
from __future__ import annotations

import time
from collections.abc import Mapping
from html import escape
from typing import Any, Optional

from . import gateway_providers
from . import slots as slotstates
from .bilingual import bi, label
from .store import StoreError

MODES = {"sticky", "priority"}
NOTES = {
    "gateway-updated": ("ok", "Endpoint updated. New requests use your new slot selection."),
    "gateway-revoked": ("ok", "Endpoint key revoked. Applications using it can no longer connect."),
    "gateway-invalid": ("warn", "Choose at least one of your own slots, use distinct priorities, "
                        "and enter an endpoint name of 1 to 60 characters."),
}


def available(cfg: Any) -> bool:
    return getattr(cfg, "gateway_ready", False) is True


def ready_provider_slots(store: Any, cfg: Any, account_id: str,
                         held: list[Mapping[str, Any]], *, now: float) -> list[str]:
    """Public-safe slot IDs only; account fingerprints and reports stay internal."""
    runtime = gateway_providers.runtime_expectations(cfg)
    if (not available(cfg) or getattr(cfg, "gateway_devin_enabled", False) is not True
            or runtime is None):
        return []
    ready = []
    for slot in _selectable(held):
        try:
            binding = store.gateway_provider_binding(slot["id"], account_id, "devin")
            if binding and store.gateway_provider_binding_is_active(
                    slot["id"], account_id, "devin", binding["claim_id"], binding["generation"],
                    now=now, provider_runtime=runtime):
                ready.append(str(slot["id"]))
        except (StoreError, KeyError, TypeError, ValueError):
            continue
    return ready


def _csrf(token: str) -> str:
    return '<input type="hidden" name="csrf" value="' + escape(token, quote=True) + '">'


def _selectable(held: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [slot for slot in held if slot.get("kind") == slotstates.MACHINE_SLOT
            and slot.get("state") in (slotstates.CLAIMING, slotstates.CLAIMED,
                                     slotstates.ACTIVE)]


def _choices(held: list[Mapping[str, Any]], selected: list[str],
             native_ready: tuple[str, ...] = ()) -> str:
    rows = []
    positions = {identifier: index + 1 for index, identifier in enumerate(selected)}
    states = {
        slotstates.CLAIMING: ("Setting up", "正在准备"),
        slotstates.CLAIMED: ("Needs sign-in", "需要登录"),
        slotstates.ACTIVE: ("In use", "使用中"),
    }
    for slot in _selectable(held):
        identifier = str(slot["id"])
        name = str(slot.get("name") or identifier)
        state = (("Devin ready", "Devin 已就绪")
                 if identifier in native_ready and slot["state"] == slotstates.CLAIMED
                 else states[slot["state"]])
        rows.append('<label class="gateway-slot"><span class="gateway-slot-identity"><b>'
                    + escape(name) + '</b><small>'
                    + label(*state, inline=True) + '</small></span>'
                    '<span class="gateway-slot-priority">'
                    + label("Priority", "顺序", inline=True)
                    + '<input type="number" min="1" max="100" step="1" name="slot.'
                    + escape(identifier, quote=True) + '" value="'
                    + str(positions.get(identifier, ""))
                    + '" aria-label="Priority for ' + escape(name, quote=True)
                    + '" data-aria-label-en="Priority for ' + escape(name, quote=True)
                    + '" data-aria-label-zh="' + escape(name + ' 的顺序', quote=True)
                    + '" placeholder="—"></span></label>')
    return ('<fieldset class="gateway-slots"><legend>'
            + label("Choose your accounts", "选择要使用的账号", inline=True) + '</legend>'
            + bi('<p class="gateway-hint">Enter <b>1</b> for your first choice, '
                 '<b>2</b> for a backup. Leave a slot blank to exclude it. '
                 'Choose only one slot to use one fixed account.</p>',
                 '<p class="gateway-hint">首选槽位填 <b>1</b>，备用槽位填 <b>2</b>；'
                 '留空表示不用。只选一个槽位，就始终使用这个账号。</p>')
            + '<div class="gateway-slot-list">' + ''.join(rows) + '</div>'
            + (bi('<p class="small muted">Only your own slots appear here. Each provider needs '
                  'its own verified sign-in. Devin uses exactly one ready slot.</p>',
                  '<p class="small muted">这里只显示自有槽位。每个服务都需要自己的已验证登录；'
                  'Devin 只使用一个已就绪槽位。</p>') if native_ready else
               bi('<p class="small muted">Only your own slots appear here. A slot still '
                 'setting up or waiting for sign-in cannot handle requests yet.</p>',
                 '<p class="small muted">这里只显示你自己的槽位。正在准备或尚未登录 '
                 'Claude 的槽位，暂时不能处理请求。</p>')) + '</fieldset>')


def _option(value: str, en: str, zh: str, selected: bool) -> str:
    # Native option elements accept text, not the bilingual spans used elsewhere.
    return ('<option value="' + value + '" data-copy-en="' + escape(en, quote=True)
            + '" data-copy-zh="' + escape(zh, quote=True) + '"'
            + (' selected' if selected else '') + '>'
            + escape(en) + '</option>')


def _routing(mode: str) -> str:
    return ('<label class="gateway-field">'
            + label("When using several accounts", "使用多个账号时", inline=True)
            + '<select name="mode">'
            + _option("sticky", "Keep the same conversation together", "同一会话用同一账号",
                      mode == "sticky")
            + _option("priority", "Follow priority for each request", "每次请求都按顺序选择",
                      mode == "priority") + '</select></label>'
            + bi('<p class="gateway-hint">Recommended: keep each conversation on the '
                 'account that started a normal reply, while it remains available. '
                 'Priority mode checks your chosen order for every request.</p>',
                 '<p class="gateway-hint">推荐保持会话：某个账号开始正常回复后，'
                 '这个会话会继续用它，前提是它仍可用。按顺序选择则每次请求都重新检查首选账号。</p>')
            + '<details class="gateway-help"><summary>'
            + label("How conversation routing works", "会话保持是怎么工作的？", inline=True)
            + '</summary>'
            + bi('<p>API clients must send a stable session ID in '
                 '<code>X-CCFleet-Session</code> or supported native metadata; without one, '
                 'requests use priority order. The preference lives in gateway memory for '
                 'up to 24 hours after a normal response starts. A restart, a long idle '
                 'period, a routing edit or cache eviction can reset it.</p>'
                 '<p>Recently failed accounts briefly move behind other choices, usually '
                 'for about 30 seconds. Either mode can use a selected backup after an '
                 'eligible failure. With one selected slot, there is no account switching.</p>',
                 '<p>客户端需要通过 <code>X-CCFleet-Session</code> 或支持的原生元数据，'
                 '提供稳定的会话标识；没有标识，就按优先级选择。网关从正常回复开始时记住账号，'
                 '最多保留 24 小时。网关重启、长时间未使用、修改路由或缓存清理，'
                 '都可能重置这个选择。</p>'
                 '<p>最近失败的账号通常会暂时排到其他选择后面，约 30 秒。'
                 '两种模式都能在允许安全切换的'
                 '失败后使用已选的备用槽位。只选一个槽位时，不会切换账号。</p>') + '</details>')


def _provider_name(provider: str) -> str:
    if provider == gateway_providers.DEVIN:
        return label("Devin · Claude agent", "Devin · Claude 智能体", inline=True)
    if provider == gateway_providers.ANTHROPIC:
        return "Anthropic"
    return label("Unavailable provider", "服务暂不可用", inline=True)


def _provider(providers: tuple[str, ...], current: str) -> str:
    if gateway_providers.DEVIN not in providers:
        if current != gateway_providers.DEVIN:
            return ""
        return ('<p class="gateway-hint">'
                + label("Devin is unavailable for this connection. Its provider is unchanged.",
                        "此连接的 Devin 暂不可用，已选服务保持不变。", inline=True)
                + '</p><button class="btn" type="submit" name="provider" value="anthropic">'
                + label("Switch to Anthropic", "切换至 Anthropic", inline=True) + '</button>')
    return ('<label class="gateway-field">'
            + label("Model provider", "模型服务", inline=True)
            + '<select name="provider">'
            + _option("anthropic", "Anthropic", "Anthropic", current == "anthropic")
            + _option("devin", "Devin · Claude agent", "Devin · Claude 智能体", current == "devin")
            + '</select></label>'
            + bi('<p class="gateway-hint">Devin uses its own Claude agent and one verified '
                 'slot of yours. Feature behavior can differ from direct Anthropic.</p>',
                 '<p class="gateway-hint">Devin 使用自己的 Claude 智能体，'
                 '以及一个已验证的自有槽位。'
                 '功能行为可能与直接使用 Anthropic 不同。</p>'))


def _form(action: str, csrf: str, held: list[Mapping[str, Any]], *,
          key: Mapping[str, Any] | None = None,
          providers: tuple[str, ...] = ("anthropic",),
          native_ready: tuple[str, ...] = ()) -> str:
    creating = key is None
    selected = list(key.get("slot_ids") or []) if key else []
    identifier = ('<input type="hidden" name="key_id" value="'
                  + escape(str(key["id"]), quote=True) + '">') if key else ''
    return ('<form class="gateway-form" method="post" action="/account/gateway/' + action + '">'
            + _csrf(csrf) + identifier
            + '<label class="gateway-field">'
            + label("Connection name", "连接名称", inline=True)
            + '<input name="name" maxlength="60" required value="'
            + escape(str(key.get("name", "")) if key else "Claude Code", quote=True)
            + '"></label>'
            + _provider(providers, str(key.get("provider", "anthropic")) if key else "anthropic")
            + _choices(held, selected, native_ready)
            + _routing(str(key.get("mode")) if key else "sticky")
            + '<div class="gateway-form-actions"><button class="btn primary" type="submit">'
            + (label("Create key", "创建密钥", inline=True) if creating
               else label("Save changes", "保存修改", inline=True)) + '</button></div></form>')


def panel(
    store: Any, cfg: Any, account: Mapping[str, Any], csrf: str, held: list[Mapping[str, Any]],
    *, now: Optional[float] = None
) -> str:
    if not available(cfg):
        return ""
    keys = store.list_gateway_keys(account["id"])
    native_slots = ready_provider_slots(store, cfg, account["id"], held,
                                         now=time.time() if now is None else now)
    providers = gateway_providers.PROVIDERS if native_slots else ("anthropic",)
    active = [key for key in keys if not key.get("revoked_at")]
    base_url = cfg.public_url.rstrip("/")
    entries = []
    for key in active:
        count = len(key.get("slot_ids") or [])
        routing = (
            label("One fixed account", "固定一个账号", inline=True)
            if count == 1
            else label("Keep conversations together", "保持会话", inline=True)
            if key.get("mode") == "sticky"
            else label("Priority order", "按顺序选择", inline=True)
        )
        revoke = (
            '<form method="post" action="/account/gateway/revoke">'
            + _csrf(csrf)
            + '<input type="hidden" name="key_id" value="'
            + escape(str(key["id"]), quote=True)
            + '">'
            '<button class="btn danger" type="submit">'
            + label("Revoke key", "撤销密钥", inline=True)
            + "</button></form>"
        )
        entries.append(
            '<details class="gateway-endpoint"><summary>'
            '<span class="gateway-endpoint-name">'
            + escape(str(key.get("name") or "Connection"))
            + "</span>"
            '<span class="gateway-badges"><span class="gateway-badge">'
            + routing
            + '</span><span class="gateway-badge">'
            + _provider_name(str(key.get("provider", "anthropic")))
            + '</span><span class="gateway-badge">'
            + label(f"{count} selected", f"已选 {count} 个槽位", inline=True)
            + '</span></span></summary><div class="gateway-endpoint-body">'
            + _form("update", csrf, held, key=key, providers=providers,
                    native_ready=tuple(native_slots))
            + '<div class="gateway-revoke">'
            + bi(
                '<p class="small muted">Revoking stops this key, including its '
                "requests in progress. It does not release your slots.</p>",
                '<p class="small muted">撤销后，此密钥以及它正在进行的请求会停止。'
                "你的槽位不会被释放。</p>",
            )
            + revoke
            + "</div>"
            + '<p class="small muted gateway-key-reference">'
            + label("Key reference", "密钥编号", inline=True)
            + ": <code>"
            + escape(str(key["id"]))
            + "</code></p></div></details>"
        )
    if _selectable(held):
        create = (
            '<details class="gateway-create"'
            + ("" if active else " open")
            + "><summary>"
            + label("New connection key", "新建连接密钥", inline=True)
            + '</summary><div class="gateway-endpoint-body">'
            + bi(
                '<p class="gateway-hint">For an app you configure yourself. '
                "Give it a name, choose your accounts, then save the key shown once.</p>",
                '<p class="gateway-hint">适合手动配置的应用。为连接取个名字，选好账号，'
                "然后保存只显示一次的密钥。</p>",
            )
            + _form("create", csrf, held, providers=providers, native_ready=tuple(native_slots))
            + "</div></details>"
        )
    else:
        create = (
            '<div class="gateway-empty">'
            + bi(
                "<h3>Start with a slot</h3><p>Claim a slot above, then sign in to "
                "your own Claude account. You can create a connection "
                "after claiming it.</p>",
                "<h3>先准备一个槽位</h3><p>在上方领取槽位，再登录你自己的 Claude 账号。"
                "领取后，就可以创建连接。</p>",
            )
            + '<a href="/docs">'
            + label("How to get a slot", "如何获得槽位", inline=True)
            + "</a></div>"
        )
    return (
        '<section class="card gateway-card" id="gateway" aria-labelledby="gateway-title">'
        '<div class="gateway-heading"><div><h2 id="gateway-title">'
        + label("Connect Claude", "连接 Claude", inline=True)
        + "</h2>"
        + bi(
            "<p>Use Claude Code on your computer. It sends a model request over "
            "HTTPS to Fleet; Fleet chooses one of your selected accounts and sends "
            "the reply back. Files, tools and saved history stay local, while "
            "selected file excerpts and tool results travel as model context.</p>",
            "<p>在电脑上运行 Claude Code。需要模型回复时，请求通过 HTTPS 发给 Fleet；"
            "Fleet 使用你选定的账号，再把回复送回电脑。文件、工具和已保存的历史留在本机，"
            "被选作上下文的文件片段、工具结果会随请求发送。</p>",
        )
        + '</div><span class="gateway-badge gateway-https">HTTPS</span></div>'
        '<div class="gateway-address"><div>'
        + label("Base URL", "连接地址", inline=True)
        + "</div><code>"
        + escape(base_url)
        + "</code></div>"
        '<div class="gateway-start"><div><h3>'
        + label("With the Fleet CLI", "使用 Fleet 命令行", inline=True)
        + "</h3>"
        + bi(
            "<p>On an already paired computer, run these in order:</p>",
            "<p>在已配对的电脑上，依次运行：</p>",
        )
        + "<pre><code>ccfleet gateway setup\nccfleet local</code></pre>"
        + bi(
            '<p class="small muted">Setup creates or reuses its own key. '
            "It does not import a key created on this page.</p>",
            '<p class="small muted">设置命令会创建或复用自己的密钥，'
            "不会导入本页手动创建的密钥。</p>",
        )
        + "</div><div><h3>"
        + label("With another app", "连接其他应用", inline=True)
        + "</h3>"
        + bi(
            "<p>For original Claude Code or a compatible Messages API app, "
            "create a connection key below and use it with this base URL.</p>",
            "<p>原版 Claude Code 或兼容 Messages API 的应用，也可以使用下方创建的"
            "连接密钥和上面的地址。</p>",
        )
        + '<a href="/docs/library/docs/public-gateway.html#keys">'
        + label("Connection guide", "查看连接说明", inline=True)
        + "</a></div></div>"
        '<div class="gateway-section-heading"><h3>'
        + label("Your connection keys", "你的连接密钥", inline=True)
        + "</h3>"
        '<span class="gateway-count">'
        + str(len(active))
        + "</span></div>"
        + (
            ""
            if active
            else bi(
                '<p class="gateway-hint">No saved connections yet. '
                "Choose guided CLI setup above, or create a key below.</p>",
                '<p class="gateway-hint">还没有保存的连接。'
                "可以用上面的命令引导设置，也可以在下方创建密钥。</p>",
            )
        )
        + "".join(entries)
        + create
        + '<details class="gateway-help gateway-technical"><summary>'
        + label("Switching, privacy and advanced setup", "切换、隐私与进阶设置", inline=True)
        + "</summary>"
        + bi(
            "<p>Only slots you own can be selected. The gateway can try a selected "
            "backup after a confirmed eligible rejection or a connection failure "
            "before a model request was sent. It does not make another attempt when "
            "the outcome is unknown or after response headers have been sent. "
            "Applications may have their own retry behavior.</p>"
            "<p>HTTPS encrypts traffic in transit, but our gateway can read model "
            "requests and replies, including selected model context. "
            "Your Claude account credential "
            "remains on its slot.</p>"
            "<p>For several slots, use <code>ccfleet gateway setup --slots first-slot "
            "backup-slot --routing sticky</code>. Replace the example names with yours. "
            "Use <code>ccfleet local --slot first-slot</code> to pin one selected slot "
            "for a launch, with no automatic switch to another account.</p>"
            "<p>A released or reclaimed slot needs its endpoint selection saved again "
            "before its new account can be used.</p>",
            "<p>只能选择你自己的槽位。已确认属于可安全切换的拒绝，或确定还没把模型请求"
            "发出去就连接失败时，网关可以尝试已选的备用槽位。如果不知道请求是否已经送达，"
            "或已向客户端发出响应头，就不会再次尝试。应用本身可能有另外的重试规则。</p>"
            "<p>HTTPS 会加密传输过程，但我们的网关在转发时能够读取模型请求和回复。"
            "你的 Claude 账号登录凭据仍保留在对应槽位上。</p>"
            "<p>选择多个槽位可以用 <code>ccfleet gateway setup --slots first-slot "
            "backup-slot --routing sticky</code>，请换成自己的槽位名。"
            "<code>ccfleet local --slot first-slot</code> 会让这次启动只用所选的一个槽位，"
            "不会自动切换到其他账号。</p>"
            "<p>槽位释放后重新领取，需要再次保存它的连接选择，才能使用这次领取后的账号。</p>",
        )
        + "</details><style>"
        + GATEWAY_CSS
        + "</style></section>"
    )


GATEWAY_CSS = """
#gateway{padding:24px;scroll-margin-top:96px}
#gateway h2{margin:0;font-size:24px;letter-spacing:-.025em}
#gateway h3{font-size:16px;margin:0 0 10px;letter-spacing:-.01em}
#gateway p{line-height:1.6;margin:8px 0}
#gateway .gateway-heading{display:flex;align-items:flex-start;justify-content:space-between;
gap:20px;margin-bottom:20px}
#gateway .gateway-heading p{color:var(--muted);max-width:64ch}
#gateway .gateway-badge{display:inline-flex;align-items:center;padding:5px 9px;
border:1px solid var(--rule-soft);border-radius:8px;background:var(--inset);
font-size:12px;line-height:1.4;font-weight:550;color:var(--muted)}
#gateway .gateway-https{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-line)}
#gateway .gateway-address{display:grid;grid-template-columns:auto minmax(0,1fr);gap:12px;
align-items:center;padding:13px 16px;border:1px solid var(--rule-soft);border-radius:10px;
background:var(--inset);font-size:13px}
#gateway .gateway-address>div{color:var(--muted);font-weight:600}
#gateway code{overflow-wrap:anywhere}
#gateway .gateway-start{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
gap:24px;margin:24px 0;padding-bottom:24px;border-bottom:1px solid var(--rule-soft)}
#gateway .gateway-start pre{margin:12px 0;white-space:pre-wrap;font-size:13px;
background:var(--inset);border:1px solid var(--rule-soft)}
#gateway .gateway-start>div{min-width:0}
#gateway .gateway-section-heading{display:flex;align-items:center;gap:10px;margin:0 0 12px}
#gateway .gateway-section-heading h3{margin:0}
#gateway .gateway-count{font-size:12px;font-weight:600;color:var(--muted)}
#gateway .gateway-hint{font-size:13px;color:var(--muted)}
#gateway summary{cursor:pointer;font-weight:600;border-radius:8px}
#gateway summary:focus-visible{outline:2px solid var(--acc);outline-offset:4px}
#gateway .gateway-endpoint,#gateway .gateway-create{border:1px solid var(--rule);
border-radius:12px;margin:10px 0;background:var(--panel)}
#gateway .gateway-endpoint>summary,#gateway .gateway-create>summary{padding:15px 18px}
#gateway .gateway-endpoint-name{overflow-wrap:anywhere}
#gateway .gateway-badges{display:inline-flex;flex-wrap:wrap;gap:6px;margin-left:10px}
#gateway .gateway-endpoint[open]>summary,#gateway .gateway-create[open]>summary{
border-bottom:1px solid var(--rule-soft)}
#gateway .gateway-endpoint-body{padding:4px 18px 18px}
#gateway .gateway-form{margin:12px 0 0}
#gateway .gateway-field{display:block;font-size:13px;font-weight:600;margin:16px 0 8px}
#gateway .gateway-field>input,#gateway .gateway-field>select{display:block;
margin-top:7px;width:100%;max-width:100%;font-weight:400}
#gateway .gateway-slots{min-width:0;border:1px solid var(--rule-soft);border-radius:10px;
padding:12px 15px 6px;margin:20px 0 16px}
#gateway .gateway-slots legend{font-size:13px;font-weight:650;padding:0 6px}
#gateway .gateway-slot-list{margin:12px 0 4px}
#gateway .gateway-slot{display:flex;justify-content:space-between;align-items:center;gap:16px;
padding:11px 0;border-top:1px solid var(--rule-soft)}
#gateway .gateway-slot-identity{min-width:0;overflow-wrap:anywhere}
#gateway .gateway-slot-identity b{display:block;font-family:var(--mono);font-size:13px}
#gateway .gateway-slot-identity small{display:block;font-size:12px;color:var(--muted);
margin-top:4px}
#gateway .gateway-slot-priority{display:flex;align-items:center;justify-content:flex-end;
gap:10px;font-size:12px;color:var(--muted);flex-shrink:0}
#gateway .gateway-slot input{width:76px;max-width:76px;margin:0}
#gateway .gateway-help{margin:12px 0;color:var(--muted);font-size:13px}
#gateway .gateway-help summary{color:var(--ink);font-size:13px}
#gateway .gateway-form-actions{margin:18px 0 4px}
#gateway .gateway-revoke{display:flex;align-items:center;justify-content:space-between;
flex-wrap:wrap;gap:12px;margin-top:20px;padding-top:14px;border-top:1px solid var(--rule-soft)}
#gateway .gateway-revoke>.bi{flex:1 1 280px}
#gateway .gateway-revoke form{margin:0}
#gateway .gateway-key-reference{font-size:11px;margin-top:14px;overflow-wrap:anywhere}
#gateway .gateway-empty{margin:14px 0;padding:18px;border:1px dashed var(--rule);
border-radius:12px;background:var(--inset)}
#gateway .gateway-technical{padding-top:16px;margin-top:20px;border-top:1px solid var(--rule-soft)}
@media(max-width:620px){
#gateway{padding:18px}
#gateway .gateway-heading{gap:12px}
#gateway h2{font-size:21px}
#gateway .gateway-start{grid-template-columns:minmax(0,1fr);gap:18px}
#gateway .gateway-address{grid-template-columns:minmax(0,1fr);gap:6px}
#gateway .gateway-badges{display:flex;margin:8px 0 0 16px}
#gateway .gateway-endpoint-body{padding:3px 13px 14px}
#gateway .gateway-endpoint>summary,#gateway .gateway-create>summary{padding:13px}
#gateway .gateway-slots{padding:10px 11px 5px}
#gateway .gateway-slot{align-items:flex-start;gap:10px}
#gateway .gateway-slot-priority{display:block;text-align:right}
#gateway .gateway-slot input{display:block;margin-top:5px}
#gateway .gateway-revoke{display:block}
#gateway .gateway-revoke form{margin-top:10px}
}
"""


def selected_slots(store: Any, account_id: str, form: Mapping[str, str]) -> list[str]:
    held = {slot["id"]: slot for slot in store.list_slots(held_by=account_id)
            if slot.get("kind") == slotstates.MACHINE_SLOT and slot.get("state") in (
                slotstates.CLAIMING, slotstates.CLAIMED, slotstates.ACTIVE)}
    choices = []
    for field, value in form.items():
        if not field.startswith('slot.') or value == '':
            continue
        identifier = field[5:]
        if identifier not in held or not value.isascii() or not value.isdigit():
            raise ValueError("invalid selection")
        priority = int(value)
        if not 1 <= priority <= 100:
            raise ValueError("invalid priority")
        choices.append((priority, identifier))
    if not choices or len({number for number, _ in choices}) != len(choices):
        raise ValueError("choose slots with unique priorities")
    return [identifier for _, identifier in sorted(choices)]


def act(store: Any, cfg: Any, account: Mapping[str, Any], path: str,
        form: Mapping[str, str], now: float, session_id: str = "") -> Any:
    # Imported at call time so usersite can use the panel without a cycle.
    from . import usersite
    viewer = usersite.viewer_for(account, session_id, cfg, store)
    if not available(cfg):
        return usersite.not_found(viewer)
    action = path.rsplit('/', 1)[-1]
    try:
        if action == 'revoke':
            store.revoke_gateway_key(form.get("key_id", ""), account["id"], now=now)
            return usersite._back("gateway-revoked", "gateway")
        if action not in {'create', 'update'} or form.get("mode") not in MODES:
            raise ValueError("invalid action")
        slot_ids = selected_slots(store, account["id"], form)
        provider = form.get("provider", None if action == "update" else "anthropic")
        if provider is not None and gateway_providers.normalize_provider(provider) is None:
            raise ValueError("invalid provider")
        runtime = gateway_providers.runtime_expectations(cfg)
        allow_devin = bool(available(cfg) and getattr(cfg, "gateway_devin_enabled", False) is True
                           and runtime is not None)
        if provider == "devin":
            native = ready_provider_slots(store, cfg, account["id"],
                store.list_slots(held_by=account["id"]), now=now)
            if len(slot_ids) != 1 or slot_ids[0] not in native:
                raise ValueError("provider is not available for this selection")
        if action == 'update':
            store.update_gateway_key(form.get("key_id", ""), account["id"], slot_ids,
                form["mode"], name=form.get("name", ""), now=now, provider=provider,
                allow_devin=allow_devin, provider_runtime=runtime)
            return usersite._back("gateway-updated", "gateway")
        key = store.create_gateway_key(account["id"], form.get("name", ""), slot_ids,
            mode=form["mode"], now=now, provider=provider, allow_devin=allow_devin,
            provider_runtime=runtime)
    except (StoreError, ValueError, TypeError):
        return usersite._back("gateway-invalid", "gateway")
    if key.get("provider", gateway_providers.ANTHROPIC) == gateway_providers.DEVIN:
        api_paths_en = ('<p>This Devin connection supports <code>POST /v1/messages</code> only. '
                        'Token counting is unavailable.</p>')
        api_paths_zh = ('<p>此 Devin 连接仅支持 <code>POST /v1/messages</code>，'
                        '不支持 token 计数。</p>')
    else:
        api_paths_en = ('<p>The gateway supports <code>POST /v1/messages</code> and '
                        '<code>POST /v1/messages/count_tokens</code>.</p>')
        api_paths_zh = ('<p>网关支持 <code>POST /v1/messages</code> 和 '
                        '<code>POST /v1/messages/count_tokens</code>。</p>')
    body = ('<div class="pagehead"><h1>'
            + label("Save your connection key", "保存你的连接密钥", inline=True)
            + '</h1></div><div class="card">'
            + bi('<p><b>This key is shown once.</b> Save it in your password manager; '
                 'the server keeps only its hash. Never share it or commit it to a project.</p>',
                 '<p><b>这个密钥只显示这一次。</b>请保存到密码管理器；服务器只保存它的哈希值，'
                 '之后无法再显示原值。不要分享密钥，也不要提交到项目里。</p>')
            # Keep the raw secret in a single escaped element, outside paired copy.
            + '<pre class="token">' + escape(str(key["key_token"])) + '</pre>'
            + '<p><b>' + label("Base URL", "连接地址", inline=True) + ':</b> <code>'
            + escape(cfg.public_url.rstrip('/')) + '</code></p>'
            + bi('<p>For native Claude Code, set <code>ANTHROPIC_BASE_URL</code> to '
                 'this URL and <code>ANTHROPIC_AUTH_TOKEN</code> to this connection key. '
                 'Keep the key in private local settings, outside your project.</p>',
                 '<p>手动配置原版 Claude Code 时，把 <code>ANTHROPIC_BASE_URL</code> '
                 '设为上面的地址，把 <code>ANTHROPIC_AUTH_TOKEN</code> 设为这个连接密钥。'
                 '将密钥放在个人的本地私密设置中，不要放进项目。</p>')
            + '<details><summary>'
            + label("API paths and guided CLI setup", "API 路径与命令行引导设置", inline=True)
            + '</summary>'
            + bi(api_paths_en +
                 '<p>On an already paired computer, <code>ccfleet gateway setup</code> '
                 'creates or reuses its own key without printing it to your terminal. '
                 'It does not import the website key shown above. Then run '
                 '<code>ccfleet local</code>.</p>',
                 api_paths_zh +
                 '<p>已配对的电脑可以用 <code>ccfleet gateway setup</code> 创建或复用'
                 '命令行自己的密钥，密钥不会打印到终端。它不会导入上方的网页密钥。'
                 '完成后运行 <code>ccfleet local</code>。</p>')
            + '</details><p><a href="/account#gateway">'
            + label("Back to connections", "返回连接管理", inline=True) + '</a></p></div>')
    return usersite.Outcome(200, body=usersite._shell("Save your connection key", body,
                                                   viewer=viewer))
