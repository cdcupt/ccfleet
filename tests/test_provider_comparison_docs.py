"""Subscription cost comparisons preserve denominators, evidence and workload scope."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ccfleetd import bilingual, reference_docs
from tests.test_html_document_preservation import VOID, Document

ROOT = Path(__file__).resolve().parents[1]
ROUTE = "/docs/library/docs/devin.html"
PIN = "0be92b7ce3edf7690672ce5217607032a4bc7c45"
PRIMARY = f"https://github.com/Fei2411/real-api-pricing/blob/{PIN}/"
EVIDENCE = ROOT / "docs/provider-allowance-evidence.json"
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
    return Decimal(raw)


def links(root):
    return {node.attrs["href"] for node in root.nodes()
            if node.tag == "a" and "href" in node.attrs}


def test_existing_public_prices_anchor_and_language_projection_are_preserved(rendered):
    language, source, page, root = rendered
    assert "docs/devin.html" in reference_docs.PUBLIC_FILES
    assert bilingual.route_target(f"/{language}{ROUTE}") == (ROUTE, language, True)
    assert not page.duplicate_ids and by_id(root, "prices").tag in {"h2", "h3", "section"}
    assert {"prices", "cost", "allowance-value", "api-prices", "connect"} <= page.ids
    assert bilingual.CSS in source and bilingual.SCRIPT_TAG in source
    assert ("订阅" in page.text) if language == "zh" else ("subscription" in page.text.lower())
    if language == "en":
        assert "每月" not in page.text
    else:
        assert "Compare subscription prices" not in page.text


def test_cost_comparison_is_limited_to_requested_claude_codex_and_devin(rendered):
    _, _, _, root = rendered
    cost = by_id(root, "cost")
    plans = rows(table(root, "plans"))
    assert len(plans) == 3
    assert {row.attrs["data-provider"] for row in plans} == {"claude", "openai", "devin"}
    api = rows(table(root, "api"))
    assert len(api) == 4
    assert {row.attrs["data-provider"] for row in api} == {"claude", "openai"}
    assert not any(node.attrs.get("data-comparison") == "other-plans" for node in cost.nodes())
    assert not re.search(r"Google|Gemini|MiniMax|Kimi|GLM|Z[.]AI|Cursor|Copilot", cost.text, re.I)
    assert {node.attrs["data-provider"] for node in cost.nodes()
            if "data-provider" in node.attrs} == {"claude", "openai", "devin"}


# Independently pinned observations prevent a changed page and fixture from agreeing
# on the same wrong plan, measurement dates, or denominator.
MATCHED = {
    "claude": (31538000000, "2026-09-22", "2026-09-27",
               "claude-opus55-max20x-round2-2026-09-30.json"),
    "devin": (6690000000, "2026-09-23", "2026-09-23",
              "devin-opus55-round1-2026-09-23.json"),
}


def test_public_evidence_fixture_preserves_pinned_inputs_and_reproducible_blended_price():
    evidence = json.loads(EVIDENCE.read_text(), parse_float=Decimal)
    assert evidence["schema_version"] == 2
    assert evidence["source_revision"] == PIN
    assert evidence["source_csv"] == PRIMARY + "data/adopted.csv"
    assert evidence["methodology"] == PRIMARY + "CONVENTIONS.md"
    assert evidence["model"] == "claude-opus-5.5"
    assert evidence["period_weeks"] == 4
    assert evidence["baseline_plan"] == "claude-max-20x"
    shares = evidence["workload_share"]
    assert shares == {"cache_read": Decimal(".97"), "cache_write_5m": Decimal(".025"),
                      "output": Decimal(".005")}
    assert sum(shares.values()) == 1
    rates = evidence["api_usd_per_million"]
    assert rates == {"cache_read": Decimal(".2"), "cache_write_5m": 5, "output": 20}
    blended = sum(shares[k] * rates[k] for k in shares)
    assert blended == evidence["api_blended_usd_per_million"] == Decimal(".419")
    assert {row["provider"] for row in evidence["rows"]} == set(MATCHED)
    for row in evidence["rows"]:
        tokens, start, end, source = MATCHED[row["provider"]]
        assert (row["tokens"], row["sample_start"], row["sample_end"]) == (tokens, start, end)
        assert row["monthly_usd"] == 200 and row["confidence"] == "medium"
        assert row["source"] == PRIMARY + "data/research/" + source
    devin = next(row for row in evidence["rows"] if row["provider"] == "devin")
    raw = devin["raw_sample"]
    assert raw == {"quota_points": 48, "input": 2672, "output": 1917552,
                   "cache_read": 483137835, "cache_write_5m": 40275697}
    raw_value = (raw["input"] * 4 + raw["output"] * 20
                 + raw["cache_read"] * Decimal(".2") + raw["cache_write_5m"] * 5)
    four_week_value = raw_value / 1000000 / Decimal(".48") * 4
    assert four_week_value.quantize(Decimal(".01")) == devin["source_monthly_api_value_usd"]
    # Adopted token counts are rounded, so their repriced value must not pretend
    # to be exactly the raw-sample value.
    adopted_value = Decimal(devin["tokens"]) / 1000000 * blended
    assert adopted_value == Decimal("2803.11")
    assert Decimal(0) < adopted_value - four_week_value < Decimal(".1")


ROUTES = [
    ("claude-sub", "claude", "subscription", "Claude Opus 5.5", "13214.422", "four-weeks",
     PRIMARY + "data/research/claude-opus55-max20x-round2-2026-09-30.json"),
    ("devin-sub", "devin", "subscription", "Claude Opus 5.5", "2803.110", "four-weeks",
     PRIMARY + "data/research/devin-opus55-round1-2026-09-23.json"),
    ("codex-sub", "openai", "subscription", "GPT-6.1 Sol", "2084", "reported-month", SECONDARY),
    ("claude-api", "claude", "api", "Claude Opus 5.5", "200", "pay-as-you-go",
     "https://platform.claude.com/docs/en/about-claude/pricing"),
    ("codex-api", "openai", "api", "GPT-6.1 Sol", "200", "pay-as-you-go",
     "https://developers.openai.com/api/docs/models/gpt-6.1-sol"),
]


ASSUMPTIONS = {
    "claude-sub": "community-four-weeks-full-use-97pct-cache",
    "devin-sub": "community-four-weeks-full-use-97pct-cache",
    "codex-sub": "reported-month-full-use-96.6pct-cache-new-rate-cohort",
    "claude-api": "standard-list-budget-no-discounts-credits-tax-tools-storage",
    "codex-api": "standard-list-budget-no-discounts-credits-tax-tools-storage",
}


def test_normalization_fixture_keeps_route_sources_periods_and_dollar_cost_formula():
    evidence = json.loads(EVIDENCE.read_text(), parse_float=Decimal)
    normalization = evidence["normalization"]
    assert normalization["unit"] == "relative-api-equivalent-cost"
    assert normalization["method"] == "cross-source-api-list-value"
    assert normalization["budget_usd"] == 200
    assert normalization["baseline_plan"] == "claude-max-20x"
    assert normalization["baseline_api_value_usd"] == Decimal("13214.422")
    assert normalization["formula"] == (
        "(budget / route_api_value) / (budget / baseline_api_value)")
    budget = Decimal(normalization["budget_usd"])
    baseline_unit_cost = budget / normalization["baseline_api_value_usd"]
    assert len(normalization["rows"]) == 5
    for row, (route, provider, channel, model, value, period, source) in zip(
            normalization["rows"], ROUTES):
        assert (row["route"], row["provider"], row["channel"], row["model"],
                row["source_period"], row["source"]) == (
                    route, provider, channel, model, period, source)
        assert row["api_value_usd"] == Decimal(value)
        assert row["assumption"] == ASSUMPTIONS[route]
        computed = ((budget / row["api_value_usd"]) / baseline_unit_cost).quantize(Decimal(".1"))
        assert row["relative_cost"] == computed
    scope = " ".join(normalization["scope"]).casefold()
    for concept in ("cross-source", "not a new matched experiment", "four-week", "reported-month",
                    "97%", "96.6%", "standard", "discount", "credits", "tax", "tools",
                    "equal raw token", "quality", "completed work"):
        assert concept in scope


def test_five_routes_recompute_cost_from_one_explicit_api_value_denominator(rendered):
    _, _, _, root = rendered
    compared = table(root, "allowance")
    assert compared.attrs["data-unit"] == "relative-api-equivalent-cost"
    assert compared.attrs["data-normalization"] == "cross-source-api-list-value"
    assert compared.attrs["data-baseline-plan"] == "claude-max-20x"
    assert number(compared, "data-baseline-api-value-usd") == Decimal("13214.422")
    budget = number(compared, "data-budget-usd")
    assert budget == 200
    assert "data-model" not in compared.attrs and "data-period-weeks" not in compared.attrs
    actual = rows(compared)
    assert len(actual) == 5
    assert [row.attrs["data-route"] for row in actual] == [route[0] for route in ROUTES]
    baseline_unit_cost = budget / Decimal("13214.422")
    for row, (route, provider, channel, model, value, period, source) in zip(actual, ROUTES):
        assert row.attrs["data-route"] == route
        assert row.attrs["data-provider"] == provider and row.attrs["data-channel"] == channel
        assert row.attrs["data-model"] == model and model in row.text
        assert row.attrs["data-source-period"] == period
        assert row.attrs["data-assumption"] == ASSUMPTIONS[route]
        assert number(row, "data-budget-usd") == budget
        api_value = number(row, "data-api-value-usd")
        assert api_value == Decimal(value)
        cost_index = ((budget / api_value) / baseline_unit_cost).quantize(Decimal(".1"))
        assert number(row, "data-relative-cost") == cost_index
        assert row.attrs["data-source"] == source and source in links(row)
        visible_index = re.escape(f"{cost_index:.1f}") if cost_index != 1 else r"1(?:\.0)?"
        assert re.search(r"(?<![0-9.])" + visible_index + r"\s*[×x倍]", row.text)
        assert re.search(r"US\$200(?![0-9])", row.text)
        visible_value = re.escape(format(api_value.quantize(Decimal("1")), ","))
        assert re.search(r"US\$" + visible_value + r"(?:[.][0-9]+)?(?![0-9])", row.text)
    # Both direct APIs buy their own list-price value dollar for dollar. This
    # equality is a value denominator, never evidence that their token prices match.
    api_rows = [row for row in actual if row.attrs["data-channel"] == "api"]
    assert len(api_rows) == 2
    assert all(number(row, "data-api-value-usd") == budget
               and number(row, "data-relative-cost") == Decimal("66.1") for row in api_rows)
    assert not any(attribute in node.attrs for node in compared.nodes() for attribute in (
        "data-tokens", "data-relative-allowance", "data-effective-usd-per-mtok"))


def test_cross_source_periods_api_budget_and_model_limitations_are_explicit(rendered):
    language, _, _, root = rendered
    cost = by_id(root, "cost")
    text = cost.text.casefold()
    concepts = (("cross-source", "cross-study"), ("four-week", "four weeks"),
                ("reported month", "reported monthly", "reported-month", "source month"),
                ("pay-as-you-go",), ("same token", "identical token", "different raw token counts"),
                ("quality", "completed work"), ("discount",), ("credit",),
                ("tax",), ("tool",), ("new-rate", "new rate")) if language == "en" else (
                    ("跨来源", "跨研究"), ("四周", "4 周"), ("来源月", "报告月", "所报月", "来源报告的月度"),
                    ("按量",), ("相同 token", "相同的 token", "token 数相同", "token 数仍然不同"),
                    ("质量", "完成的工作"), ("折扣",), ("credits", "赠金", "抵扣"),
                    ("税",), ("工具",), ("新额度", "新档"))
    for alternatives in concepts:
        assert any(term in text for term in alternatives), alternatives
    assert "96.6%" in text and "97%" in text
    if language == "en":
        assert re.search(r"does not mean.{0,30}(?:equal|same|identical).{0,30}quality", text)
        assert re.search(r"not.{0,40}official fixed-token", text)
    else:
        assert re.search(r"不代表.{0,20}(?:相同|同样).{0,20}质量", text)
        assert re.search(r"不是官方.{0,20}token", text)
    assert "october 29" in text if language == "en" else "10 月 29 日" in text


def test_matched_estimate_scope_and_primary_method_are_visible_in_each_language(rendered):
    language, _, _, root = rendered
    cost = by_id(root, "cost")
    text = cost.text.casefold()
    assert {PRIMARY + "data/adopted.csv", PRIMARY + "CONVENTIONS.md"} <= links(cost)
    assert "97%" in text and "2.5%" in text and "0.5%" in text
    assert "0.419" in text and "2803.06" in text.replace(",", "")
    concepts = (("four-week", "four week", "4-week", "4 weeks"), ("cache",),
                ("full",), ("estimate", "estimated"), ("not an official", "not a guaranteed", "official fixed-token guarantee"),
                ("output",), ("pro",)) if language == "en" else (
                    ("四周", "4 周", "4周"), ("缓存",), ("用满", "全部用完", "用完"),
                    ("估算", "估计"), ("不是官方", "非官方", "不保证", "不代表官方"),
                    ("输出",), ("pro",))
    for alternatives in concepts:
        assert any(term in text for term in alternatives), alternatives


def test_unrequested_generation_speed_ranking_and_hypothesis_are_removed(rendered):
    _, source, _, root = rendered
    cost = by_id(root, "cost")
    assert not any(node.attrs.get("data-comparison") == "speed" or "data-speed" in node.attrs
                   for node in root.nodes())
    assert not any(node.attrs.get("data-claim") == "devin-4-6x" for node in root.nodes())
    assert "speed" not in {node.attrs.get("id") for node in root.nodes()}
    assert "artificialanalysis.ai" not in " ".join(links(cost))
    assert not re.search(r"Fast mode|Ultrafast|unverified|尚未证实", cost.text, re.I)
    assert "尚未证实" not in source


def test_dated_codex_api_value_reference_remains_separate_from_matched_token_cost(rendered):
    language, _, _, root = rendered
    compared = table(root, "value")
    assert compared.attrs["data-baseline-plan"] == "claude-max-20x"
    assert number(compared, "data-baseline-monthly-usd") == 200
    assert number(compared, "data-baseline-api-value-usd") == 11726
    assert compared.attrs["data-study-date"] == "2026-10-05"
    baseline = Decimal(11726) / 200
    observations = {}
    for row in rows(compared):
        provider = row.attrs["data-provider"]
        fee, value = number(row, "data-monthly-usd"), number(row, "data-api-value-usd")
        assert provider != "devin"
        identity = provider, fee
        assert identity not in observations
        observations[identity] = value
        assert row.attrs["data-source"] == SECONDARY
        assert number(row, "data-value-index") == ((value / fee) / baseline).quantize(Decimal(".01"))
        assert "data-relative-cost" not in row.attrs and "data-relative-allowance" not in row.attrs
    assert observations == {("claude", 200): 11726, ("openai", 500): 5386,
                            ("openai", 200): 2084}
    section = by_id(root, "allowance-value")
    assert {STUDY, SECONDARY} <= links(section)
    assert "96.6" in section.text
    assert ("cache" in section.text.casefold()) if language == "en" else ("缓存" in section.text)
    assert any(term in section.text.casefold() for term in (
        ("separate", "different workload") if language == "en" else ("分开", "不同", "另一项")))


def test_devin_rows_are_bold_and_tinted_without_changing_the_cost_direction(rendered):
    _, source, _, root = rendered
    for comparison in ("plans", "allowance"):
        selected = [row for row in rows(table(root, comparison))
                    if row.attrs["data-provider"] == "devin"]
        assert len(selected) == 1
        assert any(node.tag == "strong" and "Devin" in node.text for node in selected[0].nodes())
    # The highlight must be a fixed provider selector, not a script or an
    # attribute that falsely changes the numeric comparison.
    styles = re.findall(r"<style[^>]*>(.*?)</style>", source, flags=re.S)
    assert any(re.search(r"[^{}]*\[data-provider=[\"']?devin[\"']?\][^{}]*"
                         r"\{[^{}]*background(?:-color)?\s*:", style)
               for style in styles)
    row = next(row for row in rows(table(root, "allowance"))
               if row.attrs["data-provider"] == "devin")
    assert number(row, "data-relative-cost") == Decimal("4.7")


def test_current_monthly_plan_prices_are_not_replaced_by_historical_study_prices(rendered):
    _, _, _, root = rendered
    expected = {
        "claude": [("Pro", 20), ("Max 5x", 100), ("Max 20x", 200)],
        "openai": [("Plus", 20), ("Pro", 100), ("Pro", 200), ("Pro", 500)],
        "devin": [("Pro", 20), ("Max", 200)],
    }
    compared = table(root, "plans")
    assert compared.attrs["data-unit"] == "usd-per-month"
    actual = {}
    for row in rows(compared):
        provider = row.attrs["data-provider"]
        assert provider not in actual
        actual[provider] = [(node.attrs["data-plan"], number(node, "data-monthly-usd"))
                            for node in row.nodes() if "data-plan" in node.attrs]
    assert actual == expected


def test_public_api_rates_preserve_cache_and_output_denominators(rendered):
    language, _, _, root = rendered
    compared = table(root, "api")
    assert compared.attrs["data-unit"] == "usd-per-million-tokens"
    assert not any("data-monthly-usd" in node.attrs for node in compared.nodes())
    expected = {
        "Claude Opus 5.5": ("4", "5 / 8", "0.2", "20"),
        "Claude Sonnet 5.5": ("2", "2.50 / 4", "0.1", "10"),
        "GPT-6.1 Sol": ("2", "2.50", "0.1", "10"),
        "GPT-6 Astra": ("10", "12.50", "1", "50"),
    }
    actual = {}
    for row in rows(compared):
        model = row.attrs["data-model"]
        assert model not in actual
        actual[model] = tuple(node.text for node in row.children
                              if isinstance(node, Node) and node.tag == "td")
    assert actual == expected
    assert re.search(r"1\s*M|million|1,000,000", compared.text, re.I) if language == "en" else (
        "百万" in compared.text or re.search(r"1\s*M", compared.text))
    assert {"https://developers.openai.com/api/docs/models/gpt-6.1-sol",
            "https://platform.claude.com/docs/en/about-claude/pricing"} <= links(root)


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
    for kind in ("plans", "allowance", "value", "api"):
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
