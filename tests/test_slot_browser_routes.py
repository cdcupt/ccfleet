"""Authenticated inventory browsing through the real HTTP and language routes."""
from __future__ import annotations

import base64
import re
import urllib.parse

import pytest

from ccfleetd import slots
from ccfleetd.api import access_log_route
from tests.test_language_routes import ADMIN, _sign_in, language_site  # noqa: F401
from tests.test_offer_ui import configure
from tests.test_usersite import Browser, machine, serving


@pytest.fixture
def site(monkeypatch):
    yield from serving(monkeypatch)


def inventory(store, count=3):
    for number in range(1, count + 1):
        node = f"private-node-{number}"
        machine(store, node, users=("slot01",))
        configure(store, node, host="OVHcloud" if number % 2 else "ATTVPS",
                  ip="Decodo" if number % 2 else "ResidentialProvider",
                  kind="isp" if number % 2 else "residential")


def rows(body):
    table = body.split('<table class="offer-table">', 1)
    return re.findall(r'<th scope="row">.*?Machine (\d+)', table[1]) if len(table) > 1 else []


def test_account_has_owned_controls_and_compact_inventory_link_only(site):
    store, sign_in, _ = site
    inventory(store)
    user = sign_in(quota=2)
    assert user.press('/account/claim', machine_number='1').status == 303
    body = user.page()
    assert 'Browse available slots' in body and '2 available slots' in body
    assert 'href="/account/available"' in body
    assert 'erik-1' in body
    assert 'class="slot-browser"' not in body
    assert 'Claim this slot' not in body and 'ResidentialProvider' not in body


@pytest.mark.parametrize('path', ['/account/available', '/en/account/available', '/zh/account/available'])
def test_signed_out_or_admin_basic_is_not_a_customer_session(site, path):
    store, sign_in, _ = site
    inventory(store)
    stranger = Browser(sign_in().port)
    basic = base64.b64encode(b'admin:admin-token').decode()
    for headers in ({}, {'Authorization': 'Basic ' + basic}):
        reply = stranger.call('GET', path, headers=headers)
        assert reply.status == 200
        assert 'OVHcloud' not in reply.body and 'Decodo' not in reply.body
        assert 'class="slot-browser"' not in reply.body
        assert 'accounts.google' not in reply.body
        assert '/auth/google/start' in reply.body


@pytest.mark.parametrize('query,expected', [
    ('q=dEcOdO', ['1', '3']),
    ('q=Residential', ['2']),
    ('q=ISP', ['1', '3']),
    ('brand=OVHcloud&type=isp', ['1', '3']),
    ('brand=OVHcloud&type=residential', []),
    ('q=Machine+2', ['2']),
    ('q=private-node', []),
])
def test_real_get_search_uses_public_provider_type_and_machine_fields(site, query, expected):
    store, sign_in, _ = site
    inventory(store)
    body = sign_in(quota=1).call('GET', '/account/available?' + query).body
    assert rows(body) == expected
    assert 'private-node-1' not in body and 'slot01' not in body
    assert ('No available machines match' in body) == (not expected)


def test_pagination_is_stable_and_filters_survive_links(site):
    store, sign_in, _ = site
    inventory(store, 43)
    user = sign_in(quota=1)
    pages = [rows(user.call('GET', '/account/available?page=' + str(n)).body)
             for n in (1, 2, 3)]
    assert list(map(len, pages)) == [20, 20, 3]
    assert [int(n) for page in pages for n in page] == list(range(1, 44))
    body = user.call('GET', '/account/available?q=OVH&brand=Decodo&type=isp').body
    assert rows(body) == [str(n) for n in range(1, 40, 2)]
    assert 'q=OVH&amp;brand=Decodo&amp;type=isp&amp;page=2' in body
    assert rows(user.call('GET', '/account/available?page=999999').body) == ['41', '42', '43']


def test_full_allowance_can_search_but_cannot_claim(site):
    store, sign_in, _ = site
    inventory(store)
    user = sign_in(quota=0)
    body = user.call('GET', '/account/available?q=Decodo').body
    assert rows(body) == ['1', '3']
    assert 'action="/account/claim"' not in body
    assert user.press('/account/claim', machine_number='1').status == 303
    assert store.held_slot_count(user.account['id']) == 0


def test_claim_from_browse_keeps_csrf_pricing_and_race_rechecks(site):
    store, sign_in, _ = site
    inventory(store)
    user = sign_in(quota=2)
    body = user.call('GET', '/account/available?q=Machine+1').body
    assert rows(body) == ['1'] and '$11.70 / month' in body
    assert user.call('POST', '/account/claim', form={'machine_number': '1'}).status == 403
    other = sign_in('other', 'other@example.com', quota=1)
    assert other.press('/account/claim', machine_number='1').status == 303
    response = user.press('/account/claim', machine_number='1')
    assert 'no-slot' in response.getheader('Location')
    assert store.held_slot_count(user.account['id']) == 0
    response = user.press('/account/claim', machine_number='3')
    assert response.getheader('Location').startswith('/account?note=claimed#slot-')
    [held] = store.list_slots(held_by=user.account['id'])
    assert held['state'] == slots.CLAIMING


@pytest.mark.parametrize('kind', ['isp', 'datacenter'])
def test_search_is_escaped_bounded_and_language_switch_keeps_only_filters(site, kind):
    store, sign_in, _ = site
    inventory(store)
    user = sign_in(quota=1)
    query = urllib.parse.urlencode({'q': '<script>alert(1)</script>', 'brand': 'Decodo',
                                   'type': kind, 'page': '2', 'note': 'PRIVATE'})
    body = user.call('GET', '/en/account/available?' + query).body
    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in body
    link = re.search(r'href="(/zh/account/available\?[^\"]+)"', body).group(1)
    assert 'note=' not in link and 'PRIVATE' not in link
    assert 'brand=Decodo' in link and 'type=' + kind in link and 'page=2' in link
    excessive = '&'.join('field' + str(n) + '=x' for n in range(25))
    assert user.call('GET', '/account/available?' + excessive).status == 200
    assert user.call('HEAD', '/account/available').status == 200
    assert access_log_route('/account/available?q=PRIVATE') == '/account/available'


def test_browse_localization_and_admin_host_boundary(request):
    store, _, make_browser = request.getfixturevalue('language_site')
    inventory(store)
    user = make_browser()
    _sign_in(user)
    en = user.call('GET', '/en/account/available?type=residential')
    zh = user.call('GET', '/zh/account/available?type=residential')
    assert en.status == zh.status == 200
    assert 'Available slots' in en.body and '可用槽位' in zh.body
    assert 'lang="zh-Hans"' in zh.body and 'name="type"' in zh.body
    assert '主机 2' in zh.body and 'Machine 2' not in zh.body
    assert 'action="/zh/account/available"' in zh.body
    assert make_browser(ADMIN).call('GET', '/zh/account/available').status == 404
