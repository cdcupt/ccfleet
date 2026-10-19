"""Current provider guidance must not rewrite dated Anthropic/competitor proof."""
from html.parser import HTMLParser
from pathlib import Path

import pytest

from ccfleetd import bilingual
from tests.test_html_document_preservation import VOID, Document

ROOT = Path(__file__).resolve().parents[1]
PAGE = '/docs/library/docs/platform-comparison.html'


class Balanced(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, tag

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_provider_note_is_separate_readable_and_links_to_real_connect_and_price_anchors(language):
    source = (ROOT / 'docs/platform-comparison.html').read_text()
    rendered = bilingual.project_html(source, language, PAGE)
    page = Document(rendered)
    prefix = '/zh' if language == 'zh' else ''
    for anchor in ('connect', 'prices'):
        assert f'{prefix}/docs/library/docs/devin.html#{anchor}' in page.links
        assert anchor in Document((ROOT / 'docs/devin.html').read_text()).ids
    assert 'Claude Opus 5.5 Max' in page.text
    assert 'Read' in page.text and 'Edit' in page.text and 'Bash' in page.text
    if language == 'en':
        assert 'default Anthropic route' in page.text
        assert 'three passing local tests and a streamed final answer' in page.text
        assert 'does not establish full API parity' in page.text
        assert 'Bring your own subscription' in page.text
        assert 'tools run on your computer' in page.text
    else:
        assert '默认 Anthropic 路线' in page.text
        assert '本机三项测试通过' in page.text
        assert '未证明完整 API 语义一致' in page.text
        assert '请自备订阅' in page.text
        assert '工具在你的电脑上运行' in page.text
    parser = Balanced()
    parser.feed(rendered)
    parser.close()
    assert not parser.stack and not page.duplicate_ids


def test_anthropic_proof_and_competitor_unknowns_keep_their_original_date_and_scope():
    source = (ROOT / 'docs/platform-comparison.html').read_text()
    text = Document(bilingual.project_html(source, 'en', PAGE)).text
    assert 'Anthropic evidence checked October 11' in text
    assert 'no live second-account switch was exercised' in text
    assert 'does not prove token counting or full API parity' in text
    assert 'CC Host docs checked October 11, 2026' in text
    assert 'No CC Host live routing, isolation, performance or provider-acceptance test was performed' in text
    assert 'CC Host’s TLS/SSH internals and fingerprint behavior are also unverified here' in text
    assert '1494f6b0646188bbdab622ddd4321627df328c7c' in source
    assert 'https://ccfleet.io' in source and 'ccfleet.daichenlab.com' not in source
    assert 'On the default Anthropic route, Fleet’s outgoing Python TLS connection' in text
    assert 'The Devin pilot uses native Devin CLI; no fingerprint-equivalence claim is made for it' in text


def test_comparison_retains_mobile_stacking_and_balanced_authored_html():
    source = (ROOT / 'docs/platform-comparison.html').read_text()
    parser = Balanced()
    parser.feed(source)
    parser.close()
    assert not parser.stack
    assert '@media screen and (max-width:620px)' in source
    assert '.host-path-diagram{display:block' in source
    assert '.host-feature-table tbody td{display:block;width:auto' in source
