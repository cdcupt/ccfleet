"""Translated pages keep their account boundaries and native form contracts."""
from html import escape
from html.parser import HTMLParser

import pytest

from ccfleetd import slots, statuspage, usersite
from ccfleetd.bilingual import SCRIPT_TAG
from ccfleetd.config import Config
from ccfleetd.render import LOCAL_TIMES_TAG
from ccfleetd.store import Store

NOW = 2_000_000_000.0
CSRF = "a" * 64
SLOT = {"id": "slot-01", "name": "example-1", "unix_user": "slot01", "state": slots.CLAIMED}


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.stack = []
        self.text = {"en": [], "zh": []}
        self.inputs = []
        self.forms = []
        self.links = []
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "input":
            self.inputs.append(attrs)
        if tag == "form":
            self.forms.append(attrs)
        if tag == "a":
            self.links.append(attrs)
        if tag not in {"input", "meta", "link", "br", "wbr", "img", "hr"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        classes = {name for _, attrs in self.stack for name in attrs.get("class", "").split()}
        if "copy-en" in classes:
            self.text["en"].append(value)
        if "copy-zh" in classes:
            self.text["zh"].append(value)

    def copy(self, language):
        return " ".join(self.text[language])


def test_shell_has_only_authored_scripts_and_no_translation_network_dependency():
    source = usersite._shell("example", "<p>Untranslated dynamic content</p>")
    assert 'data-language="both"' in source
    assert source.count('data-language-choice=') == 3
    assert source.count('<script') == 2
    assert SCRIPT_TAG in source and LOCAL_TIMES_TAG in source
    assert '<script src=' not in source
    assert 'Untranslated dynamic content' in source
    page = Page(source)
    assert 'Overview' in page.copy('en') and '概览' in page.copy('zh')
    assert any(link.get('href') == '/docs/guide' for link in page.links)
    assert '<p><div' not in source


def test_signed_out_google_button_preserves_the_authorization_target():
    source = usersite._signed_out(Config(public_url='https://fleet.example',
                                       google_client_id='client', google_client_secret='secret',
                                       cookie_secret='x' * 32))
    page = Page(source)
    assert [link['href'] for link in page.links if '/auth/google/start' in link.get('href', '')] == [
        '/auth/google/start?next=/account']
    assert 'Continue with Google' in page.copy('en') and '使用 Google 登录' in page.copy('zh')
    assert 'only the address and the id' in page.copy('en')
    assert '只保留邮箱和 ID' in page.copy('zh')
    assert not page.forms


def test_privacy_translation_uses_real_retention_values_and_escapes_contact():
    cfg = Config(session_ttl_s=36 * 3600, retention_days=45, contact_email='help&desk@example.com')
    source = usersite.privacy_page(cfg)
    page = Page(source)
    assert '36 hours' in page.copy('en') and '36 小时' in page.copy('zh')
    assert '45 days' in page.copy('en') and '45 天' in page.copy('zh')
    assert 'BWH can read model requests and responses' in page.copy('en')
    assert 'BWH 能读取模型请求和回复' in page.copy('zh')
    assert '内层 SSH 加密' in page.copy('zh')
    assert 'help&desk@example.com' not in source
    assert 'mailto:help&amp;desk@example.com' in source
    assert all(link.get('href') == 'mailto:help&desk@example.com' for link in page.links
               if link.get('href', '').startswith('mailto:'))


@pytest.mark.parametrize('update', [{}, {'state': 'failed', 'detail': 'controlled error'}])
def test_stable_slot_with_new_release_renders_update_controls_without_label_shadow(update):
    slot = {**SLOT, 'claude_channel': 'stable'}
    source = usersite._claude_row(slot, {'pinned_version': 'stable'},
                                 {'claude': {'version': '2.1.280'}}, update,
                                 {'latest': {'version': '2.1.284'},
                                  'stable': {'version': '2.1.280'}}, CSRF)
    page = Page(source)
    assert 'Update to 2.1.284' in page.copy('en')
    assert '更新到 2.1.284' in page.copy('zh')
    assert 'keeps itself current' in page.copy('en') and '自动保持' in page.copy('zh')
    assert page.forms == [{'class': 'inline', 'method': 'post', 'action': '/account/slots/slot-01/update'}]
    assert page.inputs == [{'type': 'hidden', 'name': 'csrf', 'value': CSRF}]


def test_release_confirmation_and_csrf_are_not_duplicated_by_translation():
    page = Page(usersite._release(SLOT, CSRF))
    assert page.forms == [{'class': 'inline', 'method': 'post', 'action': '/account/slots/slot-01/release'}]
    assert page.inputs == [{'type': 'hidden', 'name': 'csrf', 'value': CSRF},
                           {'type': 'checkbox', 'name': 'confirm', 'value': 'wipe', 'required': None}]
    assert 'Delete everything on it' in page.copy('en')
    assert '删除槽位中的文件、会话和 Claude 登录信息' in page.copy('zh')
    assert '不会删除你的 Claude 账号' in page.copy('zh')


def test_slot_and_device_names_are_escaped_and_never_treated_as_copy_labels():
    unsafe = 'Name"><img src=x onerror=alert(1)>'
    slot = {**SLOT, 'name': unsafe}
    source = usersite._slot_card(slot, {}, None, {}, CSRF, Config(), NOW)
    assert escape(unsafe) in source and '<img' not in source
    assert source.startswith('<div class="card slot"')
    page = Page(source)
    assert {form['action'] for form in page.forms} >= {
        '/account/slots/slot-01/signin', '/account/slots/slot-01/rename', '/account/slots/slot-01/release'}
    assert all(inp['value'] == CSRF for inp in page.inputs if inp.get('name') == 'csrf')


def test_pairing_code_and_commands_appear_once_while_explanations_are_bilingual():
    source = usersite.cli_pairing_page({**SLOT, 'name': 'Name'}, 'test-pairing-code')
    page = Page(source)
    assert source.count('test-pairing-code') == 1
    assert source.count('curl -fsSL https://raw.githubusercontent.com/') == 1
    assert '<strong>Name</strong>' in source
    assert 'Connect a computer' in page.copy('en') and '连接电脑' in page.copy('zh')
    assert '10 minutes' in page.copy('en') and '10 分钟' in page.copy('zh')


def test_status_translation_does_not_claim_a_website_chart_checks_models():
    store = Store(':memory:')
    try:
        page = Page(statuspage.page(store, Config(), None, NOW))
    finally:
        store.close()
    assert 'does not verify model-request readiness' in page.copy('en')
    assert '不代表下一次模型请求一定成功' in page.copy('zh')
    assert '暂无监测中的槽位主机' in page.copy('zh')


def test_bilingual_block_translation_rejects_incomplete_authored_templates():
    with pytest.raises(ValueError, match='different number'):
        usersite._blocks('<h1>Example</h1><p>Body</p>', ['示例'])
