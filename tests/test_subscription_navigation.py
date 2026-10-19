"""Optional monthly enrollment is discoverable without hiding existing management."""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from ccfleetd import offer_ui, slots, usersite, wallet_access
from ccfleetd.bilingual import project_html
from tests.test_account_setup_flow import Flow
from tests.test_cli_access import public_key
from tests.test_gateway_provider_controls import configured
from tests.test_slot_browser import ACCOUNT, NOW, Inventory
from tests.test_wallet_http import financial, form_from, grant, quote
from tests.test_wallet_http import web as web  # noqa: F401 -- real local HTTP fixture


def test_monthly_default_off_preserves_the_existing_browse_markup_and_actions():
    inventory = Inventory(1)
    before = offer_ui.browse(inventory, ACCOUNT, 'csrf', NOW, 90, {}, purchase=True)
    explicit = offer_ui.browse(inventory, ACCOUNT, 'csrf', NOW, 90, {}, purchase=True,
                               monthly=False)
    assert before == explicit
    assert '/account/purchase?machine_number=1' in before
    assert '/account/subscribe' not in before and 'Pay monthly' not in before


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_monthly_is_a_secondary_choice_and_keeps_the_one_month_wallet_purchase(language):
    inventory = Inventory(1)
    raw = offer_ui.browse(inventory, ACCOUNT, 'UNUSED_PRIVATE_CSRF', NOW, 90, {},
                          purchase=True, monthly=True)
    source = project_html(raw, language, target='/account/available')
    prefix = '/zh' if language == 'zh' else ''
    page = Flow(source)
    [one_month] = page.find('a', href=prefix + '/account/purchase?machine_number=1')
    [monthly] = page.find('a', href=prefix + '/account/subscribe?machine_number=1')
    assert one_month.has_class('primary') and monthly.has_class('btn')
    assert not monthly.has_class('primary')
    assert ('按月付款' if language == 'zh' else 'Pay monthly') in monthly.text
    assert not page.find('form', method='post')
    assert 'UNUSED_PRIVATE_CSRF' not in source


def test_monthly_choice_does_not_require_a_legacy_slot_allowance():
    raw = offer_ui.browse(Inventory(1), ACCOUNT, 'UNUSED_PRIVATE_CSRF', NOW, 90, {},
                          can_claim=False, monthly=True)
    page = Flow(raw)
    assert page.find('a', href='/account/subscribe?machine_number=1')
    assert not page.find('form', method='post')
    assert '/account/claim' not in raw and '/account/purchase' not in raw
    assert 'UNUSED_PRIVATE_CSRF' not in raw
    assert any('Action' in item.text for item in page.find('th', scope='col'))


@pytest.mark.parametrize('enabled', [False, True])
def test_actual_account_and_available_pages_gate_new_monthly_links_without_any_purchase(web, enabled):
    c = web
    c.ctx.cfg = replace(c.cfg, stripe_subscriptions_enabled=enabled)
    user = c.browser()
    before = financial(c)
    account = user.call('GET', '/account', headers=c.public)
    available = user.call('GET', '/account/available', headers=c.public)
    assert account.status == available.status == 200
    assert bool(Flow(account.body).find('a', href='/account/subscriptions')) is enabled
    assert bool(Flow(available.body).find('a', href='/account/subscribe?machine_number=1')) is enabled
    assert Flow(available.body).find('a', href='/account/purchase?machine_number=1')
    assert c.store.list_slot_subscriptions('alice') == []
    assert financial(c) == before


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_existing_own_subscription_management_remains_when_enrollment_is_off(web, language):
    c = web
    assert c.cfg.stripe_subscriptions_enabled is False
    c.store.reserve_slot_subscription('alice', 'node-a', expected_cents=1000,
        idempotency_key='synthetic-monthly-existing', merchant_id='acct_synthetic',
        livemode=False, now=time.time(), max_age_s=90, monthly_consent=True)
    before = c.store.list_slot_subscriptions('alice')
    user = c.browser()
    prefix = '/' + language
    page = user.call('GET', prefix + '/account', headers=c.public)
    [link] = Flow(page.body).find('a', href=('/zh' if language == 'zh' else '')
                                + '/account/subscriptions')
    assert ('月付管理' if language == 'zh' else 'Monthly payments') in link.text
    assert 'node-a' not in link.attrs['href']
    assert c.store.list_slot_subscriptions('alice') == before
    foreign = c.browser('bob').call('GET', prefix + '/account', headers=c.public)
    assert not Flow(foreign.body).find('a', href=('/zh' if language == 'zh' else '')
                                     + '/account/subscriptions')


def paid_monthly(c):
    """Create one actual synthetic paid recurring claim without any provider calls."""
    now = time.time()
    common = dict(merchant_id='acct_synthetic', livemode=False, now=now)
    order = c.store.reserve_slot_subscription('alice', 'node-a', expected_cents=1000,
        idempotency_key='synthetic-monthly-paid', max_age_s=90, monthly_consent=True, **common)
    c.store.prepare_subscription_customer('alice', **common)
    c.store.bind_subscription_customer('alice', customer_id='cus_synthetic', **common)
    c.store.attach_slot_subscription_session(order['id'], session_id='cs_test_synthetic',
        expires_at=now + 1800, now=now)
    c.store.bind_slot_subscription(order['id'], session_id='cs_test_synthetic',
        subscription_id='sub_synthetic', customer_id='cus_synthetic', now=now)
    c.store.record_slot_subscription_invoice(order['id'], invoice_id='in_synthetic',
        payment_intent_id='pi_synthetic', subscription_id='sub_synthetic',
        customer_id='cus_synthetic', currency='USD', amount_cents=1000,
        period_start_at=now - 10, period_end_at=now + 3600, paid=True,
        event_id='evt_synthetic', body_sha256='a' * 64, **common)
    c.store.move_slot('slot-node-a', slots.CLAIMED)
    c.store.apply_slot_report('node-a', [{'unix_user': 'slot01', 'present': True}], now=now)
    return order


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_recurring_slot_explains_cancel_first_and_guarded_return_leaves_paid_claim_untouched(web,
                                                                                         language):
    c = web
    paid_monthly(c)
    user = c.browser()
    prefix = '/zh' if language == 'zh' else ''
    page = user.call('GET', prefix + '/account', headers=c.public)
    parsed = Flow(page.body)
    [note] = parsed.find('p', **{'class': 'slot-monthly-return-note'})
    assert not any(parent.tag == 'details' for parent in note.parents)
    [manage] = parsed.inside(note, 'a')
    assert manage.attrs['href'] == prefix + '/account/subscriptions'
    assert not parsed.find('a', href=prefix + '/account/renew?slot_id=slot-node-a')
    fields = form_from(page.body, prefix + '/account/slots/slot-node-a/release')
    before = financial(c)
    response = user.call('POST', prefix + '/account/slots/slot-node-a/release', form=fields,
                         headers=c.public)
    assert response.status == 303 and 'note=monthly-release-blocked' in response.getheader('Location')
    assert financial(c) == before and c.store.get_slot('slot-node-a')['state'] == slots.CLAIMED
    refreshed = user.call('GET', response.getheader('Location'), headers=c.public)
    assert ('请先取消月付' if language == 'zh' else 'Cancel monthly billing first') in refreshed.body
    other = c.browser('bob')
    denied = other.call('POST', '/account/slots/slot-node-a/release', form={
        'csrf': usersite.csrf_for(other.session, c.cfg.cookie_secret), 'confirm': 'wipe'},
        headers=c.public)
    assert denied.status == 404 and financial(c) == before


def test_nonrecurring_wallet_slot_keeps_existing_return_controls_and_manual_renewal(web):
    c = web
    grant(c)
    user = c.browser()
    fields = quote(c, user)
    assert user.call('POST', '/account/purchase', form=fields, headers=c.public).status == 303
    c.store.move_slot('slot-node-a', slots.CLAIMED)
    source = user.call('GET', '/account', headers=c.public).body
    page = Flow(source)
    assert not page.find('p', **{'class': 'slot-monthly-return-note'})
    assert page.find('a', href='/account/renew?slot_id=slot-node-a')
    assert page.find('form', action='/account/slots/slot-node-a/release')


@pytest.mark.parametrize('state', ['current', 'expired', 'review-held'])
def test_recurring_connection_instructions_match_actual_paid_admission(web, state):
    c = web
    order = paid_monthly(c)
    now = time.time()
    c.store.move_slot('slot-node-a', slots.ACTIVE)
    c.store.set_node_access('node-a', '192.0.2.7', 2222, public_key(7))
    c.store.insert_heartbeat('node-a', now, {'node_id': 'node-a', 'mode': 'machine', 'slots': [
        {'unix_user': 'slot01', 'present': True, 'credentials': {'logged_in': True}}]})
    row = c.store.get_slot_subscription(order['id'], account_id='alice')
    if state == 'expired':
        now = row['period_end_at'] + 1
    elif state == 'review-held':
        c.store.hold_slot_subscription(order['id'], subscription_id='sub_synthetic',
            event_id='evt_synthetic_review', event_type='charge.refunded',
            body_sha256='b' * 64, now=now)
    slot = c.store.get_slot('slot-node-a')
    admitted = wallet_access.allowed(c.store, slot, now=now)
    assert admitted is (state == 'current')
    account = c.store.get_account('alice')
    source = usersite.page(c.store, configured(c.cfg), account, 'synthetic-ui-session', now)
    parsed = Flow(source)
    assert bool(parsed.find('details', data_slot_connect='anthropic')) is admitted
    if state == 'current':
        assert 'Paid period active' in source
    elif state == 'expired':
        assert 'This monthly rental is paused' in source and '1970-01-01' not in source
    else:
        assert 'Check monthly payment' in source
