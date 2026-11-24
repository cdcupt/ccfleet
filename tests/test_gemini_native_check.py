import hashlib
import io
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import gemini_native_check as probe

ROOT = Path(__file__).resolve().parents[1]


def packet(value):
    stream = io.BytesIO()
    probe.frame(stream, value)
    stream.seek(0)
    return stream


def hello(root):
    return {
        "version": 1,
        "root": str(root),
        "marker_hash": "0" * 64,
        "tools": {
            n: {
                "description": n,
                "parametersJsonSchema": {
                    "properties": {"toolSummary": {}},
                    "required": ["toolSummary"],
                },
            }
            for n in probe.TOOLS
        },
    }


def test_two_cli_probe_requires_the_workspace_grant_before_greeting(tmp_path):
    out = io.BytesIO()
    with pytest.raises(ValueError, match="grant"):
        probe.slot(io.BytesIO(), out, tmp_path / "agy", lambda: False)
    assert out.getvalue() == b""


def test_client_rejects_an_unexpected_or_repeated_operation(tmp_path):
    cmd = {
        "call": "a" * 32,
        "name": "run_command",
        "args": {"CommandLine": "git rev-parse --show-toplevel"},
    }
    relay = SimpleNamespace(stdin=io.BytesIO(), stdout=packet(cmd))
    c = probe.Client(tmp_path, "marker", relay)
    with pytest.raises(ValueError, match="unexpected"):
        c.next_response()
    name, args = probe.plan(tmp_path)[0]
    valid = dict(cmd, name=name, args=args)
    relay.stdout = packet(valid)
    _, body = c.next_response()
    assert (
        json.loads(body.decode()[6:])["candidates"][0]["content"]["parts"][0]["functionCall"]["id"]
        == valid["call"]
    )
    relay.stdout = packet(valid)
    with pytest.raises(ValueError, match="operation ID"):
        c.next_response()


def test_remote_schema_accounts_for_mcp_wrapper_and_rechecks_grant(tmp_path):
    enabled = True
    out = io.BytesIO()
    r = probe.Remote(hello(tmp_path / ".ccfleet-native-test"), io.BytesIO(), out, lambda: enabled)
    _, payload = r.dispatch("/tools/list", {})
    for tool in json.loads(payload)["tools"]:
        assert "toolSummary" not in tool["inputSchema"]["properties"]
        assert "toolSummary" not in tool["inputSchema"]["required"]
    enabled = False
    with pytest.raises(ValueError, match="grant"):
        r.dispatch("/tools/call", {"name": "read_file", "arguments": r.steps[0][1]})
    assert out.getvalue() == b""


def test_remote_refuses_a_different_marker_or_session_result(tmp_path):
    r = probe.Remote(
        hello(tmp_path / ".ccfleet-native-test"), io.BytesIO(), io.BytesIO(), lambda: True
    )
    with pytest.raises(ValueError, match="proof"):
        r.verify(0, {"output": "native-marker-" + "b" * 48})
    with pytest.raises(ValueError, match="denial"):
        r.verify(3, {"error": "timeout"})


def test_launcher_uses_pinned_component_and_existing_gemini_transport(tmp_path, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["cmd_gemini"].__globals__
    assert cli["NATIVE_CHECK_HASH"] == hashlib.sha256(Path(probe.__file__).read_bytes()).hexdigest()
    assert (
        cli["packaged_helper"]("gemini_native_check", cli["NATIVE_CHECK_HASH"]).TOOLS == probe.TOOLS
    )
    device = {
        "device_id": "test",
        "slot_id": "slot",
        "server": "https://ccfleet.io",
        "endpoint": "wss://ccfleet.io/api/cli/connect",
    }
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "config"))

    def config():
        assert scope["home"]() == tmp_path / "config/antigravity-workspace"
        return {"devices": {"test": device}, "active": "test"}

    def transport(d, *, tty, fleet_only):
        assert d == device and not tty and fleet_only
        return ["paired-transport"]

    monkeypatch.setitem(scope, "load_config", config)
    monkeypatch.setitem(scope, "ssh_transport_command", transport)
    calls = []
    monkeypatch.setitem(
        scope, "packaged_helper", lambda n, h: SimpleNamespace(run=lambda *a: calls.append(a))
    )
    monkeypatch.setitem(scope, "installed_agent", lambda name: "/trusted/agy")
    args = SimpleNamespace(
        project=str(tmp_path), slot="", pair=False, preview=False, native_check=True
    )
    assert cli["cmd_gemini"](args) == 0
    assert calls == [
        ("/trusted/agy", tmp_path, ["paired-transport", "ccfleet-gemini-native-check-v1"])
    ]
