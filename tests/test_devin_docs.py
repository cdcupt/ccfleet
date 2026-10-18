"""The Devin evaluation is discoverable without promising availability or resale."""

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
    assert {"availability", "why", "cost", "commercial"} <= page.ids
    assert f"/en{TARGET}" in page.links and f"/zh{TARGET}" in page.links
    index = bilingual.project_html((ROOT / "docs/index.html").read_text(), language,
                                   "/docs/library/docs/index.html")
    index_target = TARGET if language == "en" else "/zh" + TARGET
    assert index_target in Document(index).links
    if language == "en":
        assert "Why we are evaluating Devin" in page.text
        assert "试点准备中" not in page.text
    else:
        assert "为什么评估 Devin" in page.text
        assert "Pilot preparation" not in page.text


def test_devin_guide_distinguishes_planned_agent_tasks_from_current_model_gateway():
    source = (ROOT / "docs/devin.html").read_text()
    english = Document(bilingual.project_html(source, "en", TARGET)).text
    chinese = Document(bilingual.project_html(source, "zh", TARGET)).text
    for phrase in (
        "awaiting account verification",
        "not yet available through CC Fleet",
        "not completed an authenticated Claude-model task",
        "different application from Claude Code",
        "gateway does not accept Devin credentials",
        "Fleet front agent",
        "proposed extension",
        "does not yet delegate tasks",
    ):
        assert phrase in english
    for phrase in ("等待账号验证", "尚未开放 Devin", "与 Claude Code 是不同的应用",
                   "不接受 Devin 凭证", "Fleet 前置智能体", "尚不能将任务委派"):
        assert phrase in chinese


def test_devin_guide_discloses_cost_and_commercial_limits_with_official_sources():
    source = (ROOT / "docs/devin.html").read_text()
    page = Document(bilingual.project_html(source, "en", TARGET))
    for phrase in ("US$20/month", "US$200/month", "usage can add to the base fee",
                   "single-user plans", "lower-cost choice", "each customer buys and uses",
                   "written reseller or partner permission", "prohibits selling or reselling",
                   "sharing accounts or credentials", "no Devin package for sale"):
        assert phrase in page.text
    assert {
        "https://docs.devin.ai/cli",
        "https://docs.devin.ai/cli/models",
        "https://docs.devin.ai/admin/billing/self-serve",
        "https://devin.ai/pricing",
        "https://docs.devin.ai/cloud/outposts/overview",
        "https://cognition.com/legal/acceptable-use-policy",
        "https://cognition.com/legal/platform-terms-of-service",
    } <= set(page.links)
    assert not any(tag == "form" for tag, _ in page.tags)
    assert not any("checkout" in url or "signup" in url for url in page.links)
    for unverified_claim in ("4–6x", "4-6x", "unlimited usage", "ban-proof"):
        assert unverified_claim not in page.text
