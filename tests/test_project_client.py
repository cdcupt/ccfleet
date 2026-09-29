"""Actual project client boundaries: no local agent, no host metadata, explicit writes."""

from __future__ import annotations

import base64
import hashlib
import json
import runpy
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from ccfleet_agent import project_access as access

ROOT = Path(__file__).resolve().parents[1]


def entry(data=b"hello", executable=False):
    return {"data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(),
            "executable": executable}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path.resolve() / "client"))
    return runpy.run_path(str(ROOT / "laptop/ccfleet"))


@pytest.fixture
def paired(client, tmp_path, monkeypatch):
    slot = tmp_path.resolve() / "slot"
    (slot / ".config/ccfleet").mkdir(parents=True)
    (slot / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"test-account"}}')
    (slot / ".config/ccfleet/slot-state.json").write_text(json.dumps({
        "bound_fp": hashlib.sha256(b"test-account").hexdigest()[:16]}))
    monkeypatch.setattr(access, "sessions", lambda _: [])
    project = tmp_path.resolve() / "PRIVATE-HOST-PROJECT"
    project.mkdir()
    (project / "main.py").write_text("print('shared')\n")
    device = {"slot_id": "slot1", "slot_name": "test-slot", "device_id": "device1",
              "host_alias": "ccfleet-slot1", "key": "/private/key", "known_hosts": "/private/hosts",
              "user": "slot01"}
    client["save_config"]({"devices": {"device1": device}, "active": "device1"})
    requests = []

    def rpc(_device, operation, **fields):
        request = {"version": 1, "operation": operation, **fields}
        requests.append(request)
        return {"version": 1, "ok": True,
                **access.handle(request, slot, policy=lambda: None)}

    scope = client["cmd_snapshot"].__globals__
    monkeypatch.setitem(scope, "project_rpc", rpc)
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    monkeypatch.setattr(client["shutil"], "which", lambda _: "/usr/bin/ssh")
    return project, slot, requests, calls, device


def launch(client, project, *flags):
    return client["main"](["snapshot", "--project", str(project), *flags])


def operation(client, project, action, *flags):
    return client["main"](["project", action, "--project", str(project), *flags])


def test_packaged_helper_digest_matches_release(client):
    assert hashlib.sha256((ROOT / "ccfleet_agent/project_files.py").read_bytes()).hexdigest() == client["PROJECT_FILES_SHA256"]
    assert client["project_files"]().MAX_FILES == 1000


def test_first_share_runs_only_slot_agent_and_does_not_export_host_metadata(client, paired, monkeypatch):
    project, slot, requests, calls, _ = paired
    (project / ".env").write_text("PRIVATE-ENV-CREDENTIAL")
    (project / ".ssh").mkdir()
    (project / ".ssh/id_ed25519").write_text("PRIVATE-SSH-CREDENTIAL")
    monkeypatch.setenv("PRIVATE_LOCAL_SENTINEL", "SECRET-HOST-METADATA")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://should-not-be-used.invalid")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/agent")
    monkeypatch.setenv("TZ", "Private/Timezone")
    assert launch(client, project, "--yes") == 0
    wire = json.dumps(requests)
    for sentinel in (str(project), "PRIVATE-HOST-PROJECT", "PRIVATE-ENV-CREDENTIAL",
                     "PRIVATE-SSH-CREDENTIAL", "SECRET-HOST-METADATA", "Private/Timezone"):
        assert sentinel not in wire
    assert [r["operation"] for r in requests] == ["status", "write"]
    assert set(requests[1]["files"]) == {"main.py"}
    assert len(calls) == 1
    cmd, options = calls[0]
    assert cmd[0] == "ssh" and "ccfleet-project-session-v1" in cmd
    assert "-F" in cmd and "/dev/null" in cmd
    assert not any(value.endswith("/claude") or value == "claude" for value in cmd)
    assert set(options["env"]) == {"PATH", "HOME", "TERM", "LANG", "LC_ALL", "TZ", "CCFLEET_HOME"}
    assert options["env"]["TZ"] == "UTC"
    assert "ANTHROPIC_BASE_URL" not in options["env"]
    identifier = requests[1]["project"]
    assert (slot / "workspace/projects" / identifier / "main.py").read_text() == "print('shared')\n"


def test_readiness_sends_no_project_files_and_launches_no_agent(client, paired):
    project, slot, requests, calls, _ = paired
    assert launch(client, project, "--check") == 0
    assert requests == [{"version": 1, "operation": "status"}]
    assert not (slot / "workspace").exists() and calls == []
    assert "projects" not in client["load_config"]()


def test_first_upload_requires_explicit_consent(client, paired, monkeypatch):
    project, slot, requests, calls, _ = paired
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert launch(client, project) == 2
    assert [r["operation"] for r in requests] == ["status"]
    assert calls == [] and not (slot / "workspace").exists()


def test_remote_edits_need_review_and_pull_with_backup(client, paired, monkeypatch):
    project, slot, requests, _, _ = paired
    assert launch(client, project, "--yes") == 0
    remote = slot / "workspace/projects" / requests[1]["project"]
    (remote / "main.py").write_text("print('remote edit')\n")
    assert operation(client, project, "diff") == 0
    assert (project / "main.py").read_text() == "print('shared')\n"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert operation(client, project, "pull") == 2
    assert (project / "main.py").read_text() == "print('shared')\n"
    assert operation(client, project, "pull", "--yes") == 0
    assert (project / "main.py").read_text() == "print('remote edit')\n"
    backups = list((client["home"]() / "project-backups").glob("*/*/main.py"))
    assert len(backups) == 1 and backups[0].read_text() == "print('shared')\n"


def test_conflicting_edits_preserve_both_versions(client, paired):
    project, slot, requests, _, _ = paired
    assert launch(client, project, "--yes") == 0
    remote = slot / "workspace/projects" / requests[1]["project"]
    (project / "main.py").write_text("local edit")
    (remote / "main.py").write_text("remote edit")
    assert operation(client, project, "pull", "--yes") == 2
    assert (project / "main.py").read_text() == "local edit"
    assert (remote / "main.py").read_text() == "remote edit"


def test_nonoverlapping_local_edits_survive_pull(client, paired):
    project, slot, requests, _, _ = paired
    assert launch(client, project, "--yes") == 0
    (project / "local-only.txt").write_text("local new file")
    remote = slot / "workspace/projects" / requests[1]["project"]
    (remote / "main.py").write_text("remote edit")
    assert operation(client, project, "pull", "--yes") == 0
    assert (project / "local-only.txt").read_text() == "local new file"
    assert (project / "main.py").read_text() == "remote edit"


def test_local_change_requires_push_before_new_work(client, paired):
    project, _, requests, calls, _ = paired
    assert launch(client, project, "--yes") == 0
    (project / "main.py").write_text("local edit")
    assert launch(client, project, "--yes") == 2
    assert len(calls) == 1
    assert [r["operation"] for r in requests].count("write") == 1
    assert operation(client, project, "push", "--yes") == 0
    assert launch(client, project) == 0


def test_another_computer_can_pull_an_existing_project_without_overwriting_local_files(client, paired, tmp_path):
    project, _, requests, _, _ = paired
    assert launch(client, project, "--yes") == 0
    identifier = requests[1]["project"]
    second = tmp_path.resolve() / "second"
    second.mkdir()
    assert operation(client, second, "pull", "--remote-project", identifier, "--yes") == 0
    assert (second / "main.py").read_text() == (project / "main.py").read_text()
    assert launch(client, second) == 0


@pytest.mark.parametrize("flags", [["--print", "hello"], ["--fork-session"],
                                   ["--remote-project", "../escape"], ["--name", "a;pwd"],
                                   ["--model", "bad;model"], ["--reconnect-for", "-1"]])
def test_invalid_or_retired_flags_fail_before_any_network(client, paired, flags):
    project, _, requests, calls, _ = paired
    assert launch(client, project, *flags) == 2
    assert requests == [] and calls == []


def test_home_and_filesystem_roots_cannot_be_shared(client):
    for path in (Path.home(), Path("/")):
        with pytest.raises(client["CliError"], match="project directory"):
            client["selected_project"](str(path))


def test_mismatched_helper_is_never_executed(client, tmp_path, monkeypatch):
    executable = tmp_path / "bin/ccfleet"
    executable.parent.mkdir()
    helper = executable.with_name("ccfleet-project-files-" + client["PROJECT_FILES_SHA256"] + ".py")
    helper.write_text("raise AssertionError('must never execute')")
    monkeypatch.setitem(client["project_files"].__globals__, "__file__", str(executable))
    with pytest.raises(client["CliError"], match="missing or mismatched"):
        client["project_files"]()


@pytest.mark.parametrize("answer", [{}, {"projects": [None]}, {"projects": [{}]},
                                    {"projects": [{"project": "a" * 32, "sessions": ["bad\n"]}]},
                                    {"projects": [{"project": "../bad", "sessions": []}]}])
def test_untrusted_project_listings_are_rejected(client, answer):
    with pytest.raises(client["CliError"]):
        client["validate_project_listing"](answer)


def test_project_rpc_uses_real_bounded_framing_without_a_shell(client, tmp_path, monkeypatch):
    fake = tmp_path / "fake.py"
    fake.write_text('''import sys,json,struct
r=sys.stdin.buffer
n=struct.unpack("!I",r.read(4))[0]
assert json.loads(r.read(n)) == {"version":1,"operation":"status"}
assert r.read() == b""
b=json.dumps({"version":1,"ok":True,"ready":True,"protocol":1}).encode()
sys.stdout.buffer.write(struct.pack("!I",len(b))+b)
''')
    monkeypatch.setitem(client["project_rpc"].__globals__, "ssh_transport_command",
                        lambda *a, **kw: [sys.executable, str(fake)])
    assert client["project_rpc"]({}, "status")["ready"] is True


@pytest.mark.parametrize("payload", [b"", struct.pack("!I", 40 * 1024 * 1024),
    struct.pack("!I", 2) + b"[]", struct.pack("!I", 5) + b"short",])
def test_broken_project_wire_fails_closed(client, tmp_path, monkeypatch, payload):
    fake = tmp_path / "fake.py"
    fake.write_text(f"import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write({payload!r})")
    monkeypatch.setitem(client["project_rpc"].__globals__, "ssh_transport_command",
                        lambda *a, **kw: [sys.executable, str(fake)])
    with pytest.raises(client["CliError"]):
        client["project_rpc"]({}, "status")


def test_reconnect_reattaches_same_project_and_never_replays_new(client, paired, monkeypatch):
    _, _, _, calls, device = paired
    clock = {"now": 0}

    def run(cmd, **kw):
        calls.append((cmd, kw))
        clock["now"] += 1000
        return 255 if len(calls) == 1 else 0

    monkeypatch.setattr(subprocess, "call", run)
    monkeypatch.setattr(client["time"], "monotonic", lambda: clock["now"])
    monkeypatch.setattr(client["time"], "sleep", lambda _: None)
    args = client["parser"]().parse_args(["snapshot", "--new", "--name", "work", "--mode", "manual"])
    assert client["attach_project"](device, "a" * 32, "work", args) == 0
    assert calls[0][0][-5:] == ["new", "work", "manual", "opus", "max"]
    assert calls[1][0][-5:] == ["open", "work", "manual", "opus", "max"]


def test_ssh_transport_ignores_local_sendenv_and_proxy_settings(client, paired):
    device = paired[-1]
    command = client["ssh_transport_command"](device, tty=False)
    assert command[command.index("-F") + 1] == "/dev/null"
    assert "ForwardAgent=no" in command and "ClearAllForwardings=yes" in command
    assert "StrictHostKeyChecking=yes" in command


@pytest.mark.parametrize("name", [".ssh", ".aws", ".config", ".claude", ".codex",
                                  ".gnupg", ".kube", "Library", ".local/share"])
def test_personal_credential_roots_cannot_be_misidentified_as_projects(client, tmp_path, monkeypatch, name):
    personal = tmp_path.resolve() / "personal"
    project = personal / name / "nested"
    project.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: personal)
    with pytest.raises(client["CliError"], match="credential or application"):
        client["selected_project"](str(project))


def test_remote_diff_escapes_terminal_and_unicode_spoofing_controls(client):
    result = client["safe_display"]("hello\x1b[31m\x9b31m\u202egpj.exe")
    assert "\x1b" not in result and "\x9b" not in result and "\u202e" not in result
    assert "\\x1b" in result and "\\u202e" in result


def test_helper_executes_verified_bytes_not_loader_bytecode(client, monkeypatch):
    def forbidden(*args):
        pytest.fail("loader re-opened helper after integrity check")
    import importlib.machinery
    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", forbidden)
    assert client["project_files"]().MAX_FILES == 1000


@pytest.mark.parametrize("body", [b'{"version":1,"ok":false,"ok":true,"ready":true,"protocol":1}',
                                   b'{"version":1,"ok":true,"ready":true,"protocol":1,"host":"bad"}',
                                   b'[' * 1500 + b']' * 1500])
def test_ambiguous_or_unrecognized_project_response_fails_closed(client, tmp_path, monkeypatch, body):
    payload = struct.pack("!I", len(body)) + body
    fake = tmp_path / "fake.py"
    fake.write_text(f"import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write({payload!r})")
    monkeypatch.setitem(client["project_rpc"].__globals__, "ssh_transport_command",
                        lambda *a, **kw: [sys.executable, str(fake)])
    with pytest.raises(client["CliError"]):
        client["project_rpc"]({}, "status")
