"""One local home grant authorizes selected development folders across harnesses."""

import concurrent.futures
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ccfleet_agent import project_files
from ccfleet_agent import workspace_tools as tools


@pytest.fixture
def owner(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "owner"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(project_files, "_account_home", lambda: home)
    return project_files.ProjectAccess(home, home / ".config/ccfleet")


def access(owner, path):
    return project_files.ProjectAccess(path, owner.fleet_home)


def git(project, *args):
    subprocess.run(["git", "-c", "user.name=QA", "-c", "user.email=dev@example.invalid",
                    "-C", str(project), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})


def checkout(owner, name):
    path = owner.project / name
    path.mkdir(mode=0o700, parents=True)
    git(path, "init", "-q", "-b", "main")
    return access(owner, path)


def test_one_owner_grant_for_separate_projects_with_no_implicit_remote(owner):
    first, second = checkout(owner, "src/alpha"), checkout(owner, "somewhere else/beta")
    git(first.project, "remote", "add", "origin", "https://github.com/example/alpha")
    assert first.eligible_checkout() and second.eligible_checkout()
    assert owner.is_home() and not first.is_home()
    assert not owner.all_enabled() and not owner.navigation()
    assert not first.enabled() and not second.enabled()
    assert not owner.fleet_home.exists()
    owner.enable_all()
    assert owner.navigation() and owner.all_enabled()
    assert not owner.enabled() and not owner.eligible_checkout()  # Never pass ~ to the shell.
    assert first.enabled() and second.enabled()
    assert first.remote() is None and second.remote() is None
    assert first.snapshot()[2] != second.snapshot()[2]
    with pytest.raises(ValueError, match="pinned"):
        with tools.export_branch_bundle(first):
            pytest.fail("inherited grant supplied a publishing destination")
    record, = (owner.fleet_home / "project-access").iterdir()
    assert name_and_mode(record) == ("all-projects.json", 0o600)
    owner.disable_all()
    assert not owner.navigation() and not owner.all_enabled() and not first.enabled()
    owner.disable_all()


def name_and_mode(file):
    return file.name, file.stat().st_mode & 0o777


def test_home_must_be_account_identity_not_environment_value(owner, monkeypatch):
    fake = owner.project / "some checkout"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    misplaced = access(owner, fake)
    assert not misplaced.is_home()
    for toggle in (misplaced.enable_all, misplaced.disable_all):
        with pytest.raises(ValueError, match="home directory"):
            toggle()
    owner.enable_all()
    assert owner.navigation() and not misplaced.navigation()
    owner.project.rename(owner.project.with_name("previous-home"))
    owner.project.mkdir()
    new_registry = owner.fleet_home / "project-access"
    new_registry.mkdir(mode=0o700, parents=True)
    import shutil
    shutil.copy2(owner.project.with_name("previous-home") / ".config/ccfleet/project-access"
                 / "all-projects.json", new_registry / "all-projects.json")
    assert not owner.all_enabled()


@pytest.mark.parametrize(("name", "example"), [
    ("package.json", '{"name":"local-test"}'), ("pyproject.toml", "[project]\nname='x'"),
    ("go.mod", "module example.test/x"), ("Cargo.toml", "[package]\nname='a'"),
    ("Makefile", "all:\n\t@true"),
])
def test_owned_non_git_development_manifest_works(owner, name, example):
    path = owner.project / "work" / "my project"
    path.mkdir(parents=True)
    (path / name).write_text(example)
    owner.enable_all()
    a = access(owner, path)
    assert a.eligible_checkout() and a.enabled()
    (path / name).unlink()
    assert not a.enabled()
    (path / name).touch()
    assert not a.eligible_checkout()
    (path / name).unlink()
    other = owner.project / "data"
    other.write_text(example)
    (path / name).symlink_to(other)
    assert not a.enabled()


def test_writable_or_unsafe_git_markers_never_fall_back_to_another_manifest(owner):
    a = checkout(owner, "alpha")
    owner.enable_all()
    (a.project / "package.json").write_text("{}")
    marker = a.project / ".git"
    original = owner.project / "elsewhere"
    marker.rename(original)
    marker.symlink_to(original, target_is_directory=True)
    assert not a.eligible_checkout() and not a.enabled()
    marker.unlink()
    marker.mkdir()
    assert not a.enabled()
    marker.rmdir()
    original.rename(marker)
    marker.chmod(0o777)
    assert not a.enabled()
    marker.chmod(0o700)
    assert a.enabled()
    a.project.chmod(0o777)
    assert not a.enabled()


def test_home_owned_eligible_repos_only(owner):
    owner.enable_all()
    for name in ("Documents", "Desktop", "bin/tools", "Library/Agents", "Applications/Tool",
                 ".local/share/tools", "go/pkg/mod/github.com/example", "src/X.app/Contents",
                 "work/.password-store/test", ".ssh/repo", ".config/ccfleet/checkout"):
        a = checkout(owner, name)
        assert not a.eligible_checkout() and not a.enabled(), name
    unrelated = owner.project.parent / "outside"
    unrelated.mkdir()
    git(unrelated, "init", "-q")
    assert not access(owner, unrelated).enabled()
    source = checkout(owner, "src/repo")
    alias = owner.project / "alias"
    alias.symlink_to(source.project, target_is_directory=True)
    assert not access(owner, alias).enabled()


def test_linked_worktree_requires_owned_reciprocal_checkout_metadata(owner):
    a = checkout(owner, "main")
    git(a.project, "commit", "--allow-empty", "-qm", "Initial")
    path = owner.project / "worktrees" / "topic"
    path.parent.mkdir()
    git(a.project, "worktree", "add", "-q", "-b", "topic", str(path))
    link = access(owner, path)
    owner.enable_all()
    assert link.eligible_checkout() and link.enabled() and link.remote() is None
    marker = path / ".git"
    metadata = Path(marker.read_text().strip().removeprefix("gitdir: "))
    (metadata / "gitdir").write_text(str(a.project / ".git") + "\n")
    assert not link.enabled()
    marker.write_text("gitdir: " + str(owner.fleet_home) + "\n")
    assert not link.enabled()


def test_replacement_local_exclusion_global_epochs_and_exact_grants(owner):
    a, b = checkout(owner, "repo-a"), checkout(owner, "repo-b")
    published = "https://github.com/team/explicit"
    a.grant(published)
    original = a.snapshot()
    assert a.remote() == published and original is not None
    owner.enable_all()
    combined, inherited = a.snapshot(), b.snapshot()
    assert combined != original and a.remote() == published
    assert inherited is not None and b.remote() is None
    owner.disable_all()
    assert a.snapshot() == original and b.snapshot() is None
    owner.enable_all()
    assert a.snapshot() != combined and b.snapshot() != inherited
    a.revoke()
    assert not a.enabled() and not a.remote() and b.enabled()
    b.revoke()
    assert not b.enabled()
    a.grant(published)
    assert a.enabled() and a.remote() == published and not b.enabled()
    owner.disable_all()
    assert a.enabled() and not b.enabled()
    owner.enable_all()
    assert b.enabled() and b.remote() is None
    before = b.snapshot()
    b.project.rename(owner.project / "retired-repo")
    checkout(owner, "repo-b")
    assert b.enabled() and b.snapshot() != before


def test_foreign_or_corrupt_registry_entries_fail_closed(owner):
    a = checkout(owner, "repo")
    owner.enable_all()
    record = owner.fleet_home / "project-access" / "all-projects.json"
    data = record.read_bytes()
    record.chmod(0o644)
    assert not owner.all_enabled() and not a.enabled()
    record.chmod(0o600)
    record.write_bytes(b'{')
    assert not a.enabled()
    record.write_bytes(data)
    copy = record.with_name("original")
    record.rename(copy)
    record.symlink_to(copy)
    assert not owner.all_enabled()
    owner.disable_all()
    assert copy.read_bytes() == data and not record.exists()
    owner.enable_all()
    marker = record.with_name(a._file)
    marker.write_text("{}")
    marker.chmod(0o600)
    assert not a.enabled()


def test_concurrent_enables_preserve_the_epoch_and_checkout_exclusions(owner):
    a = checkout(owner, "repo")
    owner.enable_all()
    captured = a.snapshot()
    assert captured is not None
    def enable(_):
        owner.enable_all()
        return a.snapshot()
    with concurrent.futures.ThreadPoolExecutor(3) as workers:
        snapshots = list(workers.map(enable, range(6)))
    assert snapshots == [captured] * 6
    a.revoke()
    owner.enable_all()
    assert not a.enabled()
    owner.disable_all()
    owner.enable_all()
    assert a.enabled() and a.snapshot() != captured


@pytest.mark.skipif(sys.platform != "darwin", reason="real local macOS sandbox")
def test_mac_grant_runs_commands_in_two_checkouts_and_revokes_process(owner):
    a, b = checkout(owner, "code/A"), checkout(owner, "code/B")
    owner.enable_all()
    assert tools.run_local_command(a, "printf alpha > note && cat note")["output"] == "alpha"
    assert not (b.project / "note").exists()
    assert tools.run_local_command(b, "printf beta > note && cat note")["output"] == "beta"
    with pytest.raises(ValueError):
        tools.run_local_command(owner, "pwd")
    started = b.project / "started"
    finished = threading.Event()
    def revoke():
        while not started.exists() and not finished.wait(0.02):
            pass
        if started.exists():
            owner.disable_all()
    worker = threading.Thread(target=revoke)
    worker.start()
    try:
        start = time.monotonic()
        with pytest.raises(ValueError, match="interrupt"):
            tools.run_local_command(b, "touch started; sleep 12; touch late")
        assert time.monotonic() - start < 5
    finally:
        finished.set()
        worker.join(timeout=2)
    assert not a.enabled() and not b.enabled() and not (b.project / "late").exists()


def test_global_grant_ignores_another_checkout_pin_after_reclone(owner):
    a = checkout(owner, "src/application")
    a.grant("https://github.com/team/application")
    owner.enable_all()
    before = a.snapshot()
    a.project.rename(owner.project / "old")
    checkout(owner, "src/application")
    assert a.snapshot() is not None and a.snapshot() != before
    assert a.remote() is None
    a.project.parent.chmod(0o777)
    assert not a.enabled()


@pytest.mark.skipif(sys.platform != "darwin", reason="same Mac, two Python runtimes")
def test_all_grant_registered_and_revoked_across_local_runtimes(owner):
    module = Path(project_files.__file__).resolve()
    program = (
        "import importlib.util,sys;from pathlib import Path;"
        "spec=importlib.util.spec_from_file_location('project_files',sys.argv[1]);"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "m._account_home=lambda:Path(sys.argv[2]);a=m.ProjectAccess(Path(sys.argv[3]),"
        "Path(sys.argv[4]));print(getattr(a,sys.argv[5])())"
    )
    def other(path, method):
        return subprocess.check_output(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", program, str(module), str(owner.project),
             str(path), str(owner.fleet_home), method], cwd="/",
            env={"HOME": str(owner.project), "PATH": "/usr/bin:/bin"}, text=True,
        ).strip()
    a = checkout(owner, "src/smoke")
    owner.enable_all()
    assert other(a.project, "enabled") == "True"
    other(owner.project, "disable_all")
    assert not a.enabled()
    other(owner.project, "enable_all")
    assert a.enabled()
    owner.disable_all()
    assert other(a.project, "enabled") == "False"
