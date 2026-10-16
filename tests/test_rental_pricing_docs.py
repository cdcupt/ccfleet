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
                    "does not charge automatically", "rewrite past payments",
                    "temporarily offline", "existing holder", "Anthropic")
                   if language == "en" else
                   ("美元月费", "365/360", "除以 12", "10%", "四舍五入一次",
                    "不会自动扣款", "不会改写过去的付款记录", "暂时离线", "已有持有者",
                    "Anthropic")):
        assert phrase in document.text


def test_legacy_no_offer_documentation_keeps_its_explicit_display_rate():
    source = customer_docs.guide(Config(), price=pricing.Price("987.65", "EUR"))
    assert "987.65" in Document(source).text
    assert "rental-pricing" not in Document(source).ids


@pytest.mark.parametrize("language", ["en", "zh"])
def test_explanation_has_one_complete_pricing_section_in_selected_language(language):
    source = customer_docs.how_it_works(Config())
    page = Document(bilingual.project_html(source, language, "/docs/how-it-works"))
    assert not page.duplicate_ids and "rental-pricing" in page.ids
    assert "365/360" in page.text and "10%" in page.text
    assert ("payment history" if language == "en" else "付款历史") in page.text
    assert ("network category" if language == "en" else "网络类别") in page.text


@pytest.mark.parametrize("relative", ["README.html", "docs/runbooks.html",
                                      "docs/public-gateway.html", "docs/guidebook.html"])
def test_canonical_rental_references_keep_normalization_and_retirement_distinct(relative):
    root = Path(__file__).parents[1]
    text = Document((root / relative).read_text()).text
    for phrase in ("USD", "365/360", "10%", "half up", "Retired", "Status"):
        assert phrase in text
    assert "history" in text
