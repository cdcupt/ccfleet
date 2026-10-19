"""Synthetic adapter tests only: no Stripe account, Sandbox, payment or network."""
from __future__ import annotations

import copy
import hashlib
import hmac
import io
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

import pytest

from ccfleetd import stripe_checkout as sc

MERCHANT = 'acct_FleetSynthetic'
KEY = 'rk_test_SYNTHETIC_NOT_A_REAL_KEY'
SECRET = 'whsec_SYNTHETIC_NOT_A_REAL_SECRET'
SESSION = 'cs_test_SyntheticCheckout'
INTENT = 'pi_SyntheticPayment'
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('this test must never perform actual network I/O')
    monkeypatch.setattr(urllib.request, 'build_opener', denied)
    monkeypatch.setattr(urllib.request, 'urlopen', denied)


def settings(**overrides):
    return sc.Settings(**{'mode': 'test', 'expected_account_id': MERCHANT,
                          'api_key': KEY, 'webhook_secret': SECRET, **overrides})


def order(**overrides):
    return sc.TopupOrder(**{'id': 'topup.synthetic-1', 'account_id': 'local-owner',
                           'amount_cents': 2000, **overrides})


def metadata(local_order=None, config=None):
    local_order, config = local_order or order(), config or settings()
    return {'ccfleet_order_id': local_order.id, 'ccfleet_account_id': local_order.account_id,
            'ccfleet_merchant_id': config.expected_account_id, 'ccfleet_mode': config.mode,
            'ccfleet_purpose': sc.PURPOSE, 'integration_identifier': config.integration_identifier}


def session(*, paid=False, local_order=None, config=None):
    local_order, config = local_order or order(), config or settings()
    identity = SESSION.replace('_test_', '_live_') if config.livemode else SESSION
    amount = local_order.amount_cents
    return {'id': identity, 'object': 'checkout.session', 'mode': 'payment',
            'ui_mode': 'hosted_page', 'livemode': config.livemode,
            'currency': 'usd', 'amount_total': amount, 'amount_subtotal': amount,
            'client_reference_id': local_order.id, 'metadata': metadata(local_order, config),
            'allowed_payment_method_types': ['card'], 'payment_method_types': ['card'],
            'subscription': None, 'setup_intent': None,
            'automatic_tax': {'enabled': False}, 'adaptive_pricing': {'enabled': False},
            'total_details': {'amount_discount': 0, 'amount_shipping': 0, 'amount_tax': 0},
            'status': 'complete' if paid else 'open',
            'payment_status': 'paid' if paid else 'unpaid', 'payment_intent': INTENT if paid else None,
            'url': None if paid else 'https://checkout.stripe.com/c/pay/' + identity + '#opaque',
            'line_items': {'object': 'list', 'has_more': False, 'data': [
                {'quantity': 1, 'currency': 'usd', 'amount_total': amount,
                 'amount_subtotal': amount, 'price': {'currency': 'usd', 'unit_amount': amount,
                                                     'recurring': None}}]}}


def intent(**overrides):
    return {'id': INTENT, 'object': 'payment_intent', 'livemode': False,
            'currency': 'usd', 'amount': 2000, 'metadata': metadata(),
            'payment_method_types': ['card'], **overrides}


def account(**overrides):
    return {'object': 'account', 'id': MERCHANT, 'charges_enabled': True, **overrides}


class Response:
    def __init__(self, payload, *, status=200, url=None):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status, self.url, self.closed = status, url, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def geturl(self):
        return self.url

    def read(self, count):
        return self.raw[:count]


class Transport:
    def __init__(self, *replies):
        self.replies, self.requests, self.responses = list(replies), [], []

    def __call__(self, request, *, timeout):
        assert timeout == sc.TIMEOUT_S
        assert request.full_url.startswith('https://api.stripe.com/v1/')
        self.requests.append(request)
        assert self.replies, 'unexpected request / automatic retry'
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        result = result if isinstance(result, Response) else Response(result)
        if result.url is None:
            result.url = request.full_url
        self.responses.append(result)
        return result


def client(*replies, config=None):
    transport = Transport(*replies)
    return sc.Client(config or settings(), opener=transport), transport


def test_settings_and_client_construction_are_inert_and_do_not_expose_credentials():
    config = settings()
    adapter = sc.Client(config)
    assert adapter.settings is config and not config.livemode
    assert KEY not in repr(config) and SECRET not in repr(config)
    assert config.public_origin == 'https://ccfleet.io'
    assert sc.API_VERSION == '2026-09-30.endive'


@pytest.mark.parametrize('changes', [
    {'mode': ''}, {'mode': 'auto'}, {'mode': True}, {'mode': []},
    {'api_key': 'pk_test_SYNTHETIC'}, {'api_key': 'sk_live_SYNTHETIC'},
    {'api_key': KEY + '\n'}, {'webhook_secret': 'missing'}, {'expected_account_id': 'acct_'},
    {'expected_account_id': 'acct_bad/path'}, {'allowed_cents': []}, {'allowed_cents': ()},
    {'allowed_cents': (2000, 2000)}, {'allowed_cents': (True,)}, {'allowed_cents': (2000.0,)},
    {'allowed_cents': (1234,)}, {'integration_identifier': ''},
    {'integration_identifier': 'unstable\n'}, {'public_origin': 'http://ccfleet.io'},
    {'public_origin': 'https://ccfleet.io/elsewhere'}, {'public_origin': 'https://ccfleet.io?q=1'},
    {'public_origin': 'https://u:p@ccfleet.io'}, {'public_origin': 'https://ccfleet.io:443'},
    {'public_origin': 'https://ccfleet.io\\@evil.test'}, {'public_origin': 'https://ccfleet.io\n'},
])
def test_invalid_settings_fail_before_any_network(changes):
    with pytest.raises(sc.StripeCheckoutError):
        settings(**changes)


@pytest.mark.parametrize('changes', [{'id': ''}, {'id': 'bad/path'}, {'account_id': ''},
                                    {'amount_cents': True}, {'amount_cents': 20.0},
                                    {'amount_cents': 0}])
def test_invalid_local_order_is_never_coerced(changes):
    with pytest.raises(sc.StripeCheckoutError):
        order(**changes)


def test_create_uses_fixed_card_only_usd_price_and_binds_session_and_intent_metadata():
    adapter, fake = client(account(), session())
    created = adapter.create_session(order())
    assert created.id == SESSION and created.url == session()['url']
    assert 'opaque' not in repr(created)
    assert [request.get_method() for request in fake.requests] == ['GET', 'POST']
    assert [request.full_url for request in fake.requests] == [
        sc.API_ORIGIN + '/v1/account', sc.API_ORIGIN + '/v1/checkout/sessions']
    request = fake.requests[1]
    assert request.get_header('Authorization') == 'Bearer ' + KEY
    assert request.get_header('Stripe-version') == sc.API_VERSION
    fields = dict(urllib.parse.parse_qsl(request.data.decode(), strict_parsing=True))
    assert fields['mode'] == 'payment' and fields['ui_mode'] == 'hosted_page'
    assert fields['allowed_payment_method_types[0]'] == 'card'
    assert not any(key == 'payment_method_types' or key.startswith('payment_method_types[')
                   for key in fields)
    assert 'payment_method_configuration' not in fields
    assert fields['line_items[0][quantity]'] == '1'
    assert fields['line_items[0][price_data][unit_amount]'] == '2000'
    assert fields['line_items[0][price_data][currency]'] == 'usd'
    assert fields['line_items[0][price_data][product_data][name]'] == sc.PRODUCT_NAME
    assert fields['automatic_tax[enabled]'] == fields['adaptive_pricing[enabled]'] == 'false'
    assert fields['allow_promotion_codes'] == 'false'
    assert fields['client_reference_id'] == order().id
    assert fields['integration_identifier'] == sc.INTEGRATION_IDENTIFIER
    assert fields['success_url'] == ('https://ccfleet.io/account?checkout=returned&session_id='
                                     '{CHECKOUT_SESSION_ID}#wallet')
    assert fields['cancel_url'] == 'https://ccfleet.io/account?checkout=cancelled#wallet'
    for key, value in metadata().items():
        assert fields['metadata[' + key + ']'] == value
        assert fields['payment_intent_data[metadata][' + key + ']'] == value
    assert not any('automatic_payment_methods' in key or 'subscription' in key
                   or 'setup_future_usage' in key or 'alipay' in key for key in fields)
    assert request.get_header('Idempotency-key').startswith('ccfleet-topup-')
    assert all(response.closed for response in fake.responses)


def test_stable_order_identity_not_key_rotation_or_owner_and_amount_makes_creation_key():
    keys = []
    for local_order, config in [(order(), settings()),
                               (order(), settings(api_key='sk_test_ROTATED_SYNTHETIC_KEY')),
                               (order(account_id='different', amount_cents=5000), settings()),
                               (order(id='another-order'), settings())]:
        adapter, fake = client(account(), session(local_order=local_order, config=config), config=config)
        adapter.create_session(local_order)
        keys.append(fake.requests[1].get_header('Idempotency-key'))
    assert keys[0] == keys[1] == keys[2] and keys[0] != keys[3]


@pytest.mark.parametrize('merchant', [account(id='acct_AnotherMerchant'),
                                    account(object='customer'), {}])
def test_wrong_merchant_blocks_all_creation(merchant):
    adapter, fake = client(merchant)
    with pytest.raises(sc.StripeCheckoutError):
        adapter.create_session(order())
    assert len(fake.requests) == 1 and fake.requests[0].get_method() == 'GET'


@pytest.mark.parametrize('enabled', [False, None, 1, 'true'])
def test_live_requires_actual_charges_enabled_true(enabled):
    config = settings(mode='live', api_key='rk_live_SYNTHETIC_NOT_A_REAL_KEY')
    adapter, fake = client(account(charges_enabled=enabled), config=config)
    with pytest.raises(sc.StripeCheckoutError):
        adapter.create_session(order())
    assert len(fake.requests) == 1


def test_test_mode_does_not_require_live_activation_and_allowlist_precedes_network():
    adapter, _ = client(account(charges_enabled=False), session())
    assert adapter.create_session(order()).id == SESSION
    adapter, fake = client()
    with pytest.raises(sc.StripeCheckoutError):
        adapter.create_session(order(amount_cents=1234))
    assert not fake.requests


def test_live_receipt_keeps_live_merchant_scope_and_approved_subset():
    config = settings(mode='live', api_key='rk_live_SYNTHETIC_NOT_A_REAL_KEY',
                      allowed_cents=(2000,))
    live_session = session(paid=True, config=config)
    adapter, fake = client(account(), live_session, config=config)
    receipt = adapter.retrieve_paid_session(live_session['id'], order())
    assert receipt.livemode is True and receipt.session_id.startswith('cs_live_')
    assert receipt.merchant_id == MERCHANT and receipt.amount_cents == 2000
    assert len(fake.requests) == 2


def test_default_transport_disables_ambient_proxy_and_redirect_without_network(monkeypatch):
    seen = {}

    class Opener:
        def open(self, request, *, timeout):
            seen.update(request=request, timeout=timeout)
            return 'synthetic-response'

    def build(*handlers):
        seen['handlers'] = handlers
        return Opener()

    monkeypatch.setattr(urllib.request, 'build_opener', build)
    request = urllib.request.Request(sc.API_ORIGIN + '/v1/account')
    assert sc._https_open(request, timeout=15) == 'synthetic-response'
    proxy, redirect, tls = seen['handlers']
    assert isinstance(proxy, urllib.request.ProxyHandler) and proxy.proxies == {}
    assert isinstance(redirect, sc._NoRedirect)
    assert redirect.redirect_request(None, None, 302, None, None, 'https://evil.test') is None
    assert isinstance(tls, urllib.request.HTTPSHandler)
    assert tls._context.verify_mode == ssl.CERT_REQUIRED and tls._context.check_hostname
    assert seen['request'] is request and seen['timeout'] == 15


@pytest.mark.parametrize('reply,uncertain', [
    (urllib.error.URLError('secret-request-body'), True), (TimeoutError('secret-body'), True),
    (OSError('secret-body'), True),
    (Response(b'not JSON secret-body'), True), (Response(b''), True),
    (Response(b'x' * (sc.MAX_BODY + 1)), True),
    (Response(b'{"id":1,"id":2}'), True), (Response(b'{"x":NaN}'), True),
    (Response(b'[]'), True), (Response(b'\xff'), True),
    (Response({}, status=201, url='https://evil.test/secret-body'), True),
    (Response({}, status=503), True),
    (urllib.error.HTTPError(sc.API_ORIGIN, 500, 'secret-body', {}, io.BytesIO()), True),
    (urllib.error.HTTPError(sc.API_ORIGIN, 400, 'secret-body', {}, io.BytesIO()), False),
])
def test_uncertain_create_retains_safe_diagnostic_and_never_retries(reply, uncertain):
    adapter, fake = client(account(), reply)
    with pytest.raises(sc.StripeCheckoutError) as caught:
        adapter.create_session(order())
    assert caught.value.uncertain is uncertain
    assert 'secret' not in str(caught.value) and KEY not in str(caught.value)
    assert len(fake.requests) == 2
    assert all(response.closed for response in fake.responses)


@pytest.mark.parametrize('url', [
    'http://checkout.stripe.com/c/pay/' + SESSION,
    'https://checkout.stripe.com.evil.test/c/pay/' + SESSION,
    'https://checkout.stripe.com@evil.test/c/pay/' + SESSION,
    'https://evil@checkout.stripe.com/c/pay/' + SESSION,
    'https://checkout.stripe.com:443/c/pay/' + SESSION,
    'https://checkout.stripe.com/c/pay/cs_test_Another',
    'https://checkout.stripe.com/c/pay/' + SESSION + '/extra',
    'https://checkout.stripe.com\\evil/c/pay/' + SESSION,
    'https://checkout.stripe.com/c/pay/' + SESSION + '\n',
    'https://checkout.stripe.com/c/pay/' + SESSION + '#bad fragment', None,
])
def test_created_redirect_must_be_this_session_on_stripes_https_host(url):
    reply = session()
    reply['url'] = url
    adapter, fake = client(account(), reply)
    with pytest.raises(sc.StripeCheckoutError) as caught:
        adapter.create_session(order())
    assert caught.value.uncertain and len(fake.requests) == 2


def test_paid_receipt_is_exact_order_account_merchant_usd_and_session():
    adapter, fake = client(account(), session(paid=True))
    receipt = adapter.retrieve_paid_session(SESSION, order())
    assert receipt.as_dict() == {'order_id': order().id, 'account_id': order().account_id,
        'amount_cents': 2000, 'currency': 'USD', 'merchant_id': MERCHANT, 'livemode': False,
        'payment_status': 'paid', 'session_id': SESSION, 'payment_intent_id': INTENT}
    assert [request.get_method() for request in fake.requests] == ['GET', 'GET']
    assert fake.requests[-1].full_url.endswith('/' + SESSION + '?expand%5B%5D=line_items')


def changed(source, path, value):
    result = copy.deepcopy(source)
    target = result
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    return result


@pytest.mark.parametrize('path,value', [
    (('id',), 'cs_test_Another'), (('id',), 'cs_live_Another'),
    (('object',), 'customer'), (('mode',), 'subscription'), (('ui_mode',), 'embedded_page'),
    (('livemode',), True), (('livemode',), 0), (('currency',), 'cny'),
    (('amount_total',), 1999), (('amount_subtotal',), 0), (('amount_total',), 2000.0),
    (('client_reference_id',), 'someone-else'), (('metadata',), None),
    (('metadata', 'ccfleet_order_id'), 'another-order'),
    (('metadata', 'ccfleet_account_id'), 'another-owner'),
    (('metadata', 'ccfleet_merchant_id'), 'acct_Other'),
    (('metadata', 'ccfleet_mode'), 'live'), (('metadata', 'ccfleet_purpose'), 'something-else'),
    (('metadata', 'integration_identifier'), 'other-integration'),
    (('payment_method_types',), ['card', 'alipay']), (('subscription',), 'sub_Another'),
    (('allowed_payment_method_types',), None),
    (('allowed_payment_method_types',), ['card', 'alipay']),
    (('setup_intent',), 'seti_Another'), (('automatic_tax', 'enabled'), True),
    (('total_details', 'amount_discount'), 1), (('total_details', 'amount_shipping'), 1),
    (('total_details', 'amount_tax'), 1), (('total_details', 'amount_tax'), False),
    (('adaptive_pricing', 'enabled'), True), (('currency_conversion',), {}),
    (('presentment_details',), {'presentment_currency': 'cny', 'presentment_amount': 2000}),
    (('status',), 'open'), (('payment_status',), 'unpaid'),
    (('payment_status',), 'no_payment_required'), (('payment_intent',), None),
    (('payment_intent',), 'pi_bad/path'), (('line_items',), None),
    (('line_items', 'object'), 'something'), (('line_items', 'has_more'), True),
    (('line_items', 'data'), []), (('line_items', 'data', 0, 'quantity'), True),
    (('line_items', 'data', 0, 'quantity'), 2),
    (('line_items', 'data', 0, 'currency'), 'cny'),
    (('line_items', 'data', 0, 'amount_total'), 1999),
    (('line_items', 'data', 0, 'amount_subtotal'), 2000.0),
    (('line_items', 'data', 0, 'price', 'unit_amount'), 1999),
    (('line_items', 'data', 0, 'price', 'currency'), 'cny'),
    (('line_items', 'data', 0, 'price', 'recurring'), {'interval': 'month'}),
])
def test_paid_receipt_refuses_every_mismatched_financial_boundary(path, value):
    adapter, fake = client(account(), changed(session(paid=True), path, value))
    with pytest.raises(sc.StripeCheckoutError):
        adapter.retrieve_paid_session(SESSION, order())
    assert len(fake.requests) == 2


def test_retrieving_a_bound_open_link_only_reads_existing_session():
    adapter, fake = client(account(), session())
    assert adapter.retrieve_open_session(SESSION, order()).url == session()['url']
    assert [request.get_method() for request in fake.requests] == ['GET', 'GET']
    assert fake.requests[-1].full_url == sc.API_ORIGIN + '/v1/checkout/sessions/' + SESSION


@pytest.mark.parametrize('status,payment_status', [('expired', 'unpaid'), ('complete', 'paid'),
                                                ('open', 'paid')])
def test_bound_closed_session_does_not_create_replacement(status, payment_status):
    reply = {**session(), 'status': status, 'payment_status': payment_status}
    adapter, fake = client(account(), reply)
    with pytest.raises(sc.StripeCheckoutError):
        adapter.retrieve_open_session(SESSION, order())
    assert len(fake.requests) == 2 and all(r.get_method() == 'GET' for r in fake.requests)


@pytest.mark.parametrize('identity', ['cs_live_Unexpected', 'cs_test_bad/path', '',
                                      'https://evil.test/cs_test_fake'])
def test_session_reference_cannot_escape_endpoint_or_mode(identity):
    adapter, fake = client()
    for method in (adapter.retrieve_paid_session, adapter.retrieve_open_session):
        with pytest.raises(sc.StripeCheckoutError):
            method(identity, order())
    assert not fake.requests


def test_verified_payment_reference_does_not_require_success_or_grant_credit():
    adapter, fake = client(account(), intent(status='requires_payment_method'))
    reference = adapter.retrieve_payment_reference(INTENT)
    assert reference.as_dict() == {'payment_intent_id': INTENT, 'order_id': order().id,
        'account_id': order().account_id, 'amount_cents': 2000, 'currency': 'USD',
        'merchant_id': MERCHANT, 'livemode': False}
    assert all(request.get_method() == 'GET' for request in fake.requests)


@pytest.mark.parametrize('value', [None, {}, {'other': 'merchant-order'},
                                {'integration_identifier': 'another-integration'}])
def test_unrelated_payment_is_ignored_for_reversal_mapping(value):
    adapter, _ = client(account(), intent(metadata=value))
    assert adapter.retrieve_payment_reference(INTENT) is None


@pytest.mark.parametrize('path,value', [
    (('id',), 'pi_Other'), (('object',), 'charge'), (('livemode',), True),
    (('livemode',), 0), (('amount',), True), (('amount',), 1999),
    (('amount',), 2000.0), (('currency',), 'cny'), (('payment_method_types',), ['alipay']),
    (('metadata',), []), (('metadata',), {'ccfleet_order_id': 'partial'}),
    (('metadata',), {'integration_identifier': sc.INTEGRATION_IDENTIFIER}),
    (('metadata', 'ccfleet_merchant_id'), 'acct_Other'),
    (('metadata', 'ccfleet_mode'), 'live'), (('metadata', 'ccfleet_purpose'), 'other'),
    (('metadata', 'integration_identifier'), 'other'),
])
def test_partial_or_mismatched_fleet_payment_reference_is_not_silently_ignored(path, value):
    adapter, _ = client(account(), changed(intent(), path, value))
    with pytest.raises(sc.StripeCheckoutError):
        adapter.retrieve_payment_reference(INTENT)


def event(**overrides):
    return {'id': 'evt_SyntheticWebhook', 'object': 'event', 'livemode': False,
            'type': 'checkout.session.completed', 'data': {'object': {'id': SESSION}}, **overrides}


def raw_event(**overrides):
    return json.dumps(event(**overrides), separators=(',', ':')).encode()


def signed(raw, *, timestamp=NOW, secret=SECRET):
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b'.' + raw,
                      hashlib.sha256).hexdigest()
    return 't=' + str(timestamp) + ',v1=' + digest


def verify(raw, header=None, **overrides):
    return sc.verify_webhook(raw, signed(raw) if header is None else header, SECRET,
                             **{'mode': 'test', 'now': NOW, **overrides})


def test_webhook_verifies_original_bytes_and_accepts_rotated_v1_signatures():
    raw = raw_event()
    header = signed(raw) + ',v0=legacy,v1=' + '0' * 64
    assert verify(raw, header) == event()
    with pytest.raises(sc.StripeCheckoutError, match='signature verification'):
        verify(raw + b' ', header)
    # Authenticated unrelated types are returned for the coordinator to ignore, not credited here.
    assert verify(raw_event(type='customer.updated'))['type'] == 'customer.updated'


@pytest.mark.parametrize('offset', [-300, 0, 300])
def test_webhook_timestamp_boundary(offset):
    raw = raw_event()
    assert verify(raw, signed(raw, timestamp=NOW + offset))['id'] == event()['id']


@pytest.mark.parametrize('offset', [-301, 301])
def test_webhook_rejects_stale_and_future_replays(offset):
    raw = raw_event()
    with pytest.raises(sc.StripeCheckoutError, match='stale'):
        verify(raw, signed(raw, timestamp=NOW + offset))


@pytest.mark.parametrize('header', [
    '', 'garbage', 't=1800000000', 'v1=' + '0' * 64, 't=NaN,v1=' + '0' * 64,
    't=1800000000,t=1800000000,v1=' + '0' * 64,
    't=1800000000,v1=BAD', 't=1800000000,v1=' + 'A' * 64,
    't=1800000000,v1=' + '0' * 64 + ',broken', 't=1800000000,v1=' + '0' * 64 + ',=1',
    't=1800000000,v1=' + '0' * 64 + ',v1=' + '0' * 64 + ',v1=' + '0' * 64 + ',é=1',
    't=1800000000,' + ','.join(['v1=' + '0' * 64] * 17),
])
def test_invalid_signature_header_never_authenticates(header):
    with pytest.raises(sc.StripeCheckoutError):
        verify(raw_event(), header)


def test_json_is_not_parsed_before_authentication(monkeypatch):
    def forbidden(_raw):
        pytest.fail('unauthenticated JSON was parsed')
    monkeypatch.setattr(sc, '_json', forbidden)
    with pytest.raises(sc.StripeCheckoutError, match='signature verification'):
        verify(b'invalid JSON with potentially dangerous content',
               't=' + str(NOW) + ',v1=' + '0' * 64)


@pytest.mark.parametrize('raw', [b'{}', b'[]', b'\xff', b'{"x":NaN}',
    b'{"id":"evt_one","id":"evt_two"}', raw_event(id='not-an-event'),
    raw_event(object='checkout.session'), raw_event(livemode=True), raw_event(livemode=0),
    raw_event(type='bad event type')])
def test_valid_hmac_does_not_accept_invalid_or_ambiguous_envelope(raw):
    with pytest.raises(sc.StripeCheckoutError):
        verify(raw)


def test_connected_account_must_be_explicitly_expected_not_other_merchant():
    raw = raw_event(account=MERCHANT)
    assert verify(raw, expected_account_id=MERCHANT)['account'] == MERCHANT
    for expected in (None, 'acct_Other'):
        with pytest.raises(sc.StripeCheckoutError, match='account differs'):
            verify(raw, expected_account_id=expected)


@pytest.mark.parametrize('changes', [{'mode': ''}, {'mode': True}, {'tolerance': 0},
    {'tolerance': 601}, {'tolerance': True}, {'now': True}, {'now': -1}, {'now': float('nan')},
    {'now': float('inf')}])
def test_webhook_configuration_cannot_weaken_verification(changes):
    with pytest.raises(sc.StripeCheckoutError):
        verify(raw_event(), **changes)


def test_webhook_body_and_signature_are_bounded_before_parsing():
    for raw, header in [(b'', 'x'), (b'x' * (sc.MAX_BODY + 1), 'x'), ('text', 'x'),
                        (raw_event(), 'x' * 4097)]:
        with pytest.raises(sc.StripeCheckoutError):
            verify(raw, header)


def test_only_approved_api_endpoints_are_constructible():
    adapter, fake = client()
    for path in ('https://evil.test', '/v1/charges', '/v1/refunds', '/v1/account?x=1',
                 '/v1/checkout/sessions/cs_test_fake/../../charges'):
        with pytest.raises(sc.StripeCheckoutError):
            adapter._request(path)
    assert not fake.requests
    with pytest.raises(sc.StripeCheckoutError):
        adapter._request('/v1/refunds', fields={}, idempotency_key='synthetic')
    with pytest.raises(sc.StripeCheckoutError):
        adapter._request('/v1/checkout/sessions', fields={}, idempotency_key=None)
    with pytest.raises(sc.StripeCheckoutError, match='static Stripe payment methods'):
        adapter._request('/v1/checkout/sessions', fields={'payment_method_types[0]': 'card'},
                         idempotency_key='synthetic')
    assert not fake.requests
