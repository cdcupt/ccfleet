"""The available-slot browser bounds rows and searches approved offer metadata only."""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

from ccfleetd import offer_ui, offers, slots
from ccfleetd.bilingual import project_html

NOW = 1000.0
ACCOUNT = {"id": "viewing-account"}


class Inventory:
    def __init__(self, count=1):
        self.nodes = []
        self.slots = []
        self.beats = {}
        for number in range(1, count + 1):
            node_id = f"private-node-{number}"
            self.nodes.append({"id": node_id, "enabled": True, "capacity": 1,
                               "public_status_number": number, "reserved_for": None,
                               "owner": "private-owner", "name": "private-nickname",
                               "access_host": "192.0.2.99", "email": "private@example.test",
                               "offer": offers.Offer("OVHcloud", "Decodo", "isp",
                                                     offers.Cost("5.35", "USD"),
                                                     offers.Cost("3.33", "USD"))})
            self.slots.append({"node_id": node_id, "kind": slots.MACHINE_SLOT,
                               "state": slots.FREE, "present": 0})
            self.beats[node_id] = {"ts": NOW}

    def list_nodes(self):
        return list(self.nodes)

    def list_slots(self, *, kind=None):
        return [slot for slot in self.slots if kind is None or slot["kind"] == kind]

    def latest_heartbeats(self):
        return dict(self.beats)


class Tags(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def attributes(self, tag):
        return [attrs for found, attrs in self.tags if found == tag]


def render(store, query=None, *, can_claim=True, language="en", csrf="safe-csrf"):
    source = offer_ui.browse(store, ACCOUNT, csrf, NOW, 90, query or {}, can_claim=can_claim)
    return project_html(source, language, '/account/available')


def machines(source):
    return [int(value) for value in re.findall(r'name="machine_number" value="([0-9]+)"', source)]


def test_inventory_is_twenty_rows_and_in_numeric_public_machine_order():
    inventory = Inventory(43)
    inventory.nodes.reverse()
    page = render(inventory)
    assert machines(page) == list(range(1, 21))
    assert 'Showing 1–20 of 43 matching available machines.' in page
    assert 'Page 1 of 3' in page and '>Previous<' not in page
    assert len([attrs for attrs in Tags(page).attributes('form')
                if attrs.get('method') == 'post']) == 20
    assert machines(render(inventory, {'page': '2'})) == list(range(21, 41))
    last = render(inventory, {'page': '999999'})
    assert machines(last) == [41, 42, 43] and 'Page 3 of 3' in last
    assert 'Showing 41–43 of 43 matching available machines.' in last


@pytest.mark.parametrize('query', [{'q': 'dEcOdO'}, {'q': 'OVHCLOUD'}, {'q': ' isp '}])
def test_search_is_case_insensitive_for_approved_host_ip_and_network_type(query):
    assert machines(render(Inventory(2), query)) == [1, 2]


def test_search_can_select_stable_machine_label_and_never_uses_private_fields():
    inventory = Inventory(12)
    assert machines(render(inventory, {'q': 'Machine 12'})) == [12]
    assert machines(render(inventory, {'q': '主机 12'})) == [12]
    for private in ('private-node', 'private-owner', 'private-nickname', 'private@example.test',
                    '192.0.2.99'):
        page = render(inventory, {'q': private})
        assert machines(page) == []
        # The viewer's escaped search input is allowed; no inventory identity is emitted.
        assert 'No available machines match these filters.' in page
    unfiltered = render(inventory)
    assert all(private not in unfiltered for private in ('private-node', 'private-owner',
               'private-nickname', 'private@example.test', '192.0.2.99'))


def test_provider_filter_exactly_matches_host_or_ip_brand_and_combines_with_type_and_query():
    inventory = Inventory(3)
    inventory.nodes[1]['offer'] = offers.Offer('OVHcloud', 'IPRoyal', 'isp',
                                              offers.Cost('5.35', 'USD'),
                                              offers.Cost('5.40', 'USD', '30_days'))
    inventory.nodes[2]['offer'] = offers.Offer('ATTVPS', 'AT&T', 'residential',
                                              offers.Cost('27.44', 'USD'),
                                              offers.Cost('0', 'USD'))
    assert machines(render(inventory, {'brand': 'OVHcloud'})) == [1, 2]
    assert machines(render(inventory, {'brand': 'IPRoyal'})) == [2]
    assert machines(render(inventory, {'brand': 'AT&T', 'type': 'residential'})) == [3]
    assert machines(render(inventory, {'brand': 'OVHcloud', 'type': 'residential'})) == []
    assert machines(render(inventory, {'q': 'Machine 2', 'brand': 'OVHcloud', 'type': 'isp'})) == [2]
    # Invalid provider/type selections fall back to the unfiltered choices.
    assert machines(render(inventory, {'brand': 'OVH', 'type': 'unknown'})) == [1, 2, 3]


@pytest.mark.parametrize('restriction', ['disabled', 'capacity', 'reserved', 'held', 'present',
    'unknown-absence', 'stale', 'no-heartbeat', 'unpriced', 'non-usd', 'no-number', 'owner-slot'])
def test_browser_uses_all_existing_eligibility_requirements_before_rendering(restriction):
    inventory = Inventory(2)
    node, slot = inventory.nodes[0], inventory.slots[0]
    if restriction == 'disabled':
        node['enabled'] = False
    elif restriction == 'capacity':
        node['capacity'] = 0
    elif restriction == 'reserved':
        node['reserved_for'] = 'different-account'
    elif restriction == 'held':
        slot['state'] = slots.ACTIVE
    elif restriction == 'present':
        slot['present'] = 1
    elif restriction == 'unknown-absence':
        slot['present'] = None
    elif restriction == 'stale':
        inventory.beats[node['id']]['ts'] = NOW - 91
    elif restriction == 'no-heartbeat':
        del inventory.beats[node['id']]
    elif restriction == 'unpriced':
        node['offer'] = offers.Offer('Unpriced Host', 'Unknown Exit', 'isp')
    elif restriction == 'non-usd':
        node['offer'] = offers.Offer('Foreign Host', 'Foreign Exit', 'isp',
                                     offers.Cost('10', 'CNY'), offers.Cost('0', 'CNY'))
    elif restriction == 'no-number':
        node['public_status_number'] = None
    elif restriction == 'owner-slot':
        slot['kind'] = slots.OWNER_SLOT
    assert machines(render(inventory)) == [2]
    assert 'Unpriced Host' not in render(inventory) and 'Foreign Host' not in render(inventory)


def test_reservation_for_viewer_remains_eligible_and_provider_options_use_only_eligible_nodes():
    inventory = Inventory(2)
    inventory.nodes[0]['reserved_for'] = ACCOUNT['id']
    inventory.nodes[1]['offer'] = offers.Offer('Hidden Host', 'Hidden Exit', 'isp',
                                              offers.Cost('10', 'USD'), offers.Cost('0', 'USD'))
    inventory.nodes[1]['enabled'] = False
    page = render(inventory)
    assert machines(page) == [1]
    options = [attrs['value'] for attrs in Tags(page).attributes('option')]
    assert 'Decodo' in options and 'OVHcloud' in options
    assert 'Hidden Host' not in options and 'Hidden Exit' not in options


def test_pagination_links_preserve_filters_and_escape_special_provider_names():
    inventory = Inventory(42)
    for node in inventory.nodes:
        node['offer'] = offers.Offer('Host & Co', 'Exit+Brand', 'isp',
                                     offers.Cost('5.35', 'USD'), offers.Cost('3.33', 'USD'))
    page = render(inventory, {'q': 'Host & Co', 'brand': 'Exit+Brand', 'type': 'isp', 'page': '2'})
    links = [attrs['href'] for attrs in Tags(page).attributes('a')
             if '/account/available?' in attrs.get('href', '')]
    assert len(links) == 2
    queries = [parse_qs(urlsplit(link).query) for link in links]
    assert {query['page'][0] for query in queries} == {'1', '3'}
    for query in queries:
        assert query['q'] == ['Host & Co'] and query['brand'] == ['Exit+Brand']
        assert query['type'] == ['isp']
    assert '&amp;brand=' in page


@pytest.mark.parametrize('page', ['', '0', '-1', '1e3', '1.0', '１２', '9' * 1000])
def test_invalid_page_input_is_bounded_and_returns_first_page(page):
    assert machines(render(Inventory(25), {'page': page})) == list(range(1, 21))


def test_search_and_csrf_are_escaped_and_query_length_is_bounded():
    inventory = Inventory(1)
    page = render(inventory, {'q': '\"><script>alert(1)</script>'}, csrf='\"><img src=x>')
    assert '<script>' not in page and '<img ' not in page
    assert '&lt;script&gt;' in page
    selected = offer_ui._browse_params({'q': 'a' * 10000}, {'OVHcloud'})
    assert selected['q'] == 'a' * 80
    # CSRF needs a row to render; it stays a single escaped field.
    page = render(inventory, csrf='\"><img src=x>')
    hidden = [attrs for attrs in Tags(page).attributes('input') if attrs.get('name') == 'csrf']
    assert len(hidden) == 1 and hidden[0]['value'] == '\"><img src=x>'
    assert '<img ' not in page


def test_read_only_browser_has_prices_but_no_claim_forms_or_csrf():
    page = render(Inventory(2), can_claim=False)
    assert '$10.68 / month' in page and 'Monthly price breakdown' in page
    assert 'Claim this slot' not in page and '/account/claim' not in page
    assert 'safe-csrf' not in page and machines(page) == []
    assert not any(attrs.get('method') == 'post' for attrs in Tags(page).attributes('form'))


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_filters_counts_and_native_options_are_accessible_in_each_language(language):
    page = render(Inventory(1), language=language)
    tags = Tags(page)
    assert any(attrs.get('method') == 'get' and attrs.get('action', '').endswith('/account/available')
               for attrs in tags.attributes('form'))
    assert len(tags.attributes('label')) == 3
    assert {attrs['name'] for attrs in tags.attributes('select')} == {'brand', 'type'}
    assert 'scope="col"' in page and 'scope="row"' in page and 'tabindex="0"' in page
    assert all('<span' not in match for match in re.findall(r'<option[^>]*>.*?</option>', page))
    assert 'data-copy-' not in page
    text = unescape(re.sub(r'<[^>]+>', '', page))
    if language == 'en':
        assert 'All providers' in text and 'All network types' in text and 'Page 1 of 1' in text
        assert '全部服务商' not in text
    else:
        assert '全部服务商' in text and '全部网络类型' in text and '第 1／1 页' in text
        assert 'All providers' not in text


def test_displayed_alpha_quote_and_included_ip_breakdown_are_preserved():
    inventory = Inventory(2)
    inventory.nodes[1]['offer'] = offers.Offer('ATTVPS', 'AT&T', 'residential',
                                              offers.Cost('27.44', 'USD'),
                                              offers.Cost('0', 'USD'))
    page = render(inventory)
    assert '$10.68 / month' in page and '$30.18 / month' in page
    assert '$8.68' in page and '$2.00' in page and '$2.74' in page
    assert 'VPS + exit IP (bundled)' in page and 'Total monthly price' in page
    assert 'data-language="both"' not in page


@pytest.mark.parametrize('query', [{'q': None}, {'q': ['Decodo']}, {'brand': 'Decodo\n'},
    {'brand': 'a' * 10000}, {'type': 'isp\n'}, {'page': None}])
def test_non_string_or_invalid_filters_have_safe_bounded_fallbacks(query):
    assert machines(render(Inventory(2), query)) == [1, 2]


def test_long_provider_value_cannot_match_a_truncated_prefix():
    name = 'A' * 48
    selected = offer_ui._browse_params({'brand': name + 'extra'}, {name})
    assert selected['brand'] == ''
