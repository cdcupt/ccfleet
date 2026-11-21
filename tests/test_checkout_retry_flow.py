"""A failed checkout can be retried without minting another order or credit."""
from __future__ import annotations

import pytest

from ccfleetd import stripe_billing, stripe_checkout
from tests.test_account_setup_flow import Flow
from tests.test_stripe_billing import Client
from tests.test_stripe_routes import FORM, FakeBilling, start
from tests.test_wallet_http import financial
from tests.test_wallet_http import web as web
from tests.test_wallet_payment_http import active, topup_form


class HostedClient(Client):
    def create_session(self, order):
        session = super().create_session(order)
        return stripe_checkout.HostedSession(
            session.id, "https://checkout.stripe.com/g/pay/" + session.id + "#synthetic")

    def retrieve_open_session(self, session_id, order):
        session = super().retrieve_open_session(session_id, order)
        return stripe_checkout.HostedSession(
            session.id, "https://checkout.stripe.com/g/pay/" + session.id + "#synthetic")


@pytest.mark.parametrize("language", ["en", "zh"])
def test_error_retry_posts_original_request_and_reuses_one_checkout(web, language):
    c = web
    client = HostedClient()
    client.fail_create = True
    billing = stripe_billing.Billing(c.store, client, sandbox_database=True)
    active(c, billing)
    user = c.browser()
    original = topup_form(c, user)
    original["amount_cents"] = "1000"
    before = financial(c)
    path = ("/zh" if language == "zh" else "") + "/account/topup"
    failed = user.call("POST", path, form=original, headers=c.public)
    assert failed.status == 503 and failed.getheader("Cache-Control") == "no-store"
    page = Flow(failed.body)
    [form] = page.find("form", action=path)
    assert form.attrs["method"] == "post"
    fields = {item.attrs["name"]: item.attrs["value"] for item in page.inside(form, "input")}
    assert fields == original
    assert ("Retry the same top-up" if language == "en" else "重试同一次充值") in form.text
    assert "SENSITIVE_PROVIDER_REPLY" not in failed.body
    [order] = c.store._conn.execute("SELECT id FROM wallet_topups").fetchall()
    assert c.store._conn.execute("SELECT COUNT(*) FROM wallet_topup_sessions").fetchone()[0] == 0
    assert financial(c) == before

    # The recovered page retains both the nonce and the normal session/CSRF boundary.
    assert user.call("POST", path, form={**fields, "csrf": "bad"},
                     headers=c.public).status == 403
    assert len(client.calls) == 1
    client.fail_create = False
    resumed = user.call("POST", form.attrs["action"], form=fields, headers=c.public)
    assert resumed.status == 303
    expected = "https://checkout.stripe.com/g/pay/cs_test_" + order[0] + "#synthetic"
    assert resumed.getheader("Location") == expected
    assert client.calls[:2] == [("create", order[0]), ("create", order[0])]
    assert c.store._conn.execute("SELECT COUNT(*) FROM wallet_topups").fetchone()[0] == 1
    assert c.store._conn.execute("SELECT COUNT(*) FROM wallet_topup_sessions").fetchone()[0] == 1
    assert c.store._conn.execute("SELECT COUNT(*) FROM wallet_topup_receipts").fetchone()[0] == 0
    assert financial(c) == before

    repeated = user.call("POST", path, form=fields, headers=c.public)
    assert repeated.status == 303 and repeated.getheader("Location") == expected
    assert client.calls[-1] == ("open", "cs_test_" + order[0])
    assert len([call for call in client.calls if call[0] == "create"]) == 2
    assert financial(c) == before


def test_retry_form_escapes_fields_without_disclosing_provider_errors():
    csrf = 'verified-but-quoted\"><img src=x onerror="private()">'
    response = start(FakeBilling(failure=RuntimeError("PRIVATE_PROVIDER_KEY")),
                     form={**FORM, "csrf": csrf})
    page = Flow(response.body)
    [form] = page.find("form", action="/account/topup")
    fields = {item.attrs["name"]: item.attrs["value"] for item in page.inside(form, "input")}
    assert fields == {**FORM, "csrf": csrf}
    assert not page.find("img") and "PRIVATE_PROVIDER_KEY" not in response.body
    assert not any("onerror" in item.attrs for item in page.elements)


def test_invalid_billing_request_has_no_retry_form():
    response = start(FakeBilling(failure=stripe_billing.BillingError("private", status_code=400)))
    assert response.status == 400
    assert not Flow(response.body).find("form", action="/account/topup")
