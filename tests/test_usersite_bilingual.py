"""Translated pages keep their account boundaries and native form contracts."""
from html import escape
from html.parser import HTMLParser

import pytest

from ccfleetd import slots, statuspage, usersite
from ccfleetd.bilingual import SCRIPT_TAG, project_html
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
    assert 'data-language="en"' in source
    assert source.count('data-language-link=') == 2
    assert 'data-language-choice=' not in source and '双语 / Both' not in source
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
    assert 'The gateway can read model requests and responses' in page.copy('en')
    assert '网关能读取模型请求和回复' in page.copy('zh')
    assert '通过 HTTPS 发给 Fleet' in page.copy('zh')
    assert '不会自动清除已有文件或保存的历史' in page.copy('zh')
    for removed in ('--transport bridge', '--legacy-history', 'live-folder'):
        assert removed not in source
    assert 'help&desk@example.com' not in source
    assert 'mailto:help&amp;desk@example.com' in source
    assert all(link.get('href') == 'mailto:help&desk@example.com' for link in page.links
               if link.get('href', '').startswith('mailto:'))


def test_privacy_scopes_performance_aggregates_separately_from_the_public_usage_ranking():
    page = Page(usersite.privacy_page(Config()))
    english, chinese = page.copy('en'), page.copy('zh')
    for phrase in (
        'at least twenty observed attempts',
        'only one contributing Fleet user',
        "a total or average can reflect that user's traffic",
        'twenty samples, which can all come from one user',
        'does not split measurements by user or slot',
        'public Status performance panel',
        'homepage ranking',
        'visible to everyone',
        'Masking is not anonymity',
        'common Opus 5.5 API-equivalent USD value',
        'not a Claude Pro/Max subscription bill',
        'prompts, credentials or response contents',
        'Status describes machine health only',
        'no detailed reasons or per-machine uptime cards',
    ):
        assert phrase in english
    for phrase in (
        '至少二十次转发尝试',
        '公开汇总可能只包含一个 Fleet 用户的观测',
        '总量或平均值可能反映这个用户的流量',
        '所有样本可以来自同一个用户',
        '不按用户或槽位分别展示统计',
        '公开的服务状态性能面板',
        '首页用量排行',
        '对所有人公开',
        '遮盖不等于匿名',
        '统一按 Opus 5.5 API 参考价格折算的美元价值',
        '不是 Claude Pro/Max 订阅账单',
        '也不公开名称、邮箱、提示词、凭据或回复内容',
        '状态只表示主机健康',
        '表格不展示详细异常原因或逐台主机的可用率卡片',
    ):
        assert phrase in chinese
    assert 'three contributing Fleet users' not in english
    assert 'three-account threshold' not in english
    assert '三个 Fleet 用户' not in chinese and '三个账号门槛' not in chinese


def test_privacy_explains_independent_output_rate_and_daily_numeric_retention():
    page = Page(usersite.privacy_page(Config()))
    english, chinese = page.copy('en'), page.copy('zh')
    for phrase in (
        'A separate streamed output rate',
        'first-to-last response-body chunk time',
        'native/bridge traffic or at the HTTPS gateway',
        'at least two chunks and at least 100 ms',
        'Single-chunk or too-short replies are excluded',
        'buffering can affect the observed rate',
        'excludes the initial wait',
        'thinking or tool output',
        'twenty eligible replies are needed independently',
        'anonymous daily sample counts, output-token totals and delivery-time totals',
        'seven UTC calendar days',
        'public output-rate measure shows no model breakdown, input/cache token totals',
        'private account-bound seven-day relay file',
        'Older traffic is not backfilled',
    ):
        assert phrase in english
    for phrase in (
        '合并符合条件的节点原生／桥接流和 HTTPS 网关观测',
        '从首个到最后一个响应正文分块',
        '至少两个分块',
        '至少持续 100 毫秒',
        '单个分块或过短的回复不计入',
        '缓冲可能影响观测到的速率',
        '不含初始等待',
        '思考或工具输出',
        '单独要求至少二十个符合条件的回复',
        '每日样本数、输出 Token 总量和交付时间总和',
        '七个 UTC 日期',
        '公开输出速率不展示模型明细',
        '私有、与账号绑定的七日转发统计文件',
        '不补填历史流量',
    ):
        assert phrase in chinese


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
    assert page.forms == [{'class': 'inline', 'method': 'post',
                           'action': '/account/slots/slot-01/update',
                           'data-fleet-action': 'version'}]
    assert page.inputs == [{'type': 'hidden', 'name': 'csrf', 'value': CSRF}]


def test_release_confirmation_and_csrf_are_not_duplicated_by_translation():
    page = Page(usersite._release(SLOT, CSRF))
    assert page.forms == [{'class': 'inline', 'method': 'post', 'action': '/account/slots/slot-01/release'}]
    assert page.inputs == [{'type': 'hidden', 'name': 'csrf', 'value': CSRF},
                           {'type': 'checkbox', 'name': 'confirm', 'value': 'wipe', 'required': None}]
    assert 'Delete everything on it' in page.copy('en')
    assert '删除槽位中的文件、会话和服务商登录信息' in page.copy('zh')
    assert '不会删除你的服务商账号或取消订阅' in page.copy('zh')


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


def test_projected_product_pages_have_one_language_and_no_old_setup_tours():
    cfg = Config(public_url='https://fleet.example', google_client_id='client',
                 google_client_secret='secret', cookie_secret='x' * 32)
    for target, source in (
        ('/privacy', usersite.privacy_page(cfg)),
        ('/account/slots/slot-01/cli', usersite.cli_pairing_page(SLOT, 'fake-pairing-code')),
        ('/account', usersite._signed_out(cfg)),
    ):
        english = project_html(source, 'en', target, cfg.public_url)
        chinese = project_html(source, 'zh', target, cfg.public_url)
        assert Page(english).copy('zh') == ''
        assert Page(chinese).copy('en') == ''
        assert 'data-language="en"' in english and 'data-language="zh"' in chinese
        assert 'lang="en"' in english and 'lang="zh-Hans"' in chinese
        assert 'data-language-link="both"' not in english + chinese
        for removed in ('--transport bridge', '--legacy-history', 'live-folder', 'ccfleet-connect'):
            assert removed not in english + chinese
    assert 'fake-pairing-code' not in usersite.privacy_page(cfg)


def test_single_language_projection_keeps_release_authority_and_confirmation():
    raw = usersite._release(SLOT, CSRF)
    english = Page(project_html(raw, 'en', '/account'))
    chinese = Page(project_html(raw, 'zh', '/account'))
    assert english.forms == [{'class': 'inline', 'method': 'post',
                             'action': '/account/slots/slot-01/release'}]
    assert chinese.forms == [{'class': 'inline', 'method': 'post',
                             'action': '/zh/account/slots/slot-01/release'}]
    assert chinese.inputs == english.inputs == [
        {'type': 'hidden', 'name': 'csrf', 'value': CSRF},
        {'type': 'checkbox', 'name': 'confirm', 'value': 'wipe', 'required': None},
    ]
    assert 'Delete everything on it' in english.copy('en') and english.copy('zh') == ''
    assert '删除槽位中的文件、会话和服务商登录信息' in chinese.copy('zh') and chinese.copy('en') == ''


def test_single_language_signin_fields_keep_form_names_and_localize_accessible_labels():
    raw = usersite._sign_in(SLOT, {}, {}, CSRF, NOW)
    english = Page(project_html(raw, 'en', '/account'))
    chinese = Page(project_html(raw, 'zh', '/account'))
    en_field = next(field for field in english.inputs if field.get('name') == 'email')
    zh_field = next(field for field in chinese.inputs if field.get('name') == 'email')
    assert en_field['aria-label'] == 'Claude email'
    assert zh_field['aria-label'] == 'Claude 邮箱'
    assert en_field['placeholder'] == 'your Claude email (optional)'
    assert zh_field['placeholder'] == 'Claude 邮箱（可选）'
    assert en_field['type'] == zh_field['type'] == 'email'
    assert all(field['value'] == CSRF for field in english.inputs + chinese.inputs
               if field.get('name') == 'csrf')
    assert english.forms[0]['action'] == '/account/slots/slot-01/signin'
    assert chinese.forms[0]['action'] == '/zh/account/slots/slot-01/signin'
