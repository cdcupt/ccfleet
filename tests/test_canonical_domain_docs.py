"""A domain change has a reversible path for an already connected computer."""
from pathlib import Path

import pytest

from ccfleetd import bilingual, customer_docs
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("language", ["en", "zh"])
def test_new_domain_guide_has_separate_launcher_and_keeps_existing_connection(language):
    page = Document(bilingual.project_html(customer_docs.guide(Config()), language, "/docs/guide"))
    assert "new-domain" in page.ids and not page.duplicate_ids
    assert "https://ccfleet.io" in page.text and "0.2.6" in page.text
    assert any("install.sh | bash -s -- --next" in code for code in page.pre_codes)
    assert any("$HOME/.local/bin/ccfleet-next local --check" in code for code in page.pre_codes)
    if language == "en":
        for phrase in ("Your connection keys, selected slots and sign-ins stay the same",
                       "keeps your existing ccfleet executable and running sessions",
                       "do not need to pair again", "old API address remains available"):
            assert phrase in page.text
    else:
        for phrase in ("连接密钥、所选槽位和登录保持不变", "保留现有 ccfleet 程序",
                       "不需要重新配对或创建连接密钥", "旧 API 地址继续可用"):
            assert phrase in page.text


def test_current_navigation_links_use_canonical_domain_even_on_history_pages():
    for path in (ROOT / "docs").glob("*.html"):
        assert not any(link.startswith("https://ccfleet.daichenlab.com/")
                       for link in Document(path.read_text()).links), path.name


@pytest.mark.parametrize("language", ["en", "zh"])
def test_gateway_reference_links_the_same_migration_in_both_languages(language):
    source = (ROOT / "docs/public-gateway.html").read_text()
    page = Document(bilingual.project_html(source, language, "/docs/library/docs/public-gateway.html"))
    assert "new-domain" in page.ids and "0.2.6" in page.text
    target = "/docs/guide#new-domain" if language == "en" else "/zh/docs/guide#new-domain"
    assert target in page.links
