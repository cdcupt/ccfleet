"""Self-service keys and receipts for the slot-independent, metered gateway."""

from decimal import Decimal
from html import escape

from . import usersite, wallet
from .bilingual import label
from .openrouter import PAGE


def page(service, cfg, account, session_id, *, issued=None):
    if not service.models:
        return usersite.not_found()
    if account is None:
        body = (
            "<h1>OpenRouter</h1><p>"
            + label(
                "Use local Claude Code with Fleet credit.", "用 Fleet 余额在本机使用 Claude Code。"
            )
            + "</p>"
            '<a class="btn" href="/auth/google/start?next=/account/openrouter">'
            + label("Sign in", "登录")
            + "</a>"
        )
        return usersite.Outcome(200, body=usersite._shell("OpenRouter", body))
    owner = account["id"]
    csrf = escape(usersite.csrf_for(session_id, cfg.cookie_secret), quote=True)

    def form(action, en, zh):
        return (
            f'<form method="post" action="{PAGE}/{action}">'
            f'<input type="hidden" name="csrf" value="{csrf}"><button class="btn">'
            + label(en, zh, inline=True)
            + "</button></form>"
        )

    balance = wallet.display(service.store.wallet_balance(owner))
    body = (
        '<h1>OpenRouter</h1><div class="card"><p>'
        + label(
            "Your Fleet balance pays the OpenRouter usage cost plus a 5% service fee. "
            "OpenRouter and the selected provider receive the conversation and tool results.",
            "Fleet 余额支付 OpenRouter 的实际用量费及 5% 服务费。"
            "OpenRouter 和所选服务商会收到对话和工具结果。",
        )
        + "</p><p>"
        + label("Available Fleet credit", "可用 Fleet 余额", inline=True)
        + " · "
        + balance
        + ' · <a href="/account/topup">'
        + label("Add credit", "充值", inline=True)
        + "</a></p>"
        + form("create", "Issue a key and replace any previous key", "生成密钥并替换旧密钥")
        + form("revoke", "Revoke access", "撤销密钥")
    )
    if issued:
        body += (
            "<p>"
            + label(
                "Save this Fleet key now. It is shown once.", "请保存此 Fleet 密钥，只显示一次。"
            )
            + "</p><pre>"
            + escape(issued)
            + "</pre>"
        )
    body += (
        "<p>"
        + label(
            "Run in your project and enter your Fleet key at the hidden prompt. "
            "Use /model to switch models.",
            "在项目目录运行以下命令，在隐藏提示中输入 Fleet 密钥。使用 /model 切换模型。",
        )
        + "</p><pre>ccfleet openrouter</pre></div>"
        "<h2>" + label("Recent usage", "最近用量") + "</h2>"
    )
    for row in service.store.openrouter_receipts(owner):
        cost = row["cost_usd"]
        if cost is None:
            detail = (
                label("Released", "已解除预留")
                if row["state"] == "released"
                else label("Reserved", "已预留") + " " + wallet.display(row["hold_cents"])
            )
        else:
            fee = Decimal(cost) * Decimal("0.05")
            detail = (
                label("Usage", "用量费")
                + " $"
                + cost
                + " + "
                + label("fee", "服务费")
                + " $"
                + format(fee, "f")
                + " = $"
                + format(Decimal(cost) + fee, "f")
            )
        body += "<p><code>" + escape(row["model"]) + "</code> · " + detail + "</p>"
    viewer = usersite.viewer_for(account, session_id, cfg, service.store)
    return usersite.Outcome(200, body=usersite._shell("OpenRouter", body, viewer=viewer))
