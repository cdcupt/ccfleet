"""Structured account metadata stays local, bounded and separate from inference."""
import json
from types import SimpleNamespace

import pytest

from ccfleet_agent import agent, local_relay, mac_owner_agent
from tests.test_mac_owner_quota import facts
from tests.test_mac_owner_quota import quota_runtime as quota_runtime
from tests.test_mac_owner_runtime import owner as synthetic_owner  # noqa: F401


@pytest.fixture
def transport(monkeypatch):
    state = {'status': 200, 'body': json.dumps({'seven_day': {'utilization': 5}, 'limits': [
        {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 0,
         'scope': {'model': {'display_name': 'Fable'}, 'surface': None}}],
        'private_account': 'NEVER_FORWARD'}).encode(), 'calls': [], 'closed': False,
             'bound': ['matched', 'matched']}
    class Connection:
        sock = SimpleNamespace(settimeout=lambda value: None, shutdown=lambda value: None)
        def connect(self):
            pass
        def request(self, method, path, **kwargs):
            state['calls'].append((method, path, kwargs))
        def getresponse(self):
            return SimpleNamespace(status=state['status'], read=lambda maximum: state['body'][:maximum])
        def close(self):
            state['closed'] = True
    relay = SimpleNamespace(bound_account=lambda home: state['bound'].pop(0),
        credential=lambda *args: ('SECRET_NATIVE_ACCESS', 9e9), connect_upstream=Connection,
        USER_AGENT='ccfleet-slot-relay/2', decode_json=local_relay.decode_json)
    monkeypatch.setattr(mac_owner_agent, 'sibling', lambda name:
                        {'local_relay': relay, 'agent': agent}[name])
    return state


def test_metadata_reader_makes_one_fixed_get_and_forwards_only_numeric_limits(transport):
    got = mac_owner_agent._provider_quota(None, 'matched')
    assert got['outcome'] == 'quota_read'
    assert got['reading']['week']['used_pct'] == 5
    assert got['reading']['models']['fable']['used_pct'] == 0
    assert len(transport['calls']) == 1
    method, path, request = transport['calls'][0]
    assert method == 'GET' and path == '/api/oauth/usage' and 'body' not in request
    assert request['headers']['authorization'] == 'Bearer SECRET_NATIVE_ACCESS'
    assert 'SECRET' not in json.dumps(got) and 'NEVER_FORWARD' not in json.dumps(got)
    assert transport['closed']


@pytest.mark.parametrize('status,outcome', [(302, 'native_network_error'),
    (401, 'native_auth_rejected'), (403, 'native_auth_rejected'),
    (429, 'native_rate_limited'), (502, 'native_network_error')])
def test_metadata_errors_never_follow_redirects_retry_or_forward_bodies(transport, status, outcome):
    transport.update(status=status, body=b'SECRET_PROVIDER_ERROR')
    assert mac_owner_agent._provider_quota(None, 'matched') == {'outcome': outcome}
    assert len(transport['calls']) == 1 and transport['closed']


@pytest.mark.parametrize('body', [b'x' * (mac_owner_agent.MAX_REPLY + 1), b'<html>private</html>',
                                  b'{"seven_day":1,"seven_day":2}'])
def test_bad_metadata_is_bounded_and_dropped(transport, body):
    transport['body'] = body
    assert 'reading' not in mac_owner_agent._provider_quota(None, 'matched')
    assert transport['closed']


def test_account_change_during_metadata_request_drops_the_reading(transport):
    transport['bound'][-1] = 'changed'
    assert mac_owner_agent._provider_quota(None, 'matched') == {'outcome': 'native_auth_rejected'}


def test_provider_reading_survives_without_native_disk_cache(quota_runtime, monkeypatch):
    from ccfleet_agent import mac_policy
    owner = quota_runtime
    from tests.test_private_owner_nodes import NOW
    calls = []
    def read(*args):
        calls.append(True)
        return {'outcome': 'quota_read', 'reading': {'checked_at': NOW,
                'week': {'used_pct': 5}, 'models': {'fable': {'used_pct': 0}},
                'private_account': 'NEVER_FORWARD'}}
    monkeypatch.setattr(mac_owner_agent, '_provider_quota', read)
    got = mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW)
    assert got == {'checked_at': NOW, 'week': {'used_pct': 5}, 'models': {'fable': {'used_pct': 0}}}
    assert mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW + 60) == got
    assert len(calls) == 1
    assert 'NEVER_FORWARD' not in json.dumps(mac_policy.read_json(owner[0], 'maintenance.json'))
    assert mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW + 90,
                                         maintenance=False) == got
