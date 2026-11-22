"""Reusable Gemini enrollment grants each computer a separate workspace-only identity."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

import pytest

from ccfleetd import cli_access, usersite
from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store, StoreError
from tests.test_api import call
from tests.test_gateway_access import public_key
from tests.test_private_owner_nodes import NOW, device, enable
from tests.test_private_owner_nodes import private as private


@pytest.fixture
def enabled(private):
    store, _ = private
    enable(store)
    store.private_owner_access("mac-private", workspace_at=NOW)
    return store


def issue(store, expected_id="", *, now=NOW):
    return store.set_antigravity_enrollment("operator", expected_id=expected_id, now=now)


def enroll(store, code, key, *, now=NOW):
    return store.register_cli_device(code, public_key(key), f"Mac {key}", now=now,
                                     workspace_pairing=True)


def status(store):
    return store.antigravity_pairing_status("mac-private", "operator", now=NOW)


@pytest.mark.parametrize("rename", ["rename_node", "rename_slot"])
def test_private_rename_cannot_move_an_enrollment_or_disconnect_its_computers(enabled, rename):
    store = enabled
    code = issue(store)
    first = enroll(store, code, 4)
    with pytest.raises(StoreError, match="private owner"):
        getattr(store, rename)("mac-private", "renamed-private")
    assert store.get_slot("renamed-private") is None
    assert store.resolve_cli_device(first["device_token"], now=NOW)["slot_id"] == "mac-private"
    assert enroll(store, code, 5)["slot_id"] == "mac-private"


def test_reusable_code_rotation_and_revocation_preserve_each_devices_grant(enabled):
    store = enabled
    claude = device(store)
    code = issue(store)
    initial = status(store)
    assert initial["enrollment_id"] and initial["devices"] == [] and initial["pending_count"] == 0
    assert code.startswith(cli_access.GEMINI_ENROLL_PREFIX)
    assert code not in "\n".join(store._conn.iterdump())
    assert cli_access.token_hash(code) not in json.dumps(initial)
    first = enroll(store, code, 4)
    later = NOW + 31 * 86400
    store.private_owner_access("mac-private", workspace_at=later)
    second = enroll(store, code, 5, now=later)
    assert first["workspace_only"] and second["workspace_only"]
    assert first["device_token"] != second["device_token"] and first["device_id"] != second["device_id"]
    assert len(status(store)["devices"]) == 2
    registered = {row["id"]: row for row in store.list_cli_devices("mac-private", held_by="operator")}
    assert registered[first["device_id"]]["workspace_only"] is True
    assert "workspace_only" not in registered[claude["device_id"]]
    assert store.resolve_cli_device(code, now=later) is None

    with pytest.raises(StoreError, match="changed"):
        issue(store, now=later)
    replacement = issue(store, initial["enrollment_id"], now=later)
    with pytest.raises(StoreError, match="changed"):
        issue(store, initial["enrollment_id"], now=later)
    with pytest.raises(StoreError, match="changed"):
        store.revoke_antigravity_enrollment("operator", initial["enrollment_id"])
    with pytest.raises(StoreError, match="no longer valid"):
        enroll(store, code, 6, now=later)
    third = enroll(store, replacement, 6, now=later)
    store.revoke_antigravity_enrollment("operator", status(store)["enrollment_id"])
    assert status(store)["enrollment_id"] is None
    with pytest.raises(StoreError, match="no longer valid"):
        enroll(store, replacement, 7, now=later)
    for computer in (first, second, third, claude):
        assert store.resolve_cli_device(computer["device_token"], now=later)
    store.revoke_cli_device("mac-private", first["device_id"], held_by="operator")
    assert not store.resolve_cli_device(first["device_token"], now=later)
    assert store.resolve_cli_device(second["device_token"], now=later)["workspace_only"]


def test_reusable_code_checks_owner_claim_generation_heartbeat_and_capacity(enabled, monkeypatch):
    store = enabled
    code = issue(store)
    with pytest.raises(StoreError):
        store.set_antigravity_enrollment("other-admin", expected_id="", now=NOW)
    with pytest.raises(StoreError):
        store.revoke_antigravity_enrollment("other-admin", status(store)["enrollment_id"])
    with pytest.raises(StoreError, match="offline"):
        enroll(store, code, 4, now=NOW + 181)
    with pytest.raises(StoreError, match="renew"):
        store.register_cli_device(code, public_key(4), "denied", now=NOW, workspace_pairing=True,
                                  admit=lambda slot: False)
    with pytest.raises(StoreError, match="workspace"):
        store.register_cli_device(code, public_key(4), "normal CLI", now=NOW)
    assert not status(store)["devices"]
    first = enroll(store, code, 4)
    with pytest.raises(StoreError, match="fresh device key"):
        enroll(store, code, 4)
    monkeypatch.setattr(cli_access, "MAX_DEVICES_PER_SLOT", 1)
    with pytest.raises(StoreError, match="too many"):
        enroll(store, code, 5)
    store.revoke_cli_device("mac-private", first["device_id"], held_by="operator")
    store.set_node_access("mac-private", "127.0.0.1", 2244, public_key(1))
    assert status(store)["enrollment_id"] is None
    with pytest.raises(StoreError, match="no longer valid"):
        enroll(store, code, 5)
    fresh = issue(store)
    store.set_private_owner_inference("mac-private", "operator", False, now=NOW)
    store.set_private_owner_inference("mac-private", "operator", True, now=NOW)
    with pytest.raises(StoreError, match="no longer valid"):
        enroll(store, fresh, 5)


def test_upgrade_and_concurrent_creation_preserve_existing_connections(enabled, tmp_path):
    old_cli = device(enabled)
    old_code = enabled.antigravity_pairing("operator", now=NOW)
    path = tmp_path / "fleet.db"
    with closing(sqlite3.connect(path)) as original:
        enabled._conn.backup(original)
        original.execute("DROP TABLE agy_enrollments")
        original.commit()

    barrier = threading.Barrier(2)

    def create(store):
        barrier.wait(5)
        try:
            return issue(store)
        except StoreError as exc:
            assert "changed" in str(exc)
            return None

    with closing(Store(path)) as first, closing(Store(path)) as second, ThreadPoolExecutor(2) as pool:
        results = list(pool.map(create, (first, second)))
    assert results.count(None) == 1
    code = next(result for result in results if result)
    with closing(Store(path)) as reopened:
        assert reopened.resolve_cli_device(old_cli["device_token"], now=NOW)
        assert enroll(reopened, old_code, 4)["workspace_only"]
        assert enroll(reopened, code, 5)["workspace_only"]
        assert len(status(reopened)["devices"]) == 2


def test_registration_api_reuses_only_the_new_workspace_code(enabled, cfg):
    store = enabled
    now = time.time()
    store.private_owner_access("mac-private", workspace_at=now)
    reusable = issue(store, now=now)
    single = store.antigravity_pairing("operator", now=now)
    config = replace(cfg, public_url="https://fleet.example.com")
    server = build_server(Context(store, config, Monitor(store, config, LogNotifier())),
                          host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def register(code, key, *, purpose="antigravity-workspace"):
            return call(server, "POST", "/api/cli/register", {
                "public_key": public_key(key), "device_name": f"Mac {key}", "purpose": purpose,
            }, {"Authorization": f"Bearer {code}"})

        computers = []
        for key in (2, 3):
            result, body, _ = register(reusable, key)
            assert result == 201
            computer = json.loads(body)
            assert computer["workspace_only"] and reusable.encode() not in body
            computers.append(computer)
        assert register(reusable, 4, purpose="claude")[0] == 400
        assert register(single, 5)[0] == 201
        assert register(single, 6)[0] == 400
        for token, expected in ((reusable, 401), (computers[0]["device_token"], 200)):
            assert call(server, "POST", "/api/cli/revoke",
                        headers={"Authorization": "Bearer " + token})[0] == expected
        assert not store.cli_device_is_active(computers[0]["device_id"])
        assert store.cli_device_is_active(computers[1]["device_id"])

        owner = store.get_account("operator")
        path = "/account/slots/mac-private/cli-revoke"
        device_id = computers[1]["device_id"]
        assert usersite.act(store, config, owner, path, {"device_id": device_id}, now).status == 303
        assert not store.cli_device_is_active(device_id)
        assert register(reusable, 7)[0] == 201
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
