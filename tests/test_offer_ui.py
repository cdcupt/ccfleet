"""Different network offers are visible before a user chooses their machine."""
from __future__ import annotations

import http.client
import json
import re
import time

import pytest

from ccfleetd import offer_ui, offers, slots
from ccfleetd.bilingual import project_html
from ccfleetd.store import Store
from tests.test_price import console  # noqa: F401 -- shared authenticated HTTP fixture
from tests.test_usersite import machine, serving


@pytest.fixture
def site(monkeypatch):
    yield from serving(monkeypatch)


def configure(store, node="m1", *, host="ExampleHost", ip="ExampleIP", kind="isp",
              cost="4.70", ip_cost="5.00", currency="USD"):
    store.set_node_offer(node, host_brand=host, ip_brand=ip, ip_type=kind,
                         hosting=offers.Cost(cost, currency), ip=offers.Cost(ip_cost, currency))


def test_public_offer_is_neutral_and_contains_only_brand_type_rate(site):
    store, sign_in, _ = site
    machine(store, node="secret-machine", users=("slot01",))
    configure(store, "secret-machine")
    store.set_price("20", "USD", by="operator", now=1)
    public = sign_in().call("GET", "/en/").body
    table = public.split('id="slot-pricing"', 1)[1].split('</section>', 1)[0]
    assert 'Machine 1' in table and 'ExampleHost' in table and 'ExampleIP' in table
    assert 'ISP' in table and '$10.67 / month' in table and '10%' in table
    assert all(secret not in table for secret in ("secret-machine", "slot01", "erik@example.com",
                                                 "4.70", "5.00", "token_hash", "access_host"))
    assert "A slot costs <strong>$20" not in public


def test_public_offer_currencies_are_not_silently_converted(site):
    store, sign_in, _ = site
    machine(store, "m1", users=("slot01",))
    machine(store, "m2", users=("slot01",))
    configure(store, "m1", host="ExampleResidential", ip="ExampleResidential", kind="residential",
              cost="89", ip_cost="10", currency="CNY")
    configure(store, "m2", host="ExampleBundled", ip="ExampleBundled", kind="residential",
              cost="14.99", ip_cost="1")
    page = sign_in().call("GET", "/en/").body
    assert '¥108.90 CNY / month' in page and '$17.59 / month' in page
    assert 'Residential' in page


def test_unknown_component_never_uses_legacy_rate_or_becomes_claimable(site):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    store.set_node_offer("m1", host_brand="ExampleHost", ip_brand="ExampleIP", ip_type="isp")
    store.set_price("20", "USD", by="operator", now=1)
    user = sign_in(quota=1)
    assert 'Price pending' in user.call("GET", "/en/").body
    assert 'Claim this slot' not in user.page()
    response = user.press("/account/claim", machine_number="1")
    assert response.status == 303 and 'no-slot' in response.getheader('Location')
    assert store.list_slots()[0]['state'] == slots.FREE


def test_partial_rollout_keeps_unconfigured_nodes_unpriced_and_unclaimable(site):
    store, sign_in, _ = site
    machine(store, 'm1', users=("slot01",))
    machine(store, 'm2', users=("slot01",))
    configure(store, 'm1')
    store.set_price('20', 'USD', by='operator', now=1)
    user = sign_in(quota=1)
    catalogue = user.call('GET', '/en/').body.split('id="slot-pricing"', 1)[1]
    assert 'Price pending' in catalogue
    assert '$20 / month' not in catalogue
    assert user.press('/account/claim', machine_number='2').getheader('Location').startswith(
        '/account?note=no-slot')
    assert store.list_slots(held_by=user.account['id']) == []


@pytest.mark.parametrize('present', [None, 1])
def test_public_free_requires_machine_confirmation_of_absence(site, present):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    configure(store)
    store._conn.execute('UPDATE slots SET present = ?', (present,))
    store._conn.commit()
    page = sign_in().call('GET', '/en/').body
    table = page.split('class="offer-table"', 1)[1].split('</table>', 1)[0]
    assert 'Preparing' in table and '>Free<' not in table


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_disabled_machine_is_not_offered_but_its_records_are_retained(site, language):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    configure(store, host='RetiredHost', ip='RetiredIP')
    machine(store, 'm2', users=("slot01",))
    configure(store, 'm2', host='CurrentHost', ip='CurrentIP')
    store.set_enabled('m1', False)
    page = sign_in().call('GET', f'/{language}/').body
    table = page.split('class="offer-table"', 1)[1].split('</table>', 1)[0]
    assert 'Machine 1' not in table and '主机 1' not in table
    assert 'RetiredHost' not in table and 'RetiredIP' not in table
    assert 'CurrentHost' in table and 'CurrentIP' in table
    assert '/status#machine-1' not in table
    assert store.get_node('m1')['enabled'] is False
    assert store.get_node_offer('m1').host_brand == 'RetiredHost'
    assert len(store.list_slots(node_id='m1')) == 1


def test_no_catalogue_when_only_disabled_machines_have_offers(site):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    configure(store, host='RetiredHost')
    store.set_enabled('m1', False)
    page = sign_in().call('GET', '/en/').body
    assert 'id="slot-pricing"' not in page and 'RetiredHost' not in page


@pytest.mark.parametrize('has_report', [False, True])
def test_enabled_unavailable_machine_stays_in_public_catalogue(site, has_report):
    store, sign_in, _ = site
    machine(store, users=("slot01",), heard=1)
    configure(store)
    if not has_report:
        store._conn.execute("DELETE FROM heartbeats WHERE node_id = 'm1'")
        store._conn.commit()
    page = sign_in().call('GET', '/en/').body
    table = page.split('class="offer-table"', 1)[1].split('</table>', 1)[0]
    assert 'Machine 1' in table and 'ExampleHost' in table and 'Unavailable' in table
    assert ('/status#machine-1' in table) is has_report
    assert '>Free<' not in table


def test_catalogue_explains_fixed_markup_without_exposing_supplier_costs(site):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    store.set_node_offer('m1', host_brand='ExampleHost', ip_brand='ExampleIP', ip_type='isp',
                         hosting=offers.Cost('64.20', 'USD', 'year'),
                         ip=offers.Cost('5.40', 'USD', '30_days'))
    page = sign_in().call('GET', '/en/').body
    section = page.split('id="slot-pricing"', 1)[1].split('</section>', 1)[0]
    assert '$11.91 / month' in section
    assert 'fixed 10% markup' in section and 'average calendar month' in section
    assert 'USD offers' in section and 'not a guaranteed quality score' in section
    assert all(secret not in section for secret in ('64.20', '5.40', 'hosting_cost', '30_days'))


@pytest.mark.parametrize("number", ["", "0", "-1", "1.0", "1e0", "1\n", "99999999999", "9"])
def test_claim_requires_an_explicit_existing_public_machine_choice(site, number):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    configure(store)
    user = sign_in(quota=1)
    response = user.press('/account/claim', machine_number=number)
    assert response.status == 303 and 'no-slot' in response.getheader('Location')
    assert store.list_slots()[0]['state'] == slots.FREE


def test_claim_uses_the_selected_price_tier_not_first_free_machine(site):
    store, sign_in, _ = site
    machine(store, "m1", users=("slot01",))
    machine(store, "m2", users=("slot01",))
    configure(store, "m1", host="ExampleResidential", kind="residential", cost="20", ip_cost="0")
    configure(store, "m2")
    user = sign_in(quota=1)
    before = user.page()
    assert 'Claim this slot' in before and '$10.67 / month' in before
    assert user.press('/account/claim', machine_number="2").status == 303
    [held] = store.list_slots(held_by=user.account['id'])
    assert held['node_id'] == 'm2'
    assert 'ExampleHost' in user.page() and 'ExampleIP' in user.page()
    assert store.list_slots(node_id='m1')[0]['state'] == slots.FREE


@pytest.mark.parametrize("restricted", ["disabled", "offline", "reserved", "occupied"])
def test_choice_does_not_bypass_existing_claim_restrictions(site, restricted):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    configure(store)
    user = sign_in(quota=1)
    if restricted == 'disabled':
        store.set_enabled('m1', False)
    elif restricted == 'offline':
        store._conn.execute('UPDATE heartbeats SET ts = 1')
        store._conn.commit()
    else:
        other = sign_in('other-google', 'other@example.com', quota=1)
        if restricted == 'reserved':
            store.reserve_machine('m1', other.account['id'])
        else:
            store.claim_slot(other.account['id'], node_id='m1', now=time.time())
    assert 'Claim this slot' not in user.page()
    response = user.press('/account/claim', machine_number='1')
    assert response.status == 303 and 'no-slot' in response.getheader('Location')
    assert store.list_slots(held_by=user.account['id']) == []


def test_new_metadata_is_public_in_both_languages_and_status_stays_neutral(site):
    store, sign_in, _ = site
    machine(store, 'secret-machine', users=("slot01",))
    configure(store, 'secret-machine')
    browser = sign_in()
    for lang, words in [('en', 'Slots &amp; pricing'), ('zh', '槽位与价格')]:
        page = browser.call('GET', f'/{lang}/').body
        assert words in page and 'ExampleHost' in page and '$10.67' in page
        assert ('价格待确认' not in page if lang == 'en' else 'Price pending' not in page)
    status = browser.call('GET', '/en/status').body
    assert 'ExampleHost / ExampleIP' in status and 'secret-machine' not in status


@pytest.mark.parametrize('path', ['/en/docs/guide', '/en/docs/terms'])
def test_customer_pages_stop_promising_one_global_price(site, path):
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    configure(store)
    store.set_price('20', 'USD', by='operator', now=1)
    page = sign_in().call('GET', path)
    assert page.status == 200 and '10%' in page.body and '#slot-pricing' in page.body
    assert 'A slot costs <strong>$20' not in page.body


def test_operator_can_set_offer_but_an_owner_and_missing_csrf_cannot(request):
    store, call = request.getfixturevalue("console")
    store.add_node('m1', 'operator', now=1)
    fields = {'host_brand': 'ExampleHost', 'ip_brand': 'ExampleIP', 'ip_type': 'isp',
              'hosting_cost': '4.70', 'hosting_currency': 'USD', 'hosting_period': 'month',
              'ip_cost': '5.00', 'ip_currency': 'USD', 'ip_period': 'month'}
    for kwargs in ({'who': 'owner'}, {'who': 'admin', 'csrf': 'bad'}):
        assert call('POST', '/actions/machine/m1/offer', fields, **kwargs).status == 403
        assert store.get_node_offer('m1') is None
    assert call('POST', '/actions/machine/m1/offer', fields).status == 303
    assert offers.monthly_price(store.get_node_offer('m1')).amount == '10.67'
    before = store.get_node_offer('m1')
    assert call('POST', '/actions/machine/m1/offer', {**fields, 'hosting_cost': 'NaN'}).status == 400
    assert store.get_node_offer('m1') == before
    assert call('POST', '/actions/machine/m1/offer-clear', {}).status == 303
    assert store.get_node_offer('m1') is None


def test_provider_labels_cannot_be_endpoint_or_contact_disclosures():
    for label in ('192.0.2.3', 'https://host.example', 'owner@example.com', '<script>'):
        with pytest.raises(offers.OfferError):
            offers.Offer(host_brand=label)


def test_catalogue_reads_no_private_identity_from_corrupt_configured_offer():
    store = Store(':memory:')
    try:
        machine(store, 'private-name', users=("slot01",))
        store._conn.execute("UPDATE nodes SET offer_json = ?", ('{"host_brand":"192.0.2.3"}',))
        store._conn.commit()
        html = project_html(offer_ui.catalogue(store, time.time(), 300), 'en')
        assert 'Price pending' in html and 'Not provided' in html
        assert '192.0.2.3' not in html and 'private-name' not in html
        assert re.search(r'Machine [0-9]+', html)
    finally:
        store.close()


def test_populated_offers_preserve_json_api_scoping_and_never_serialize_costs(request):
    store, call = request.getfixturevalue("console")
    store.add_node('m1', 'ana', now=1)
    store.add_node('m2', 'bob', now=1)
    configure(store, 'm1')
    configure(store, 'm2', cost='7.13')
    for who, count in (('admin', 2), ('owner', 1)):
        response = call('GET', '/api/nodes', who=who)
        assert response.status == 200
        assert len(json.loads(response.body)['nodes']) == count
        assert all(field not in response.body for field in (
            'offer_json', 'hosting', 'ip_brand', '4.70', '7.13', '5.00', 'token_hash'))
    assert call('GET', '/api/nodes', who='anonymous').status == 401


def test_populated_offer_does_not_leak_or_break_heartbeat_desired_state(site):
    store, sign_in, _ = site
    token = store.add_node('m1', 'operator', now=time.time())
    configure(store)
    browser = sign_in()
    # Send JSON through a real HTTP connection rather than Browser's form encoder.
    connection = http.client.HTTPConnection('127.0.0.1', browser.port, timeout=10)
    connection.request('POST', '/api/heartbeat',
                       body=json.dumps({'node_id': 'm1', 'mode': 'machine'}),
                       headers={'Authorization': 'Bearer ' + token,
                                'Content-Type': 'application/json'})
    reply = connection.getresponse()
    body = reply.read().decode()
    connection.close()
    assert reply.status == 200
    assert json.loads(body)['desired']
    assert all(field not in body for field in ('offer_json', 'hosting', '4.70', '5.00'))
