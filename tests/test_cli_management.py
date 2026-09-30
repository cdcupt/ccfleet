"""Local management stays on one pairing and never launches Claude or SSH."""
import copy
import io
import json
import signal
import time
from types import SimpleNamespace

import pytest

from ccfleet_agent import client_experience
from tests.test_native_local_cli import client as client  # noqa: F401


@pytest.fixture
def paired(client, monkeypatch):  # noqa: F811
    client.device.update(server="https://fleet.invalid", device_token="PRIVATE_TOKEN")
    client.cli["save_config"]({"devices": {"device": client.device}, "active": "device"})
    client.context = {"authenticated": True, "device": {"active": True}, "protocol": 2,
                      "slot": {"state": "active", "ready": True, "health": "ready",
                               "reason": "credentials_current", "readiness_source": "reported",
                               "observed_at": time.time(), "account_generation": "a" * 24}}
    client.reads = []
    def context(device):
        client.reads.append(device.copy())
        return copy.deepcopy(client.context)
    monkeypatch.setitem(client.scope, "client_experience", lambda: client_experience)
    monkeypatch.setitem(client.scope, "device_context", context)
    return client


def callback(paired, monkeypatch):
    recorded = []
    def serve(read_status):
        recorded.append(read_status)
        return 0
    monkeypatch.setitem(paired.scope, "client_experience",
                        lambda: SimpleNamespace(serve_mcp=serve))
    assert paired.cli["main"](["mcp", "serve"]) == 0
    assert not paired.reads and not paired.events and not paired.calls
    return recorded[0]


@pytest.mark.parametrize("field", ["device_id", "device_token", "slot_id", "server"])
def test_pairing_replacement_stops_without_requesting_other_account(paired, monkeypatch, field):
    read = callback(paired, monkeypatch)
    assert read()["authenticated"] is True
    config = paired.cli["load_config"]()
    config["devices"]["device"][field] = "PRIVATE_REPLACEMENT"
    paired.cli["save_config"](config)
    with pytest.raises(paired.cli["CliError"], match="pairing changed") as error:
        read()
    assert "PRIVATE" not in str(error.value) and len(paired.reads) == 1
    config["devices"]["device"] = paired.device
    paired.cli["save_config"](config)
    with pytest.raises(paired.cli["CliError"], match="authorization changed"):
        read()
    assert len(paired.reads) == 1


def test_active_device_selection_cannot_redirect_an_existing_mcp_process(paired, monkeypatch):
    read = callback(paired, monkeypatch)
    config = paired.cli["load_config"]()
    config["devices"]["other"] = {**paired.device, "device_id": "other", "slot_id": "other-slot"}
    config["active"] = "other"
    paired.cli["save_config"](config)
    assert read()["authenticated"] is True
    assert paired.reads[-1]["device_id"] == "device"


def test_account_generation_change_invalidates_until_explicit_restart(paired, monkeypatch):
    read = callback(paired, monkeypatch)
    read()
    paired.context["slot"]["account_generation"] = "b" * 24
    with pytest.raises(paired.cli["CliError"], match="assignment changed"):
        read()
    with pytest.raises(paired.cli["CliError"], match="authorization changed"):
        read()
    assert len(paired.reads) == 2


@pytest.mark.parametrize("status", [401, 403, 404])
def test_revoke_failure_is_fixed_and_no_later_callback_runs(paired, monkeypatch, status):
    read = callback(paired, monkeypatch)
    def denied(_device):
        raise paired.cli["CliError"]("PRIVATE_UPSTREAM_MESSAGE", status)
    monkeypatch.setitem(paired.scope, "device_context", denied)
    with pytest.raises(paired.cli["CliError"], match="status is unavailable") as error:
        read()
    assert "PRIVATE" not in str(error.value)
    monkeypatch.setitem(paired.scope, "device_context", lambda _: pytest.fail("revoked request"))
    with pytest.raises(paired.cli["CliError"], match="authorization changed"):
        read()


@pytest.mark.parametrize("change", [{"authenticated": False}, {"protocol": True},
                                    {"device": {"active": False}}, {"slot": {}},
                                    {"slot": {"account_generation": "PRIVATE"}}])
def test_malformed_status_cannot_become_authorized(paired, monkeypatch, change):
    read = callback(paired, monkeypatch)
    paired.context.update(change)
    with pytest.raises(paired.cli["CliError"], match="valid authenticated status") as error:
        read()
    assert "PRIVATE" not in str(error.value)


def test_mcp_total_deadline_interrupts_even_a_callback_that_never_finishes(paired, monkeypatch):
    previous = signal.getsignal(signal.SIGALRM)
    monkeypatch.setitem(paired.scope, "MCP_READ_TIMEOUT", 0.05)
    monkeypatch.setitem(paired.scope, "device_context", lambda _: time.sleep(1))
    started = time.monotonic()
    with pytest.raises(paired.cli["CliError"], match="deadline"):
        paired.cli["mcp_status_context"](paired.device)
    assert time.monotonic() - started < 0.5
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is previous


def test_mcp_never_replaces_an_existing_deadline(paired, monkeypatch):
    monkeypatch.setitem(paired.scope, "device_context", lambda _: pytest.fail("request"))
    previous = signal.getsignal(signal.SIGALRM)
    try:
        signal.setitimer(signal.ITIMER_REAL, 10)
        with pytest.raises(paired.cli["CliError"], match="available main-thread"):
            paired.cli["mcp_status_context"](paired.device)
        assert 9 < signal.getitimer(signal.ITIMER_REAL)[0] <= 10
        assert signal.getsignal(signal.SIGALRM) is previous
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_real_stdio_discovery_and_call_keep_output_protocol_only(paired, monkeypatch):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "PRIVATE_CLIENT_MACHINE", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "ccfleet_health", "arguments": {}}},
    ]
    source = io.BytesIO(b"".join((json.dumps(line) + "\n").encode() for line in lines))
    output = io.BytesIO()
    with monkeypatch.context() as patches:
        patches.setattr(paired.cli["sys"], "stdin", SimpleNamespace(buffer=source))
        patches.setattr(paired.cli["sys"], "stdout", SimpleNamespace(buffer=output))
        assert paired.cli["main"](["mcp", "serve"]) == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert len(replies[1]["result"]["tools"]) == 3
    assert replies[2]["result"].get("isError") is not True
    assert "PRIVATE" not in output.getvalue().decode()
    assert "account_generation" not in output.getvalue().decode()
    assert len(paired.reads) == 1 and not paired.events and not paired.calls
