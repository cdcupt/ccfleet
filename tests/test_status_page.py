"""The public status page, and each holder's line about their slot's machine.

Public machine rows use persistent neutral numbers, availability and last reports.
Private node/slot/holder identities are not public labels.
"""
from __future__ import annotations

import re
import threading
import time
from html.parser import HTMLParser

import pytest

from ccfleetd import names, render, status, statuspage, usersite
from ccfleetd.bilingual import SCRIPT_TAG as LANGUAGE_TAG
from ccfleetd.bilingual import label, project_html
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.rules import LEVEL_CRITICAL, LEVEL_WARN
from tests.conftest import refresh_of
from tests.test_admin_host import ADMIN, PRODUCT, split  # noqa: F401  (the fixture)
from tests.test_usersite import report, site  # noqa: F401  (the fixture, by name)
from tests.test_usersite_bilingual import Page
from tests.test_usersite_v2 import one_slot_machine, whole_card

NOW = 1_790_000_040.0
NAMES = ("pool-7", "pool-8", "erik-9", "ana-1", "ana@example.com", "erik@example.com",
         "slot01", "us-west")


def fleet(store, now=NOW, ages=(20, 30, 40)):
    """As live: somebody's own node, and two shared machines of which one is
    held, so its slot is named after its holder."""
    store.add_account("a1", "sub-a1", "ana@example.com", slot_quota=1, now=now - 86400)
    store.add_account("e1", "sub-e1", "erik@example.com", slot_quota=3, now=now - 86400)
    store.set_account_handle("a1", "ana")          # a name that is plainly a person's
    store.add_node("erik-9", "erik", region="us-west", now=now - 86400)
    store.hold_owner_node("erik-9", "e1", unix_user="erik", now=now - 86400)
    for node in ("pool-7", "pool-8"):
        store.add_node(node, "op", region="us-west", now=now - 86400)
        store.add_slot(node, node, "slot01", now=now - 86400)
        store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}],
                                now=now - 86400)
    held = store.claim_slot("a1", now=now - 3600, node_id="pool-7")
    assert held["name"] == "ana-1"
    store.insert_heartbeat("erik-9", now - ages[0], {"node_id": "erik-9"})
    for node, age in (("pool-7", ages[1]), ("pool-8", ages[2])):
        store.insert_heartbeat(node, now - age, {"node_id": node, "mode": "machine",
                                                 "slots": []})


def page_of(store, cfg, now=NOW):
    return statuspage.page(store, cfg, None, now)


def shown(page):
    """What a person reads: the tags gone."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


# -- now ------------------------------------------------------------------------------------

def test_everything_up_is_all_systems_operational(store, cfg):
    fleet(store)
    page = shown(page_of(store, cfg))
    assert "All systems operational" in page and "3 of 3 operational" in page
    assert "Website and account pages" in page and "Slot machines" in page


def test_one_machine_down_is_a_partial_outage_and_says_since_when(store, cfg):
    fleet(store, ages=(20, 30, 400))
    raw = page_of(store, cfg)
    assert "Partial outage" in shown(raw) and "2 of 3 operational" in shown(raw)
    assert "1 down" in shown(raw)
    since = status.utc_iso(NOW - 400 + 300)
    assert f'<time datetime="{since}" data-local>' in raw


def test_a_degraded_machine_is_a_partial_outage_too(store, cfg):
    fleet(store)
    store.open_alert("pool-8", "disk_high", LEVEL_WARN, "disk 88% used", NOW - 120)
    page = shown(page_of(store, cfg))
    assert "Partial outage" in page and "1 degraded" in page


def test_every_machine_down_is_a_major_outage(store, cfg):
    fleet(store, ages=(2000, 2000, 2000))
    page = shown(page_of(store, cfg))
    assert "Major outage" in page and "0 of 3 operational" in page


def test_no_machines_yet(store, cfg):
    raw = page_of(store, cfg)
    page = shown(raw)
    assert "All systems operational" in page and "No slot machines yet" in page
    assert "No uptime counted yet" in page
    assert 'aria-label="Website and account pages: no data yet"' in raw


# -- what it never says ----------------------------------------------------------------------

def test_the_page_names_no_machine_and_nobody(store, cfg):
    fleet(store, ages=(20, 30, 400))
    store.open_alert("pool-7", "disk_high", LEVEL_WARN, "disk 88% used on pool-7", NOW - 60)
    for minute in range(3):
        status.record(store, NOW + 60 * minute, check_interval_s=60)
    raw = page_of(store, cfg, NOW + 180)
    for name in NAMES:
        assert name not in raw, name


def machine_row(raw, number):
    found = re.search(r'<tr\b[^>]*\bid="machine-' + str(number) + r'"[^>]*>(.*?)</tr>',
                      raw, re.S)
    assert found, number
    return found.group(1)


def test_each_machine_has_one_neutral_row_with_status_and_last_report(store, cfg):
    fleet(store, ages=(20, 30, 400))
    store.open_alert("pool-7", "disk_high", LEVEL_WARN,
                     "ana@example.com disk/path on 192.0.2.3", NOW - 60)
    nodes = {node['id']: node for node in store.list_nodes()}
    raw = page_of(store, cfg)
    assert 'id="slot-machines"' in raw
    assert raw.count('class="st-machine-row"') == 3
    owner = shown(machine_row(raw, nodes['erik-9']['public_status_number']))
    assert 'Operational' in owner
    disk = shown(machine_row(raw, nodes['pool-7']['public_status_number']))
    assert 'Degraded' in disk
    down = machine_row(raw, nodes['pool-8']['public_status_number'])
    assert 'Down' in shown(down)
    assert f'datetime="{status.utc_iso(NOW - 400)}" data-local' in down
    for private in (*NAMES, '192.0.2.3', 'disk/path'):
        assert private not in raw


@pytest.mark.parametrize("rule,level,expected", [
    ('slot_missing:private-user@example.com', LEVEL_CRITICAL, 'Down'),
    ('slot_wipe_failed:private-user', LEVEL_CRITICAL, 'Degraded'),
    ('slot_occupied:private-user', LEVEL_CRITICAL, 'Degraded'),
    ('slot_provision_failed:private-user', LEVEL_WARN, 'Degraded'),
    ('claude_missing', LEVEL_CRITICAL, 'Down'),
    ('disk_high', LEVEL_CRITICAL, 'Down'),
])
def test_machine_alerts_keep_their_status_without_exposing_reason_details(
        store, cfg, rule, level, expected):
    fleet(store)
    store.open_alert('pool-8', rule, level, 'PRIVATE_RAW_ERROR /root/private 192.0.2.8', NOW - 60)
    raw = page_of(store, cfg)
    number = store.get_node('pool-8')['public_status_number']
    row = machine_row(raw, number)
    assert expected in shown(row)
    assert '<ul>' not in row and '<p' not in row
    for private in ('private-user', 'PRIVATE_RAW_ERROR', '/root/private', '192.0.2.8'):
        assert private not in raw


@pytest.mark.parametrize("rule", status.EXCLUDED_RULES)
def test_credential_quota_and_informational_alerts_do_not_change_public_machine_status(
        store, cfg, rule):
    fleet(store)
    store.open_alert('pool-8', rule, LEVEL_CRITICAL, 'PRIVATE_ACCOUNT_REASON', NOW - 60)
    raw = page_of(store, cfg)
    assert '3 of 3 operational' in shown(raw)
    for node in store.list_nodes():
        assert 'Operational' in shown(machine_row(raw, node['public_status_number']))
    assert 'PRIVATE_ACCOUNT_REASON' not in raw and rule not in raw


def test_machine_status_shows_original_timestamp_during_restart_grace(store, cfg):
    fleet(store, ages=(600, 600, 600))
    store.set_listening_since(NOW - 5)
    raw = page_of(store, cfg)
    assert '3 of 3 operational' in shown(raw)
    assert raw.count(f'datetime="{status.utc_iso(NOW - 600)}" data-local') == 3


def test_compact_machine_sheet_omits_uptime_and_keeps_aggregate_history_read_once(
        store, cfg, monkeypatch):
    fleet(store)
    store.count_status('node:pool-7', status.GREEN, NOW, grace=0)
    store.count_status('node:pool-7', status.RED, NOW + 60, grace=0)
    number = next(node['public_status_number'] for node in store.list_nodes() if node['id'] == 'pool-7')
    calls = []
    original = store.status_minutes
    def recorded(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'status_minutes', recorded)
    raw = project_html(page_of(store, cfg, NOW + 120), 'en', '/status')
    assert 'uptime' not in shown(machine_row(raw, number))
    assert '90-day recorded uptime' not in raw
    assert len(calls) == 1
    assert len(bars(raw)) == 2


def test_machine_name_is_visible_only_to_an_authenticated_operator_and_is_escaped(store, cfg):
    fleet(store)
    admin = usersite.Viewer({'role': 'admin', 'email': 'operator@example.com'}, 'csrf')
    member = usersite.Viewer({'role': 'user', 'email': 'viewer@example.com'}, 'csrf')
    assert 'class="st-machine-name"' not in statuspage.page(store, cfg, member, NOW)
    raw = statuspage.page(store, cfg, admin, NOW)
    for slot in store.list_slots():
        assert names.display(slot) in raw
    detail = status.MachineDetail('<script>alert(1)</script>', 7, status.State(status.GREEN),
                                  ('reported',), NOW)
    html = statuspage.machine_details_panel([detail], admin, NOW)
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html and '<script>' not in html
    anonymous = statuspage.machine_details_panel([detail], None, NOW)
    assert 'alert(1)' not in anonymous


def test_operator_machine_name_is_compact_and_omits_the_different_internal_id(store, cfg):
    fleet(store)
    store.add_node('operator-private', 'op', now=NOW)
    store.add_slot('operator-private', 'operator-private', 'slot01', now=NOW)
    store.apply_slot_report('operator-private', [{'unix_user': 'slot01', 'present': False}], now=NOW)
    store.insert_heartbeat('operator-private', NOW, {'node_id': 'operator-private', 'mode': 'machine'})
    admin = usersite.Viewer({'role': 'admin', 'email': 'operator@example.com'}, 'csrf')
    member = usersite.Viewer({'role': 'user', 'email': 'viewer@example.com'}, 'csrf')
    slots = {slot['node_id']: slot for slot in store.list_slots()}
    nodes = {node['id']: node for node in store.list_nodes()}
    free_name = names.display(slots['operator-private'])
    held_name = names.display(slots['pool-7'])
    english = project_html(statuspage.page(store, cfg, admin, NOW), 'en', '/status')
    free = shown(machine_row(english, nodes['operator-private']['public_status_number']))
    held = shown(machine_row(english, nodes['pool-7']['public_status_number']))
    assert free_name in free and 'operator-private' not in free
    assert held_name in held and 'pool-7' not in held
    assert 'Internal machine ID:' not in english
    assert '<small class="st-machine-name">' in machine_row(
        english, nodes['operator-private']['public_status_number'])
    for viewer in (None, member):
        public = statuspage.page(store, cfg, viewer, NOW)
        for private in (free_name, held_name, 'pool-7', 'operator-private'):
            assert private not in public
    detail = status.MachineDetail('private-node', 10, status.State(status.GREEN), ('reported',),
                                  NOW, '<img src=x onerror=alert(1)>')
    html = statuspage.machine_details_panel([detail], admin, NOW)
    assert '&lt;img src=x onerror=alert(1)&gt;' in html and '<img' not in html


@pytest.mark.parametrize('number', [None, True, 'erik-9', 0, -1])
def test_missing_or_invalid_public_number_never_falls_back_to_a_personal_name(number):
    detail = status.MachineDetail('private-person', number, status.State(status.GREEN),
                                  ('reported',), NOW)
    html = statuspage.machine_details_panel([detail], None, NOW)
    assert 'private-person' not in html and 'class="st-machine-row"' not in html


def test_detail_snapshot_is_batched_once_and_keeps_the_original_aggregate_components(
        store, cfg, monkeypatch):
    fleet(store)
    store.add_node('disabled-private', 'op', now=NOW)
    store.add_slot('disabled-private', 'disabled-private', 'slot01', now=NOW)
    store.set_enabled('disabled-private', False)
    store.insert_heartbeat('disabled-private', NOW, {'node_id': 'disabled-private', 'mode': 'machine'})
    calls = []
    original = status.machine_details
    def details(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(status, 'machine_details', details)
    raw = page_of(store, cfg)
    assert calls == [(store, NOW)]
    assert raw.count('class="st-machine-row"') == 3
    assert '3 of 3 operational' in shown(raw)
    assert 'disabled-private' not in raw


def test_machine_detail_projection_has_only_the_selected_language(store, cfg):
    fleet(store, ages=(20, 30, 400))
    store.open_alert('pool-7', 'disk_high', LEVEL_WARN, 'RAW_SECRET', NOW - 60)
    raw = page_of(store, cfg)
    english = project_html(raw, 'en', '/status')
    chinese = project_html(raw, 'zh', '/zh/status')
    assert 'Machine 1' in shown(english) and '主机 1' not in shown(english)
    assert 'Degraded' in shown(english)
    assert '主机 1' in shown(chinese) and 'Machine 1' not in shown(chinese)
    assert '性能或容量异常' in shown(chinese)
    assert '最近上报' in shown(chinese) and 'Last report' not in shown(chinese)
    assert 'RAW_SECRET' not in english + chinese
    assert english.count('<script') == chinese.count('<script') == 2


class MachineSheet(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tables = []
        self.headers = []
        self.rows = []
        self.regions = []
        self.row = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.tables.append(attrs)
        elif tag == 'div' and attrs.get('role') == 'region':
            self.regions.append(attrs)
        elif tag == 'tr' and attrs.get('id', '').startswith('machine-'):
            self.row = {'attrs': attrs, 'cells': []}
            self.rows.append(self.row)
        elif tag in ('th', 'td'):
            if self.row is not None:
                self.row['cells'].append((tag, attrs))
            elif tag == 'th':
                self.headers.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'tr':
            self.row = None


def test_machine_sheet_has_three_accessible_columns_and_one_row_per_machine(store, cfg):
    fleet(store)
    raw = project_html(page_of(store, cfg), 'en', '/status')
    section = re.search(r'<section\b[^>]*id="slot-machines"[^>]*>(.*?)</section>', raw, re.S).group(1)
    sheet = MachineSheet(section)
    assert len(sheet.tables) == 1
    assert sheet.tables[0]['aria-labelledby'] == 'slot-machines-title'
    assert len(sheet.headers) == 3 and all(header['scope'] == 'col' for header in sheet.headers)
    assert len(sheet.rows) == 3
    assert all([tag for tag, _attrs in row['cells']] == ['th', 'td', 'td'] for row in sheet.rows)
    assert all(row['cells'][0][1]['scope'] == 'row' for row in sheet.rows)
    assert sheet.regions[0]['tabindex'] == '0'
    headings = re.findall(r'<th scope="col">(.*?)</th>', section, re.S)
    assert [shown(heading).strip() for heading in headings] == ['Machine', 'Status', 'Last report']
    for removed in ('<article', '<h3', '<ul', 'recorded uptime', 'st-machine-card'):
        assert removed not in section


@pytest.mark.parametrize('language,reading', [('en', 'Reading this page'), ('zh', '如何理解状态')])
@pytest.mark.parametrize('resources', [None, {'state': 'disabled'}])
def test_compact_machine_sheet_is_immediately_above_reading_help_near_page_bottom(
        store, cfg, language, reading, resources):
    fleet(store)
    raw = project_html(statuspage.page(store, cfg, None, NOW, machine_report=resources),
                       language, '/zh/status' if language == 'zh' else '/status')
    performance_at = raw.index('id="performance"')
    history_at = raw.index('class="card st-list"')
    sheet_at = raw.index('id="slot-machines"')
    help_at = raw.index('class="card st-key"')
    assert performance_at < history_at < sheet_at < help_at
    if resources is not None:
        assert performance_at < raw.index('id="gateway-machine"') < history_at
    sheet_end = raw.index('</section>', sheet_at) + len('</section>')
    assert raw[sheet_end:].startswith('<div class="card st-key">')
    assert reading in shown(raw[help_at:])
    assert len(MachineSheet(raw[sheet_at:sheet_end]).rows) == 3
    for number in (1, 2, 3):
        assert f'id="machine-{number}"' in raw
    for name in NAMES:
        assert name not in raw


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_performance_copy_retains_sample_minima_without_a_multi_account_prerequisite(language):
    report = {'state': 'limited_data', 'reporting_users': 765432,
              'reporting_slots': 654321, 'eligible_slots': 543210,
              'account_name': 'PRIVATE_ACCOUNT', 'email': 'private@example.com'}
    raw = project_html(statuspage.performance_panel(report, NOW), language,
                       '/zh/status' if language == 'zh' else '/status')
    text = shown(raw)
    if language == 'en':
        assert 'At least 20 recorded attempts are needed' in text
        assert 'Each timing separately requires at least 20 samples' in text
        assert 'even if only one account contributes' in text
        assert 'from 3' not in text and '3 different' not in text
    else:
        assert '至少有 20 次已记录的尝试' in text
        assert '至少 20 个样本' in text
        assert '即使只有一个账号贡献数据' in text
        assert '3 个账号' not in text and '3 个不同' not in text
    for private in ('765432', '654321', '543210', 'PRIVATE_ACCOUNT', 'private@example.com'):
        assert private not in raw


def test_machine_sheet_scales_to_many_sorted_rows_with_stable_public_anchors():
    details = [status.MachineDetail(f'private-node-{number}', number,
                                   status.State(status.RED if number % 2 else status.GREEN),
                                   ('heartbeat_down',), NOW - number,
                                   f'private-display-{number}') for number in range(50, 0, -1)]
    details += [status.MachineDetail('secret-unknown', number, status.State(status.RED),
                                     ('RAW_PRIVATE_REASON',), None)
                for number in (None, True, 'secret-label', 0, -1)]
    raw = statuspage.machine_details_panel(details, None, NOW)
    sheet = MachineSheet(raw)
    assert [row['attrs']['id'] for row in sheet.rows] == [f'machine-{number}' for number in range(1, 51)]
    assert len(sheet.tables) == 1 and len(sheet.rows) == 50
    assert len(re.findall(r'<time\b', raw)) == 50
    for private in ('private-node', 'private-display', 'secret-', 'RAW_PRIVATE_REASON'):
        assert private not in raw
    assert 'st-machine-card' not in raw and '<article' not in raw


def test_machine_sheet_missing_report_is_unknown_in_both_languages():
    detail = status.MachineDetail('private-node', 7, status.State(status.RED),
                                  ('heartbeat_missing',), None)
    raw = statuspage.machine_details_panel([detail], None, NOW)
    assert 'Not yet received' in shown(project_html(raw, 'en', '/status'))
    chinese = shown(project_html(raw, 'zh', '/zh/status'))
    assert '尚未收到' in chinese and 'Not yet received' not in chinese
    assert 'Down' in shown(project_html(raw, 'en', '/status'))
    assert '<time' not in raw and 'private-node' not in raw


def test_empty_machine_sheet_does_not_invent_machine_rows(store, cfg):
    raw = project_html(page_of(store, cfg), 'en', '/status')
    assert 'No labeled slot machines to show yet.' in shown(raw)
    section = re.search(r'<section\b[^>]*id="slot-machines"[^>]*>(.*?)</section>', raw, re.S).group(1)
    sheet = MachineSheet(section)
    assert len(sheet.tables) == 1 and sheet.rows == []
    assert 'colspan="3"' in section and 'Machine 1' not in section


def test_the_page_carries_only_the_two_authored_scripts(store, cfg):
    fleet(store)
    raw = page_of(store, cfg)
    assert raw.count("<script") == 2
    assert render.LOCAL_TIMES_TAG in raw and LANGUAGE_TAG in raw


def test_the_page_refreshes_itself_every_minute(store, cfg):
    assert refresh_of(page_of(store, cfg)) == (60, "")


# -- the ninety days ---------------------------------------------------------------------------

def bars(raw):
    return re.findall(r'<div class="st-bar"[^>]*>(.*?)</div>', raw, re.S)


def test_each_component_has_a_bar_of_ninety_days(store, cfg):
    fleet(store)
    found = bars(page_of(store, cfg))
    assert len(found) == 2
    for bar in found:
        assert len(re.findall(r"<span class=\"d ", bar)) == status.HISTORY_DAYS


def test_a_day_says_what_it_was_on_hover_and_to_a_screen_reader(store, cfg):
    fleet(store)
    status.record(store, NOW, check_interval_s=60)
    raw = page_of(store, cfg, NOW + 60)
    today = status.utc_day(int(NOW // 60))
    assert f'title="{today} (UTC): 100% up"' in raw
    assert re.search(r'role="img" aria-label="Website and account pages: 100% uptime', raw)


def test_uptime_is_shown_for_each_component_and_overall(store, cfg):
    fleet(store)
    store.count_status(status.SITE, status.GREEN, NOW, grace=2, gap_state=status.RED)
    store.count_status(status.SITE, status.GREEN, NOW + 3 * 60, grace=0, gap_state=status.RED)
    store.count_status("node:pool-7", status.GREEN, NOW, grace=2)
    store.count_status("node:pool-8", status.YELLOW, NOW, grace=2)     # degraded is up
    store.count_status(status.GROUP, status.YELLOW, NOW, grace=2)
    raw = page_of(store, cfg, NOW + 240)
    page = shown(raw)
    assert "50.00% uptime" in page                 # the site: 2 up, 2 down
    assert "100% uptime" in page                   # the machines: 2 machine-minutes, both up
    assert "66.66% uptime over the last 90 days" in page    # 4 up of 6 counted
    assert ('aria-label="Website and account pages: 50.00% uptime over the last 90 days; '
            '1 day with trouble"') in raw
    assert 'aria-label="Slot machines: 100% uptime over the last 90 days; 1 day with trouble"' in raw


def test_each_day_says_what_it_was(store, cfg):
    today = status.utc_day(int(NOW // 60))
    yesterday = status.utc_day(int(NOW // 60) - 1440)
    store.count_status(status.SITE, status.GREEN, NOW, grace=2, gap_state=status.RED)
    store.count_status(status.SITE, status.GREEN, NOW + 3 * 60, grace=0, gap_state=status.RED)
    for i, state in enumerate([status.RED, status.RED, status.YELLOW]):
        store.count_status(status.GROUP, state, NOW + 60 * i, grace=2)
    raw = page_of(store, cfg, NOW + 240)
    assert f'title="{today} (UTC): 50.00% up, down 2 min"' in raw
    assert f'title="{today} (UTC): every machine down 2 min, some in trouble 1 min"' in raw
    assert f'title="{yesterday} (UTC): no data"' in raw
    store.count_status(status.GROUP, status.GREEN, NOW + 86400, grace=2)
    tomorrow = status.utc_day(int(NOW // 60) + 1440)
    assert f'title="{tomorrow} (UTC): all up"' in page_of(store, cfg, NOW + 86400 + 60)


# -- where it is ----------------------------------------------------------------------------

def test_the_status_page_is_public_on_the_product_and_absent_from_the_console(split):  # noqa: F811
    _, browser, _ = split
    reply = browser(PRODUCT).call("GET", "/status")
    assert reply.status == 200 and "All systems operational" in reply.body
    assert browser(ADMIN).call("GET", "/status").status == 404


def test_every_public_page_links_it_from_the_footer_and_the_front_page_from_its_body(split):  # noqa: F811
    _, browser, _ = split
    for path in ("/", "/docs/guide", "/privacy"):
        page = browser(PRODUCT).call("GET", path).body
        nav = page[page.index('<nav class="doc-nav">'):].split("</nav>", 1)[0]
        assert 'href="/status"' in nav and "Status" in nav, path
        foot = page[page.index('<footer class="sitefoot">'):]
        assert 'href="/status"' in foot, path
    front = browser(PRODUCT).call("GET", "/").body
    assert 'href="/status"' in front[:front.index('<footer class="sitefoot">')]
    for prefix in ("/en", "/zh"):
        landing = browser(PRODUCT).call("GET", prefix + "/").body
        nav = landing[landing.index('<nav class="doc-nav">'):].split("</nav>", 1)[0]
        target = "/zh/status" if prefix == "/zh" else "/status"
        assert f'href="{target}"' in nav
        assert ("服务状态" if prefix == "/zh" else "Status") in nav
        destination = browser(PRODUCT).call("GET", target)
        assert destination.status == 200
        assert f'<a href="{target}" class="here">' in destination.body


def test_signed_in_the_only_address_on_it_is_the_viewers_own_in_their_menu(site):  # noqa: F811
    """Codex, PR #111: signed in, the page wears the same bar as every public
    page, whose menu shows the viewer their own address. Nobody else's name,
    address or machine is anywhere on it."""
    store, sign_in, _ = site
    fleet(store, now=time.time())
    viewer = sign_in(sub="google-viewer", email="viewer@example.com")
    raw = viewer.call("GET", "/status").body
    menu_at = raw.index('<details class="usermenu">')
    menu = raw[menu_at:raw.index("</details>", menu_at) + len("</details>")]
    assert "viewer@example.com" in menu
    rest = raw.replace(menu, "")
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", rest)
    for name in NAMES:
        assert name not in rest, name


def test_how_it_works_says_where_to_look(split):  # noqa: F811
    _, browser, _ = split
    page = browser(PRODUCT).call("GET", "/docs/how-it-works").body
    assert 'href="/status"' in page[:page.index('<footer class="sitefoot">')]


# -- the holder's line ----------------------------------------------------------------------

def test_the_line_says_the_state_and_when_it_began():
    green = status.slot_line(status.State(status.GREEN), NOW)
    assert "Machine: operational" in shown(green) and 'href="/status"' in green
    late = status.slot_line(status.State(status.YELLOW, NOW - 300), NOW)
    assert "Machine: degraded since" in shown(late)
    assert f'<time datetime="{status.utc_iso(NOW - 300)}" data-local>5m ago</time>' in late
    down = status.slot_line(status.State(status.RED), NOW)
    assert "Machine: down" in shown(down) and "since" not in down


def claimed_card(site_, age=5, alert=None):
    store, sign_in, _ = site_
    one_slot_machine(store, "pool-1")
    erik = sign_in(quota=1)
    assert erik.press("/account/claim").status == 303
    report(store, "pool-1", [{"unix_user": "slot01", "present": True}], ts=time.time() - age)
    if alert:
        store.open_alert("pool-1", alert[0], alert[1], "x", time.time() - 30)
    return shown(whole_card(erik.page(), "pool-1"))


def test_a_slot_card_says_its_machine_is_up(site):  # noqa: F811
    assert "Machine: operational" in claimed_card(site)


def test_the_dots_are_styled_wherever_the_line_or_the_page_is(store, cfg):
    """The state's colour is a styled dot: both pages carry its rules."""
    assert ".st-dot.green{" in page_of(store, cfg)
    assert ".st-dot.green{" in usersite._shell("x", "")


def test_a_slot_card_says_its_machine_is_down(site):  # noqa: F811
    assert "Machine: down since" in claimed_card(site, age=400)


def test_a_slot_card_says_its_machine_is_degraded(site):  # noqa: F811
    assert "Machine: degraded since" in claimed_card(site, alert=("disk_high", LEVEL_WARN))


def test_somebodys_own_node_is_judged_by_its_own_five_minute_beat(site):  # noqa: F811
    """An owner's node reports every five minutes: six minutes quiet is normal
    for it, where a shared machine would be down."""
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    store.add_node("erik-1", "erik", now=time.time())
    store.hold_owner_node("erik-1", erik.account["id"], unix_user="erik", now=time.time())
    store.insert_heartbeat("erik-1", time.time() - 400, {"node_id": "erik-1"})
    assert "Machine: operational" in shown(whole_card(erik.page(), "erik-1"))


# -- counted once a minute by the server that serves it ------------------------------------------

class OneRound:
    """A stop event that lets run_forever go round once."""

    def __init__(self):
        self.rounds = 0

    def is_set(self):
        self.rounds += 1
        return self.rounds > 1

    def wait(self, _seconds):
        return None


def test_the_serving_loop_counts_the_minute(store, cfg):
    fleet(store, now=time.time())
    Monitor(store, cfg, LogNotifier()).run_forever(OneRound())
    components = {r["component"] for r in store.status_minutes(since_day="2000-01-01")}
    assert components == {status.SITE, status.GROUP, "node:pool-7", "node:pool-8",
                          "node:erik-9"}


def test_a_check_that_fails_still_counts_the_minute(store, cfg, monkeypatch):
    """The server is up whether or not one check went wrong."""
    monitor = Monitor(store, cfg, LogNotifier())
    monkeypatch.setattr(monitor, "check_all", lambda *a, **k: 1 / 0)
    monitor.run_forever(OneRound())
    assert [r["component"] for r in store.status_minutes(since_day="2000-01-01")] == [
        status.SITE]


def test_a_check_run_by_hand_counts_nothing(store, cfg):
    """`ccfleetd check` runs check_all beside a server that may be down: only
    the serving loop's minute says the site is up."""
    fleet(store, now=time.time())
    Monitor(store, cfg, LogNotifier()).check_all()
    assert store.status_minutes(since_day="2000-01-01") == []
    assert store.listening_since() is None, "and it is not listening for reports either"


def test_the_grace_follows_the_configured_check_interval(store):
    cfg = Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_DB": ":memory:",
                           "CCFLEET_CHECK_INTERVAL_S": "300"})
    monitor = Monitor(store, cfg, LogNotifier(), clock=lambda: NOW)
    monitor.record_status(NOW)
    monitor.record_status(NOW + 300)                      # four minutes unseen: normal here
    [row] = store.status_minutes(since_day="2000-01-01")
    assert (row["green"], row["red"]) == (6, 0)


def test_status_is_recorded_under_the_monitor_lock(store, cfg):
    """One writer at a time with the checks, which read the same heartbeats."""
    monitor = Monitor(store, cfg, LogNotifier())
    monitor._lock.acquire()
    worker = threading.Thread(target=monitor.record_status, args=(NOW,))
    worker.start()
    worker.join(timeout=0.3)
    assert worker.is_alive(), "recorded without the lock"
    monitor._lock.release()
    worker.join(timeout=5)
    assert not worker.is_alive()


# -- the front page's pill (Erik, 2026-09-25) ---------------------------------------------------

def pill_on(page, language="en"):
    """The front page's status pill: the link that says the banner's words."""
    path = '/zh/status' if language == 'zh' else '/status'
    found = re.search(r'<a class="pill st-pill[^"]*" href="' + re.escape(path)
                      + r'"[^>]*>.*?</a>', page, re.S)
    assert found, "no status pill"
    return found.group(0)


@pytest.mark.parametrize("ages, alert, level, cls", [
    ((20, 30, 40), None, status.GREEN, "ok"),
    ((20, 30, 400), None, status.YELLOW, "warn"),
    ((20, 30, 40), ("disk_high", LEVEL_WARN), status.YELLOW, "warn"),
    ((2000, 2000, 2000), None, status.RED, "critical"),
])
def test_the_pill_says_what_the_banner_says_in_its_colour(store, cfg, ages, alert, level, cls):
    fleet(store, ages=ages)
    if alert:
        store.open_alert("pool-8", alert[0], alert[1], "x", NOW - 120)
    banner = statuspage.BANNERS[level]
    assert ("<strong>" + label(banner, statuspage.BANNERS_ZH[level], inline=True)
            + "</strong>") in page_of(store, cfg)
    assert statuspage.health(store, NOW) == level
    pill = statuspage.pill(level)
    assert Page(pill).copy('en').strip() == banner
    assert Page(pill).copy('zh').strip() == statuspage.BANNERS_ZH[level]
    assert f'class="pill st-pill {cls}"' in pill
    assert f'aria-label="Status: {banner}"' in project_html(pill, 'en')
    assert f'aria-label="服务状态：{statuspage.BANNERS_ZH[level]}"' in project_html(pill, 'zh')


def test_with_no_machine_counted_the_pill_is_a_plain_link_never_a_green(store):
    """The status page says all is well with nothing to count. The front page
    saying it too would vouch for machines nobody has measured."""
    assert statuspage.health(store, NOW) is None
    pill = statuspage.pill(None)
    assert Page(pill).copy('en').strip() == "Status"
    assert Page(pill).copy('zh').strip() == "服务状态"
    assert pill.startswith('<a class="pill st-pill"')
    assert "ok" not in pill and "operational" not in pill


def test_the_front_page_says_it_at_its_top_and_no_other_page_does(split):  # noqa: F811
    store, browser, _ = split
    fleet(store, now=time.time())
    for path in ("/", "/docs"):
        page = browser(PRODUCT).call("GET", path).body
        main = page[page.index("<main"):]
        assert main.index('class="pill st-pill') < main.index("<h1>"), path
        assert Page(pill_on(page)).copy('en').strip() == "All systems operational", path
        assert Page(pill_on(page)).copy('zh').strip() == '', path
        chinese = browser(PRODUCT).call("GET", "/zh" + (path if path != '/' else '/')).body
        assert Page(pill_on(chinese, 'zh')).copy('zh').strip() == statuspage.BANNERS_ZH[status.GREEN], path
        assert Page(pill_on(chinese, 'zh')).copy('en').strip() == '', path
        assert page.count("<script") == 2, "only the two shared authored scripts"
        assert render.LOCAL_TIMES_TAG in page and LANGUAGE_TAG in page
        assert ".st-pill{" in page and ".pill.ok{" in page, "styled, from the theme's tokens"
    assert 'class="pill st-pill' not in browser(PRODUCT).call("GET", "/docs/guide").body


def test_the_front_page_says_trouble_and_names_nobody(split):  # noqa: F811
    store, browser, _ = split
    fleet(store, now=time.time(), ages=(20, 30, 400))
    page = browser(PRODUCT).call("GET", "/").body
    assert Page(pill_on(page)).copy('en').strip() == "Partial outage"
    assert Page(pill_on(page)).copy('zh').strip() == ''
    chinese = browser(PRODUCT).call("GET", "/zh/").body
    assert Page(pill_on(chinese, 'zh')).copy('zh').strip() == statuspage.BANNERS_ZH[status.YELLOW]
    for name in NAMES:
        assert name not in page, name


def test_a_front_page_with_no_machine_yet_says_only_status(split):  # noqa: F811
    _, browser, _ = split
    pill = pill_on(browser(PRODUCT).call("GET", "/").body)
    assert Page(pill).copy('en').strip() == "Status"
    assert Page(pill).copy('zh').strip() == ""
    chinese = pill_on(browser(PRODUCT).call("GET", "/zh/").body, "zh")
    assert Page(chinese).copy('zh').strip() == "服务状态"


# -- the server's own silence is not the machines' ---------------------------------------------

def test_the_serving_loop_says_when_it_began_listening(store, cfg):
    Monitor(store, cfg, LogNotifier(), clock=lambda: NOW).run_forever(OneRound())
    assert store.listening_since() == NOW


def test_a_restart_after_a_long_outage_finds_every_machine_up(store, cfg):
    """Ten minutes down, every machine's last report is ten minutes old: none of
    them is late for it, the server is."""
    fleet(store, ages=(600, 600, 600))
    store.set_listening_since(NOW - 5)
    assert "All systems operational" in shown(page_of(store, cfg))


def test_a_machine_down_before_the_restart_is_still_down_after_it(store, cfg):
    fleet(store, ages=(20, 600, 30))
    store.begin_outage("pool-7", NOW - 300)
    store.set_listening_since(NOW - 5)
    assert "Partial outage" in shown(page_of(store, cfg))


def test_a_slot_card_after_a_restart_does_not_blame_the_machine(site):  # noqa: F811
    site[0].set_listening_since(time.time() - 5)
    assert "Machine: operational" in claimed_card(site, age=400)


def test_listening_since_is_kept_and_a_bad_value_is_none(store):
    store.set_listening_since(NOW - 3600)
    store.set_listening_since(NOW)                   # every start says it again
    assert store.listening_since() == NOW
    store._conn.execute("UPDATE settings SET value = 'soon' WHERE key = 'listening_since'")
    assert store.listening_since() is None
