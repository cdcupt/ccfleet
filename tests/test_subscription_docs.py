"""Payment instructions follow enabled choices and retain existing contract terms."""
from __future__ import annotations

import pytest

from ccfleetd import bilingual, customer_docs
from ccfleetd.config import Config
from tests.test_account_setup_flow import Flow
from tests.test_html_document_preservation import Document


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_getting_started_changes_payment_steps_only_after_monthly_enrollment_is_enabled(language):
    expected = (['Sign in', 'Choose how to pay', 'Review and confirm'] if language == 'en' else
                ['登录本站', '选择付费方式', '核对并确认'])
    for wallet in (False, True):
        source = customer_docs.guide(Config(stripe_subscriptions_enabled=True),
                                     wallet_enabled=wallet, per_slot_pricing=True)
        projected = bilingual.project_html(source, language, '/docs/guide')
        page = Flow(projected)
        [steps] = page.find('ol', **{'class': 'steps'})
        direct = [item for item in page.inside(steps, 'li') if item.parents[-1] is steps]
        assert [page.inside(item, 'h3')[0].text for item in direct[:3]] == expected
        assert page.find('a', href=('/zh' if language == 'zh' else '') + '/account/available')
        assert not page.find('form')  # Documentation never starts or enrolls a payment.
        assert not Document(projected).duplicate_ids
    disabled = bilingual.project_html(customer_docs.guide(Config(), wallet_enabled=True),
                                       language, '/docs/guide')
    disabled_page = Flow(disabled)
    [steps] = disabled_page.find('ol', **{'class': 'steps'})
    direct = [item for item in disabled_page.inside(steps, 'li') if item.parents[-1] is steps]
    assert disabled_page.inside(direct[1], 'h3')[0].text == (
        'Add service credit' if language == 'en' else '添加服务余额')
    assert ('Continue to secure payment' if language == 'en' else '前往安全付款页面') in disabled


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('language', ['en', 'zh'])
def test_monthly_terms_stay_readable_when_new_enrollment_is_disabled(enabled, language):
    source = customer_docs.terms(Config(stripe_subscriptions_enabled=enabled), wallet_enabled=True)
    projected = bilingual.project_html(source, language, '/docs/terms')
    page = Document(projected)
    assert 'monthly-payments' in page.ids
    assert ('/zh' if language == 'zh' else '') + '/account/subscriptions' in page.links
    assert not Flow(projected).find('form')
    assert not page.duplicate_ids
    if language == 'en':
        assert 'When offered' in page.text and 'independent monthly plans' in page.text
        assert 'not enrolled or converted automatically' in page.text
        assert 'cancel' in page.text.lower() and 'paid period' in page.text
    else:
        assert '提供此选项时' in page.text and '独立的月付计划' in page.text
        assert '不会自动转换' in page.text and '已付款的服务期仍保留到期' in page.text
