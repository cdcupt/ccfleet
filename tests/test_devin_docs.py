"""Provider routing stays distinct from agent delegation and unverified availability."""

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
        assert "尚未在 Fleet 验证" not in page.text
    else:
        assert "为什么评估 Devin" in page.text
        assert "Not yet verified in Fleet" not in page.text


def test_devin_guide_keeps_claude_code_and_the_same_api_with_planned_website_provider_choice():
    source = (ROOT / "docs/devin.html").read_text()
    english = Document(bilingual.project_html(source, "en", TARGET)).text
    chinese = Document(bilingual.project_html(source, "zh", TARGET)).text
    for phrase in (
        "Not yet verified in Fleet",
        "currently supports Anthropic only",
        "no Devin provider selection is active",
        "official Claude Code",
        "model provider chosen on the Fleet website",
        "same Fleet API address",
        "choose Anthropic or Devin for model requests",
        "request interface, streaming replies, tool use, authentication and subscription billing",
    ):
        assert phrase in english
    for phrase in ("尚未在 Fleet 验证", "当前只支持 Anthropic", "尚未开放 Devin 提供商选择",
                   "官方 Claude Code", "在 Fleet 网站选择模型提供商", "同一个 Fleet API 地址",
                   "请求接口、流式回复、工具使用、认证，以及订阅如何计费"):
        assert phrase in chinese
    assert "does not prove Claude Code compatibility" in english
    assert "provides a candidate model connection" in english
    assert "translation between its Messages API and the integration’s OpenAI-compatible format" in english
    assert "Messages API 与该集成使用的 OpenAI 兼容格式之间做转换" in chinese
    for removed_plan in ("Fleet front agent", "execution agent", "MCP", "Outposts",
                         "coding tasks", "awaiting account verification"):
        assert removed_plan not in english
    for removed_plan in ("前置智能体", "执行智能体", "委派任务", "等待账号验证"):
        assert removed_plan not in chinese


@pytest.mark.parametrize("language", ["en", "zh"])
def test_provider_diagram_is_responsive_accessible_and_marks_the_devin_route_unavailable(language):
    source = (ROOT / "docs/devin.html").read_text()
    page = Document(bilingual.project_html(source, language, TARGET))
    diagrams = [attrs for tag, attrs in page.tags if tag == "svg"]
    assert len(diagrams) == 2
    assert {item["class"] for item in diagrams} == {"route-wide", "route-compact"}
    assert all(item["role"] == "img" and item["aria-label"] for item in diagrams)
    assert all("width" not in item and item.get("viewbox") for item in diagrams)
    assert "@media(max-width:620px)" in source
    assert "Desired design" in page.text if language == "en" else "目标设计" in page.text
    for diagram in diagrams:
        assert "Anthropic" in diagram["aria-label"] and "Devin" in diagram["aria-label"]
        assert "Only Anthropic is available today" in diagram["aria-label"] if language == "en" else (
            "目前只有 Anthropic 可用" in diagram["aria-label"])
    assert "https://server.codeium.com" not in page.text


def test_devin_guide_discloses_cost_and_commercial_limits_with_official_sources():
    source = (ROOT / "docs/devin.html").read_text()
    page = Document(bilingual.project_html(source, "en", TARGET))
    for phrase in ("US$20/month", "US$200/month", "single-user plans with usage quotas",
                   "extra usage can cost more", "does not prove those subscriptions would cover",
                   "Claude subscription may be cheaper", "written reseller or partner permission",
                   "policy prohibits resale and account sharing", "No Devin package is for sale"):
        assert phrase in page.text
    assert {
        "https://docs.devin.ai/cli",
        "https://docs.devin.ai/cli/models",
        "https://docs.devin.ai/api-reference/overview",
        "https://www.npmjs.com/package/@cognitionai/opencode-devin",
        "https://docs.devin.ai/admin/billing/self-serve",
        "https://code.claude.com/docs/en/llm-gateway-protocol",
        "https://cognition.com/legal/acceptable-use-policy",
    } <= set(page.links)
    assert not any(tag == "form" for tag, _ in page.tags)
    assert not any("checkout" in url or "signup" in url for url in page.links)
    for unverified_claim in ("4–6x", "4-6x", "unlimited usage", "ban-proof"):
        assert unverified_claim not in page.text
    assert len(page.text.split()) < 388
