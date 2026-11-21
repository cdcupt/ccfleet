"""Stripe-hosted path variants retain the same exact-session redirect boundary."""
from __future__ import annotations

import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from ccfleetd import stripe_checkout, subscription_routes
from tests.test_stripe_checkout import account, client, order, session, settings
from tests.test_stripe_routes import FakeBilling, config, start
from tests.test_stripe_subscriptions import flow as flow

PREFIXES = ("/c/pay/", "/g/pay/", "/pay/")


@pytest.fixture(autouse=True)
def forbid_actual_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("hosted URL regression tests only use synthetic responses")
    monkeypatch.setattr(urllib.request, "build_opener", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("mode", ["test", "live"])
def test_topup_create_preserves_exact_hosted_variant_without_changing_payment_request(prefix, mode):
    cfg = settings(mode=mode, api_key="rk_" + mode + "_SYNTHETIC_NOT_A_REAL_KEY")
    reply = session(config=cfg)
    url = "https://checkout.stripe.com" + prefix + reply["id"] + "?locale=en#synthetic"
    reply["url"] = url
    adapter, transport = client(account(), reply, config=cfg)
    hosted = adapter.create_session(order())
    assert hosted.id == reply["id"] and hosted.url == url
    assert [request.get_method() for request in transport.requests] == ["GET", "POST"]
    fields = urllib.parse.parse_qs(transport.requests[-1].data.decode())
    assert fields["mode"] == ["payment"] and fields["line_items[0][price_data][unit_amount]"] == ["2000"]
    assert fields["automatic_tax[enabled]"] == ["false"]
    assert fields["allowed_payment_method_types[0]"] == ["card"]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_bound_topup_retrieval_accepts_hosted_variant_without_recreating_session(prefix):
    reply = session()
    reply["url"] = "https://checkout.stripe.com" + prefix + reply["id"] + "#synthetic"
    adapter, transport = client(account(), reply)
    hosted = adapter.retrieve_open_session(reply["id"], order())
    assert hosted.id == reply["id"] and hosted.url == reply["url"]
    assert [request.get_method() for request in transport.requests] == ["GET", "GET"]
    assert transport.requests[-1].full_url.endswith("/v1/checkout/sessions/" + reply["id"])


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("mode", ["test", "live"])
def test_topup_http_boundary_accepts_exact_mode_and_hosted_session(prefix, mode):
    identity = "cs_" + mode + "_SyntheticHostedBoundary"
    url = "https://checkout.stripe.com" + prefix + identity + "#synthetic"
    billing = FakeBilling(result=stripe_checkout.HostedSession(identity, url))
    result = start(billing, cfg=config(stripe_mode=mode))
    assert result.status == 303 and result.location == url
    assert len(billing.calls) == 1


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("operation", ["create", "retrieve"])
def test_subscriptions_share_exact_hosted_path_validation(flow, prefix, operation):
    adapter, local, responses, calls = flow
    reply = responses["/v1/checkout/sessions/cs_test_Own"]
    url = "https://checkout.stripe.com" + prefix + reply["id"] + "#synthetic"
    reply["url"] = url
    hosted = (adapter.create_subscription_session(local, "cus_Own") if operation == "create"
              else adapter.retrieve_open_session(reply["id"], local, "cus_Own"))
    assert hosted.id == reply["id"] and hosted.url == url
    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == (1 if operation == "create" else 0)
    assert all(call[1] == "/v1/checkout/sessions" for call in posts)


def subscription_resume(identity, url):
    cfg = SimpleNamespace(broker_only=False, stripe_mode="test", heartbeat_max_age_s=60)
    billing = SimpleNamespace(resume_checkout=lambda *args, **kwargs:
                              stripe_checkout.HostedSession(identity, url))
    return subscription_routes.checkout_action(billing, cfg, {"id": "synthetic-owner"},
        {"csrf": "caller-verified", "order_id": "synthetic-order",
         "idempotency_key": "synthetic-request"}, now=1_800_000_000, resume=True)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_subscription_resume_http_boundary_accepts_hosted_variant(prefix):
    identity = "cs_test_SyntheticResume"
    url = "https://checkout.stripe.com" + prefix + identity + "#synthetic"
    result = subscription_resume(identity, url)
    assert result.status == 303 and result.location == url


IDENTITY = "cs_test_SyntheticHostedBoundary"
URL = "https://checkout.stripe.com/g/pay/" + IDENTITY
ATTACKS = (
    URL.replace("https:", "http:"),
    URL.replace("checkout.stripe.com", "checkout.stripe.com.evil.invalid"),
    URL.replace("checkout.stripe.com", "checkout.stripe.com@evil.invalid"),
    URL.replace("checkout.stripe.com", "user@checkout.stripe.com"),
    URL.replace("checkout.stripe.com", "user:password@checkout.stripe.com"),
    URL.replace("checkout.stripe.com", "checkout.stripe.com:443"),
    URL.replace("checkout.stripe.com", "checkout.stripe.com:invalid"),
    URL.replace(IDENTITY, "cs_test_DifferentSession"),
    URL.replace("/g/pay/", "/g/pay%2F"),
    URL.replace("/g/pay/", "/%67/pay/"),
    URL.replace("/g/pay/", "//g/pay/"),
    URL.replace("/g/pay/", "/g/pay/../pay/"),
    URL.replace(IDENTITY, IDENTITY.replace("_", "%5F", 1)),
    URL + "/extra",
    URL + "%2Fextra",
    URL + "/",
    URL.replace(".com/g", ".com\\g"),
    URL + "#back\\slash",
    URL + "\r\nLocation: https://evil.invalid",
    URL + "\x00",
    URL + "#bad fragment",
    URL + "#非ASCII",
)


@pytest.mark.parametrize("url", ATTACKS)
def test_g_pay_rejects_host_auth_port_encoding_and_session_attacks_at_both_boundaries(url):
    with pytest.raises(stripe_checkout.StripeCheckoutError):
        stripe_checkout._hosted_url(url, IDENTITY)
    result = start(FakeBilling(result=stripe_checkout.HostedSession(IDENTITY, url)))
    assert result.status == 503 and not result.location
    result = subscription_resume(IDENTITY, url)
    assert result.status == 503 and not result.location


@pytest.mark.parametrize("identity", ["cs_live_Synthetic", "cs_test_bad/path", "cs_test_", ""])
def test_g_pay_does_not_relax_session_identity_or_mode(identity):
    url = "https://checkout.stripe.com/g/pay/" + identity
    result = start(FakeBilling(result=stripe_checkout.HostedSession(identity, url)))
    assert result.status == 503 and not result.location
    result = subscription_resume(identity, url)
    assert result.status == 503 and not result.location
