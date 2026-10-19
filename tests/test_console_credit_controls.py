"""The admin's USD-credit mode replaces count controls without changing their data."""
from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from ccfleetd import consoleslots, wallet_routes, wallet_ui
from ccfleetd.store import Store
from tests.test_account_setup_flow import Flow
from tests.test_console_slots import accounts_card
from tests.test_wallet_http import web as web  # noqa: F401 -- imported synthetic HTTP fixture


@pytest.mark.parametrize("credit_mode,actor", [
    (True, "google-admin"), (True, "token-admin"), (False, "google-admin"),
])
def test_admin_credit_mode_hides_count_forms_and_legacy_mode_keeps_them(web, credit_mode, actor):
    c = web
    c.ctx.cfg = replace(c.cfg, usd_slot_credit_enabled=credit_mode)
    c.store.set_slot_quota("alice", 3)
    before = (c.store.get_account("alice")["slot_quota"], c.store.wallet_balance("alice"),
              c.store.list_wallet_entries("alice"), c.store.list_slot_purchases("alice"))
    admin = c.browser("admin", site="admin")
    headers = dict(c.admin)
    if actor == "token-admin":
        headers["Authorization"] = "Basic " + base64.b64encode(
            ("operator:" + c.cfg.admin_token).encode()).decode()
    page = admin.call("GET", "/admin", headers=headers)
    assert page.status == 200
    card = accounts_card(page.body)
    parsed = Flow(card)
    allowance_forms = [form for form in parsed.find("form")
                       if form.attrs.get("action", "").endswith("/allowance")]
    credit_forms = [form for form in parsed.find("form")
                    if form.attrs.get("action", "").endswith("/credit")]
    if credit_mode:
        assert not allowance_forms and not parsed.find("input", name="count")
        assert "Set allowance" not in card and "Count allowances" not in card
        assert "Earlier manual payment records below do not add Fleet credit." in card
        if actor == "google-admin":
            assert len(credit_forms) == len(c.store.list_accounts())
            assert parsed.find("form", action="/actions/account/alice/credit")
        else:
            assert not credit_forms and "Sign in using an admin account to add credit." in card
    else:
        assert len(allowance_forms) == len(c.store.list_accounts())
        assert parsed.find("input", name="count", value="3")
        assert "Set allowance" in card and not credit_forms
    assert (c.store.get_account("alice")["slot_quota"], c.store.wallet_balance("alice"),
            c.store.list_wallet_entries("alice"), c.store.list_slot_purchases("alice")) == before


def test_admin_credit_fragment_is_english_and_preserves_all_form_fields(web, monkeypatch):
    csrf, nonce = 'SYNTHETIC_CSRF<&"', 'usd-synthetic-grant'
    monkeypatch.setattr(wallet_routes, "nonce", lambda: nonce)
    expected = Flow(wallet_ui.admin_credit_grant_form(
        account_id="alice", csrf=csrf, idempotency_key=nonce))
    actual = Flow(consoleslots._accounts_card(web.store,
        {"alice": web.store.get_account("alice")}, csrf, 1_700_000_000, {},
        wallet_enabled=True, credit_actor=True))
    [form] = actual.find("form", action="/actions/account/alice/credit")
    [original] = expected.find("form", action="/actions/account/alice/credit")
    assert form.attrs == original.attrs
    assert {item.attrs["name"]: item.attrs for item in actual.inside(form, "input")} == {
        item.attrs["name"]: item.attrs for item in expected.inside(original, "input")}
    fields = {item.attrs["name"]: item.attrs.get("value", "")
              for item in actual.inside(form, "input")}
    assert fields["csrf"] == csrf and fields["idempotency_key"] == nonce
    assert [button.text for button in actual.inside(form, "button")] == ["Add credit"]
    assert "Amount · USD" in form.text and "Private reason" in form.text
    assert not any("\u4e00" <= char <= "\u9fff" for char in form.text)


def test_admin_inline_commands_and_machine_identity_wrap_without_changing_text(web):
    empty = Store(":memory:")
    try:
        empty_page = consoleslots.section(empty, "synthetic-csrf", 1_700_000_000)
    finally:
        empty.close()
    web.store.set_slot_quota("alice", 1)
    web.store.add_node("own-machine", "alice", now=1_700_000_000)
    web.store.hold_owner_node("own-machine", "alice", now=1_700_000_000)
    occupied_page = consoleslots.section(web.store, "synthetic-csrf", 1_700_000_000)
    codes = Flow(empty_page + occupied_page).find("code")
    assert {code.text for code in codes} == {
        "ccfleetd slot add <machine> --machine <machine> --unix-user slot01",
        "ccfleetd account role <email> admin",
        "ccfleetd node hold own-machine --none", "node-a", "slot-node-a",
    }
    assert all(code.has_class("code-wrap") for code in codes)


def test_admin_monthly_history_is_read_only_and_hides_provider_identifiers():
    orders = [{"machine_number": 7, "amount_cents": 1283, "provider_status": "active",
               "subscription_id": "sub_private_do_not_render", "customer_id": "cus_private",
               "session_id": "cs_private", "claim_id": "private-claim",
               "cancel_at_period_end": True, "period_end_at": 1_800_000_000},
              {"machine_number": 8, "amount_cents": 1068, "status": "checkout_open",
               "subscription_id": None}]
    body = consoleslots._monthly_account_history(orders)
    parsed = Flow(body)
    assert "Machine 7" in body and "$12.83" in body and "Stops at period end" in body
    assert "Machine 8" not in body
    assert not parsed.find("form") and not parsed.find("input")
    assert not any(value in body for value in
                   ("sub_private", "cus_private", "cs_private", "private-claim", "/portal"))
    assert consoleslots._monthly_account_history([]) == ""
