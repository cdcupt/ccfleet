"""The complete public catalogue is separate from examples and private claims."""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from dataclasses import replace

import pytest

from ccfleetd import offers
from ccfleetd.api import Context, access_log_route, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store
from tests.test_admin_host import Browser
from tests.test_language_routes import ADMIN, PRODUCT, _sign_in, language_site  # noqa: F401
from tests.test_offer_ui import configure
from tests.test_usersite import machine


@pytest.fixture
def site(request):
    return request.getfixturevalue('language_site')


def inventory(store, count=3):
    for number in range(1, count + 1):
        node = f'private-node-{number}'
        machine(store, node, users=('privateunix',))
        configure(store, node, host='OVHcloud' if number % 2 else 'AAITR',
                  ip='Decodo' if number % 2 else 'AAITR',
                  kind='isp' if number % 2 else 'residential')


def numbers(source):
    return [int(n) for n in re.findall(r'href="/(?:en/|zh/)?status#machine-([0-9]+)"', source)]


@pytest.mark.parametrize('prefix', ['', '/en', '/zh'])
def test_public_catalogue_get_and_head_work_without_login_and_are_read_only(site, prefix):
    store, _, make_browser = site
    inventory(store)
    before = '\n'.join(store._conn.iterdump())
    browser = make_browser()
    reply = browser.call('GET', prefix + '/slots')
    assert reply.status == 200 and numbers(reply.body) == [1, 2, 3]
    assert 'no-store' in reply.getheader('Cache-Control')
    assert 'action="/account/claim"' not in reply.body
    assert 'name="machine_number"' not in reply.body
    assert all(private not in reply.body for private in ('private-node', 'privateunix',
                                                        'reader@example.com', 'token_hash',
                                                        'access_host', 'reserved_for'))
    assert browser.call('HEAD', prefix + '/slots').status == 200
    assert make_browser(ADMIN).call('GET', prefix + '/slots').status == 404
    assert '\n'.join(store._conn.iterdump()) == before
    assert access_log_route(prefix + '/slots?q=PRIVATE') == '/slots'


def test_homepage_is_bounded_examples_with_a_catalogue_jump(site):
    store, _, make_browser = site
    inventory(store, 43)
    browser = make_browser()
    page = browser.call('GET', '/zh/').body
    section = page.split('id="slot-pricing"', 1)[1].split('</section>', 1)[0]
    examples = re.findall(r'<(?:article|section|div)\b[^>]*class="[^"]*\boffer-example\b', section)
    assert 1 <= len(examples) <= 3
    assert '住宅 IP' in section and 'ISP IP' in section
    assert 'href="/zh/slots"' in section
    assert 'class="offer-table"' not in section
    assert len(numbers(browser.call('GET', '/slots').body)) == 20


@pytest.mark.parametrize('query,expected', [
    ('q=dEcOdO', [1, 3]), ('q=residential', [2]), ('q=Machine+2', [2]),
    ('brand=OVHcloud&type=isp', [1, 3]),
    ('brand=OVHcloud&type=residential', []), ('q=private-node', []),
])
def test_public_provider_machine_and_type_search(site, query, expected):
    store, _, make_browser = site
    inventory(store)
    reply = make_browser().call('GET', '/slots?' + query)
    assert reply.status == 200 and numbers(reply.body) == expected


def test_public_pagination_retains_filters_and_has_no_claim_authority(site):
    store, _, make_browser = site
    inventory(store, 43)
    browser = make_browser()
    pages = [numbers(browser.call('GET', '/slots?page=' + str(n)).body) for n in (1, 2, 3)]
    assert list(map(len, pages)) == [20, 20, 3]
    assert [n for page in pages for n in page] == list(range(1, 44))
    filtered = browser.call('GET', '/slots?q=OVH&brand=Decodo&type=isp').body
    assert numbers(filtered) == list(range(1, 40, 2))
    assert 'q=OVH&amp;brand=Decodo&amp;type=isp&amp;page=2' in filtered
    assert 'action="/account/claim"' not in filtered
    assert '/account/available' in filtered


def test_public_inventory_keeps_offline_in_use_and_unknown_costs_but_hides_retired(site):
    store, _, make_browser = site
    inventory(store, 4)
    store.set_enabled('private-node-4', False)
    store.add_account('private-holder', 'private-google', 'holder@example.test',
                      slot_quota=1, now=time.time())
    store.claim_slot('private-holder', node_id='private-node-1', now=time.time())
    store._conn.execute("UPDATE heartbeats SET ts=1 WHERE node_id='private-node-2'")
    store._conn.commit()
    store.set_node_offer('private-node-3', host_brand='UnknownCostHost', ip_brand='UnknownExit',
                         ip_type='isp', hosting=offers.Cost('5.35', 'USD'))
    body = make_browser().call('GET', '/slots').body
    assert numbers(body) == [1, 2, 3]
    assert 'In use' in body and 'Unavailable' in body and 'Pricing unavailable' in body
    assert 'UnknownCostHost' in body
    assert 'holder@example.test' not in body and 'private-holder' not in body
    assert len(store.list_nodes()) == 4 and store.held_slot_count('private-holder') == 1


def test_language_switch_preserves_bounded_search_only_and_escapes_input(site):
    store, _, make_browser = site
    inventory(store)
    query = urllib.parse.urlencode({'q': '<script>bad</script>', 'brand': 'Decodo',
                                   'type': 'isp', 'page': '2', 'note': 'PRIVATE'})
    body = make_browser().call('GET', '/en/slots?' + query).body
    assert '<script>bad</script>' not in body and '&lt;script&gt;bad&lt;/script&gt;' in body
    zh = re.search(r'href="(/zh/slots\?[^"]+)"', body).group(1)
    assert 'brand=Decodo' in zh and 'type=isp' in zh and 'page=2' in zh
    assert 'note=' not in zh and 'PRIVATE' not in zh
    assert make_browser().call('GET', '/slots?' + '&'.join(f'a{n}=x' for n in range(20))).status == 200


def test_signed_in_public_browse_does_not_write_or_replace_private_account_flow(site):
    store, _, make_browser = site
    inventory(store)
    browser = make_browser()
    _sign_in(browser)
    before = '\n'.join(store._conn.iterdump())
    body = browser.call('GET', '/slots').body
    assert numbers(body) == [1, 2, 3] and 'action="/account/claim"' not in body
    assert '\n'.join(store._conn.iterdump()) == before
    account = browser.call('GET', '/account').body
    assert 'class="slot-browser"' not in account
    private = make_browser().call('GET', '/account/available').body
    assert 'class="slot-browser"' not in private and '/auth/google/start' in private


@pytest.mark.parametrize('path', ['/slots', '/en/slots', '/zh/slots'])
def test_broker_does_not_serve_public_catalogue(cfg, path):
    cfg = replace(cfg, broker_only=True)
    store = Store(':memory:')
    server = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                          host='127.0.0.1', port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        assert Browser(server.server_address[1], PRODUCT).call('GET', path).status == 404
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        store.close()
