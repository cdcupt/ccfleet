import json
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from ccfleetd import dedicated_slots, home_mac_transport, home_macs
from ccfleetd.store import Store
from tests.test_gateway_access import add_held_slot
from tests.test_home_mac_registration import generation
from tests.test_home_mac_registration import service as _registration_service


@pytest.fixture
def slot_service(tmp_path, monkeypatch):
    yield from _registration_service.__wrapped__(tmp_path, monkeypatch)


def enroll(service, tmp, mp, name):
    store, cfg, request, cookies, _ = service
    tmp.mkdir()
    token, code, _ = generation(tmp, mp, request)
    status, _, headers = request(
        home_macs.ADD_ACTION, "alice", {"csrf": cookies["alice"][1], "code": code, "name": name}
    )
    assert status == 303
    uri = urlsplit(headers["Location"])
    assert uri.path == home_macs.POOL
    sid = parse_qs(uri.query)["slot"][0]
    return token, sid


def status(request, slot, owner="alice"):
    response = request(home_macs.STATUS + "?slot=" + slot, owner)
    assert response[0] == 200, response
    return json.loads(response[1])["state"]


def test_two_macs_and_hosted_slot_are_private_and_separately_selectable(
    slot_service, tmp_path, monkeypatch
):
    service = slot_service
    store, cfg, request, cookies, _ = service
    assert "/account/machines/add" in request("/account", "alice")[1]
    assert "Add my Mac" in request(home_macs.ADD, "alice")[1]
    _, one = enroll(service, tmp_path / "one", monkeypatch, "Living room Mac")
    _, two = enroll(service, tmp_path / "two", monkeypatch, "Studio Mac")
    assert one != two
    store.set_slot_quota("alice", 1)
    add_held_slot(store, 6, "alice")
    for slot, name in ((one, "Living room Mac"), (two, "Studio Mac"), ("s6", "")):
        code, body, _ = request(dedicated_slots.slot_url(slot), "alice")
        assert code == 200 and f'value="{slot}" selected' in body
        assert all(f'value="{n}"' in body for n in (one, two, "s6"))
        assert name in body
        assert request(dedicated_slots.slot_url(slot), "bob")[0] == 404
    assert request(home_macs.STATUS + "?slot=" + one, "bob")[0] == 404
    assert store.held_slot_count("alice") == 1
    own = home_macs.list_for_owner(store, "alice", time.time())
    removed = next(m for m in own if m["slot_id"] == one)
    home_macs.remove(store, "alice", removed["id"], time.time())
    assert request(dedicated_slots.slot_url(one), "alice")[0] == 404
    assert request(dedicated_slots.slot_url(two), "alice")[0] == 200
    assert store.get_node(two)["enabled"]


def test_animation_reflects_real_probe_and_fixed_connection(slot_service, tmp_path, monkeypatch):
    service = slot_service
    store, cfg, request, cookies, _ = service
    token, sid = enroll(service, tmp_path / "host", monkeypatch, "Home mini")
    auth = {"Authorization": "Bearer " + token}
    assert status(request, sid) == "waiting"
    assert request(home_macs.API + "check-in", fields={}, headers=auth)[0] == 200
    assert request(home_macs.API + "wait", fields={}, headers=auth)[0] == 200
    until = time.monotonic() + 5
    ticket = None
    while not ticket and time.monotonic() < until:
        ticket = json.loads(request(home_macs.API + "wait", fields={}, headers=auth)[1])["ticket"]
    assert status(request, sid) == "checking"
    assert 'data-state="checking"' in request(dedicated_slots.slot_url(sid), "alice")[1]
    reg = home_macs.for_owner(store, "alice", time.time())["id"]
    assert not home_mac_transport.accept(store, cfg.db_path, "0" * 32, ticket)
    with home_mac_transport.accept(store, cfg.db_path, reg, ticket) as peer:
        peer.sendall(b"SSH-2.0-TestSlot\r\n")
    while status(request, sid) == "checking" and time.monotonic() < until:
        time.sleep(0.02)
    assert status(request, sid) == "connected"
    credentials = {
        "present": True,
        "logged_in": True,
        "bound_fp": "a" * 16,
        "account_fp": "a" * 16,
        "expires_at": (time.time() + 3600) * 1000,
    }
    store.insert_heartbeat(
        sid,
        time.time(),
        {
            "mode": "private_owner",
            "credentials": credentials,
            "slots": [{"present": True, "unix_user": "macowner", "credentials": credentials}],
        },
    )
    code, body, _ = request(dedicated_slots.slot_url(sid), "alice")
    assert code == 200 and "/account/dedicated/connect" in body and 'data-state="connected"' in body
    slot = store.get_slot(sid)
    form = {
        "csrf": cookies["alice"][1],
        "slot": sid,
        "claim": slot["gateway_claim_id"],
        "service": "anthropic",
    }
    assert request(home_macs.POOL + "/connect", "alice", {**form, "claim": "0" * 32})[0] == 403
    assert request(home_macs.POOL + "/connect", "alice", {**form, "service": "devin"})[0] == 409
    assert (
        request(home_macs.POOL + "/connect", "bob", {**form, "csrf": cookies["bob"][1]})[0] == 403
    )
    code, body, _ = request(home_macs.POOL + "/connect", "alice", form)
    assert code == 200 and "Save your connection key" in body
    assert store.list_gateway_keys("alice")[0]["slot_ids"] == [sid]


def test_migration_from_one_mac_constraints_preserves_active_keys(tmp_path):
    from ccfleetd import private_owner

    dbfile = str(tmp_path / "old.db")
    conn = sqlite3.connect(dbfile)
    conn.executescript(
        private_owner.SCHEMA.replace(
            "account_id TEXT NOT NULL,\n    monthly_cost",
            "account_id TEXT NOT NULL UNIQUE,\n    monthly_cost",
        )
    )
    conn.executescript(
        home_macs.SCHEMA.replace("owner_id TEXT NOT NULL,", "owner_id TEXT NOT NULL UNIQUE,")
    )
    conn.execute("INSERT INTO private_owner_nodes VALUES ('home-one','alice',0,1,0,1,1)")
    conn.execute(
        "INSERT INTO home_macs(registration_id,owner_id,node_id,name,public_key,device_hash)"
        " VALUES ('original','alice','home-one','Office','k1','d1')"
    )
    conn.execute("INSERT INTO agy_hosts VALUES ('home-one',1)")
    conn.commit()
    conn.close()
    store = Store(dbfile)
    try:
        with store._write_txn() as db:
            db.execute("INSERT INTO private_owner_nodes VALUES ('home-two','alice',0,1,0,2,2)")
            db.execute(
                "INSERT INTO home_macs(registration_id,owner_id,node_id,name,public_key,device_hash) "
                "VALUES ('another','alice','home-two','Office','k2','d2')"
            )
            assert (
                db.execute("SELECT COUNT(*) FROM home_macs WHERE owner_id='alice'").fetchone()[0]
                == 2
            )
            assert (
                db.execute(
                    "SELECT device_hash FROM home_macs WHERE registration_id='original'"
                ).fetchone()[0]
                == "d1"
            )
            assert db.execute("SELECT node_id FROM agy_hosts").fetchone()[0] == "home-one"
    finally:
        store.close()


def test_failed_check_retry_and_offline_are_distinct(slot_service, tmp_path, monkeypatch):
    store, cfg, request, cookies, _ = slot_service
    token, sid = enroll(slot_service, tmp_path / "host", monkeypatch, "Failure fixture")
    auth = {"Authorization": "Bearer " + token}
    request(home_macs.API + "check-in", fields={}, headers=auth)
    ticket = None
    until = time.monotonic() + 4
    while not ticket and time.monotonic() < until:
        ticket = json.loads(request(home_macs.API + "wait", fields={}, headers=auth)[1])["ticket"]
    reg = home_macs.for_slot(store, "alice", sid, time.time())["id"]
    with home_mac_transport.accept(store, cfg.db_path, reg, ticket) as peer:
        peer.sendall(b"wrong service\r\n")
    while status(request, sid) == "checking" and time.monotonic() < until:
        time.sleep(0.02)
    assert status(request, sid) == "retry"
    assert request(home_macs.PROBE, "bob", {"csrf": cookies["bob"][1], "slot": sid})[0] == 403
    assert request(home_macs.PROBE, "alice", {"csrf": cookies["alice"][1], "slot": sid})[0] == 303
    assert status(request, sid) == "waiting"
    assert home_macs.for_slot(store, "alice", sid, time.time() + 130)["tunnel_state"] == "offline"


def test_personal_slots_coexist_with_legacy_gemini(slot_service, tmp_path, monkeypatch):
    store, cfg, req, _, _ = slot_service
    _, home = enroll(slot_service, tmp_path / "home", monkeypatch, "Personal Mac")
    store.set_account_role("alice", "admin")
    store.register_private_owner_node(
        "legacy", "alice", "nativeuser", monthly_cost_usd_cents=0, now=time.time()
    )
    from tests.test_gateway_access import public_key

    store.set_node_access("legacy", "localhost", 22, public_key(1))
    store.set_private_owner_inference("legacy", "alice", True, now=time.time())
    assert store.gemini_setup_state("alice")[0]["id"] == "legacy"
    store.set_account_role("alice", "user")
    assert store.slot_inference_allowed(home, "alice")
    assert not store.slot_inference_allowed("legacy", "alice")


def test_other_slot_provider_and_account_services_keep_their_routing(
    slot_service, tmp_path, monkeypatch
):
    from tests.test_gateway_provider_controls import configured, native_report

    store, cfg, req, cookies, _ = slot_service
    account = store.get_account("alice")
    config = configured(cfg, openrouter_models="example/model", devin_trial_enabled=True)
    _, sid = enroll(slot_service, tmp_path / "home", monkeypatch, "Studio")
    store.set_slot_quota("alice", 1)
    add_held_slot(store, 8, "alice")
    store.apply_slot_report("m8", [{"unix_user": "slot01", "present": True}], now=time.time())
    hosted = store.get_slot("s8")
    now = time.time()
    store.bind_gateway_provider(
        "s8",
        "alice",
        "devin",
        hosted["gateway_claim_id"],
        native_report(now),
        expected_generation=0,
        now=now,
    )
    result = dedicated_slots.connect(
        store,
        config,
        account,
        "session",
        {
            "csrf": "checked-by-http",
            "service": "devin",
            "claim": hosted["gateway_claim_id"],
            "slot": "s8",
        },
        now,
    )
    assert result.status == 200
    selected = store.list_gateway_keys("alice")[0]
    assert selected["slot_ids"] == ["s8"] and selected["provider"] == "devin"
    home = dedicated_slots.page(store, config, account, "session", now, sid)
    assert 'href="/account/openrouter"' in home and 'href="/account/devin-trial"' in home
    assert 'name="service" value="devin"' not in home
    key = store.create_gateway_key("alice", "GPT on hosted", ["s8"], now=now)
    reservation = store.reserve_api_model(key["id"], "s8", "openai", "org-example", "gpt-model")
    store.grant_api_model(key["id"], "s8", {**reservation, "credential_fp": "f" * 64})
    page = dedicated_slots.page(store, config, account, "session", now, "s8")
    assert "<code>fleet-gpt</code>" in page and "<code>fleet-gpt</code>" not in home
