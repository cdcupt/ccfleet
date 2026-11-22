"""Bilingual setup for the private Mac's Gemini subscription and API connections."""

from __future__ import annotations

from collections import Counter
from html import escape

from . import agent_guide, gemini_setup_store, resets
from .bilingual import bi, label

PATH = "/account/gemini"

CSS = """
.site .page.gemini-page{max-width:1040px;padding-block:32px 64px}
.gemini-page .gm-back{display:inline-flex;align-items:center;gap:7px;min-height:38px;
color:var(--muted);font-size:14px;text-decoration:none;font-weight:600}
.gemini-page .gm-back:hover{color:var(--acc);text-decoration:underline}
.gm-header{margin:16px 0 26px}.gm-header h1{font-size:clamp(29px,4vw,39px);margin:0}
.gm-header p{margin:10px 0 0;max-width:70ch;color:var(--muted);line-height:1.65}
.gm-grid{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(235px,1fr);
gap:22px;align-items:start}.gm-grid>*{min-width:0}
.gemini-page .gm-panel{background:var(--panel);border:1px solid var(--rule);
border-radius:var(--radius);box-shadow:var(--shadow);padding:clamp(20px,3vw,32px)}
.gemini-page .gm-panel h2{font-size:20px;line-height:1.4;margin:0 0 20px}
.gm-form{display:grid;gap:20px;margin:0}.gm-field{display:grid;align-content:start;gap:7px;min-width:0}
.gemini-page .gm-field label{margin:0;color:var(--ink);font-size:14px;font-weight:650}
.gemini-page .gm-field :is(input,select){display:block;width:100%;min-width:0;
min-height:46px;border:1px solid var(--rule);border-radius:9px;background:var(--panel);
color:var(--ink);font:inherit;font-size:16px;line-height:1.4;padding:11px 12px}
.gemini-page .gm-field :is(input,select):focus-visible{outline:2px solid var(--acc);
outline-offset:2px}.gm-field input::placeholder{color:var(--muted)}
.gm-help,.gm-secondary{font-size:13px;line-height:1.6;color:var(--muted)}
.gm-help{margin:0}.gm-pair{display:grid;align-items:start;gap:16px;
grid-template-columns:repeat(auto-fit,minmax(min(100%,210px),1fr))}
.gm-actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin:2px 0 0;
padding-top:18px;border-top:1px solid var(--rule-soft)}
.gemini-page .gm-actions button{min-height:44px;padding:10px 18px}
.gm-metahead{display:flex;align-items:center;gap:12px}
.gm-device{width:36px;height:36px;display:grid;place-items:center;flex:0 0 auto;
background:var(--acc-soft);border-radius:10px;color:var(--acc)}
.gm-device:before{content:"";width:18px;height:13px;border:2px solid currentColor;
border-radius:4px;box-shadow:0 4px 0 -2px var(--acc-soft),0 6px 0 -3px currentColor}
.gemini-page .gm-metahead h2{margin:0;font-size:17px}.gm-meta{margin:18px 0 0}
.gm-meta>div{display:grid;gap:3px;padding:11px 0;border-top:1px solid var(--rule-soft)}
.gm-subscription{margin:10px 0 0;padding-top:12px;border-top:1px solid var(--rule-soft);
font-size:13px;line-height:1.65}
.gm-meta dt{font-weight:550;font-size:12px;color:var(--muted)}.gm-meta dd{margin:0;
font-size:15px;overflow-wrap:anywhere}.gm-meta ul{padding-left:18px;margin:0}
.gm-note{background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn);
border-radius:10px;padding:12px 15px;margin:0 0 18px}
.gm-disconnect{margin-top:20px;border-top:1px solid var(--rule);padding-top:16px}
.gm-panel form:has(.gm-check input:invalid) .gm-actions button.primary{
background:var(--panel);border-color:var(--rule);color:var(--ink)}
.gm-disconnect summary{cursor:pointer;color:var(--muted);font-weight:600;font-size:14px}
.gm-disconnect form{display:grid;gap:12px;margin-top:14px}
.gemini-page .gm-check{display:flex;align-items:flex-start;gap:10px;color:var(--ink);
font-size:14px;line-height:1.55;margin:0}.gm-disconnect button{justify-self:start}
.gemini-page .gm-code{margin:14px 0;max-width:100%;white-space:pre-wrap;
overflow-wrap:anywhere;word-break:break-all;font:600 16px/1.65 var(--mono);
padding:16px 18px;border:1px solid var(--acc-line);border-radius:10px;
background:var(--acc-soft);color:var(--ink);user-select:all}
.gm-copy-line{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.gm-copy-line button{min-height:44px}.gm-copy-line [role=status]{color:var(--ok)}
.gm-next{margin:18px 0 0;color:var(--muted);font-size:14px}
.gemini-page [hidden]{display:none!important}
@media(max-width:760px){.gemini-page #subscription{scroll-margin-top:min(16rem,50vh)}
.gm-grid{grid-template-columns:minmax(0,1fr);gap:16px}
.site .page.gemini-page{padding-block:16px 40px}.gm-header{margin:10px 0 20px}}
@media(max-width:560px){.gm-pair{grid-template-columns:minmax(0,1fr);gap:18px}
.gm-actions button,.gm-copy-line button{width:100%}}
"""


def tr(en, zh):
    return label(en, zh, inline=True)


def hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(value, quote=True)}">'


def field(identifier, title, control, hint=""):
    help_text = f'<p class="gm-help" id="{identifier}-hint">{hint}</p>' if hint else ""
    return (
        f'<div class="gm-field"><label for="{identifier}">{title}</label>{control}{help_text}</div>'
    )


def option(value, english, chinese):
    return (
        f'<option value="{escape(value, quote=True)}" data-copy-en="{escape(english, quote=True)}" '
        f'data-copy-zh="{escape(chinese, quote=True)}">{escape(english)}</option>'
    )


def panel(title, body):
    return '<section class="gm-panel"><h2>' + title + "</h2>" + body + "</section>"


def slot_panel(slot, keys, binding, grants):
    def row(title, value):
        return "<div><dt>" + title + "</dt><dd>" + value + "</dd></div>"

    status = tr("No API key configured", "未配置 API 密钥")
    if binding and gemini_setup_store.retiring(binding):
        status = tr("Removal pending", "等待移除")
    elif binding and binding["credential_fp"]:
        status = tr("API key saved", "密钥已保存")
    metadata = row(tr("Host", "主机"), escape(slot["name"] or slot["id"]))
    metadata += row(tr("API credential status", "API 凭据状态"), status)
    if binding:
        metadata += row(tr("Model", "当前模型"), escape(binding["model"]))
    if grants:
        metadata += row(
            tr("Connected computers", "已连接的电脑"),
            "<ul>"
            + "".join("<li>" + escape(v["name"]) + "</li>" for k, v in keys.items() if k in grants)
            + "</ul>",
        )
    metadata += row(tr("API key storage", "API 密钥保存在"),
                    tr("Mac login Keychain", "Mac 登录钥匙串"))
    return (
        '<aside class="gm-panel"><div class="gm-metahead"><span aria-hidden="true" '
        'class="gm-device"></span><h2>'
        + tr("Mac slot", "Mac 槽位")
        + "</h2></div>"
        + '<dl class="gm-meta">'
        + metadata
        + '</dl><p class="gm-subscription">'
        '<a href="https://antigravity.google/docs/install?tab=cli" '
        'target="_blank" rel="noopener noreferrer">'
        + tr(
            "Antigravity sign-in instructions for the slot Mac",
            "查看槽位 Mac 的 Antigravity 登录说明",
        )
        + "</a></p></aside>"
    )


def shell(title, description, main, sidebar, viewer, *, note="", back="/account", back_label=None):
    from . import usersite

    back_label = back_label or tr("My slots", "我的槽位")
    body = (
        f'<a class="gm-back" href="{escape(back, quote=True)}">'
        '<span aria-hidden="true">←</span>' + back_label + '</a><header class="gm-header">'
        "<h1>" + title + "</h1><p>" + description + "</p></header>"
    )
    if note:
        body += '<p class="gm-note" role="status">' + note + "</p>"
    body += '<div class="gm-grid"><div>' + main + "</div>" + sidebar + "</div>"
    return usersite._shell("Gemini connections", body, extra_css=CSS, width="gemini-page",
                          viewer=viewer)


def setup_form(csrf, keys, binding):
    counts = Counter(k["name"] for k in keys.values())
    options = option("new", "Pair a new computer", "配对新电脑")
    for key, record in keys.items():
        name = record["name"]
        if counts[name] > 1:
            name += " · " + key[-6:]
        options += option(key, "Saved connection · " + name, "已有连接 · " + name)
    controls = field(
        "gm-client",
        tr("Client computer", "客户端电脑"),
        '<select id="gm-client" name="key" aria-describedby="gm-client-hint">'
        + options
        + "</select>",
        tr(
            "A new computer gets a pairing code. You can also link a saved connection.",
            "新电脑会获得配对码；也可以选择已有的 Fleet 连接。",
        ),
    )
    if not binding:
        project = field(
            "gm-project",
            tr("Google Cloud project number", "Google Cloud 项目编号"),
            '<input type="text" id="gm-project" required inputmode="numeric" '
            'name="project" pattern="[1-9][0-9]*" maxlength="30" '
            'aria-describedby="gm-project-hint">',
            tr(
                "Use the number of the project that owns your API key.",
                "请填写 API 密钥所属项目的数字编号。",
            ),
        )
        model = field(
            "gm-model",
            tr("Gemini model ID", "Gemini 模型 ID"),
            '<input type="text" id="gm-model" required name="model" '
            'pattern="gemini-[A-Za-z0-9._\\-]+" maxlength="95" '
            'placeholder="gemini-…" aria-describedby="gm-model-hint">',
            '<a href="https://ai.google.dev/gemini-api/docs/models" '
            'target="_blank" rel="noopener noreferrer">'
            + tr("View Gemini models", "查看 Gemini 模型列表")
            + "</a>",
        )
        controls += '<div class="gm-pair">' + project + model + "</div>"
    button = tr("Continue", "下一步") if not binding else tr("Connect computer", "连接电脑")
    return (
        f'<form method="post" class="gm-form" action="{PATH}/start">'
        + hidden("csrf", csrf)
        + controls
        + '<div class="gm-actions">'
        '<button class="primary" type="submit">' + button + "</button></div></form>"
    )


def disconnect_form(csrf, binding):
    retiring = gemini_setup_store.retiring(binding)
    action = (
        tr("Finish removal", "完成移除") if retiring else tr("Disconnect Gemini", "断开 Gemini")
    )
    return (
        '<details class="gm-disconnect"'
        + (" open" if retiring else "")
        + "><summary>"
        + action
        + f'</summary><form method="post" action="{PATH}/disconnect">'
        + hidden("csrf", csrf)
        + hidden("binding", binding["id"])
        + '<label class="gm-check"><input type="checkbox" required name="confirm" value="1">'
        + tr(
            "Remove this key from the Mac and revoke access for its linked computers.",
            "从 Mac 移除密钥，并撤销关联电脑的 Gemini 访问权限。",
        )
        + '</label><button type="submit" class="danger">'
        + action
        + "</button></form></details>"
    )


def credential_form(csrf, flow_token, nonce, public_key, script):
    hint = tr(
        "Your browser encrypts the key for the selected Mac.",
        "浏览器会先加密密钥，再将其发送到所选 Mac。",
    )
    control = (
        '<input type="password" required minlength="16" maxlength="2048" id="gemini-key" '
        'autocomplete="off" aria-describedby="gemini-key-hint">'
    )
    return (
        f'<form method="post" class="gm-form" id="gemini-credential" action="{PATH}/finish" '
        f'data-nonce="{escape(nonce, quote=True)}" data-pub="{escape(public_key, quote=True)}">'
        + hidden("csrf", csrf)
        + hidden("flow", flow_token)
        + hidden("envelope", "")
        + field("gemini-key", tr("Gemini API key", "Gemini API 密钥"), control, hint)
        + '<p id="crypto-required" class="gm-note">'
        + tr(
            "Enable JavaScript in a secure browser to encrypt your key.",
            "请在支持安全连接的浏览器中启用 JavaScript，以便加密密钥。",
        )
        + '</p><p role="alert" class="gm-note" hidden>'
        + tr("Encryption failed. Reopen setup and try again.", "加密失败，请重新打开设置再试。")
        + '</p><div class="gm-actions"><button class="primary" disabled type="submit">'
        + tr("Save on my Mac", "保存到我的 Mac")
        + "</button></div></form><script>"
        + script
        + "</script>"
    )


def pairing_result(code, slot, keys, binding, grants, viewer, script):
    code_panel = panel(
        tr("One-time pairing code", "一次性配对码"),
        '<p class="gm-secondary">'
        + tr("Valid for 10 minutes.", "有效期为 10 分钟。")
        + '</p><pre class="gm-code" id="gemini-code">'
        + escape(code)
        + '</pre><div class="gm-copy-line"><button class="primary" type="button" '
        'id="gemini-copy" hidden>'
        + tr("Copy code", "复制配对码")
        + '</button><span id="copy-status" role="status"></span></div>'
        + '<p class="gm-next">'
        + tr(
            "After pairing, choose Gemini · API in Claude Code’s /model menu.",
            "完成配对后，在 Claude Code 的 /model 菜单选择 Gemini · API。",
        )
        + "</p>",
    )
    return shell(
        tr("Pair your computer", "配对电脑"),
        tr(
            "Paste the code into the Fleet pairing prompt on the computer you want to connect.",
            "在需要连接的电脑上，将配对码粘贴到 Fleet 配对提示中。",
        ),
        code_panel + "<script>" + script + "</script>",
        slot_panel(slot, keys, binding, grants),
        viewer,
        back=PATH,
        back_label=tr("Gemini setup", "Gemini 设置"),
    )

WORKSPACE_DOWNLOAD = (
    "https://github.com/cdcupt/ccfleet/releases/download/workspace-mac-v1.0.1/CCFleet-Workspace.zip"
)


def pairing_summary(pairing):
    count = len(pairing["devices"])
    if count:
        summary = tr(f"{count} paired computer" + ("s" if count != 1 else ""),
                     f"已配对 {count} 台电脑")
    else:
        summary = tr("No paired computers", "尚无已配对的电脑")
    if pairing["enrollment_id"]:
        summary += " · " + tr("Reusable setup code active", "可重复使用的配对码已启用")
    pending = pairing["pending_count"]
    if pending:
        summary += " · " + tr(f"{pending} unused one-time code" + ("s" if pending != 1 else ""),
                              f"{pending} 个待使用的一次性配对码")
    return summary


def account_link(pairing):
    if pairing["devices"]:
        action = tr("Manage Gemini", "管理 Gemini")
    elif pairing["enrollment_id"] or pairing["pending_count"]:
        action = tr("Finish pairing", "完成配对")
    else:
        action = tr("Pair a computer", "配对电脑")
    return ('<p><strong>Gemini</strong> · ' + pairing_summary(pairing)
            + '</p><p><a class="btn" href="' + PATH + '#subscription">' + action + '</a></p>')


def workspace_form(csrf, fields, consent, action):
    return ('<form method="post" action="/account/gemini/workspace">' + hidden("csrf", csrf)
            + ''.join(hidden(key, value) for key, value in fields.items())
            + '<label class="gm-check"><input type="checkbox" required name="trust" value="1">'
            + consent
            + '</label><div class="gm-actions"><button class="primary" type="submit">'
            + action + '</button></div></form>')


def reusable_controls(csrf, enrollment_id, ready):
    consent = tr("I authorize anyone with this code to pair a Mac and access Antigravity "
                 "and its tools on my slot.", "我授权任何持有此码的人配对 Mac，"
                 "并使用我槽位上的 Antigravity 及其工具。")
    save = '<p>' + tr("The new code is shown once. Save it in your password manager.",
                      "新配对码只显示一次，请将它保存在密码管理器中。") + '</p>'
    scope = tr("Replacing or disabling the code stops future pairings with it. "
               "Remove a paired Mac individually to end its access.",
               "更换或停用配对码后，旧码不能再用于新配对。若要撤销已配对 Mac 的访问权限，"
               "请单独移除该设备。")
    if not enrollment_id:
        return ('<p>' + scope + '</p>' + save + workspace_form(
                csrf, {"operation": "create"}, consent,
                tr("Create reusable setup code", "创建可重复使用的配对码")) if ready else "")
    revision = {"enrollment_id": enrollment_id}
    replace = (save + workspace_form(csrf, {**revision, "operation": "replace"}, consent,
                                     tr("Replace setup code", "更换配对码")) if ready else "")
    disable = workspace_form(csrf, {**revision, "operation": "disable"},
                             tr("I want to stop new pairings with this code.",
                                "我要禁止使用此码继续配对新电脑。"),
                             tr("Disable setup code", "停用配对码"))
    return ('<details class="gm-disconnect"><summary>'
            + tr("Replace or disable the code", "更换或停用配对码") + '</summary><p>'
            + tr("If you lost your setup code, replace it to get a new one.",
                 "如已丢失配对码，可更换配对码以获取新码。") + ' ' + scope
            + '</p>' + replace + disable + '</details>')


def workspace_devices(csrf, slot_id, pairing):
    from . import usersite

    if not pairing["devices"]:
        return ""
    rows = []
    for device in pairing["devices"]:
        stamp = escape(resets.iso(device["created_at"]))
        remove = usersite._form(f"/account/slots/{slot_id}/cli-revoke", csrf, "Remove",
                                hidden("device_id", device["id"]), "quiet")
        rows.append('<li><b>' + escape(device["name"]) + '</b> · ' + tr("Paired", "配对时间")
                    + f' <time datetime="{stamp}" data-local>{stamp}</time> ' + remove + '</li>')
    warning = ('<p class="gm-secondary">' + tr(
        "When removing a computer that holds a copy of your setup code, replace or disable "
        "the code too. Otherwise that computer can pair again.",
        "若移除的电脑保存了配对码，请同时更换或停用此码，否则该电脑仍可再次配对。") + '</p>'
        if pairing["enrollment_id"] else "")
    return '<ul>' + ''.join(rows) + '</ul>' + warning


def pairing_help():
    return ('<details class="gm-disconnect"><summary>'
            + tr("Name a Mac or pair it again", "为 Mac 命名或重新配对") + '</summary><p>'
            + tr("To recognize each Mac on this page, give it a distinct name when you first pair.",
                 "首次配对时，可为每台 Mac 取不同名称，便于在此页识别。")
            + '</p><pre><code>ccfleet pair gemini --name "Work MacBook"</code></pre><p>'
            + tr("If you removed this Mac on the website, the usual pairing command still "
                 "reports its saved local profile. To reconnect it, run this command and "
                 "paste your current setup code.",
                 "如果你已在网站上移除这台 Mac，常规配对命令仍会提示本机已有配对记录。"
                 "要重新连接，请运行以下命令并按提示粘贴当前有效的配对码。")
            + '</p><pre><code>ccfleet pair gemini --replace</code></pre></details>')


def t3_setup():
    return ('<details class="gm-disconnect"><summary>'
            + tr("Add Gemini to T3 Code", "在 T3 Code 中添加 Gemini") + '</summary>'
            + agent_guide.t3_provider_steps() + '</details>')


def workspace_panel(csrf, ready, pairing, slot_id):
    offline = ('' if ready else '<p role="status">'
               + tr("The Antigravity workspace service is offline.",
                    "Antigravity 工作区服务当前不在线。") + '</p>')
    legacy_form = (workspace_form(csrf, {}, tr(
        "I trust one computer to use Antigravity and its tools on my slot Mac.",
        "我信任一台电脑使用槽位 Mac 上的 Antigravity 及其工具。"),
        tr("Create one-time code", "创建一次性配对码")) if ready else "")
    pending = ('<p>' + tr("If you already have an unused one-time code, paste it on the Mac "
                          "you’re pairing within 10 minutes of creation.",
                          "如已有未使用的一次性配对码，请在创建后 10 分钟内在待配对 Mac 上粘贴。")
               + '</p>') if pairing["pending_count"] else ''
    return ('<section class="gm-panel" id="subscription"><h2>'
            + tr("Antigravity subscription", "Antigravity 订阅") + '</h2><p><strong>'
            + pairing_summary(pairing) + '</strong></p>'
            + offline + '<p>'
            + tr("Open your project in T3 Code on your Mac. CCFleet connects it to Antigravity "
                 "on your private Mac slot. Sign in to Google on the slot. Your subscription "
                 "covers the model calls without a Gemini API key.",
                 "在这台 Mac 的 T3 Code 中打开项目。CCFleet 会连接专属 Mac 槽位上的 Antigravity。"
                 "只需在槽位登录 Google，模型调用使用订阅额度，无需 Gemini API 密钥。")
            + '</p><p class="gm-secondary">'
            + tr("The slot receives requested file contents and test output. These may reach "
                 "Google as model context.", "槽位会收到按需读取的文件内容和测试输出。"
                 "这些内容也可能作为模型上下文发送给 Google。")
            + '</p>'
            + reusable_controls(csrf, pairing["enrollment_id"], ready)
            + '<h3>' + tr("Pair each Mac once", "每台 Mac 只需配对一次") + '</h3><p>'
            + tr("Run these commands on each trusted Mac, including one already paired with "
                 "Claude. Paste your saved setup code at the hidden prompt. Pairing returns "
                 "you to the terminal.",
                 "在每台受信任的 Mac 上运行以下命令，已配对 Claude 的 Mac 也需要这一步。"
                 "按提示粘贴保存的配对码（输入不会显示）。配对完成后会返回终端。")
            + '</p><pre><code>ccfleet update\nccfleet pair gemini</code></pre>'
            + '<p><a href="/docs/guide#t3-install">'
            + tr("Install CCFleet on a new Mac", "在新 Mac 上安装 CCFleet") + '</a></p>'
            + t3_setup()
            + bi('<p>If T3 is already configured, open your project and choose CCFleet Gemini. '
                 'For a terminal chat, run <code>ccfleet gemini</code>.</p>',
                 '<p>如果 T3 已配置，打开项目并选择 CCFleet Gemini 即可。'
                 '若要在终端中对话，可运行 <code>ccfleet gemini</code>。</p>')
            + pending + pairing_help()
            + workspace_devices(csrf, slot_id, pairing)
            + '<details class="gm-disconnect"><summary>'
            + tr("Workspace app or older CLI", "Workspace 应用或旧版 CLI") + '</summary><p>'
            + tr("These clients use a one-time code valid for 10 minutes.",
                 "这些客户端使用一次性配对码，有效期为 10 分钟。")
            + '</p>' + legacy_form + '<p><a href="' + WORKSPACE_DOWNLOAD + '">'
            + tr("Download CCFleet Workspace for Mac", "下载 Mac 版 CCFleet Workspace")
            + '</a></p></details></section>')


def workspace_result(code, slot, keys, binding, grants, viewer, script, *, reusable):
    if reusable:
        title = tr("Reusable Gemini setup code", "可重复使用的 Gemini 配对码")
        instructions = tr("Save this code in your password manager now. It’s shown only once. "
                          "On each Mac you trust, update CCFleet and paste the same code "
                          "at the prompt. After pairing, open your Gemini connection in T3.",
                          "请立即将配对码保存到密码管理器，此码只显示一次。"
                          "在每台受信任的 Mac 上更新 CCFleet，再按提示粘贴同一个码。"
                          "配对后可在 T3 中打开 Gemini 连接。")
        command = "ccfleet update\nccfleet pair gemini"
        followup = pairing_help() + t3_setup()
    else:
        title = tr("One-time Antigravity pairing code", "Antigravity 一次性配对码")
        instructions = tr("On the Mac you’re pairing, open a terminal and run this command. "
                          "Paste the code at the prompt within 10 minutes.",
                          "在待配对 Mac 上打开终端并运行此命令，然后在 10 分钟内按提示粘贴配对码。")
        command = "ccfleet gemini"
        followup = ('<p><a href="' + WORKSPACE_DOWNLOAD + '">'
                    + tr("Download the Mac launcher", "下载 Mac 启动器") + '</a></p>')
    content = ('<p>' + instructions + '</p><pre><code>' + command + '</code></pre>'
               + '<pre class="gm-code" id="gemini-code">' + escape("agy1-" + code)
               + '</pre><div class="gm-copy-line"><button type="button" id="gemini-copy" hidden>'
               + tr("Copy code", "复制配对码") + '</button><span id="copy-status" role="status">'
               + '</span></div>' + followup + '<script>' + script + '</script>')
    return shell(tr("Connect your local project", "连接本地项目"),
                 tr("The Google sign-in stays on your private Mac slot.",
                    "Google 登录凭据保存在你的专属 Mac 槽位。"),
                 panel(title, content),
                 slot_panel(slot, keys, binding, grants), viewer, back=PATH + "#subscription",
                 back_label=tr("Gemini connections", "Gemini 连接"))
