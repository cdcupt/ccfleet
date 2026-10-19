"""Pure own-account monthly-slot views; no billing or entitlement side effects."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional

from . import wallet
from .bilingual import label

CSS = """
body.site:has(.subscription-review) .topbar .doc-nav{display:none}
.subscription-ui{min-width:0;max-width:100%;margin:20px 0;padding:24px;
border:1px solid var(--rule-soft);border-radius:14px;background:var(--panel)}
.subscription-ui h1{margin:0 0 10px;font-size:clamp(26px,4vw,32px);
letter-spacing:-.035em;line-height:1.2}
.subscription-ui h2{margin:0;font-size:18px;line-height:1.3}
.subscription-ui h3{margin:0;font-size:15px;overflow-wrap:anywhere}
.subscription-ui p{font-size:13px;line-height:1.65;overflow-wrap:anywhere}
.subscription-ui .subscription-muted{color:var(--muted)}
.subscription-ui .subscription-back{display:inline-flex;gap:7px;font-size:13px;
color:var(--muted);margin-bottom:20px;text-decoration:none}
.subscription-ui .subscription-price{padding:20px;margin:22px 0;background:var(--inset);
border:1px solid var(--rule-soft);border-radius:12px}
.subscription-ui .subscription-price p{margin:0 0 8px;font-weight:600}
.subscription-ui .subscription-price strong{font-size:36px;letter-spacing:-.04em;
line-height:1.2;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.subscription-ui .subscription-price small{font-size:14px;color:var(--muted);margin-left:6px}
.subscription-ui .subscription-facts{display:grid;gap:10px;padding-left:20px;
margin:20px 0;font-size:13px;line-height:1.6}
.subscription-ui .subscription-consent{display:flex;align-items:flex-start;gap:10px;
padding:16px;border:1px solid var(--rule);border-radius:10px;cursor:pointer;
font-size:13px;line-height:1.6;background:var(--inset)}
.subscription-ui .subscription-consent input{flex:0 0 auto;width:18px;height:18px;
margin:2px 0 0;accent-color:var(--acc)}
.subscription-ui .subscription-submit{display:flex;align-items:center;justify-content:center;
gap:10px;width:100%;min-height:48px;margin-top:16px;white-space:normal;
font-size:14px;font-weight:650;overflow-wrap:anywhere}
.subscription-ui .subscription-payment-note{margin:10px 0 0;text-align:center;
font-size:12px;color:var(--muted)}
.subscription-ui .subscription-separate{border-top:1px solid var(--rule-soft);
padding-top:16px;margin:20px 0 0;color:var(--muted)}
.subscription-ui .subscription-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
gap:12px;margin:18px 0}
.subscription-ui .subscription-card{min-width:0;border:1px solid var(--rule-soft);
border-radius:12px;padding:18px;background:var(--inset)}
.subscription-ui .subscription-card-head{display:flex;flex-wrap:wrap;align-items:center;
justify-content:space-between;gap:8px 12px}
.subscription-ui .subscription-status{display:inline-flex;padding:4px 8px;
border:1px solid var(--rule);border-radius:999px;font-size:11px;line-height:1.35;
font-weight:600;color:var(--muted);max-width:100%;overflow-wrap:anywhere}
.subscription-ui .subscription-status.active{color:var(--ok,#22784f);border-color:currentColor}
.subscription-ui .subscription-status.attention{color:var(--warn,#94620a);
border-color:currentColor}
.subscription-ui .subscription-card-price{font-size:24px;font-weight:650;
letter-spacing:-.035em;font-variant-numeric:tabular-nums;margin:18px 0 12px}
.subscription-ui .subscription-card-price small{font-size:12px;letter-spacing:0;
font-weight:400;color:var(--muted)}
.subscription-ui .subscription-dates{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
gap:9px 14px;margin:0;font-size:12px;line-height:1.5}
.subscription-ui .subscription-dates dt{color:var(--muted)}
.subscription-ui .subscription-dates dd{margin:0;text-align:right;overflow-wrap:anywhere}
.subscription-ui .subscription-cancel-note{margin:14px 0 0;font-size:12px;color:var(--muted)}
.subscription-ui .subscription-cancel-form{margin:16px 0 0;padding-top:14px;
border-top:1px solid var(--rule-soft)}
.subscription-ui .subscription-cancel-form button{min-height:42px;white-space:normal}
.subscription-ui .subscription-cancel-form p{margin:8px 0 0;font-size:12px;color:var(--muted)}
.subscription-ui .subscription-checkout{margin-top:16px;padding-top:14px;
border-top:1px solid var(--rule-soft)}
.subscription-ui .subscription-checkout-actions{display:flex;flex-wrap:wrap;gap:8px}
.subscription-ui .subscription-checkout-actions form{margin:0;max-width:100%}
.subscription-ui .subscription-checkout button{min-height:42px;white-space:normal}
.subscription-ui .subscription-checkout button:disabled{opacity:.58;cursor:not-allowed}
.subscription-ui .subscription-checkout p{margin:10px 0 0;font-size:12px;color:var(--muted)}
.subscription-ui .subscription-manage{display:flex;flex-wrap:wrap;align-items:center;
gap:10px 16px;margin-top:18px}
.subscription-ui .subscription-manage form{margin:0;max-width:100%}
.subscription-ui .subscription-manage .btn{min-height:44px;white-space:normal;
overflow-wrap:anywhere}
.subscription-ui .subscription-manage p{margin:0;color:var(--muted);font-size:12px}
.subscription-ui input:focus-visible,.subscription-ui button:focus-visible,
.subscription-ui a:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
@media(max-width:560px){.subscription-ui{padding:18px 16px;border-radius:12px}
.subscription-ui .subscription-grid{grid-template-columns:minmax(0,1fr)}
.subscription-ui .subscription-price{padding:16px;margin:18px 0}
.subscription-ui .subscription-price strong{font-size:32px}
.subscription-ui .subscription-manage,.subscription-ui .subscription-manage form,
.subscription-ui .subscription-manage .btn{width:100%}
.subscription-ui .subscription-manage .btn{text-align:center}
.subscription-ui .subscription-dates{gap:8px 10px}}
"""


def _hidden(name: str, value: Any) -> str:
    return ('<input type="hidden" name="' + name + '" value="'
            + escape(str(value), quote=True) + '">')


def _csrf(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("a server CSRF nonce is required")
    return _hidden("csrf", value)


def _machine(number: Any) -> bool:
    return type(number) is int and 1 <= number <= 2**63 - 1


def review(machine_number: int, monthly_cents: int, *, csrf: str,
           idempotency_key: str) -> str:
    """Display an explicit new-slot recurring choice; no prechecked consent."""
    if not _machine(machine_number):
        raise ValueError("a public machine number is required")
    price = escape(wallet.display(wallet.positive_cents(monthly_cents)))
    fields = (_csrf(csrf) + _hidden("machine_number", machine_number)
              + _hidden("expected_cents", monthly_cents)
              + _hidden("idempotency_key", wallet.request_id(idempotency_key)))
    facts = (
        ("Your first payment is completed on Stripe before the slot starts.",
         "先到 Stripe 完成首笔付款，槽位才会开通。"),
        ("It renews monthly until you cancel. Stripe shows the billing dates.",
         "之后每月续费，直到你取消；账单日期以 Stripe 显示为准。"),
        ("Cancel renewal at any time. It stops after Stripe’s current billing period; "
         "access is limited to time already paid.",
         "可随时停止续费，在 Stripe 当前账期结束时生效；只能使用已付费的服务时间。"),
        ("An unpaid invoice already issued may still be retried after you request cancellation.",
         "申请取消后，已经开出的未付款账单仍可能重试扣款。"),
    )
    return ('<section class="subscription-ui subscription-review">'
            + '<a class="subscription-back" href="/account/available">'
            + '<span aria-hidden="true">←</span>'
            + label("Back to slots", "返回槽位列表", inline=True) + '</a><h1>'
            + label("Make this slot monthly", "为这个槽位开通月付", inline=True) + '</h1><p '
            + 'class="subscription-muted">'
            + label("Choose monthly payment for this slot only. Other slots keep their own plan.",
                    "只为这个槽位选择月付，其他槽位的付费方式保持不变。", inline=True)
            + '</p><div class="subscription-price"><p>'
            + label(f"Machine {machine_number}", f"主机 {machine_number}", inline=True)
            + '</p><strong>' + price + '</strong><small>'
            + label("USD / month", "美元／月", inline=True) + '</small></div>'
            + '<ul class="subscription-facts">' + ''.join(
                '<li>' + label(en, zh, inline=True) + '</li>' for en, zh in facts)
            + '</ul><form method="post" action="/account/subscribe">' + fields
            + '<label class="subscription-consent"><input type="checkbox" '
            + 'name="monthly_consent" value="yes" required><span>'
            + label("I agree to pay " + wallet.display(monthly_cents)
                    + " USD each month for this slot until I cancel.",
                    "我同意每月为这个槽位支付 " + wallet.display(monthly_cents)
                    + " 美元，直到我取消。", inline=True)
            + '</span></label><p class="subscription-payment-note">'
            + '<a href="/docs/terms">'
            + label("Read the service terms", "阅读服务条款", inline=True)
            + '</a></p><button class="btn primary subscription-submit" type="submit">'
            + label("Continue to Stripe", "前往 Stripe 付款", inline=True)
            + '<span aria-hidden="true">→</span></button></form>'
            + '<p class="subscription-payment-note">'
            + label("Monthly payment starts only after you agree and complete checkout.",
                    "只有你同意并完成付款后，才会开通月付。", inline=True) + '</p>'
            + '<p class="subscription-separate">'
            + label("Fleet credit is not used for this payment. Your Claude or Devin "
                    "subscription is paid separately.",
                    "本次付款不使用 Fleet 余额。Claude 或 Devin 订阅需要另外支付。",
                    inline=True) + '</p></section>')


def _date(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(wallet.instant(value), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return None


def _status(order: Mapping[str, Any]) -> tuple[str, str, str]:
    if order.get("payment_review_required") is True:
        return "Payment review required", "付款需要核查", ""
    if order.get("cancel_required") is True:
        return "Cancellation pending", "正在确认取消", ""
    state, provider = order.get("status"), order.get("provider_status")
    provider = provider if isinstance(provider, str) else ""
    if state == "closed":
        return (("Ended", "已结束", "") if _bound(order) else
                ("Not started", "尚未开通", ""))
    if provider == "canceled":
        return "Renewal stopped", "已停止续费", ""
    if provider == "incomplete_expired":
        return "Checkout expired", "付款已过期", ""
    if state == "active":
        if provider in {"past_due", "unpaid", "incomplete", "paused"}:
            return "Check payment", "请检查付款", "attention"
        if order.get("cancel_at_period_end") in (True, 1):
            return "Ends after this period", "本期结束后停止", ""
        return "Monthly active", "月付进行中", "active"
    return {
        "reserved": ("Preparing checkout", "正在准备付款", ""),
        "checkout_open": ("Awaiting first payment", "等待首笔付款", ""),
        "unknown": ("Checking payment", "正在确认付款", "attention"),
    }.get(state if isinstance(state, str) else "", ("Status pending", "状态待确认", ""))


def _bound(order: Mapping[str, Any]) -> bool:
    identity = order.get("subscription_id")
    return isinstance(identity, str) and identity.startswith("sub_") and len(identity) > 4


def _cancel_form(order: Mapping[str, Any], csrf: str) -> str:
    """A local own-order command; provider binding is verified by the POST handler."""
    nonce = order.get("cancel_idempotency_key")
    if (not _bound(order) or nonce is None or order.get("status") == "closed"
            or order.get("cancel_at_period_end") in (True, 1)
            or order.get("provider_status") in ("canceled", "incomplete_expired")):
        return ''
    return ('<form class="subscription-cancel-form" method="post" '
            + 'action="/account/subscriptions/cancel">' + _csrf(csrf)
            + _hidden("order_id", wallet.identity(order.get("id")))
            + _hidden("idempotency_key", wallet.request_id(nonce))
            + '<button class="btn" type="submit">'
            + label("Cancel renewal", "停止续费", inline=True) + '</button><p>'
            + label("Once confirmed, renewal stops after Stripe’s current billing period. "
                    "Access is limited to paid time. "
                    "An existing unpaid invoice may still be retried. "
                    "Files are not deleted automatically.",
                    "续费在 Stripe 当前账期结束后停止；只能使用已付费的服务时间。"
                    "已有未付款账单仍可能重试扣款，不会自动删除文件。", inline=True)
            + '</p></form>')


def _checkout_controls(order: Mapping[str, Any], csrf: str) -> str:
    nonce = order.get("checkout_idempotency_key")
    if nonce is None or order.get("status") == "closed" or order.get("claim_id"):
        return ''
    fields = (_csrf(csrf) + _hidden("order_id", wallet.identity(order.get("id")))
              + _hidden("idempotency_key", wallet.request_id(nonce)))
    if order.get("payment_review_required") is True:
        resume = ('<button class="btn primary" type="button" disabled>'
                  + label("Checkout paused for review", "付款核查中，暂时无法继续", inline=True)
                  + '</button>')
    else:
        resume = ('<form method="post" action="/account/subscriptions/resume">' + fields
                  + '<button class="btn primary" type="submit">'
                  + label("Continue checkout", "继续付款", inline=True) + '</button></form>')
    return ('<div class="subscription-checkout"><div class="subscription-checkout-actions">'
            + resume + '<form method="post" action="/account/subscriptions/cancel-checkout">'
            + fields + '<button class="btn" type="submit">'
            + label("Cancel checkout", "取消这次付款", inline=True) + '</button></form></div><p>'
            + label("An unpaid checkout closes only after Stripe confirms it has expired. "
                    "If payment already completed, you keep that paid month; confirmed "
                    "cancellation stops future renewal. It does not automatically refund money.",
                    "未付款的页面只有在 Stripe 确认已过期后才会关闭。若你已经付款，已付费的月份"
                    "仍会保留，取消确认后停止今后的续费，不会自动退款。", inline=True)
            + '</p></div>')


def summary(orders: Sequence[Mapping[str, Any]], *, csrf: str,
            management_ready: bool = True) -> str:
    """Render already-authorized local rows without reflecting provider identities."""
    if type(management_ready) is not bool:
        raise ValueError("management readiness must be an explicit server boolean")
    cards = []
    bound = False
    payment_review = False
    cancellation_pending = False
    for order in orders:
        if not isinstance(order, Mapping):
            continue
        bound = bound or _bound(order)
        payment_review = payment_review or order.get("payment_review_required") is True
        cancellation_pending = cancellation_pending or order.get("cancel_required") is True
        number = order.get("machine_number")
        title = label(f"Machine {number}", f"主机 {number}", inline=True) if _machine(number) else (
            label("Monthly slot", "月付槽位", inline=True))
        try:
            if order.get("currency", "USD") != "USD":
                raise wallet.WalletError("only USD is displayed")
            price = escape(wallet.display(wallet.positive_cents(order.get("amount_cents"))))
        except wallet.WalletError:
            price = label("Amount unavailable", "金额暂不可用", inline=True)
        en, zh, tone = _status(order)
        end = _date(order.get("period_end_at"))
        next_bill = _date(order.get("next_bill_at"))
        cancel = order.get("cancel_at_period_end") in (True, 1)
        dates = '<dt>' + label("Paid through", "已付费至", inline=True) + '</dt><dd>' + (
            escape(end) if end else label("Not available yet", "暂未确定", inline=True)) + '</dd>'
        if (not cancel and order.get("status") != "closed"
                and order.get("provider_status") != "canceled"):
            dates += '<dt>' + label("Next bill", "下次账单", inline=True) + '</dt><dd>' + (
                escape(next_bill) if next_bill else label("See Stripe", "请在 Stripe 查看",
                                                          inline=True)) + '</dd>'
        note = ('<p class="subscription-cancel-note">'
                + label("No new monthly cycle after Stripe’s current billing period. "
                        "Access is limited to paid time. "
                        "Existing unpaid invoices may still be retried.",
                        "Stripe 当前账期结束后不再开新账期；只能使用已付费的时间。"
                        "已有未付款账单仍可能重试扣款。", inline=True)
                + '</p>' if cancel and order.get("status") != "closed" else '')
        pending = order.get("cancel_required") is True
        extra_status = ('<span class="subscription-status">'
            + label("Cancellation pending", "正在确认取消", inline=True) + '</span>'
            if pending and order.get("payment_review_required") is True else '')
        if pending:
            note += '<p class="subscription-cancel-note">' + label(
                "Cancellation is being checked. Future renewals are not confirmed as stopped yet.",
                "正在核对取消结果，目前尚未确认已停止今后的续费。", inline=True) + '</p>'
        cards.append('<article class="subscription-card"><div class="subscription-card-head">'
            + '<h3>' + title + '</h3><span class="subscription-status ' + tone + '">'
            + label(en, zh, inline=True) + '</span>' + extra_status
            + '</div><div class="subscription-card-price">'
            + price + ' <small>' + label("USD / month", "美元／月", inline=True)
            + '</small></div><dl class="subscription-dates">' + dates + '</dl>' + note
            + (_cancel_form(order, csrf) + _checkout_controls(order, csrf)
               if management_ready else '') + '</article>')
    if not cards:
        body = '<p class="subscription-muted">' + label(
            "No monthly slots yet. A slot becomes monthly only when you choose it "
            "and finish payment.",
            "还没有月付槽位。只有你主动选择并完成付款，才会开通月付。", inline=True) + '</p>'
    else:
        body = ('<p class="subscription-muted">'
            + label("Each slot has its own monthly setup and payment status.",
                    "每个槽位分别显示月付设置与付款状态。", inline=True)
            + '</p><div class="subscription-grid">' + ''.join(cards) + '</div>')
        if not management_ready:
            body += '<p class="subscription-muted">' + label(
                "This is your saved payment history. Online management is unavailable here.",
                "这里显示已保存的付款记录，当前暂时无法在线管理。", inline=True) + '</p>'
        elif bound and not payment_review and not cancellation_pending:
            body += ('<div class="subscription-manage"><form method="post" '
                + 'action="/account/subscriptions/portal">' + _csrf(csrf)
                + '<button class="btn" type="submit">'
                + label("Manage or cancel on Stripe", "在 Stripe 管理或取消", inline=True)
                + '</button></form><p>'
                + label("Cancellation takes effect at the end of the paid period.",
                        "取消后，本期已付费服务仍可使用到期。", inline=True) + '</p></div>')
        elif payment_review:
            body += '<p class="subscription-muted">' + label(
                "Stripe account management is paused during payment review. "
                "You can still request cancellation for individual slots above.",
                "付款核查期间暂停 Stripe 账户管理入口，"
                "仍可使用上方按钮申请取消各个槽位的付款或续费。",
                inline=True) + '</p>'
        elif cancellation_pending:
            body += '<p class="subscription-muted">' + label(
                "Stripe account management is paused while cancellation is being checked. "
                "Use the slot's cancellation button to check the same request again.",
                "核对取消结果期间暂停 Stripe 账户管理入口，"
                "可用该槽位的取消按钮再次核对同一次请求。",
                inline=True) + '</p>'
        else:
            body += '<p class="subscription-muted">' + label(
                "The Stripe subscription portal becomes available "
                "after a subscription is confirmed. "
                "An unfinished checkout does not start monthly billing.",
                "订阅确认后才会显示 Stripe 订阅管理入口。尚未完成的付款不会开通月付。",
                inline=True) + '</p>'
    return ('<section class="subscription-ui subscription-summary"><h2>'
            + label("Monthly slots", "月付槽位", inline=True) + '</h2>' + body + '</section>')
