import concurrent.futures
import hashlib
import io
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from ccfleet_agent import gemini_editor_distribution as dist


def archive(entries=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, mode in entries or [(n, stat.S_IFREG | 0o755) for n in dist.IDENTITIES]:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            z.writestr(info, name.encode())
    return out.getvalue()


@pytest.fixture
def slot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(dist.mac_policy, "authenticated_owner", lambda *_: None)
    base = dist.mac_policy.directory(home)
    base.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(dist.mac_policy, "_TRUST_ROOT", tmp_path)
    monkeypatch.setattr(dist.sys, "platform", "darwin")
    monkeypatch.setattr(dist.platform, "machine", lambda: "arm64")
    data = archive()
    sha = hashlib.sha256(data).hexdigest()
    monkeypatch.setitem(dist.PLATFORMS, "arm64", ("darwin-aarch64", sha))
    entry = {"cmd": "./agy_acp_server.par", "sha256": sha, "archive": dist._url("1.5.2", "arm64")}
    manifest = {"id": "antigravity-acp", "version": "1.5.2",
                "distribution": {"binary": {"darwin-aarch64": entry}}}
    calls = []
    def fetch(url, dest, limit, timeout):
        calls.append(url)
        dest.write_bytes(json.dumps(manifest).encode() if url == dist.REGISTRY else data)
    checks = []
    def verify(argv, **kwargs):
        assert argv[:4] == ["/usr/bin/codesign", "--verify", "--strict", "-R"]
        requirement, file = argv[4:]
        name = Path(file).name
        assert requirement == (f'=identifier "{dist.IDENTITIES[name]}" and anchor apple generic and '
                               'certificate leaf[subject.OU] = "EQHXZ8M8AV"')
        checks.append(name)
    monkeypatch.setattr(dist.gemini_distribution, "_fetch", fetch)
    monkeypatch.setattr(dist.subprocess, "run", verify)
    return home, entry, manifest, calls, checks


def test_cold_warm_and_concurrent_launches(slot):
    home, _, _, calls, checks = slot
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(dist.ensure_runtime, [home, home])
    assert first == second
    assert first.read_bytes() == b"agy_acp_server.par" and first.with_name("localharness_external").is_file()
    assert calls.count(dist.REGISTRY) == 2
    assert calls.count(dist._url("1.5.2", "arm64")) == 1
    assert checks == list(dist.IDENTITIES) * 2


def test_updates_atomically_across_minor_versions_and_keeps_old_binary(slot, monkeypatch):
    home, entry, manifest, calls, _ = slot
    old = dist.ensure_runtime(home)
    manifest["version"] = "1.6.1"
    entry["archive"] = dist._url("1.6.1", "arm64")
    current = dist.ensure_runtime(home)
    assert current != old and old.is_file()
    manifest["version"] = "1.5.2"
    entry["archive"] = dist._url("1.5.2", "arm64")
    assert dist.ensure_runtime(home) == current and calls.count(entry["archive"]) == 1
    def offline(*_):
        raise dist.gemini_distribution.ArtifactUnavailable("vendor artifact unavailable")
    monkeypatch.setattr(dist.gemini_distribution, "_fetch", offline)
    assert dist.ensure_runtime(home) == current


@pytest.mark.parametrize("change", [{"sha256":"0"*64}, {"cmd":"/bin/sh"},
                                   {"archive":"https://untrusted.invalid/fixture.zip"}, {"args":["x"]}])
def test_changed_baseline_or_unrecognized_source_is_refused(slot, change):
    home, entry, _, calls, _ = slot
    entry.update(change)
    with pytest.raises(ValueError, match="unrecognized"):
        dist.ensure_runtime(home)
    assert calls == [dist.REGISTRY]


def test_invalid_publisher_does_not_activate_release(slot, monkeypatch):
    home, entry, manifest, _, _ = slot
    old = dist.ensure_runtime(home)
    manifest["version"] = "2.0.0"
    entry["archive"] = dist._url("2.0.0", "arm64")
    def reject(argv, **kwargs):
        if argv[-1].endswith("localharness_external"):
            raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(dist.subprocess, "run", reject)
    with pytest.raises(subprocess.CalledProcessError):
        dist.ensure_runtime(home)
    assert old.is_file() and not (old.parent.parent / "2.0.0-arm64").exists()


def test_archive_paths_and_types_are_bounded(tmp_path):
    entries = [(n, stat.S_IFREG | 0o755) for n in dist.IDENTITIES]
    for malicious in (entries + [("../outside", stat.S_IFREG)], [entries[0], (entries[1][0],stat.S_IFLNK)]):
        data = archive(malicious)
        z = tmp_path / "agent.zip"
        z.write_bytes(data)
        with pytest.raises(ValueError, match="archive"):
            dist._extract(z, tmp_path, hashlib.sha256(data).hexdigest())
    assert not (tmp_path.parent / "outside").exists()


def test_refuses_symlink_or_other_user_writable_cache(slot, tmp_path):
    home, _, _, _, _ = slot
    cache = dist.mac_policy.directory(home) / "editor-runtime"
    cache.symlink_to(tmp_path)
    with pytest.raises((OSError, ValueError)):
        dist.ensure_runtime(home)
    cache.unlink()
    cache.mkdir(mode=0o700)
    os.chmod(cache, 0o777)
    with pytest.raises((OSError, ValueError)):
        dist.ensure_runtime(home)
