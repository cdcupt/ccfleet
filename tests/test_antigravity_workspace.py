"""Owner opt-in, device separation, selected-root operations and protocol boundaries."""

import hashlib
import os
import sys
from pathlib import Path

import pytest

from ccfleet_agent import antigravity_slot, workspace_io
from ccfleet_agent.workspace_tools import Workspace
from tests.test_gateway_access import public_key


def test_workspace_tools_read_save_local_approval_and_boundaries(tmp_path):
    repo = tmp_path / "chosen"
    repo.mkdir()
    calls = []
    workspace = Workspace(repo, lambda action, detail: not calls.append((action, detail)))
    try:
        make = {"path": "a.txt", "expected": "new", "text": "one"}
        assert workspace.call("save_project", make)["saved"] == "a.txt"
        assert (repo / "a.txt").read_text() == "one"
        version = workspace.call("read_project", {"path": "a.txt"})["sha256"]
        assert workspace.call("list_project", {}) == ["a.txt"]
        (repo / "a.txt").write_text("two")
        with pytest.raises(ValueError, match="changed"):
            workspace.call("save_project", {**make, "expected": version})
        outside = tmp_path / "another-project"
        outside.mkdir()
        (outside / "readme").write_text("private")
        (repo / "link").symlink_to(outside)
        for name in ("../another-project/readme", "link/readme", ".git/config"):
            with pytest.raises((ValueError, OSError)):
                workspace.call("read_project", {"path": name})
        os.link(outside / "readme", repo / "hard")
        with pytest.raises(ValueError):
            workspace.call("read_project", {"path": "hard"})
        assert not workspace.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert workspace.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
        workspace.approve = lambda *_: False
        with pytest.raises(ValueError, match="declined"):
            workspace.call("save_project", {**make, "path": "declined"})
        assert not (repo / "declined").exists()
        with pytest.raises(ValueError):
            workspace.call("test_project", {"command": "unused"})
    finally:
        workspace.close()


def test_packet_boundaries_and_daemon_revocation(tmp_path, monkeypatch):
    original = workspace_io.frame(b"K", b"hello") + workspace_io.frame(b"R", b"1234")
    partial = bytearray(original[:7])
    assert list(workspace_io.unpack(partial)) == []
    partial.extend(original[7:])
    assert list(workspace_io.unpack(partial)) == [(b"K", b"hello"), (b"R", b"1234")]
    monkeypatch.setattr(antigravity_slot.mac_policy, "require_enabled", lambda home: None)
    key = public_key(5)
    grant = {"access": {"device_public_keys": [key], "workspace_public_keys": [key]}}
    monkeypatch.setattr(antigravity_slot.mac_policy, "read_json", lambda *args: grant)
    fingerprint = hashlib.sha256(key.encode()).digest()
    assert antigravity_slot.permitted(tmp_path, fingerprint)
    grant["access"]["workspace_public_keys"] = []
    assert not antigravity_slot.permitted(tmp_path, fingerprint)


@pytest.mark.skipif(sys.platform != "darwin", reason="uses the macOS test sandbox")
def test_test_runner_actually_runs_on_the_selected_mac(tmp_path):
    # The test supplies a local executable, just as a project may supply its toolchain.
    path = tmp_path.resolve()
    (path / "marker").write_text("selected")
    runner = path / "go"
    runner.write_text("#!/bin/sh\n/bin/cat marker > result\n/bin/echo tested\n")
    runner.chmod(0o700)
    approvals = []
    workspace = Workspace(path, lambda *args: not approvals.append(args), "go", test_path=str(path))
    try:
        result = workspace.call("test_project", {})
        assert result["exit"] == 0 and (path / "result").read_text() == "selected"
        assert approvals == [("Run local tests", "go test ./...")]
    finally:
        workspace.close()


def test_slot_terminal_and_workspace_mcp_use_distinct_working_directories(tmp_path):
    import json
    import select
    import socket
    import subprocess
    import time

    from ccfleet_agent import workspace_io

    remote, local = tmp_path / "slot", tmp_path / "laptop"
    remote.mkdir()
    local.mkdir()
    agent = tmp_path / "fixture-agent"
    # Use the generated native CLI MCP configuration exactly as an MCP host would.
    agent.write_text(
        f"#!{sys.executable}\n"
        + """
import json, subprocess, sys
from pathlib import Path
cfg=json.loads(Path('.agents/mcp_config.json').read_text())['mcpServers']['ccfleet-local-project']
mcp=subprocess.Popen([cfg['command'],*cfg['args']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
def call(i,method,params):
    mcp.stdin.write(json.dumps({'jsonrpc':'2.0','id':i,'method':method,'params':params})+'\\n')
    mcp.stdin.flush()
    return json.loads(mcp.stdout.readline())['result']
assert call(1,'initialize',{'protocolVersion':'2025-11-25'})['capabilities']['tools']=={}
assert len(call(2,'tools/list',{})['tools'])>=4
assert not call(3,'tools/call',{'name':'save_project','arguments':{
    'path':'proof.txt','expected':'new','text':'saved on client'}}).get('isError')
assert not Path('proof.txt').exists()
print('CCFLEET-WORKSPACE-PASSED',flush=True)
mcp.terminate()
mcp.wait()
"""
    )
    agent.chmod(0o700)
    program = (
        "import socket,sys; from pathlib import Path; "
        "sys.path.insert(0,sys.argv[4]); from ccfleet_agent.antigravity_slot "
        "import session; session(socket.socket(fileno=int(sys.argv[1])),Path(sys.argv[2]),"
        "Path(sys.argv[3]),lambda:True)"
    )
    laptop, slot = socket.socketpair()
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            program,
            str(slot.fileno()),
            str(remote),
            str(agent),
            str(Path(__file__).resolve().parents[1]),
        ],
        pass_fds=(slot.fileno(),),
    )
    slot.close()
    ws = Workspace(local, lambda *_: True)
    terminal, pending, rpc = bytearray(), bytearray(), bytearray()
    try:
        end = time.monotonic() + 12
        while time.monotonic() < end and b"CCFLEET-WORKSPACE-PASSED" not in terminal:
            if not select.select([laptop], [], [], 0.35)[0]:
                continue
            chunk = laptop.recv(65536)
            if not chunk:
                break
            pending.extend(chunk)
            for kind, payload in workspace_io.unpack(pending):
                if kind == b"O":
                    terminal.extend(payload)
                elif kind == b"M":
                    rpc.extend(payload)
                    while b"\n" in rpc:
                        line, _, rest = rpc.partition(b"\n")
                        rpc = bytearray(rest)
                        answer = ws.rpc(json.loads(line))
                        if answer:
                            laptop.sendall(
                                workspace_io.frame(b"M", json.dumps(answer).encode() + b"\n")
                            )
        assert b"CCFLEET-WORKSPACE-PASSED" in terminal, bytes(terminal)
        assert (local / "proof.txt").read_text() == "saved on client"
        assert not (remote / "proof.txt").exists()
    finally:
        laptop.close()
        ws.close()
        process.wait(timeout=6)


@pytest.mark.skipif(sys.platform != "darwin", reason="uses the macOS test sandbox")
def test_project_test_runner_cannot_write_to_sibling_project(tmp_path):
    root = tmp_path.resolve() / "selected"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("original")
    runner = root / "go"
    runner.write_text("#!/bin/sh\n/bin/echo changed > ../outside.txt\n")
    runner.chmod(0o700)
    workspace = Workspace(root, lambda *_: True, "go", test_path=str(root))
    try:
        assert workspace.call("test_project", {})["exit"] != 0
        assert (tmp_path / "outside.txt").read_text() == "original"
    finally:
        workspace.close()
