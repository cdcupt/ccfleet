"""The one-command installer and legacy-client transition."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALL = ROOT / "laptop" / "install.sh"
HELPER_SOURCE = "\"\"\"A pinned project-file helper fixture.\"\"\"\nVALUE = 1\n"
HELPER_DIGEST = hashlib.sha256(HELPER_SOURCE.encode()).hexdigest()


def test_real_installer_loads_all_pinned_release_helpers_without_pairing(tmp_path):
    home = tmp_path / "isolated-home"
    home.mkdir(mode=0o700)
    destination = home / ".local/bin"
    env = {**os.environ, "HOME": str(home), "CCFLEET_HOME": str(home / ".config/ccfleet"),
           "CCFLEET_INSTALL_DIR": str(destination),
           "CCFLEET_INSTALL_URL": (ROOT / "laptop/ccfleet").as_uri(),
           "CCFLEET_PROJECT_FILES_URL": (ROOT / "ccfleet_agent/project_files.py").as_uri(),
           "CCFLEET_LIVE_FILES_URL": (ROOT / "ccfleet_agent/live_files.py").as_uri(),
           "CCFLEET_LIVE_CLIENT_URL": (ROOT / "ccfleet_agent/live_client.py").as_uri()}
    result = subprocess.run(["bash", str(INSTALL)], env=env, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    source = ("import runpy,sys; c=runpy.run_path(sys.argv[1]); "
              "[c[name]() for name in ('project_files','live_files','live_client')]")
    result = subprocess.run(["python3", "-I", "-c", source, str(destination / "ccfleet")],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert len(list(destination.glob("ccfleet-*.py"))) == 3
    assert not (home / ".config/ccfleet").exists()
LIVE_FILES_SOURCE = '"""Synthetic live filesystem helper."""\nVALUE = 2\n'
LIVE_CLIENT_SOURCE = '"""Synthetic live transport helper."""\nVALUE = 3\n'
LIVE_FILES_DIGEST = hashlib.sha256(LIVE_FILES_SOURCE.encode()).hexdigest()
LIVE_CLIENT_DIGEST = hashlib.sha256(LIVE_CLIENT_SOURCE.encode()).hexdigest()


def run_install(tmp_path: Path, *args: str, pair_rc: int = 0,
                old_rc: int = 0, old_client: bool = True,
                helper_source: str = HELPER_SOURCE, helper_missing: bool = False,
                digest_assignment: str | None = None, client_prelude: str = "",
                existing_client: str | None = None, download_url: str | None = None,
                helper_url_override: bool = True, python_setup: str = "",
                setup_rc: int = 0, shell: str = "/bin/zsh",
                extra_env: dict[str, str] | None = None, install_dir: Path | None = None,
                live_helpers: bool = False, live_declarations: str | None = None,
                live_files_source: str = LIVE_FILES_SOURCE,
                live_client_source: str = LIVE_CLIENT_SOURCE, live_missing: str = ""):
    home = tmp_path / "home"
    dest = install_dir or home / ".local" / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    if existing_client is not None:
        (dest / "ccfleet").write_text(existing_client)
    log = tmp_path / "calls.log"
    client = tmp_path / "fake-ccfleet"
    assignment = (digest_assignment if digest_assignment is not None
                  else f"PROJECT_FILES_SHA256 = {HELPER_DIGEST!r}")
    if live_helpers:
        assignment += "\n" + (live_declarations if live_declarations is not None else
                               f"LIVE_FILES_SHA256 = {LIVE_FILES_DIGEST!r}\n"
                               f"LIVE_CLIENT_SHA256 = {LIVE_CLIENT_DIGEST!r}")
    client.write_text("#!/usr/bin/env python3\n" + assignment + "\n" + client_prelude + "\n" + """
import os, sys
with open(os.environ["TEST_LOG"], "a") as stream:
    stream.write("client:" + " ".join(sys.argv[1:]) + "\\n")
code = os.environ.get("PAIR_RC", "0") if sys.argv[1:2] == ["login"] else (
    os.environ.get("SETUP_RC", "0") if sys.argv[1:2] == ["setup"] else "0")
raise SystemExit(int(code))
""")
    client.chmod(0o755)
    helper = tmp_path / "fake-project-files.py"
    if not helper_missing:
        helper.write_text(helper_source)
    live_files = tmp_path / "fake-live-files.py"
    live_client = tmp_path / "fake-live-client.py"
    if live_missing != "live_files":
        live_files.write_text(live_files_source)
    if live_missing != "live_client":
        live_client.write_text(live_client_source)
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
           "SETUP_RC": str(setup_rc), "SHELL": shell,
           "PATH": f"{dest}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    for key in ("CCFLEET_HOME", "CCFLEET_TOKEN_FILE", "ZDOTDIR", "XDG_CONFIG_HOME"):
        env.pop(key, None)
    env.update(extra_env or {})
    if helper_url_override:
        env["CCFLEET_PROJECT_FILES_URL"] = helper.as_uri()
        env["CCFLEET_LIVE_FILES_URL"] = live_files.as_uri()
        env["CCFLEET_LIVE_CLIENT_URL"] = live_client.as_uri()
    else:
        env.pop("CCFLEET_PROJECT_FILES_URL", None)
        env.pop("CCFLEET_LIVE_FILES_URL", None)
        env.pop("CCFLEET_LIVE_CLIENT_URL", None)
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
source = ("CLIENT_SOURCE" if url.endswith("/laptop/ccfleet") else
          "LIVE_FILES_SOURCE" if url.endswith(("/live_files.py", "/fake-live-files.py")) else
          "LIVE_CLIENT_SOURCE" if url.endswith(("/live_client.py", "/fake-live-client.py")) else
          "HELPER_SOURCE")
shutil.copyfile(os.environ[source], sys.argv[4])
""")
        curl.chmod(0o755)
        env.update(DOWNLOAD_LOG=str(tmp_path / "downloads.log"),
                   CLIENT_SOURCE=str(client), HELPER_SOURCE=str(helper),
                   LIVE_FILES_SOURCE=str(live_files), LIVE_CLIENT_SOURCE=str(live_client))
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


def test_live_release_installs_three_verified_versioned_helpers_without_executing(tmp_path):
    result, calls, dest = run_install(tmp_path, live_helpers=True)
    assert result.returncode == 0, result.stderr
    assert calls == []
    for name, digest, source in (
        ("project-files", HELPER_DIGEST, HELPER_SOURCE),
        ("live-files", LIVE_FILES_DIGEST, LIVE_FILES_SOURCE),
        ("live-client", LIVE_CLIENT_DIGEST, LIVE_CLIENT_SOURCE),
    ):
        installed = dest / f"ccfleet-{name}-{digest}.py"
        assert installed.read_text() == source
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest
        assert installed.stat().st_mode & 0o777 == 0o644
    assert not list(dest.glob(".ccfleet*"))


@pytest.mark.parametrize("helper", ["live_files", "live_client"])
@pytest.mark.parametrize("failure", ["download", "tampered", "syntax"])
def test_all_live_helpers_must_validate_before_any_install_or_client_replacement(tmp_path,
                                                                                 helper, failure):
    options = {"live_missing": helper} if failure == "download" else {
        helper + "_source": "def invalid(:\n" if failure == "syntax" else "# tampered\n"}
    if failure == "syntax":
        damaged_digest = hashlib.sha256(options[helper + "_source"].encode()).hexdigest()
        files = damaged_digest if helper == "live_files" else LIVE_FILES_DIGEST
        client = damaged_digest if helper == "live_client" else LIVE_CLIENT_DIGEST
        options["live_declarations"] = (f"LIVE_FILES_SHA256 = {files!r}\n"
                                        f"LIVE_CLIENT_SHA256 = {client!r}")
    result, calls, dest = run_install(tmp_path, "--setup", live_helpers=True,
                                      existing_client="old working client\n", **options)
    assert result.returncode != 0
    assert calls == []
    assert (dest / "ccfleet").read_text() == "old working client\n"
    assert not list(dest.glob("ccfleet-*.py"))
    assert not list(dest.glob(".ccfleet*"))
    assert not (tmp_path / "home/.zshrc").exists()


@pytest.mark.parametrize("declarations", [
    f"LIVE_FILES_SHA256 = {LIVE_FILES_DIGEST!r}",
    f"LIVE_CLIENT_SHA256 = {LIVE_CLIENT_DIGEST!r}",
    f"LIVE_FILES_SHA256 = '0' * 64\nLIVE_CLIENT_SHA256 = {LIVE_CLIENT_DIGEST!r}",
    f"LIVE_FILES_SHA256: str = {LIVE_FILES_DIGEST!r}\n"
    f"LIVE_CLIENT_SHA256 = {LIVE_CLIENT_DIGEST!r}",
    f"LIVE_FILES_SHA256 = {LIVE_FILES_DIGEST!r}\nLIVE_FILES_SHA256 = {LIVE_FILES_DIGEST!r}\n"
    f"LIVE_CLIENT_SHA256 = {LIVE_CLIENT_DIGEST!r}",
    f"LIVE_FILES_SHA256 = {LIVE_FILES_DIGEST!r}\nLIVE_CLIENT_SHA256 = 'bad'",
])
def test_incomplete_or_ambiguous_live_digests_are_rejected(tmp_path, declarations):
    result, calls, dest = run_install(tmp_path, live_helpers=True,
                                      live_declarations=declarations,
                                      existing_client="old working client\n")
    assert result.returncode != 0
    assert calls == []
    assert "LIVE_" in result.stderr
    assert (dest / "ccfleet").read_text() == "old working client\n"
    assert not list(dest.glob("ccfleet-*.py"))


def test_live_helper_validation_never_executes_downloaded_source(tmp_path):
    marker = tmp_path / "must-not-execute"
    source = f"open({str(marker)!r}, 'w').write('unsafe execution')\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    result, calls, _ = run_install(
        tmp_path, live_helpers=True, client_prelude=source,
        live_declarations=f"LIVE_FILES_SHA256 = {digest!r}\nLIVE_CLIENT_SHA256 = {digest!r}",
        live_files_source=source, live_client_source=source)
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert not marker.exists()


def test_live_helper_urls_follow_source_ref_and_explicit_overrides(tmp_path):
    prefix = "https://raw.githubusercontent.com/cdcupt/ccfleet/releases/live"
    result, _, _ = run_install(tmp_path, live_helpers=True,
                               download_url=prefix + "/laptop/ccfleet", helper_url_override=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "downloads.log").read_text().splitlines() == [
        prefix + "/laptop/ccfleet", prefix + "/ccfleet_agent/project_files.py",
        prefix + "/ccfleet_agent/live_files.py", prefix + "/ccfleet_agent/live_client.py",
    ]
    # The same digest-pinned installation can use local helper override URLs.
    (tmp_path / "downloads.log").unlink()
    result, _, _ = run_install(tmp_path, live_helpers=True,
                               download_url=prefix + "/laptop/ccfleet")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "downloads.log").read_text().splitlines()[1:] == [
        (tmp_path / "fake-project-files.py").as_uri(),
        (tmp_path / "fake-live-files.py").as_uri(),
        (tmp_path / "fake-live-client.py").as_uri(),
    ]


def test_live_release_keeps_prior_helper_versions(tmp_path):
    dest = tmp_path / "home/.local/bin"
    dest.mkdir(parents=True)
    previous = dest / ("ccfleet-live-files-" + "a" * 64 + ".py")
    previous.write_text("# old helper kept for old client\n")
    result, _, _ = run_install(tmp_path, live_helpers=True)
    assert result.returncode == 0, result.stderr
    assert previous.read_text() == "# old helper kept for old client\n"


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


def test_setup_reuses_ready_pairing_without_calling_login_and_then_cleans_legacy(tmp_path):
    result, calls, dest = run_install(tmp_path, "--setup", pair_rc=99)
    assert result.returncode == 0, result.stderr
    assert calls == ["client:setup --name computer", "old:--remove"]
    assert "Setup complete" in result.stdout
    assert "ccfleet local" in result.stdout
    assert "No project files were uploaded and no Claude session was started" in result.stdout
    assert str(dest) in (tmp_path / "home/.zshrc").read_text()


def test_setup_passes_optional_name_and_slot_without_shell_interpretation(tmp_path):
    result, calls, _ = run_install(tmp_path, "--setup", "--name", "Personal laptop",
                                    "--slot", "my-slot")
    assert result.returncode == 0, result.stderr
    assert calls[0] == "client:setup --name Personal laptop --slot my-slot"


def test_failed_setup_keeps_legacy_and_startup_file_untouched(tmp_path):
    rc = tmp_path / "home/.zshrc"
    rc.parent.mkdir()
    rc.write_bytes(b"# original configuration without trailing newline")
    result, calls, _ = run_install(tmp_path, "--setup", setup_rc=2)
    assert result.returncode == 1
    assert calls == ["client:setup --name computer"]
    assert rc.read_bytes() == b"# original configuration without trailing newline"
    assert not list(rc.parent.glob(".zshrc.ccfleet-*"))
    assert "legacy setup and PATH were not changed" in result.stderr
    assert "Setup complete" not in result.stdout


def test_setup_cleanup_failure_does_not_configure_path_or_claim_completion(tmp_path):
    result, calls, _ = run_install(tmp_path, "--setup", old_rc=1)
    assert result.returncode == 1
    assert calls == ["client:setup --name computer", "old:--remove"]
    assert "cleanup failed" in result.stderr
    assert not (tmp_path / "home/.zshrc").exists()
    assert "Setup complete" not in result.stdout


@pytest.mark.parametrize("shell, files", [
    ("/bin/zsh", [".zshrc"]),
    ("/bin/bash", [".bash_profile", ".bashrc"]),
    ("/usr/local/bin/fish", [".config/fish/config.fish"]),
])
def test_setup_configures_each_supported_shell_and_is_idempotent(tmp_path, shell, files):
    result, _, dest = run_install(tmp_path, "--setup", old_client=False, shell=shell)
    assert result.returncode == 0, result.stderr
    before = {}
    for name in files:
        rc = tmp_path / "home" / name
        before[name] = rc.read_bytes()
        assert before[name].count(b"# >>> CC Fleet PATH >>>") == 1
        assert before[name].count(b"# <<< CC Fleet PATH <<<") == 1
        assert str(dest).encode() in before[name]
        assert rc.stat().st_mode & 0o777 == 0o600
    result, _, _ = run_install(tmp_path, "--setup", old_client=False, shell=shell)
    assert result.returncode == 0, result.stderr
    for name in files:
        rc = tmp_path / "home" / name
        assert rc.read_bytes() == before[name]
        assert not list(rc.parent.glob(rc.name + ".ccfleet-backup-*"))


def test_path_configuration_preserves_bytes_mode_and_private_backup_without_sourcing(tmp_path):
    rc = tmp_path / "home/.zshrc"
    rc.parent.mkdir()
    marker = tmp_path / "must-not-run"
    original = f"touch '{marker}'\n".encode() + b"# opaque byte \xff without newline"
    rc.write_bytes(original)
    rc.chmod(0o640)
    result, _, _ = run_install(tmp_path, "--setup", old_client=False)
    assert result.returncode == 0, result.stderr
    assert rc.read_bytes().startswith(original + b"\n# >>> CC Fleet PATH >>>\n")
    assert rc.stat().st_mode & 0o777 == 0o640
    backups = list(rc.parent.glob(".zshrc.ccfleet-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert not marker.exists()


def test_bash_preserves_existing_login_startup_choice(tmp_path):
    profile = tmp_path / "home/.profile"
    profile.parent.mkdir()
    profile.write_text("# profile customizations\n")
    result, _, _ = run_install(tmp_path, "--setup", old_client=False, shell="/bin/bash")
    assert result.returncode == 0, result.stderr
    assert not (profile.parent / ".bash_profile").exists()
    assert not (profile.parent / ".bash_login").exists()
    assert "# >>> CC Fleet PATH >>>" in profile.read_text()
    assert (profile.parent / ".bashrc").exists()


@pytest.mark.parametrize("shell, variable, suffix", [
    ("/bin/zsh", "ZDOTDIR", ".zshrc"),
    ("/usr/local/bin/fish", "XDG_CONFIG_HOME", "fish/config.fish"),
])
def test_shell_configuration_respects_owned_locations_inside_home(tmp_path, shell, variable, suffix):
    folder = tmp_path / "home/settings"
    folder.mkdir(parents=True)
    result, _, _ = run_install(tmp_path, "--setup", old_client=False, shell=shell,
                               extra_env={variable: str(folder)})
    assert result.returncode == 0, result.stderr
    assert "# >>> CC Fleet PATH >>>" in (folder / suffix).read_text()


def test_startup_location_outside_home_is_not_modified(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".zshrc").write_text("keep me\n")
    result, _, _ = run_install(tmp_path, "--setup", old_client=False,
                               extra_env={"ZDOTDIR": str(outside)})
    assert result.returncode != 0
    assert "outside your home" in result.stderr
    assert (outside / ".zshrc").read_text() == "keep me\n"
    assert "Setup complete" not in result.stdout


@pytest.mark.parametrize("target_inside", [False, True])
def test_startup_symlink_is_refused_without_replacing_or_touching_target(tmp_path, target_inside):
    home = tmp_path / "home"
    home.mkdir()
    target = (home if target_inside else tmp_path) / "actual-startup"
    target.write_text("# original\n")
    rc = home / ".zshrc"
    rc.symlink_to(target)
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert rc.is_symlink()
    assert target.read_text() == "# original\n"
    assert "symlink" in result.stderr
    assert "manually" in result.stderr


def test_startup_directory_symlink_is_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside-config"
    outside.mkdir()
    (home / ".config").symlink_to(outside, target_is_directory=True)
    result, _, _ = run_install(tmp_path, "--setup", old_client=False, shell="/bin/fish")
    assert result.returncode != 0
    assert not (outside / "fish/config.fish").exists()
    assert "symlink" in result.stderr


def test_unknown_shell_has_actionable_incomplete_status_without_wrong_rc_edit(tmp_path):
    result, _, dest = run_install(tmp_path, "--setup", old_client=False, shell="/bin/nu")
    assert result.returncode != 0
    assert "login shell is not supported" in result.stderr
    assert "PATH setup is incomplete" in result.stderr
    assert str(dest / "ccfleet") in result.stderr
    assert "absolute client path" in result.stderr
    assert not (tmp_path / "home/.profile").exists()
    assert "Setup complete" not in result.stdout


@pytest.mark.parametrize("suffix", ["bad:path", "bad\npath"])
def test_unsafe_install_paths_fail_before_client_execution(tmp_path, suffix):
    result, calls, dest = run_install(tmp_path, "--setup",
                                      install_dir=tmp_path / "home" / suffix)
    assert result.returncode != 0
    assert "install path must not contain" in result.stderr
    assert calls == []
    assert not (dest / "ccfleet").exists()


def test_shell_path_block_quotes_metacharacters_literally(tmp_path):
    dest = tmp_path / "home" / "tools ' $(false) space"
    result, _, _ = run_install(tmp_path, "--setup", old_client=False, shell="/bin/bash",
                               install_dir=dest)
    assert result.returncode == 0, result.stderr
    rc = tmp_path / "home/.bashrc"
    checked = subprocess.run(["bash", "--noprofile", "--norc", "-c",
                              'source "$1"; source "$1"; printf "%s" "$PATH"', "bash", str(rc)],
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                             timeout=10)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.split(":").count(str(dest)) == 1


def test_path_block_prioritizes_new_client_over_an_older_preceding_installation(tmp_path):
    result, _, dest = run_install(tmp_path, "--setup", old_client=False, shell="/bin/bash")
    assert result.returncode == 0, result.stderr
    old = tmp_path / "old-bin"
    old.mkdir()
    (old / "ccfleet").write_text("#!/bin/sh\nexit 99\n")
    (old / "ccfleet").chmod(0o755)
    checked = subprocess.run(["bash", "--noprofile", "--norc", "-c",
                              'source "$1"; source "$1"; command -v ccfleet', "bash",
                              str(tmp_path / "home/.bashrc")],
                             env={"PATH": f"{old}:{dest}:/usr/bin:/bin"},
                             capture_output=True, text=True, timeout=10)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == str(dest / "ccfleet")


def test_atomic_editor_replacement_is_preserved_and_path_setup_reports_incomplete(tmp_path):
    rc = tmp_path / "home/.zshrc"
    rc.parent.mkdir()
    rc.write_text("# old startup\n")
    hook = f"""
import os
from pathlib import Path
target = Path({str(rc)!r})
original_fsync = os.fsync
def racing_fsync(fd):
    original_fsync(fd)
    if target.exists():
        opened, current = os.fstat(fd), target.stat()
        if ((opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
                and b'# >>> CC Fleet PATH >>>' in target.read_bytes()):
            replacement = target.with_suffix('.editor-replacement')
            replacement.write_text('# newer user edits\\n')
            os.replace(replacement, target)
os.fsync = racing_fsync
"""
    result, _, _ = run_install(tmp_path, "--setup", old_client=False, python_setup=hook)
    assert result.returncode != 0
    assert rc.read_text() == "# newer user edits\n"
    assert "replacement was preserved" in result.stderr
    assert "PATH setup is incomplete" in result.stderr
    assert "Setup complete" not in result.stdout
    assert next(rc.parent.glob(".zshrc.ccfleet-backup-*")).read_text() == "# old startup\n"


@pytest.mark.parametrize("original", [
    "# >>> CC Fleet PATH >>>\n# manually changed\n# <<< CC Fleet PATH <<<\n",
    "# >>> CC Fleet PATH >>>",
    "# <<< CC Fleet PATH <<<\r\n",
])
def test_modified_managed_path_block_is_preserved_and_reported(tmp_path, original):
    rc = tmp_path / "home/.zshrc"
    rc.parent.mkdir()
    rc.write_text(original)
    result, _, _ = run_install(tmp_path, "--setup", old_client=False)
    assert result.returncode != 0
    assert rc.read_bytes() == original.encode()
    assert "existing CC Fleet PATH block differs" in result.stderr
    assert not list(rc.parent.glob(".zshrc.ccfleet-backup-*"))


@pytest.mark.parametrize("name", ["token", "token.off", ".ccfleet-token.test"])
def test_setup_refuses_legacy_symlink_before_invoking_cleanup(tmp_path, name):
    directory = tmp_path / "home/.config/ccfleet"
    directory.mkdir(parents=True)
    config = directory / "config.json"
    config.write_text('{"pairing":"keep"}\n')
    (directory / name).symlink_to(config)
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert config.read_text() == '{"pairing":"keep"}\n'
    assert "legacy cleanup is incomplete" in result.stderr
    assert "Existing pairing was kept" in result.stderr


def test_setup_refuses_custom_legacy_token_location(tmp_path):
    custom = tmp_path / "home/.config/ccfleet/config.json"
    custom.parent.mkdir(parents=True)
    custom.write_text('{"pairing":"keep"}\n')
    result, calls, _ = run_install(tmp_path, "--setup",
                                   extra_env={"CCFLEET_TOKEN_FILE": str(custom)})
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "custom token locations" in result.stderr
    assert custom.read_text() == '{"pairing":"keep"}\n'


def test_setup_refuses_predictable_legacy_temporary_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "keep"
    target.write_text("unchanged\n")
    (home / ".zshrc.ccfleet-tmp").symlink_to(target)
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "temporary path already exists" in result.stderr
    assert target.read_text() == "unchanged\n"


@pytest.mark.parametrize("old_client", [False, True])
def test_setup_does_not_claim_legacy_cleanup_when_artifacts_remain(tmp_path, old_client):
    token = tmp_path / "home/.config/ccfleet/token"
    token.parent.mkdir(parents=True)
    token.write_text("synthetic-legacy-marker\n")
    result, calls, _ = run_install(tmp_path, "--setup", old_client=old_client)
    assert result.returncode != 0
    assert calls == (["client:setup --name computer", "old:--remove"] if old_client
                     else ["client:setup --name computer"])
    assert "legacy cleanup is incomplete" in result.stderr
    assert token.read_text() == "synthetic-legacy-marker\n"
    assert "Setup complete" not in result.stdout
    assert not (tmp_path / "home/.zshrc").exists()


def test_setup_with_real_legacy_helper_preserves_new_pairing_keys_and_history(tmp_path):
    home = tmp_path / "home"
    commands = home / ".local/bin"
    commands.mkdir(parents=True)
    old = commands / "ccfleet-connect"
    old.write_bytes((ROOT / "laptop/ccfleet-connect.sh").read_bytes())
    old.chmod(0o755)
    config = home / ".config/ccfleet"
    config.mkdir(parents=True)
    (config / "token").write_text("synthetic-legacy-token\n")
    (config / "config.json").write_text('{"devices":{}}\n')
    for name in ("keys/device-key", "local-history/session.json"):
        target = config / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("preserve this existing data\n")
    rc = home / ".zshrc"
    original = ("# previous user configuration\n# >>> ccfleet connect >>>\n"
                "export CLAUDE_CODE_OAUTH_TOKEN=synthetic\n# <<< ccfleet connect <<<\n"
                "# following user configuration\n")
    rc.write_text(original)
    rc.chmod(0o600)
    result, calls, _ = run_install(tmp_path, "--setup", old_client=False)
    assert result.returncode == 0, result.stderr
    assert calls == ["client:setup --name computer"]
    assert not (config / "token").exists()
    assert "# >>> ccfleet connect >>>" not in rc.read_text()
    assert "# >>> CC Fleet PATH >>>" in rc.read_text()
    assert "# previous user configuration\n# following user configuration\n" in rc.read_text()
    assert rc.stat().st_mode & 0o777 == 0o600
    assert (config / "config.json").read_text() == '{"devices":{}}\n'
    for name in ("keys/device-key", "local-history/session.json"):
        assert (config / name).read_text() == "preserve this existing data\n"
    backups = list(home.glob(".zshrc.ccfleet-legacy-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("field", ["key", "known_hosts"])
@pytest.mark.parametrize("legacy_name", ["token", "token.off", ".ccfleet-token.saved"])
def test_setup_refuses_legacy_path_used_by_any_saved_device(tmp_path, field, legacy_name):
    directory = tmp_path / "home/.config/ccfleet"
    directory.mkdir(parents=True)
    candidate = directory / legacy_name
    candidate.write_text("paired device material\n")
    device = {"key": str(directory / "device-key"),
              "known_hosts": str(directory / "host-pin")}
    device[field] = str(candidate)
    configuration = {"devices": {"preserved": device}, "active": "another-device"}
    config = directory / "config.json"
    config.write_text(json.dumps(configuration))
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "overlaps pairing data" in result.stderr
    assert candidate.read_text() == "paired device material\n"
    assert json.loads(config.read_text()) == configuration
    assert not (tmp_path / "home/.zshrc").exists()


def test_saved_host_pin_symlink_alias_protects_legacy_named_target(tmp_path):
    directory = tmp_path / "home/.config/ccfleet"
    directory.mkdir(parents=True)
    candidate = directory / "token"
    candidate.write_text("host pin material\n")
    alias = directory / "pin-link"
    alias.symlink_to(candidate)
    (directory / "config.json").write_text(json.dumps({"devices": {"d": {
        "key": str(directory / "key"), "known_hosts": str(alias),
    }}}))
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "overlaps pairing data" in result.stderr
    assert candidate.read_text() == "host pin material\n"


def test_custom_ccfleet_home_config_is_not_mistaken_for_old_saved_tokens(tmp_path):
    directory = tmp_path / "home/.config/ccfleet/tokens"
    directory.mkdir(parents=True)
    config = directory / "config.json"
    config.write_text('{"devices":{}}\n')
    result, calls, _ = run_install(tmp_path, "--setup",
                                   extra_env={"CCFLEET_HOME": str(directory)})
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "overlaps pairing data" in result.stderr
    assert config.read_text() == '{"devices":{}}\n'


@pytest.mark.parametrize("preserved", ["local", "project-backups", "local-history"])
def test_retained_history_alias_protects_old_saved_token_directory(tmp_path, preserved):
    directory = tmp_path / "home/.config/ccfleet"
    saved = directory / "tokens"
    saved.mkdir(parents=True)
    note = saved / "history"
    note.write_text("retained history\n")
    (directory / preserved).symlink_to(saved, target_is_directory=True)
    (directory / "config.json").write_text('{"devices":{}}\n')
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "overlaps pairing data" in result.stderr
    assert note.read_text() == "retained history\n"


@pytest.mark.parametrize("data", [
    [], {}, {"devices": []}, {"devices": {"d": []}},
    {"devices": {"d": {"key": None, "known_hosts": "pin"}}},
    {"devices": {"d": {"key": "key", "known_hosts": ""}}},
])
def test_invalid_pairing_metadata_stops_legacy_cleanup(tmp_path, data):
    config = tmp_path / "home/.config/ccfleet/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(data))
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "legacy cleanup is incomplete" in result.stderr
    assert json.loads(config.read_text()) == data


def test_oversized_pairing_metadata_stops_legacy_cleanup(tmp_path):
    config = tmp_path / "home/.config/ccfleet/config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"devices":{},"padding":"' + "x" * (16 * 1024 * 1024) + '"}')
    original_size = config.stat().st_size
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "pairing configuration cannot be safely checked" in result.stderr
    assert config.stat().st_size == original_size


def test_group_writable_home_is_rejected_before_legacy_cleanup(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o770)
    result, calls, _ = run_install(tmp_path, "--setup")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "home directory is unsafe" in result.stderr


def test_startup_file_used_as_saved_device_key_is_not_modified(tmp_path):
    directory = tmp_path / "home/.config/ccfleet"
    directory.mkdir(parents=True)
    key = tmp_path / "home/.bash_profile"
    key.write_text("paired key material\n")
    (directory / "config.json").write_text(json.dumps({"devices": {"d": {
        "key": str(key), "known_hosts": str(directory / "pin"),
    }}}))
    result, calls, _ = run_install(tmp_path, "--setup", shell="/bin/bash")
    assert result.returncode != 0
    assert calls == ["client:setup --name computer"]
    assert "overlaps pairing data" in result.stderr
    assert key.read_text() == "paired key material\n"


@pytest.mark.parametrize("args", [
    ("--wat",), ("--name",), ("--name", "x"), ("--setup", "--migrate"),
    ("--slot", "one"), ("--migrate", "--slot", "one"), ("--setup", "--slot"),
])
def test_bad_installer_arguments_fail_before_downloading(tmp_path, args):
    result, calls, dest = run_install(tmp_path, *args)
    assert result.returncode == 2
    assert calls == [] and not (dest / "ccfleet").exists()


def test_customer_docs_publish_the_one_command_transition():
    from ccfleetd.config import Config
    from ccfleetd.customer_docs import guide

    page = guide(Config())
    assert "laptop/install.sh | bash -s -- --setup" in page
    assert "<pre><code>curl -fsSL" in page
    assert "--setup" in page
