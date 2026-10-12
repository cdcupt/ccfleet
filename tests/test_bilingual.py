"""Language selection stays browser-local and does not relax script policy."""

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
    assert bilingual.CONTROLS.count('type="button"') == 3
    assert 'innerHTML' not in bilingual.SCRIPT
    assert '.value' not in bilingual.SCRIPT
    assert 'fetch(' not in bilingual.SCRIPT and 'XMLHttpRequest' not in bilingual.SCRIPT
    assert 'option[data-copy-en][data-copy-zh]' in bilingual.SCRIPT
    assert 'textContent' in bilingual.SCRIPT
