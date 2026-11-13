"""Private-owner SSH convergence keeps ordinary administrator access intact."""
from __future__ import annotations

import base64
import copy
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import mac_keys as keys


def public_key(value):
    blob = (struct.pack("!I", 11) + b"ssh-ed25519" + struct.pack("!I", 32)
            + bytes([value]) * 32)
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


def snapshot(**changes):
    return {"version": 1, "unix_user": "owner", "enabled": True,
            "device_public_keys": [public_key(1)], "gateway_public_keys": [public_key(2)],
            **changes}


@pytest.fixture
def owner(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    home = tmp_path / "owner"
    home.mkdir(mode=0o700)
    entry = home / "runtime" / "mac_slot_entry.py"
    entry.parent.mkdir(mode=0o700)
    entry.write_text("# synthetic fixed entrypoint\n")
    entry.chmod(0o600)
    identity = SimpleNamespace(pw_uid=os.getuid(), pw_name="owner", pw_dir=str(home))
    monkeypatch.setattr(keys.mac_policy, "_TRUST_ROOT", tmp_path)
    monkeypatch.setattr(keys.mac_policy, "authenticated_owner", lambda h=None: identity)
    return home, entry


def existing(owner, raw):
    home, _ = owner
    (home / ".ssh").mkdir(mode=0o700)
    path = home / ".ssh" / "authorized_keys"
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def reconcile(owner, value=None):
    home, entry = owner
    return keys.reconcile(value or snapshot(), home=home, entrypoint=entry)


def test_adds_only_fixed_restricted_keys_and_preserves_administrator_bytes(owner):
    admin = ("# existing administrator\n" + public_key(3) + " admin-key\n").encode()
    path = existing(owner, admin)
    value = snapshot()
    saved = copy.deepcopy(value)
    result = reconcile(owner, value)
    raw = path.read_bytes()
    assert raw.startswith(admin)
    assert result == {"device_keys": 1, "gateway_keys": 1, "changed": True}
    assert value == saved
    assert raw.count(keys.BEGIN) == raw.count(keys.END) == 1
    assert (f'restrict,command="/usr/bin/python3 -I -B {owner[1]}" {public_key(1)}').encode() in raw
    assert (f'restrict,command="/usr/bin/python3 -I -B {owner[1]} --gateway-only" '
            f'{public_key(2)}').encode() in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == os.getuid()
    before = path.stat().st_mtime_ns
    assert reconcile(owner)["changed"] is False
    assert path.stat().st_mtime_ns == before


def test_rotates_only_managed_block_and_preserves_prefix_and_suffix(owner):
    prefix = (public_key(3) + " administrator-before\n").encode()
    suffix = ("# unrelated trailing entry\n" + public_key(4) + " administrator-after\n").encode()
    path = existing(owner, prefix)
    reconcile(owner)
    path.write_bytes(path.read_bytes() + suffix)
    reconcile(owner, snapshot(device_public_keys=[public_key(5)], gateway_public_keys=[]))
    raw = path.read_bytes()
    assert raw.startswith(prefix) and raw.endswith(suffix)
    assert public_key(1).encode() not in raw and public_key(2).encode() not in raw
    assert raw.count(keys.BEGIN) == 1 and public_key(5).encode() in raw
    reconcile(owner, snapshot(enabled=False))
    raw = path.read_bytes()
    assert raw.startswith(prefix) and raw.endswith(suffix)
    assert b"restrict,command=" not in raw


def test_unterminated_administrator_line_remains_an_exact_prefix(owner):
    admin = (public_key(3) + " no-final-newline").encode()
    path = existing(owner, admin)
    reconcile(owner)
    assert path.read_bytes().startswith(admin + b"\n" + keys.BEGIN)


@pytest.mark.parametrize("prefix", ["", 'command="echo administrator",no-pty '])
def test_rejects_managed_key_already_present_outside_block(owner, prefix):
    original = (prefix + public_key(1) + " existing\n").encode()
    path = existing(owner, original)
    with pytest.raises(keys.AccessError, match="outside"):
        reconcile(owner)
    assert path.read_bytes() == original


@pytest.mark.parametrize("raw", [
    keys.BEGIN + b"\n", keys.END + b"\n",
    keys.BEGIN + b"\n" + keys.END + b"\n" + keys.BEGIN + b"\n" + keys.END + b"\n",
    b"not-a-marker " + keys.BEGIN + b"\n" + keys.END + b"\n",
    keys.END + b"\n" + keys.BEGIN + b"\n",
])
def test_ambiguous_managed_blocks_fail_closed(owner, raw):
    path = existing(owner, raw)
    with pytest.raises(keys.AccessError):
        reconcile(owner)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"enabled": 1}, {"unix_user": "bad;user"},
    {"command": "/bin/sh"}, {"device_public_keys": "not-a-list"},
    {"device_public_keys": [public_key(1), public_key(1)]},
    {"gateway_public_keys": [public_key(1)]},
    {"device_public_keys": [public_key(1) + "\ncommand=/bin/sh"]},
    {"device_public_keys": ['command="/bin/sh" ' + public_key(1)]},
    {"device_public_keys": ["ssh-ed25519 invalid"]},
    {"device_public_keys": [public_key(i) for i in range(11)]},
])
def test_rejects_untyped_or_unbounded_access_snapshots(change):
    with pytest.raises(keys.AccessError):
        keys.validate_snapshot(snapshot(**change))


def test_snapshot_cannot_target_another_local_account(owner):
    with pytest.raises(keys.AccessError, match="different Unix user"):
        reconcile(owner, snapshot(unix_user="another"))
    assert not (owner[0] / ".ssh").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "world-readable", "fifo"])
def test_unsafe_authorized_keys_is_never_replaced(owner, unsafe):
    path = existing(owner, b"# admin\n")
    if unsafe == "world-readable":
        path.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(path, path.with_name("other-link"))
    elif unsafe == "symlink":
        target = path.with_name("admin-original")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    before = path.lstat()
    with pytest.raises(keys.AccessError):
        reconcile(owner)
    assert path.lstat().st_ino == before.st_ino


@pytest.mark.parametrize("unsafe", ["symlink", "writable", "not-private"])
def test_unsafe_ssh_directory_is_refused(owner, unsafe):
    home, _ = owner
    directory = home / ".ssh"
    if unsafe == "symlink":
        target = home / "elsewhere"
        target.mkdir(mode=0o700)
        directory.symlink_to(target)
    else:
        directory.mkdir(mode=0o777 if unsafe == "writable" else 0o755)
        directory.chmod(0o777 if unsafe == "writable" else 0o755)
    with pytest.raises((OSError, ValueError)):
        reconcile(owner)
    assert not (directory / "authorized_keys").exists()


def test_fixed_entrypoint_cannot_be_a_symlink(owner):
    _, entry = owner
    target = entry.with_name("other.py")
    entry.rename(target)
    entry.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        reconcile(owner)
    assert not (owner[0] / ".ssh").exists()


def test_concurrent_administrator_edit_is_preserved(owner, monkeypatch):
    path = existing(owner, b"# original admin\n")
    replacement = (public_key(3) + " newly-added-admin\n").encode()
    original_read, calls = keys._read, []

    def read(directory, uid):
        calls.append(1)
        if len(calls) == 2:
            path.write_bytes(replacement)
        return original_read(directory, uid)

    monkeypatch.setattr(keys, "_read", read)
    with pytest.raises(keys.AccessError, match="changed during update"):
        reconcile(owner)
    assert path.read_bytes() == replacement
    assert not list(path.parent.glob(".ccfleet-keys-*"))


def test_isolated_import_uses_only_installed_sibling_module():
    module = Path(keys.__file__).resolve()
    code = ("import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('isolated_mac_keys',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "assert m.mac_policy.__file__.endswith('/ccfleet_agent/mac_policy.py')")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(module)],
                            capture_output=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr.decode()
