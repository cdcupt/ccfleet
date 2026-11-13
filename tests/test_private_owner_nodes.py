"""A private Mac is an owner's computer, never a rentable machine."""
from __future__ import annotations

import base64
import http.client
import json
import threading
import time
import urllib.parse
from dataclasses import asdict

import pytest

from ccfleetd import (
    api,
    gateway_ui,
    heartbeat,
    offer_ui,
    performance,
    private_owner,
    sessions,
    status,
    statuspage,
    usage,
    usersite,
)
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store, StoreError
from tests.test_gateway_access import add_held_slot, public_key
from tests.test_gateway_throughput import forward, framed, stream
from tests.test_gateway_throughput import scenario as scenario
from tests.test_public_usage import reading

NOW = 2_000_000_000.0
FP = "a" * 16


def report():
    credentials = {"present": True, "logged_in": True, "store": "keychain",
                   "account_fp": FP, "bound_fp": FP, "expires_at": (NOW + 3600) * 1000}
    return {"node_id": "mac-private", "agent_ts": NOW, "mode": "private_owner",
            "claude": {"version": "3.0.0"}, "credentials": credentials,
            "slots": [{"unix_user": "nativeuser", "present": True,
                       "credentials": credentials}]}


@pytest.fixture
def private(store):
    store.add_account("operator", "sub-operator", "operator@example.test", role="admin",
                      slot_quota=4, now=NOW - 100)
    store.add_account("other-admin", "sub-other-admin", "other-admin@example.test", role="admin",
                      slot_quota=4, now=NOW - 100)
    store.add_account("customer", "sub-customer", "customer@example.test",
                      slot_quota=4, now=NOW - 100)
    made = store.register_private_owner_node("mac-private", "operator", "nativeuser",
        monthly_cost_usd_cents=17000, region="private-residential", now=NOW - 20)
    store.set_node_access("mac-private", "127.0.0.1", 2224, public_key(1))
    return store, made


def enable(store):
    store.set_private_owner_inference("mac-private", "operator", True, now=NOW)
    store.insert_heartbeat("mac-private", NOW, report())


def device(store):
    pairing = store.request_cli_pairing("mac-private", "operator", now=NOW)
    return store.register_cli_device(pairing, public_key(2), "Own laptop", now=NOW + 1)


def endpoint(store):
    made = store.create_gateway_key("operator", "Own Mac", ["mac-private"], now=NOW)
    return store.resolve_gateway_key(made["key_token"], now=NOW + 1)


def test_private_registration_is_atomic_nonrentable_and_cost_is_not_a_quote(private):
    store, made = private
    slot, node = store.get_slot("mac-private"), store.get_node("mac-private")
    assert slot["kind"] == "owner" and slot["held_by"] == "operator"
    assert slot["gateway_claim_id"] and slot["inference_enabled"] is False
    assert node["public_status_number"] is None and node["offer"] is None
    assert node["private_owner"]["monthly_cost_usd_cents"] == 17000
    assert store.node_for_token(made["node_token"])["id"] == "mac-private"
    for table in ("wallet_entries", "slot_purchases", "slot_subscriptions"):
        assert store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    for account in ("customer", "missing"):
        with pytest.raises(StoreError):
            store.register_private_owner_node("rejected", account, "nativeuser",
                monthly_cost_usd_cents=17000, now=NOW)
        assert store.get_node("rejected") is None and store.get_slot("rejected") is None
    with pytest.raises(StoreError):
        store.register_private_owner_node("second-private", "operator", "nativeuser",
            monthly_cost_usd_cents=17000, now=NOW)


def test_private_node_stays_private_after_store_reopen(tmp_path):
    path = tmp_path / "fleet.db"
    store = Store(path)
    store.add_account("operator", "sub", "operator@example.test", role="admin", now=NOW)
    store.register_private_owner_node("mac-private", "operator", "nativeuser",
        monthly_cost_usd_cents=17000, now=NOW)
    store.close()
    store = Store(path)
    try:
        node = store.get_node("mac-private")
        assert node["public_status_number"] is None
        assert status.public_machine_number(node) is None
        # Even a legacy Store assigning a number cannot override private visibility.
        store._conn.execute("UPDATE nodes SET public_status_number=99 WHERE id='mac-private'")
        store._conn.commit()
        assert status.public_machine_number(store.get_node("mac-private")) is None
        assert offer_ui._public_nodes(store) == []
    finally:
        store.close()


def test_only_bound_administrator_can_pair_and_select_private_owner(private):
    store, _ = private
    with pytest.raises(StoreError):
        store.request_cli_pairing("mac-private", "operator", now=NOW)
    enable(store)
    for account in ("other-admin", "customer"):
        with pytest.raises(StoreError):
            store.request_cli_pairing("mac-private", account, now=NOW)
        with pytest.raises(StoreError):
            store.create_gateway_key(account, "not mine", ["mac-private"], now=NOW)
    paired = device(store)
    key = endpoint(store)
    [target] = store.gateway_targets(key, now=NOW + 1)
    assert target["slot_id"] == "mac-private" and target["private_owner"] is True
    assert store.resolve_cli_device(paired["device_token"], now=NOW + 1)
    assert store.cli_device_is_active(paired["device_id"])
    assert store.gateway_request_is_active(key["id"], key["generation"], "mac-private",
                                           target["claim_generation"], now=NOW + 1)


@pytest.mark.parametrize("change", ["disable", "demote", "raw-role-change", "raw-owner-change"])
def test_revocation_is_enforced_for_existing_connections_and_keys(private, change):
    store, _ = private
    enable(store)
    paired, key = device(store), endpoint(store)
    [target] = store.gateway_targets(key, now=NOW + 1)
    if change == "disable":
        store.set_private_owner_inference("mac-private", "operator", False, now=NOW + 2)
    elif change == "demote":
        store.set_account_role("operator", "user")
    elif change == "raw-role-change":
        store._conn.execute("UPDATE accounts SET role='user' WHERE id='operator'")
        store._conn.commit()
    else:
        store._conn.execute("UPDATE slots SET held_by='other-admin' WHERE id='mac-private'")
        store._conn.commit()
    assert store.resolve_cli_device(paired["device_token"], now=NOW + 3) is None
    assert not store.cli_device_is_active(paired["device_id"])
    assert store.gateway_targets(key, now=NOW + 3) == []
    assert not store.gateway_request_is_active(key["id"], key["generation"], "mac-private",
                                               target["claim_generation"], now=NOW + 3)
    grants = store.private_owner_access("mac-private", public_key(3))
    assert grants["enabled"] is False
    assert grants["device_public_keys"] == grants["gateway_public_keys"] == []


def test_reenable_requires_fresh_grants_and_does_not_resurrect_devices(private):
    store, _ = private
    enable(store)
    paired, key = device(store), endpoint(store)
    claim = store.get_slot("mac-private")["gateway_claim_id"]
    store.set_private_owner_inference("mac-private", "operator", False, now=NOW + 2)
    store.set_private_owner_inference("mac-private", "operator", True, now=NOW + 3)
    assert store.get_slot("mac-private")["gateway_claim_id"] != claim
    assert not store.cli_device_is_active(paired["device_id"])
    assert store.gateway_targets(key, now=NOW + 4) == []


def test_generic_owner_slots_remain_non_inference_and_machine_slots_keep_working(private):
    store, _ = private
    store.add_node("ordinary-owner", "nativeuser", now=NOW)
    store.hold_owner_node("ordinary-owner", "operator", now=NOW)
    store.set_node_access("ordinary-owner", "192.0.2.1", 22, public_key(4))
    with pytest.raises(StoreError):
        store.request_cli_pairing("ordinary-owner", "operator", now=NOW)
    with pytest.raises(StoreError):
        store.create_gateway_key("operator", "ordinary", ["ordinary-owner"], now=NOW)
    add_held_slot(store, 1, "operator", now=NOW)
    paired = store.register_cli_device(store.request_cli_pairing("s1", "operator", now=NOW),
                                      public_key(4), "regular", now=NOW + 1)
    assert store.resolve_cli_device(paired["device_token"], now=NOW + 2)


def test_mac_cannot_enter_machine_lifecycle_or_remote_account_management(private):
    store, _ = private
    before = store.get_slot("mac-private")
    actions = [lambda: store.begin_release("mac-private", held_by="operator"),
               lambda: store.rename_slot("mac-private", "renamed-private"),
               lambda: store.move_slot("mac-private", "releasing"),
               lambda: store.hold_owner_node("mac-private", "other-admin", now=NOW),
               lambda: store.unhold_owner_node("mac-private"),
               lambda: store.add_slot("extra-slot", "mac-private", "another", now=NOW),
               lambda: store.set_node_offer("mac-private"),
               lambda: store.request_login("mac-private", "operator@example.test", NOW),
               lambda: store.request_slot_login("mac-private", "operator@example.test", NOW),
               lambda: store.request_claude_update("mac-private", NOW)]
    for action in actions:
        with pytest.raises(StoreError):
            action()
    store.apply_slot_report("mac-private", [{"unix_user": "nativeuser", "present": False}], now=NOW)
    assert store.get_slot("mac-private") == before
    assert store.get_slot("renamed-private") is None
    assert store.private_owner_access("mac-private") is not None


def test_private_status_catalogue_and_other_operator_view_have_no_node(private, cfg):
    store, _ = private
    enable(store)
    assert status.snapshot(store, NOW) == {}
    assert offer_ui._public_nodes(store) == []
    assert "mac-private" not in offer_ui.public_browse(store, NOW, 180, {})
    assert "mac-private" not in offer_ui.catalogue(store, NOW, 180)
    assert private_owner.OperatorView(store, "other-admin").list_nodes() == []
    assert private_owner.OperatorView(store, "other-admin").list_slots() == []
    assert len(private_owner.OperatorView(store, "operator").list_nodes()) == 1
    page = usersite.page(store, cfg, store.get_account("operator"), "session", NOW)
    assert "Private administrator Mac" in page and "$170.00 / month" in page
    assert 'action="/account/slots/mac-private/release"' not in page
    other = usersite.page(store, cfg, store.get_account("other-admin"), "session", NOW)
    assert "mac-private" not in other and "$170.00" not in other


def test_private_gateway_requires_fresh_matching_owner_report(private):
    store, _ = private
    enable(store)
    key = endpoint(store)
    assert store.gateway_targets(key, now=NOW + 1)
    assert store.gateway_targets(key, now=NOW + 181) == []
    bad = report()
    bad["slots"][0]["credentials"]["account_fp"] = "b" * 16
    store.insert_heartbeat("mac-private", NOW + 2, bad)
    assert store.gateway_targets(key, now=NOW + 3) == []


@pytest.mark.parametrize("expiry", [None, 0, (NOW - 1) * 1000, (NOW + 10) * 1000])
def test_private_gateway_rejects_unknown_expired_or_expiring_native_access(private, expiry):
    store, _ = private
    enable(store)
    key = endpoint(store)
    raw = report()
    raw["slots"][0]["credentials"]["expires_at"] = expiry
    store.insert_heartbeat("mac-private", NOW + 1, raw)
    assert store.gateway_targets(key, now=NOW + 2) == []
    assert store.gateway_public_keys("mac-private", public_key(3), now=NOW + 2) == []


def test_private_capability_is_visible_in_gateway_selection_only_when_enabled(private):
    store, _ = private
    assert gateway_ui._selectable(store.list_slots(held_by="operator")) == []
    enable(store)
    assert [s["id"] for s in gateway_ui._selectable(store.list_slots(held_by="operator"))] == ["mac-private"]
    assert gateway_ui.selected_slots(store, "operator", {"slot.mac-private": "1"}) == ["mac-private"]


def test_private_heartbeat_has_only_fixed_grants_and_never_desired_actions(private):
    store, made = private
    enable(store)
    paired = device(store)
    endpoint(store)
    cfg = Config(bind_port=0, public_url="http://fleet.test")
    ctx = api.Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    server = api.build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def call(payload):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                conn.request("POST", "/api/heartbeat", json.dumps(payload),
                             {"Host": "fleet.test", "Authorization": "Bearer " + made["node_token"],
                              "Content-Type": "application/json"})
                reply = conn.getresponse()
                return reply.status, json.loads(reply.read())
            finally:
                conn.close()

        raw = report()
        raw["agent_ts"] = time.time()
        code, payload = call(raw)
        assert code == 200
        assert payload["desired"] == {} and payload["pinned_version"] == ""
        grants = payload["private_owner_access"]
        assert set(grants) == {"version", "unix_user", "enabled", "device_public_keys", "gateway_public_keys"}
        assert grants["device_public_keys"] == [paired["public_key"]]
        assert grants["unix_user"] == "nativeuser" and grants["enabled"] is True
        raw["mode"] = "machine"
        assert call(raw)[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_private_owner_mode_survives_bounded_heartbeat_validation():
    cleaned = heartbeat.validate_heartbeat(report(), "mac-private", now=NOW)
    assert cleaned["mode"] == "private_owner" and len(cleaned["slots"]) == 1


def test_private_response_is_delivered_without_public_throughput_observation(scenario):
    scenario.store.gateway_targets = lambda _key: [{
        "slot_id": "PRIVATE_SLOT", "claim_generation": "PRIVATE_GENERATION",
        "private_owner": True}]
    forward(scenario)
    assert scenario.attempts[0].finish_called and scenario.attempts[0].closed
    assert scenario.handler.wfile.getvalue() == framed(stream())
    assert scenario.runtime.close()
    assert scenario.store.records == []


def test_public_projections_unchanged_by_large_private_reports_and_history(private, cfg):
    store, _ = private
    add_held_slot(store, 1, "customer", now=NOW - 30)
    public_metrics = reading()
    public_metrics.update(observed_at=NOW, last_success_at=NOW)
    credentials = {"present": True, "logged_in": True, "bound_fp": "b" * 16,
                   "account_fp": "b" * 16}
    store.insert_heartbeat("m1", NOW, {"mode": "machine", "slots": [
        {"unix_user": "slot01", "present": True, "credentials": credentials,
         "relay": public_metrics}]})
    status.record(store, NOW, 60)

    def projection():
        return {"inventory": offer_ui.public_browse(store, NOW, 180, {}),
                "status_html": statuspage.page(store, cfg, None, NOW),
                "status": {key: asdict(value) for key, value in status.snapshot(store, NOW).items()},
                "history": asdict(status.history(store, NOW)),
                "performance": performance.summary(store, cfg, NOW),
                "usage": usage.public_summary(store, cfg, NOW),
                "throughput": store.gateway_throughput_summary(NOW)}

    before = projection()
    enable(store)
    private_report = report()
    private_metrics = reading(samples=9000, input_tokens=999999, output=888888,
                              read_tokens=777777, write_tokens=666666, elapsed=9_000_000)
    private_metrics.update(observed_at=NOW, last_success_at=NOW)
    private_report["slots"][0]["relay"] = private_metrics
    private_report["usage"] = {"total_tokens": 999999999}
    store.insert_heartbeat("mac-private", NOW, private_report)
    # Legacy code may assign a label or record history; neither makes it public.
    store._conn.execute("UPDATE nodes SET public_status_number=99 WHERE id='mac-private'")
    store._conn.commit()
    store.count_status("node:mac-private", status.RED, NOW, grace=2)
    assert projection() == before


def test_other_admin_cannot_read_private_node_alerts_or_manage_direct_urls(private):
    store, _ = private
    enable(store)
    store.open_alert("mac-private", "no_heartbeat", "warning", "private-mac-alert", NOW)
    cfg = Config(bind_port=0, public_url="http://fleet.test", admin_host="admin.fleet.test",
                 admin_token="synthetic-break-glass-token",
                 cookie_secret="s" * 32, cookie_secure=False,
                 google_client_id="synthetic", google_client_secret="synthetic")
    ctx = api.Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    server = api.build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def request(account, path, method="GET", *, basic=False):
            session = store.create_session(account, now=time.time(), ttl_s=3600, site="admin")
            cookie = sessions.COOKIE_NAME + "=" + sessions.sign(session, cfg.cookie_secret)
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                body = urllib.parse.urlencode({"csrf": api.csrf_token(cfg)}) if method == "POST" else None
                headers = {"Host": "admin.fleet.test", "Cookie": cookie,
                           "Content-Type": "application/x-www-form-urlencoded"}
                if basic:
                    headers["Authorization"] = "Basic " + base64.b64encode(
                        (cfg.admin_user + ":" + cfg.admin_token).encode()).decode()
                conn.request(method, path, body, headers)
                reply = conn.getresponse()
                return reply.status, reply.read().decode()
            finally:
                conn.close()

        for path in ("/api/nodes", "/api/alerts", "/admin"):
            code, body = request("other-admin", path)
            assert code == 200 and "mac-private" not in body and "170.00" not in body
            assert "private-mac-alert" not in body
        code, body = request("operator", "/api/nodes")
        assert code == 200 and "mac-private" in body
        for path in ("/actions/node/mac-private/disable", "/actions/machine/mac-private/access",
                     "/actions/slot/mac-private/release"):
            assert request("other-admin", path, "POST")[0] == 403
        assert store.get_node("mac-private")["enabled"] is True
        # A valid break-glass credential must not combine with a demoted
        # owner's cookie to manufacture private administrator authority.
        store.set_account_role("operator", "user")
        add_held_slot(store, 1, "customer", now=NOW)
        for path in ("/api/nodes", "/api/alerts", "/admin"):
            code, body = request("operator", path, basic=True)
            assert code == 200 and "mac-private" not in body and "private-mac-alert" not in body
        for path in ("/actions/node/mac-private/disable", "/actions/machine/mac-private/access",
                     "/actions/slot/mac-private/release"):
            assert request("operator", path, "POST", basic=True)[0] == 403
        assert store.get_node("mac-private")["enabled"] is True
        assert request("operator", "/actions/node/m1/disable", "POST", basic=True)[0] == 303
        assert store.get_node("m1")["enabled"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_private_alerts_stay_local_and_do_not_disclose_a_private_duplicate_elsewhere(private, cfg):
    store, _ = private
    enable(store)
    add_held_slot(store, 1, "customer", now=NOW - 30)
    public = report()
    public.update(node_id="m1", mode="machine")
    public["slots"][0]["unix_user"] = "slot01"
    store.insert_heartbeat("m1", NOW, public)

    class Notifier:
        messages = []

        def send(self, message):
            self.messages.append(message)

    notifier = Notifier()
    monitor = Monitor(store, cfg, notifier)
    monitor.record_heartbeat(store.get_node("mac-private"), report(), NOW)
    assert any(alert["rule"] == "account_elsewhere"
               for alert in store.open_alerts("mac-private"))
    assert notifier.messages == []
    monitor.check_node(store.get_node("m1"), NOW)
    assert not any(alert["rule"].startswith("account_elsewhere")
                   for alert in store.open_alerts("m1"))
    assert "mac-private" not in repr(notifier.messages)
    monitor.record_status(NOW)
    assert store.open_outage("mac-private") is None
    assert private_owner.OperatorView(store, "other-admin").held_slot_count("operator") == 0
    assert private_owner.OperatorView(store, "operator").held_slot_count("operator") == 1


def test_private_duplicate_warning_redacts_other_private_owner_identity(private, cfg):
    store, _ = private
    enable(store)
    store.register_private_owner_node("private-other-admin", "other-admin", "othernative",
        monthly_cost_usd_cents=30000, now=NOW)
    other = report()
    other["node_id"] = "private-other-admin"
    other["slots"][0]["unix_user"] = "othernative"
    store.insert_heartbeat("private-other-admin", NOW, other)
    monitor = Monitor(store, cfg, LogNotifier())
    monitor.check_node(store.get_node("mac-private"), NOW)
    alerts = [alert for alert in store.open_alerts("mac-private")
              if alert["rule"] == "account_elsewhere"]
    assert len(alerts) == 1
    assert "another private node" in alerts[0]["message"]
    assert "private-other-admin" not in json.dumps(alerts)
    assert "othernative" not in json.dumps(alerts)
