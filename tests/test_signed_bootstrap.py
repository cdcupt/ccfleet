"""Real shell bootstrap protocol with fake curl and real synthetic release signatures.

The checksum-pinned bootstrap adapter below is TEST-ONLY. It injects an in-memory
fetcher into the real client_release module so no network/provider is contacted.
Crypto, bundle validation, activation and high-water checks remain real.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import client_release as release
from tests import test_client_release as release_cases

signer = release_cases.signer
ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "laptop/install.sh"
CLIENT_URL = release.RAW_BASE + "main/laptop/ccfleet"

BOOTSTRAP_ADAPTER = '''"""SYNTHETIC TEST ONLY: never distribute this helper."""
import base64, importlib.util, json, os
from pathlib import Path

with open(os.environ["TEST_BOOTSTRAP_LOG"], "a") as stream:
    stream.write("verified-helper-executed\\n")

def install_channel(key, destination):
    before = Path(os.environ["TEST_BEFORE_LAUNCHER"])
    target = Path(destination) / "ccfleet"
    if before.exists():
        assert target.read_bytes() == before.read_bytes(), "launcher replaced before signature check"
    else:
        assert not target.exists(), "unverified bootstrap client installed before signature check"
    downloads = Path(os.environ["TEST_DOWNLOAD_LOG"]).read_text().splitlines()
    assert len(downloads) == 8, "all seven helpers must be checked before adapter execution"
    with open(os.environ["TEST_BOOTSTRAP_LOG"], "a") as stream:
        stream.write("signature-check-before-activation\\n")
    spec = importlib.util.spec_from_file_location("synthetic_real_release", os.environ["TEST_RELEASE_MODULE"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = json.loads(Path(os.environ["TEST_SIGNED_FIXTURES"]).read_text())
    def fetch(url, limit):
        raw = base64.b64decode(values[url])
        assert len(raw) <= limit
        return raw
    return module.install_channel(key, Path(destination), fetch=fetch,
                                  now=float(os.environ["TEST_OBSERVATION_TIME"]))
'''


@pytest.fixture
def bootstrap(tmp_path, signer):
    home = tmp_path.resolve() / "home"
    destination = home / ".local/bin"
    destination.mkdir(parents=True, mode=0o700)
    pairing = home / ".config/ccfleet/config.json"
    pairing.parent.mkdir(parents=True, mode=0o700)
    pairing.write_bytes(b'{"devices":{"synthetic":"kept"}}')
    history = home / ".claude/history.jsonl"
    history.parent.mkdir(mode=0o700)
    history.write_bytes(b"synthetic native conversation remains\n")
    before = tmp_path / "before-launcher"
    hook_log, download_log = tmp_path / "hook.log", tmp_path / "downloads.log"
    curl_args = tmp_path / "curl-args.jsonl"
    sources = tmp_path / "sources"
    sources.mkdir()
    payloads, declarations, source_paths = {}, [], {}
    for constant, name in release.HELPERS.items():
        raw = (BOOTSTRAP_ADAPTER if name == "client_release" else
               f'raise RuntimeError("SYNTHETIC {name}: helper must not execute during bootstrap")\n')
        path = sources / (name + ".py")
        path.write_text(raw)
        source_paths[name] = path
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        declarations.append(f'{constant} = "{digest}"')
        payloads[release.RAW_BASE + "main/ccfleet_agent/" + name + ".py"] = str(path)
    client = sources / "ccfleet"
    client.write_text("#!/usr/bin/env python3\n" + "\n".join(declarations) +
                      f"\nRELEASE_PUBLIC_KEY = {signer.public!r}\n" +
                      'raise RuntimeError("SYNTHETIC bootstrap client must not execute")\n')
    payloads[CLIENT_URL] = payloads[client.as_uri()] = str(client)
    mapping = tmp_path / "curl-fixtures.json"
    mapping.write_text(json.dumps(payloads))
    signed_fixtures = tmp_path / "signed-fixtures.json"
    bindir = tmp_path / "commands"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(f'''#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
url = next(a for a in args if a.startswith(("https://", "file://")))
target = args[args.index("-o") + 1]
values = json.loads(Path(os.environ["TEST_CURL_FIXTURES"]).read_text())
assert url in values, "unexpected URL: network is forbidden in this test"
with open(os.environ["TEST_DOWNLOAD_LOG"], "a") as stream:
    stream.write(url + "\\n")
with open(os.environ["TEST_CURL_ARGS"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
shutil.copyfile(values[url], target)
''')
    curl.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("CCFLEET_")}
    env.update(HOME=str(home), SHELL="/bin/bash", CCFLEET_HOME=str(pairing.parent),
               CCFLEET_INSTALL_DIR=str(destination),
               PATH=f"{bindir}:{Path(sys.executable).parent}:{os.defpath}",
               TEST_BOOTSTRAP_LOG=str(hook_log), TEST_DOWNLOAD_LOG=str(download_log),
               TEST_CURL_ARGS=str(curl_args),
               TEST_CURL_FIXTURES=str(mapping), TEST_BEFORE_LAUNCHER=str(before),
               TEST_RELEASE_MODULE=str(ROOT / "ccfleet_agent/client_release.py"),
               TEST_SIGNED_FIXTURES=str(signed_fixtures),
               TEST_OBSERVATION_TIME=str(release_cases.NOW))

    def channel(version="3.0", *, bad_signature=False):
        item = release_cases.distribution(signer, version, version[0] * 40)
        release_cases.signed_channel(item, signer)
        if bad_signature:
            item.values[release.DEFAULT_CHANNEL_SIGNATURE_URL] = signer.sign(
                item.values[release.DEFAULT_CHANNEL_URL], other=True)
        signed_fixtures.write_text(json.dumps({url: base64.b64encode(raw).decode()
                                             for url, raw in item.values.items()}))

    def managed():
        release_cases.install(release_cases.distribution(signer, "1.0"), signer, destination)
        release_cases.install(release_cases.distribution(signer, "2.0", "2" * 40),
                              signer, destination)
        release.rollback(destination, signer.public)
        before.write_bytes((destination / "ccfleet").read_bytes())

    def run(*, custom=False, cwd=None, extra_env=None):
        chosen = {**env, **({"CCFLEET_INSTALL_URL": client.as_uri()} if custom else {}),
                  **(extra_env or {})}
        result = subprocess.run(["bash", str(INSTALLER)], env=chosen,
                                cwd=cwd, capture_output=True, text=True, timeout=30)
        assert pairing.read_bytes() == b'{"devices":{"synthetic":"kept"}}'
        assert history.read_bytes() == b"synthetic native conversation remains\n"
        return result

    channel()
    return SimpleNamespace(destination=destination, client=client, sources=source_paths,
                           hook=hook_log, downloads=download_log, curl_args=curl_args, before=before,
                           channel=channel, managed=managed, run=run)


def test_default_https_bootstrap_activates_verified_release_not_downloaded_client(bootstrap, signer):
    result = bootstrap.run()
    assert result.returncode == 0, result.stderr
    status = release.status(bootstrap.destination, signer.public)
    assert status["version"] == "3.0" and status["signature_verified"]
    assert "SYNTHETIC bootstrap" not in (bootstrap.destination / "ccfleet").read_text()
    assert bootstrap.hook.read_text().splitlines() == [
        "verified-helper-executed", "signature-check-before-activation"]
    assert len(bootstrap.downloads.read_text().splitlines()) == 8
    assert not list(bootstrap.destination.glob(".ccfleet.*"))


def test_bad_channel_signature_preserves_existing_managed_launcher_and_highwater(bootstrap, signer):
    bootstrap.managed()
    bootstrap.channel(bad_signature=True)
    result = bootstrap.run()
    assert result.returncode != 0 and "signature" in result.stderr.lower()
    assert (bootstrap.destination / "ccfleet").read_bytes() == bootstrap.before.read_bytes()
    assert release.status(bootstrap.destination, signer.public)["highest_version"] == "2.0"
    assert "signature-check-before-activation" in bootstrap.hook.read_text()


@pytest.mark.parametrize("helper", list(release.HELPERS.values()))
def test_every_helper_checksum_is_verified_before_release_helper_executes_or_client_changes(
        bootstrap, signer, helper):
    bootstrap.managed()
    path = bootstrap.sources[helper]
    path.write_bytes(path.read_bytes() + b"# tampered download\n")
    result = bootstrap.run()
    assert result.returncode != 0 and "checksum mismatch" in result.stderr
    assert not bootstrap.hook.exists()
    assert (bootstrap.destination / "ccfleet").read_bytes() == bootstrap.before.read_bytes()
    assert release.status(bootstrap.destination, signer.public)["highest_version"] == "2.0"


def test_download_failure_does_not_leave_a_partial_client_install(bootstrap, signer):
    bootstrap.managed()
    bootstrap.sources["local_jobs"].unlink()
    result = bootstrap.run()
    assert result.returncode != 0 and not bootstrap.hook.exists()
    assert (bootstrap.destination / "ccfleet").read_bytes() == bootstrap.before.read_bytes()
    assert release.status(bootstrap.destination, signer.public)["version"] == "1.0"


def test_default_bootstrap_keeps_managed_highwater_when_reinstalling_after_rollback(bootstrap, signer):
    bootstrap.managed()
    bootstrap.channel("1.0")
    result = bootstrap.run()
    assert result.returncode != 0 and "downgrade" in result.stderr
    assert (bootstrap.destination / "ccfleet").read_bytes() == bootstrap.before.read_bytes()
    assert release.status(bootstrap.destination, signer.public)["highest_version"] == "2.0"


def test_default_bootstrap_updates_managed_client_without_erasing_existing_rollback_lineage(
        bootstrap, signer):
    bootstrap.managed()
    result = bootstrap.run()
    assert result.returncode == 0, result.stderr
    status = release.status(bootstrap.destination, signer.public)
    assert status["version"] == status["highest_version"] == "3.0"
    assert status["previous"].startswith("1.0-")
    assert release.rollback(bootstrap.destination, signer.public)["version"] == "1.0"


def test_explicit_custom_source_cannot_replace_managed_launcher(bootstrap, signer):
    bootstrap.managed()
    result = bootstrap.run(custom=True)
    assert result.returncode != 0 and "managed signed release is installed" in result.stderr
    assert not bootstrap.hook.exists()
    assert (bootstrap.destination / "ccfleet").read_bytes() == bootstrap.before.read_bytes()
    assert release.status(bootstrap.destination, signer.public)["highest_version"] == "2.0"


def test_bootstrap_does_not_import_project_or_pythonpath_modules_before_verification(
        bootstrap, signer, tmp_path):
    project = tmp_path / "untrusted-project"
    project.mkdir()
    marker = tmp_path / "untrusted-module-executed"
    (project / "ast.py").write_text(
        "from pathlib import Path\n" + f"Path({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('synthetic project module must not run during installation')\n")
    result = bootstrap.run(cwd=project, extra_env={"PYTHONPATH": str(project)})
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert release.status(bootstrap.destination, signer.public)["signature_verified"]


def test_default_bootstrap_downloads_are_https_only_bounded_and_cannot_follow_redirects(bootstrap):
    result = bootstrap.run()
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in bootstrap.curl_args.read_text().splitlines()]
    assert len(calls) == 8
    for args in calls:
        assert args[args.index("--proto") + 1] == "=https"
        assert 0 < float(args[args.index("--max-time") + 1]) <= 120
        follows = "--location" in args or any(
            value.startswith("-") and not value.startswith("--") and "L" in value for value in args)
        assert not follows or args[args.index("--max-redirs") + 1] == "0"
