"""Current provider guidance preserves dated proof and anonymous comparison limits."""
import re
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
    pilot = re.search(r'<article\b[^>]*\bid="devin-pilot"[^>]*>(.*?)</article>',
                      rendered, re.S)
    assert pilot
    pilot_page = Document(pilot[1])
    assert f'{prefix}/docs/library/docs/devin.html#connect' in pilot_page.links
    assert f'{prefix}/docs/library/docs/devin.html#prices' in pilot_page.links
    if language == 'en':
        assert 'default Anthropic route' in page.text
        assert 'October 18, 2026' in pilot_page.text
        assert 'three passing local tests and a streamed final answer' in pilot_page.text
        assert 'Token counting and full API parity are not established' in pilot_page.text
        assert 'Bring your own subscription' in pilot_page.text
        assert 'tools run on your computer' in pilot_page.text
        assert 'native Devin CLI on your own slot' in pilot_page.text
    else:
        assert '默认 Anthropic 路线' in page.text
        assert '2026 年 10 月 18 日' in pilot_page.text
        assert '本机三项测试通过' in pilot_page.text
        assert '尚未证明 Token 计数与完整 API 语义一致' in pilot_page.text
        assert '请自备订阅' in pilot_page.text
        assert '工具在你的电脑上运行' in pilot_page.text
        assert '自有槽位上的原生 Devin CLI' in pilot_page.text
    parser = Balanced()
    parser.feed(rendered)
    parser.close()
    assert not parser.stack and not page.duplicate_ids


def test_anthropic_proof_and_reference_platform_unknowns_keep_their_original_date_and_scope():
    source = (ROOT / 'docs/platform-comparison.html').read_text()
    text = Document(bilingual.project_html(source, 'en', PAGE)).text
    assert 'October 11, 2026' in text
    assert 'Anthropic · one live slot' in text
    assert 'Native Read/Write, streamed replies and token counting passed on one live account' in text
    assert 'Multi-slot behavior used controlled tests' in text
    assert 'no live second-account switch was exercised' in text
    assert 'Token counting and full API parity are not established' in text
    assert 'vendor documentation checked October 11, 2026; internals were not audited' in text
    assert 'No live routing, isolation, performance or provider-acceptance test was performed' in text
    assert 'reference platform’s exact fingerprint/TLS stack and renewal algorithm remain unverified' in text
    assert 'Reference platform' in text
    assert not re.search(r'CC\s*Host|cchost[.]ai', source, re.I)
    assert '1494f6b0646188bbdab622ddd4321627df328c7c' in source
    assert 'ccfleet.io' in source and 'ccfleet.daichenlab.com' not in source
    assert 'Fleet’s outgoing Python TLS connection has not been proven equivalent' in text
    assert 'native Claude Code’s JA3 fingerprint' in text
    assert 'The native Devin pilot makes no fingerprint-equivalence claim' in text


def test_comparison_retains_mobile_stacking_table_overflow_and_balanced_authored_html():
    source = (ROOT / 'docs/platform-comparison.html').read_text()
    parser = Balanced()
    parser.feed(source)
    parser.close()
    assert not parser.stack
    assert '@media(max-width:960px){.route{grid-template-columns:1fr}' in source
    assert '.evidence-grid{grid-template-columns:1fr}' in source
    assert '@media(max-width:640px)' in source
    assert '.two-up,.references{grid-template-columns:1fr}' in source
    assert '.table-scroll{overflow-x:auto' in source
    page = Document(source)
    assert page.tables and all(any('table-scroll' in attrs.get('class', '').split()
                                  for _, attrs in parents) for parents in page.tables)
