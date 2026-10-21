"""Technical evidence stays visible, bilingual and free of competitor identity."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from ccfleetd import bilingual, customer_docs
from ccfleetd.config import Config
from tests.test_html_document_preservation import VOID, Document

ROOT = Path(__file__).resolve().parents[1]
COMPARISON = "/docs/library/docs/platform-comparison.html"


class Placement(HTMLParser):
    """Record ancestor attributes to distinguish visible entry points from disclosures."""

    def __init__(self, source):
        super().__init__()
        self.stack = []
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.elements.append((tag, attrs, tuple(self.stack)))
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


@pytest.mark.parametrize("language", ["en", "zh"])
def test_how_it_works_exposes_technical_entry_points_outside_advanced_routing(language):
    source = bilingual.project_html(customer_docs.how_it_works(Config()), language,
                                    "/docs/how-it-works")
    page = Document(source)
    elements = Placement(source).elements
    entry = next(attrs for _, attrs, _ in elements
                 if attrs.get("id") == "technical-overview")
    assert entry["class"] == "technical-overview"
    assert source.index('id="technical-overview"') < source.index('class="how"')
    expected = {f"{COMPARISON}{fragment}"
                for fragment in ("", "#architecture", "#verification", "#sources")}
    entry_links = {bilingual.route_target(attrs["href"])[0] for tag, attrs, parents in elements
                   if tag == "a" and "href" in attrs and
                   any(parent.get("id") == "technical-overview" for _, parent in parents)}
    assert expected <= entry_links
    for tag, attrs, parents in elements:
        if tag == "a" and any(parent.get("id") == "technical-overview"
                              for _, parent in parents):
            assert all(parent_tag != "details" for parent_tag, _ in parents)
        if attrs.get("id") == "model-connections":
            assert attrs["class"] == "routing-guide"
            assert any(parent_tag == "details" and
                       "routing-details" in parent.get("class", "").split()
                       for parent_tag, parent in parents)
            assert "card" not in attrs["class"].split()
    assert not page.duplicate_ids


@pytest.mark.parametrize("language", ["en", "zh"])
def test_technical_guide_keeps_architecture_evidence_and_sources_visible(language):
    source = bilingual.project_html((ROOT / "docs/platform-comparison.html").read_text(),
                                    language, COMPARISON)
    page = Document(source)
    assert {"architecture", "verification", "routing", "host-comparison", "sources"} <= page.ids
    for _, attrs, parents in Placement(source).elements:
        if attrs.get("id") in {"architecture", "verification", "sources"}:
            assert all(tag != "details" for tag, _ in parents)
    assert source.index('id="architecture"') < source.index('id="verification"')
    assert source.index('id="verification"') < source.index('id="host-comparison"')
    assert not page.duplicate_ids
    assert len(page.tables) == 1
    assert any("1494f6b0646188bbdab622ddd4321627df328c7c/ccfleetd/gateway.py" in link
               for link in page.links)
    if language == "en":
        for limitation in ("no live second-account switch was exercised",
                           "Token counting and full API parity are not established",
                           "has not been proven equivalent",
                           "internals not audited"):
            assert limitation in page.text
    else:
        for limitation in ("尚未实测第二个真实账号", "未审计其内部实现", "不宣称连接指纹相同"):
            assert limitation in page.text


def test_current_customer_documents_hide_competitor_names_and_identifying_urls():
    pages = [customer_docs.how_it_works(Config()), customer_docs.guide(Config()),
             (ROOT / "docs/platform-comparison.html").read_text(),
             (ROOT / "docs/index.html").read_text()]
    for source in pages:
        assert not re.search(r"CC\s*Host|cchost[.]ai", source, re.I)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_every_how_it_works_disclosure_has_a_shared_summary_and_padded_body(language):
    """All three sections retain the same layout boundary in either language."""
    source = bilingual.project_html(customer_docs.how_it_works(Config()), language,
                                    "/docs/how-it-works")
    elements = Placement(source).elements
    disclosures = [attrs for tag, attrs, _ in elements
                   if tag == "details" and "doc-disclosure" in attrs.get("class", "").split()]
    assert len(disclosures) == 3
    for attrs in disclosures:
        children = [(tag, child) for tag, child, parents in elements
                    if parents and parents[-1] == ("details", attrs)]
        assert sum(tag == "summary" for tag, _ in children) == 1
        assert any(tag == "div" and (
            "disclosure-body" in child.get("class", "").split()
            or "routing-guide" in child.get("class", "").split()) for tag, child in children)
