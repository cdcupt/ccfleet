"""Retired records leave the normal console without hiding live incidents."""
import json
import threading
from contextlib import closing

import pytest

from ccfleetd import offers, slots
from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.render import build_rows, render_dashboard
from ccfleetd.store import Store

from .test_api import basic, call
from .test_html_document_preservation import Document

NOW = 2_000_000


def seed(store):
    for node in ('retired-empty', 'retired-vm', 'offline-live'):
        store.add_node(node, 'operator', now=NOW - 1000)
        store.insert_heartbeat(node, NOW - 5000,
                               {'node_id': node, 'mode': slots.MACHINE_MODE})
    for node in ('retired-vm', 'offline-live'):
        store.set_machine_capacity(node, 1)
        store.add_slot(node, node, 'slot01', now=NOW - 1000)
        store.apply_slot_report(node, [{'unix_user': 'slot01', 'present': False}], now=NOW)
    for node in ('retired-empty', 'retired-vm'):
        store.set_enabled(node, False)
        store.open_alert(node, 'retired-note', 'warn', 'obsolete machine issue', NOW)
    store.open_alert('offline-live', 'live-note', 'critical', 'active machine needs attention', NOW)
    store.set_node_offer('offline-live', host_brand='Working provider', ip_brand='Assigned ISP',
                         ip_type='isp', hosting=offers.Cost('5.35', 'USD', 'month'),
                         ip=offers.Cost('5.40', 'USD', '30_days'))


def test_console_hides_retired_rows_controls_alerts_and_flow_cadence(cfg):
    with closing(Store(':memory:')) as store:
        seed(store)
        before = store.list_nodes()
        rows = build_rows(before, store.latest_heartbeats(), store.open_alerts(), NOW,
                          slots=store.list_slots())
        page = render_dashboard(rows, store.open_alerts(), NOW, cfg, csrf='synthetic-csrf',
                                logins={'retired-vm': {'state': 'url_ready'}})
        assert 'retired-empty' not in page and 'retired-vm' not in page
        assert 'obsolete machine issue' not in page
        assert 'offline-live' in page and 'active machine needs attention' in page
        document = Document(page)
        assert 'Status updates keep your open forms' in document.text
        assert any(tag == 'a' and attrs.get('href') == '/admin'
                   and attrs.get('data-fleet-status') == 'admin'
                   for tag, attrs in document.tags)
        assert not any(attrs.get('http-equiv', '').lower() == 'refresh'
                       for attrs in document.meta)
        assert store.list_nodes() == before
        assert store.get_slot('retired-vm')['state'] == slots.FREE


@pytest.mark.parametrize('state', [slots.CLAIMING, slots.CLAIMED, slots.ACTIVE,
                                  slots.RELEASING, 'unknown'])
def test_disabled_unfinished_slots_remain_visible_for_recovery(cfg, state):
    with closing(Store(':memory:')) as store:
        seed(store)
        rows = build_rows(store.list_nodes(), store.latest_heartbeats(), [], NOW,
                          slots=store.list_slots())
        row = next(r for r in rows if r['id'] == 'retired-vm')
        row['slot_state'] = state
        page = render_dashboard([row], [], NOW, cfg, csrf='synthetic-csrf')
        assert 'retired-vm' in page


def test_disabled_owner_node_remains_visible(cfg):
    with closing(Store(':memory:')) as store:
        store.add_node('personal-owner', 'operator', now=NOW)
        store.set_enabled('personal-owner', False)
        rows = build_rows(store.list_nodes(), {}, [], NOW)
        assert 'personal-owner' in render_dashboard(rows, [], NOW, cfg)


def test_authenticated_console_filters_display_but_inventory_api_retains_records(cfg):
    store = Store(':memory:')
    seed(store)
    server = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                          host='127.0.0.1', port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        status, raw, _ = call(server, 'GET', '/admin', headers=basic(cfg.admin_token))
        assert status == 200
        page = raw.decode()
        assert 'retired-empty' not in page and 'retired-vm' not in page
        assert 'offline-live' in page and 'Alpha slot pricing' in page
        status, raw, _ = call(server, 'GET', '/api/nodes', headers=basic(cfg.admin_token))
        assert status == 200
        assert {'retired-empty', 'retired-vm', 'offline-live'} <= {
            row['id'] for row in json.loads(raw)['nodes']}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        store.close()
