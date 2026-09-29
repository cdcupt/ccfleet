"""The slot project boundary never reads credentials or trusts client metadata."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pty
import stat
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import project_access as access

PROJECT = "a" * 32
OTHER = "b" * 32
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "slot"
    (home / ".config/ccfleet").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "account-a"}}))
    (home / ".config/ccfleet/slot-state.json").write_text(json.dumps({
        "bound_fp": hashlib.sha256(b"account-a").hexdigest()[:16]}))
    # A directory instead of a token file makes accidental credential reads fail.
    (home / ".claude/.credentials.json").mkdir(parents=True)
    return home


def file(data=b"hello\n", executable=False):
    return {"data": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest(), "executable": executable}


def call(home, operation="status", **values):
    request = {"version": 1, "operation": operation, **values}
    input_, output = io.BytesIO(), io.BytesIO()
    access.write_frame(input_, request)
    input_.seek(0)
    code = access.serve_one(input_, output, home, policy=lambda: None)
    output.seek(0)
    result = access.read_frame(output)
    assert output.read() == b""
    return code, result


def test_status_only_reports_readiness_and_never_requires_a_credential(home):
    assert call(home) == (0, {"version": 1, "ok": True, "ready": True, "protocol": 1})
    assert not (home / "workspace").exists()


def test_disabled_gate_rejects_before_consuming_client_data(home):
    stream = io.BytesIO(b"must not be read")
    output = io.BytesIO()

    def disabled():
        raise access.ProjectAccessError("disabled", "project access is not enabled for this slot")

    assert access.serve_one(stream, output, home, policy=disabled) == 2
    assert stream.tell() == 0
    output.seek(0)
    assert access.read_frame(output)["error"] == "disabled"


@pytest.mark.parametrize("parent_mode,parent_uid,marker_mode,marker_uid,nlink,allowed", [
    (stat.S_IFDIR | 0o755, 0, stat.S_IFREG | 0o644, 0, 1, True),
    (stat.S_IFDIR | 0o775, 0, stat.S_IFREG | 0o644, 0, 1, False),
    (stat.S_IFDIR | 0o755, 1, stat.S_IFREG | 0o644, 0, 1, False),
    (stat.S_IFDIR | 0o755, 0, stat.S_IFREG | 0o666, 0, 1, False),
    (stat.S_IFDIR | 0o755, 0, stat.S_IFREG | 0o644, 1, 1, False),
    (stat.S_IFLNK | 0o755, 0, stat.S_IFREG | 0o644, 0, 1, False),
    (stat.S_IFDIR | 0o755, 0, stat.S_IFLNK | 0o644, 0, 1, False),
    (stat.S_IFDIR | 0o755, 0, stat.S_IFREG | 0o644, 0, 2, False),
])
def test_only_regular_root_owned_policies_enable_access(monkeypatch, parent_mode, parent_uid,
                                                       marker_mode, marker_uid, nlink, allowed):
    directory = Path("/policy")

    def lstat(path):
        mode, uid = (parent_mode, parent_uid) if path == directory else (marker_mode, marker_uid)
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=nlink)

    monkeypatch.setattr(Path, "lstat", lstat)
    if allowed:
        access.require_enabled(directory)
    else:
        with pytest.raises(access.ProjectAccessError, match="policy"):
            access.require_enabled(directory)


def test_missing_policy_blocks_access(tmp_path):
    with pytest.raises(access.ProjectAccessError, match="not enabled"):
        access.require_enabled(tmp_path / "missing")


@pytest.mark.parametrize("change", ["different", "restart", "missing", "symlink"])
def test_account_binding_failures_are_closed(home, change):
    state = home / ".config/ccfleet/slot-state.json"
    if change == "different":
        state.write_text('{"bound_fp":"other"}')
    elif change == "restart":
        data = json.loads(state.read_text())
        state.write_text(json.dumps({**data, "account_restart": True}))
    elif change == "missing":
        (home / ".claude.json").write_text("{}")
    else:
        state.rename(state.with_suffix(".old"))
        state.symlink_to(state.with_suffix(".old"))
    code, result = call(home)
    assert code == 2 and result["error"] == "account_unavailable"


@pytest.mark.parametrize("raw", [b"", b"\0\0\0\0", struct.pack("!I", access.MAX_FRAME + 1),
                                  b"\0\0\0\x02[", b"\0\0\0\x02[]", b"\0\0\0\x01\xff"])
def test_invalid_frames(raw):
    with pytest.raises(access.ProjectAccessError):
        access.read_frame(io.BytesIO(raw))


def test_duplicate_fields_are_rejected():
    raw = b'{"version":1,"version":1}'
    with pytest.raises(access.ProjectAccessError, match="JSON"):
        access.read_frame(io.BytesIO(struct.pack("!I", len(raw)) + raw))


@pytest.mark.parametrize("payload", [
    {"version": True, "operation": "status"},
    {"version": 1, "operation": "status", "environment": {"HOME": "/Users/private"}},
    {"version": 1, "operation": "status", "fingerprint": "client-private"},
    {"version": 1, "operation": "read", "project": "../secrets"},
    {"version": 1, "operation": "read", "project": "A" * 32},
    {"version": 1, "operation": "run", "command": "id"},
    {"version": 1, "operation": ["status"]},
])
def test_client_identity_arbitrary_paths_and_commands_are_not_protocol_fields(payload):
    with pytest.raises(access.ProjectAccessError):
        access.validate_request(payload)


def test_read_missing_project_is_empty_and_does_not_create_workspace(home):
    code, result = call(home, "read", project=PROJECT)
    assert code == 0 and result["files"] == result["manifest"] == {}
    assert not (home / "workspace").exists()


def test_write_read_conflict_and_backup_roundtrip(home, monkeypatch):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    code, written = call(home, "write", project=PROJECT,
                         files={"src/main.txt": file()}, expected={})
    assert code == 0 and written["files"] == {"src/main.txt": file()}
    root = home / "workspace/projects" / PROJECT
    assert (root / "src/main.txt").read_bytes() == b"hello\n"
    assert call(home, "read", project=PROJECT)[1] == written
    code, changed = call(home, "write", project=PROJECT, files={"src/main.txt": file(b"updated")},
                         expected=written["manifest"])
    assert code == 0 and changed["files"]["src/main.txt"] == file(b"updated")
    backups = home / ".config/ccfleet/project-backups" / PROJECT
    assert any(path.read_bytes() == b"hello\n" for path in backups.rglob("main.txt"))
    code, stale = call(home, "write", project=PROJECT, files={}, expected=written["manifest"])
    assert code == 2 and stale["error"] == "conflict"
    assert (root / "src/main.txt").read_bytes() == b"updated"


def test_lost_write_response_can_be_retried_without_another_backup(home, monkeypatch):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    payload = {"project": PROJECT, "files": {"probe": file()}, "expected": {}}
    assert call(home, "write", **payload)[0] == 0
    backups = home / ".config/ccfleet/project-backups" / PROJECT
    before = sorted(backups.iterdir())
    assert call(home, "write", **payload)[0] == 0
    assert sorted(backups.iterdir()) == before


def test_unreviewed_remote_only_file_blocks_all_changes(home, monkeypatch):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    root = access.project_root(home, PROJECT, create=True)
    (root / "unknown.txt").write_bytes(b"retain remote work")
    code, result = call(home, "write", project=PROJECT, files={"probe": file()}, expected={})
    assert code == 2 and result["error"] == "conflict"
    assert not (root / "probe").exists()
    assert (root / "unknown.txt").read_bytes() == b"retain remote work"


def test_read_requires_two_consistent_snapshots(home, monkeypatch):
    access.project_root(home, PROJECT, create=True)
    views = iter([{"probe": file(b"before")}, {"probe": file(b"after")}])
    monkeypatch.setattr(access.project_files, "snapshot", lambda _, **kw: next(views))
    code, result = call(home, "read", project=PROJECT)
    assert code == 2 and result["error"] == "busy"
    assert "files" not in result


def test_account_change_after_snapshot_prevents_file_disclosure(home, monkeypatch):
    root = access.project_root(home, PROJECT, create=True)
    (root / "probe").write_bytes(b"account private content")
    accounts = iter(["original", "original", "changed"])
    monkeypatch.setattr(access, "bound_account", lambda _: next(accounts))
    code, result = call(home, "read", project=PROJECT)
    assert code == 2 and result["error"] == "account_unavailable"
    assert "account private content" not in json.dumps(result)


@pytest.mark.parametrize("prefix", ["workspace", "workspace/projects", f"workspace/projects/{PROJECT}"])
def test_symlinked_project_ancestors_never_write_outside_workspace(home, tmp_path, monkeypatch, prefix):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    target = tmp_path / "outside"
    target.mkdir()
    link = home / prefix
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    code, response = call(home, "write", project=PROJECT, files={"probe": file()}, expected={})
    assert code == 2 and response["error"] == "invalid"
    assert list(target.iterdir()) == []


def test_project_writes_are_blocked_until_every_matching_session_exits(home, monkeypatch):
    monkeypatch.setattr(access, "sessions", lambda _: [f"p_{PROJECT}_main"])
    code, response = call(home, "write", project=PROJECT, files={"probe": file()}, expected={})
    assert code == 2 and response["error"] == "busy"
    assert not (home / "workspace/projects" / PROJECT / "probe").exists()
    monkeypatch.setattr(access, "sessions", lambda _: [f"p_{OTHER}_main", "ccfleet"])
    assert call(home, "write", project=PROJECT, files={"probe": file()}, expected={})[0] == 0


def test_project_operations_hold_a_shared_external_lock(home):
    with access.project_lock(home, PROJECT):
        code, result = call(home, "read", project=PROJECT)
    assert code == 2 and result["error"] == "busy"


def test_project_listing_contains_only_opaque_ids_and_valid_session_names(home, monkeypatch):
    access.project_root(home, PROJECT, create=True)
    access.project_root(home, OTHER, create=True)
    (home / "workspace/projects/private-name").mkdir()
    (home / "workspace/projects" / ("c" * 32)).symlink_to(home, target_is_directory=True)
    monkeypatch.setattr(access, "sessions", lambda _: [f"p_{PROJECT}_work", f"p_{PROJECT}_main",
                                                       f"p_{PROJECT}_invalid.name", "ccfleet"])
    assert call(home, "list")[1]["projects"] == [
        {"project": PROJECT, "sessions": ["main", "work"]}, {"project": OTHER, "sessions": []}]


def test_empty_project_listing(home):
    assert call(home, "list")[1]["projects"] == []


@pytest.mark.parametrize("stderr,allowed", [
    (b"no server running on /tmp/tmux-1000/default", True),
    (b"error connecting to /tmp/tmux-1000/default (No such file or directory)", True),
    (b"error connecting to /tmp/tmux-1000/default (Permission denied)", False),
    (b"unexpected internal problem", False),
])
def test_tmux_failures_cannot_silently_allow_concurrent_writes(home, monkeypatch, stderr, allowed):
    monkeypatch.setattr(access.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout=b"", stderr=stderr))
    if allowed:
        assert access.sessions(home) == []
    else:
        with pytest.raises(access.ProjectAccessError):
            access.sessions(home)


def test_session_scan_uses_clean_environment(home, monkeypatch):
    monkeypatch.setenv("PRIVATE_CLIENT_SENTINEL", "must-not-forward")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"p_{PROJECT}_main\n".encode(), stderr=b"")

    monkeypatch.setattr(access.subprocess, "run", run)
    assert access.sessions(home) == [f"p_{PROJECT}_main"]
    assert calls[0][1]["env"] == access.slot_environment(home)
    assert "PRIVATE_CLIENT_SENTINEL" not in calls[0][1]["env"]


class Executed(Exception):
    pass


def test_native_claude_is_execed_with_only_slot_environment_and_cwd(home, monkeypatch):
    root = access.project_root(home, PROJECT, create=True)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unexpected.example")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "private-token")
    monkeypatch.setenv("HOME", "/Users/private")
    monkeypatch.setenv("TZ", "Private/Timezone")
    monkeypatch.setattr(access, "require_enabled", lambda: None)
    captured = {}
    monkeypatch.setattr(access.os, "chdir", lambda path: captured.update(cwd=path))
    monkeypatch.setattr(access.os, "umask", lambda mode: None)

    def execute(path, argv, env):
        captured.update(path=path, argv=argv, env=env)
        raise Executed

    monkeypatch.setattr(access.os, "execve", execute)
    with pytest.raises(Executed):
        access.run_claude(home, PROJECT, "manual", "opus", "high")
    assert captured["cwd"] == root
    assert captured["argv"] == [str(home / ".local/bin/claude"), "--permission-mode", "manual",
                                "--model", "opus", "--effort", "high"]
    assert captured["env"] == access.slot_environment(home)
    assert set(captured["env"]) == {"HOME", "USER", "LOGNAME", "PATH", "LANG", "TZ", "TERM"}
    assert captured["env"]["HOME"] == str(home) and captured["env"]["TZ"] == "UTC"


def test_new_session_uses_fixed_runner_then_exact_tmux_attach(home, monkeypatch):
    root = access.project_root(home, PROJECT, create=True)
    monkeypatch.setattr(access, "require_enabled", lambda: None)
    monkeypatch.setattr(access, "sessions", lambda _: [])
    calls = []
    monkeypatch.setattr(access.subprocess, "run", lambda argv, **kw: (
        calls.append((argv, kw)) or SimpleNamespace(returncode=0)))

    def execute(path, argv, env):
        calls.append((argv, {"env": env}))
        raise Executed

    monkeypatch.setattr(access.os, "execve", execute)
    with pytest.raises(Executed):
        access.open_session(home, PROJECT, "new", "work", "bypassPermissions", "default", "max")
    target = f"p_{PROJECT}_work"
    assert calls[0][0] == [access.TMUX, "new-session", "-d", "-s", target, "-c", str(root),
                           access.PYTHON, "-I", str(Path(access.__file__).resolve()), "_run", PROJECT,
                           "bypassPermissions", "default", "max"]
    assert calls[1][0] == [access.TMUX, "attach-session", "-t", "=" + target]
    assert all(kwargs["env"] == access.slot_environment(home) for _, kwargs in calls)


def test_open_reuses_existing_session_without_overriding_model(home, monkeypatch):
    access.project_root(home, PROJECT, create=True)
    monkeypatch.setattr(access, "require_enabled", lambda: None)
    monkeypatch.setattr(access, "sessions", lambda _: [f"p_{PROJECT}_work"])
    monkeypatch.setattr(access.subprocess, "run", lambda *a, **kw: pytest.fail("must not restart"))
    monkeypatch.setattr(access.os, "execve", lambda *a: (_ for _ in ()).throw(Executed()))
    with pytest.raises(Executed):
        access.open_session(home, PROJECT, "open", "work", "manual", "opus", "high")
    with pytest.raises(access.ProjectAccessError, match="already exists"):
        access.open_session(home, PROJECT, "new", "work", "manual", "opus", "high")


@pytest.mark.parametrize("changes", [
    {"project": "../outside"}, {"action": "restart"}, {"name": "bad;command"},
    {"name": "long" * 9}, {"mode": "--system-prompt"}, {"model": "opus;id"},
    {"effort": "high\nid"},
])
def test_invalid_session_values_are_rejected(changes):
    values = dict(project=PROJECT, action="open", name="main", mode="manual", model="opus", effort="high")
    with pytest.raises(access.ProjectAccessError):
        access.validate_session(**{**values, **changes})


@pytest.mark.parametrize("command,expected", [
    ("ccfleet-project-v1", "entered_project"),
    ("ccfleet-relay-v1", ""),
    ("ccfleet-project-v1 extra", ""),
    ("ccfleet-project-v1\nid", ""),
    ("ccfleet-project-v1; touch unexpected", ""),
])
def test_forced_command_only_dispatches_exact_nonpty_project_protocol(tmp_path, command, expected):
    entry = tmp_path / "slot-entry.sh"
    entry.write_text((ROOT / "node/slot-entry.sh").read_text())
    package = tmp_path / "ccfleet_agent"
    package.mkdir()
    (package / "project_access.py").write_text('print("entered_project")\n')
    (package / "local_relay.py").write_text('raise RuntimeError("retired relay must not run")\n')
    result = subprocess.run(["bash", str(entry)], env={**os.environ, "SSH_ORIGINAL_COMMAND": command},
                            capture_output=True, text=True)
    assert result.stdout.strip() == expected
    assert (result.returncode == 0) == bool(expected)
    if command == "ccfleet-relay-v1":
        assert "retired" in result.stderr


def test_machine_installer_includes_both_project_modules():
    text = (ROOT / "node/machine-setup.sh").read_text()
    assert "fetch ccfleet_agent/project_access.py" in text
    assert "fetch ccfleet_agent/project_files.py" in text


@pytest.mark.parametrize("command,accepted", [
    (f"ccfleet-project-session-v1 {PROJECT} new work manual opus high", True),
    (f"ccfleet-project-session-v1 {PROJECT} new work manual opus high extra", False),
    (f"ccfleet-project-session-v1 {PROJECT} new work manual opus high\nid", False),
    (f"ccfleet-project-session-v1 {PROJECT} new work manual opus", False),
])
def test_terminal_project_entry_passes_only_fixed_separate_arguments(tmp_path, command, accepted):
    entry = tmp_path / "slot-entry.sh"
    entry.write_text((ROOT / "node/slot-entry.sh").read_text())
    package = tmp_path / "ccfleet_agent"
    package.mkdir()
    log = tmp_path / "args.json"
    (package / "project_access.py").write_text(
        f"import json,sys\nopen({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    master, slave = pty.openpty()
    try:
        result = subprocess.run(["bash", str(entry)], stdin=slave, stdout=slave,
                                stderr=subprocess.PIPE,
                                env={**os.environ, "SSH_ORIGINAL_COMMAND": command})
    finally:
        os.close(slave)
        os.close(master)
    assert (result.returncode == 0) == accepted
    if accepted:
        assert json.loads(log.read_text()) == ["session", PROJECT, "new", "work", "manual",
                                               "opus", "high"]
    else:
        assert not log.exists()


def test_script_import_works_under_python_isolated_mode():
    result = subprocess.run(["/usr/bin/python3", "-I", str(Path(access.__file__)), "invalid"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert result.stderr.strip() == "unsupported project entry point"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_unsafe_lock_is_refused_without_reading_project(home, kind):
    directory = home / ".config/ccfleet/project-locks"
    directory.mkdir(mode=0o700)
    lock = directory / PROJECT
    if kind == "symlink":
        lock.symlink_to(home / ".claude.json")
    elif kind == "hardlink":
        os.link(home / ".claude.json", lock)
    else:
        lock.write_text("")
        lock.chmod(0o644)
    assert call(home, "read", project=PROJECT)[0] == 2


def test_account_switch_while_locking_never_applies_files(home, monkeypatch):
    calls = iter(["first", "second"])
    monkeypatch.setattr(access, "bound_account", lambda _: next(calls))
    code, result = call(home, "write", project=PROJECT, files={"file": file()}, expected={})
    assert code == 2 and result["error"] == "account_unavailable"
    assert not (home / "workspace").exists()


def test_internal_errors_are_generic_and_do_not_leak_private_paths(home, monkeypatch):
    monkeypatch.setattr(access, "handle", lambda *a, **kw: (_ for _ in ()).throw(
        OSError("/Users/private-owner/secret-internal")))
    code, result = call(home)
    assert code == 2 and result["error"] == "internal"
    assert "private-owner" not in json.dumps(result)


def test_launcher_uses_unix_account_home_not_client_environment(home, monkeypatch):
    monkeypatch.setenv("HOME", "/Users/private-client")
    monkeypatch.setattr(access.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(home)))
    captured = []
    monkeypatch.setattr(access.signal, "alarm", lambda _: None)
    monkeypatch.setattr(access, "serve_one", lambda a, b, selected: captured.append(selected) or 0)
    assert access.main([]) == 0
    assert captured == [home]


def test_terminal_guard_and_invalid_internal_entry_return_safe_errors(monkeypatch, capsys):
    monkeypatch.setattr(access.sys.stdin, "isatty", lambda: False)
    assert access.main(["session", PROJECT, "open", "work", "manual", "opus", "high"]) == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert access.main(["unexpected"]) == 2
    assert "unsupported project entry point" in capsys.readouterr().err


def test_native_default_bypass_argv_is_explicit(home):
    assert access.claude_command(home, "bypassPermissions", "default", "default") == [
        str(home / ".local/bin/claude"), "--dangerously-skip-permissions"]


def test_remote_gitignore_excludes_generated_files_but_keeps_explicit_selections(home, monkeypatch):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    files = {".gitignore": file(b"scratch/\n*.cache\n"), "main.py": file(b"pass\n"),
             "scratch/tracked.txt": file(b"explicitly selected\n")}
    code, written = call(home, "write", project=PROJECT, files=files, expected={})
    assert code == 0 and written["files"] == files
    root = home / "workspace/projects" / PROJECT
    assert not (root / ".git").exists()
    (root / "scratch/generated.txt").write_text("private ignored generated content")
    (root / "generated.cache").write_text("do not transfer")
    code, received = call(home, "read", project=PROJECT)
    assert code == 0 and received["files"] == files
    updated = {**files, "main.py": file(b"print('updated')\n")}
    code, response = call(home, "write", project=PROJECT, files=updated,
                          expected=written["manifest"])
    assert code == 0 and response["files"] == updated
    assert (root / "scratch/generated.txt").read_text() == "private ignored generated content"
    assert (root / "generated.cache").read_text() == "do not transfer"
    assert not (root / ".git").exists()


def test_selection_record_is_private_outside_project_and_only_relative_file_state(home, monkeypatch):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    code, response = call(home, "write", project=PROJECT, files={"probe.txt": file()}, expected={})
    assert code == 0
    path = home / ".config/ccfleet/project-manifests" / (PROJECT + ".json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == response["manifest"]
    assert str(home) not in path.read_text()
    assert sorted((home / "workspace/projects" / PROJECT).iterdir()) == [
        home / "workspace/projects" / PROJECT / "probe.txt"]


@pytest.mark.parametrize("data", ["not json", "[]", '{"../outside":{}}',
                                 '{"probe":{"sha256":"bad","executable":true}}',
                                 '{"probe":{},"probe":{}}'])
def test_corrupted_selection_record_fails_closed_without_project_changes(home, monkeypatch, data):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    directory = home / ".config/ccfleet/project-manifests"
    directory.mkdir(mode=0o700)
    record = directory / (PROJECT + ".json")
    record.write_text(data)
    record.chmod(0o600)
    code, result = call(home, "write", project=PROJECT, files={"probe": file()}, expected={})
    assert code == 2 and result["error"] == "invalid"
    assert not (home / "workspace").exists()


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "public", "hardlink", "oversized"])
def test_unsafe_selection_record_fails_closed(home, monkeypatch, kind):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    directory = home / ".config/ccfleet/project-manifests"
    record = directory / (PROJECT + ".json")
    if kind == "directory_symlink":
        target = home / "outside-selection"
        target.mkdir(mode=0o700)
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        if kind == "symlink":
            record.symlink_to(home / ".claude.json")
        elif kind == "hardlink":
            os.link(home / ".claude.json", record)
        else:
            record.write_text("{}" if kind == "public" else " " * (access.MAX_RECORD + 1))
            record.chmod(0o644 if kind == "public" else 0o600)
    assert call(home, "write", project=PROJECT, files={"probe": file()}, expected={})[0] == 2
    assert not (home / "workspace").exists()


def test_write_ahead_selection_keeps_ignored_partial_apply_recoverable(home, monkeypatch):
    monkeypatch.setattr(access, "sessions", lambda _: [])
    original = {".gitignore": file(b"scratch/\n")}
    code, first = call(home, "write", project=PROJECT, files=original, expected={})
    assert code == 0
    desired = {**original, "scratch/work.txt": file(b"important partial work")}
    root = home / "workspace/projects" / PROJECT

    def partial_apply(*args):
        (root / "scratch").mkdir()
        (root / "scratch/work.txt").write_bytes(b"important partial work")
        raise OSError("simulated storage failure")

    monkeypatch.setattr(access.project_files, "apply_snapshot", partial_apply)
    code, failed = call(home, "write", project=PROJECT, files=desired, expected=first["manifest"])
    assert code == 2 and failed["error"] == "internal"
    record = access.read_manifest(home, PROJECT)
    assert set(record) == set(desired)
    code, recovered = call(home, "read", project=PROJECT)
    assert code == 0 and recovered["files"] == desired
    # A lost/failed reply can be acknowledged idempotently without re-applying.
    code, retry = call(home, "write", project=PROJECT, files=desired, expected=first["manifest"])
    assert code == 0 and retry["files"] == desired
