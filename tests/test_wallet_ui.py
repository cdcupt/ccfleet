"""Pure financial UI: exact values, explicit consent, owner privacy and no fake top-up."""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import quote

import pytest

from ccfleetd import wallet, wallet_ui
from ccfleetd.bilingual import project_html
from tests.test_html_document_preservation import Document, scrollable

CSRF = 'csrf "<&'
NONCE = 'server-nonce.abc-123'


class Forms(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.forms, self.elements = [], []
        self.current = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.elements.append((tag, attrs))
        if tag == 'form':
            assert self.current is None, 'forms must not nest'
            self.current = {'attrs': attrs, 'inputs': []}
            self.forms.append(self.current)
        if tag == 'input' and self.current is not None:
            self.current['inputs'].append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


def purchase(**changes):
    values = dict(machine_number=7, expected_cents=2050, balance_cents=3000,
                  csrf=CSRF, idempotency_key=NONCE)
    return wallet_ui.purchase_confirmation(**{**values, **changes})


def renewal(**changes):
    values = dict(slot_id='slot-1', slot_name='my-slot', claim_id='current-claim',
                  expected_cents=2050, balance_cents=3000, csrf=CSRF, idempotency_key=NONCE)
    return wallet_ui.renewal_confirmation(**{**values, **changes})


@pytest.mark.parametrize('balance,shown', [(0, '$0.00'), (2005, '$20.05'),
    (wallet.MAX_CENTS, '$999999999.99')])
def test_balance_is_exact_and_topup_is_honestly_disabled(balance, shown):
    source = wallet_ui.balance_card(balance)
    page, parsed = Document(source), Forms(source)
    assert shown in page.text and 'Fleet credit' in page.text and 'USD service credit' in page.text
    assert 'Online top-ups are unavailable for this account' in page.text
    assert 'An admin can add Fleet credit' in page.text
    assert not parsed.forms
    [button] = [attrs for tag, attrs in parsed.elements if tag == 'button']
    assert button['type'] == 'button' and 'disabled' in button and 'onclick' not in button
    assert page.links == ['/account/available']
    assert not any(tag in {'script', 'iframe'} for tag, _ in page.tags)


def test_explicit_ready_topup_posts_only_server_nonces_and_fixed_usd_amount():
    source = wallet_ui.balance_card(3000, topup_ready=True, csrf=CSRF, idempotency_key=NONCE)
    parsed, page = Forms(source), Document(source)
    [form] = parsed.forms
    assert form['attrs']['action'] == '/account/topup' and form['attrs']['method'] == 'post'
    assert {item['name']: item['value'] for item in form['inputs']} == {
        'csrf': CSRF, 'idempotency_key': NONCE}
    assert all(item['type'] == 'hidden' for item in form['inputs'])
    [select] = [attrs for tag, attrs in page.tags if tag == 'select']
    assert select['name'] == 'amount_cents'
    options = [attrs for tag, attrs in page.tags if tag == 'option']
    assert [item['value'] for item in options] == ['1000', '2000', '5000', '10000']
    assert [item['value'] for item in options if 'selected' in item] == ['2000']
    assert all(value in page.text for value in ('$10.00', '$20.00', '$50.00', '$100.00'))
    buttons = [attrs for tag, attrs in page.tags if tag == 'button']
    assert len(buttons) == 2
    assert buttons[0]['type'] == 'submit' and 'disabled' not in buttons[0]
    assert buttons[1]['type'] == 'button' and 'disabled' in buttons[1]
    assert buttons[1]['aria-describedby'] == 'wallet-alipay-help'
    assert 'Pay on Stripe' in page.text and 'No automatic charges' in page.text
    assert 'Alipay is not available for Fleet credit' in page.text
    assert 'Online top-ups are unavailable for this account' not in page.text
    assert page.links == ['/account/available']
    assert not any(key.startswith('on') for _, attrs in page.tags for key in attrs)
    assert not {'script', 'iframe'} & {tag for tag, _ in page.tags}


@pytest.mark.parametrize('changes', [
    {'topup_ready': 'true'}, {'topup_ready': 1}, {'topup_ready': None},
    {'csrf': ''}, {'csrf': None}, {'csrf': 'bad\nnonce'}, {'csrf': 'x' * 1025},
    {'idempotency_key': ''}, {'idempotency_key': 'browser nonce <script>'},
])
def test_active_topup_requires_explicit_readiness_and_valid_server_context(changes):
    values = dict(topup_ready=True, csrf=CSRF, idempotency_key=NONCE)
    with pytest.raises(ValueError):
        wallet_ui.balance_card(3000, **{**values, **changes})


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_active_card_topup_is_single_language_and_alipay_stays_inactive(language):
    source = wallet_ui.balance_card(3000, topup_ready=True, csrf=CSRF, idempotency_key=NONCE)
    projected = project_html(source, language, target='/account')
    text = Document(projected).text
    expected = ('Pay by card', 'Amount · USD', 'Alipay is not available for Fleet credit')
    removed = ('银行卡充值', '金额 · 美元', 'Fleet 余额暂不支持支付宝充值')
    if language == 'zh':
        expected, removed = removed, expected
    assert all(value in text for value in expected)
    assert all(value not in text for value in removed)
    assert len(Forms(projected).forms) == 1


@pytest.mark.parametrize('topup_ready', [False, True])
def test_compact_existing_customer_card_keeps_balance_actions_but_skips_first_purchase_guide(
        topup_ready):
    source = wallet_ui.balance_card(3000, compact=True, topup_ready=topup_ready,
                                   csrf=CSRF, idempotency_key=NONCE)
    parsed, text = Forms(source), Document(source).text
    assert '$30.00' in text and 'Fleet credit' in text and 'Choose a slot' in text
    assert 'Review the monthly price' not in text and 'Buy the first month' not in text
    assert not any(tag == 'ol' for tag, _ in parsed.elements)
    assert len(parsed.forms) == int(topup_ready)
    assert 'wallet-steps' in wallet_ui.balance_card(3000)


@pytest.mark.parametrize('renderer,action,targets', [
    (purchase, '/account/purchase', {'machine_number': '7'}),
    (renewal, '/account/renew', {'slot_id': 'slot-1', 'claim_id': 'current-claim'}),
])
def test_confirmation_submits_only_expected_financial_targets_and_server_nonces(renderer, action,
                                                                              targets):
    source = renderer()
    page = Document(source)
    [form] = Forms(source).forms
    assert form['attrs']['method'] == 'post' and form['attrs']['action'] == action
    fields = {item['name']: item for item in form['inputs']}
    assert set(fields) == {'csrf', 'idempotency_key', 'expected_cents', *targets}
    assert {key: item['value'] for key, item in fields.items()} == {
        'csrf': CSRF, 'idempotency_key': NONCE, 'expected_cents': '2050', **targets}
    assert all(item['type'] == 'hidden' for item in fields.values())
    assert '$30.00' in page.text and '$20.50' in page.text and '$9.50' in page.text
    assert 'One calendar month in UTC' in page.text
    assert 'Renew manually' in page.text and 'No automatic charges' in page.text
    assert all(not key.startswith('on') for _, attrs in page.tags for key in attrs)
    assert not {'script', 'img', 'svg'} & {tag for tag, _ in page.tags}


@pytest.mark.parametrize('renderer', [purchase, renewal])
@pytest.mark.parametrize('balance', [0, 2049])
def test_insufficient_credit_has_no_active_financial_form(renderer, balance):
    source = renderer(balance_cents=balance)
    parsed = Forms(source)
    assert not parsed.forms
    [button] = [attrs for tag, attrs in parsed.elements if tag == 'button']
    assert button['type'] == 'button' and 'disabled' in button
    assert 'Not enough credit' in Document(source).text
    assert 'Credit left' not in Document(source).text
    assert 'You need ' in Document(source).text


@pytest.mark.parametrize('value', [True, 20.5, '2050', -1, 0, wallet.MAX_CENTS + 1])
def test_purchase_never_accepts_rounded_or_invalid_expected_money(value):
    with pytest.raises(wallet.WalletError):
        purchase(expected_cents=value)


@pytest.mark.parametrize('value', [True, 20.5, '3000', wallet.MAX_CENTS + 1])
def test_balance_never_coerces_unknown_money_to_zero(value):
    with pytest.raises(wallet.WalletError):
        wallet_ui.balance_card(value)
    with pytest.raises(wallet.WalletError):
        purchase(balance_cents=value)


@pytest.mark.parametrize('value', [0, True, 1.5, '<script>7</script>'])
def test_only_a_real_public_machine_number_is_submitted(value):
    with pytest.raises(ValueError):
        purchase(machine_number=value)


def test_renewal_escapes_visible_name_and_hidden_claim_and_ignores_private_order_details():
    name = '<script>alert("slot")</script>'
    source = renewal(slot_name=name, slot_id='slot"<&', claim_id='claim"<&', current_order={
        'period_end_at': 1700000000.0, 'id': 'PRIVATE_ORDER', 'account_id': 'PRIVATE_ACCOUNT',
        'offer_snapshot': 'PRIVATE_COSTS', 'claim_generation': 'PRIVATE_GENERATION',
        'actor_id': 'PRIVATE_ACTOR', 'reason': 'PRIVATE_NOTE'})
    page = Document(source)
    assert name in page.text and '2023-11-14 22:13 UTC' in page.text
    assert 'If you still have time left, the new month starts after it' in page.text
    assert '<script>' not in source and not any(tag == 'script' for tag, _ in page.tags)
    assert all(marker not in source for marker in ('PRIVATE_ORDER', 'PRIVATE_ACCOUNT',
        'PRIVATE_COSTS', 'PRIVATE_GENERATION', 'PRIVATE_ACTOR', 'PRIVATE_NOTE'))
    fields = {item['name']: item['value'] for item in Forms(source).forms[0]['inputs']}
    assert fields['slot_id'] == 'slot"<&' and fields['claim_id'] == 'claim"<&'


def test_owner_ledger_shows_only_generic_date_activity_and_signed_USD_amount():
    rows = [dict(id='PRIVATE_ROW', account_id='PRIVATE_ACCOUNT', kind='admin_credit',
        delta_cents=2005, currency='USD', created_at=1700000000, actor_kind='PRIVATE_KIND',
        actor_id='PRIVATE_ACTOR', reason='<script>PRIVATE_REASON</script>',
        idempotency_key='PRIVATE_OPERATION', offer_snapshot='PRIVATE_OFFER',
        claim_id='PRIVATE_CLAIM', claim_generation='PRIVATE_GENERATION')]
    rows += [dict(kind='slot_purchase', delta_cents=-50, currency='USD', created_at=1700000001)] * 9
    source = wallet_ui.owner_ledger(rows)
    page = Document(source)
    assert '+$20.05' in page.text and '-$0.50' in page.text
    assert 'Credit added' in page.text and 'Slot month' in page.text
    assert '2023-11-14 22:13 UTC' in page.text
    assert 'PRIVATE_' not in source and not any(tag == 'script' for tag, _ in page.tags)
    assert sum(tag == 'tr' for tag, _ in page.tags) == 9  # header + eight recent rows
    assert not Forms(source).forms
    assert page.tables and all(scrollable(parents, wallet_ui.CSS) for parents in page.tables)
    regions = [attrs for tag, attrs in page.tags if attrs.get('class') == 'wallet-ledger-sheet']
    assert len(regions) == 1 and regions[0]['role'] == 'region' and regions[0]['tabindex'] == '0'


def test_unknown_ledger_values_are_unavailable_not_fake_USD_or_raw_HTML():
    source = wallet_ui.owner_ledger([
        {'kind': '<svg onload=attack()>', 'delta_cents': 10000, 'currency': 'CNY',
         'created_at': '<img src=x>'},
        {'kind': {}, 'delta_cents': '<b>$99</b>', 'currency': 'USD', 'created_at': None},
    ])
    page = Document(source)
    assert '$100.00' not in page.text and '$99' not in page.text and 'CNY' not in page.text
    assert 'Credit adjustment' in page.text and '—' in page.text
    assert not {'svg', 'img', 'script'} & {tag for tag, _ in page.tags}
    assert 'onload' not in source


def test_empty_owner_ledger_is_short_and_no_unconfigured_payment_link_is_active():
    source = wallet_ui.owner_ledger([])
    assert 'No credit activity yet' in Document(source).text
    assert not Document(source).tables and not Document(source).links
    assert 'checkout' not in source and 'stripe' not in source


def test_admin_credit_form_is_an_auditable_manual_grant_not_a_payment_or_actor_override():
    account = 'acct/<script>"&'
    source = wallet_ui.admin_credit_grant_form(account_id=account, csrf=CSRF,
                                             idempotency_key=NONCE)
    parsed, page = Forms(source), Document(source)
    [form] = parsed.forms
    assert form['attrs']['method'] == 'post'
    assert form['attrs']['action'] == '/actions/account/' + quote(account, safe='') + '/credit'
    fields = {item['name']: item for item in form['inputs']}
    assert set(fields) == {'csrf', 'idempotency_key', 'amount', 'reason'}
    assert fields['csrf']['value'] == CSRF and fields['idempotency_key']['value'] == NONCE
    assert fields['amount']['type'] == 'text' and fields['amount']['inputmode'] == 'decimal'
    assert fields['amount']['maxlength'] == '12' and 'required' in fields['amount']
    assert fields['reason']['maxlength'] == str(wallet.REASON_MAX) and 'required' in fields['reason']
    assert 'Manual service credit' in page.text and 'private admin reason' in page.text
    assert not Document(source).links and 'PRIVATE_ACTOR' not in source
    assert not {'script', 'img', 'iframe'} & {tag for tag, _ in page.tags}


@pytest.mark.parametrize('renderer', [purchase, renewal])
def test_caller_must_supply_valid_stable_idempotency_context(renderer):
    with pytest.raises(wallet.WalletError):
        renderer(idempotency_key='not a server nonce <script>')


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_every_component_projects_one_language_with_clear_financial_copy(language):
    components = [wallet_ui.balance_card(3000), wallet_ui.owner_ledger([
        dict(kind='admin_credit', delta_cents=3000, currency='USD', created_at=1700000000)]),
        purchase(), renewal(), wallet_ui.admin_credit_grant_form(account_id='alice', csrf=CSRF,
                                                               idempotency_key=NONCE)]
    projected = [project_html(source, language, target='/account') for source in components]
    visible = ' '.join(Document(source).text for source in projected)
    omitted = 'copy-zh' if language == 'en' else 'copy-en'
    assert all(omitted not in source for source in projected)
    own = ('Fleet credit', 'Buy first month', 'No automatic charges', 'Private reason')
    other = ('Fleet 余额', '购买首月', '不会自动扣款', '内部备注')
    if language == 'zh':
        own, other = other, own
    assert all(phrase in visible for phrase in own)
    assert all(phrase not in visible for phrase in other)
    for source in projected:
        assert not {'script', 'iframe'} & {tag for tag, _ in Document(source).tags}


def test_component_CSS_is_scoped_and_provides_keyboard_focus_and_narrow_screen_wrapping():
    assert 'body{' not in wallet_ui.CSS and 'html{' not in wallet_ui.CSS and ':root' not in wallet_ui.CSS
    assert '.wallet-ui .wallet-ledger-sheet:focus-visible' in wallet_ui.CSS
    assert 'overflow-x:auto' in wallet_ui.CSS and 'min-width:0' in wallet_ui.CSS
    assert '@media(max-width:' in wallet_ui.CSS and 'overflow-wrap:anywhere' in wallet_ui.CSS


@pytest.mark.parametrize('kind,en,zh',[
    ('admin_debit','Credit removed','余额扣减'),
    ('purchase_return','Service credit returned','服务余额退回'),
    ('stripe_topup','Online top-up','在线充值'),
])
def test_known_credit_adjustments_have_generic_owner_labels_without_private_notes(kind,en,zh):
    source=wallet_ui.owner_ledger([dict(kind=kind,delta_cents=-100 if kind=='admin_debit' else 100,
        currency='USD',created_at=1700000000,reason='PRIVATE_ADMIN_NOTE',actor_id='PRIVATE_ADMIN',
        id='PRIVATE_REFERENCE',payment_intent_id='PRIVATE_STRIPE_ID')])
    assert en in Document(project_html(source,'en',target='/account')).text
    assert zh in Document(project_html(source,'zh',target='/account')).text
    assert 'PRIVATE_' not in source and not Forms(source).forms
