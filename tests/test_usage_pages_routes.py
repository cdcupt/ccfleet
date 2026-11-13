"""The dedicated usage pages keep existing account and operator boundaries."""
import re
import time

import pytest

from ccfleetd.api import csrf_token
from tests.test_api import basic
from tests.test_html_document_preservation import Document
from tests.test_usersite import Browser, active, claimed, machine, report, serving
from tests.test_usersite import site as site


def assigned(store, browser, node, percent):
    machine(store, node, users=("slot01",))
    slot = active(store, claimed(store, browser, node), node)
    now = time.time()
    report(store, node, [{"unix_user": "slot01", "present": True,
                         "credentials": {"logged_in": True},
                         "quota": {"checked_at": now, "week": {"used_pct": 51},
                                   "models": {"fable": {"used_pct": percent,
                                                         "resets_at": now + 3600}}}}])
    return slot


@pytest.mark.parametrize("prefix", ["", "/en", "/zh"])
def test_usage_page_shows_only_current_holders_limits(site, prefix):
    store, sign_in, _ = site
    owner = sign_in(quota=1)
    mine = assigned(store, owner, "mine", 100)
    other = sign_in("other", "other@example.test", quota=1)
    theirs = assigned(store, other, "theirs", 23)
    response = owner.call("GET", prefix + "/account/usage")
    assert response.status == 200
    text = Document(response.body).text
    assert mine["name"] in text and theirs["name"] not in text
    assert "Fable" in text and "100%" in text and "51%" in text
    assert "23%" not in text
    assert ('用量与额度' if prefix == '/zh' else 'Usage & limits') in text
    if prefix == '/zh':
        assert 'class="copy-en"' not in response.body
    else:
        assert 'class="copy-zh"' not in response.body
    stranger = Browser(owner.port).call("GET", prefix + "/account/usage")
    assert stranger.status == 200
    assert mine["name"] not in stranger.body and "100%" not in Document(stranger.body).text
    assert 'name="csrf"' not in stranger.body


def test_operator_usage_page_requires_operator_auth_and_uses_scope(site):
    store, sign_in, cfg = site
    owner = sign_in(quota=1)
    mine = assigned(store, owner, "mine", 100)
    assert mine["name"] not in owner.call("GET", "/admin/usage").body
    response = owner.call("GET", "/admin/usage", headers=basic(cfg.admin_token))
    assert response.status == 200 and mine["name"] in response.body
    assert "Fable" in response.body and "100%" in response.body


@pytest.mark.parametrize("partial", [False, True])
def test_usage_refresh_stays_on_dedicated_page_and_checks_csrf(site, partial):
    store, sign_in, _ = site
    owner = sign_in(quota=1)
    slot = assigned(store, owner, "mine", 100)
    page = owner.call("GET", "/account/usage").body
    csrf = re.search(r'name="csrf" value="([0-9a-f]{64})"', page)[1]
    path = f'/account/slots/{slot["id"]}/quota'
    failed = owner.call("POST", path, form={"csrf": "0" * 64, "usage_page": "1"})
    assert failed.status == 403 and store.get_slot(slot["id"])["quota_wanted_at"] is None
    response = owner.call("POST", path, form={"csrf": csrf, "usage_page": "1"},
                          headers={"X-Fleet-Partial": "1"} if partial else {})
    assert response.status == (200 if partial else 303)
    location = response.body if partial else response.getheader("Location")
    assert "/account/usage?note=reading" in location
    assert store.get_slot(slot["id"])["quota_wanted_at"] is not None


def test_operator_refresh_stays_on_dedicated_page(site):
    store, sign_in, cfg = site
    owner = sign_in(quota=1)
    slot = assigned(store, owner, "mine", 100)
    response = owner.call("POST", f'/actions/slot/{slot["id"]}/quota',
                          form={"csrf": csrf_token(cfg), "usage_page": "1"},
                          headers=basic(cfg.admin_token))
    assert response.status == 303
    assert response.getheader("Location").startswith("/admin/usage#")


def test_usage_page_refresh_feedback_is_bounded_and_localized(site):
    _, sign_in, _ = site
    owner = sign_in()
    response = owner.call("GET", "/account/usage?note=reading")
    assert "A new quota reading was requested" in Document(response.body).text
    response = owner.call("GET", "/zh/account/usage?note=reading")
    assert "已请求新的额度读数" in Document(response.body).text
    response = owner.call("GET", "/account/usage?note=PRIVATE_UNTRUSTED_TEXT")
    assert "PRIVATE_UNTRUSTED_TEXT" not in response.body
    query = "&".join(f"key{i}=value" for i in range(17))
    assert owner.call("GET", "/account/usage?" + query).status == 400


def test_usage_pages_respect_separate_product_and_admin_hosts(monkeypatch):
    generator = serving(monkeypatch, admin_host="admin.example.test")
    _, sign_in, cfg = next(generator)
    try:
        browser = sign_in()
        assert browser.call("GET", "/admin/usage", headers={
            "Host": "product.example.test", **basic(cfg.admin_token)}).status == 404
        assert browser.call("GET", "/account/usage", headers={
            "Host": "admin.example.test"}).status == 404
        assert browser.call("GET", "/zh/admin/usage", headers={
            "Host": "admin.example.test", **basic(cfg.admin_token)}).status == 200
    finally:
        generator.close()
