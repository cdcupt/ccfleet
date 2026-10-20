"""Full pin hashing stays fail-closed without buffering or per-process caching."""
from __future__ import annotations

import hashlib
import io
import json
import os

import pytest

from ccfleet_agent import devin_account as account
from ccfleet_agent import devin_native as native
from tests.test_devin_account import registration
from tests.test_devin_account import scenario as account_scenario

scenario = account_scenario


def pinned(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def test_every_pin_check_streams_the_entire_file_again_without_buffering(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-runtime"
    raw = bytes(range(256)) * 1025
    record = pinned(path, raw)
    reads = []
    original = os.read

    def read(fd, maximum):
        assert 0 < maximum <= 65536
        result = original(fd, maximum)
        reads.append(len(result))
        return result

    monkeypatch.setattr(native.os, "read", read)
    monkeypatch.setattr(native, "_read", lambda *a, **kw: pytest.fail("pin buffered whole file"))
    for checks in (1, 2):
        assert native._pinned(record, uid=os.geteuid(), private=True) == path
        assert sum(reads) == checks * len(raw)
    assert len([size for size in reads if size]) > 2


@pytest.mark.parametrize("kind", ["mode", "owner", "link", "empty", "oversize"])
def test_streaming_keeps_all_original_descriptor_guards_before_read(tmp_path, monkeypatch, kind):
    path = tmp_path / "synthetic-source"
    pinned(path, b"reviewed")
    uid, maximum = os.geteuid(), 20
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "owner":
        uid += 1
    elif kind == "link":
        os.link(path, tmp_path / "second-link")
    elif kind == "empty":
        path.write_bytes(b"")
    else:
        maximum = 3
    monkeypatch.setattr(native.os, "read", lambda *a: pytest.fail("unsafe descriptor read"))
    with pytest.raises(native.NativeError, match="file_guard"):
        native._file_sha256(path, uid=uid, maximum=maximum, private=True)


def test_public_source_mode_and_private_source_mode_are_distinct(tmp_path):
    path = tmp_path / "source"
    record = pinned(path, b"reviewed")
    path.chmod(0o644)
    assert native._pinned(record, uid=os.geteuid()) == path
    with pytest.raises(native.NativeError, match="file_guard"):
        native._pinned(record, uid=os.geteuid(), private=True)
    path.chmod(0o622)
    with pytest.raises(native.NativeError, match="file_guard"):
        native._pinned(record, uid=os.geteuid())


def test_symlink_and_unexpected_path_still_reject_before_hash(tmp_path, monkeypatch):
    path = tmp_path / "source"
    record = pinned(path, b"reviewed")
    link = tmp_path / "link"
    link.symlink_to(path)
    monkeypatch.setattr(native, "_file_sha256", lambda *a, **kw: pytest.fail("unsafe path hashed"))
    with pytest.raises(native.NativeError, match="path_guard"):
        native._pinned({**record, "path": str(link)}, uid=os.geteuid())
    with pytest.raises(native.NativeError, match="source_path_guard"):
        native._pinned(record, expected=tmp_path / "different", uid=os.geteuid())


def test_root_ancestry_and_full_hash_still_run_for_root_sources(tmp_path, monkeypatch):
    path = tmp_path / "source"
    record = pinned(path, b"reviewed")
    calls = []
    monkeypatch.setattr(native, "_root_ancestry", lambda value: calls.append(("ancestry", value)))

    def digest(value, **options):
        calls.append(("digest", value, options))
        return record["sha256"]

    monkeypatch.setattr(native, "_file_sha256", digest)
    assert native._pinned(record) == path
    assert calls == [("ancestry", path), ("digest", path, {
        "uid": 0, "maximum": 256 * 1024 * 1024, "private": False})]


@pytest.mark.parametrize("mutation", ["already-read-byte", "replace", "grow"])
def test_changes_during_streaming_fail_even_if_a_read_prefix_was_already_verified(
        tmp_path, monkeypatch, mutation):
    path = tmp_path / "source"
    raw = b"a" * 131072
    record = pinned(path, raw)
    before = path.stat()
    original = os.read
    changed = []

    def read(fd, maximum):
        part = original(fd, maximum)
        if part and not changed:
            changed.append(True)
            if mutation == "already-read-byte":
                with path.open("r+b") as stream:
                    stream.write(b"b")
                # An attacker can restore mtime, but cannot restore kernel ctime.
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif mutation == "replace":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(raw)
                replacement.chmod(0o600)
                os.replace(replacement, path)
            else:
                with path.open("ab") as stream:
                    stream.write(b"b")
        return part

    monkeypatch.setattr(native.os, "read", read)
    with pytest.raises(native.NativeError, match="file_changed"):
        native._pinned(record, uid=os.geteuid(), private=True)
    assert changed


def test_growth_never_reads_beyond_the_original_safety_bound_plus_one(tmp_path, monkeypatch):
    path = tmp_path / "source"
    pinned(path, b"1234")
    original = os.read
    reads = []

    def read(fd, maximum):
        part = original(fd, maximum)
        reads.append(len(part))
        if len(reads) == 1:
            with path.open("ab") as stream:
                stream.write(b"x" * 100)
        return part

    monkeypatch.setattr(native.os, "read", read)
    with pytest.raises(native.NativeError, match="file_limit"):
        native._file_sha256(path, uid=os.geteuid(), maximum=8, private=True)
    assert sum(reads) == 9


def test_file_descriptor_closes_on_guard_failure_and_digest_mismatch(tmp_path, monkeypatch):
    path = tmp_path / "source"
    record = pinned(path, b"reviewed")
    original = os.open
    descriptors = []

    def opened(*args, **kwargs):
        fd = original(*args, **kwargs)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(native.os, "open", opened)
    with pytest.raises(native.NativeError, match="source_digest_guard"):
        native._pinned({**record, "sha256": "0" * 64}, uid=os.geteuid(), private=True)
    path.chmod(0o644)
    with pytest.raises(native.NativeError, match="file_guard"):
        native._pinned(record, uid=os.geteuid(), private=True)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_public_model_map_digest_keeps_its_validation_contract(scenario, monkeypatch):
    calls = []

    def validate(self):
        calls.append(self)
        return scenario.home, scenario.state

    monkeypatch.setattr(native.NativeConfig, "validate", validate)
    assert account.model_map_digest(scenario.config) == scenario.runtime["model_map_digest"]
    assert calls == [scenario.config]

    def rejected(self):
        raise native.NativeError("policy_changed")

    monkeypatch.setattr(native.NativeConfig, "validate", rejected)
    with pytest.raises(native.NativeError, match="policy_changed"):
        account.model_map_digest(scenario.config)


def test_actual_collector_entrypoint_still_validates_on_every_authorization(scenario, monkeypatch):
    validations = []

    def validate(self):
        validations.append(self)
        return scenario.home, scenario.state

    def load_config(cls, path, *, expected_sha256):
        scenario.config.validate()
        return scenario.config

    monkeypatch.setattr(native.NativeConfig, "validate", validate)
    monkeypatch.setattr(native.NativeConfig, "load", classmethod(load_config))
    monkeypatch.setattr(account.SubjectSourceProfile, "load", classmethod(
        lambda cls, path, **kwargs: scenario.profile))
    output = io.StringIO()
    assert account.main(["--collect-profile"], stdin=io.BytesIO(json.dumps(
        registration(scenario)).encode()), stdout=output, popen_factory=scenario.popen) == 0
    value = json.loads(output.getvalue())
    assert value["reason"] == "verified" and value["cleanup_confirmed"]
    assert len(validations) == 9  # Load + two direct validations + six authorizations.
    assert len(scenario.calls) == 2


def test_revocation_during_capture_still_fails_and_cleans_without_replay(scenario, monkeypatch):
    capture = account._capture

    def revoked(process, **options):
        assert options["permitted"]() is True
        scenario.flags["authorized"] = False
        return capture(process, **options)

    monkeypatch.setattr(account, "_capture", revoked)
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "metadata_authorization_or_timeout"
    assert result.report["account_fp"] is None and result.credential_sha256 is None
    assert len(scenario.calls) == 1 and result.cleanup_confirmed
    assert list(scenario.state.iterdir()) == []
