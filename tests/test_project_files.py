"""Project sharing is content-only, bounded, and never a general filesystem mount."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ccfleet_agent import project_files as project


def entry(data=b"hello", *, executable=False):
    return {"data": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(), "executable": executable}


@pytest.fixture
def root(tmp_path):
    # macOS's /var and /tmp spellings are symlinks; the API deliberately refuses them.
    result = tmp_path.resolve() / "project"
    result.mkdir()
    return result


def test_snapshot_copies_only_content_relative_names_and_executable_flag(root):
    (root / "src").mkdir()
    source = root / "src/main.py"
    source.write_bytes(b"\x00\xffprint('hello')\n")
    source.chmod(0o4755)
    result = project.snapshot(root)
    assert result == {"src/main.py": entry(source.read_bytes(), executable=True)}
    assert str(root) not in json.dumps(result)
    assert project.manifest(result) == {"src/main.py": {
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "executable": True}}


@pytest.mark.parametrize("name", [
    ".git/a", ".ssh/config", ".aws/config", ".config/tool", ".claude/settings.json",
    ".codex/config.toml", "node_modules/a", ".venv/a", "venv/a", "build/a", "dist/a",
    "__pycache__/a.pyc", ".env", ".env.local", "nested/.ENV.production", "cert.pem",
    "host.key", "id_rsa", "id_ed25519.pub", "credentials.json", "secrets.yaml",
    ".netrc", ".npmrc", ".idea/workspace.xml", ".vscode/settings.json",
])
def test_sensitive_paths_are_never_scanned_or_accepted(root, name, monkeypatch):
    # Fake .git metadata must not cause a git invocation in this filesystem-filter test.
    monkeypatch.setattr(project, "_git_paths", lambda *args: None)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("LOCAL-SECRET-NEVER-SHARED")
    (root / "main.py").write_text("public")
    assert project.snapshot(root) == {"main.py": entry(b"public")}
    with pytest.raises(project.ProjectError, match="excluded"):
        project.validate_snapshot({name: entry()})


@pytest.mark.parametrize("name", [
    "", "/absolute", "../outside", "a/../b", "./a", "a//b", "a/", "a\\b",
    "x\nsecret", "x\x00", "x\t", "x\u202ey", "a:b", "a?b", "a*b", 'a"b', "a<b",
    "a|b", "tail.", "tail ", "CON", "aux.txt", "LPT9.log", "COM1", "x" * 256,
    "/".join(["a"] * 65), "/".join(["a" * 200] * 6), "\udcff",
])
def test_invalid_or_ambiguous_paths_rejected(name):
    with pytest.raises(project.ProjectError):
        project.validate_snapshot({name: entry()})


@pytest.mark.parametrize("files", [
    {"src": entry(), "src/a": entry()},
    {"Foo": entry(), "foo": entry()},
    {"\u00e9": entry(), "e\u0301": entry()},
    {"Dir": entry(), "dir/file": entry()},
])
def test_cross_platform_aliases_and_file_parent_collisions_rejected(files):
    with pytest.raises(project.ProjectError):
        project.validate_snapshot(files)


@pytest.mark.parametrize("item", [
    None, [], {}, {"data": "", "sha256": "a" * 64, "executable": False, "mtime": 1},
    {"data": "", "sha256": "a" * 64, "executable": 0},
    {"data": "", "sha256": "z" * 64, "executable": False},
    {"data": "", "sha256": "A" * 64, "executable": False},
    {"data": "!", "sha256": "a" * 64, "executable": False},
    {"data": "é", "sha256": "a" * 64, "executable": False},
    {"data": 1, "sha256": "a" * 64, "executable": False},
    {"data": "", "sha256": 1, "executable": False},
    {"data": "YQ==\n", "sha256": hashlib.sha256(b"a").hexdigest(), "executable": False},
    {"data": "YR==", "sha256": hashlib.sha256(b"a").hexdigest(), "executable": False},
])
def test_invalid_entries_and_additional_metadata_rejected(item):
    with pytest.raises(project.ProjectError):
        project.validate_snapshot({"file": item})


def test_empty_and_binary_snapshot_roundtrip():
    files = {"empty": entry(b""), "binary": entry(bytes(range(256)))}
    assert project.validate_snapshot(files) == files
    assert project.validate_snapshot({}) == {}
    assert project.validate_manifest({}) == {}
    with pytest.raises(project.ProjectError):
        project.validate_snapshot([])


def test_snapshot_and_validation_enforce_size_and_count_limits(root, monkeypatch):
    monkeypatch.setattr(project, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(project, "MAX_TOTAL_BYTES", 6)
    monkeypatch.setattr(project, "MAX_FILES", 2)
    with pytest.raises(project.ProjectError):
        project.validate_snapshot({"a": entry(b"12345")})
    with pytest.raises(project.ProjectError):
        project.validate_snapshot({"a": entry(b"1234"), "b": entry(b"5678")})
    with pytest.raises(project.ProjectError):
        project.validate_snapshot({name: entry(b"") for name in "abc"})
    (root / "a").write_bytes(b"12345")
    with pytest.raises(project.ProjectError):
        project.snapshot(root)
    (root / "a").write_bytes(b"1234")
    (root / "b").write_bytes(b"5678")
    with pytest.raises(project.ProjectError):
        project.snapshot(root)
    (root / "c").write_bytes(b"")
    with pytest.raises(project.ProjectError):
        project.snapshot(root)


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo"])
def test_snapshot_refuses_links_and_special_files(root, kind):
    outside = root.parent / "private"
    outside.mkdir()
    (outside / "secret").write_text("never send")
    if kind == "symlink":
        (root / "danger").symlink_to(outside / "secret")
    elif kind == "directory_symlink":
        (root / "danger").symlink_to(outside, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside / "secret", root / "danger")
    else:
        os.mkfifo(root / "danger")
    with pytest.raises(project.ProjectError):
        project.snapshot(root)


def test_root_and_ancestor_symlinks_rejected(root):
    alias = root.parent / "alias"
    alias.symlink_to(root, target_is_directory=True)
    (root / "child").mkdir()
    for path in [alias, alias / "child"]:
        with pytest.raises(project.ProjectError, match="symlink"):
            project.snapshot(path)
        with pytest.raises(project.ProjectError, match="symlink"):
            project.apply_snapshot(path, {}, {}, root.parent / "backup")


def test_gitignore_honored_without_personal_git_configuration(root, monkeypatch):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored/\nignored.txt\n")
    (root / "ignored").mkdir()
    (root / "ignored/secret").write_text("not shared")
    (root / "ignored.txt").write_text("not shared")
    (root / "main.py").write_text("share")
    (root / ".env").write_text("not even if tracked")
    subprocess.run(["git", "-C", str(root), "add", ".env"], check=True)
    malicious = root.parent / "personal.gitconfig"
    malicious.write_text("[core]\nexcludesFile = /must/not/read/host/path\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(malicious))
    monkeypatch.setenv("GIT_DIR", "/must/not/read/other/repo")
    result = project.snapshot(root)
    assert set(result) == {".gitignore", "main.py"}


def test_git_fsmonitor_cannot_execute_from_repository_config(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    marker = root.parent / "should-never-exist"
    hook = root / "hook"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(root), "config", "core.fsmonitor", str(hook)], check=True)
    assert "hook" in project.snapshot(root)
    assert not marker.exists()


def test_deleted_tracked_files_are_absent_from_git_snapshot(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "old").write_text("old")
    subprocess.run(["git", "-C", str(root), "add", "old"], check=True)
    (root / "old").unlink()
    assert project.snapshot(root) == {}


def test_git_symlink_metadata_and_broken_repository_fail_closed(root):
    (root / ".git").symlink_to(root.parent)
    with pytest.raises(project.ProjectError, match="Git metadata"):
        project.snapshot(root)
    (root / ".git").unlink()
    (root / ".git").write_text("garbage")
    with pytest.raises(project.ProjectError, match="Git ignore"):
        project.snapshot(root)


def test_directory_scan_is_bounded_even_when_directories_are_empty(root, monkeypatch):
    for name in ["a", "b", "c"]:
        (root / name).mkdir()
    monkeypatch.setattr(project, "MAX_SCANNED_ENTRIES", 2)
    with pytest.raises(project.ProjectError, match="scan exceeds"):
        project.snapshot(root)


def test_subprocess_output_limit_enforced_while_reading(root):
    with pytest.raises(project.ProjectError, match="listing exceeds"):
        project._git_output([sys.executable, "-c", "print('x' * 10000)"], root,
                            {"PATH": os.defpath}, 100)


def test_git_discovery_refuses_another_root(root, monkeypatch):
    (root / ".git").mkdir()
    monkeypatch.setattr(project, "_git_output", lambda *args: b"/other/root\n")
    with pytest.raises(project.ProjectError, match="must match"):
        project.snapshot(root)


def test_git_unavailable_fails_closed(root, monkeypatch):
    (root / ".git").mkdir()

    def unavailable(*args):
        raise FileNotFoundError("git")

    monkeypatch.setattr(project, "_git_output", unavailable)
    with pytest.raises(project.ProjectError, match="Git ignore"):
        project.snapshot(root)


def test_changes_reports_only_content_or_executable_changes():
    before = project.manifest({"same": entry(), "edit": entry(), "gone": entry()})
    after = project.manifest({"same": entry(), "edit": entry(executable=True), "new": entry()})
    assert project.changes(before, after) == [
        {"path": "edit", "status": "modified"},
        {"path": "gone", "status": "deleted"},
        {"path": "new", "status": "added"},
    ]


def test_apply_add_update_delete_preserves_unshared_and_creates_private_backups(root):
    (root / "edit").write_bytes(b"before")
    (root / "gone").write_bytes(b"recover me")
    (root / "unshared").write_bytes(b"untouched")
    (root / ".env").write_bytes(b"LOCAL_SECRET")
    expected = project.manifest({"edit": entry(b"before"), "gone": entry(b"recover me")})
    desired = {"edit": entry(b"after"), "new/script": entry(b"#!/bin/sh\n", executable=True)}
    backup = root.parent / "backups/session"
    project.apply_snapshot(root, desired, expected, backup)
    assert (root / "edit").read_bytes() == b"after"
    assert not (root / "gone").exists()
    assert (root / "new/script").read_bytes() == b"#!/bin/sh\n"
    assert (root / "unshared").read_bytes() == b"untouched"
    assert (root / ".env").read_bytes() == b"LOCAL_SECRET"
    assert (backup / "edit").read_bytes() == b"before"
    assert (backup / "gone").read_bytes() == b"recover me"
    assert not (backup / "new").exists()
    assert stat.S_IMODE((root / "edit").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "new/script").stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700


@pytest.mark.parametrize("conflict", ["changed", "deleted", "executable", "untracked_collision"])
def test_all_conflicts_are_detected_before_any_file_changes(root, conflict):
    (root / "a").write_bytes(b"original")
    (root / "z").write_bytes(b"original")
    expected = project.manifest(project.snapshot(root))
    desired = {"a": entry(b"new"), "z": entry(b"new")}
    if conflict == "changed":
        (root / "z").write_bytes(b"local edit")
    elif conflict == "deleted":
        (root / "z").unlink()
    elif conflict == "executable":
        (root / "z").chmod(0o700)
    else:
        (root / "zz").write_bytes(b"untracked")
        desired["zz"] = entry(b"overwrite")
    with pytest.raises(project.ProjectError, match="conflict"):
        project.apply_snapshot(root, desired, expected, root.parent / "backup")
    assert (root / "a").read_bytes() == b"original"
    assert not (root.parent / "backup").exists()


@pytest.mark.parametrize("kind", ["leaf_symlink", "parent_symlink", "hardlink", "fifo", "dir"])
def test_apply_refuses_unsafe_destination_without_touching_outside(root, kind):
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    name = "danger"
    if kind == "leaf_symlink":
        (root / name).symlink_to(outside / "secret")
    elif kind == "parent_symlink":
        (root / name).symlink_to(outside, target_is_directory=True)
        name += "/secret"
    elif kind == "hardlink":
        os.link(outside / "secret", root / name)
    elif kind == "fifo":
        os.mkfifo(root / name)
    else:
        (root / name).mkdir()
    with pytest.raises(project.ProjectError):
        project.apply_snapshot(root, {name: entry(b"evil")}, {}, root.parent / "backup")
    assert (outside / "secret").read_bytes() == b"secret"


@pytest.mark.parametrize("case", ["inside", "ancestor", "public", "nonempty", "symlink"])
def test_backup_must_be_private_empty_and_outside_project(root, case):
    (root / "a").write_bytes(b"original")
    expected = project.manifest(project.snapshot(root))
    backup = root.parent / "backup"
    if case == "inside":
        backup = root / "backup"
    elif case == "ancestor":
        backup = root.parent
    elif case == "public":
        backup.mkdir(mode=0o755)
        backup.chmod(0o755)
    elif case == "nonempty":
        backup.mkdir(mode=0o700)
        (backup / "valuable").write_text("keep")
    else:
        backup.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(project.ProjectError):
        project.apply_snapshot(root, {"a": entry(b"change")}, expected, backup)
    assert (root / "a").read_bytes() == b"original"


def test_identical_apply_is_noop_and_does_not_create_backup(root):
    (root / "file").write_bytes(b"same")
    files = project.snapshot(root)
    before = (root / "file").stat()
    backup = root.parent / "not-needed"
    project.apply_snapshot(root, files, project.manifest(files), backup)
    assert not backup.exists()
    assert (root / "file").stat().st_ino == before.st_ino


@pytest.mark.parametrize("shape", ["file_to_dir", "dir_to_file"])
def test_file_directory_shape_changes_rejected_before_updates(root, shape):
    if shape == "file_to_dir":
        (root / "thing").write_bytes(b"old")
        desired = {"thing/file": entry(b"new")}
    else:
        (root / "thing").mkdir()
        (root / "thing/file").write_bytes(b"old")
        desired = {"thing": entry(b"new")}
    expected = project.manifest(project.snapshot(root))
    with pytest.raises(project.ProjectError):
        project.apply_snapshot(root, desired, expected, root.parent / "backup")
    assert project.manifest(project.snapshot(root)) == expected


def test_apply_rechecks_after_backups_and_rejects_concurrent_edit(root, monkeypatch):
    (root / "file").write_bytes(b"before")
    expected = project.manifest(project.snapshot(root))
    original_write = project._write

    def change_during_backup(*args, **kwargs):
        original_write(*args, **kwargs)
        (root / "file").write_bytes(b"concurrent")

    monkeypatch.setattr(project, "_write", change_during_backup)
    with pytest.raises(project.ProjectError, match="changed during apply"):
        project.apply_snapshot(root, {"file": entry(b"after")}, expected, root.parent / "backup")
    assert (root / "file").read_bytes() == b"concurrent"
    assert (root.parent / "backup/file").read_bytes() == b"before"


def test_snapshot_detects_file_replaced_during_read(root, monkeypatch):
    (root / "file").write_bytes(b"original")
    real_read = os.read
    replaced = False

    def replace_during_read(*args):
        nonlocal replaced
        data = real_read(*args)
        if not replaced:
            replaced = True
            (root / "file").unlink()
            (root / "file").write_bytes(b"replacement")
        return data

    monkeypatch.setattr(project.os, "read", replace_during_read)
    with pytest.raises(project.ProjectError, match="changed during read"):
        project.snapshot(root)


def test_unsafe_incoming_snapshot_prevalidated_before_backup_or_mutations(root):
    (root / "a").write_bytes(b"original")
    expected = project.manifest(project.snapshot(root))
    with pytest.raises(project.ProjectError):
        project.apply_snapshot(root, {"a": entry(b"new"), "../outside": entry()}, expected,
                               root.parent / "backup")
    assert (root / "a").read_bytes() == b"original"
    assert not (root.parent / "backup").exists()


def test_apply_bounds_existing_content_even_when_manifest_has_no_file_sizes(root, monkeypatch):
    (root / "a").write_bytes(b"1234")
    (root / "b").write_bytes(b"5678")
    expected = project.manifest(project.snapshot(root))
    monkeypatch.setattr(project, "MAX_TOTAL_BYTES", 6)
    with pytest.raises(project.ProjectError, match="existing project exceeds"):
        project.apply_snapshot(root, {}, expected, root.parent / "backup")
    assert (root / "a").read_bytes() == b"1234"
    assert (root / "b").read_bytes() == b"5678"
    assert not (root.parent / "backup").exists()


def test_shared_snapshot_honors_gitignore_without_project_git_metadata(root):
    (root / ".gitignore").write_text("*.log\ncache/\n")
    (root / "debug.log").write_text("slot generated; not shared")
    (root / "main.py").write_text("shared")
    (root / "cache").mkdir()
    (root / "cache/item").write_text("not shared")
    result = project.snapshot(root, tracked=[])
    assert set(result) == {".gitignore", "main.py"}
    assert not (root / ".git").exists()
    assert "debug.log" in project.snapshot(root)  # ordinary non-Git behavior is unchanged


def test_shared_snapshot_preserves_explicitly_shared_tracked_but_ignored_files(root):
    (root / ".gitignore").write_text("*.log\n")
    (root / "debug.log").write_text("generated")
    (root / "approved.log").write_text("explicitly approved")
    result = project.snapshot(root, tracked={"approved.log": {}}.keys())
    assert set(result) == {".gitignore", "approved.log"}
    assert result["approved.log"] == entry(b"explicitly approved")
    (root / "approved.log").unlink()
    assert set(project.snapshot(root, tracked=["approved.log"])) == {".gitignore"}


@pytest.mark.parametrize("tracked", [
    ["../outside"], ["/etc/passwd"], ["a\\b"], [".env"], [".ssh/config"],
    ["credentials.json"], ["a", "a/b"], ["A", "a"], [None], "filename", b"filename",
])
def test_shared_snapshot_refuses_unsafe_tracked_paths_before_invoking_git(root, tracked, monkeypatch):
    monkeypatch.setattr(project, "_git_output", lambda *args: pytest.fail("invalid tracked paths"))
    with pytest.raises(project.ProjectError):
        project.snapshot(root, tracked=tracked)


def test_shared_tracked_iteration_and_combined_file_count_are_bounded(root, monkeypatch):
    monkeypatch.setattr(project, "MAX_FILES", 2)
    with pytest.raises(project.ProjectError, match="list exceeds"):
        project.snapshot(root, tracked=("same" for _ in range(3)))
    (root / "new").write_text("new")
    with pytest.raises(project.ProjectError, match="oversized"):
        project.snapshot(root, tracked=["old1", "old2"])


def test_shared_snapshot_uses_nested_rules_and_preserves_negation(root):
    (root / ".gitignore").write_text("*.log\n!keep.log\n")
    (root / "keep.log").write_text("shared")
    (root / "hide.log").write_text("not shared")
    (root / "nested").mkdir()
    (root / "nested/.gitignore").write_text("*.txt\n")
    (root / "nested/hide.txt").write_text("not shared")
    (root / "nested/main.py").write_text("shared")
    assert set(project.snapshot(root, tracked=[])) == {
        ".gitignore", "keep.log", "nested/.gitignore", "nested/main.py"}


def test_shared_snapshot_never_uses_real_git_or_personal_environment(root, monkeypatch):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".git/info/exclude").write_text("visible\n")
    (root / "visible").write_text("only content shared")
    monkeypatch.setenv("GIT_DIR", "/private-host-fingerprint")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/private-templates")
    monkeypatch.setenv("LOCAL_HOST_SENTINEL", "private-host-fingerprint")
    real_output = project._git_output
    captured = []

    def output(command, selected, env, limit):
        captured.append((command, dict(env)))
        return real_output(command, selected, env, limit)

    monkeypatch.setattr(project, "_git_output", output)
    result = project.snapshot(root, tracked=[])
    assert set(result) == {"visible"}
    assert "private-host-fingerprint" not in json.dumps(result)
    assert str(root) not in json.dumps(result)
    assert all("LOCAL_HOST_SENTINEL" not in env and "GIT_DIR" not in env
               and "GIT_TEMPLATE_DIR" not in env for _, env in captured)
    git_args = [arg for command, _ in captured for arg in command if arg.startswith("--git-dir=")]
    assert len(git_args) == 1
    temporary_git = Path(git_args[0].split("=", 1)[1])
    assert root not in temporary_git.parents
    assert not temporary_git.parent.exists()


def test_shared_snapshot_temporary_metadata_never_created_inside_selected_root(root, monkeypatch):
    monkeypatch.setattr(project.tempfile, "gettempdir", lambda: str(root))
    (root / "main.py").write_text("shared")
    assert project.snapshot(root, tracked=[]) == {"main.py": entry(b"shared")}
    assert sorted(path.name for path in root.iterdir()) == ["main.py"]
    assert not list(root.parent.glob("ccfleet-ignore-*"))


def test_shared_snapshot_cleans_temporary_git_on_failure(root, monkeypatch):
    monkeypatch.setattr(project.tempfile, "gettempdir", lambda: str(root.parent))
    monkeypatch.setattr(project, "_git_output", lambda *args: (_ for _ in ()).throw(
        FileNotFoundError("private-details-not-returned")))
    with pytest.raises(project.ProjectError, match="cannot safely read shared"):
        project.snapshot(root, tracked=[])
    assert not list(root.parent.glob("ccfleet-ignore-*"))


def test_shared_gitignored_symlink_is_not_followed_and_tracked_symlink_is_rejected(root):
    outside = root.parent / "secret"
    outside.write_text("never shared")
    (root / ".gitignore").write_text("*.log\n")
    (root / "link.log").symlink_to(outside)
    assert set(project.snapshot(root, tracked=[])) == {".gitignore"}
    with pytest.raises(project.ProjectError):
        project.snapshot(root, tracked=["link.log"])
