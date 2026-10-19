"""Dated provider comparisons keep price, allowance value and measured time distinct."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ccfleetd import bilingual, reference_docs
from tests.test_html_document_preservation import VOID, Document

ROOT = Path(__file__).resolve().parents[1]
ROUTE = "/docs/library/docs/devin.html"
AA = "https://artificialanalysis.ai/agents/coding-agents/comparisons/"
AA_METHOD = "https://artificialanalysis.ai/methodology/coding-agents-benchmarking"
STUDY = "https://newsletter.semianalysis.com/p/anthropic-subscriptions-offer-5x"
SECONDARY = "https://www.zhihu.com/question/2090772030799061671/answer/2090892647946785906"


@dataclass
class Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    @property
    def text(self):
        return " ".join(" ".join(child.text if isinstance(child, Node) else child
                                 for child in self.children).split())


class Tree(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        child = Node(tag, dict(attrs))
        self.stack[-1].children.append(child)
        if tag not in VOID:
            self.stack.append(child)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


@pytest.fixture(params=["en", "zh"])
def rendered(request):
    source = (ROOT / "docs/devin.html").read_text()
    language = request.param
    projected = bilingual.project_html(source, language, ROUTE)
    return language, source, Document(projected), Tree(projected).root


def by_id(root, identifier):
    found = [node for node in root.nodes() if node.attrs.get("id") == identifier]
    assert len(found) == 1, identifier
    return found[0]


def table(root, comparison):
    found = [node for node in root.nodes()
             if node.tag == "table" and node.attrs.get("data-comparison") == comparison]
    assert len(found) == 1, comparison
    return found[0]


def rows(root):
    return [node for node in root.nodes() if node.tag == "tr" and "data-provider" in node.attrs]


def number(node, attribute):
    raw = node.attrs[attribute]
    assert re.fullmatch(r"[0-9]+(?:[.][0-9]+)?", raw), (attribute, raw)
    value = float(raw)
    assert math.isfinite(value)
    return value


def links(root):
    return {node.attrs["href"] for node in root.nodes()
            if node.tag == "a" and "href" in node.attrs}


def test_existing_public_prices_anchor_and_language_projection_are_preserved(rendered):
    language, source, page, root = rendered
    assert "docs/devin.html" in reference_docs.PUBLIC_FILES
    assert bilingual.route_target(f"/{language}{ROUTE}") == (ROUTE, language, True)
    assert not page.duplicate_ids and by_id(root, "prices").tag in {"h2", "h3", "section"}
    assert {"prices", "cost", "speed", "allowance-value", "api-prices", "connect"} <= page.ids
    assert bilingual.CSS in source and bilingual.SCRIPT_TAG in source
    assert ("订阅" in page.text) if language == "zh" else ("subscription" in page.text.lower())
    if language == "en":
        assert "每月" not in page.text
    else:
        assert "Compare subscription prices" not in page.text


def test_requested_subscription_api_and_other_provider_categories_remain_distinct(rendered):
    _, _, _, root = rendered
    plans = {row.attrs["data-provider"] for row in rows(table(root, "plans"))}
    api = {row.attrs["data-provider"] for row in rows(table(root, "api"))}
    other = {row.attrs["data-provider"] for row in rows(table(root, "other-plans"))}
    assert {"claude", "openai", "devin"} <= plans
    assert "openai" in api and api.intersection({"claude", "anthropic"})
    assert {"minimax", "google", "glm", "kimi"} <= plans | other
    assert len(other) >= 3


# Frozen publisher observations, not an expectation generated from the HTML.
# Times are the rounded active-agent attempt times retrieved on 2026-10-18 PDT.
SPEED = {
    ("claude", 66.0): (66, ("Opus", "5.5", "max"), {
        AA + "claude-code-vs-codex", AA + "claude-code-vs-devin-fusion-cli"}),
    ("openai", 15.5): (63, ("6.1", "Sol", "xhigh"), {AA + "claude-code-vs-codex"}),
    ("openai", 29.4): (62, ("Astra", "max"), {AA + "claude-code-vs-codex"}),
    ("google", 34.5): (64, ("Antigravity", "Gemini", "Argon"), {AA + "antigravity-cli-vs-codex"}),
    ("glm", 48.1): (54, ("Opencode", "GLM-5.3"), {AA + "claude-code-vs-opencode"}),
    ("devin", 54.8): (68, ("Fusion", "Opus", "Luna"), {AA + "claude-code-vs-devin-fusion-cli"}),
    ("kimi", 60.0): (52, ("Kimi", "K3"), {AA + "codex-vs-kimi-code-cli"}),
}


def test_agent_timing_indices_recompute_from_the_specific_66_minute_baseline(rendered):
    _, _, _, root = rendered
    compared = table(root, "speed")
    baseline = number(compared, "data-baseline-minutes")
    assert baseline == 66
    assert compared.attrs["data-checked"] in {"2026-10-18", "2026-10-19"}
    observed = set()
    for row in rows(compared):
        minutes = number(row, "data-minutes")
        identity = row.attrs["data-provider"], minutes
        assert identity in SPEED and identity not in observed
        observed.add(identity)
        score, configuration, sources = SPEED[identity]
        assert number(row, "data-score") == score
        assert number(row, "data-speed") == pytest.approx(round(baseline / minutes, 1))
        assert row.attrs["data-source"] in sources
        assert row.attrs.get("data-config")
        assert row.attrs["data-source"] in links(row)
        for component in configuration:
            assert component.casefold() in row.text.casefold()
        # The index must be visible and attached to its configuration/quality.
        assert re.search(r"(?<![0-9.])" + re.escape(f"{round(baseline / minutes, 1):.1f}")
                         + r"\s*[×x]", row.text)
    assert observed == set(SPEED)


def test_timing_method_covers_quality_attempts_failures_rounding_and_exclusions(rendered):
    language, _, _, root = rendered
    section = by_id(root, "speed")
    assert AA_METHOD in links(section)
    concepts = (("attempt",), ("quality", "coding index"), ("fail",), ("missing",),
                ("round",), ("environment",), ("verifier", "judge")) if language == "en" else (
                    ("尝试",), ("质量", "编程指数"), ("失败",), ("缺失",),
                    ("舍入", "四舍五入"), ("环境",), ("验证", "裁判"))
    text = section.text.casefold()
    for alternatives in concepts:
        assert any(concept in text for concept in alternatives), alternatives
    assert ("active" in text) if language == "en" else (
        any(term in text for term in ("活动", "活跃", "工作时间", "进程运行")))


def test_devin_four_to_six_hypothesis_is_unverified_and_outside_ranked_tables(rendered):
    language, _, _, root = rendered
    claims = [node for node in root.nodes() if node.attrs.get("data-claim") == "devin-4-6x"]
    assert len(claims) == 1 and claims[0].attrs.get("data-status") == "unverified"
    claim = claims[0]
    assert "Devin" in claim.text and re.search(r"4\s*[–—-]\s*6", claim.text)
    assert (re.search(r"unverified|unmeasured|not measured", claim.text, re.I)
            if language == "en" else re.search(r"未验证|未测量|未经.*验证|尚未实测|未证实", claim.text))
    assert not any(claim is node for kind in ("speed", "value", "api", "plans", "other-plans")
                   for node in table(root, kind).nodes())
    assert "data-speed" not in claim.attrs and "data-value-index" not in claim.attrs


def test_dated_api_value_per_dollar_is_not_a_speed_or_generic_claude_plan_index(rendered):
    language, _, _, root = rendered
    compared = table(root, "value")
    assert compared.attrs["data-baseline-plan"] == "claude-max-20x"
    assert number(compared, "data-baseline-monthly-usd") == 200
    assert number(compared, "data-baseline-api-value-usd") == 11726
    assert compared.attrs["data-study-date"] == "2026-10-05"
    baseline = 11726 / 200
    baseline_rows = []
    codex_values = {}
    for row in rows(compared):
        if row.attrs["data-provider"] == "devin":
            assert row.attrs.get("data-status") == "unknown"
            assert "data-value-index" not in row.attrs and "data-api-value-usd" not in row.attrs
            continue
        fee, value = number(row, "data-monthly-usd"), number(row, "data-api-value-usd")
        assert row.attrs["data-source"] == SECONDARY
        assert fee > 0 and value > 0
        assert number(row, "data-value-index") == pytest.approx(round((value / fee) / baseline, 2))
        assert "data-speed" not in row.attrs
        if row.attrs["data-provider"] == "claude":
            baseline_rows.append(row)
            assert fee == 200 and value == 11726
            assert re.search(r"20\s*[×x倍]", row.text)
        elif row.attrs["data-provider"] == "openai":
            assert fee not in codex_values
            codex_values[fee] = value
    assert len(baseline_rows) == 1
    assert codex_values == {200: 2084, 500: 5386}
    section = by_id(root, "allowance-value")
    assert {STUDY, SECONDARY} <= links(section)
    assert "96.6" in section.text
    assert ("cache" in section.text.casefold()) if language == "en" else ("缓存" in section.text)
    assert re.search(r"\bnot\b.{0,40}\bspeed\b|\bno\b.{0,20}\bspeed\b|\bneither\b.{0,50}\bspeed\b",
                     section.text.casefold()) if language == "en" else (
        re.search(r"不.{0,30}速度", section.text))


def test_minimax_current_monthly_m31_prices_are_distinct_from_the_historical_m3_study(rendered):
    language, _, _, root = rendered
    current = [row for kind in ("plans", "other-plans") for row in rows(table(root, kind))
               if row.attrs["data-provider"] == "minimax"]
    assert len(current) == 1 and current[0].attrs["data-model"] == "M3.1 Flash Preview"
    plans = {node.attrs["data-plan"]: number(node, "data-monthly-usd")
             for node in current[0].nodes() if "data-plan" in node.attrs}
    assert plans == {"Go": 22, "Explore": 55, "Build": 132}
    historical = [row for row in rows(table(root, "value"))
                  if row.attrs["data-provider"] == "minimax"]
    assert len(historical) == 1 and historical[0].attrs["data-model"] == "M3"
    assert historical[0].attrs["data-snapshot"] == "2026-10-05"
    assert number(historical[0], "data-monthly-usd") == 132
    assert "Ultra" in historical[0].text
    context = by_id(root, "allowance-value").text
    assert ("histor" in context.lower()) if language == "en" else (
        "历史" in context or "旧" in context)
    assert "M3.1" in context


def test_codex_plan_prices_and_api_token_units_do_not_become_subscription_api_credit_claims(rendered):
    language, _, _, root = rendered
    codex = [row for row in rows(table(root, "plans")) if row.attrs["data-provider"] == "openai"]
    assert len(codex) == 1
    plans = [number(node, "data-monthly-usd")
             for node in codex[0].nodes() if "data-plan" in node.attrs]
    assert sorted(plans) == [20, 100, 200, 500]
    assert "https://learn.chatgpt.com/docs/pricing" in links(root)
    text = codex[0].text.casefold()
    for claim in re.finditer(
            r"(?:includes?|included|包含|赠送)\s*(?:openai\s+)?api\s*(?:credits|额度)", text):
        assert re.search(r"\b(?:not|no|without)\b|不|未", text[max(0, claim.start() - 30):claim.start()])
    api = table(root, "api")
    assert api.attrs["data-unit"] == "usd-per-million-tokens"
    assert not any("data-monthly-usd" in node.attrs for node in api.nodes())
    assert re.search(r"1\s*M|million|1,000,000", api.text, re.I) if language == "en" else (
        "百万" in api.text or re.search(r"1\s*M", api.text))
    assert {"https://developers.openai.com/api/docs/models/gpt-6.1-sol",
            "https://platform.claude.com/docs/en/about-claude/pricing"} <= links(root)


def test_comparison_tables_and_sources_are_accessible_and_no_secrets_or_ips_are_published(rendered):
    _, source, page, root = rendered
    assert not re.search(r"(?<![0-9])(?:[0-9]{1,3}[.]){3}[0-9]{1,3}(?![0-9])", source)
    assert not re.search(r"ccf_api_[A-Za-z0-9_-]{43}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}", source)
    for kind in ("plans", "speed", "value", "api", "other-plans"):
        compared = table(root, kind)
        assert any(node.tag == "caption" and node.text for node in compared.nodes()), kind
        headers = [node for node in compared.nodes() if node.tag == "th"]
        assert headers and all(node.attrs.get("scope") in {"row", "col"} and node.text
                               for node in headers), kind
        for anchor in (node for node in compared.nodes() if node.tag == "a"):
            assert anchor.text or anchor.attrs.get("aria-label")
            parsed = urlsplit(anchor.attrs["href"])
            if parsed.scheme or parsed.netloc:
                assert parsed.scheme == "https" and parsed.netloc
                assert parsed.username is None and parsed.password is None
            elif not parsed.path and parsed.fragment:
                assert parsed.fragment in page.ids
    assert not any(tag in {"form", "input"} for tag, _ in page.tags)
