"""Pure USD-credit components. Callers supply authorized data and form nonces."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional
from urllib.parse import quote

from . import stripe_checkout, wallet
from .bilingual import label

CSS = """
.wallet-ui{min-width:0;max-width:100%;margin:20px 0;padding:20px;
border:1px solid var(--rule-soft);border-radius:14px;background:var(--panel)}
.wallet-ui h2{margin:0 0 12px;font-size:18px;line-height:1.3}
.wallet-ui p{font-size:13px;line-height:1.65;overflow-wrap:anywhere}
.wallet-ui .wallet-muted{color:var(--muted)}
.wallet-ui .wallet-balance{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 12px}
.wallet-ui .wallet-balance strong{font-size:clamp(28px,4vw,40px);line-height:1.2;
letter-spacing:-.03em;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.wallet-ui .wallet-balance small{font-size:12px;color:var(--muted)}
.wallet-ui .wallet-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:16px}
.wallet-ui .wallet-actions>form{margin:0;min-width:0;max-width:100%}
.wallet-ui .btn,.wallet-ui button{min-height:42px;white-space:normal;overflow-wrap:anywhere}
.wallet-ui button:disabled{opacity:.58;cursor:not-allowed}
.wallet-ui .wallet-steps{display:flex;flex-wrap:wrap;gap:8px 22px;padding-left:22px;
margin:16px 0 0;font-size:12px;color:var(--muted)}
.wallet-ui .wallet-steps li{padding-left:2px;max-width:100%;overflow-wrap:anywhere}
.wallet-ui .wallet-ledger-sheet{overflow-x:auto;max-width:100%;min-width:0;
border:1px solid var(--rule-soft);border-radius:10px;margin-top:14px}
.wallet-ui .wallet-ledger{width:100%;min-width:360px;border-collapse:collapse;
font-size:12px;white-space:normal;table-layout:auto}
.wallet-ui .wallet-ledger th,.wallet-ui .wallet-ledger td{text-align:left;
white-space:normal;vertical-align:middle;padding:10px 12px;border-bottom:1px solid var(--rule-soft)}
.wallet-ui .wallet-ledger th{font-weight:600;color:var(--muted);background:var(--inset)}
.wallet-ui .wallet-ledger tr:last-child td{border-bottom:0}
.wallet-ui .wallet-ledger th:last-child,.wallet-ui .wallet-ledger td:last-child{
text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.wallet-ui .wallet-credit{color:var(--ok)}
.wallet-ui .wallet-target{font-weight:650;font-size:16px;overflow-wrap:anywhere}
.wallet-ui .wallet-review-data{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
gap:9px 18px;margin:16px 0}
.wallet-ui .wallet-review-data dt{color:var(--muted);font-size:13px}
.wallet-ui .wallet-review-data dd{margin:0;text-align:right;font-size:14px;
font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.wallet-ui .wallet-review-data .wallet-total{font-weight:700;padding-top:10px;
border-top:1px solid var(--rule-soft)}
.wallet-ui .wallet-form{display:grid;gap:14px;min-width:0;max-width:100%;margin-top:16px}
.wallet-ui .wallet-field{display:grid;gap:7px;min-width:0;margin:0;font-size:13px}
.wallet-ui .wallet-field input,.wallet-ui .wallet-field select{box-sizing:border-box;
width:100%;min-width:0;max-width:100%;
min-height:42px;padding:10px 12px;border:1px solid var(--rule);border-radius:9px;
background:var(--panel);color:var(--ink);font:inherit}
.wallet-ui .wallet-field small{color:var(--muted);font-size:12px}
.wallet-ui input:focus-visible,.wallet-ui select:focus-visible,.wallet-ui button:focus-visible,
.wallet-ui a:focus-visible,.wallet-ui .wallet-ledger-sheet:focus-visible{
outline:2px solid var(--acc);outline-offset:3px}
.wallet-ui .wallet-topup{display:flex;flex-wrap:wrap;align-items:end;gap:10px}
.wallet-ui .wallet-topup .wallet-field{min-width:130px;flex:1}
@media(max-width:560px){.wallet-ui{padding:16px;border-radius:12px}
.wallet-ui .wallet-actions{align-items:stretch}
.wallet-ui .wallet-actions>.btn,.wallet-ui .wallet-actions>form,
.wallet-ui .wallet-actions>form>button{width:100%}
.wallet-ui .wallet-review-data{gap:9px 12px}
.wallet-ui .wallet-steps{display:grid;gap:6px}}
"""


def _hidden(name: str, value: Any) -> str:
    return ('<input type="hidden" name="' + name + '" value="'
            + escape(str(value), quote=True) + '">')


def _security(csrf: str, idempotency_key: str) -> str:
    return _hidden("csrf", csrf) + _hidden("idempotency_key", wallet.request_id(idempotency_key))


def _month() -> str:
    return ('<p class="wallet-muted">' + label(
        "One calendar month in UTC. Renew manually. No automatic charges.",
        "按 UTC 日历计算一个月。需要手动续费，不会自动扣款。", inline=True) + '</p>')


def _date(value: Any) -> str:
    try:
        moment = datetime.fromtimestamp(wallet.instant(value), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return "—"
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def balance_card(balance_cents: int, *, topup_ready: bool = False, csrf: str = "",
                 idempotency_key: str = "", compact: bool = False) -> str:
    shown = escape(wallet.display(balance_cents))
    if type(topup_ready) is not bool:
        raise ValueError("top-up readiness must be an explicit server boolean")
    if type(compact) is not bool:
        raise ValueError("compact credit display must be an explicit server boolean")
    if topup_ready:
        if (not isinstance(csrf, str) or not 1 <= len(csrf) <= 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in csrf)):
            raise ValueError("a server CSRF nonce is required for top-ups")
        controls = ('<form class="wallet-topup" method="post" action="/account/topup">'
            + _security(csrf, idempotency_key) + '<label class="wallet-field">'
            + label("Amount · USD", "金额 · 美元", inline=True)
            + '<select name="amount_cents">' + ''.join(
                '<option value="' + str(amount) + '"'
                + (' selected' if amount == 2000 else '') + '>'
                + escape(wallet.display(amount)) + '</option>'
                for amount in stripe_checkout.DEFAULT_AMOUNTS) + '</select></label>'
            + '<button class="btn primary" type="submit">'
            + label("Pay by card", "银行卡充值", inline=True) + '</button></form>'
            + '<button type="button" disabled aria-describedby="wallet-alipay-help">'
            + label("Alipay · coming later", "支付宝 · 暂未开通", inline=True) + '</button>')
        help_text = ('<p class="wallet-muted" id="wallet-topup-help">'
            + label("Pay on Stripe. Credit appears after payment is confirmed. "
                    "No automatic charges.",
                    "在 Stripe 页面付款，确认到账后添加余额。不会自动扣款。", inline=True)
            + '</p><p class="wallet-muted" id="wallet-alipay-help">'
            + label("Alipay is not available yet.", "支付宝充值暂未开通。", inline=True) + '</p>')
    else:
        controls = ('<button type="button" disabled aria-describedby="wallet-topup-help">'
            + label("Add credit · card / Alipay", "充值 · 银行卡／支付宝", inline=True)
            + '</button>')
        help_text = ('<p class="wallet-muted" id="wallet-topup-help">'
            + label("Online top-ups are not connected yet. An admin can add Fleet credit.",
                    "在线充值暂未开通，可由管理员添加 Fleet 余额。", inline=True) + '</p>')
    steps = ('' if compact else '<ol class="wallet-steps">' + ''.join(
        '<li>' + label(en, zh, inline=True) + '</li>'
        for en, zh in (("Choose a slot", "选择槽位"),
                       ("Review the monthly price", "确认月费"),
                       ("Buy the first month", "购买首月"))) + '</ol>')
    return ('<section class="wallet-ui wallet-card" id="wallet"><h2>'
            + label("Fleet credit", "Fleet 余额", inline=True) + '</h2>'
            + '<div class="wallet-balance"><strong>' + shown + '</strong><small>'
            + label("USD service credit", "美元服务余额", inline=True) + '</small></div>'
            + steps
            + '<div class="wallet-actions"><a class="btn primary" href="/account/available">'
            + label("Choose a slot", "选择槽位", inline=True) + '</a>'
            + controls + '</div>' + help_text + '</section>')


def owner_ledger(entries: Sequence[Mapping[str, Any]], *, limit: int = 8) -> str:
    """Only generic activity/date/amount are public; private notes never render."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("ledger limit must be an integer from 1 to 50")
    rows = []
    for entry in entries[:limit]:
        kind = entry.get("kind") if isinstance(entry.get("kind"), str) else ""
        en, zh = {"admin_credit": ("Credit added", "余额入账"),
                  "admin_debit": ("Credit removed", "余额扣减"),
                  "purchase_return": ("Service credit returned", "服务余额退回"),
                  "stripe_topup": ("Online top-up", "在线充值"),
                  "slot_purchase": ("Slot month", "槽位月费")}.get(
                      kind, ("Credit adjustment", "余额调整"))
        try:
            if entry.get("currency") != "USD":
                raise wallet.WalletError("only USD is displayed")
            delta = entry.get("delta_cents")
            shown = wallet.display(delta)
            if delta > 0:
                shown = "+" + shown
        except wallet.WalletError:
            delta, shown = 0, "—"
        tone = ' class="wallet-credit"' if delta > 0 else ''
        rows.append('<tr><td>' + escape(_date(entry.get("created_at")))
                    + '</td><td>' + label(en, zh, inline=True) + '</td><td'
                    + tone + '>' + escape(shown) + '</td></tr>')
    body = ('<div class="wallet-ledger-sheet" role="region" tabindex="0" '
            'aria-label="Recent credit activity" data-aria-label-en="Recent credit activity" '
            'data-aria-label-zh="近期余额记录"><table class="wallet-ledger"><thead><tr>'
            + ''.join('<th scope="col">' + label(en, zh, inline=True) + '</th>' for en, zh in (
                ("Date · UTC", "日期 · UTC"), ("Activity", "项目"),
                ("Amount · USD", "金额 · 美元")))
            + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>' if rows else
            '<p class="wallet-muted">'
            + label("No credit activity yet.", "暂无余额记录。", inline=True)
            + '</p>')
    return ('<section class="wallet-ui wallet-history"><h2>'
            + label("Recent credit activity", "近期余额记录", inline=True)
            + '</h2>' + body + '</section>')


def _confirmation(*, title: tuple[str, str], target_html: str, expected_cents: int,
                  balance_cents: int, csrf: str, idempotency_key: str,
                  action: str, fields: str, renewal: bool, extra: str = "") -> str:
    price = wallet.display(wallet.positive_cents(expected_cents))
    balance = wallet.display(balance_cents)
    nonce = _security(csrf, idempotency_key)
    rows = [("Your Fleet credit", "当前 Fleet 余额", balance),
            ("One month", "一个月费用", price)]
    if balance_cents >= expected_cents:
        rows.append(("Credit left", "购买后余额", wallet.display(balance_cents - expected_cents)))
    summary = '<dl class="wallet-review-data">' + ''.join(
        '<dt>' + label(en, zh, inline=True) + '</dt><dd>' + escape(value) + '</dd>'
        for en, zh, value in rows) + '</dl>'
    if balance_cents >= expected_cents:
        button = label(("Renew one month · " if renewal else "Buy first month · ") + price,
                       ("续费一个月 · " if renewal else "购买首月 · ") + price, inline=True)
        controls = ('<form method="post" action="' + action + '">' + nonce + fields
                    + _hidden("expected_cents", expected_cents)
                    + '<button class="btn primary" type="submit">' + button + '</button></form>')
    else:
        shortfall = expected_cents - balance_cents
        needed = wallet.display(shortfall) if shortfall <= wallet.MAX_CENTS else ""
        message = label("You need " + needed + " more Fleet credit." if needed else
                        "Add Fleet credit before buying or renewing.",
                        "还需要 " + needed + " Fleet 余额。" if needed else
                        "购买或续费前，请先添加 Fleet 余额。", inline=True)
        controls = ('<button class="btn" type="button" disabled>'
                    + label("Not enough credit", "余额不足", inline=True)
                    + '</button><p class="wallet-muted">' + message + '</p>')
    return ('<section class="wallet-ui wallet-review"><h2>' + label(*title, inline=True)
            + '</h2><p class="wallet-target">' + target_html + '</p>' + summary
            + _month() + extra + '<div class="wallet-actions">' + controls + '</div></section>')


def purchase_confirmation(*, machine_number: int, expected_cents: int, balance_cents: int,
                          csrf: str, idempotency_key: str) -> str:
    if type(machine_number) is not int or not 1 <= machine_number <= 2**63 - 1:
        raise ValueError("a public machine number is required")
    target = label(f"Machine {machine_number}", f"主机 {machine_number}", inline=True)
    return _confirmation(title=("Review this slot", "确认购买槽位"), target_html=target,
        expected_cents=expected_cents, balance_cents=balance_cents, csrf=csrf,
        idempotency_key=idempotency_key, action="/account/purchase",
        fields=_hidden("machine_number", machine_number), renewal=False)


def renewal_confirmation(*, slot_id: str, slot_name: str, claim_id: str, expected_cents: int,
                         balance_cents: int, csrf: str, idempotency_key: str,
                         current_order: Optional[Mapping[str, Any]] = None) -> str:
    fields = (_hidden("slot_id", wallet.identity(slot_id))
              + _hidden("claim_id", wallet.identity(claim_id)))
    extra = '<p class="wallet-muted">' + label(
        "If you still have time left, the new month starts after it. Otherwise it starts now.",
        "尚未到期时，从当前到期时间续一个月；已到期则从现在起算。", inline=True) + '</p>'
    if current_order is not None:
        end = _date(current_order.get("period_end_at"))
        if end != "—":
            extra += '<p class="wallet-muted">' + label(
                "Currently paid through: " + end, "当前已付费至：" + end, inline=True) + '</p>'
    return _confirmation(title=("Renew this slot", "续费此槽位"), target_html=escape(slot_name),
        expected_cents=expected_cents, balance_cents=balance_cents, csrf=csrf,
        idempotency_key=idempotency_key, action="/account/renew", fields=fields,
        renewal=True, extra=extra)


def admin_credit_grant_form(*, account_id: str, csrf: str, idempotency_key: str) -> str:
    action = "/actions/account/" + quote(wallet.identity(account_id), safe="") + "/credit"
    return ('<section class="wallet-ui wallet-admin"><h2>'
            + label("Add Fleet credit", "添加 Fleet 余额", inline=True) + '</h2>'
            + '<p class="wallet-muted">' + label(
                "Manual service credit. Record a private admin reason.",
                "手动添加服务余额，请填写管理员内部备注。", inline=True) + '</p>'
            + '<form class="wallet-form" method="post" action="' + escape(action, quote=True) + '">'
            + _security(csrf, idempotency_key)
            + '<label class="wallet-field"><span>'
            + label("Amount · USD", "金额 · 美元", inline=True)
            + '</span><input type="text" name="amount" inputmode="decimal" maxlength="12" '
            'pattern="[0-9]{1,9}([.][0-9]{1,2})?" placeholder="20.00" autocomplete="off" required>'
            '<small>' + label("Use dollars and cents, for example 20.50.",
                             "填写美元金额，例如 20.50。", inline=True) + '</small></label>'
            + '<label class="wallet-field"><span>'
            + label("Private reason", "内部备注", inline=True)
            + '</span><input type="text" name="reason" maxlength="' + str(wallet.REASON_MAX)
            + '" autocomplete="off" required></label><div class="wallet-actions">'
            '<button class="btn primary" type="submit">'
            + label("Add credit", "添加余额", inline=True) + '</button></div></form></section>')
