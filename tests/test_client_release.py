"""Signed release tests use synthetic Git content and disposable signing keys only."""

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import client_release as release

NOW = 1_800_000_000


def test_project_path_cannot_replace_signature_verifier(tmp_path, signer, monkeypatch):
    fake = tmp_path / "ssh-keygen"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(release.ReleaseError):
        release.verify_signature(b"untrusted release", b"forged signature", signer.public)


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    executable = shutil.which("ssh-keygen")
    if executable is None:
        pytest.skip("OpenSSH signature verification needs ssh-keygen")
    directory = tmp_path_factory.mktemp("synthetic-release-keys").resolve()
    keys = []
    for name in ("trusted", "untrusted"):
        key = directory / name
        subprocess.run([executable, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       capture_output=True, timeout=10, check=True)
        public = " ".join(key.with_suffix(".pub").read_text().split()[:2])
        keys.append((key, public))

    def sign(raw, *, other=False, namespace=release.NAMESPACE):
        path = directory / ("data-" + uuid.uuid4().hex)
        path.write_bytes(raw)
        subprocess.run([executable, "-Y", "sign", "-f", str(keys[int(other)][0]),
                        "-n", namespace, str(path)], capture_output=True, timeout=10, check=True)
        return Path(str(path) + ".sig").read_bytes()

    return SimpleNamespace(sign=sign, key=keys[0][0], public=keys[0][1],
                           other_public=keys[1][1])


def bundle(version="1.0", revision="1" * 40):
    contents = {f"ccfleet_agent/{name}.py": f"VALUE = {name!r}\n".encode()
                for name in release.HELPERS.values()}
    client = "#!/usr/bin/env python3\n" + "\n".join(
        f'{constant} = "{hashlib.sha256(contents[f"ccfleet_agent/{name}.py"]).hexdigest()}"'
        for constant, name in release.HELPERS.items()) + f"\nprint({version!r})\n"
    contents["laptop/ccfleet"] = client.encode()
    manifest = {"schema": 1, "version": version, "revision": revision, "files": [
        {"path": path, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
         "url": release.RAW_BASE + revision + "/" + path}
        for path, raw in sorted(contents.items())]}
    return manifest, contents


def distribution(signer, version="1.0", revision="1" * 40):
    manifest, contents = bundle(version, revision)
    raw = release.canonical(manifest)
    manifest_url = release.RAW_BASE + "a" * 40 + f"/laptop/releases/{version}/manifest.json"
    values = {manifest_url: raw, manifest_url + ".sig": signer.sign(raw)}
    values.update({entry["url"]: contents[entry["path"]] for entry in manifest["files"]})
    calls = []

    def fetch(url, limit):
        calls.append(url)
        result = values[url]
        if isinstance(result, Exception):
            raise result
        assert len(result) <= limit
        return result

    return SimpleNamespace(manifest=manifest, contents=contents, values=values, fetch=fetch,
                           url=manifest_url, calls=calls)


def install(item, signer, destination):
    return release.install(item.url, item.url + ".sig", signer.public, destination,
                           fetch=item.fetch)


def run_client(destination):
    environment = {**os.environ,
                   "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath}
    return subprocess.run([str(destination / "ccfleet")], env=environment,
                          capture_output=True, text=True, timeout=5, check=True).stdout.strip()


def test_launcher_isolates_python_startup_and_preserves_special_paths_and_arguments(tmp_path):
    destination = tmp_path.resolve() / "bin with spaces ' quote and $literal"
    destination.mkdir(mode=0o700)
    identifier = "1.2.3-" + "1" * 64
    state = {"schema": 1, "current": identifier, "previous": None, "highest": identifier}
    target = destination / release.ROOT_NAME / identifier / "ccfleet"
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_text("import json, sys\nprint(json.dumps({"
                      "'arguments': sys.argv[1:], 'isolated': sys.flags.isolated}))\n")
    project = tmp_path / "untrusted-project"
    project.mkdir()
    marker = tmp_path / "startup-executed"
    (project / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('injected')\n")
    launcher = destination / "ccfleet"
    raw = release._launcher(destination, state)
    launcher.write_bytes(raw)
    launcher.chmod(0o700)
    arguments = ["--json", "a b", "quote'and\"double", "$(not-a-command)", "--", "-argument"]
    environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                   "HOME": str(tmp_path), "PYTHONPATH": str(project), "LANG": "C"}
    result = subprocess.run([str(launcher), *arguments], cwd=project, env=environment,
                            capture_output=True, text=True, timeout=5, check=True)
    assert json.loads(result.stdout) == {"arguments": arguments, "isolated": 1}
    assert not marker.exists()
    assert raw.splitlines()[1].startswith(release.LAUNCHER_PREFIX.encode())
    assert release._state(destination) == (state, raw)
    launcher.write_bytes(raw.replace(b"exec python3 -I ", b"exec python3 ", 1))
    with pytest.raises(release.ReleaseError, match="modified"):
        release._state(destination)


@pytest.mark.parametrize("url", [
    "http://raw.githubusercontent.com/cdcupt/ccfleet/" + "1" * 40 + "/laptop/ccfleet",
    release.RAW_BASE + "main/laptop/ccfleet",
    release.RAW_BASE + "1" * 40 + "/../secret",
    release.RAW_BASE + "1" * 40 + "/laptop%2Fccfleet",
    release.RAW_BASE + "1" * 40 + "/laptop/ccfleet?token=private",
    release.RAW_BASE + "1" * 40 + "/laptop/ccfleet#fragment",
    "https://raw.githubusercontent.com.evil.invalid/cdcupt/ccfleet/file",
    "https://github.com/other/ccfleet/releases/download/1.0/manifest.json",
    "https://private@raw.githubusercontent.com/cdcupt/ccfleet/file",
])
def test_release_origin_revision_and_literal_path_are_fixed(url):
    with pytest.raises(release.ReleaseError):
        release.approved_url(url)


def test_only_specific_mutable_signed_channel_is_accepted():
    assert release.approved_url(release.DEFAULT_CHANNEL_URL, channel=True)
    assert release.approved_url(release.DEFAULT_CHANNEL_SIGNATURE_URL, channel=True)
    assert release.approved_url(release.RELEASE_BASE + "1.0/manifest.json")
    with pytest.raises(release.ReleaseError):
        release.approved_url(release.DEFAULT_CHANNEL_URL)


@pytest.mark.parametrize("change", [
    lambda m: m.update(schema=True), lambda m: m.update(key="self-supplied"),
    lambda m: m.update(revision="main"), lambda m: m.update(version="1.00"),
    lambda m: m["files"].append(m["files"][0]),
    lambda m: m["files"][0].update(path="../../outside"),
    lambda m: m["files"][0].update(size=True),
    lambda m: m["files"][0].update(size=release.MAX_FILE + 1),
    lambda m: m["files"][0].update(sha256="not-a-checksum"),
    lambda m: m["files"][0].update(url=release.RAW_BASE + "2" * 40 + "/laptop/ccfleet"),
    lambda m: m.update(files=[]),
])
def test_strict_manifest_rejects_ambiguous_or_unsafe_fields(change):
    manifest, _ = bundle()
    change(manifest)
    with pytest.raises(release.ReleaseError):
        release.parse_manifest(release.canonical(manifest))


def test_duplicate_json_and_oversized_metadata_rejected():
    with pytest.raises(release.ReleaseError):
        release.parse_manifest(b'{"schema":1,"schema":1}')
    with pytest.raises(release.ReleaseError):
        release.parse_manifest(b" " * (release.MAX_MANIFEST + 1))


@pytest.mark.parametrize("mode", ["wrong_key", "wrong_namespace", "tamper", "missing"])
def test_signature_is_verified_against_pinned_key_before_any_bundle_file(signer, tmp_path, mode):
    item = distribution(signer)
    if mode == "wrong_key":
        item.values[item.url + ".sig"] = signer.sign(item.values[item.url], other=True)
    elif mode == "wrong_namespace":
        item.values[item.url + ".sig"] = signer.sign(item.values[item.url], namespace="other-app")
    elif mode == "tamper":
        item.values[item.url] += b" "
    else:
        item.values[item.url + ".sig"] = b""
    with pytest.raises(release.ReleaseError):
        install(item, signer, tmp_path.resolve() / "bin")
    assert item.calls == [item.url, item.url + ".sig"]
    assert not (tmp_path / "bin").exists()


def test_manifest_and_client_must_pin_the_complete_same_helper_set(signer):
    manifest, contents = bundle()
    release.verify_bundle(manifest, contents)
    missing = copy.deepcopy(manifest)
    removed = missing["files"].pop(0)["path"]
    with pytest.raises(release.ReleaseError, match="declarations"):
        release.verify_bundle(missing, {p: b for p, b in contents.items() if p != removed})
    altered = copy.deepcopy(manifest)
    helper = next(entry for entry in altered["files"] if entry["path"] != "laptop/ccfleet")
    alternate = b"VALUE = 'changed'\n"
    helper.update(size=len(alternate), sha256=hashlib.sha256(alternate).hexdigest())
    with pytest.raises(release.ReleaseError, match="client pin"):
        release.verify_bundle(altered, {**contents, helper["path"]: alternate})


@pytest.mark.parametrize("source", [
    b"PROJECT_FILES_SHA256 = 'a' * 64\n", b"OTHER_SHA256 = 'a'\n",
    b"PROJECT_FILES_SHA256: str = '" + b"a" * 64 + b"'\n",
    b"PROJECT_FILES_SHA256 = x = '" + b"a" * 64 + b"'\n",
    b"PROJECT_FILES_SHA256 = '" + b"a" * 64 + b"'\nPROJECT_FILES_SHA256 = '" + b"b" * 64 + b"'\n",
])
def test_computed_unknown_duplicate_and_annotated_helper_digests_rejected(source):
    with pytest.raises(release.ReleaseError):
        release.helper_digests(source)


def test_install_activates_only_complete_verified_bundle_and_reports_version(signer, tmp_path):
    destination = tmp_path.resolve() / "bin"
    item = distribution(signer)
    result = install(item, signer, destination)
    assert result["version"] == "1.0" and result["revision"] == "1" * 40
    assert result["signature_verified"] and not result["rollback_available"]
    assert run_client(destination) == "1.0"
    assert (destination / release.ROOT_NAME).stat().st_mode & 0o077 == 0
    assert len(list((destination / release.ROOT_NAME / result["current"]).glob("*.py"))) == len(release.HELPERS)
    before = (destination / "ccfleet").read_bytes()
    assert install(item, signer, destination) == result
    assert (destination / "ccfleet").read_bytes() == before
    assert release.status(destination)["signature_verified"] is False


@pytest.mark.parametrize("failure", ["checksum", "interrupted", "activation", "durability"])
def test_failed_update_preserves_launcher_pairing_and_history(signer, tmp_path, monkeypatch, failure):
    destination = tmp_path.resolve() / "bin"
    old = distribution(signer)
    install(old, signer, destination)
    config = tmp_path / "config.json"
    config.write_text('{"private_pairing":"synthetic"}')
    history = tmp_path / "history.jsonl"
    history.write_text("synthetic conversation\n")
    baseline = {p: p.read_bytes() for p in (destination / "ccfleet", config, history)}
    new = distribution(signer, "2.0", "2" * 40)
    path = new.manifest["files"][0]["url"]
    if failure == "checksum":
        raw = new.values[path]
        new.values[path] = b"#" + raw[1:]
    elif failure == "interrupted":
        new.values[path] = release.ReleaseError("synthetic interrupted download")
    elif failure == "activation":
        original_replace = release.os.replace

        def fail_activate(source, target, **kwargs):
            if target == "ccfleet":
                raise OSError("synthetic atomic replacement failure")
            return original_replace(source, target, **kwargs)

        monkeypatch.setattr(release.os, "replace", fail_activate)
    else:
        replaced = [False]
        original_replace, original_fsync = release.os.replace, release.os.fsync

        def record_replace(source, target, **kwargs):
            result = original_replace(source, target, **kwargs)
            if target == "ccfleet":
                replaced[0] = True
            return result

        def fail_durability(fd):
            if replaced[0]:
                replaced[0] = False
                monkeypatch.setattr(release.os, "fsync", original_fsync)
                raise OSError("synthetic durability failure after activation")
            return original_fsync(fd)

        monkeypatch.setattr(release.os, "replace", record_replace)
        monkeypatch.setattr(release.os, "fsync", fail_durability)
    with pytest.raises((release.ReleaseError, OSError)):
        install(new, signer, destination)
    assert all(p.read_bytes() == raw for p, raw in baseline.items())
    assert run_client(destination) == "1.0"


def test_legacy_backup_restores_client_and_helpers_without_config_changes(signer, tmp_path):
    destination = tmp_path.resolve() / "bin"
    destination.mkdir()
    manifest, contents = bundle("0.1")
    for entry in manifest["files"]:
        path = destination / release.installed_name(entry["path"], entry["sha256"])
        path.write_bytes(contents[entry["path"]])
    config = tmp_path / "pairing.json"
    config.write_bytes(b"synthetic pairing remains")
    result = install(distribution(signer), signer, destination)
    assert result["previous"].startswith("legacy-")
    reverted = release.rollback(destination, signer.public)
    assert reverted["legacy"] and reverted["highest_version"] == "1.0"
    assert not reverted["signature_verified"] and run_client(destination) == "0.1"
    assert config.read_bytes() == b"synthetic pairing remains"
    assert release.rollback(destination, signer.public)["version"] == "1.0"


def test_rollback_verifies_target_and_preserves_highwater_against_channel_replay(signer, tmp_path):
    destination = tmp_path.resolve() / "bin"
    old, new = distribution(signer), distribution(signer, "2.0", "2" * 40)
    install(old, signer, destination)
    install(new, signer, destination)
    reverted = release.rollback(destination, signer.public)
    assert reverted["version"] == "1.0" and reverted["highest_version"] == "2.0"
    with pytest.raises(release.ReleaseError, match="downgrade"):
        install(old, signer, destination)
    assert install(new, signer, destination)["version"] == "2.0"
    state = release.status(destination, signer.public)
    path = destination / release.ROOT_NAME / state["previous"] / "ccfleet"
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"# tampered\n")
    before = (destination / "ccfleet").read_bytes()
    with pytest.raises(release.ReleaseError):
        release.rollback(destination, signer.public)
    assert (destination / "ccfleet").read_bytes() == before


def test_same_version_different_content_is_not_a_silent_replacement(signer, tmp_path):
    destination = tmp_path.resolve() / "bin"
    install(distribution(signer), signer, destination)
    with pytest.raises(release.ReleaseError, match="same-version"):
        install(distribution(signer, revision="2" * 40), signer, destination)


@pytest.mark.parametrize("kind", ["launcher_symlink", "launcher_hardlink", "storage_symlink",
                                   "storage_public", "parent_symlink"])
def test_symlinks_hardlinks_and_unsafe_storage_cannot_redirect_installation(signer, tmp_path, kind):
    destination = tmp_path.resolve() / "bin"
    destination.mkdir()
    protected = tmp_path / "outside"
    protected.write_bytes(b"must remain")
    if kind == "launcher_symlink":
        (destination / "ccfleet").symlink_to(protected)
    elif kind == "launcher_hardlink":
        os.link(protected, destination / "ccfleet")
    elif kind == "storage_symlink":
        (destination / release.ROOT_NAME).symlink_to(tmp_path, target_is_directory=True)
    elif kind == "storage_public":
        (destination / release.ROOT_NAME).mkdir(mode=0o755)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(destination, target_is_directory=True)
        destination = alias
    with pytest.raises((release.ReleaseError, OSError)):
        install(distribution(signer), signer, destination)
    assert protected.read_bytes() == b"must remain"


def signed_channel(item, signer, **changes):
    channel = {"schema": 1, "version": item.manifest["version"],
               "revision": item.manifest["revision"], "manifest_url": item.url,
               "signature_url": item.url + ".sig", "expires_at": NOW + 3600, **changes}
    raw = release.canonical(channel)
    item.values[release.DEFAULT_CHANNEL_URL] = raw
    item.values[release.DEFAULT_CHANNEL_SIGNATURE_URL] = signer.sign(raw)
    return channel


def test_signed_stable_channel_discovers_immutable_release(signer, tmp_path):
    item = distribution(signer)
    signed_channel(item, signer)
    result = release.install_channel(signer.public, tmp_path.resolve() / "bin",
                                     fetch=item.fetch, now=NOW)
    assert result["version"] == "1.0" and result["signature_verified"]
    assert item.calls[:2] == [release.DEFAULT_CHANNEL_URL, release.DEFAULT_CHANNEL_SIGNATURE_URL]


@pytest.mark.parametrize("changes", [
    {"expires_at": NOW}, {"expires_at": NOW + release.MAX_CHANNEL_AGE + 1},
    {"expires_at": True}, {"expires_at": float("nan")}, {"schema": True},
    {"revision": "main"}, {"version": "latest"}, {"key": "self-supplied"},
    {"manifest_url": release.RAW_BASE + "main/manifest.json"},
    {"signature_url": release.RAW_BASE + "a" * 40 + "/different.sig"},
])
def test_signed_channel_strict_schema_expiry_and_urls_fail_closed(signer, tmp_path, changes):
    item = distribution(signer)
    if isinstance(changes.get("expires_at"), float):
        changes = {"expires_at": None}
    signed_channel(item, signer, **changes)
    with pytest.raises(release.ReleaseError):
        release.install_channel(signer.public, tmp_path.resolve() / "bin",
                                fetch=item.fetch, now=NOW)
    assert not (tmp_path / "bin").exists()


def test_signed_channel_cannot_point_at_other_version_or_replay_after_rollback(signer, tmp_path):
    destination = tmp_path.resolve() / "bin"
    old, new = distribution(signer), distribution(signer, "2.0", "2" * 40)
    install(old, signer, destination)
    install(new, signer, destination)
    release.rollback(destination, signer.public)
    signed_channel(old, signer)
    with pytest.raises(release.ReleaseError, match="downgrade"):
        release.install_channel(signer.public, destination, fetch=old.fetch, now=NOW)
    signed_channel(new, signer, revision="3" * 40)
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.install_channel(signer.public, destination, fetch=new.fetch, now=NOW)


def test_download_refuses_redirect_encoding_and_excess_bytes(monkeypatch):
    url = release.RAW_BASE + "1" * 40 + "/laptop/ccfleet"
    class Reply:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def getcode(self): return 200
        def geturl(self): return url
        def read1(self, size): return b"x" * size

    reply = Reply()
    monkeypatch.setattr(release.urllib.request, "build_opener", lambda *args:
                        SimpleNamespace(open=lambda *a, **k: reply))
    with pytest.raises(release.ReleaseError, match="size"):
        release.download(url, 4)
    reply.geturl = lambda: "https://elsewhere.invalid"
    with pytest.raises(release.ReleaseError, match="redirected"):
        release.download(url, 4)
    reply.geturl = lambda: url
    reply.headers = {"Content-Encoding": "gzip"}
    with pytest.raises(release.ReleaseError, match="encoded"):
        release.download(url, 4)
    assert release._NoRedirect().redirect_request(None, None, 302, None, None, "https://x") is None


def test_download_total_read_deadline_is_not_reset_by_each_chunk(monkeypatch):
    url = release.RAW_BASE + "1" * 40 + "/laptop/ccfleet"
    clock = [0]
    monkeypatch.setattr(release.time, "monotonic", lambda: clock[0])

    class Reply:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def getcode(self): return 200
        def geturl(self): return url
        def read1(self, size):
            clock[0] += 25
            return b"x"

    monkeypatch.setattr(release.urllib.request, "build_opener", lambda *args:
                        SimpleNamespace(open=lambda *a, **k: Reply()))
    with pytest.raises(release.ReleaseError, match="deadline"):
        release.download(url, 100)
    assert clock[0] == 75


def test_an_update_lock_is_nonblocking_and_exclusive(signer, tmp_path):
    destination = tmp_path.resolve() / "bin"
    with release._installation(destination):
        with pytest.raises(release.ReleaseError, match="already running"):
            install(distribution(signer), signer, destination)
    assert not (destination / "ccfleet").exists()


def test_status_and_rollback_fail_clearly_without_an_installed_version(tmp_path, signer):
    destination = tmp_path.resolve() / "bin"
    assert release.status(destination)["installed"] is False
    with pytest.raises(release.ReleaseError, match="no previous"):
        release.rollback(destination, signer.public)


def test_build_tool_reads_committed_sources_not_uncommitted_worktree(signer, tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    manifest, contents = bundle()
    for path, raw in contents.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "synthetic release"], cwd=repo, check=True, capture_output=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "laptop/ccfleet").write_text("DO NOT BUILD DIRTY CONTENT")
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("builder", root / "tools/build_client_release.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    output = tmp_path / "manifest.json"
    result = builder.build(repo, revision, "3.0", output,
                           signing_key=signer.key, public_key=signer.public)
    assert result["signed"] and result["revision"] == revision
    built = release.verify_manifest(output.read_bytes(), Path(str(output) + ".sig").read_bytes(),
                                    signer.public)
    assert next(entry for entry in built["files"] if entry["path"] == "laptop/ccfleet")["sha256"] == \
        hashlib.sha256(contents["laptop/ccfleet"]).hexdigest()
