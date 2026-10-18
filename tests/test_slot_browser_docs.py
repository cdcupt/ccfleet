"""Customer guidance separates browsing unallocated machines from held-slot controls."""

from pathlib import Path

import pytest

from ccfleetd import bilingual, customer_docs
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document


@pytest.mark.parametrize("language", ["en", "zh"])
def test_guide_explains_signed_in_machine_browsing_without_changing_billing(language):
    source = customer_docs.guide(Config(), per_slot_pricing=True)
    page = Document(bilingual.project_html(source, language, "/docs/guide"))
    assert any(link.endswith("/account/available") for link in page.links)
    assert not page.duplicate_ids
    phrases = (("Your slots", "Browse available slots", "signed-in page", "host provider",
                "IP provider", "Machine 7", "filter by provider and network type",
                "ISP, Residential or Datacenter", "up to 20 machines", "you return to Your slots",
                "existing eligibility checks", "does not charge automatically",
                "Alpha pricing", "$2 minimum", "agreed price")
               if language == "en" else
               ("我的槽位", "浏览可用槽位", "页面需要登录", "主机服务商", "IP 服务商",
                "主机 7", "按服务商及网络类型", "ISP、住宅或机房", "最多展示 20 台主机",
                "返回“我的槽位”管理", "领取条件仍然适用", "不会自动扣款", "Alpha 定价",
                "最低 $2", "原约定价格"))
    for phrase in phrases:
        assert phrase in page.text


@pytest.mark.parametrize("relative", ["docs/index.html", "docs/public-gateway.html",
                                      "docs/guidebook.html", "docs/runbooks.html"])
def test_related_references_link_to_the_authenticated_slot_browser(relative):
    page = Document((Path(__file__).parents[1] / relative).read_text())
    assert "/account/available" in page.links or "/account/available" in page.codes
    for phrase in ("Browse available slots", "Your slots", "20 machines", "Alpha"):
        assert phrase in page.text
    assert not page.duplicate_ids


def test_browsing_docs_do_not_change_public_homepage_catalogue():
    source = customer_docs.overview(Config(), per_slot_pricing=True)
    assert 'href="#slot-pricing"' in source
    assert "Alpha pricing" in source
