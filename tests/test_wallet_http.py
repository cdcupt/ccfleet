"""Actual HTTP credit boundaries; in-memory storage and synthetic identities only."""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import sessions, slots, status, wallet_access
from ccfleetd.api import Context, build_server, csrf_token
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.passwords import hash_password
from ccfleetd.store import Store, StoreError
from tests.test_account_setup_flow import Flow
from tests.test_cli_access import public_key
from tests.test_usersite import Browser
from tests.test_wallet_store import accounts, machine


@pytest.fixture
def web():
    now = time.time()
    store = Store(":memory:")
    accounts(store)
    machine(store, at=now)
    cfg = Config(public_url="https://fleet.example.com", admin_host="admin.example.com",
                 cookie_secret="synthetic-cookie-secret-0123456789", cookie_secure=False,
                 admin_token="synthetic-break-glass-token", usd_slot_credit_enabled=True)
    ctx = Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    server = build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def browser(account="alice", site="product"):
        session = store.create_session(account, now=time.time(), ttl_s=3600, site=site)
        client = Browser(server.server_address[1])
        client.jar[sessions.COOKIE_NAME] = sessions.sign(session, cfg.cookie_secret)
        client.session = session
        return client
    yield SimpleNamespace(store=store, cfg=cfg, ctx=ctx, server=server, browser=browser,
                          public={"Host": "fleet.example.com"}, admin={"Host": "admin.example.com"})
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    ctx.gateway.close()
    store.close()


def grant(c, amount="30"):
    return c.store.grant_wallet_credit("alice", amount, actor_id="admin", reason="PRIVATE GRANT NOTE",
        idempotency_key="synthetic-grant", now=time.time())


def form_from(source, action):
    parsed = Flow(source)
    [form] = parsed.find("form", action=action)
    return {item.attrs["name"]: item.attrs.get("value", "")
            for item in parsed.inside(form, "input")}


def quote(c, browser):
    number = status.public_machine_number(c.store.get_node("node-a"))
    response = browser.call("GET", "/account/purchase?machine_number=" + str(number),
                            headers=c.public)
    assert response.status == 200
    return form_from(response.body, "/account/purchase")


def financial(c):
    return (c.store.wallet_balance("alice"), c.store.list_wallet_entries("alice"),
            c.store.list_slot_purchases("alice"), c.store.get_slot("slot-node-a"))


def test_actual_purchase_requires_session_CSRF_and_exact_quote_then_replays_once(web):
    c = web
    grant(c)
    user = c.browser()
    before = financial(c)
    fields = quote(c, user)
    assert financial(c) == before  # GET never spends or claims.
    assert fields["expected_cents"] == "1000"
    assert c.store.get_account("alice")["slot_quota"] == 0
    assert Browser(c.server.server_address[1]).call("POST", "/account/purchase", form=fields,
                                                 headers=c.public).status == 303
    assert user.call("POST", "/account/purchase", form={**fields, "csrf": "bad"},
                     headers=c.public).status == 403
    assert user.call("POST", "/account/purchase", form={**fields, "expected_cents": "999"},
                     headers=c.public).status == 409
    assert financial(c) == before
    assert user.call("POST", "/account/claim", form={"csrf": fields["csrf"]},
                     headers=c.public).status == 303
    assert financial(c) == before
    assert user.call("POST", "/account/purchase", form=fields, headers=c.public).status == 303
    after = financial(c)
    assert after[0] == 2000 and after[3]["held_by"] == "alice"
    assert user.call("POST", "/account/purchase", form=fields, headers=c.public).status == 303
    assert financial(c) == after
    page = user.call("GET", "/account", headers=c.public)
    assert "PRIVATE GRANT NOTE" not in page.body and "synthetic-grant" not in page.body
    assert "Fleet credit" in page.body and "Paid month active" in page.body


def test_renewal_is_own_claim_only_and_requires_explicit_current_quote(web):
    c = web
    grant(c)
    user = c.browser()
    fields = quote(c, user)
    assert user.call("POST", "/account/purchase", form=fields, headers=c.public).status == 303
    c.store.move_slot("slot-node-a", slots.CLAIMED)
    c.store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": True}], now=time.time())
    before = financial(c)
    assert c.browser("bob").call("GET", "/account/renew?slot_id=slot-node-a",
                                 headers=c.public).status == 404
    review = user.call("GET", "/account/renew?slot_id=slot-node-a", headers=c.public)
    assert review.status == 200
    renewal = form_from(review.body, "/account/renew")
    assert financial(c) == before
    assert user.call("POST", "/account/renew", form={**renewal, "claim_id": "wrong"},
                     headers=c.public).status == 404
    assert financial(c) == before
    assert user.call("POST", "/account/renew", form=renewal, headers=c.public).status == 303
    after = financial(c)
    assert after[0] == 1000 and len(after[2]) == 2
    assert after[2][0]["period_end_at"] > before[2][0]["period_end_at"]
    assert user.call("POST", "/account/renew", form=renewal, headers=c.public).status == 303
    assert financial(c) == after


def test_financial_routes_bound_query_and_unchanged_site_boundary(web):
    c = web
    user = c.browser()
    assert user.call("GET", "/account/purchase?" + "&".join("x=1" for _ in range(17)),
                     headers=c.public).status == 400
    assert user.call("GET", "/account/purchase?machine_number=1", headers=c.admin).status == 404
    assert user.call("POST", "/account/purchase", form={}, headers=c.admin).status == 404


@pytest.mark.parametrize("kind", ["token", "forged-name", "named-admin", "google-admin", "owner"])
def test_credit_grant_actor_is_server_authenticated_database_admin(web, kind):
    c = web
    c.store.add_user("named-admin", hash_password("synthetic-password"), "admin", now=time.time())
    c.store.add_user("owner", hash_password("synthetic-password"), "owner", now=time.time())
    browser = c.browser("admin", site="admin") if kind == "google-admin" else Browser(c.server.server_address[1])
    credentials = {"token": ("any", c.cfg.admin_token), "forged-name": ("named-admin", c.cfg.admin_token),
                   "named-admin": ("named-admin", "synthetic-password"),
                   "owner": ("owner", "synthetic-password")}
    headers = dict(c.admin)
    if kind != "google-admin":
        user, password = credentials[kind]
        headers["Authorization"] = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
    form = {"csrf": csrf_token(c.cfg), "amount": "12.50", "reason": "PRIVATE CREDIT NOTE",
            "idempotency_key": "actual-http-grant", "actor_id": "admin", "actor_kind": "account"}
    denied = browser.call("POST", "/actions/account/alice/credit", form={**form, "csrf": "bad"}, headers=headers)
    assert denied.status == 403
    response = browser.call("POST", "/actions/account/alice/credit", form=form, headers=headers)
    if kind in {"token", "forged-name", "owner"}:
        assert response.status == 403 and c.store.wallet_balance("alice") == 0
        if kind != "owner":
            assert "Sign in using an admin account" in response.body
    else:
        assert response.status == 303 and c.store.wallet_balance("alice") == 1250
        [entry] = c.store.list_wallet_entries("alice")
        assert entry["actor_id"] == ("admin" if kind == "google-admin" else "named-admin")
        assert browser.call("POST", "/actions/account/alice/credit", form=form, headers=headers).status == 303
        assert c.store.wallet_balance("alice") == 1250


def test_disabled_mode_retains_old_claims_and_no_financial_form(web):
    c = web
    c.ctx.cfg = replace(c.cfg, usd_slot_credit_enabled=False)
    user = c.browser()
    assert user.call("GET", "/account/purchase?machine_number=1", headers=c.public).status == 404
    assert "Fleet credit" not in user.call("GET", "/account", headers=c.public).body


def test_expired_paid_claim_stops_new_pairing_atomically_but_keeps_claim_and_login(web):
    c = web
    grant(c)
    order = c.store.purchase_slot("alice", "node-a", expected_cents=1000,
        idempotency_key="expired-purchase", now=time.time(), max_age_s=90)
    c.store.move_slot(order["slot_id"], slots.CLAIMED)
    c.store.move_slot(order["slot_id"], slots.ACTIVE)
    c.store.set_node_access("node-a", "192.0.2.1", 2222, public_key())
    before = financial(c)
    def check(row):
        return wallet_access.allowed(c.store, row, now=order["period_end_at"])
    with pytest.raises(StoreError, match="renew"):
        c.store.request_cli_pairing(order["slot_id"], "alice", now=time.time(), admit=check)
    assert c.store._conn.execute("SELECT COUNT(*) FROM cli_pairings").fetchone()[0] == 0
    code = c.store.request_cli_pairing(order["slot_id"], "alice", now=time.time())
    with pytest.raises(StoreError, match="renew"):
        c.store.register_cli_device(code, public_key(), "laptop", now=time.time(), admit=check)
    assert c.store._conn.execute("SELECT COUNT(*) FROM cli_pairings").fetchone()[0] == 1
    assert c.store._conn.execute("SELECT COUNT(*) FROM cli_devices").fetchone()[0] == 0
    assert financial(c) == before
    assert c.store.register_cli_device(code, public_key(), "laptop", now=time.time())


def test_review_hold_hides_new_financial_forms_but_completed_receipt_replays(web):
    from tests.test_wallet_topups import hold, ready, settle
    c = web
    grant(c)
    user = c.browser()
    fields = quote(c, user)
    assert user.call("POST", "/account/purchase", form=fields, headers=c.public).status == 303
    topup = ready(c.store)
    settle(c.store, topup)
    hold(c.store)
    before = financial(c)
    review = user.call("GET", "/account/renew?slot_id=slot-node-a", headers=c.public)
    assert review.status == 200 and "under review" in review.body
    assert not Flow(review.body).find("form", action="/account/renew")
    assert user.call("POST", "/account/purchase", form=fields, headers=c.public).status == 303
    assert financial(c) == before


def test_financial_form_rejects_duplicate_fields_before_spending(web):
    import urllib.parse

    from tests.test_api import call
    c = web
    grant(c)
    user = c.browser()
    fields = quote(c, user)
    raw = urllib.parse.urlencode(fields) + "&expected_cents=1000"
    headers = {**c.public, "Cookie": "; ".join(k + "=" + v for k, v in user.jar.items()),
               "Content-Type": "application/x-www-form-urlencoded"}
    before = financial(c)
    assert call(c.server, "POST", "/account/purchase", raw, headers)[0] == 400
    assert financial(c) == before


def test_financial_read_failure_is_not_displayed_as_a_usable_zero_balance(web, monkeypatch):
    c = web
    user = c.browser()
    def failed(*args, **kwargs):
        raise RuntimeError("PRIVATE FINANCIAL READ DETAIL")
    monkeypatch.setattr(c.store, "wallet_balance", failed)
    page = user.call("GET", "/account", headers=c.public)
    assert page.status == 200 and "Credit is unavailable" in page.body
    assert "PRIVATE" not in page.body and "$0.00" not in page.body
    review = user.call("GET", "/account/purchase?machine_number=1", headers=c.public)
    assert review.status == 400 and "PRIVATE" not in review.body


def test_expired_device_status_connect_and_registration_return_renewal_without_losing_identity(web):
    from tests.test_api import call
    c = web
    past = time.time() - 70 * 86400
    c.store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=past)
    c.store.insert_heartbeat("node-a", past, {"node_id": "node-a", "mode": "machine"})
    grant(c)
    order = c.store.purchase_slot("alice", "node-a", expected_cents=1000,
        idempotency_key="past-purchase", now=past, max_age_s=90)
    assert order["period_end_at"] < time.time()
    c.store.move_slot(order["slot_id"], slots.CLAIMED)
    c.store.move_slot(order["slot_id"], slots.ACTIVE)
    c.store.set_node_access("node-a", "192.0.2.1", 2222, public_key())
    code = c.store.request_cli_pairing(order["slot_id"], "alice", now=time.time())
    device = c.store.register_cli_device(code, public_key(), "laptop", now=time.time())
    second = c.store.request_cli_pairing(order["slot_id"], "alice", now=time.time())
    headers = {**c.public, "Authorization": "Bearer " + device["device_token"]}
    for path in ("/api/cli/status", "/api/cli/connect"):
        code, body, _ = call(c.server, "GET", path, headers=headers)
        assert code == 403 and wallet_access.EXPIRED.encode() in body
    code, body, _ = call(c.server, "POST", "/api/cli/register",
        {"public_key": public_key(2), "device_name": "another"},
        {**c.public, "Authorization": "Bearer " + second})
    assert code == 403 and wallet_access.EXPIRED.encode() in body
    assert c.store.resolve_cli_device(device["device_token"], now=time.time(), touch=False)
    assert c.store._conn.execute("SELECT COUNT(*) FROM cli_devices").fetchone()[0] == 1
    assert c.store._conn.execute("SELECT COUNT(*) FROM cli_pairings").fetchone()[0] == 1
    assert c.store.get_slot(order["slot_id"])["held_by"] == "alice"


@pytest.mark.parametrize("invalid", ["invalid-ascii", "错误令牌"])
def test_bad_ascii_and_unicode_CSRF_fail_safely_for_customer_and_admin(web, invalid):
    c = web
    grant(c)
    user = c.browser()
    fields = quote(c, user)
    before = financial(c)
    response = user.call("POST", "/account/purchase", form={**fields, "csrf": invalid},
                         headers=c.public)
    assert response.status == 403 and "bad or missing csrf" in response.body
    header = "Basic " + base64.b64encode(("operator:" + c.cfg.admin_token).encode()).decode()
    response = user.call("POST", "/actions/account/alice/credit",
        form={"csrf": invalid, "amount": "10", "reason": "Synthetic", "idempotency_key": "csrf"},
        headers={**c.admin, "Authorization": header})
    assert response.status == 403 and "bad or missing csrf" in response.body
    assert financial(c) == before


def test_existing_slot_controls_precede_collapsed_credit_history_and_skip_first_purchase_steps(web):
    c = web
    grant(c)
    user = c.browser()
    first = user.call("GET", "/account", headers=c.public)
    assert [item for item in Flow(first.body).find("ol") if item.has_class("wallet-steps")]
    fields = quote(c, user)
    assert user.call("POST", "/account/purchase", form=fields, headers=c.public).status == 303
    page = user.call("GET", "/account", headers=c.public)
    parsed = Flow(page.body)
    assert not [item for item in parsed.find("ol") if item.has_class("wallet-steps")]
    [history] = parsed.find("details", id="credit-history")
    [card] = parsed.find("div", id="slot-slot-node-a")
    assert "open" not in history.attrs and parsed.elements.index(card) < parsed.elements.index(history)
    assert "Credit added" in history.text and "PRIVATE GRANT NOTE" not in page.body
    assert "Paid month active" in card.text
