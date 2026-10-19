"""Authenticated recurring-rental boundaries, separate from existing credit checkout."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import (
    offer_ui,
    stripe_checkout,
    stripe_subscriptions,
    subscription_billing,
    subscription_ui,
    wallet,
    wallet_routes,
)
from .bilingual import label


def managing(billing: Any, cfg: Any) -> bool:
    return billing is not None and getattr(cfg, "broker_only", False) is False


def enrolling(billing: Any, cfg: Any) -> bool:
    return managing(billing, cfg) and getattr(cfg, "stripe_subscriptions_enabled", False) is True


def failure(status: int = 503, code: str = "unconfirmed"):
    from . import usersite

    if code == "pending-checkout":
        message = label(
            "You already have a monthly checkout in progress. Continue or cancel it "
            "in Monthly payments before choosing another slot.",
            "你已有一笔进行中的月租结账。请先在「月租付款」中继续或取消，再选择其他槽位。",
            inline=True,
        )
        target, title = "/account/subscriptions", label("Monthly payments", "月租付款", inline=True)
    elif code == "processing-required":
        message = label(
            "This checkout still needs payment confirmation. Check Monthly payments. "
            "If it stays pending, ask Fleet to check this checkout; do not start a replacement.",
            "这笔结账仍需确认付款状态，请查看「月租付款」。"
            "如果持续待处理，请让 Fleet 核查这笔结账，不要另开一笔。",
            inline=True,
        )
        target, title = "/account/subscriptions", label("Monthly payments", "月租付款", inline=True)
    elif code == "quote-changed":
        message = label(
            "The price changed. Review the current monthly price before you continue.",
            "价格已更新，请先确认当前月租价格，再继续。",
            inline=True,
        )
        target, title = "/account/available", label("Choose a slot", "选择槽位", inline=True)
    else:
        message = label(
            "This monthly rental could not be confirmed. Return to your slots "
            "and retry the same request. No existing slot was enrolled automatically.",
            "暂时无法确认月租，请返回我的槽位，重试同一次请求。已有槽位不会自动开通月租。",
            inline=True,
        )
        target, title = "/account", label("Your slots", "我的槽位", inline=True)
    body = (
        '<section class="card"><h1>'
        + label("Review monthly rental", "确认月租", inline=True)
        + "</h1><p>"
        + message
        + '</p><a href="'
        + target
        + '">'
        + title
        + "</a></section>"
    )
    return usersite.Outcome(status, body=usersite._shell("Monthly rentals", body, door=True))


def page(
    billing, store, cfg, account, session_id: str, now: float, path: str, query: Mapping[str, str]
):
    from . import usersite

    viewer = usersite.viewer_for(account, session_id, cfg, store)
    if getattr(cfg, "broker_only", False) is True or (
        path == "/account/subscribe" and not enrolling(billing, cfg)
    ):
        return usersite.not_found(viewer)
    if account is None:
        return usersite.Outcome(200, body=usersite._signed_out(cfg))
    csrf = usersite.csrf_for(session_id, cfg.cookie_secret)
    try:
        if path == "/account/subscriptions":
            rows = store.list_slot_subscriptions(account["id"])
            for row in rows:
                # The immutable order snapshot labels the same machine as Stripe.
                if (
                    row.get("subscription_id")
                    and row.get("claim_id")
                    and row.get("status") != "closed"
                ):
                    row["cancel_idempotency_key"] = wallet_routes.nonce()
                if row.get("status") != "closed" and not row.get("claim_id"):
                    row["checkout_idempotency_key"] = wallet_routes.nonce()
            body = subscription_ui.summary(rows, csrf=csrf, management_ready=managing(billing, cfg))
        else:
            if set(query) != {"machine_number"}:
                return failure(400)
            node = wallet_routes.machine(store, query.get("machine_number", ""))
            available = offer_ui.available(store, account, now, cfg.heartbeat_max_age_s)
            if node is None or not any(row["id"] == node["id"] for row in available):
                return usersite.not_found(viewer)
            body = subscription_ui.review(
                node["public_status_number"],
                wallet_routes.quote(node),
                csrf=csrf,
                idempotency_key=wallet_routes.nonce(),
            )
        return usersite.Outcome(
            200,
            body=usersite._shell(
                "Monthly rentals", body, extra_css=subscription_ui.CSS, viewer=viewer
            ),
        )
    except Exception:
        return failure()


def start(billing, store, cfg, account, form: Mapping[str, str], now: float):
    from . import usersite

    if not enrolling(billing, cfg) or not isinstance(account, Mapping):
        return failure(503)
    try:
        if (
            set(form)
            != {"csrf", "machine_number", "expected_cents", "monthly_consent", "idempotency_key"}
            or form.get("monthly_consent") != "yes"
            or not all(isinstance(value, str) for value in form.values())
        ):
            return failure(400)
        node = wallet_routes.machine(store, form["machine_number"])
        if node is None:
            return failure(404)
        amount = wallet_routes.expected_cents(form["expected_cents"])
        session = billing.start_checkout(
            account["id"],
            node["id"],
            expected_cents=amount,
            monthly_consent=True,
            idempotency_key=wallet.request_id(form["idempotency_key"]),
            now=wallet.instant(now),
            max_age_s=cfg.heartbeat_max_age_s,
        )
        if not isinstance(session, stripe_checkout.HostedSession):
            return failure()
        identity = stripe_checkout._provider_id(session.id, "cs_" + cfg.stripe_mode + "_")
        location = stripe_checkout._hosted_url(session.url, identity)
        return usersite.Outcome(303, location=location)
    except subscription_billing.SubscriptionError as exc:
        return failure(exc.status_code, exc.code)
    except Exception:
        return failure()


def portal(billing, cfg, account, form: Mapping[str, str]):
    from . import usersite

    if not managing(billing, cfg) or not isinstance(account, Mapping) or set(form) != {"csrf"}:
        return failure(400)
    try:
        location = billing.portal(wallet.identity(account["id"]))
        # Check even injected billing results; opaque portal secrets never render in HTML.
        location = stripe_subscriptions.portal_url(location)
        return usersite.Outcome(303, location=location)
    except Exception:
        return failure()


def webhook(billing, cfg, raw_body: bytes, signature: str, *, now: float):
    if not managing(billing, cfg):
        return 503, {"error": "subscription processing is unavailable"}
    if (
        not isinstance(raw_body, bytes)
        or not 0 < len(raw_body) <= stripe_checkout.MAX_BODY
        or not isinstance(signature, str)
        or not signature.isascii()
        or not 0 < len(signature) <= 4096
        or re.search(r"[\x00-\x1f\x7f]", signature)
    ):
        return 400, {"error": "invalid subscription notification"}
    try:
        result = billing.handle_webhook(raw_body, signature, now=wallet.instant(now))
        if result not in {"paid", "held", "updated", "closed", "ignored"}:
            return 503, {"error": "subscription notification could not be confirmed"}
    except subscription_billing.SubscriptionError as exc:
        return (400 if exc.status_code == 400 else 503), {
            "error": "subscription notification could not be confirmed"
        }
    except Exception:
        return 503, {"error": "subscription notification could not be confirmed"}
    return 200, {"received": True}


def cancel(billing, cfg, account, form: Mapping[str, str], *, now: float):
    from . import usersite

    if not managing(billing, cfg) or not isinstance(account, Mapping):
        return failure(503)
    if set(form) != {"csrf", "order_id", "idempotency_key"}:
        return failure(400)
    try:
        result = billing.cancel(
            wallet.identity(account["id"]),
            wallet.identity(form["order_id"]),
            idempotency_key=wallet.request_id(form["idempotency_key"]),
            now=wallet.instant(now),
        )
        if type(result) is not bool:
            return failure()
        return usersite.Outcome(
            303,
            location="/account/subscriptions?note="
            + ("cancellation-confirmed" if result else "cancellation-pending"),
        )
    except subscription_billing.SubscriptionError as exc:
        return failure(exc.status_code, exc.code)
    except Exception:
        return failure()


def checkout_action(billing, cfg, account, form: Mapping[str, str], *, now: float, resume: bool):
    from . import usersite

    if not managing(billing, cfg) or not isinstance(account, Mapping):
        return failure(503)
    if set(form) != {"csrf", "order_id", "idempotency_key"}:
        return failure(400)
    try:
        account_id, order_id = wallet.identity(account["id"]), wallet.identity(form["order_id"])
        identity = wallet.request_id(form["idempotency_key"])
        moment = wallet.instant(now)
        if resume:
            result = billing.resume_checkout(
                account_id, order_id, now=moment, max_age_s=cfg.heartbeat_max_age_s
            )
            if result is not None:
                if not isinstance(result, stripe_checkout.HostedSession):
                    return failure()
                identifier = stripe_checkout._provider_id(result.id, "cs_" + cfg.stripe_mode + "_")
                return usersite.Outcome(
                    303, location=stripe_checkout._hosted_url(result.url, identifier)
                )
        else:
            result = billing.cancel_checkout(
                account_id, order_id, idempotency_key=identity, now=moment
            )
            if result not in {"closed", "paid-cancelled", "pending"}:
                return failure()
        return usersite.Outcome(303, location="/account/subscriptions")
    except subscription_billing.SubscriptionError as exc:
        return failure(exc.status_code, exc.code)
    except Exception:
        return failure()
