"""Operator language aliases retain authentication, CSRF and the selected language."""
from __future__ import annotations

import json
import re
import urllib.parse

import pytest

from ccfleetd import bilingual, consoleslots, render, slots
from tests.test_account_setup_flow import Flow
from tests.test_console_slots import console as console  # noqa: F401
from tests.test_console_slots import held_slot, machine_said
from tests.test_html_document_preservation import Document
from tests.test_language_routes import ADMIN  # noqa: F401
from tests.test_language_routes import language_site as language_site

AUTH = {"Authorization": "Bearer operator-token-long-enough"}


@pytest.mark.parametrize("language,heading", [("en", "Fleet admin"), ("zh", "Fleet 管理后台")])
def test_operator_dashboard_has_working_language_switch_and_localized_controls(
        console, language, heading):
    store, call = console
    held_slot(store)
    reply = call("GET", f"/{language}/admin")
    assert reply.status == 200
    assert heading in Document(reply.body).text
    assert 'href="/en/admin"' in reply.body and 'href="/zh/admin"' in reply.body
    assert f'action="/{language}/actions/' in reply.body if language == "zh" else (
        'action="/actions/' in reply.body)
    assert ("Set allowance" if language == "en" else "设置槽位上限") in reply.body
    assert ("Slots &amp; machines" if language == "en" else "槽位与机器") in reply.body
    assert ("Advanced machine controls" if language == "en" else "高级机器控制") in reply.body
    assert ('class="copy-zh"' if language == "en" else 'class="copy-en"') not in reply.body
    assert render.ADMIN_QUOTA_POLL_CSP in reply.getheader("Content-Security-Policy")
    assert render.ADMIN_QUOTA_POLL_TAG in reply.body


def test_chinese_admin_action_redirect_and_partial_quota_response_keep_language(language_site):
    store, _, make_browser = language_site
    browser = make_browser(ADMIN)
    slot = held_slot(store)
    machine_said(store, {"unix_user": "slot01", "credentials": {
        "present": True, "logged_in": True}})
    # The slot is active only after its native sign-in is confirmed.
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
        "provisioned_for": slot["claimed_at"],
        "credentials": {"present": True, "logged_in": True}}], now=slot["claimed_at"] + 1)
    page = browser.call("GET", "/zh/admin", headers=AUTH)
    token = re.search(r'name="csrf" value="([0-9a-f]{64})"', page.body)[1]
    assert browser.call("POST", "/zh/actions/node/m1/disable", {"csrf": token},
                        headers=AUTH).getheader(
        "Location") == "/zh/admin#manage"
    store.set_enabled("m1", True)
    reply = browser.call("POST", f"/zh/actions/slot/{slot['id']}/quota",
                         {"csrf": token}, headers={**AUTH, "X-Fleet-Partial": "1"})
    assert reply.status == 200 and json.loads(reply.body) == {"location": "/zh/admin#usage"}
    refused = browser.call("POST", f"/zh/actions/slot/{slot['id']}/quota",
                           {"csrf": "incorrect"}, headers=AUTH)
    assert refused.status == 403


def test_dedicated_admin_host_supports_language_but_product_host_stays_private(language_site):
    _, _, browser = language_site
    assert browser().call("GET", "/zh/admin", headers=AUTH).status == 404
    page = browser(ADMIN).call("GET", "/zh/admin", headers=AUTH)
    assert page.status == 200 and 'lang="zh-Hans"' in page.body
    assert "Fleet 管理后台" in page.body
    door = browser(ADMIN).call("GET", "/zh/admin")
    assert door.status == 401 and "使用 Google 登录" in door.body
    assert 'href="/zh/auth/basic"' in door.body
    basic = browser(ADMIN).call("GET", "/zh/auth/basic", headers=AUTH)
    assert basic.status == 303 and basic.getheader("Location") == "/zh/admin"
    assert browser(ADMIN).call("POST", "/zh/auth/basic", form={}, headers=AUTH).status == 404


def test_cancelled_operator_oauth_returns_to_selected_admin_language(language_site):
    _, _, make_browser = language_site
    browser = make_browser(ADMIN)
    start = browser.call("GET", "/zh/auth/google/start?next=%2Fzh%2Fadmin")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(start.getheader("Location")).query)
    callback = "/auth/google/callback?" + urllib.parse.urlencode({
        "state": params["state"][0], "error": "access_denied"})
    cancelled = browser.call("GET", callback)
    assert cancelled.status == 303
    assert cancelled.getheader("Location") == "/zh/admin?signin=cancelled"


def test_admin_aliases_are_bounded_and_never_localize_api_or_unknown_actions():
    assert bilingual.route_target("/zh/admin") == ("/admin", "zh", True)
    assert bilingual.route_target("/zh/actions/slot/slot-1/quota", "POST") == (
        "/actions/slot/slot-1/quota", "zh", True)
    for target in ("/zh/api/nodes", "/zh/actions/slot/slot-1/unknown",
                   "/zh/actions/slot/../quota", "/zh/actions/slot/%2e%2e/quota"):
        assert bilingual.route_target(target, "POST") is None


def test_authored_leaf_translation_preserves_dynamic_values_and_language_on_auto_refresh():
    source = ('<meta http-equiv="refresh" content="4;url=/admin">'
              '<button data-copy-zh="移除">Remove</button>'
              '<input name="confirm" value="Remove">'
              '<span class="row-name">Remove</span>')
    result = bilingual.project_html(source, "zh", "/admin")
    assert '<button>移除</button>' in result
    assert 'value="Remove"' in result and '<span class="row-name">Remove</span>' in result
    assert 'content="4;url=/zh/admin"' in result


@pytest.mark.parametrize("language", ["en", "zh"])
def test_reclaim_warning_and_confirmation_labels_localize_without_changing_exact_name(language):
    name, csrf = 'mine<&"quote>', 'csrf<&"token>'
    source = consoleslots._slot_actions({"id": "slot-1", "name": name,
                                        "state": slots.ACTIVE}, csrf)
    page = Flow(bilingual.project_html(source, language, "/admin"))
    [confirmation] = page.find("input", name="confirm")
    [token] = page.find("input", name="csrf")
    [form] = page.find("form")
    assert "value" not in confirmation.attrs and "required" in confirmation.attrs
    assert confirmation.attrs["autocomplete"] == "off"
    assert token.attrs["value"] == csrf
    assert page.find("b")[0].text == name
    if language == "zh":
        assert confirmation.attrs["placeholder"] == f"输入 {name}"
        assert confirmation.attrs["aria-label"] == f"输入 {name} 以确认收回并清除数据"
        assert "所有文件和 Claude 登录信息" in page.find("p")[-1].text
        assert "Wipes it" not in page.find("p")[-1].text
        assert form.attrs["action"] == "/zh/actions/slot/slot-1/reclaim"
    else:
        assert confirmation.attrs["placeholder"] == f"type {name}"
        assert confirmation.attrs["aria-label"] == f"Type {name} to confirm"
        assert "everything on it goes, its Claude sign-in with it" in page.find("p")[-1].text
        assert form.attrs["action"] == "/actions/slot/slot-1/reclaim"
