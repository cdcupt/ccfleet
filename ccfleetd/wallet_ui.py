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
body.site:has(.wallet-topup-page) .topbar .doc-nav{display:none}
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
.wallet-ui .wallet-add-credit{display:inline-flex;align-items:center;justify-content:center;
gap:10px;padding:12px 20px;font-weight:650}
.wallet-ui .wallet-plus{font-size:21px;line-height:1;font-weight:400}
.wallet-topup-page{max-width:640px;margin:12px auto;padding:24px}
.wallet-topup-page .wallet-back{display:inline-flex;gap:7px;font-size:13px;color:var(--muted);
margin-bottom:18px;text-decoration:none}
.wallet-topup-page .wallet-eyebrow{margin:0 0 8px;font-size:11px;letter-spacing:.1em;
font-weight:650;text-transform:uppercase;color:var(--muted)}
.wallet-topup-page h1{font-size:clamp(27px,5vw,34px);letter-spacing:-.035em;
line-height:1.15;margin:0 0 8px}
.wallet-topup-page .wallet-intro{font-size:15px;color:var(--muted);margin:0}
.wallet-topup-page .wallet-current{display:flex;align-items:center;justify-content:space-between;
gap:16px;margin:18px 0;padding:12px 16px;background:var(--inset);border-radius:10px;
font-size:13px;color:var(--muted)}
.wallet-topup-page .wallet-current strong{font-size:23px;color:var(--ink);
font-variant-numeric:tabular-nums;overflow-wrap:anywhere;text-align:right}
.wallet-topup-page .wallet-progress{display:flex;list-style:none;
padding:0;margin:18px 0;gap:12px;font-size:12px;color:var(--muted)}
.wallet-topup-page .wallet-progress li{flex:1;display:flex;gap:8px;align-items:center;
min-width:0;overflow-wrap:anywhere}
.wallet-topup-page .wallet-step-number{width:23px;height:23px;
flex:0 0 23px;display:grid;place-items:center;border:1px solid var(--rule);border-radius:50%}
.wallet-topup-page .wallet-progress li[aria-current=step]{color:var(--ink);font-weight:600}
.wallet-topup-page .wallet-progress li[aria-current=step] .wallet-step-number{background:var(--acc);
border-color:var(--acc);color:var(--panel)}
.wallet-topup-page .wallet-topup-form{margin:0;display:grid;gap:14px}
.wallet-topup-page fieldset{min-width:0;margin:0;padding:0;border:0}
.wallet-topup-page legend{font-size:14px;font-weight:650;padding:0;margin-bottom:12px}
.wallet-topup-page .wallet-amounts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
gap:10px}
.wallet-topup-page .wallet-amount{position:relative;display:block;min-width:0;cursor:pointer}
.wallet-topup-page .wallet-amount input{position:absolute;opacity:0;width:1px;height:1px}
.wallet-topup-page .wallet-amount-tile{display:grid;place-items:center;gap:4px;min-height:78px;
padding:10px 6px;border:1px solid var(--rule);border-radius:12px;background:var(--panel);
transition:border-color .15s,background .15s,box-shadow .15s}
.wallet-topup-page .wallet-amount-tile strong{font-size:25px;line-height:1.2;
font-variant-numeric:tabular-nums;letter-spacing:-.035em;color:var(--ink)}
.wallet-topup-page .wallet-amount-tile small{font-size:11px;color:var(--muted)}
.wallet-topup-page .wallet-amount input:checked+.wallet-amount-tile{
border-color:var(--acc);box-shadow:inset 0 0 0 1px var(--acc);background:var(--inset)}
.wallet-topup-page .wallet-amount input:focus-visible+.wallet-amount-tile{
outline:2px solid var(--acc);outline-offset:3px}
.wallet-topup-page .wallet-amount:hover .wallet-amount-tile{border-color:var(--acc)}
.wallet-topup-page .wallet-checkout-button{width:100%;display:flex;align-items:center;
justify-content:center;gap:12px;min-height:50px;font-size:14px;font-weight:650}
.wallet-topup-page .wallet-payment-note{font-size:12px;text-align:center;
color:var(--muted);margin:0}
.wallet-topup-page .wallet-after{margin:20px 0 0;padding-top:16px;
border-top:1px solid var(--rule-soft);color:var(--muted)}
@media(max-width:560px){.wallet-ui{padding:16px;border-radius:12px}
.wallet-ui .wallet-actions{align-items:stretch}
.wallet-ui .wallet-actions>.btn,.wallet-ui .wallet-actions>form,
.wallet-ui .wallet-actions>form>button{width:100%}
.wallet-ui .wallet-review-data{gap:9px 12px}
.wallet-ui .wallet-steps{display:grid;gap:6px}
.wallet-topup-page{padding:22px 18px;margin:12px 0}
.wallet-topup-page .wallet-amounts{grid-template-columns:repeat(2,minmax(0,1fr))}
.wallet-topup-page .wallet-progress{gap:7px;font-size:11px}
.wallet-topup-page .wallet-progress li{gap:5px;align-items:flex-start}
.wallet-topup-page .wallet-current{padding:14px;gap:10px}}
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


def balance_card(balance_cents: int, *, topup_ready: bool = False,
                 compact: bool = False) -> str:
    shown = escape(wallet.display(balance_cents))
    if type(topup_ready) is not bool:
        raise ValueError("top-up readiness must be an explicit server boolean")
    if type(compact) is not bool:
        raise ValueError("compact credit display must be an explicit server boolean")
    if topup_ready:
        controls = ('<a class="btn primary wallet-add-credit" href="/account/topup">'
            + '<span class="wallet-plus" aria-hidden="true">+</span>'
            + label("Add credit", "充值", inline=True) + '</a>')
        help_text = ('<p class="wallet-muted" id="wallet-topup-help">'
            + label("Add credit by card for Fleet slot rentals. Your Claude or Devin "
                    "subscription is separate.",
                    "使用银行卡充值，余额用于租用 Fleet 槽位。Claude 或 Devin 订阅需要另付。",
                    inline=True) + '</p>')
    else:
        controls = ('<button type="button" disabled aria-describedby="wallet-topup-help">'
            + label("Card top-up unavailable", "银行卡充值暂不可用", inline=True)
            + '</button>')
        help_text = ('<p class="wallet-muted" id="wallet-topup-help">'
            + label("Online top-ups are unavailable for this account. "
                    "An admin can add Fleet credit.",
                    "此账户暂不可在线充值，可由管理员添加 Fleet 余额。", inline=True) + '</p>')
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
            + '<div class="wallet-actions">' + controls
            + '<a class="btn" href="/account/available">'
            + label("Choose a slot", "选择槽位", inline=True) + '</a>'
            + '</div>' + help_text + '</section>')


def topup_page(balance_cents: int, *, topup_ready: bool, csrf: str = "",
               idempotency_key: str = "", blocked: bool = False) -> str:
    """Choose an existing preset; creation and settlement stay at existing boundaries."""
    shown = escape(wallet.display(balance_cents))
    if type(topup_ready) is not bool or type(blocked) is not bool:
        raise ValueError("top-up readiness must be an explicit server boolean")
    active = topup_ready and not blocked
    controls = ''
    if active:
        if (not isinstance(csrf, str) or not 1 <= len(csrf) <= 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in csrf)):
            raise ValueError("a server CSRF nonce is required for top-ups")
        amounts = ''.join(
            '<label class="wallet-amount"><input type="radio" name="amount_cents" value="'
            + str(amount) + '"' + (' checked' if amount == 2000 else '') + ' required>'
            + '<span class="wallet-amount-tile"><strong>'
            + escape(wallet.display(amount).removesuffix('.00'))
            + '</strong><small>USD</small></span></label>'
            for amount in stripe_checkout.DEFAULT_AMOUNTS)
        controls = ('<form class="wallet-topup-form" method="post" action="/account/topup">'
            + _security(csrf, idempotency_key) + '<fieldset><legend>'
            + label("How much would you like to add?", "你想充值多少？", inline=True)
            + '</legend><div class="wallet-amounts">' + amounts + '</div></fieldset>'
            + '<button class="btn primary wallet-checkout-button" type="submit">'
            + label("Continue to secure payment", "前往安全付款页面", inline=True)
            + '<span aria-hidden="true">→</span></button>'
            + '<p class="wallet-payment-note">'
            + label("Pay by card on Stripe. One payment, no automatic charges.",
                    "在 Stripe 使用银行卡付款。仅充值一次，不会自动扣款。", inline=True)
            + '</p></form>')
    else:
        message = (("Your credit is under review. Top-ups are paused.",
                    "余额正在核查，暂时无法充值。") if blocked else
                   ("Card top-up is unavailable for this account. Ask the operator for help.",
                    "此账户暂时无法银行卡充值，请联系运营者。"))
        controls = '<p class="wallet-muted">' + label(*message, inline=True) + '</p>'
    progress = '<ol class="wallet-progress">' + ''.join(
        '<li' + (' aria-current="step"' if index == 0 else '') + '>'
        + '<span class="wallet-step-number" aria-hidden="true">' + str(index + 1) + '</span>'
        + label(en, zh, inline=True) + '</li>'
        for index, (en, zh) in enumerate((("Choose amount", "选择金额"),
            ("Pay on Stripe", "Stripe 付款"), ("Credit added", "余额到账")))) + '</ol>'
    return ('<section class="wallet-ui wallet-topup-page">'
            + '<a class="wallet-back" href="/account#wallet"><span aria-hidden="true">←</span>'
            + label("Your balance", "返回余额", inline=True) + '</a>'
            + '<p class="wallet-eyebrow">CC Fleet</p><h1>'
            + label("Add credit", "充值", inline=True) + '</h1><p class="wallet-intro">'
            + label("Top up now. Choose or renew your slot when you are ready.",
                    "先充值，再按需要选择或续费槽位。", inline=True)
            + '</p><div class="wallet-current"><span>'
            + label("Current balance", "当前余额", inline=True) + '</span><strong>'
            + shown + '</strong></div>' + progress + controls
            + '<div class="wallet-after"><p>'
            + label("After payment is confirmed, the credit appears in your Fleet balance. "
                    "Adding credit does not buy or renew a slot automatically.",
                    "付款确认后，余额会添加到你的 Fleet 账户。充值不会自动购买或续费槽位。",
                    inline=True) + '</p><p>'
            + label("Fleet credit pays for our slot rentals only. It cannot be transferred "
                    "or withdrawn. Claude and Devin subscriptions are separate. "
                    "Alipay is not available.",
                    "Fleet 余额仅用于支付槽位租金，不能转让或提现。Claude 和 Devin 订阅需要另付。"
                    "暂不支持支付宝。", inline=True) + '</p></div></section>')


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
                "Manual service credit. This does not charge a card. "
                "Record a private admin reason.",
                "手动添加 Fleet 余额，不会从用户银行卡扣款。请填写管理员内部备注。",
                inline=True) + '</p>'
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
