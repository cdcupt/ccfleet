"""Recurring choices require consent and do not disclose provider identities."""
from __future__ import annotations

import pytest

from ccfleetd import subscription_ui, wallet
from ccfleetd.bilingual import project_html
from tests.test_account_setup_flow import Flow
from tests.test_html_document_preservation import Document

CSRF = 'synthetic-csrf"<&'
NONCE = 'monthly-synthetic.abc-123'


def review(**changes):
    values = dict(machine_number=7, monthly_cents=1025, csrf=CSRF, idempotency_key=NONCE)
    return subscription_ui.review(**{**values, **changes})


def order(**changes):
    values = dict(account_id='PRIVATE_OWNER', node_id='PRIVATE_NODE', slot_id='PRIVATE_SLOT',
        amount_cents=1025, currency='USD', status='active', provider_status='active',
        cancel_at_period_end=0, claim_id='PRIVATE_CLAIM', session_id='cs_PRIVATE_SESSION',
        expires_at=1700000500, subscription_id='sub_PRIVATE_SUBSCRIPTION',
        customer_id='cus_PRIVATE_CUSTOMER', id='PRIVATE_LOCAL_ORDER',
        offer_snapshot='PRIVATE_PRICING', idempotency_key='PRIVATE_NONCE')
    return {**values, **changes}


def inputs(page, form):
    return {item.attrs['name']: item.attrs for item in page.inside(form, 'input')}


def test_review_has_one_explicit_recurring_consent_and_exact_authenticated_POST_fields():
    source = review()
    page = Flow(source)
    [form] = page.find('form')
    assert form.attrs == {'method': 'post', 'action': '/account/subscribe'}
    fields = inputs(page, form)
    assert set(fields) == {'csrf', 'machine_number', 'expected_cents', 'idempotency_key',
                           'monthly_consent'}
    assert {name: item['value'] for name, item in fields.items()} == {
        'csrf': CSRF, 'machine_number': '7', 'expected_cents': '1025',
        'idempotency_key': NONCE, 'monthly_consent': 'yes'}
    consent = fields.pop('monthly_consent')
    assert consent['type'] == 'checkbox' and 'required' in consent and 'checked' not in consent
    assert all(item['type'] == 'hidden' for item in fields.values())
    [button] = page.inside(form, 'button')
    assert button.attrs['type'] == 'submit'
    [terms] = page.inside(form, 'a')
    assert terms.attrs['href'] == '/docs/terms'
    assert '$10.25' in form.text and 'each month' in form.text and 'until I cancel' in form.text
    assert 'Fleet credit is not used' in Document(source).text
    assert 'paid separately' in Document(source).text
    assert not any(item.attrs.get('type') == 'hidden' and item.attrs.get('name') in {
        'customer_id', 'subscription_id', 'account_id', 'slot_id', 'node_id'}
        for item in page.elements)


def test_summary_manages_multiple_slots_with_one_CSRF_only_POST_and_no_provider_identifiers():
    source = subscription_ui.summary([order(machine_number=7), order(
        machine_number=8, amount_cents=5000, cancel_at_period_end=1)], csrf=CSRF)
    page = Flow(source)
    cards = [item for item in page.find('article') if item.has_class('subscription-card')]
    assert len(cards) == 2
    assert 'Machine 7' in cards[0].text and '$10.25' in cards[0].text
    assert 'Machine 8' in cards[1].text and '$50.00' in cards[1].text
    assert 'Ends after this period' in cards[1].text
    [form] = page.find('form')
    assert form.attrs == {'method': 'post', 'action': '/account/subscriptions/portal'}
    assert inputs(page, form) == {'csrf': {'type': 'hidden', 'name': 'csrf', 'value': CSRF}}
    assert 'Manage or cancel on Stripe' in form.text
    assert 'PRIVATE_' not in source
    assert not page.find('a')  # Management never happens through a GET link.


def test_unknown_dates_are_honest_and_checkout_expiry_is_not_presented_as_paid_time():
    source = subscription_ui.summary([order(period_end_at=None,
        next_bill_at='<script>PRIVATE_DATE</script>')], csrf=CSRF)
    text = Document(source).text
    assert 'Paid through' in text and 'Not available yet' in text and 'See Stripe' in text
    assert '2023-11-14' not in text and 'PRIVATE_' not in source and '<script>' not in source
    source = subscription_ui.summary([order(period_end_at=1700000000,
        next_bill_at=1701000000)], csrf=CSRF)
    assert '2023-11-14 22:13 UTC' in source and '2023-11-26 12:00 UTC' in source


@pytest.mark.parametrize('state,provider,cancel,expected', [
    ('active', 'active', 0, 'Monthly active'),
    ('active', 'active', 1, 'Ends after this period'),
    ('active', 'past_due', 0, 'Check payment'),
    ('active', 'canceled', 0, 'Renewal stopped'),
    ('closed', 'canceled', 0, 'Ended'),
    ('checkout_open', '', 0, 'Awaiting first payment'),
    ('reserved', '', 0, 'Preparing checkout'),
    ('unknown', '', 0, 'Checking payment'),
    ('<svg onload=attack()>', '<script>PRIVATE_STATUS</script>', 0, 'Status pending'),
    ({}, {}, 0, 'Status pending'),
])
def test_statuses_are_fixed_customer_labels_not_raw_provider_text(state, provider, cancel, expected):
    source = subscription_ui.summary([order(status=state, provider_status=provider,
        cancel_at_period_end=cancel)], csrf=CSRF)
    assert expected in Document(source).text
    assert 'PRIVATE_' not in source and 'onload' not in source and '<script>' not in source


def test_missing_or_malformed_public_metadata_cannot_leak_private_labels_or_money():
    source = subscription_ui.summary([order(machine_number='<img src=x>',
        amount_cents='<script>PRIVATE_MONEY</script>',
        slot_name='PRIVATE_NAME', currency='CNY')], csrf=CSRF)
    assert 'Monthly slot' in Document(source).text and 'Amount unavailable' in Document(source).text
    assert 'PRIVATE_' not in source and '<img' not in source and '<script>' not in source
    assert '$0.00' not in source


def test_empty_summary_has_no_management_action_or_false_automatic_enrollment():
    source = subscription_ui.summary([], csrf='')
    assert not Flow(source).find('form')
    assert 'only when you choose it and finish payment' in Document(source).text


def test_all_pending_or_expired_unpaid_orders_have_no_portal_until_a_subscription_is_bound():
    pending = [order(status='checkout_open', subscription_id=None),
               order(status='closed', subscription_id=None),
               order(status='unknown', subscription_id=None)]
    source = subscription_ui.summary(pending, csrf=CSRF)
    assert not Flow(source).find('form')
    assert 'Awaiting first payment' in Document(source).text
    assert 'Not started' in Document(source).text
    assert 'Stripe subscription portal becomes available after a subscription is confirmed' in (
        Document(source).text)
    assert 'PRIVATE_' not in source
    mixed = subscription_ui.summary(pending + [order()], csrf=CSRF)
    [form] = Flow(mixed).find('form')
    assert form.attrs['action'] == '/account/subscriptions/portal'
    assert 'PRIVATE_' not in mixed


def test_payment_review_status_overrides_active_without_erasing_historical_paid_through():
    source = subscription_ui.summary([order(payment_review_required=True,
        period_end_at=1700000000)], csrf=CSRF)
    assert 'Payment review required' in Document(source).text
    assert 'Monthly active' not in Document(source).text
    assert '2023-11-14 22:13 UTC' in source
    assert not Flow(source).find('form', action='/account/subscriptions/portal')


def test_direct_cancellation_submits_only_local_order_and_server_security_context():
    source = subscription_ui.summary([order(id='local-order-one',
        cancel_idempotency_key=NONCE, payment_review_required=True)], csrf=CSRF)
    page = Flow(source)
    [form] = page.find('form')
    assert form.attrs['method'] == 'post'
    assert form.attrs['action'] == '/account/subscriptions/cancel'
    fields = inputs(page, form)
    assert {name: item['value'] for name, item in fields.items()} == {
        'csrf': CSRF, 'order_id': 'local-order-one', 'idempotency_key': NONCE}
    assert all(item['type'] == 'hidden' for item in fields.values())
    [button] = page.inside(form, 'button')
    assert button.attrs['type'] == 'submit' and 'Cancel renewal' in button.text
    assert 'paid period' in form.text and 'not deleted automatically' in form.text
    assert 'local-order-one' not in Document(source).text
    assert 'PRIVATE_' not in source
    assert not page.find('a')


def test_review_hold_pauses_account_portal_but_keeps_independent_own_plan_cancellation():
    source = subscription_ui.summary([
        order(id='local-order-one', cancel_idempotency_key='cancel-one',
              payment_review_required=True, cancel_required=True),
        order(id='local-order-two', cancel_idempotency_key='cancel-two', machine_number=8),
    ], csrf=CSRF)
    page = Flow(source)
    assert not page.find('form', action='/account/subscriptions/portal')
    forms = page.find('form', action='/account/subscriptions/cancel')
    assert len(forms) == 2
    assert [(inputs(page, form)['order_id']['value'],
             inputs(page, form)['idempotency_key']['value']) for form in forms] == [
        ('local-order-one', 'cancel-one'), ('local-order-two', 'cancel-two')]
    [first_card] = [item for item in page.find('article') if 'Payment review required' in item.text]
    assert 'Cancellation pending' in first_card.text
    assert 'not confirmed as stopped yet' in first_card.text
    assert 'Monthly active' not in first_card.text
    assert 'PRIVATE_' not in source


@pytest.mark.parametrize('changes', [
    {'subscription_id': None}, {'cancel_idempotency_key': None},
    {'cancel_at_period_end': True}, {'provider_status': 'canceled'}, {'status': 'closed'},
])
def test_unbound_unprepared_or_already_canceled_order_has_no_new_cancel_command(changes):
    source = subscription_ui.summary([order(**{'cancel_idempotency_key': NONCE, **changes})],
                                     csrf=CSRF)
    assert not Flow(source).find('form', action='/account/subscriptions/cancel')


def test_cancel_pending_is_not_presented_as_an_already_stopped_renewal():
    source = subscription_ui.summary([order(cancel_required=True,
        cancel_idempotency_key=NONCE)], csrf=CSRF)
    text = Document(source).text
    assert 'Cancellation pending' in text and 'not confirmed as stopped yet' in text
    assert 'No further monthly renewal' not in text
    assert not Flow(source).find('form', action='/account/subscriptions/portal')
    assert Flow(source).find('form', action='/account/subscriptions/cancel')


@pytest.mark.parametrize('value', ['', '<script>nonce</script>', None])
def test_direct_cancel_cannot_submit_a_malformed_server_nonce(value):
    if value is None:
        source = subscription_ui.summary([order(cancel_idempotency_key=value)], csrf=CSRF)
        assert not Flow(source).find('form', action='/account/subscriptions/cancel')
    else:
        with pytest.raises(wallet.WalletError):
            subscription_ui.summary([order(cancel_idempotency_key=value)], csrf=CSRF)


def test_pending_checkout_can_resume_or_cancel_using_only_local_order_and_server_context():
    source = subscription_ui.summary([order(id='local-pending-order', status='checkout_open',
        claim_id='', subscription_id=None, checkout_idempotency_key=NONCE)], csrf=CSRF)
    page = Flow(source)
    forms = page.find('form')
    assert [item.attrs for item in forms] == [
        {'method': 'post', 'action': '/account/subscriptions/resume'},
        {'method': 'post', 'action': '/account/subscriptions/cancel-checkout'}]
    for form in forms:
        fields = inputs(page, form)
        assert {name: item['value'] for name, item in fields.items()} == {
            'csrf': CSRF, 'order_id': 'local-pending-order', 'idempotency_key': NONCE}
        assert all(item['type'] == 'hidden' for item in fields.values())
        [button] = page.inside(form, 'button')
        assert button.attrs['type'] == 'submit'
    assert 'Continue checkout' in forms[0].text and 'Cancel checkout' in forms[1].text
    assert 'PRIVATE_' not in source and not page.find('a')
    text = Document(source).text
    assert 'Stripe confirms it has expired' in text and 'keep that paid month' in text
    assert 'does not automatically refund money' in text
    assert 'local-pending-order' not in text


def test_pending_checkout_review_pauses_resume_but_keeps_cancel_checkout_available():
    source = subscription_ui.summary([order(id='local-pending-order', status='unknown',
        claim_id='', subscription_id=None, checkout_idempotency_key=NONCE,
        payment_review_required=True)], csrf=CSRF)
    page = Flow(source)
    assert not page.find('form', action='/account/subscriptions/resume')
    [form] = page.find('form')
    assert form.attrs['action'] == '/account/subscriptions/cancel-checkout'
    assert inputs(page, form)['order_id']['value'] == 'local-pending-order'
    [paused] = [button for button in page.find('button') if 'disabled' in button.attrs]
    assert paused.attrs['type'] == 'button'
    assert 'Checkout paused for review' in paused.text


@pytest.mark.parametrize('changes', [
    {'status': 'closed'}, {'claim_id': 'PRIVATE_CURRENT_CLAIM'},
    {'checkout_idempotency_key': None},
])
def test_pending_controls_never_apply_to_closed_claimed_or_unprepared_rows(changes):
    row = order(**{'status': 'checkout_open', 'claim_id': '', 'subscription_id': None,
                   'checkout_idempotency_key': NONCE, **changes})
    source = subscription_ui.summary([row], csrf=CSRF)
    assert not Flow(source).find('form', action='/account/subscriptions/resume')
    assert not Flow(source).find('form', action='/account/subscriptions/cancel-checkout')
    assert 'PRIVATE_' not in source


@pytest.mark.parametrize('value', ['', 'bad nonce <script>'])
def test_pending_actions_fail_closed_if_server_nonce_is_malformed(value):
    with pytest.raises(wallet.WalletError):
        subscription_ui.summary([order(status='checkout_open', claim_id='',
            subscription_id=None, checkout_idempotency_key=value)], csrf=CSRF)


def test_read_only_saved_history_never_exposes_mutation_forms_or_provider_identifiers():
    source = subscription_ui.summary([order(status='unknown', claim_id='',
        cancel_idempotency_key=NONCE, checkout_idempotency_key=NONCE)],
        csrf='', management_ready=False)
    assert not Flow(source).find('form')
    assert 'saved payment history' in Document(source).text
    assert 'PRIVATE_' not in source
    assert 'subscription-card' in source


@pytest.mark.parametrize('value', ['true', 1, None])
def test_management_readiness_is_a_server_boolean(value):
    with pytest.raises(ValueError):
        subscription_ui.summary([order()], csrf=CSRF, management_ready=value)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_views_project_single_language_keep_form_context_and_use_native_keyboard_controls(language):
    for raw in (review(), subscription_ui.summary([order()], csrf=CSRF)):
        source = project_html(raw, language, target='/account')
        page = Flow(source)
        assert ('copy-zh' if language == 'en' else 'copy-en') not in source
        [form] = page.find('form')
        assert inputs(page, form)['csrf']['value'] == CSRF
        assert not any(name.startswith('on') for item in page.elements for name in item.attrs)
        assert not {'script', 'iframe', 'img'} & {item.tag for item in page.elements}
    assert 'input:focus-visible' in subscription_ui.CSS
    assert 'grid-template-columns:minmax(0,1fr)' in subscription_ui.CSS
    assert 'body.site:has(.subscription-review) .topbar .doc-nav{display:none}' in subscription_ui.CSS


@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '7', 2**63])
def test_review_requires_a_real_public_machine_number(value):
    with pytest.raises(ValueError):
        review(machine_number=value)


@pytest.mark.parametrize('value', [True, 0, -1, 10.25, '1025', wallet.MAX_CENTS + 1])
def test_review_requires_exact_positive_integer_USD_cents(value):
    with pytest.raises(wallet.WalletError):
        review(monthly_cents=value)


@pytest.mark.parametrize('value', ['', None, 'bad\ncsrf', 'x' * 1025])
def test_active_forms_require_valid_server_CSRF(value):
    with pytest.raises(ValueError):
        review(csrf=value)
    with pytest.raises(ValueError):
        subscription_ui.summary([order()], csrf=value)


@pytest.mark.parametrize('value', ['', '<script>nonce</script>', None])
def test_review_requires_a_valid_server_idempotency_key(value):
    with pytest.raises(wallet.WalletError):
        review(idempotency_key=value)
