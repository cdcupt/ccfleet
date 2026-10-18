"""Bounded real homepage examples and a public catalogue without customer controls."""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from ccfleetd import offer_ui, offers, slots
from ccfleetd.bilingual import project_html
from tests.test_slot_browser import ACCOUNT, NOW, Inventory, Tags, machines


def page(inventory, query=None, language='en'):
    return project_html(offer_ui.public_browse(inventory, NOW, 90, query or {}), language, '/slots')


def home(inventory, language='en'):
    return project_html(offer_ui.catalogue(inventory, NOW, 90), language, '/')


def table_numbers(source):
    table = source.split('<tbody>', 1)[1].split('</tbody>', 1)[0] if '<tbody>' in source else ''
    return [int(number) for number in re.findall(r'Machine ([0-9]+)', table)]


def set_offer(node, host='OVHcloud', ip='Decodo', kind='isp', hosting='5.35', exit_ip='3.33'):
    node['offer'] = offers.Offer(host, ip, kind, offers.Cost(hosting, 'USD'),
                                offers.Cost(exit_ip, 'USD'))


def test_home_examples_are_deduplicated_and_capped_at_three_real_configurations():
    inventory = Inventory(40)
    for index in (5, 10, 15, 20, 25):
        set_offer(inventory.nodes[index], host=f'Provider {index}', hosting=str(index + 1))
    inventory.nodes.reverse()
    result = home(inventory)
    assert result.count('<article class="offer-example">') == 3
    assert 'class="offer-examples"' in result and 'id="slot-pricing"' in result
    assert 'href="/slots"' in result and 'View all slots' in result
    assert '<table' not in result and 'name="machine_number"' not in result
    assert result.count('OVHcloud') == 1
    assert 'not a promise that a matching slot is available' in result
    assert 'class="offer-policy"' in result and 'Alpha pricing &amp; service limits' in result


def test_home_includes_both_network_types_when_later_residential_configuration_exists():
    inventory = Inventory(40)
    set_offer(inventory.nodes[10], host='Other Host')
    set_offer(inventory.nodes[30], host='Residential Host', ip='Residential Network',
              kind='residential', hosting='27.44', exit_ip='0')
    result = home(inventory)
    assert result.count('<article class="offer-example">') == 3
    assert 'Residential Host' in result and 'ip-category residential' in result
    assert 'ip-category isp' in result and '$30.18 / month' in result
    assert 'VPS + exit IP (bundled)' in result and 'quality ranking' in result
    assert 'premium network category' not in result and 'standard category' not in result


def test_home_can_represent_all_three_network_types_without_more_than_three_examples():
    inventory = Inventory(40)
    set_offer(inventory.nodes[20], host='Residential Host', kind='residential')
    set_offer(inventory.nodes[30], host='Datacenter Host', kind='datacenter')
    result = home(inventory)
    assert result.count('<article class="offer-example">') == 3
    for kind in ('isp', 'residential', 'datacenter'):
        assert f'ip-category {kind}' in result


@pytest.mark.parametrize('language,query', [('en', 'Datacenter'), ('zh', '机房')])
def test_datacenter_filter_and_search_remain_public_metadata_only_in_each_language(language, query):
    inventory = Inventory(3)
    set_offer(inventory.nodes[1], host='Example Datacenter', kind='datacenter')
    result = page(inventory, {'type': 'datacenter', 'q': query}, language=language)
    table = result.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
    assert re.findall(r'(?:Machine|主机) ([0-9]+)', table) == ['2']
    assert ('Datacenter' if language == 'en' else '机房 IP') in table
    assert 'value="datacenter" selected' in result
    assert all(secret not in result for secret in ('private-node', 'private-owner', '192.0.2.99'))


def test_missing_price_never_becomes_a_home_example_and_public_catalogue_stays_honest():
    inventory = Inventory(2)
    inventory.nodes[0]['offer'] = offers.Offer('Unpriced Host', 'Unknown Exit', 'isp')
    result = home(inventory)
    assert result.count('<article class="offer-example">') == 1
    assert 'Unpriced Host' not in result and 'Unpriced Host' in page(inventory)
    inventory.nodes[1]['offer'] = offers.Offer('Another Host', 'Another Exit', 'isp')
    result = home(inventory)
    assert '<article' not in result and 'Pricing unavailable' in result
    assert 'View all slots' in result and 'Pricing unavailable' in page(inventory)


def test_disabled_and_undeclared_machines_are_absent_without_losing_unavailable_entries():
    inventory = Inventory(5)
    inventory.nodes[0]['enabled'] = False
    inventory.slots = [slot for slot in inventory.slots if slot['node_id'] != 'private-node-2']
    inventory.beats.pop('private-node-3')
    inventory.nodes[3]['public_status_number'] = None
    result = page(inventory)
    assert table_numbers(result) == [3, 5]
    assert 'Unavailable' in result and '/status#machine-3' not in result
    assert '/status#machine-5' in result


@pytest.mark.parametrize('state,present,wanted', [(slots.FREE, 0, 'Available'),
    (slots.ACTIVE, 1, 'In use'), (slots.FREE, None, 'Preparing'),
    (slots.CLAIMING, 0, 'In use')])
def test_public_rows_show_state_without_holder_identity(state, present, wanted):
    inventory = Inventory(1)
    inventory.slots[0].update(state=state, present=present, held_by='private-customer')
    result = page(inventory)
    body = result.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
    assert wanted in body and 'private-customer' not in result
    assert all(value not in result for value in ('private-node', 'private-owner',
        'private-nickname', 'private@example.test', '192.0.2.99', 'csrf', '/account/claim'))
    assert not any(attrs.get('method') == 'post' for attrs in Tags(result).attributes('form'))


def test_stale_heartbeat_does_not_hide_public_price_or_turn_machine_available():
    inventory = Inventory(1)
    inventory.beats['private-node-1']['ts'] = NOW - 100
    result = page(inventory)
    body = result.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
    assert 'Unavailable' in body and 'Available' not in body
    assert '$10.68 / month' in body


def test_declared_owner_slot_remains_public_but_not_in_private_available_pool():
    inventory = Inventory(1)
    inventory.slots[0].update(kind=slots.OWNER_SLOT, state=slots.ACTIVE, present=1)
    assert table_numbers(page(inventory)) == [1]
    assert 'In use' in page(inventory)
    assert offer_ui.available(inventory, ACCOUNT, NOW, 90) == []


def test_public_pagination_is_twenty_rows_and_preserves_search_provider_and_type():
    inventory = Inventory(45)
    for node in inventory.nodes:
        set_offer(node, host='Host & Co', ip='Exit+Brand')
    result = page(inventory, {'q': 'Host & Co', 'brand': 'Exit+Brand', 'type': 'isp', 'page': '2'})
    assert table_numbers(result) == list(range(21, 41))
    assert 'Showing 21–40 of 45 matching machines.' in result
    links = [attrs['href'] for attrs in Tags(result).attributes('a')
             if '/slots?' in attrs.get('href', '')]
    assert len(links) == 2
    for link in links:
        query = parse_qs(urlsplit(link).query)
        assert query['q'] == ['Host & Co'] and query['brand'] == ['Exit+Brand']
        assert query['type'] == ['isp'] and query['page'][0] in ('1', '3')
    assert '&amp;brand=' in result
    assert table_numbers(page(inventory, {'page': '999999'})) == [41, 42, 43, 44, 45]
    assert table_numbers(page(inventory, {'page': '1e9'})) == list(range(1, 21))


def test_public_filters_include_unpriced_provider_and_exact_type_and_brand():
    inventory = Inventory(3)
    inventory.nodes[0]['offer'] = offers.Offer('Unpriced Host', 'Unknown Exit', 'isp')
    set_offer(inventory.nodes[1], host='Residential Host', kind='residential')
    assert table_numbers(page(inventory, {'brand': 'Unpriced Host'})) == [1]
    assert table_numbers(page(inventory, {'type': 'residential'})) == [2]
    assert table_numbers(page(inventory, {'q': 'Machine 3', 'brand': 'Decodo'})) == [3]
    assert table_numbers(page(inventory, {'q': 'private-node'})) == []
    assert table_numbers(page(inventory, {'q': 'private@example.test'})) == []


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_public_controls_and_complete_alpha_policy_work_in_each_language(language):
    result = page(Inventory(1), language=language)
    forms = Tags(result).attributes('form')
    assert len(forms) == 1 and forms[0]['method'] == 'get'
    assert forms[0]['action'].endswith('/slots')
    assert 'name="q"' in result and 'name="brand"' in result and 'name="type"' in result
    assert 'aria-label=' in result and 'tabindex="0"' in result
    assert 'data-copy-' not in result and 'name="csrf"' not in result
    if language == 'en':
        for words in ('Sign in to choose a slot', '10% of resource cost, with a $2 minimum',
                      '365/360', 'does not charge or renew automatically',
                      'already agreed billing period retains its agreed price',
                      'multiple users', 'Anthropic API usage is billed separately'):
            assert words in result
        assert '全部服务商' not in result
    else:
        assert '全部服务商' in result and '登录并选择槽位' in result
        assert '续费前会提供新报价' in result and 'All providers' not in result


def test_network_badges_have_distinct_accessible_neutral_colors_and_text_labels():
    css = offer_ui.CSS
    residential = re.search(r'\.ip-category\.residential\{([^}]+)', css)[1]
    isp = re.search(r'\.ip-category\.isp\{([^}]+)', css)[1]
    datacenter = re.search(r'\.ip-category\.datacenter\{([^}]+)', css)[1]
    assert len({residential, isp, datacenter}) == 3
    assert '#60269c' in residential and '#1253a0' in isp and '#374151' in datacenter

    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        return sum((channel / 12.92 if channel <= 0.04045 else
                    ((channel + 0.055) / 1.055) ** 2.4) * weight
                   for channel, weight in zip(channels, (0.2126, 0.7152, 0.0722)))

    for foreground, background in (('#60269c', '#f4edff'), ('#1253a0', '#edf5ff'),
                                   ('#374151', '#f3f4f6')):
        ratio = (luminance(background) + 0.05) / (luminance(foreground) + 0.05)
        assert ratio >= 4.5
    assert 'Residential' in project_html(offer_ui.ip_label('residential'), 'en')
    assert 'ISP' in project_html(offer_ui.ip_label('isp'), 'en')
    assert 'Datacenter' in project_html(offer_ui.ip_label('datacenter'), 'en')
    assert '机房 IP' in project_html(offer_ui.ip_label('datacenter'), 'zh')


def test_public_example_prices_are_the_authoritative_recorded_quotes():
    inventory = Inventory(3)
    set_offer(inventory.nodes[1], host='Other Host', exit_ip='5.40')
    set_offer(inventory.nodes[2], host='Residential Host', kind='residential',
              hosting='27.44', exit_ip='0')
    result = home(inventory)
    for node in inventory.nodes:
        quote = offers.quote(node['offer'])
        assert f'${quote.total.amount} / month' in result
        assert Decimal(quote.total.amount) == Decimal(quote.resource.amount) + Decimal(quote.service_fee.amount)
    assert machines(result) == []


def test_home_uses_lowest_recorded_quote_per_type_then_lowest_remaining_configuration():
    inventory = Inventory(5)
    set_offer(inventory.nodes[0], host='ATTVPS', ip='AT&T', kind='residential',
              hosting='27.44', exit_ip='0')
    inventory.nodes[1]['offer'] = offers.Offer('OVHcloud', 'IPRoyal', 'isp',
                                              offers.Cost('5.35', 'USD'),
                                              offers.Cost('5.40', 'USD', '30_days'))
    set_offer(inventory.nodes[3], host='AAITR', ip='Residential Network', kind='residential',
              hosting='22.28', exit_ip='0')
    result = home(inventory)
    assert result.count('<article class="offer-example">') == 3
    assert 'ATTVPS' not in result
    assert 'Decodo' in result and 'IPRoyal' in result and 'AAITR' in result
    assert '$10.68 / month' in result and '$12.83 / month' in result and '$24.51 / month' in result
    assert result.index('$10.68 / month') < result.index('$12.83 / month') < result.index('$24.51 / month')


def test_all_unconfigured_prices_keep_a_contextual_home_link_and_full_public_rows():
    inventory = Inventory(2)
    for node in inventory.nodes:
        node['offer'] = None
    result = home(inventory)
    assert 'Pricing unavailable' in result and 'href="/slots"' in result
    assert '<article' not in result
    assert table_numbers(page(inventory)) == [1, 2]
    assert 'Pricing unavailable' in page(inventory)


def test_example_deduplication_preserves_distinct_quotes_with_equal_rounded_components():
    inventory = Inventory(2)
    set_offer(inventory.nodes[0], hosting='0.001', exit_ip='0.001')
    set_offer(inventory.nodes[1], hosting='0.004', exit_ip='0.004')
    result = home(inventory)
    assert result.count('<article class="offer-example">') == 2
    assert '$2.00 / month' in result and '$2.01 / month' in result
