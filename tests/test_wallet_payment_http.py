"""Actual local HTTP payment boundary, always synthetic Billing/provider clients."""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import urllib.parse
from dataclasses import replace

import pytest

from ccfleetd import stripe_billing, stripe_checkout, stripe_config, usersite
from ccfleetd.api import Context
from tests.test_account_setup_flow import Flow
from tests.test_api import call
from tests.test_stripe_billing import Client, paid_object
from tests.test_stripe_routes import FakeBilling
from tests.test_usersite import Browser
from tests.test_wallet_http import financial
from tests.test_wallet_http import web as web  # noqa: F401 -- imported HTTP fixture


def active(c, billing=None):
    c.ctx.cfg = replace(c.cfg, stripe_topup_enabled=True, stripe_product_approved=True,
                        stripe_sandbox_database=True)
    c.ctx.billing = FakeBilling() if billing is None else billing
    return c.ctx.billing


def topup_form(c, user):
    result = user.call("GET", "/account", headers=c.public)
    assert result.status == 200
    [link] = Flow(result.body).find("a", href="/account/topup")
    assert 'Add credit' in link.text
    result = user.call("GET", link.attrs['href'], headers=c.public)
    assert result.status == 200
    parsed = Flow(result.body)
    [form] = parsed.find("form", action="/account/topup")
    return {item.attrs["name"]: item.attrs.get("value", "")
            for item in parsed.inside(form, "input")
            if item.attrs.get('type') == 'hidden' or 'checked' in item.attrs}


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_topup_get_is_authenticated_localized_read_only_and_ignores_injected_fields(web, language):
    c = web
    fake = active(c)
    c.store.grant_wallet_credit('alice', '12.34', actor_id='admin', reason='Synthetic test',
        idempotency_key='synthetic-own-balance', now=time.time())
    c.store.grant_wallet_credit('bob', '56.78', actor_id='admin', reason='Synthetic test',
        idempotency_key='synthetic-other-balance', now=time.time())
    user = c.browser()
    before = financial(c)
    target = '/' + language + '/account/topup'
    page = user.call('GET', target + '?account_id=bob&amount_cents=10000&csrf=PRIVATE_QUERY',
                     headers=c.public)
    assert page.status == 200 and page.getheader('Cache-Control') == 'no-store'
    assert 'PRIVATE_QUERY' not in page.body and '$12.34' in page.body and '$56.78' not in page.body
    parsed = Flow(page.body)
    for choice in ('en', 'zh'):
        [language_link] = parsed.find('a', data_language_link=choice)
        assert language_link.attrs['href'] == '/' + choice + '/account/topup'
    [form] = parsed.find('form', action=('/zh' if language == 'zh' else '') + '/account/topup')
    fields = parsed.inside(form, 'input')
    hidden = {item.attrs['name']: item.attrs.get('value', '') for item in fields
              if item.attrs.get('type') == 'hidden'}
    assert set(hidden) == {'csrf', 'idempotency_key'}
    assert hidden['csrf'] == usersite.csrf_for(user.session, c.cfg.cookie_secret)
    assert hidden['idempotency_key'].startswith('usd-')
    radios = [item for item in fields if item.attrs.get('type') == 'radio']
    assert [item.attrs['value'] for item in radios] == ['1000', '2000', '5000', '10000']
    assert [item.attrs['value'] for item in radios if 'checked' in item.attrs] == ['2000']
    assert ('前往安全付款页面' if language == 'zh' else 'Continue to secure payment') in page.body
    assert ('Continue to secure payment' if language == 'zh' else '前往安全付款页面') not in page.body
    assert not fake.calls and financial(c) == before
    anonymous = Browser(c.server.server_address[1]).call('GET', target, headers=c.public)
    assert anonymous.status == 200 and not Flow(anonymous.body).find('form', action='/account/topup')
    assert hidden['csrf'] not in anonymous.body and hidden['idempotency_key'] not in anonymous.body
    assert '$12.34' not in anonymous.body


@pytest.mark.parametrize('amount', ['1000', '2000', '5000', '10000'])
def test_preset_selected_on_new_page_posts_through_existing_own_account_boundary(web, amount):
    c = web
    fake = active(c)
    user = c.browser()
    fields = topup_form(c, user)
    fields['amount_cents'] = amount
    before = financial(c)
    response = user.call('POST', '/account/topup', form=fields, headers=c.public)
    assert response.status == 303
    assert fake.calls[0][:4] == ('start', 'alice', int(amount), fields['idempotency_key'])
    assert financial(c) == before


@pytest.mark.parametrize('amount', ['999', '1001', '10000.0', '-1000', '02000', '200000'])
def test_new_topup_form_still_rejects_nonpreset_or_malformed_amounts(web, amount):
    c = web
    fake = active(c)
    user = c.browser()
    fields = topup_form(c, user)
    response = user.call('POST', '/account/topup', form={**fields, 'amount_cents': amount},
                         headers=c.public)
    assert response.status == 400 and not fake.calls


@pytest.mark.parametrize('nonce', ['', 'malformed nonce <script>'])
def test_topup_new_page_nonce_is_still_validated_before_provider_dispatch(web, nonce):
    c = web
    fake = active(c)
    user = c.browser()
    fields = topup_form(c, user)
    response = user.call('POST', '/account/topup', form={**fields, 'idempotency_key': nonce},
                         headers=c.public)
    assert response.status == 400 and not fake.calls


def test_topup_get_with_a_review_hold_has_no_checkout_form_or_provider_calls(web):
    from tests.test_wallet_topups import hold, ready, settle
    c = web
    fake = active(c)
    settle(c.store, ready(c.store))
    hold(c.store)
    before = financial(c)
    response = c.browser().call('GET', '/account/topup', headers=c.public)
    assert response.status == 200 and 'Top-ups are paused' in response.body
    assert not Flow(response.body).find('form', action='/account/topup')
    assert not fake.calls and financial(c) == before


def test_Context_disabled_is_inert_and_broker_never_constructs_billing(web, monkeypatch):
    c = web
    def fail(*args, **kwargs):
        pytest.fail("disabled must not open credentials or construct Billing")
    monkeypatch.setattr(stripe_config, "build_billing", fail)
    # Disabled factory itself is separately tested. A broker must bypass it altogether.
    ctx = Context(c.store, replace(c.cfg, broker_only=True, stripe_topup_enabled=True), None)
    assert ctx.billing is None
    ctx.gateway.close()


def test_actual_topup_requires_own_session_CSRF_and_no_injected_identity(web):
    c = web
    billing = active(c)
    user = c.browser()
    fields = topup_form(c, user)
    before = financial(c)
    response = user.call("POST", "/zh/account/topup", form={**fields, "csrf": "bad"},
                         headers=c.public)
    assert response.status == 403 and not billing.calls
    response = user.call("POST", "/account/topup", form={**fields, "account_id": "bob"},
                         headers=c.public)
    assert response.status == 400 and not billing.calls
    response = user.call("POST", "/zh/account/topup", form=fields, headers=c.public)
    assert response.status == 303
    assert response.getheader("Location") == "https://checkout.stripe.com/c/pay/cs_test_SyntheticBoundary"
    assert billing.calls[0][:4] == ("start", "alice", 2000, fields["idempotency_key"])
    assert financial(c) == before


@pytest.mark.parametrize("host", ["admin.example.com", "unconfigured.example.com"])
def test_payment_route_is_strict_product_host_only(web, host):
    c = web
    billing = active(c)
    user = c.browser()
    fields = {"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret),
              "amount_cents": "2000", "idempotency_key": "host-gate"}
    assert user.call("POST", "/account/topup", form=fields, headers={"Host": host}).status == 404
    assert user.call("GET", "/account/topup", headers={"Host": host}).status == 404
    assert user.call("GET", "/zh/account/topup", headers={"Host": host}).status == 404
    assert call(c.server, "POST", "/api/payments/stripe/webhook", b"{}",
        {"Host": host, "Stripe-Signature": "t=1,v1=synthetic"})[0] == 404
    assert not billing.calls


def test_disabled_and_broker_routes_never_dispatch_payment_or_show_active_form(web):
    c = web
    fake = FakeBilling()
    c.ctx.billing = fake
    user = c.browser()
    page = user.call("GET", "/account", headers=c.public)
    assert not Flow(page.body).find("form", action="/account/topup")
    assert not Flow(page.body).find("a", href="/account/topup")
    chooser = user.call('GET', '/account/topup', headers=c.public)
    assert chooser.status == 200 and 'Card top-up is unavailable' in chooser.body
    assert not Flow(chooser.body).find('form', action='/account/topup')
    fields = {"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret),
              "amount_cents": "2000", "idempotency_key": "disabled-gate"}
    assert user.call("POST", "/account/topup", form=fields, headers=c.public).status == 503
    assert call(c.server, "POST", "/api/payments/stripe/webhook", b"{}",
                {**c.public, "Stripe-Signature": "t=1,v1=synthetic"})[0] == 503
    active(c, fake)
    c.ctx.cfg = replace(c.ctx.cfg, broker_only=True)
    assert user.call("GET", "/account/topup", headers=c.public).status == 404
    assert user.call("POST", "/account/topup", form=fields, headers=c.public).status == 404
    assert call(c.server, "POST", "/api/payments/stripe/webhook", b"{}",
                {**c.public, "Stripe-Signature": "t=1,v1=synthetic"})[0] == 404
    assert not fake.calls


@pytest.mark.parametrize("returned", ["returned", "cancelled"])
def test_forged_browser_return_never_settles_or_reads_a_submitted_session(web, returned):
    c = web
    fake = active(c)
    user = c.browser()
    before = financial(c)
    page = user.call("GET", "/zh/account?" + urllib.parse.urlencode({
        "checkout": returned, "session_id": "PRIVATE_SESSION", "account_id": "bob",
        "payment_status": "paid"}), headers=c.public)
    assert page.status == 200 and "付款可能仍在处理中" in page.body
    assert "PRIVATE_SESSION" not in page.body and not fake.calls
    assert financial(c) == before


def signed(billing, value):
    now = int(time.time())
    event = {"id": "evt_actual_http_synthetic", "object": "event", "livemode": False,
             "type": "checkout.session.completed", "data": {"object": value}}
    raw = json.dumps(event, separators=(",", ":")).encode()
    digest = hmac.new(billing.settings.webhook_secret.encode(), str(now).encode() + b"." + raw,
                      hashlib.sha256).hexdigest()
    return raw, "t=" + str(now) + ",v1=" + digest


def test_raw_signed_webhook_is_only_credit_path_and_replays_once(web):
    c = web
    real = stripe_billing.Billing(c.store, Client(), sandbox_database=True)
    active(c, real)
    user = c.browser()
    fields = topup_form(c, user)
    response = user.call("POST", "/account/topup", form=fields, headers=c.public)
    assert response.status == 303 and c.store.wallet_balance("alice") == 0
    order = next(iter(real.client.orders.values()))
    session = stripe_checkout.HostedSession("cs_test_" + order.id, response.getheader("Location"))
    raw, signature = signed(real, paid_object(real, session))
    headers = {**c.public, "Stripe-Signature": signature, "Content-Type": "application/json"}
    assert call(c.server, "POST", "/api/payments/stripe/webhook", raw + b" ", headers)[0] == 400
    assert c.store.wallet_balance("alice") == 0
    assert call(c.server, "POST", "/api/payments/stripe/webhook", raw, headers)[0] == 200
    assert c.store.wallet_balance("alice") == 2000
    assert call(c.server, "POST", "/api/payments/stripe/webhook", raw, headers)[0] == 200
    assert c.store.wallet_balance("alice") == 2000
    assert len(c.store.list_wallet_entries("alice")) == 1


def test_duplicate_webhook_signature_is_rejected_before_billing(web):
    c = web
    fake = active(c)
    sock = socket.create_connection(c.server.server_address, timeout=3)
    sock.sendall(b"POST /api/payments/stripe/webhook HTTP/1.1\r\nHost: fleet.example.com\r\n"
                 b"Content-Length: 2\r\nStripe-Signature: one\r\nStripe-Signature: two\r\n\r\n{}")
    response = sock.recv(4096)
    sock.close()
    assert b"400" in response.split(b"\r\n", 1)[0] and not fake.calls
