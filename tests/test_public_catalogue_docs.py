"""Public catalogue guidance keeps discovery distinct from authenticated claims."""

from pathlib import Path

import pytest

from ccfleetd import bilingual, customer_docs
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("page", [customer_docs.guide, customer_docs.how_it_works])
def test_docs_explain_full_public_catalogue_and_existing_private_claim_flow(page, language):
    options = {"per_slot_pricing": True} if page is customer_docs.guide else {}
    source = page(Config(), **options)
    document = Document(bilingual.project_html(source, language, "/docs/guide"))
    assert any(link.endswith("/slots") for link in document.links)
    assert any(link.endswith("/account/available") for link in document.links)
    phrases = (("up to three real configuration examples", "full public slot catalogue",
                "Alpha price breakdowns", "host/IP-provider", "Machine 7", "filter by provider",
                "up to 20 machines", "purple Residential", "blue ISP", "gray Datacenter", "do not rank quality",
                "does not reserve a slot", "Browse available slots", "within your allowance",
                "$2 minimum", "does not charge automatically")
               if language == "en" else
               ("最多三个实际配置示例", "完整公开槽位目录", "Alpha 报价明细", "主机／IP 服务商",
                "主机 7", "按服务商与 IP 类型筛选", "最多展示 20 台主机", "紫色住宅",
                "蓝色 ISP", "灰色机房", "不代表质量排名", "浏览不会预留槽位", "浏览可用槽位",
                "在自己的额度内选择", "最低 $2", "不会自动扣款"))
    for phrase in phrases:
        assert phrase in document.text
    assert not document.duplicate_ids


@pytest.mark.parametrize("language", ["en", "zh"])
def test_home_copy_describes_bounded_examples_and_links_the_full_public_catalogue(language):
    source = customer_docs.overview(Config(), per_slot_pricing=True)
    page = Document(bilingual.project_html(source, language, "/"))
    assert any(link.endswith("/slots") for link in page.links)
    assert ("up to three real configuration examples" if language == "en"
            else "最多三个实际配置示例") in page.text
    assert "Alpha" in page.text


@pytest.mark.parametrize("relative", ["docs/index.html", "docs/public-gateway.html",
                                      "docs/guidebook.html", "docs/runbooks.html"])
def test_related_references_distinguish_the_public_catalogue_from_private_claims(relative):
    source = (Path(__file__).parents[1] / relative).read_text()
    page = Document(source)
    assert any(link.endswith("/slots") for link in page.links) or "/slots" in page.codes
    assert "/account/available" in page.links or "/account/available" in page.codes
    for phrase in ("configuration examples", "20 machines", "Residential", "ISP", "Datacenter", "Alpha"):
        assert phrase in page.text
    assert "/#slot-pricing" not in source
