"""Releasing a slot must never recursively delete a live laptop filesystem."""

from __future__ import annotations

import os
import subprocess

import pytest

from .test_slot_scripts import REMOVE, fake_system


def run_release(tmp_path, entries, *, unmount=True, remove_entry=True):
    # The fake account has no real /home/slot01 directory on the test machine.
    bindir = fake_system(tmp_path, slot_home=None)
    table = tmp_path / "mountinfo"
    table.write_text(entries)
    log = tmp_path / "unmounts"
    command = bindir / "umount"
    command.write_text(
        "#!/usr/bin/env python3\nimport os,pathlib,sys\n"
        "pathlib.Path(os.environ['UNMOUNT_LOG']).write_text(' '.join(sys.argv[1:]))\n"
        + ("pathlib.Path(os.environ['CCFLEET_MOUNTINFO']).write_text('')\n" if remove_entry else "")
        + f"raise SystemExit({0 if unmount else 1})\n")
    command.chmod(0o755)
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
           "CCFLEET_MOUNTINFO": str(table), "CCFLEET_ETC_DIR": str(tmp_path / "etc"),
           "CCFLEET_SLICE_ROOT": str(tmp_path / "systemd"), "UNMOUNT_LOG": str(log)}
    result = subprocess.run([str(REMOVE), "--slot", "slot01"], env=env,
                            capture_output=True, text=True, timeout=20)
    return result, bindir / "gone.marker", log


def line(path, filesystem="fuse.sshfs"):
    return f"51 20 0:42 / {path} rw - {filesystem} ccfleet rw\n"


def test_known_live_mount_is_detached_before_account_removal(tmp_path):
    mount = "/home/slot01/workspace/live/" + "a" * 32
    result, removed, log = run_release(tmp_path, line(mount))
    assert result.returncode == 0, result.stderr
    assert removed.exists()
    assert log.read_text() == "-l -- " + mount


@pytest.mark.parametrize("path,filesystem", [
    ("/home/slot01", "fuse.sshfs"),
    ("/home/slot01/workspace/other", "fuse.sshfs"),
    ("/home/slot01/workspace/live/" + "a" * 32, "ext4"),
    ("/home/slot01/workspace/live/" + "a" * 32 + "/nested", "fuse.sshfs"),
])
def test_unknown_mounts_refuse_release_before_any_deletion(tmp_path, path, filesystem):
    result, removed, log = run_release(tmp_path, line(path, filesystem))
    assert result.returncode != 0
    assert "mounted filesystem safety check failed" in result.stderr
    assert not removed.exists() and not log.exists()


@pytest.mark.parametrize("unmount,remove_entry", [(False, False), (True, False)])
def test_failed_or_ineffective_unmount_cannot_release_the_slot(tmp_path, unmount, remove_entry):
    result, removed, _ = run_release(tmp_path, line("/home/slot01/workspace/live/" + "b" * 32),
                                      unmount=unmount, remove_entry=remove_entry)
    assert result.returncode != 0 and not removed.exists()


def test_other_users_mounts_are_not_touched(tmp_path):
    result, removed, log = run_release(tmp_path, line("/home/slot02/workspace/live/" + "c" * 32))
    assert result.returncode == 0, result.stderr
    assert removed.exists() and not log.exists()


def test_unreadable_or_malformed_mount_table_is_not_treated_as_empty(tmp_path):
    result, removed, log = run_release(tmp_path, "not a mount table\n")
    assert result.returncode != 0
    assert not removed.exists() and not log.exists()
