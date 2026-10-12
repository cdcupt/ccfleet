"""The current documentation keeps one set of anchors and hash-allowed scripts."""

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ccfleetd import bilingual

ROOT = Path(__file__).parents[1]


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.local_links = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.local_links.append(urlsplit(attrs["href"]).fragment)


@pytest.mark.parametrize("name", ["index", "platform-comparison", "public-gateway"])
def test_current_library_pages_have_unique_anchors_and_exact_language_script(name):
    html = (ROOT / f"docs/{name}.html").read_text()
    parser = Anchors()
    parser.feed(html)
    assert not [key for key, count in Counter(parser.ids).items() if count > 1]
    assert not set(parser.local_links) - set(parser.ids)
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S)
    assert scripts == [bilingual.SCRIPT]
    assert 'data-language="en"' in html
    assert 'data-language="both"' not in html and "data-language-choice" not in html
    assert 'data-language-link="zh"' in html and 'lang="zh-Hans"' in html
    for language in ("en", "zh"):
        rendered = bilingual.project_html(html, language, f"/docs/library/docs/{name}.html")
        omitted = "copy-zh" if language == "en" else "copy-en"
        assert f'class="{omitted}"' not in rendered.split("<body", 1)[1]
        assert f'href="/{language}/docs/library/docs/{name}.html"' in rendered
        assert not re.search(
            r"historical|--transport bridge|--legacy-history",
            rendered.split("<body", 1)[1],
            flags=re.I,
        )


def test_comparison_keeps_known_transport_and_unknown_competitor_internals_separate():
    html = (ROOT / "docs/platform-comparison.html").read_text()
    assert "我们的 HTTPS 网关" in html
    assert "厂商公开文档；内部实现未审计" in html
    assert "默认随机策略" in html and "只使用你自己持有且明确选中的槽位" in html
    assert "no live second-account switch was exercised" in html
