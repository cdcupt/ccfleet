"""Actual local HTTP boundaries with synthetic recurring billing only."""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import replace

import pytest

from ccfleetd import stripe_checkout, subscription_billing, usersite
from tests.test_account_setup_flow import Flow
from tests.test_api import call
from tests.test_wallet_http import financial
from tests.test_wallet_http import web as web  # noqa: F401 -- real local HTTP fixture


class Billing:
    def __init__(self):
        self.calls = []

    def start_checkout(self, account_id, node_id, **kwargs):
        self.calls.append(("start", account_id, node_id, kwargs))
        return stripe_checkout.HostedSession(
            "cs_test_Synthetic", "https://checkout.stripe.com/c/pay/cs_test_Synthetic"
        )

    def portal(self, account_id):
        self.calls.append(("portal", account_id))
        return "https://billing.stripe.com/p/session/test_synthetic"

    def cancel(self, account_id, order_id, **kwargs):
        self.calls.append(("cancel", account_id, order_id, kwargs))
        return True

    def resume_checkout(self, account_id, order_id, **kwargs):
        self.calls.append(("resume", account_id, order_id, kwargs))
        return stripe_checkout.HostedSession(
            "cs_test_Synthetic", "https://checkout.stripe.com/c/pay/cs_test_Synthetic"
        )

    def cancel_checkout(self, account_id, order_id, **kwargs):
        self.calls.append(("cancel-checkout", account_id, order_id, kwargs))
        return "closed"

    def handle_webhook(self, body, signature, *, now):
        self.calls.append(("event", body, signature))
        return "updated"


def active(c):
    c.ctx.cfg = replace(c.cfg, stripe_subscriptions_enabled=True)
    c.ctx.recurring = Billing()
    return c.ctx.recurring


@pytest.mark.parametrize("language", ["en", "zh"])
def test_review_is_read_only_localized_and_preserves_public_machine_selection(web, language):
    c = web
    billing = active(c)
    user = c.browser()
    before = financial(c)
    result = user.call(
        "GET", "/" + language + "/account/subscribe?machine_number=1", headers=c.public
    )
    assert result.status == 200 and not billing.calls and financial(c) == before
    parsed = Flow(result.body)
    prefix = "/zh" if language == "zh" else ""
    [form] = parsed.find("form", action=prefix + "/account/subscribe")
    fields = parsed.inside(form, "input")
    [consent] = [field for field in fields if field.attrs.get("name") == "monthly_consent"]
    assert "checked" not in consent.attrs and "required" in consent.attrs
    for choice in ("en", "zh"):
        [link] = parsed.find("a", data_language_link=choice)
        assert link.attrs["href"] == "/" + choice + "/account/subscribe?machine_number=1"
    assert "PRIVATE_QUERY" not in result.body


@pytest.mark.parametrize(
    "change,status",
    [
        ({"monthly_consent": ""}, 400),
        ({"account_id": "bob"}, 400),
        ({"csrf": "bad"}, 403),
        ({"monthly_consent": "true"}, 400),
    ],
)
def test_monthly_post_requires_current_user_csrf_and_explicit_consent(web, change, status):
    c = web
    billing = active(c)
    user = c.browser()
    before = financial(c)
    fields = {
        "csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret),
        "machine_number": "1",
        "expected_cents": "1000",
        "idempotency_key": "synthetic-monthly-request",
        "monthly_consent": "yes",
    }
    result = user.call("POST", "/account/subscribe", form={**fields, **change}, headers=c.public)
    assert result.status == status and not billing.calls and financial(c) == before


def test_monthly_post_sends_only_authenticated_identity_and_fixed_quote(web):
    c = web
    billing = active(c)
    user = c.browser()
    fields = {
        "csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret),
        "machine_number": "1",
        "expected_cents": "1000",
        "idempotency_key": "synthetic-monthly-request",
        "monthly_consent": "yes",
    }
    result = user.call("POST", "/zh/account/subscribe", form=fields, headers=c.public)
    assert result.status == 303 and result.getheader("Location").startswith(
        "https://checkout.stripe.com/"
    )
    method, account, node, args = billing.calls[0]
    assert (method, account, node) == ("start", "alice", "node-a")
    assert args["expected_cents"] == 1000 and args["monthly_consent"] is True
    assert c.store.wallet_balance("alice") == 0 and c.store.list_slot_subscriptions("alice") == []


def test_portal_management_keeps_working_when_enrollment_is_off(web):
    c = web
    billing = active(c)
    user = c.browser()
    c.ctx.cfg = replace(c.ctx.cfg, stripe_subscriptions_enabled=False)
    result = user.call(
        "POST",
        "/account/subscriptions/portal",
        form={"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret)},
        headers=c.public,
    )
    assert result.status == 303 and billing.calls == [("portal", "alice")]
    before = len(billing.calls)
    result = user.call(
        "POST",
        "/account/subscribe",
        form={"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret)},
        headers=c.public,
    )
    assert result.status == 503 and len(billing.calls) == before


def test_subscription_webhook_has_separate_host_broker_and_framing_boundary(web):
    c = web
    billing = active(c)
    path = "/api/payments/stripe/subscriptions/webhook"
    assert (
        call(
            c.server,
            "POST",
            path,
            b"{}",
            {"Host": "admin.example.com", "Stripe-Signature": "synthetic"},
        )[0]
        == 404
    )
    assert call(c.server, "POST", path, b"{}", c.public)[0] == 400
    assert not billing.calls
    assert (
        call(
            c.server,
            "POST",
            path,
            b'{"own":"synthetic"}',
            {**c.public, "Stripe-Signature": "synthetic"},
        )[0]
        == 200
    )
    assert billing.calls[0] == ("event", b'{"own":"synthetic"}', "synthetic")
    c.ctx.cfg = replace(c.ctx.cfg, broker_only=True)
    assert (
        call(c.server, "POST", path, b"{}", {**c.public, "Stripe-Signature": "synthetic"})[0] == 404
    )


def test_existing_credit_webhook_does_not_dispatch_monthly_billing(web):
    c = web
    billing = active(c)
    result = call(
        c.server,
        "POST",
        "/api/payments/stripe/webhook",
        b"{}",
        {**c.public, "Stripe-Signature": "synthetic"},
    )
    assert result[0] == 503 and not billing.calls


ACTIONS = ("cancel", "resume", "cancel-checkout")


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("bad", ["duplicate", "utf8", "percent-utf8", "identity", "csrf", "host"])
def test_contract_management_requires_strict_own_session_form(web, action, bad):
    c = web
    billing = active(c)
    user = c.browser()
    fields = {
        "csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret),
        "order_id": "local-order",
        "idempotency_key": "synthetic-operation",
    }
    path = "/account/subscriptions/" + action
    before = financial(c)
    if bad in {"duplicate", "utf8", "percent-utf8"}:
        raw = urllib.parse.urlencode(fields).encode()
        raw += {
            "duplicate": b"&order_id=foreign-order",
            "utf8": b"&bad=\xff",
            "percent-utf8": b"&bad=%FF",
        }[bad]
        headers = {
            **c.public,
            "Cookie": "; ".join(k + "=" + v for k, v in user.jar.items()),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        assert call(c.server, "POST", path, raw, headers)[0] == 400
    else:
        if bad == "identity":
            fields["account_id"] = "foreign-user"
        if bad == "csrf":
            fields["csrf"] = "bad"
        response = user.call(
            "POST", path, form=fields, headers=c.admin if bad == "host" else c.public
        )
        assert response.status == {"identity": 400, "csrf": 403, "host": 404}[bad]
    assert not billing.calls and financial(c) == before


@pytest.mark.parametrize("action", ACTIONS)
def test_owned_contract_actions_keep_working_with_enrollment_off(web, action):
    c = web
    billing = active(c)
    user = c.browser()
    c.ctx.cfg = replace(c.ctx.cfg, stripe_subscriptions_enabled=False)
    response = user.call(
        "POST",
        "/zh/account/subscriptions/" + action,
        form={
            "csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret),
            "order_id": "own-order",
            "idempotency_key": "synthetic-operation",
        },
        headers=c.public,
    )
    assert response.status == 303
    assert billing.calls[0][:3] == (action, "alice", "own-order")
    assert "account_id" not in billing.calls[0][3]


def test_pending_checkout_refusal_links_to_owner_resume_and_cancel_controls(web):
    c = web
    billing = active(c)
    user = c.browser()

    def pending(*args, **kwargs):
        raise subscription_billing.SubscriptionError(status_code=409, code="pending-checkout")

    billing.start_checkout = pending
    response = user.call(
        "POST",
        "/account/subscribe",
        headers=c.public,
        form={
            "csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret),
            "machine_number": "1",
            "expected_cents": "1000",
            "idempotency_key": "synthetic-second",
            "monthly_consent": "yes",
        },
    )
    assert response.status == 409
    assert 'href="/account/subscriptions"' in response.body
    assert "Continue or cancel" in response.body and "SENSITIVE" not in response.body


@pytest.mark.parametrize(
    "url,status",
    [
        ("https://billing.stripe.com/p/session?secret=test_SYNTHETIC_OPAQUE", 303),
        ("https://billing.stripe.com/p/session?secret=test_SYNTHETIC_OPAQUE&next=evil", 503),
    ],
)
def test_http_portal_redirect_accepts_only_verified_stripe_opaque_secret_query(web, url, status):
    c = web
    billing = active(c)
    billing.portal = lambda account_id: url
    user = c.browser()
    response = user.call(
        "POST",
        "/account/subscriptions/portal",
        headers=c.public,
        form={"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret)},
    )
    assert response.status == status
    assert "SYNTHETIC_OPAQUE" not in response.body
    if status == 303:
        assert response.getheader("Location") == url


def test_closed_only_monthly_history_remains_readable_when_provider_processing_is_off(web):
    c = web
    now = time.time()
    order = c.store.reserve_slot_subscription(
        "alice",
        "node-a",
        expected_cents=1000,
        monthly_consent=True,
        idempotency_key="closed-history",
        merchant_id="acct_Synthetic",
        livemode=False,
        now=now,
        max_age_s=90,
    )
    c.store.attach_slot_subscription_session(
        order["id"], session_id="cs_test_Closed", expires_at=now + 3600, now=now
    )
    c.store.close_slot_subscription(
        order["id"],
        now=now,
        proof={
            "order_id": order["id"],
            "session_id": "cs_test_Closed",
            "session_status": "expired",
            "payment_status": "unpaid",
            "subscription_id": None,
            "merchant_id": "acct_Synthetic",
            "livemode": False,
        },
    )
    assert c.ctx.recurring is None and not c.store.slot_subscription_processing_required()
    before = c.store.list_slot_subscriptions("alice")
    response = c.browser().call("GET", "/account/subscriptions", headers=c.public)
    assert response.status == 200
    assert (
        len(
            [
                item
                for item in Flow(response.body).find("article")
                if item.has_class("subscription-card")
            ]
        )
        == 1
    )
    assert not [
        item
        for item in Flow(response.body).find("form")
        if "/account/subscriptions" in item.attrs.get("action", "")
    ]
    assert c.store.list_slot_subscriptions("alice") == before
    foreign = c.browser("bob").call("GET", "/account/subscriptions", headers=c.public)
    assert foreign.status == 200
    assert not [
        item for item in Flow(foreign.body).find("article") if item.has_class("subscription-card")
    ]
