"""Own-account credit confirmations and POST actions; no payment-provider calls."""
from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from typing import Any

from . import offer_ui, offers, slots, status, wallet, wallet_ui
from .bilingual import label
from .store import (
    InsufficientWalletCredit,
    NoSlotAvailable,
    NotYours,
    StoreError,
    WalletQuoteChanged,
)

ERRORS = {
    "quote": ("A current USD quote or credit balance is unavailable.",
              "当前美元报价或余额暂不可用。"),
    "legacy": ("This operator-managed slot does not need credit renewal.",
              "此槽位由运营方管理，无需使用余额续费。"),
    "changed": ("The price changed. Review the new quote before buying.",
              "价格已变更，请确认新报价后再购买。"),
    "credit": ("There is not enough Fleet credit for this month.", "Fleet 余额不足以支付一个月。"),
    "slot": ("This slot is no longer available. Review the current options.",
              "此槽位当前不可购买，请查看其他选项。"),
    "request": ("This request could not be completed. Review your balance and slot.",
              "本次操作未完成，请查看余额与槽位状态。"),
    "hold": ("Your credit is under review. Buying and renewing are paused.",
              "余额正在核查，暂时无法购买或续费。"),
}

def nonce() -> str:
    return "usd-" + secrets.token_hex(24)


def enabled(cfg: Any) -> bool:
    return getattr(cfg, "usd_slot_credit_enabled", False) is True


def expected_cents(value: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,10}", value) is None:
        raise StoreError("a current USD quote is required")
    try:
        return wallet.positive_cents(int(value))
    except wallet.WalletError as exc:
        raise StoreError(str(exc)) from exc


def machine(store, number: str):
    if not isinstance(number, str) or re.fullmatch(r"[1-9][0-9]{0,18}", number) is None:
        return None
    return next((node for node in store.list_nodes()
                 if status.public_machine_number(node) == int(number)), None)


def quote(node) -> int:
    current = offers.quote(node.get("offer")) if node else None
    if current is None or current.total.currency != "USD":
        raise StoreError("a current USD quote is unavailable")
    return wallet.usd_cents(current.total.amount)


def page(store, cfg, account, session_id: str, now: float, operation: str,
         query: Mapping[str, str]):
    from . import usersite
    if not enabled(cfg):
        return usersite.not_found(usersite.viewer_for(account, session_id, cfg, store))
    if account is None:
        return usersite.Outcome(200, body=usersite._signed_out(cfg))
    viewer = usersite.viewer_for(account, session_id, cfg, store)
    try:
        balance = store.wallet_balance(account["id"])
        if store.wallet_spending_blocked(account["id"]):
            body = (wallet_ui.balance_card(balance) + '<p>'
                    + label(*ERRORS["hold"], inline=True) + '</p>')
            return usersite.Outcome(200, body=usersite._shell("Review slot credit", body,
                extra_css=wallet_ui.CSS, viewer=viewer))
        common = {"balance_cents": balance,
                  "csrf": usersite.csrf_for(session_id, cfg.cookie_secret),
                  "idempotency_key": nonce()}
        if operation == "purchase":
            node = machine(store, query.get("machine_number", ""))
            eligible = offer_ui.available(store, account, now, cfg.heartbeat_max_age_s)
            if node is None or not any(item["id"] == node["id"] for item in eligible):
                return usersite.not_found(viewer)
            body = wallet_ui.purchase_confirmation(
                machine_number=status.public_machine_number(node),
                expected_cents=quote(node), **common)
        elif operation == "renew":
            slot = store.get_slot(query.get("slot_id", ""))
            if slot is None or slot.get("held_by") != account["id"]:
                return usersite.not_found(viewer)
            end = store.paid_slot_through(account["id"], slot["id"],
                                          claim_id=slot["gateway_claim_id"])
            if end is None:
                return failure(cfg, viewer, "legacy")
            if slot["state"] not in (slots.CLAIMED, slots.ACTIVE):
                return failure(cfg, viewer, "request")
            body = wallet_ui.renewal_confirmation(slot_id=slot["id"],
                slot_name=slot.get("name") or slot["id"], claim_id=slot["gateway_claim_id"],
                expected_cents=quote(store.get_node(slot["node_id"])),
                current_order={"period_end_at": end}, **common)
        else:
            return usersite.not_found(viewer)
    except Exception:
        return failure(cfg, viewer, "quote")
    return usersite.Outcome(200, body=usersite._shell("Review slot credit", body,
        extra_css=wallet_ui.CSS, viewer=viewer))


def failure(cfg, viewer, code: str, status_code: int = 400):
    from . import usersite
    body = ('<section class="card"><h1>' + label("Review your slot", "确认槽位", inline=True)
            + '</h1><p>' + label(*ERRORS.get(code, ERRORS["request"]), inline=True)
            + '</p><a href="/account">'
            + label("Back to your slots", "返回我的槽位", inline=True) + '</a></section>')
    return usersite.Outcome(status_code, body=usersite._shell("Review slot credit", body,
                                                           viewer=viewer))


def act(store, cfg, account, session_id: str, now: float, operation: str,
        form: Mapping[str, str]):
    from . import usersite
    viewer = usersite.viewer_for(account, session_id, cfg, store)
    if not enabled(cfg):
        return usersite.not_found(viewer)
    if operation not in {"purchase", "renew"}:
        return usersite.not_found(viewer)
    try:
        cents = expected_cents(form.get("expected_cents", ""))
        if operation == "purchase":
            node = machine(store, form.get("machine_number", ""))
            if node is None:
                return usersite.not_found(viewer)
            order = store.purchase_slot(account["id"], node["id"], expected_cents=cents,
                idempotency_key=form.get("idempotency_key", ""), now=now,
                max_age_s=cfg.heartbeat_max_age_s)
            note = "wallet-purchased"
        else:
            order = store.renew_paid_slot(account["id"], form.get("slot_id", ""),
                claim_id=form.get("claim_id", ""), expected_cents=cents,
                idempotency_key=form.get("idempotency_key", ""), now=now,
                max_age_s=cfg.heartbeat_max_age_s)
            note = "wallet-renewed"
        return usersite._back(note, "slot-" + order["slot_id"])
    except NotYours:
        return usersite.not_found(viewer)
    except WalletQuoteChanged:
        return failure(cfg, viewer, "changed", 409)
    except InsufficientWalletCredit:
        return failure(cfg, viewer, "credit")
    except NoSlotAvailable:
        return failure(cfg, viewer, "slot")
    except (StoreError, wallet.WalletError):
        return failure(cfg, viewer, "request")
