"""The one-command installer and legacy-client transition."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALL = ROOT / "laptop" / "install.sh"
HELPER_SOURCE = "\"\"\"A pinned project-file helper fixture.\"\"\"\nVALUE = 1\n"
HELPER_DIGEST = hashlib.sha256(HELPER_SOURCE.encode()).hexdigest()


def run_install(tmp_path: Path, *args: str, pair_rc: int = 0,
                old_rc: int = 0, old_client: bool = True,
                helper_source: str = HELPER_SOURCE, helper_missing: bool = False,
                digest_assignment: str | None = None, client_prelude: str = "",
                existing_client: str | None = None, download_url: str | None = None,
                helper_url_override: bool = True, python_setup: str = ""):
    home = tmp_path / "home"
    dest = home / ".local" / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    if existing_client is not None:
        (dest / "ccfleet").write_text(existing_client)
    log = tmp_path / "calls.log"
    client = tmp_path / "fake-ccfleet"
    assignment = (digest_assignment if digest_assignment is not None
                  else f"PROJECT_FILES_SHA256 = {HELPER_DIGEST!r}")
    client.write_text("#!/usr/bin/env python3\n" + assignment + "\n" + client_prelude + "\n" + """
import os, sys
with open(os.environ["TEST_LOG"], "a") as stream:
    stream.write("client:" + " ".join(sys.argv[1:]) + "\\n")
raise SystemExit(int(os.environ.get("PAIR_RC", "0")) if sys.argv[1:2] == ["login"] else 0)
""")
    client.chmod(0o755)
    helper = tmp_path / "fake-project-files.py"
    if not helper_missing:
        helper.write_text(helper_source)
    old = dest / "ccfleet-connect"
    if old_client:
        old.write_text("""#!/bin/sh
printf 'old:%s\\n' "$*" >> "$TEST_LOG"
exit "${OLD_RC:-0}"
""")
        old.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "CCFLEET_INSTALL_DIR": str(dest),
           "CCFLEET_INSTALL_URL": download_url or client.as_uri(), "TEST_LOG": str(log),
           "PAIR_RC": str(pair_rc), "OLD_RC": str(old_rc),
           "PATH": f"{dest}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    if helper_url_override:
        env["CCFLEET_PROJECT_FILES_URL"] = helper.as_uri()
    else:
        env.pop("CCFLEET_PROJECT_FILES_URL", None)
    if python_setup:
        # Instrument installer subprocesses without replacing the installer logic.
        hooks = tmp_path / "python-hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(python_setup)
        env["PYTHONPATH"] = str(hooks)
    if download_url is not None:
        # Keep URL derivation tests offline, while exercising the real installer.
        curl = dest / "curl"
        curl.write_text("""#!/usr/bin/env python3
import os, pathlib, shutil, sys
url = sys.argv[2]
with open(os.environ["DOWNLOAD_LOG"], "a") as stream:
    stream.write(url + "\\n")
source = "CLIENT_SOURCE" if url.endswith("/laptop/ccfleet") else "HELPER_SOURCE"
shutil.copyfile(os.environ[source], sys.argv[4])
""")
        curl.chmod(0o755)
        env.update(DOWNLOAD_LOG=str(tmp_path / "downloads.log"),
                   CLIENT_SOURCE=str(client), HELPER_SOURCE=str(helper))
    result = subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True,
                            text=True, timeout=30)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls, dest


def test_plain_install_does_not_start_a_transition(tmp_path):
    result, calls, dest = run_install(tmp_path)
    assert result.returncode == 0
    assert calls == []
    assert (dest / "ccfleet").stat().st_mode & 0o111
    helper = dest / f"ccfleet-project-files-{HELPER_DIGEST}.py"
    assert helper.read_text() == HELPER_SOURCE
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == HELPER_DIGEST
    assert helper.stat().st_mode & 0o777 == 0o644
    assert not list(dest.glob(".ccfleet*"))


@pytest.mark.parametrize("helper_missing", [False, True])
def test_unavailable_or_tampered_helper_preserves_existing_client(tmp_path, helper_missing):
    old_source = "#!/bin/sh\nprintf 'old client\\n'\n"
    result, calls, dest = run_install(
        tmp_path, "--migrate", existing_client=old_source,
        helper_source="raise RuntimeError('tampered helper')\n", helper_missing=helper_missing,
    )
    assert result.returncode != 0
    assert calls == []
    assert (dest / "ccfleet").read_text() == old_source
    assert not list(dest.glob("ccfleet-project-files-*"))
    assert not list(dest.glob(".ccfleet*"))
    if not helper_missing:
        assert "checksum mismatch" in result.stderr


def test_plain_install_never_executes_downloaded_programs(tmp_path):
    marker = tmp_path / "executed"
    side_effect = f"open({str(marker)!r}, 'w').write('executed')\n"
    helper_digest = hashlib.sha256(side_effect.encode()).hexdigest()
    result, calls, dest = run_install(
        tmp_path, client_prelude=side_effect, helper_source=side_effect,
        digest_assignment=f"PROJECT_FILES_SHA256 = {helper_digest!r}",
    )
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert not marker.exists()
    assert (dest / f"ccfleet-project-files-{helper_digest}.py").read_text() == side_effect


@pytest.mark.parametrize("assignment", [
    "", "PROJECT_FILES_SHA256 = 'abc'", "PROJECT_FILES_SHA256 = 'A' * 64",
    f"PROJECT_FILES_SHA256 = {('A' * 64)!r}",
    f"PROJECT_FILES_SHA256: str = {HELPER_DIGEST!r}",
    f"PROJECT_FILES_SHA256 = {HELPER_DIGEST!r}\nPROJECT_FILES_SHA256 = {HELPER_DIGEST!r}",
    f"PROJECT_FILES_SHA256 = OTHER = {HELPER_DIGEST!r}",
])
def test_invalid_digest_declarations_leave_client_untouched(tmp_path, assignment):
    result, calls, dest = run_install(tmp_path, digest_assignment=assignment,
                                      existing_client="old client\n")
    assert result.returncode != 0
    assert calls == []
    assert (dest / "ccfleet").read_text() == "old client\n"
    assert "PROJECT_FILES_SHA256" in result.stderr


def test_computed_digest_is_not_executed(tmp_path):
    marker = tmp_path / "computed"
    result, _, dest = run_install(
        tmp_path, existing_client="old client\n",
        digest_assignment=f"PROJECT_FILES_SHA256 = open({str(marker)!r}, 'w').write('bad')",
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert (dest / "ccfleet").read_text() == "old client\n"


def test_syntax_invalid_helper_leaves_existing_client_untouched(tmp_path):
    invalid = "def broken(:\n"
    digest = hashlib.sha256(invalid.encode()).hexdigest()
    result, calls, dest = run_install(
        tmp_path, helper_source=invalid, digest_assignment=f"PROJECT_FILES_SHA256 = {digest!r}",
        existing_client="old client\n",
    )
    assert result.returncode != 0
    assert calls == []
    assert (dest / "ccfleet").read_text() == "old client\n"
    assert not list(dest.glob("ccfleet-project-files-*"))


def test_install_keeps_previous_helpers_and_user_configuration(tmp_path):
    dest = tmp_path / "home" / ".local" / "bin"
    dest.mkdir(parents=True)
    old_helper = dest / ("ccfleet-project-files-" + "a" * 64 + ".py")
    old_helper.write_text("old helper\n")
    config = tmp_path / "home" / ".config" / "ccfleet" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"pairing":"preserved"}\n')
    result, _, _ = run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert old_helper.read_text() == "old helper\n"
    assert config.read_text() == '{"pairing":"preserved"}\n'


def test_interruption_before_client_replacement_keeps_old_client_and_both_helpers(tmp_path):
    dest = tmp_path / "home" / ".local" / "bin"
    dest.mkdir(parents=True)
    old_helper = dest / ("ccfleet-project-files-" + "a" * 64 + ".py")
    old_helper.write_text("old helper\n")
    result, calls, _ = run_install(
        tmp_path, existing_client="old client\n", python_setup="""
import os
original_replace = os.replace
def interrupted_replace(source, destination):
    if str(destination).endswith('/ccfleet'):
        raise OSError('simulated interruption before client replacement')
    return original_replace(source, destination)
os.replace = interrupted_replace
""",
    )
    assert result.returncode != 0
    assert "simulated interruption" in result.stderr
    assert calls == []
    assert (dest / "ccfleet").read_text() == "old client\n"
    assert old_helper.read_text() == "old helper\n"
    assert (dest / f"ccfleet-project-files-{HELPER_DIGEST}.py").read_text() == HELPER_SOURCE
    assert not list(dest.glob(".ccfleet*"))


def test_python_older_than_39_is_rejected_before_downloading(tmp_path):
    result, calls, dest = run_install(
        tmp_path, existing_client="old client\n",
        python_setup="import sys\nsys.version_info = (3, 8, 20)\n",
    )
    assert result.returncode != 0
    assert "Python 3.9 or newer" in result.stderr
    assert calls == []
    assert (dest / "ccfleet").read_text() == "old client\n"
    assert not list(dest.glob(".ccfleet*"))
    assert not list(dest.glob("ccfleet-project-files-*"))


def test_directory_at_helper_destination_does_not_replace_old_client(tmp_path):
    dest = tmp_path / "home" / ".local" / "bin"
    (dest / f"ccfleet-project-files-{HELPER_DIGEST}.py").mkdir(parents=True)
    result, _, _ = run_install(tmp_path, existing_client="old client\n")
    assert result.returncode != 0
    assert (dest / "ccfleet").read_text() == "old client\n"


@pytest.mark.parametrize("ref", ["main", "v2.0", "feature/project-connector"])
def test_helper_url_uses_the_same_source_ref(tmp_path, ref):
    prefix = f"https://raw.githubusercontent.com/cdcupt/ccfleet/{ref}"
    result, _, _ = run_install(tmp_path, download_url=prefix + "/laptop/ccfleet",
                              helper_url_override=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "downloads.log").read_text().splitlines() == [
        prefix + "/laptop/ccfleet", prefix + "/ccfleet_agent/project_files.py",
    ]


def test_explicit_helper_url_overrides_source_ref(tmp_path):
    url = "https://raw.githubusercontent.com/cdcupt/ccfleet/some-ref/laptop/ccfleet"
    result, _, _ = run_install(tmp_path, download_url=url)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "downloads.log").read_text().splitlines() == [
        url, (tmp_path / "fake-project-files.py").as_uri(),
    ]


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
