"""The footer keeps product identity together and separates its description."""
from html.parser import HTMLParser

import pytest

from ccfleetd import usersite
from ccfleetd.bilingual import project_html


class Footer(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.in_footer = False
        self.stack = []
        self.identity = None
        self.brand_parent = None
        self.badge_parent = None
        self.description_parent = None
        self.brand_href = None
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'footer':
            self.in_footer = True
        if not self.in_footer:
            return
        parent = self.stack[-1] if self.stack else None
        frame = (tag, attrs)
        if 'sitefoot-identity' in attrs.get('class', '').split():
            self.identity = frame
        if tag == 'a' and 'brand' in attrs.get('class', '').split():
            self.brand_parent, self.brand_href = parent, attrs.get('href')
        if 'alpha-badge' in attrs.get('class', '').split():
            self.badge_parent = parent
        if tag == 'p':
            self.description_parent = parent
        if tag not in ('path', 'circle', 'rect'):
            self.stack.append(frame)

    def handle_endtag(self, tag):
        if not self.in_footer:
            return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        if tag == 'footer':
            self.in_footer = False

    def handle_data(self, data):
        if self.in_footer:
            self.text.append(data)


@pytest.mark.parametrize('language,badge,description,home', [
    ('en', 'Alpha', 'Local Claude Code.', '/'),
    ('zh', 'Alpha 内测', 'Claude Code 在本机运行', '/zh/'),
])
def test_footer_groups_brand_and_alpha_without_pulling_description_into_the_row(
        language, badge, description, home):
    html = project_html(usersite._shell('status', '<p>Page content</p>'), language, '/')
    footer = Footer(html)
    assert footer.identity is not None
    assert footer.brand_parent is footer.identity and footer.badge_parent is footer.identity
    assert footer.description_parent is not footer.identity
    assert footer.brand_href == home
    text = ''.join(footer.text)
    assert badge in text and description in text
    assert 'Alpha 内测' not in text if language == 'en' else 'Local Claude Code.' not in text
