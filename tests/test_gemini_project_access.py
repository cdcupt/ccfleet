"""Owner grants and the connecting Mac's project-scoped development tools."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ccfleet_agent import project_files
from ccfleet_agent import workspace_tools as tools

MAC = pytest.mark.skipif(sys.platform != "darwin", reason="exercises the macOS Seatbelt sandbox")


@pytest.fixture
def grant(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = root / "work tree"
    project.mkdir()
    return project_files.ProjectAccess(project, home / ".config/ccfleet")


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
        text=True, stderr=subprocess.PIPE,
    ).strip()


def test_durable_exact_checkout_grant_is_local_and_revocable(grant):
    access = grant
    assert not access.enabled() and access.remote() is None and not access.fleet_home.exists()
    remote = "https://gitlab.com/group/subgroup/project"
    access.grant(remote)
    assert access.enabled() and access.remote() == remote
    assert list(access.project.iterdir()) == []
    child = access.project / "child"
    child.mkdir()
    assert not project_files.ProjectAccess(child, access.fleet_home).enabled()
    second = project_files.ProjectAccess(access.project, access.fleet_home)
    assert second.enabled()
    record, = (access.fleet_home / "project-access").iterdir()
    assert record.stat().st_mode & 0o777 == 0o600
    assert record.parent.stat().st_mode & 0o777 == 0o700
    original = access.snapshot()
    second.revoke()
    assert not access.enabled()
    second.revoke()
    access.grant()
    assert access.enabled() and second.remote() is None and second.snapshot() != original


def test_directory_replacements_and_unsafe_registry_fail_closed(grant, tmp_path):
    access = grant
    access.grant()
    moved = tmp_path / "moved"
    access.project.rename(moved)
    access.project.mkdir()
    assert not access.enabled()
    access.project.rmdir()
    access.project.symlink_to(moved, target_is_directory=True)
    assert not access.enabled()
    access.project.unlink()
    moved.rename(access.project)
    assert access.enabled()
    record, = (access.fleet_home / "project-access").iterdir()
    saved = tmp_path / "outside-record"
    record.rename(saved)
    record.symlink_to(saved)
    assert not access.enabled()
    access.revoke()
    assert saved.is_file()
    os.link(saved, record)
    assert not access.enabled()
    record.unlink()
    record.write_bytes(saved.read_bytes())
    record.chmod(0o644)
    assert not access.enabled()
    record.unlink()
    os.mkfifo(record)
    assert not access.enabled()


def test_project_within_secrets_or_containing_controls_cannot_grant(grant):
    grant.revoke()
    roots = [Path("/"), Path.home(), grant.project, grant.project / ".ssh"]
    for folder in roots:
        if folder == grant.project:
            access = project_files.ProjectAccess(folder, folder / "control")
        else:
            if folder.name == ".ssh":
                folder.mkdir()
            access = project_files.ProjectAccess(folder, grant.fleet_home)
        assert not access.enabled()
        with pytest.raises(ValueError):
            access.grant()
    assert not grant.fleet_home.exists()


@pytest.mark.parametrize("remote", [
    "https://user:secret@github.com/a/b", "-o opt", "file:///tmp/r", "https://example.com/a/b",
    "https://github.com/g/../x", "https://gitlab.com/a/b?secret=x", "https://github.com/a/b\n",
])
def test_grant_rejects_urls_with_extra_authority(grant, remote):
    with pytest.raises(ValueError):
        grant.grant(remote)
    assert not grant.enabled()


def test_command_needs_persistent_grant_and_has_no_unsandboxed_fallback(grant, monkeypatch):
    with pytest.raises(ValueError, match="enable"):
        tools.run_local_command(grant, "pwd")
    grant.grant()
    monkeypatch.setattr(tools.sys, "platform", "linux")
    with pytest.raises(ValueError, match="macOS"):
        tools.run_local_command(grant, "pwd")
    for bad in ("", None, "nul\0", "x" * (tools.COMMAND_LIMIT + 1)):
        with pytest.raises(ValueError, match="command"):
            tools.run_local_command(grant, bad)
    with pytest.raises(ValueError, match="pinned"):
        with tools.export_branch_bundle(grant):
            pytest.fail("unpinned project opened a bundle")


@MAC
def test_real_shell_commits_and_exports_only_current_branch(grant, tmp_path):
    access = grant
    git(access.project, "init", "-q", "-b", "topic")
    git(access.project, "config", "user.name", "Local author")
    git(access.project, "config", "user.email", "test@example.invalid")
    access.grant("https://github.com/example/test")
    outcome = tools.run_local_command(
        access, "printf 'hello\n' > tracked && git add tracked && "
                "git commit -q -m First && git show -s --format=%s HEAD",
    )
    assert outcome == {"exit": 0, "output": "First\n"}
    git(access.project, "branch", "other")
    assert git(access.project, "show", "-s", "--format=%an", "HEAD") == "Local author"
    assert git(access.project, "status", "--porcelain") == ""
    with tools.export_branch_bundle(access) as (branch, path):
        assert branch == "topic" and path.is_file()
        assert git(tmp_path, "bundle", "list-heads", str(path)) == (
            git(access.project, "rev-parse", "HEAD") + " refs/heads/topic"
        )
        assert not path.is_relative_to(access.project)
    assert not path.exists()


@MAC
def test_sandbox_isolates_credentials_network_control_files_and_sibling(grant, tmp_path, monkeypatch):
    access = grant
    git(access.project, "init", "-q")
    secret = tmp_path.resolve() / "outside"
    secret.write_text("fixture private file")
    (access.project / "elsewhere").symlink_to(secret)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(secret))
    monkeypatch.setenv("GH_TOKEN", "sentinel")
    access.grant()
    src = access.project / ".git"
    before = (src / "config").read_bytes()
    registry, = (access.fleet_home / "project-access").iterdir()
    # Communicate only synthetic paths and results in this test workspace.
    checks = (
        'test -z "$SSH_AUTH_SOCK" && test -z "$GH_TOKEN"',
        '! cat elsewhere 2>/dev/null', '! printf changed > elsewhere',
        '! git config core.hooksPath here',
        '! touch .git/hooks/injected', '! touch .git/objects/info/alternates',
        f"! cat '{registry}'", f"! printf changed > '{registry}'",
        "printf 'ok\n'",
    )
    result = tools.run_local_command(access, " && ".join(checks))
    assert result["exit"] == 0 and result["output"].endswith("ok\n"), result
    assert access.enabled() and secret.read_text() == "fixture private file"
    assert (src / "config").read_bytes() == before
    assert not (src / "hooks/injected").exists()
    import socket
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        result = tools.run_local_command(
            access, f"/usr/bin/nc -w 1 -z 127.0.0.1 {listener.getsockname()[1]}",
        )
        assert result["exit"] != 0
        listener.settimeout(.1)
        with pytest.raises(socket.timeout):
            listener.accept()


@MAC
def test_output_timeout_and_revocation_interrupt_a_running_command(grant, monkeypatch):
    access = grant
    access.grant()
    result = tools.run_local_command(
        access, "/usr/bin/yes abc | /usr/bin/head -n 12000; printf 'LAST'\nexit 7",
    )
    assert result["exit"] == 7 and result["output"].endswith("LAST")
    assert "truncated" in result["output"] and len(result["output"].encode()) <= 32768
    monkeypatch.setattr(tools, "COMMAND_TIMEOUT", .2)
    with pytest.raises(ValueError, match="timed out"):
        tools.run_local_command(access, "sleep 15")
    monkeypatch.setattr(tools, "COMMAND_TIMEOUT", 20)
    started = access.project / "started"
    cancelled = threading.Event()
    def revoke():
        while not started.exists() and not cancelled.wait(.01):
            pass
        if started.exists():
            access.revoke()
    watcher = threading.Thread(target=revoke)
    watcher.start()
    try:
        start = time.monotonic()
        with pytest.raises(ValueError, match="interrupted"):
            tools.run_local_command(access, "touch started; sleep 10; touch late")
        assert time.monotonic() - start < 4
    finally:
        cancelled.set()
        watcher.join(2)
    assert not (access.project / "late").exists() and not access.enabled()


@MAC
def test_linked_worktree_has_local_files_but_no_access_to_sibling_gitdir(grant, tmp_path):
    main = tmp_path.resolve() / "primary"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "commit", "-q", "--allow-empty", "-m", "First")
    grant.project.rmdir()
    git(main, "worktree", "add", "-q", "-b", "topic", str(grant.project))
    head = git(main, "rev-parse", "topic")
    grant.grant()
    assert tools.run_local_command(grant, "printf hello > source")["exit"] == 0
    assert (grant.project / "source").read_text() == "hello"
    result = tools.run_local_command(grant, "git add source && git commit -qm Second")
    assert result["exit"] != 0 and git(main, "rev-parse", "topic") == head


@MAC
def test_background_children_end_with_command_and_toolchains_work(grant):
    grant.grant()
    # A backgrounded dev server must not outlive the turn when its pipes are closed.
    result = tools.run_local_command(grant, "(sleep 1; touch lingering) >/dev/null 2>&1 & echo ok")
    assert result == {"exit": 0, "output": "ok\n"}
    program = grant.project / "lifetime.py"
    program.write_text(
        "import os, subprocess\n"
        "print(subprocess.check_output(['/bin/echo', 'child']).decode().strip())\n"
        "for op in (lambda: os.setsid(), lambda: os.setpgid(0, 0)):\n"
        "    try: op()\n"
        "    except OSError: continue\n"
        "    raise AssertionError('detached process group')\n"
        "try: pid=os.posix_spawn('/usr/bin/true', ['true'], {}, setpgroup=0)\n"
        "except (OSError, NotImplementedError): pass\n"
        "else: os.waitpid(pid,0); raise AssertionError('spawned separate group')\n"
    )
    assert tools.run_local_command(grant, "python3 lifetime.py") == {"exit": 0, "output": "child\n"}
    time.sleep(1.1)
    assert not (grant.project / "lingering").exists()


@MAC
def test_npm_subcommands_run_in_project_sandbox_when_installed(grant):
    import shutil
    if not shutil.which("npm", path=tools.DEV_PATH):
        pytest.skip("npm unavailable")
    import json
    (grant.project / "package.json").write_text(json.dumps({
        "name": "ccfleet-project-check", "version": "1.0.0",
        "scripts": {"test": "node -e \"console.log('local dev test passed')\""},
    }))
    grant.grant()
    outcome = tools.run_local_command(grant, "npm --silent test")
    assert outcome["exit"] == 0 and "local dev test passed" in outcome["output"]


@MAC
def test_grant_works_in_both_directions_with_system_python(grant):
    from ccfleet_agent import project_files
    module = Path(project_files.__file__).resolve()
    program = (
        "import importlib.util,sys;from pathlib import Path;"
        "s=importlib.util.spec_from_file_location('signed_project_files',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "a=m.ProjectAccess(Path(sys.argv[2]),Path(sys.argv[3]));"
        "print(getattr(a,sys.argv[4])())"
    )
    def other(method):
        return subprocess.check_output(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", program, str(module),
             str(grant.project), str(grant.fleet_home), method],
            env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}, cwd="/", text=True,
        ).strip()
    grant.grant()
    assert other("enabled") == "True"
    other("revoke")
    assert not grant.enabled()
    other("grant")
    assert grant.enabled()
    grant.revoke()
    assert other("enabled") == "False"
