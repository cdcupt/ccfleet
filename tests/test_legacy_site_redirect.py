"""Retire only alias browser pages; existing client and OAuth origins survive."""
from __future__ import annotations

import http.client
import json
import time
import urllib.parse
from dataclasses import replace

import pytest

from ccfleetd import reference_docs, sessions
from ccfleetd.api import legacy_browser_target
from ccfleetd.bilingual import CURRENT_PAGES
from tests import test_domain_migration as origins
from tests.conftest import heartbeat
from tests.test_admin_host import Browser

migration = origins.migration
NEW, OLD, ADMIN = origins.NEW, origins.OLD, origins.ADMIN


@pytest.mark.parametrize('path', sorted(CURRENT_PAGES | {
    '/docs/library/' + name for name in reference_docs.PUBLIC_FILES}))
def test_only_known_literal_browser_paths_are_eligible(path):
    assert legacy_browser_target(path) == path


@pytest.mark.parametrize('target,expected', [
    ('/zh', '/zh/'), ('/en/', '/en/'),
    ('/zh/docs/how-it-works', '/zh/docs/how-it-works'),
    ('/docs/how-it-works?lang=zh', '/zh/docs/how-it-works'),
    ('/en/docs/guide?lang=zh', '/zh/docs/guide'),
    ('/zh/slots?lang=en&type=datacenter&page=2', '/en/slots?type=datacenter&page=2'),
    ('/slots?type=datacenter&page=2&q=Bugnet&brand=NetBlazer',
     '/slots?type=datacenter&page=2&q=Bugnet&brand=NetBlazer'),
    ('/account/available?lang=zh&type=isp&token=private', '/zh/account/available?type=isp'),
    ('/account?token=private&code=private&next=https://attacker.test', '/account'),
    ('/docs/library/docs/runbooks.html?token=private&lang=zh',
     '/docs/library/docs/runbooks.html'),
    ('/slots?type=unknown&page=-1&brand=' + 'b' * 49 + '&q=' + 'q' * 100,
     '/slots?q=' + 'q' * 80),
    ('/slots?q=se%0D%0Aarch&type=datacenter', '/slots?type=datacenter'),
    ('/slots?' + '&'.join('token=private' for _ in range(17)), '/slots'),
])
def test_only_bounded_browse_queries_and_language_are_carried(target, expected):
    assert legacy_browser_target(target) == expected


@pytest.mark.parametrize('target', [
    '/unknown', '/account/slots/s1/cli', '/admin', '/healthz', '/robots.txt',
    '/api/cli/connect', '/api/cli/status', '/api/cli/gateway/options',
    '/api/gateway/status', '/api/heartbeat', '/api/cli/register',
    '/v1/messages', '/v1/messages?beta=true', '/v1/messages/count_tokens',
    '/auth/google/start?next=/account', '/zh/auth/google/start?next=/zh/account',
    '/auth/google/callback?code=private&state=private', '/auth/signout',
    '/docs/library/unknown.html', '/docs/library/../README.html',
    '/slots/', '/slots//', '/%73lots', '/zh/%73lots', '/zh/../slots',
    '/slots\\x', '//attacker.test/slots', 'https://attacker.test/slots',
    '/slots#private', '/slots?x=\r\nprivate', '/slots?' + 'q' * 4096,
])
def test_protocol_auth_unknown_or_ambiguous_paths_are_never_redirect_targets(target):
    assert legacy_browser_target(target) is None


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('path,selected', [
    ('/', '/'), ('/en/docs/guide', '/en/docs/guide'),
    ('/zh/docs/how-it-works', '/zh/docs/how-it-works'),
    ('/privacy', '/privacy'), ('/status', '/status'), ('/account', '/account'),
    ('/slots?lang=zh&type=datacenter&page=2&token=private',
     '/zh/slots?type=datacenter&page=2'),
    ('/docs/library/docs/runbooks.html?code=private', '/docs/library/docs/runbooks.html'),
])
def test_alias_ui_redirects_to_fixed_canonical_without_secret_query_or_cookie(migration, method, path, selected):
    code, headers, raw = migration.call(method, path, headers={
        'X-Forwarded-Host': 'attacker.test', 'X-Forwarded-Proto': 'http',
        'Cookie': 'untrusted=private'})
    assert code == 308 and headers['Location'] == NEW + selected
    assert raw == b'' and headers['Content-Length'] == '0'
    assert headers['Cache-Control'] == 'no-store'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert 'Set-Cookie' not in headers and 'private' not in headers['Location']


def test_canonical_site_unknown_hosts_admin_and_loopback_keep_their_boundaries(migration):
    m = migration
    assert m.call('GET', '/en/docs/how-it-works', origin=NEW)[0] == 200
    for path in ('/unknown', '/api/nodes', '/admin', '/%73lots'):
        code, headers, _ = m.call('GET', path, origin=NEW)
        assert code == 404 and 'Location' not in headers
    for host in ('attacker.test', 'ccfleet.io.attacker.test', 'ccfleet.io:80'):
        code, headers, _ = m.call('GET', '/slots', headers={
            'Host': host, 'X-Forwarded-Host': 'ccfleet.io'})
        assert code == 404 and 'Location' not in headers
    code, headers, _ = m.call('GET', '/slots', headers={'Host': ADMIN})
    assert code == 404 and 'Location' not in headers
    assert m.call('GET', '/healthz', headers={'Host': '127.0.0.1:8110'})[0] == 200
    assert m.call('GET', '/healthz')[0] == 200


def test_unknown_or_absolute_legacy_targets_stay_404_not_open_redirects(migration):
    for path in ('/unknown', '/docs/library/unknown.html', '/%73lots',
                 '/zh/../slots', 'https://attacker.test/slots',
                 '/slots?' + 'q' * 4096, '/slots?code=private#fragment'):
        code, headers, _ = migration.call('GET', path)
        assert code == 404 and 'Location' not in headers


def test_duplicate_host_never_selects_a_redirect_authority(migration):
    connection = http.client.HTTPConnection(*migration.server.server_address, timeout=3)
    connection.putrequest('GET', '/slots', skip_host=True)
    connection.putheader('Host', 'ccfleet.daichenlab.com')
    connection.putheader('Host', 'ccfleet.io')
    connection.endheaders()
    reply = connection.getresponse()
    assert reply.status == 404 and reply.getheader('Location') is None
    reply.read()
    connection.close()


def test_legacy_devices_keys_model_posts_websocket_and_heartbeat_do_not_redirect(migration):
    m = migration
    device = m.paired(OLD)
    assert device['endpoint'] == OLD.replace('https:', 'wss:') + '/api/cli/connect'
    key = m.store.create_gateway_key('a1', 'Existing key', ['s1'], now=time.time())
    for path, token, expected in [
        ('/api/cli/status', device['device_token'], 200),
        ('/api/cli/gateway/options', device['device_token'], 200),
        ('/api/gateway/status', key['key_token'], 200),
        ('/api/cli/connect', device['device_token'], 426),
    ]:
        code, headers, _ = m.call('GET', path, token=token)
        assert code == expected and 'Location' not in headers
    for path in ('/v1/messages', '/v1/messages?beta=true',
                 '/v1/messages/count_tokens', '/v1/messages/count_tokens?beta=true'):
        code, headers, raw = m.call('POST', path, token=key['key_token'], body={})
        assert code == 200 and 'Location' not in headers
        assert json.loads(raw)['provider_requests'] == 0
    token = m.store.rotate_token('m1')
    code, headers, _ = m.call('POST', '/api/heartbeat', token=token,
        body={**heartbeat(time.time())['payload'], 'node_id': 'm1'})
    assert code == 200 and 'Location' not in headers


def test_old_oauth_flow_keeps_host_only_state_cookie_and_callback(migration):
    browser = Browser(migration.server.server_address[1], urllib.parse.urlsplit(OLD).netloc)
    start = browser.call('GET', '/auth/google/start?next=/account')
    assert start.status == 303
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(start.getheader('Location')).query)
    assert query['redirect_uri'] == [OLD + '/auth/google/callback']
    assert sessions.FLOW_COOKIE_NAME in browser.jar
    assert all('Domain=' not in cookie for cookie in start.headers.get_all('Set-Cookie'))
    callback = '/auth/google/callback?' + urllib.parse.urlencode({'state': query['state'][0], 'code': 'c'})
    back = browser.call('GET', callback)
    assert back.status == 303 and back.getheader('Location') == '/account'
    assert migration.exchanges[-1] == OLD + '/auth/google/callback'
    assert any(name != sessions.FLOW_COOKIE_NAME for name in browser.jar)
    # A later page visit changes site; cookies are never copied to the new origin.
    page = browser.call('GET', '/account')
    assert page.status == 308 and page.getheader('Location') == NEW + '/account'
    assert page.getheader('Set-Cookie') is None
    logout = browser.call('POST', '/auth/signout', form={})
    assert logout.status != 308


def test_unconfigured_aliases_and_broker_do_not_acquire_browser_redirects(migration):
    m = migration
    m.ctx.cfg = replace(m.cfg, public_url_aliases=())
    code, headers, _ = m.call('GET', '/')
    assert code == 200 and 'Location' not in headers
    m.ctx.cfg = replace(m.cfg, broker_only=True)
    code, headers, _ = m.call('GET', '/slots')
    assert code == 404 and 'Location' not in headers
