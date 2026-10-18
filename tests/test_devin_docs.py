"""Native pilot evidence, local tool execution and pending public activation stay distinct."""

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
    assert {"availability", "why", "route", "compatibility", "cost", "commercial"} <= page.ids
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
        "private four-step fixture",
        "seven finalized native provider requests",
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
                   "由你的电脑执行", "私有四步测试", "Opus 5.5 Max", "原生智能体兼容模式",
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
    for phrase in ("US$20/month", "US$200/month", "single-user plans with usage quotas",
                   "extra usage can cost more", "not a billing guarantee",
                   "Claude subscription may be cheaper", "written reseller or partner permission",
                   "policy prohibits resale and account sharing", "no operator account pool",
                   "Resale approval is pending", "no Devin package is for sale"):
        assert phrase in page.text
    assert {
        "https://docs.devin.ai/cli",
        "https://docs.devin.ai/cli/models",
        "https://docs.devin.ai/admin/billing/self-serve",
        "https://cognition.com/legal/acceptable-use-policy",
    } <= set(page.links)
    assert not any(tag == "form" for tag, _ in page.tags)
    assert not any("checkout" in url or "signup" in url for url in page.links)
    for unverified_claim in ("4–6x", "4-6x", "unlimited usage", "ban-proof"):
        assert unverified_claim not in page.text
    assert len(page.text.split()) < 560


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
