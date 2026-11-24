"""Frontend, bilingual, pricing, CSP, and account-entry tests for OpenRouter."""

from __future__ import annotations

import base64
import hashlib
import http.client
import re
from dataclasses import replace
from decimal import Decimal

import pytest

from ccfleetd import usersite
from tests.test_openrouter import MODELS
from tests.test_openrouter import router as router  # noqa: F401
from tests.test_wallet_http import form_from, grant
from tests.test_wallet_http import web as web  # noqa: F401

SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)
ID_RE = re.compile(r'\bid="([^"]+)"')


def _assert_csp_and_locale(response, locale: str, *, allow_ids_once: bool = True) -> None:
    csp = response.getheader("Content-Security-Policy") or ""
    allowed = set(re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", csp))
    assert allowed, "Missing script-src SHA-256 hashes in CSP header"
    for match in SCRIPT_RE.finditer(response.body):
        if "src=" in match.group("attrs").lower():
            continue
        digest = "sha256-" + base64.b64encode(
            hashlib.sha256(match.group("body").encode("utf-8")).digest()
        ).decode("ascii")
        assert digest in allowed, f"Inline script hash {digest} missing from CSP"
    forbidden = 'class="copy-zh"' if locale == "en" else 'class="copy-en"'
    assert forbidden not in response.body
    expected_lang = 'lang="en"' if locale == "en" else 'lang="zh-Hans"'
    assert expected_lang in response.body[:400]
    if allow_ids_once:
        seen: set[str] = set()
        dups: list[str] = []
        for match in ID_RE.finditer(response.body):
            ident = match.group(1)
            if ident in seen:
                dups.append(ident)
            seen.add(ident)
        assert not dups, f"Duplicate IDs: {dups}"


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_account_entry_gated_and_reflects_account_specific_pricing(router, locale):
    c = router
    prefix = "/" + locale
    # Enabled: regular user sees OpenRouter entry card & sub-nav with +5% pricing
    user = c.browser("alice")
    page = user.call("GET", prefix + "/account", headers=c.public)
    assert page.status == 200
    _assert_csp_and_locale(page, locale)
    href = ("/zh" if locale == "zh" else "") + "/account/openrouter"
    assert f'href="{href}"' in page.body
    assert 'id="openrouter"' in page.body
    assert "5%" in page.body
    assert "0.2.25" in page.body
    assert "sk-or-" not in page.body

    # Active key pill appears once a key exists without exposing the key
    key = c.store.openrouter_key("alice")
    with_key = user.call("GET", prefix + "/account", headers=c.public)
    assert ("密钥已启用" if locale == "zh" else "Key active") in with_key.body
    assert key not in with_key.body

    # Designated owner admin sees Admin covered / $0.00 pricing on account entry card
    c.ctx.cfg = replace(c.ctx.cfg, openrouter_admin_account_id="admin")
    c.service.cfg = c.ctx.cfg
    admin_page = c.browser("admin").call("GET", prefix + "/account", headers=c.public)
    assert admin_page.status == 200
    _assert_csp_and_locale(admin_page, locale)
    assert "$0.00" in admin_page.body
    assert ("管理员覆盖" if locale == "zh" else "Admin covered") in admin_page.body

    # Non-designated admin still sees regular +5% pricing
    c.store.set_account_role("bob", "admin")
    other_admin = c.browser("bob").call("GET", prefix + "/account", headers=c.public)
    assert "5%" in other_admin.body
    assert ("管理员覆盖" if locale == "zh" else "Admin covered") not in other_admin.body

    # Disabled: zero OpenRouter links or entry card on /account
    c.ctx.cfg = replace(c.ctx.cfg, openrouter_models="")
    disabled_page = user.call("GET", prefix + "/account", headers=c.public)
    assert 'id="openrouter"' not in disabled_page.body
    assert "/account/openrouter" not in disabled_page.body


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_signed_out_openrouter_door_en_and_zh(router, locale):
    c = router
    prefix = "/" + locale
    conn = http.client.HTTPConnection(*c.server.server_address, timeout=5)
    try:
        conn.request("GET", prefix + "/account/openrouter", headers=c.public)
        page = conn.getresponse()
        page.body = page.read().decode("utf-8")
    finally:
        conn.close()
    assert page.status == 200
    _assert_csp_and_locale(page, locale)
    assert "/auth/google/start?next=" in page.body
    assert "/openrouter" in page.body
    assert "5%" in page.body
    assert "sk-or-" not in page.body
    expected_title = "OpenRouter 模型与用量" if locale == "zh" else "CC Fleet · OpenRouter"
    assert expected_title in page.body


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_openrouter_page_key_lifecycle_models_cli_and_receipts(router, locale):
    c = router
    prefix = "/" + locale
    grant(c, "25")
    user = c.browser("alice")

    # 1. Initial GET shows no active key, +5% pricing, $25.00 credit, CLI 0.2.25 instructions
    initial = user.call("GET", prefix + "/account/openrouter", headers=c.public)
    assert initial.status == 200
    assert initial.getheader("Cache-Control") == "no-store"
    _assert_csp_and_locale(initial, locale)
    assert "$25.00" in initial.body
    assert "5%" in initial.body
    assert "/openrouter" in initial.body
    assert "0.2.25" in initial.body
    assert "ccfleet openrouter --check" in initial.body
    assert "ccfleet openrouter" in initial.body
    assert "/model" in initial.body
    assert ("尚无有效密钥" if locale == "zh" else "No active key") in initial.body
    assert ("暂无 OpenRouter 请求记录" if locale == "zh" else "No OpenRouter requests yet") in initial.body
    assert "sk-or-" not in initial.body

    # 2. Issue key -> shown ONCE, progressive Copy button present, language switcher safe
    create_action = ("/zh" if locale == "zh" else "") + "/account/openrouter/create"
    form = form_from(initial.body, create_action)
    created = user.call("POST", prefix + "/account/openrouter/create", form=form, headers=c.public)
    assert created.status == 200
    assert created.getheader("Cache-Control") == "no-store"
    _assert_csp_and_locale(created, locale)
    [key] = re.findall(r"ccf_or_[A-Za-z0-9_-]{43}", created.body)
    assert "data-copy-code" in created.body
    assert ("已生成新密钥" if locale == "zh" else "New key issued") in created.body
    assert "/account/openrouter/create" in created.body
    # Switch-language link on POST response must point to safe GET /account/openrouter
    other_prefix = "/en" if locale == "zh" else "/zh"
    assert f'href="{other_prefix}/account/openrouter"' in created.body

    # 3. Record settled, pending, and released receipts + recent model ordering
    rid_settled = c.store.openrouter_reserve("alice", key, MODELS[1], 200)
    c.store.openrouter_submit(rid_settled, "alice", key)
    c.store.openrouter_identify(rid_settled, "gen-ui-settled")
    c.store.openrouter_settle(rid_settled, "gen-ui-settled", Decimal("1.00"))

    rid_pending = c.store.openrouter_reserve("alice", key, MODELS[0], 50)
    c.store.openrouter_submit(rid_pending, "alice", key)

    rid_released = c.store.openrouter_reserve("alice", key, MODELS[0], 25)
    c.store.openrouter_release(rid_released)

    after = user.call("GET", prefix + "/account/openrouter", headers=c.public)
    assert after.status == 200
    _assert_csp_and_locale(after, locale)
    assert key not in after.body
    assert "sk-or-" not in after.body
    assert ("密钥已启用" if locale == "zh" else "Active key on file") in after.body
    # MODELS[1] was settled last, so it must appear BEFORE MODELS[0] as the next default
    assert after.body.index(MODELS[1]) < after.body.index(MODELS[0])
    assert ("上次使用 · 默认" if locale == "zh" else "Last used · Default") in after.body
    # Receipts show settled ($1.00 + $0.05 = $1.05), pending ($0.50 hold), and released
    assert "$1.05" in after.body and "$0.05" in after.body
    assert "$0.50" in after.body
    assert rid_settled in after.body and rid_pending in after.body and rid_released in after.body
    assert ("已结算" if locale == "zh" else "Settled") in after.body
    assert ("已预留" if locale == "zh" else "Reserved") in after.body
    assert ("已解除预留" if locale == "zh" else "Released") in after.body

    # 4. Revoke key -> back to No active key
    revoke_form = {"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret)}
    revoked = user.call("POST", prefix + "/account/openrouter/revoke", form=revoke_form, headers=c.public)
    assert revoked.status == 200
    _assert_csp_and_locale(revoked, locale)
    assert ("尚无有效密钥" if locale == "zh" else "No active key") in revoked.body
    assert key not in revoked.body


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_openrouter_page_admin_covered_pricing_and_receipts(router, locale):
    c = router
    prefix = "/" + locale
    c.ctx.cfg = replace(c.ctx.cfg, openrouter_admin_account_id="admin")
    c.service.cfg = c.ctx.cfg
    admin_key = c.store.openrouter_key("admin")
    rid = c.store.openrouter_reserve("admin", admin_key, MODELS[0], 100, admin_account_id="admin")
    c.store.openrouter_submit(rid, "admin", admin_key)
    c.store.openrouter_identify(rid, "gen-admin-ui")
    c.store.openrouter_settle(rid, "gen-admin-ui", Decimal("0.42"))

    page = c.browser("admin").call("GET", prefix + "/account/openrouter", headers=c.public)
    assert page.status == 200
    _assert_csp_and_locale(page, locale)
    assert "$0.00" in page.body
    assert "$0.42" in page.body
    assert rid in page.body
    assert "sk-or-" not in page.body
    assert admin_key not in page.body
    if locale == "en":
        assert "Fleet covers this admin account" in page.body
        assert "Server-side only" in page.body
    else:
        assert "Fleet 承担此管理员账户的 OpenRouter 用量费用" in page.body
        assert "仅限服务器端" in page.body
    # Admin-covered pricing panel should not push an Add credit CTA button
    assert 'class="btn primary or-topup-btn"' not in page.body


def test_openrouter_catalog_overflow_places_ten_recent_models_first(router):
    c = router
    offered = [f"example/model-{n:02d}" for n in range(14)]
    c.service.models = dict.fromkeys(offered, (Decimal("0.5"), Decimal("1.5")))
    key = c.store.openrouter_key("alice")
    for model in offered[2:12]:
        rid = c.store.openrouter_reserve("alice", key, model, 0)
        c.store.openrouter_submit(rid, "alice", key)
        c.store.openrouter_identify(rid, "gen-" + rid)
        c.store.openrouter_settle(rid, "gen-" + rid, Decimal("0"))

    page = c.browser("alice").call("GET", "/en/account/openrouter", headers=c.public)
    assert page.status == 200
    _assert_csp_and_locale(page, "en")
    assert "14 offered · 10 recent" in page.body
    assert "Show 4 more offered models" in page.body
    # Most recently used (model-11) is first; non-recent (model-00) is inside the overflow details
    start = page.body.index('id="openrouter-models"')
    end = page.body.index('id="openrouter-receipts"')
    section = page.body[start:end]
    assert section.index("example/model-11") < section.index("example/model-02")
    assert section.index("example/model-02") < section.index('class="or-more-models"')
    assert section.index('class="or-more-models"') < section.index("example/model-00")
