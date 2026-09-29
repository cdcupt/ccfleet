"""A transport replica shares authorization, but has no independent maintenance loop."""

import threading
from dataclasses import replace

import pytest

from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store
from tests.test_api import _active_cli_slot, _ssh_key, call


@pytest.fixture
def replicas(tmp_path, cfg):
    path = str(tmp_path / "fleet.sqlite")
    primary, replica = Store(path), Store(path)
    slot = _active_cli_slot(primary)
    pairing = primary.request_cli_pairing(slot["id"], slot["held_by"], now=10)
    device = primary.register_cli_device(pairing, _ssh_key(), "test-device", now=11)
    servers = []
    for store, config in ((primary, cfg), (replica, replace(cfg, broker_only=True))):
        server = build_server(Context(store, config, Monitor(store, config, LogNotifier())),
                              host="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    yield servers, primary, replica, device
    for server in servers:
        server.shutdown()
        server.server_close()
    primary.close()
    replica.close()


def test_replica_observes_primary_revocation_without_cache(replicas):
    servers, primary, _, device = replicas
    auth = {"Authorization": "Bearer " + device["device_token"]}
    assert all(call(server, "GET", "/api/cli/status", headers=auth)[0] == 200
               for server in servers)
    primary.revoke_cli_token(device["device_token"])
    assert all(call(server, "GET", "/api/cli/status", headers=auth)[0] == 401
               for server in servers)


def test_primary_observes_revocation_requested_through_replica(replicas):
    servers, _, _, device = replicas
    auth = {"Authorization": "Bearer " + device["device_token"]}
    assert call(servers[1], "POST", "/api/cli/revoke", {}, auth)[0] == 200
    assert call(servers[0], "GET", "/api/cli/status", headers=auth)[0] == 401


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/account"),
                                        ("GET", "/admin"), ("GET", "/api/nodes"),
                                        ("POST", "/api/heartbeat"),
                                        ("POST", "/api/cli/register")])
def test_replica_has_no_account_console_or_provisioning_surface(replicas, method, path):
    servers, _, _, _ = replicas
    assert call(servers[1], method, path, {})[0] == 404


def test_replica_health_remains_available_if_primary_http_listener_is_down(replicas):
    servers, _, _, device = replicas
    servers[0].shutdown()
    servers[0].server_close()
    assert call(servers[1], "GET", "/healthz")[0] == 200
    assert call(servers[1], "GET", "/api/cli/status", headers={
        "Authorization": "Bearer " + device["device_token"]})[0] == 200
