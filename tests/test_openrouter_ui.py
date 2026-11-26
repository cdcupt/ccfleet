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
    assert "0.2.26" in page.body
    assert "0.2.27" in page.body
    assert "1,048,576" in page.body
    assert "bypassPermissions" in page.body
    assert "--permission-mode auto" in page.body
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

    # 1. Initial GET shows no active key, +5% pricing, $25.00 credit, CLI 0.2.26/0.2.27 instructions
    initial = user.call("GET", prefix + "/account/openrouter", headers=c.public)
    assert initial.status == 200
    assert initial.getheader("Cache-Control") == "no-store"
    _assert_csp_and_locale(initial, locale)
    assert "$25.00" in initial.body
    assert "5%" in initial.body
    assert "/openrouter" in initial.body
    assert "0.2.26" in initial.body
    assert "0.2.27" in initial.body
    assert "1,048,576" in initial.body
    assert "bypassPermissions" in initial.body
    assert "ccfleet update" in initial.body
    assert "ccfleet openrouter --check" in initial.body
    assert "ccfleet openrouter --forget-key" in initial.body
    assert "ccfleet openrouter --permission-mode auto" in initial.body
    assert "CCFLEET_OPENROUTER_TOKEN" in initial.body
    assert "ccfleet openrouter" in initial.body
    assert "/model" in initial.body
    assert 'id="openrouter-troubleshooting"' in initial.body
    assert ("尚无有效密钥" if locale == "zh" else "No active key") in initial.body
    assert (
        "暂无 OpenRouter 请求记录" if locale == "zh" else "No OpenRouter requests yet"
    ) in initial.body
    assert "sk-or-" not in initial.body

    # 2. Issue key -> 303 PRG redirect to /account/openrouter, shown ONCE, Copy button present
    create_action = ("/zh" if locale == "zh" else "") + "/account/openrouter/create"
    form = form_from(initial.body, create_action)
    assert "request_id" in form and len(form["request_id"]) >= 16
    post_created = user.call(
        "POST", prefix + "/account/openrouter/create", form=form, headers=c.public
    )
    assert post_created.status == 303
    assert post_created.getheader("Cache-Control") == "no-store"
    assert post_created.getheader("Location") == prefix + "/account/openrouter"
    created = user.call("GET", post_created.getheader("Location"), headers=c.public)
    assert created.status == 200
    assert created.getheader("Cache-Control") == "no-store"
    _assert_csp_and_locale(created, locale)
    [key] = re.findall(r"ccf_or_[A-Za-z0-9_-]{43}", created.body)
    assert "data-copy-code" in created.body
    assert ("已生成新密钥" if locale == "zh" else "New key issued") in created.body
    assert "/account/openrouter/create" in created.body
    # Switch-language link on landed GET page points cleanly to the other locale
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

    # 4. Revoke key -> 303 PRG redirect back to No active key
    revoke_form = {"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret)}
    post_revoked = user.call(
        "POST", prefix + "/account/openrouter/revoke", form=revoke_form, headers=c.public
    )
    assert post_revoked.status == 303
    assert post_revoked.getheader("Location") == prefix + "/account/openrouter"
    revoked = user.call("GET", post_revoked.getheader("Location"), headers=c.public)
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


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_openrouter_first_party_docs_and_guide_reflect_026_and_027(router, locale):
    c = router
    prefix = "/" + locale
    for path, extra_marker in (
        ("/docs/guide", 'id="openrouter-cli"'),
        ("/docs/library/docs/public-gateway.html", 'id="openrouter"'),
        ("/docs/library/docs/index.html", "public-gateway.html#openrouter"),
    ):
        conn = http.client.HTTPConnection(*c.server.server_address, timeout=5)
        try:
            conn.request("GET", prefix + path, headers=c.public)
            resp = conn.getresponse()
            resp.body = resp.read().decode("utf-8")
        finally:
            conn.close()
        assert resp.status == 200
        _assert_csp_and_locale(resp, locale)
        assert extra_marker in resp.body
        assert "0.2.26" in resp.body and "0.2.27" in resp.body
        if path != "/docs/library/docs/index.html":
            for marker in (
                "ccfleet update",
                "ccfleet openrouter",
                "--forget-key",
                "CCFLEET_OPENROUTER_TOKEN",
                "1,048,576",
                "bypassPermissions",
                "ccfleet openrouter --permission-mode auto",
                "/model",
            ):
                assert marker in resp.body, f"Missing {marker!r} on {prefix + path}"


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_openrouter_recent_usage_gathers_many_receipts_with_clean_timestamps(router, locale):
    c = router
    prefix = "/" + locale
    grant(c, "20")
    key = c.store.openrouter_key("alice")
    now = 1_763_980_000.0
    for idx in range(16):
        day_offset = 0 if idx < 8 else 86400
        model = MODELS[idx % 2]
        rid = c.store.openrouter_reserve("alice", key, model, 50)
        with c.store._write_txn() as db:
            db.execute(
                "UPDATE openrouter_requests SET created_at=? WHERE id=?",
                (now - day_offset - idx * 300, rid),
            )
        c.store.openrouter_submit(rid, "alice", key)
        c.store.openrouter_identify(rid, f"gen-grp-{idx}")
        c.store.openrouter_settle(rid, f"gen-grp-{idx}", Decimal("0.01"))

    page = c.browser("alice").call("GET", prefix + "/account/openrouter", headers=c.public)
    assert page.status == 200
    _assert_csp_and_locale(page, locale)
    # 16 receipts across 2 UTC days x 2 models -> 4 collapsed summary groups
    assert page.body.count('<details class="or-usage-group">') == 4
    assert '<details class="or-usage-group" open>' not in page.body
    assert 'data-local="datetime"' in page.body
    assert ("共 16 条请求 · 已归为 4 组" if locale == "zh" else "16 requests · 4 groups") in page.body


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_openrouter_usage_shows_all_time_totals_beyond_latest_50(router, locale):
    c = router
    prefix = "/" + locale
    c.ctx.cfg = replace(c.ctx.cfg, openrouter_admin_account_id="admin")
    c.service.cfg = c.ctx.cfg
    admin_key = c.store.openrouter_key("admin")

    # Day 1 (older, outside latest 50 individual receipts): 20 settled requests totaling $14.482921966448
    day1_base = 1_795_400_000.0
    day1_each = Decimal("0.7241460983224")
    for idx in range(20):
        rid = c.store.openrouter_reserve("admin", admin_key, MODELS[0], 0, admin_account_id="admin")
        with c.store._write_txn() as db:
            db.execute(
                "UPDATE openrouter_requests SET created_at=? WHERE id=?",
                (day1_base + idx * 60, rid),
            )
        c.store.openrouter_submit(rid, "admin", admin_key)
        c.store.openrouter_identify(rid, f"gen-day1-{idx}")
        c.store.openrouter_settle(rid, f"gen-day1-{idx}", day1_each)

    # Day 2 (newer): 60 settled requests totaling $0.9420494352 + 2 unsettled submitted requests
    day2_base = day1_base + 86400
    day2_each = Decimal("0.01570082392")
    for idx in range(60):
        rid = c.store.openrouter_reserve("admin", admin_key, MODELS[0], 0, admin_account_id="admin")
        with c.store._write_txn() as db:
            db.execute(
                "UPDATE openrouter_requests SET created_at=? WHERE id=?",
                (day2_base + idx * 60, rid),
            )
        c.store.openrouter_submit(rid, "admin", admin_key)
        c.store.openrouter_identify(rid, f"gen-day2-{idx}")
        c.store.openrouter_settle(rid, f"gen-day2-{idx}", day2_each)
    for idx in range(2):
        rid = c.store.openrouter_reserve("admin", admin_key, MODELS[0], 0, admin_account_id="admin")
        with c.store._write_txn() as db:
            db.execute(
                "UPDATE openrouter_requests SET created_at=? WHERE id=?",
                (day2_base + (60 + idx) * 60, rid),
            )
        c.store.openrouter_submit(rid, "admin", admin_key)

    # Backward-compatible receipts API stays capped at 50 while overview aggregates all 82 rows
    assert len(c.store.openrouter_receipts("admin")) == 50
    overview = c.store.openrouter_usage_overview("admin")
    assert overview["summary"]["total_requests"] == 82
    assert overview["summary"]["settled_count"] == 80
    assert overview["summary"]["pending_count"] == 2
    assert overview["summary"]["upstream_cost_usd"] == Decimal("15.424971401648")
    assert len(overview["groups"]) == 2
    assert overview["groups"][0]["count"] == 62
    assert len(overview["groups"][0]["receipts"]) == 50
    assert overview["groups"][1]["count"] == 20
    assert overview["groups"][1]["receipts"] == []

    page = c.browser("admin").call("GET", prefix + "/account/openrouter", headers=c.public)
    assert page.status == 200
    _assert_csp_and_locale(page, locale)
    # Headline totals reflect all 80 settled requests & full $15.42 spend (not truncated to < $1)
    assert ">80<" in page.body
    assert 'title="$15.424971401648">$15.42</span>' in page.body
    assert 'title="$14.482921966448">$14.48</span>' in page.body
    assert (
        "共 82 条请求 · 已归为 2 组（总计覆盖全部；最近 50 条可展开）"
        if locale == "zh"
        else "82 requests · 2 groups (all-time totals; latest 50 expandable)"
    ) in page.body
    # Day 2 expands (50 of 62 receipts) while Day 1 renders as a static summary group
    assert page.body.count('<details class="or-usage-group">') == 1
    assert 'class="or-group-head or-group-head-static"' in page.body
    assert (
        "以下仅显示本组最近 50 条明细（共 62 条请求）"
        if locale == "zh"
        else "Showing the 50 most recent requests out of 62 in this group"
    ) in page.body
    assert ("汇总" if locale == "zh" else "Summary") in page.body


def test_openrouter_paid_account_all_time_debits_and_older_hold_beyond_50(router):
    c = router
    grant(c, "50")
    key = c.store.openrouter_key("alice")

    # Request 0: older unsettled hold of $1.25 (will fall outside the latest 50 rows)
    old_hold_rid = c.store.openrouter_reserve("alice", key, MODELS[0], 125)
    c.store.openrouter_submit(old_hold_rid, "alice", key)
    base = 1_795_500_000.0
    with c.store._write_txn() as db:
        db.execute("UPDATE openrouter_requests SET created_at=? WHERE id=?", (base, old_hold_rid))

    # Requests 1..60: 60 settled requests of $0.20 each ($0.21 billed each -> $12.60 total debited)
    for idx in range(1, 61):
        rid = c.store.openrouter_reserve("alice", key, MODELS[0], 25)
        with c.store._write_txn() as db:
            db.execute(
                "UPDATE openrouter_requests SET created_at=? WHERE id=?",
                (base + idx * 10, rid),
            )
        c.store.openrouter_submit(rid, "alice", key)
        c.store.openrouter_identify(rid, f"gen-paid-{idx}")
        c.store.openrouter_settle(rid, f"gen-paid-{idx}", Decimal("0.20"))

    page = c.browser("alice").call("GET", "/en/account/openrouter", headers=c.public)
    assert page.status == 200
    _assert_csp_and_locale(page, "en")
    # All-time settled: 60 requests, $12.00 upstream usage, $12.60 wallet debited
    assert ">60<" in page.body
    assert ">$12.00<" in page.body
    assert ">$12.60<" in page.body
    # Available balance ($50.00 - $12.60 - $1.25 = $36.15) AND older $1.25 hold warning are shown
    assert "$36.15" in page.body
    assert "$1.25 is currently reserved for in-flight OpenRouter requests" in page.body


def test_openrouter_usd_formatting_precision():
    from ccfleetd import openrouter_account

    assert openrouter_account._usd(Decimal("0")) == "$0.00"
    assert openrouter_account._usd(Decimal("1.00")) == "$1.00"
    assert openrouter_account._usd(Decimal("15.424971401648")) == "$15.42"
    assert openrouter_account._usd_exact(Decimal("15.424971401648")) == "$15.424971401648"
    assert openrouter_account._usd(Decimal("1234.567")) == "$1,234.57"
    assert openrouter_account._usd(Decimal("0.42")) == "$0.42"
    assert openrouter_account._usd(Decimal("0.1596774")) == "$0.1597"
    assert openrouter_account._usd(Decimal("0.000027552")) == "$0.000028"
    assert openrouter_account._usd(Decimal("0.0000004")) == "<$0.000001"
    assert openrouter_account._usd_html(Decimal("1.05")) == "$1.05"
    assert (
        openrouter_account._usd_html(Decimal("15.424971401648"))
        == '<span title="$15.424971401648">$15.42</span>'
    )

