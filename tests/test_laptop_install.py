"""The one-command installer and legacy-client transition."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALL = ROOT / "laptop" / "install.sh"


def run_install(tmp_path: Path, *args: str, pair_rc: int = 0,
                old_rc: int = 0, old_client: bool = True):
    home = tmp_path / "home"
    dest = home / ".local" / "bin"
    dest.mkdir(parents=True)
    log = tmp_path / "calls.log"
    client = tmp_path / "fake-ccfleet"
    client.write_text("""#!/usr/bin/env python3
import os, sys
with open(os.environ["TEST_LOG"], "a") as stream:
    stream.write("client:" + " ".join(sys.argv[1:]) + "\\n")
raise SystemExit(int(os.environ.get("PAIR_RC", "0")) if sys.argv[1:2] == ["login"] else 0)
""")
    client.chmod(0o755)
    old = dest / "ccfleet-connect"
    if old_client:
        old.write_text("""#!/bin/sh
printf 'old:%s\\n' "$*" >> "$TEST_LOG"
exit "${OLD_RC:-0}"
""")
        old.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "CCFLEET_INSTALL_DIR": str(dest),
           "CCFLEET_INSTALL_URL": client.as_uri(), "TEST_LOG": str(log),
           "PAIR_RC": str(pair_rc), "OLD_RC": str(old_rc),
           "PATH": f"{dest}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    result = subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True,
                            text=True, timeout=30)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls, dest


def test_plain_install_does_not_start_a_transition(tmp_path):
    result, calls, dest = run_install(tmp_path)
    assert result.returncode == 0
    assert calls == []
    assert (dest / "ccfleet").stat().st_mode & 0o111


def test_migration_pairs_before_removing_the_old_setup(tmp_path):
    result, calls, _ = run_install(tmp_path, "--migrate")
    assert result.returncode == 0, result.stderr
    assert calls == ["client:login --name computer", "old:--remove"]
    assert "Transition complete" in result.stdout
    assert "revoke it in your Anthropic account" in result.stdout


def test_migration_accepts_a_non_identifying_device_label(tmp_path):
    result, calls, _ = run_install(tmp_path, "--migrate", "--name", "personal laptop")
    assert result.returncode == 0
    assert calls[0] == "client:login --name personal laptop"


def test_failed_pairing_leaves_the_old_setup_untouched(tmp_path):
    result, calls, _ = run_install(tmp_path, "--migrate", pair_rc=2)
    assert result.returncode == 1
    assert calls == ["client:login --name computer"]
    assert "old setup was not removed" in result.stderr


def test_failed_legacy_cleanup_is_reported_after_pairing(tmp_path):
    result, calls, _ = run_install(tmp_path, "--migrate", old_rc=1)
    assert result.returncode == 1
    assert calls == ["client:login --name computer", "old:--remove"]
    assert "cleanup failed" in result.stderr


def test_migration_without_an_old_command_is_still_a_valid_new_pairing(tmp_path):
    result, calls, _ = run_install(tmp_path, "--migrate", old_client=False)
    assert result.returncode == 0
    assert calls == ["client:login --name computer"]
    assert "No installed ccfleet-connect command" in result.stdout


@pytest.mark.parametrize("args", [("--wat",), ("--name",), ("--name", "x")])
def test_bad_installer_arguments_fail_before_downloading(tmp_path, args):
    result, calls, dest = run_install(tmp_path, *args)
    assert result.returncode == 2
    assert calls == [] and not (dest / "ccfleet").exists()


def test_customer_docs_publish_the_one_command_transition():
    from ccfleetd.config import Config
    from ccfleetd.customer_docs import guide

    page = guide(Config())
    assert "laptop/install.sh | bash -s -- --migrate" in page
    assert "<pre><code>curl -fsSL" in page
    assert "pairs the new client first" in page
