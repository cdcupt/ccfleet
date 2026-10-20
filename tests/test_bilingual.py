"""Language pages preserve security boundaries and work without display-switch scripts."""

import base64
import hashlib
import re

from ccfleetd import bilingual
from ccfleetd.api import HTML_HEADERS


def test_untrusted_labels_are_text_in_both_languages():
    result = bilingual.label('<script>alert("x")</script>', '<img src=x onerror=alert(1)>')
    assert '<script>' not in result and '<img ' not in result
    assert '&lt;script&gt;' in result and '&lt;img ' in result
    assert 'lang="en"' in result and 'lang="zh-Hans"' in result


def test_language_script_is_allowed_only_by_its_exact_digest():
    digest = base64.b64encode(hashlib.sha256(bilingual.SCRIPT.encode()).digest()).decode()
    assert bilingual.SCRIPT_CSP == f"'sha256-{digest}'"
    sources = re.search(r"script-src ([^;]+)", HTML_HEADERS['Content-Security-Policy'])[1]
    assert bilingual.SCRIPT_CSP in sources
    assert 'unsafe-inline' not in sources and 'http' not in sources
    assert bilingual.SCRIPT_TAG == f'<script>{bilingual.SCRIPT}</script>'


def test_language_control_does_not_submit_forms_or_read_secrets():
    assert bilingual.CONTROLS.count('data-language-link=') == 2
    assert 'both' not in bilingual.CONTROLS.lower()
    assert 'innerHTML' not in bilingual.SCRIPT
    assert '.value' not in bilingual.SCRIPT
    assert 'fetch(' not in bilingual.SCRIPT and 'XMLHttpRequest' not in bilingual.SCRIPT
    assert 'localStorage' not in bilingual.SCRIPT
    assert 'location.hash' in bilingual.SCRIPT


def test_projection_removes_the_other_language_and_keeps_secrets_once():
    source = ('<html lang="en"><head><script>const x="<&";</script></head>'
              '<body>' + bilingual.CONTROLS + bilingual.label('Hello', '你好')
              + '<pre>FAKE_SECRET_ONCE</pre><form action="/account/gateway/create">'
              '<input name="csrf" value="FAKE&amp;QUOTE&#x27;"></form></body></html>')
    for lang, wanted, absent in [('en', 'Hello', '你好'), ('zh', '你好', 'Hello')]:
        html = bilingual.project_html(source, lang, '/account')
        assert wanted in html and absent not in html
        assert html.count('FAKE_SECRET_ONCE') == 1
        assert '<script>const x="<&";</script>' in html
        assert 'value="FAKE&amp;QUOTE&#x27;"' in html
        prefix = '/zh' if lang == 'zh' else ''
        assert f'action="{prefix}/account/gateway/create"' in html


def test_options_attributes_and_language_links_do_not_echo_request_parameters():
    source = (bilingual.CONTROLS + '<select><option value="sticky" data-copy-en="Sticky" '
              'data-copy-zh="保持会话">Sticky / 保持会话</option></select>'
              '<input data-placeholder-en="Name" data-placeholder-zh="名称">')
    html = bilingual.project_html(source, 'zh', '/account?note=PRIVATE_REQUEST_VALUE')
    assert 'PRIVATE_REQUEST_VALUE' not in html
    assert '<option value="sticky">保持会话</option>' in html
    assert 'placeholder="名称"' in html and 'data-placeholder-en' not in html
    assert 'href="/en/account"' in html and 'href="/zh/account"' in html


def test_localization_never_creates_api_or_foreign_aliases():
    for target in ['/zh/v1/messages', '/en/api/nodes',
                   '/zh/docs/library/../README.html', '/zh/docs%2fguide']:
        assert bilingual.route_target(target) is None
    for url in ['https://accounts.google.com/auth?secret=x', '/v1/messages',
                '/auth/google/callback?code=FAKE']:
        assert bilingual.localized_url(url, 'zh', public_origin='https://fleet.example') == url
