"""Current rental guidance matches the offer policy in both language views."""

from pathlib import Path

import pytest

from ccfleetd import bilingual, customer_docs, pricing
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document


@pytest.mark.parametrize("page", [customer_docs.guide, customer_docs.terms])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_offer_guidance_replaces_the_legacy_global_rate_in_each_language(page, language):
    source = page(Config(), price=pricing.Price("987.65", "EUR"), per_slot_pricing=True)
    document = Document(bilingual.project_html(source, language, "/docs/guide"))
    assert "987.65" not in document.text and "EUR" not in document.text
    assert "rental-pricing" in document.ids and not document.duplicate_ids
    for phrase in (("monthly USD rate", "365/360", "divided by 12", "10%", "half up",
                    "$2 minimum", "round the normalized USD resource cost", "Alpha",
                    "does not charge automatically", "renew automatically", "rewrite past payments",
                    "temporarily offline", "existing holder", "Anthropic API usage")
                   if language == "en" else
                   ("美元月费", "365/360", "除以 12", "10%", "四舍五入到美分", "最低 $2",
                    "Alpha", "不会自动扣款", "自动续费", "不会改写过去的付款记录", "暂时离线",
                    "已有持有者", "Anthropic API 用量")):
        assert phrase in document.text


@pytest.mark.parametrize("page", [customer_docs.overview, customer_docs.guide, customer_docs.terms])
def test_costless_legacy_selling_price_is_not_a_resource_quote(page):
    source = page(Config(), price=pricing.Price("987.65", "EUR"))
    assert "987.65" not in Document(source).text
    assert "Pricing is unavailable" in Document(source).text
    assert "rental-pricing" not in Document(source).ids


@pytest.mark.parametrize("language", ["en", "zh"])
def test_explanation_has_one_complete_pricing_section_in_selected_language(language):
    source = customer_docs.how_it_works(Config())
    page = Document(bilingual.project_html(source, language, "/docs/how-it-works"))
    assert not page.duplicate_ids and "rental-pricing" in page.ids
    assert "365/360" in page.text and "10%" in page.text
    assert ("payment history" if language == "en" else "付款历史") in page.text
    assert ("network types" if language == "en" else "网络类型") in page.text


@pytest.mark.parametrize("relative", ["README.html", "docs/runbooks.html", "docs/index.html",
                                      "docs/public-gateway.html", "docs/guidebook.html"])
def test_canonical_rental_references_keep_normalization_and_retirement_distinct(relative):
    root = Path(__file__).parents[1]
    text = Document((root / relative).read_text()).text
    for phrase in ("USD", "365/360", "10%", "half up", "Retired", "Status"):
        assert phrase in text
    assert "history" in text
    for phrase in ("Alpha", "$2", "service fee", "bundl", "USD", "unavailable",
                   "agreed price", "renewal"):
        assert phrase in text
    for obsolete in ("fixed 10% markup", "Residential is premium", "standard network category",
                     "round the final total once", "Raw supplier costs remain operator-only"):
        assert obsolete not in text


@pytest.mark.parametrize("language", ["en", "zh"])
def test_alpha_policy_explains_costs_and_limits_without_quality_ranking(language):
    text = Document(bilingual.project_html(customer_docs.guide(Config(), per_slot_pricing=True),
                                          language, "/docs/guide")).text
    phrases = (("VPS cost", "exit-IP cost", "service fee and total", "recorded bundled",
                "Missing costs", "not converted to USD", "no separate residential premium",
                "still being tested", "not launched commercially", "one residential slot",
                "multiple users", "introductory", "updated quote before renewal",
                "already agreed billing period retains its agreed price",
                "$8.68", "$10.68", "$10.83", "$12.83", "$22.28", "$24.51", "$27.44", "$30.18",
                "through $20", "normalization rounding")
               if language == "en" else
               ("VPS 成本", "出口 IP 成本", "服务费与总价", "打包资源成本", "成本缺失",
                "转换为美元", "不另加住宅溢价", "仍在测试", "尚未正式商业发布", "一个住宅槽位",
                "多用户", "初期价格", "续期前会提供更新报价", "已约定计费周期仍执行原约定价格",
                "$8.68", "$10.68", "$10.83", "$12.83", "$22.28", "$24.51", "$27.44", "$30.18",
                "不超过 $20", "折算舍入"))
    for phrase in phrases:
        assert phrase in text
    assert "fixed 10% markup" not in text and "高级类别" not in text


@pytest.mark.parametrize("language", ["en", "zh"])
def test_home_identity_identifies_alpha_without_an_end_date(language):
    page = Document(bilingual.project_html(customer_docs.overview(Config(), per_slot_pricing=True),
                                          language, "/"))
    assert ("Alpha — Early Access" if language == "en" else "Alpha — 早期体验") in page.text
    assert ("dedicated VPS" if language == "en" else "专属 VPS") in page.text
    assert ("dedicated exit IP" if language == "en" else "专属出口 IP") in page.text
    assert "fixed 10% markup" not in page.text
