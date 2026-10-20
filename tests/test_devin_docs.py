"""Own-account pilot evidence, plan costs and available setup controls stay distinct."""

from pathlib import Path

import pytest

from ccfleetd import bilingual, reference_docs
from tests.test_html_document_preservation import Document

ROOT = Path(__file__).resolve().parents[1]
TARGET = "/docs/library/docs/devin.html"


@pytest.mark.parametrize("language", ["en", "zh"])
def test_devin_guide_has_separate_language_routes_and_a_library_link(language):
    source = (ROOT / "docs/devin.html").read_text()
    assert "docs/devin.html" in reference_docs.PUBLIC_FILES
    assert bilingual.CSS in source and bilingual.SCRIPT_TAG in source
    assert bilingual.route_target(f"/{language}{TARGET}") == (TARGET, language, True)
    page = Document(bilingual.project_html(source, language, TARGET))
    assert not page.duplicate_ids
    assert "".join(page.title).count("CC Fleet") == 1
    assert {"availability", "why", "route", "compatibility", "limits", "cost", "prices", "connect", "commercial"} <= page.ids
    assert f"/en{TARGET}" in page.links and f"/zh{TARGET}" in page.links
    index = bilingual.project_html((ROOT / "docs/index.html").read_text(), language,
                                   "/docs/library/docs/index.html")
    index_target = TARGET if language == "en" else "/zh" + TARGET
    assert index_target in Document(index).links
    if language == "en":
        assert "Why we are evaluating Devin" in page.text
        assert "Devin 是由运营方明确启用的试点" not in page.text
    else:
        assert "为什么评估 Devin" in page.text
        assert "Devin is an operator-enabled pilot" not in page.text


def test_devin_guide_explains_the_native_route_and_limits_private_pilot_evidence():
    source = (ROOT / "docs/devin.html").read_text()
    english = Document(bilingual.project_html(source, "en", TARGET)).text
    chinese = Document(bilingual.project_html(source, "zh", TARGET)).text
    for phrase in (
        "Devin is an operator-enabled pilot",
        "Your account page offers it only after your own slot has been verified",
        "Anthropic remains the default",
        "Check your account page for current provider readiness",
        "official Claude Code",
        "your own Devin CLI on your assigned slot",
        "Fleet API address and key token stay the same",
        "website provider setting chooses Anthropic or Devin",
        "internal MCP bridge",
        "Read, Edit and Bash run on your computer",
        "owner-pilot check through the public Fleet endpoint",
        "passed three local tests",
        "All four streamed replies were verified",
        "Opus 5.5 Max",
        "native-agent compatibility mode",
        "history become labeled context",
        "Images, documents, prior thinking blocks",
        "count_tokens is unsupported",
        "max_tokens is passed as context, not an enforced output or spending cap",
        "Universal Claude Code parity is unverified",
    ):
        assert phrase in english
    for phrase in ("Devin 是由运营方明确启用的试点", "只有自有节点通过验证后", "Anthropic 仍为默认服务",
                   "服务就绪状态", "官方 Claude Code",
                   "自有 Devin CLI", "Fleet API 地址和密钥保持不变", "内部 MCP 桥",
                   "由你的电脑执行", "自有账号试点通过公开 Fleet 入口", "三个本地测试",
                   "四次流式回复均核实", "Opus 5.5 Max", "原生智能体兼容模式",
                   "带标签的上下文", "启动前拒绝", "不支持 count_tokens",
                   "max_tokens 仅作为上下文传入，不是强制的输出或费用上限"):
        assert phrase in chinese
    for removed_plan in ("Fleet front agent", "execution agent", "Outposts", "OpenCode",
                         "candidate model connection", "OpenAI-compatible format",
                         "awaiting account verification"):
        assert removed_plan not in english
    for removed_plan in ("前置智能体", "执行智能体", "委派任务", "等待账号验证"):
        assert removed_plan not in chinese


@pytest.mark.parametrize("language", ["en", "zh"])
def test_provider_diagram_is_responsive_accessible_and_makes_pilot_access_conditional(language):
    source = (ROOT / "docs/devin.html").read_text()
    page = Document(bilingual.project_html(source, language, TARGET))
    diagrams = [attrs for tag, attrs in page.tags if tag == "svg"]
    assert len(diagrams) == 2
    assert {item["class"] for item in diagrams} == {"route-wide", "route-compact"}
    assert all(item["role"] == "img" and item["aria-label"] for item in diagrams)
    assert all("width" not in item and item.get("viewbox") for item in diagrams)
    assert "@media(max-width:620px)" in source
    assert "Devin CLI" in page.text
    for diagram in diagrams:
        assert "Anthropic" in diagram["aria-label"] and "Devin" in diagram["aria-label"]
        assert "offered after verification" in diagram["aria-label"] if language == "en" else (
            "自有节点验证后提供试点选择" in diagram["aria-label"])
        assert "pending" not in diagram["aria-label"]
    assert "https://server.codeium.com" not in page.text


def test_devin_guide_discloses_cost_and_commercial_limits_with_official_sources():
    source = (ROOT / "docs/devin.html").read_text()
    page = Document(bilingual.project_html(source, "en", TARGET))
    for phrase in ("US$20/month", "USD per month",
                   "Extra usage can cost more", "a lower price does not guarantee more work",
                   "written reseller or partner permission",
                   "policy prohibits resale and account sharing",
                   "The paid private-slot service uses each owner's own account on one node",
                   "The separate limited trial does not establish account-sharing or reseller permission",
                   "Reseller permission has not been obtained", "no Devin package is for sale"):
        assert phrase in page.text
    assert {
        "https://docs.devin.ai/cli",
        "https://docs.devin.ai/cli/models",
        "https://docs.devin.ai/admin/billing/self-serve",
        "https://devin.ai/pricing",
        "https://claude.com/pricing",
        "https://support.claude.com/en/articles/11049741-what-is-the-max-plan",
        "https://cognition.com/legal/platform-terms-of-service",
        "https://cognition.com/legal/acceptable-use-policy",
    } <= set(page.links)
    assert not any(tag == "form" for tag, _ in page.tags)
    assert not any("checkout" in url or "signup" in url for url in page.links)
    for unverified_claim in ("unlimited usage", "ban-proof"):
        assert unverified_claim not in page.text
    assert not any(attrs.get("data-claim") == "devin-4-6x" for _, attrs in page.tags)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_prices_keep_current_plans_and_fleet_fees_separate_from_other_metrics(language):
    source = (ROOT / "docs/devin.html").read_text()
    page = Document(bilingual.project_html(source, language, TARGET))
    comparisons = {attrs.get("data-comparison") for tag, attrs in page.tags if tag == "table"}
    assert {"plans", "allowance", "value", "api"} <= comparisons
    prices = {(attrs["data-plan"], float(attrs["data-monthly-usd"]))
              for _, attrs in page.tags if "data-plan" in attrs}
    assert {("Pro", 20), ("Max 5x", 100), ("Max 20x", 200), ("Max", 200)} <= prices
    if language == "en":
        for phrase in ("US$200/year prepaid", "about US$17/month", "Claude Max is monthly only",
                       "five-hour and weekly limits", "larger weekly allowance, no daily cap",
                       "tax",
                       "provider subscription + selected Fleet slot + extra usage and tax",
                       "billing period"):
            assert phrase in page.text
        assert "Fleet 槽位费用另计" not in page.text
    else:
        for phrase in ("预付每年 200 美元", "约每月 17 美元", "Claude Max 只按月订阅",
                       "五小时及每周额度", "无每日上限", "税费",
                       "提供商订阅 + 所选 Fleet 槽位 + 额外用量及税费",
                       "计费周期"):
            assert phrase in page.text
        assert "The Fleet slot costs extra" not in page.text


@pytest.mark.parametrize("language", ["en", "zh"])
def test_setup_guide_uses_real_controls_without_pretending_web_signin_is_available(language):
    page = Document(bilingual.project_html((ROOT / "docs/devin.html").read_text(), language, TARGET))
    prefix = "/zh" if language == "zh" else ""
    assert prefix + "/account/available" in page.links and prefix + "/account#gateway" in page.links
    assert not any(tag in {"form", "button", "input"} for tag, _ in page.tags)
    assert "claude --model opus --effort max" in page.codes
    assert prefix + "/docs/library/docs/public-gateway.html#keys" in page.links
    if language == "en":
        for phrase in ("The operator enables this pilot", "your own Devin subscription",
                       "Never send your credentials to the operator", "Connection settings",
                       "One-click web sign-in is not supported yet", "Devin runs on your slot",
                       "Model provider", "Devin · Claude agent", "select that one slot",
                       "Use the displayed Fleet connection settings", "approve local tools",
                       "Already Devin ready?", "after pilot enablement and slot verification"):
            assert phrase in page.text
        assert "Sign in to Devin" not in page.text
    else:
        for phrase in ("你自己的 Devin 订阅", "运营方先在槽位上启用试点", "请勿把登录凭据发给运营方",
                       "目前还不支持网页一键登录", "Devin 在你的槽位上运行", "连接设置", "模型服务",
                       "Devin · Claude 智能体", "只选这个槽位", "Fleet 连接配置",
                       "批准本地工具操作", "试点启用且槽位验证后"):
            assert phrase in page.text
        assert "点击登录 Devin" not in page.text


@pytest.mark.parametrize("language", ["en", "zh"])
def test_privacy_discloses_slot_copies_admin_access_and_undefined_retention(language):
    source = (ROOT / "docs/devin.html").read_text()
    page = Document(bilingual.project_html(source, language, TARGET))
    expected = (
        "Saved files and local Claude Code history stay on your computer",
        "File contents you send, selected context, replies and tool handoff records",
        "may also be stored on your slot by native Devin",
        "Machine administrators (root) can read those records",
        "may remain until the slot is cleared",
        "no retention period is guaranteed",
    ) if language == "en" else (
        "保存的文件和本地 Claude Code 历史仍在你的电脑上",
        "你发送的文件内容、选定上下文、回复及工具交接记录",
        "原生 Devin 保存在槽位上", "主机管理员（root）可以读取这些记录",
        "记录可能保留到槽位清理时", "不保证具体保留期限",
    )
    for disclosure in expected:
        assert disclosure in page.text
    for false_guarantee in ("nothing is stored", "immediately deleted", "zero retention",
                            "从不保存", "立即删除", "零保留"):
        assert false_guarantee not in page.text


def test_native_operator_runbook_preserves_separate_verifier_and_private_grant_trust():
    page = Document((ROOT / "docs/runbooks.html").read_text())
    assert "native-devin-operator-pilot" in page.ids
    assert "#native-devin-operator-pilot" in page.links
    for phrase in (
        "public Fleet endpoint checks must be recorded separately",
        "account page offers Devin only after the holder's own slot is verified",
        "max_tokens is passed as context, not an enforced output or spending cap",
        "CCFLEET_GATEWAY_DEVIN_ENABLED", "metadata.json", "template.json", "verifier_user",
        "different unprivileged UID", "same-UID stdout cannot attest",
        "auth status", "models list --format json", "original slot bytes",
        "credential hash detects changes; it is never account identity",
        "eight-field public report", "240 seconds", "300 seconds",
        "ccfleet-devin-gateway-v1", "devin_relay.py --gateway-protocol-v1",
        "actual owned MCP", "no provider fallback or automatic replay",
        "no Devin package is for sale",
    ):
        assert phrase in page.text
    assert not page.duplicate_ids
    assert "https://server.codeium.com" not in page.text
