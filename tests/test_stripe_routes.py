"""Synthetic payment request boundaries; no actual Stripe or HTTP network calls."""
from __future__ import annotations

import io
import urllib.request
from email.message import Message
from types import SimpleNamespace

import pytest

from ccfleetd import stripe_billing, stripe_checkout, stripe_routes
from ccfleetd.bilingual import project_html
from tests.test_html_document_preservation import Document
from tests.test_stripe_billing import Client, paid_object, signed
from tests.test_wallet_store import NOW
from tests.test_wallet_store import store as store

SESSION = 'cs_test_SyntheticBoundary'
URL = 'https://checkout.stripe.com/c/pay/' + SESSION
FORM = {'csrf': 'already-verified-by-caller', 'idempotency_key': 'server-operation-1',
        'amount_cents': '2000'}


@pytest.fixture(autouse=True)
def forbid_actual_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('only injected synthetic billing is allowed')
    monkeypatch.setattr(urllib.request, 'build_opener', denied)
    monkeypatch.setattr(urllib.request, 'urlopen', denied)


def config(**overrides):
    return SimpleNamespace(**{'broker_only': False, 'usd_slot_credit_enabled': True,
        'stripe_topup_enabled': True, 'stripe_product_approved': True,
        'stripe_mode': 'test', **overrides})


class FakeBilling:
    def __init__(self, *, result=None, failure=None, webhook_result='credited'):
        self.result = result if result is not None else stripe_checkout.HostedSession(SESSION, URL)
        self.failure, self.webhook_result, self.calls = failure, webhook_result, []

    def start_checkout(self, account_id, amount, *, idempotency_key, now):
        self.calls.append(('start', account_id, amount, idempotency_key, now))
        if self.failure is not None:
            raise self.failure
        return self.result

    def handle_webhook(self, raw_body, signature, *, now):
        self.calls.append(('webhook', raw_body, signature, now))
        if self.failure is not None:
            raise self.failure
        return self.webhook_result


def start(billing=None, *, cfg=None, account=None, form=None, now=NOW):
    return stripe_routes.start(billing or FakeBilling(), cfg or config(),
        {'id': 'trusted-owner'} if account is None else account,
        FORM if form is None else form, now)


@pytest.mark.parametrize('amount', [1000, 2000, 5000, 10000])
def test_topup_uses_authenticated_account_preset_and_stable_nonce_only(amount):
    fake = FakeBilling()
    outcome = start(fake, form={**FORM, 'amount_cents': str(amount)})
    assert outcome.status == 303 and outcome.location == URL and not outcome.body
    assert fake.calls == [('start', 'trusted-owner', amount, FORM['idempotency_key'], float(NOW))]


@pytest.mark.parametrize('changes', [{'broker_only': True}, {'usd_slot_credit_enabled': False},
    {'stripe_topup_enabled': False}, {'stripe_product_approved': False},
    {'stripe_topup_enabled': 1}, {'usd_slot_credit_enabled': 'true'}])
def test_disabled_or_broker_context_never_calls_billing(changes):
    fake, cfg = FakeBilling(), config(**changes)
    assert not stripe_routes.enabled(fake, cfg)
    assert start(fake, cfg=cfg).status == 503
    assert stripe_routes.webhook(fake, cfg, b'raw', 'synthetic-signature', NOW)[0] == 503
    assert not fake.calls
    assert stripe_routes.enabled(None, config()) is False


@pytest.mark.parametrize('account', [{}, {'id': ''}, {'id': 'private\nidentity'}, [], 'someone'])
def test_missing_server_account_never_uses_a_submitted_identity(account):
    fake = FakeBilling()
    assert start(fake, account=account).status == 401
    assert not fake.calls


@pytest.mark.parametrize('value', ['', '02000', '+2000', '2000.00', '2e3', '-2000',
                                 '2000 ', ' 2000', '２０００', '0', '1999', '10001', 2000, None])
def test_tampered_amount_is_not_coerced_or_sent_to_provider(value):
    fake = FakeBilling()
    assert start(fake, form={**FORM, 'amount_cents': value}).status == 400
    assert not fake.calls


@pytest.mark.parametrize('form', [
    {}, {'csrf': 'valid', 'amount_cents': '2000'}, {**FORM, 'account_id': 'another-user'},
    {**FORM, 'actor_id': 'admin'}, {**FORM, 'session_id': SESSION},
    {**FORM, 'checkout': 'success'}, {**FORM, 'csrf': ''}, {**FORM, 'csrf': 'x' * 1025},
    {**FORM, 'idempotency_key': ''}, {**FORM, 'idempotency_key': 'bad <identity>'},
    {**FORM, 'idempotency_key': ['duplicate', 'field']}, [],
])
def test_unexpected_fields_or_invalid_request_context_do_not_start_checkout(form):
    fake = FakeBilling()
    assert start(fake, form=form).status == 400
    assert not fake.calls


@pytest.mark.parametrize('identity,url', [
    (SESSION, 'http://checkout.stripe.com/c/pay/' + SESSION),
    (SESSION, 'https://checkout.stripe.com.evil.test/c/pay/' + SESSION),
    (SESSION, 'https://checkout.stripe.com@evil.test/c/pay/' + SESSION),
    (SESSION, 'https://evil@checkout.stripe.com/c/pay/' + SESSION),
    (SESSION, 'https://checkout.stripe.com:443/c/pay/' + SESSION),
    (SESSION, URL + '\r\nSet-Cookie: sensitive'),
    (SESSION, 'https://checkout.stripe.com/c/pay/cs_test_Another'),
    ('cs_live_Unexpected', 'https://checkout.stripe.com/c/pay/cs_live_Unexpected'),
    ('cs_test_bad/path', 'https://checkout.stripe.com/c/pay/cs_test_bad/path'),
])
def test_even_injected_billing_cannot_supply_unsafe_or_wrong_mode_redirect(identity, url):
    fake = FakeBilling(result=stripe_checkout.HostedSession(identity, url))
    outcome = start(fake)
    assert outcome.status == 503 and not outcome.location
    assert 'evil.test' not in outcome.body and 'sensitive' not in outcome.body


@pytest.mark.parametrize('failure,status', [
    (stripe_billing.BillingError('PRIVATE_PROVIDER_BODY', status_code=400), 400),
    (stripe_billing.BillingError('PRIVATE_SESSION_URL', status_code=503), 503),
    (stripe_billing.BillingError('PRIVATE_ACCOUNT', status_code=302), 503),
    (RuntimeError('PRIVATE_API_KEY'), 503),
    (stripe_checkout.StripeCheckoutError('PRIVATE_REQUEST', uncertain=True), 503),
])
def test_failure_response_is_fixed_safe_html_and_never_replays(failure, status):
    fake = FakeBilling(failure=failure)
    outcome = start(fake)
    assert outcome.status == status and not outcome.location and 'PRIVATE_' not in outcome.body
    assert len(fake.calls) == 1
    for language in ('en', 'zh'):
        visible = Document(project_html(outcome.body, language, target='/account/topup')).text
        assert ('返回余额页面' if language == 'zh' else 'Back to your balance') in visible
        assert ('Back to your balance' if language == 'zh' else '返回余额页面') not in visible


def headers(*extra, length='3', signature='synthetic-signature'):
    value = Message()
    if length is not None:
        value['Content-Length'] = length
    if signature is not None:
        value['Stripe-Signature'] = signature
    for name, item in extra:
        value[name] = item
    return value


def test_framing_and_exact_read_keep_original_bytes_and_do_not_consume_next_request():
    source = io.BytesIO(b'\x00\xff xnext-request')
    length, signature = stripe_routes.webhook_frame(headers(length='4'))
    assert stripe_routes.read_webhook_body(source, length) == b'\x00\xff x'
    assert source.read() == b'next-request' and signature == 'synthetic-signature'


@pytest.mark.parametrize('extra', [
    ('content-length', '3'), ('Stripe-Signature', 'another-signature'),
    ('Transfer-Encoding', 'chunked'), ('Transfer-Encoding', ''),
    ('Content-Encoding', 'gzip'), ('Content-Encoding', 'identity'), ('Expect', '100-continue'),
])
def test_duplicate_authority_or_unsupported_framing_is_refused(extra):
    with pytest.raises(stripe_routes.WebhookRequestError) as caught:
        stripe_routes.webhook_frame(headers(extra))
    assert caught.value.status == 400


def test_duplicate_nonfinancial_header_is_also_refused():
    with pytest.raises(stripe_routes.WebhookRequestError):
        stripe_routes.webhook_frame(headers(('X-Trace', 'one'), ('x-trace', 'two')))


@pytest.mark.parametrize('length', ['0', '03', '+3', '-3', ' 3', '3 ', '3,3', '٣',
                                   '1e3', '100000000000000'])
def test_length_must_be_one_bounded_canonical_ascii_integer(length):
    with pytest.raises(stripe_routes.WebhookRequestError) as caught:
        stripe_routes.webhook_frame(headers(length=length))
    assert caught.value.status == 400


def test_missing_length_and_over_limit_have_specific_safe_status():
    for length, expected in [(None, 411), (str(stripe_routes.MAX_BODY + 1), 413)]:
        with pytest.raises(stripe_routes.WebhookRequestError) as caught:
            stripe_routes.webhook_frame(headers(length=length))
        assert caught.value.status == expected
        assert str(caught.value) == 'invalid payment notification framing'


@pytest.mark.parametrize('signature', [None, '', 'x' * 4097, 'non-ascii-签名',
                                     'bad\r\nsignature', 'bad\x00signature'])
def test_signature_header_must_be_single_clean_and_bounded(signature):
    with pytest.raises(stripe_routes.WebhookRequestError):
        stripe_routes.webhook_frame(headers(signature=signature))


def test_partial_stream_reads_are_joined_without_changing_signed_bytes():
    class Partial:
        def __init__(self):
            self.source = io.BytesIO(b'{ "exact": true }')

        def read(self, size):
            return self.source.read(min(2, size))

    assert stripe_routes.read_webhook_body(Partial(), 17) == b'{ "exact": true }'


@pytest.mark.parametrize('length', [True, 0, -1, '3', stripe_routes.MAX_BODY + 1])
def test_read_cannot_start_with_unbounded_or_invalid_length(length):
    class Forbidden:
        def read(self, maximum):
            pytest.fail('bad length was read')
    with pytest.raises(stripe_routes.WebhookRequestError):
        stripe_routes.read_webhook_body(Forbidden(), length)


@pytest.mark.parametrize('value', [b'', b'ab', 'abc', bytearray(b'abc'), b'abcd'])
def test_truncated_or_malformed_stream_never_reaches_signature_verification(value):
    class Truncated:
        def __init__(self):
            self.called = False

        def read(self, maximum):
            if self.called:
                return b''
            self.called = True
            return value

    with pytest.raises(stripe_routes.WebhookRequestError):
        stripe_routes.read_webhook_body(Truncated(), 3)


def test_deadline_or_disconnection_errors_are_safe_framing_failures():
    class Timeout:
        def read(self, maximum):
            raise TimeoutError('PRIVATE_SOCKET_DETAILS')
    with pytest.raises(stripe_routes.WebhookRequestError) as caught:
        stripe_routes.read_webhook_body(Timeout(), 3)
    assert 'PRIVATE_' not in str(caught.value)


@pytest.mark.parametrize('result', ['credited', 'held', 'ignored'])
def test_webhook_ack_is_static_and_passes_unchanged_raw_bytes(result):
    fake = FakeBilling(webhook_result=result)
    raw = b' { "SYNTHETIC_PRIVATE_SESSION": "test" } '
    assert stripe_routes.webhook(fake, config(), raw, 'synthetic-signature', NOW) == (
        200, {'received': True})
    assert fake.calls == [('webhook', raw, 'synthetic-signature', float(NOW))]


@pytest.mark.parametrize('raw,signature,expected', [
    (b'', 'sig', 400), ('not-bytes', 'sig', 400), (b'x' * (stripe_routes.MAX_BODY + 1), 'sig', 413),
    (b'raw', '', 400), (b'raw', 'x' * 4097, 400), (b'raw', 'bad\nheader', 400),
])
def test_invalid_webhook_bounds_are_refused_without_billing(raw, signature, expected):
    fake = FakeBilling()
    status, body = stripe_routes.webhook(fake, config(), raw, signature, NOW)
    assert status == expected and body == {'error': 'invalid payment notification'}
    assert not fake.calls


@pytest.mark.parametrize('failure,status', [
    (stripe_billing.BillingError('PRIVATE_BAD_HMAC', status_code=400), 400),
    (stripe_billing.BillingError('PRIVATE_DATABASE_DETAILS'), 503),
    (RuntimeError('PRIVATE_SIGNING_SECRET'), 503),
])
def test_unverified_or_unconfirmed_notification_returns_safe_retry_status(failure, status):
    fake = FakeBilling(failure=failure)
    actual, body = stripe_routes.webhook(fake, config(), b'raw', 'synthetic-signature', NOW)
    assert actual == status and 'PRIVATE_' not in str(body)
    assert len(fake.calls) == 1


def test_unknown_billing_result_is_not_acknowledged():
    fake = FakeBilling(webhook_result='PRIVATE_PROVIDER_DATA')
    status, body = stripe_routes.webhook(fake, config(), b'raw', 'synthetic-signature', NOW)
    assert status == 503 and 'PRIVATE_' not in str(body)


def test_real_coordinator_with_fake_provider_authenticates_raw_body_and_credits_once(store):
    billing = stripe_billing.Billing(store, Client(), sandbox_database=True)
    outcome = stripe_routes.start(billing, config(), {'id': 'alice'}, FORM, NOW)
    assert outcome.status == 303 and store.wallet_balance('alice') == 0
    [local] = billing.client.orders.values()
    hosted = stripe_checkout.HostedSession('cs_test_' + local.id, outcome.location)
    raw, signature = signed(billing, paid_object(billing, hosted))
    before = list(billing.client.calls)
    # The signature does not authorize changed bytes or a copied browser-return value.
    assert stripe_routes.webhook(billing, config(), raw + b' ', signature, NOW)[0] == 400
    assert store.wallet_balance('alice') == 0 and billing.client.calls == before
    for _ in range(2):
        assert stripe_routes.webhook(billing, config(), raw, signature, NOW) == (
            200, {'received': True})
    assert store.wallet_balance('alice') == 2000
    assert len(store.list_wallet_entries('alice')) == 1
