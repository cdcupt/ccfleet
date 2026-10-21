"""Private traffic routes preserve owner isolation and unknown measurements."""
import base64
import re
import threading
from copy import deepcopy
from html.parser import HTMLParser

import pytest

from ccfleet_agent.network_traffic import empty
from ccfleetd import api, bilingual, traffic, traffic_ui
from ccfleetd.config import Config
from ccfleetd.store import Store
from tests.test_html_document_preservation import Document
from tests.test_panel_actions import run_js
from tests.test_usersite import Browser, claimed, machine, serving


@pytest.fixture
def site(monkeypatch):
    yield from serving(monkeypatch)


def measured():
    network = {**empty("machine_nic"), "state": "ready", "observed_at": 1790042040,
               "measurement_started_at": 1790042010, "measurement_through_at": 1790042040,
               "coverage_s": 30, "received_bytes": 1200000, "sent_bytes": 42000,
               "receive_bytes_per_s": 40000.0, "send_bytes_per_s": 1400.0,
               "gaps": 1, "resets": 2}
    return {"network": network, "network_attribution": "shared_machine",
            "api": {"state": "ready", "window_days": 7, "requests": 19, "success": 17,
                    "errors": 2, "usage_samples": 15, "usage_eligible_messages": 17,
                    "cache_read_samples": 0, "cache_creation_samples": 15,
                    "tokens": {"input_tokens": 12000, "output_tokens": 24000,
                               "cache_read_input_tokens": None, "cache_creation_input_tokens": 0},
                    "token_state": "partial"}}


class ReadonlyRegions(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.stack, self.ids = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        readonly = "data-fleet-readonly" in fields
        if readonly:
            assert not any(active for _, active in self.stack), "nested refresh region"
            assert fields.get("id") and fields["id"] not in self.ids
            self.ids.append(fields["id"])
        if tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.stack.append((tag, readonly))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


@pytest.mark.parametrize("owner", [False, True])
def test_status_refresh_updates_real_traffic_regions_without_replacing_disclosures_or_forms(owner):
    if owner:
        source = traffic_ui.slot_panel(measured(), slot_id="canonical-slot-id")
        expected = ["slot-traffic-canonical-slot-id"]
    else:
        source = traffic_ui.controller_panel({}, [
            ("canonical-slot-id", "First displayed name", measured()),
            ("second-slot-id", "Another slot", measured())])
        expected = ["traffic-controller-host", "traffic-controller-service", "traffic-fleet-api",
                    "slot-traffic-canonical-slot-id", "slot-traffic-second-slot-id"]
        renamed = traffic_ui.controller_panel({}, [
            ("second-slot-id", "Renamed", measured()),
            ("canonical-slot-id", "Also renamed", measured())])
        assert set(ReadonlyRegions(renamed).ids) == set(expected)
    regions = ReadonlyRegions(source)
    assert regions.ids == expected
    result = run_js("status-traffic", traffic_ids=regions.ids)
    assert [call["method"] for call in result["calls"]] == ["GET"]
    assert result["updates"] == ["readonly", *expected]
    assert result["traffic"] == [{"id": identifier,
        "content": ["fresh " + identifier + " 1.2 MB"]} for identifier in expected]
    assert result["details"]["open"] is True and result["scrollY"] == 777
    assert result["input"]["value"] == "KEEP PRIVATE INPUT"


@pytest.mark.parametrize("language", ["en", "zh"])
def test_measured_private_traffic_labels_period_scope_and_unknown_cache(language):
    value = measured()
    before = deepcopy(value)
    source = bilingual.project_html(traffic_ui.slot_panel(value), language, "/account")
    text = Document(source).text
    for phrase in (("Shared machine NIC traffic", "not traffic attributable to your slot",
                    "Measured period", "Valid interval coverage: 30 seconds", "Gaps: 1",
                    "Counter resets: 2", "Unknown", "15 / 17", "Partial recorded fields")
                   if language == "en" else ("共享机器网卡流量", "不能归属于你的槽位", "观测时段",
                    "有效区间覆盖：30 秒", "缺口：1", "计数重置：2", "未知", "15 / 17", "已记录字段不完整")):
        assert phrase in text
    assert "1.2 MB" in text and "40 KB/s" in text and "UTC" in text
    assert "0 / 15" in text and "15 / 15" in text
    assert "Cache read tokens</span></dt><dd>0" not in source
    assert value == before


@pytest.mark.parametrize("state", ["stale", "warming", "unavailable", []])
def test_stale_unavailable_or_baseline_network_never_looks_like_zero_or_current_transfer(state):
    row = measured()["network"]
    row["state"] = state
    source = bilingual.project_html(traffic_ui.network_panel(row, title="Network"), "en", "/account")
    text = Document(source).text
    assert "1.2 MB" not in text and "40 KB/s" not in text
    assert "0 B" not in text and '<dl class="traffic-grid">' not in source
    assert ("stale" if state == "stale" else "baseline" if state == "warming" else "unavailable") in text


def test_no_api_observation_never_fabricates_zero_and_host_container_are_separate():
    source = bilingual.project_html(traffic_ui.controller_panel({}), "en", "/admin")
    text = Document(source).text
    assert "Controller host (DMIT)" in text and "CC Fleet service container" in text
    assert "not DMIT-only API totals" in text and "do not add them together" in text
    assert "No fresh API observation" in text and "Current usage is unknown" in text
    assert '<dl class="traffic-grid">' not in source


def test_customer_route_reads_only_held_slots_and_never_exposes_controller_traffic(site, monkeypatch):
    store, sign_in, _ = site
    machine(store)
    alice = sign_in("google-alice", "alice@example.test", quota=1)
    bob = sign_in("google-bob", "bob@example.test", quota=1)
    first, second = claimed(store, alice), claimed(store, bob)
    calls = []
    def observation(source, cfg, slot, now):
        calls.append(slot["id"])
        result = measured()
        result["api"]["tokens"]["input_tokens"] = (111111 if slot["id"] == first["id"] else 222222)
        return result
    monkeypatch.setattr(traffic, "slot_summary", observation)
    def forbidden(*args, **kwargs):
        raise AssertionError("customer page attempted controller traffic")
    monkeypatch.setattr(traffic, "controller_summary", forbidden)
    for browser, own, visible, hidden in ((alice, first, "111,111", "222,222"),
                                         (bob, second, "222,222", "111,111")):
        calls.clear()
        page = browser.page()
        assert calls == [own["id"]]
        assert visible in page and hidden not in page
        assert f'id="slot-traffic-{own["id"]}" data-fleet-readonly' in page
        assert 'slot-traffic' in page
        assert 'id="private-traffic"' not in page and "Controller host (DMIT)" not in page
    calls.clear()
    guest = Browser(alice.port)
    for path in ("/account", "/", "/status", "/docs/guide", "/docs/how-it-works"):
        page = guest.call("GET", path).body
        assert 'class="slot-disclosure slot-traffic"' not in page
        assert 'id="private-traffic"' not in page
    assert calls == []


def test_admin_requires_authorization_then_sees_each_slot_and_cached_network(site, monkeypatch):
    store, sign_in, _ = site
    machine(store)
    alice = sign_in(quota=1)
    claimed(store, alice)
    machine(store, node="retired", users=("slot01",))
    store.set_enabled("retired", False)
    calls = []
    seen = []
    def controller(source, cfg, now, *, network_report=None):
        calls.append("controller")
        assert network_report is not None
        return {"host": measured()["network"], "service": empty("service_namespace"),
                "api": measured()["api"], "reporting_slots": 1, "eligible_slots": 1}
    monkeypatch.setattr(traffic, "controller_summary", controller)
    def slot_reading(source, cfg, slot, now, *, include_unheld=False):
        seen.append((slot["node_id"], slot["state"], include_unheld))
        return measured()
    monkeypatch.setattr(traffic, "slot_summary", slot_reading)
    guest = Browser(alice.port)
    denied = guest.call("GET", "/admin")
    assert denied.status in (303, 401) and calls == []
    header = {"Authorization": "Basic " + base64.b64encode(b"admin:admin-token").decode()}
    reply = guest.call("GET", "/zh/admin", headers=header)
    assert reply.status == 200 and calls == ["controller"]
    assert {state for _, state, _ in seen} == {"claimed", "free"}
    assert all(node == "m1" and unheld for node, _, unheld in seen)
    for slot in store.list_slots(node_id="m1"):
        assert f'id="slot-traffic-{slot["id"]}" data-fleet-readonly' in reply.body
    assert 'id="private-traffic"' in reply.body and "控制器宿主机（DMIT）" in reply.body
    assert re.search(r'href="[^"]*#private-traffic"', reply.body)
    assert "网络与 API 流量" in reply.body
    assert reply.body.index('id="private-traffic"') < reply.body.index('id="admin-alerts"')
    assert "不是仅经 DMIT 的 API 总量" in reply.body and "槽位机器网络" in reply.body


@pytest.mark.parametrize("broker", [False, True])
def test_only_primary_starts_cached_sampler_and_shutdown_joins_it(monkeypatch, broker):
    entered, exited = threading.Event(), threading.Event()
    class Observer:
        def run(self, stop):
            entered.set()
            stop.wait(2)
            exited.set()
    class Monitor:
        def run_forever(self, stop):
            stop.wait(2)
    class Server:
        def serve_forever(self):
            if not broker:
                assert entered.wait(1)
        def server_close(self):
            pass
    monkeypatch.setattr(traffic, "Observer", Observer)
    monkeypatch.setattr(api, "build_server", lambda ctx: Server())
    monkeypatch.setattr(api.signal, "signal", lambda *args: None)
    store = Store(":memory:")
    try:
        ctx = api.Context(store, Config(broker_only=broker), Monitor())
        assert (ctx.traffic_observer is None) is broker
        assert not entered.is_set()
        api.serve(ctx)
        assert entered.is_set() is not broker
        assert exited.is_set() is not broker
    finally:
        store.close()
