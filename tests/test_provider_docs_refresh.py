"""Current user guidance and external source links, without model calls."""
import pytest

from ccfleetd import bilingual, customer_docs, usersite
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_official_external_source_paths_are_preserved(language):
    external = 'https://support.claude.com/en/articles/11049741-what-is-the-max-plan'
    source = '<a href="' + external + '">Source</a><a href="/en/account">Account</a>'
    rendered = bilingual.project_html(source, language, '/docs/guide', 'https://fleet.example')
    assert 'href="' + external + '"' in rendered
    assert ('href="/account"' if language == 'en' else 'href="/zh/account"') in rendered


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_current_pages_teach_provider_choice_and_separate_subscription_cost(language):
    for page in (customer_docs.overview, customer_docs.guide, customer_docs.how_it_works):
        html = bilingual.project_html(page(Config()), language)
        text = Document(html).text
        assert 'Anthropic' in text and 'Devin' in text
        assert '/docs/library/docs/devin.html#prices' in html
        assert '/docs/library/docs/devin.html#connect' in html
    privacy = Document(bilingual.project_html(usersite.privacy_page(Config()), language)).text
    for phrase in (('tool-handoff records', 'may remain until the slot is cleared', 'verification')
                   if language == 'en' else ('工具交接记录', '保留到槽位被清理', '验证指纹')):
        assert phrase in privacy


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_devin_ready_card_does_not_require_an_unrelated_claude_login(language):
    slot = {'id':'s1', 'node_id':'n1', 'kind':'machine', 'state':'claimed',
            'name':'my-slot', 'unix_user':'slot01'}
    html = usersite._slot_card(slot, {}, None, {}, 'csrf', Config(), 100,
        devin_state='ready', devin_html='<section data-provider="devin">Devin</section>')
    projected = bilingual.project_html(html, language)
    assert 'data-state="claimed"' in projected  # Authentication state is not falsified.
    assert ('Devin ready' if language == 'en' else 'Devin 已就绪') in projected
    assert '<details class="provider-other">' in projected
    assert ('Anthropic sign-in (optional)' if language == 'en' else 'Anthropic 登录（可选）') in projected
