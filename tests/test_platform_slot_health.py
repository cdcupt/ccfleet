"""Infrastructure diagnosis uses existing reports, not a synthetic model success."""
from copy import deepcopy

import pytest

from ccfleetd import consoleslots, status, statuspage, usersite
from ccfleetd.bilingual import project_html
from tests.test_machine_status import ready_report
from tests.test_status_page import fleet, shown

NOW = 2_000_000_000.0


def report(*, age=10, disk=25, memory=35):
    return {'ts': NOW - age, 'payload': {'disk': {'used_pct': disk},
            'mem': {'used_pct': memory}, 'quota': {'session': {'used_pct': 100}},
            'credentials': {'logged_in': False}, 'egress': {'ip': '192.0.2.33'}}}


def panel(store, platform=None, language='en'):
    return project_html(statuspage.diagnosis_panel(store, platform, NOW), language, '/status')


def test_complete_fresh_platform_checks_are_green_but_never_a_model_test():
    check = status.platform_check(ready_report(), NOW)
    assert check.level == status.GREEN
    text = shown(project_html(status.health_html(check, NOW), 'en', '/status'))
    assert 'CPU 12.3%' in text and 'Storage 25.0%' in text
    assert 'Model requests are not tested' in text


@pytest.mark.parametrize('raw', [None, {}, {'state': 'ready'}, {'state': 'unavailable'}])
def test_missing_platform_is_unknown(raw):
    assert status.platform_check(raw, NOW).level == status.UNKNOWN


@pytest.mark.parametrize('age', [61, 600, -6])
def test_stale_or_future_platform_sample_never_shows_current_numbers(age):
    raw = ready_report()
    raw['observed_at'] = NOW - age
    check = status.platform_check(raw, NOW)
    assert check.level == status.UNKNOWN and check.metrics == ()
    assert '12.3%' not in status.health_html(check, NOW)


@pytest.mark.parametrize('group,key,expected', [
    ('host', 'cpu_pct', 'cpu_pressure'), ('host', 'memory_pct', 'memory_pressure'),
    ('host', 'io_wait_pct', 'io_wait_pressure'), ('host', 'steal_pct', 'steal_pressure'),
    ('service', 'cpu_budget_pct', 'service_cpu_pressure'),
    ('service', 'memory_pct', 'service_memory_pressure'),
])
def test_platform_pressure_identifies_resource_without_proving_outage(group, key, expected):
    raw = ready_report()
    raw[group][key] = 94
    check = status.platform_check(raw, NOW)
    assert check.level == status.YELLOW and expected in check.reasons


def test_critical_platform_storage_is_visible_even_if_other_metrics_missing():
    raw = ready_report()
    raw['storage']['used_pct'] = 97
    raw['service'] = {}
    check = status.platform_check(raw, NOW)
    assert check.level == status.RED and 'disk_critical' in check.reasons


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), 10**400, '<img src=x>'])
def test_bad_or_injected_measurement_stays_unknown_and_private(value):
    raw = ready_report()
    raw['host']['memory_pct'] = value
    check = status.platform_check(raw, NOW)
    assert check.level == status.UNKNOWN
    assert all(name != 'memory' for name, _ in check.metrics)
    assert '<img' not in status.health_html(check, NOW)


def test_heartbeat_alone_is_not_green_and_fresh_resource_checks_ignore_account_limits():
    assert status.machine_check({'ts': NOW, 'payload': {}}, [], True, NOW).level == status.UNKNOWN
    alerts = [{'rule': code, 'level': 'critical'} for code in status.EXCLUDED_RULES]
    check = status.machine_check(report(), alerts, True, NOW)
    assert check.level == status.GREEN
    html = status.health_html(check, NOW)
    assert 'quota' not in html and '192.0.2.33' not in html and 'logged_in' not in html


def test_machine_stopped_reporting_does_not_keep_old_resource_numbers_green():
    check = status.machine_check(report(age=400, disk=12.3), [], True, NOW)
    assert check.level == status.RED and check.metrics == ()
    assert check.reasons == ('heartbeat_down',)
    assert 'provider outage' in shown(status.health_html(check, NOW))
    assert '12.3%' not in status.health_html(check, NOW)


def test_machine_missing_or_future_report_is_unknown_not_a_confirmed_hardware_failure():
    for raw in (None, report(age=-60), {'ts': True}, {'ts': float('inf')}):
        assert status.machine_check(raw, [], True, NOW).level == status.UNKNOWN


def test_machine_report_deadlines_follow_actual_agent_schedule():
    assert status.machine_check(report(age=200), [], True, NOW).level == status.YELLOW
    assert status.machine_check(report(age=200), [], False, NOW).level == status.GREEN


def test_public_panel_lists_only_occupied_machines_with_neutral_labels(store):
    fleet(store, now=NOW)
    nodes = {node['id']: node for node in store.list_nodes()}
    html = panel(store, ready_report())
    assert f'Machine {nodes["pool-7"]["public_status_number"]}' in shown(html)
    assert f'Machine {nodes["erik-9"]["public_status_number"]}' in shown(html)
    assert f'Machine {nodes["pool-8"]["public_status_number"]}' not in shown(html)
    for private in ('ana@example.com', 'erik@example.com', 'pool-7', 'pool-8', 'erik-9',
                    'slot01', '192.0.2.33', 'quota_read', 'native_timeout'):
        assert private not in html
    assert 'Not enough data' in shown(html)
    assert 'not automatically tested' in shown(html)


def test_occupied_machine_with_no_heartbeat_is_not_silently_omitted(store):
    fleet(store, now=NOW)
    store._conn.execute('DELETE FROM heartbeats WHERE node_id=?', ('pool-7',))
    html = panel(store, ready_report())
    number = store.get_node('pool-7')['public_status_number']
    assert f'Machine {number}' in shown(html)
    assert 'No machine report is available' in shown(html)


def test_platform_and_slot_evidence_are_correlated_without_inventing_vendor_status(store):
    fleet(store, now=NOW, ages=(20, 400, 20))
    text = shown(panel(store, ready_report()))
    assert 'Platform resources look normal; check the flagged slot machines next' in text
    assert 'does not rule out network or model-provider errors' in text
    raw = ready_report()
    raw['host']['cpu_pct'] = 94
    text = shown(panel(store, raw))
    assert 'Start with the platform' in text and 'not proof of a DMIT outage' in text


def test_english_and_chinese_health_copy_are_independent(store):
    fleet(store, now=NOW)
    en, zh = panel(store, ready_report()), panel(store, ready_report(), 'zh')
    assert 'Where is the problem?' in en and '哪里出了问题' not in en
    assert '哪里出了问题' in zh and 'Where is the problem?' not in zh
    assert '主机' in zh and '模型调用成功' in zh


def test_account_and_admin_show_owned_resource_diagnosis_in_partial_readonly_region(store, cfg):
    fleet(store, now=NOW)
    store.insert_heartbeat('pool-7', NOW, report(disk=97)['payload'])
    account = store.get_account('a1')
    raw = usersite.page(store, cfg, account, 'synthetic-session', NOW)
    assert 'machine-health-' in raw and 'data-fleet-readonly' in raw
    assert 'Storage is critically full' in shown(raw)
    accounts = {account['id']: account for account in store.list_accounts()}
    admin = consoleslots._slots_card(store, accounts, 'synthetic-csrf', NOW, cfg=cfg)
    assert 'admin-machine-health-' in admin and 'Storage is critically full' in shown(admin)


def test_diagnosis_does_not_mutate_or_render_arbitrary_payload_fields():
    raw = report()
    raw['payload']['private_error'] = '<script>private-token</script>'
    before = deepcopy(raw)
    html = status.health_html(status.machine_check(raw, [], True, NOW), NOW)
    assert raw == before and 'private-token' not in html and '<script>' not in html
