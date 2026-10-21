"""Fake root filesystem/spawn boundaries; no native or provider execution."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import devin_account
from ccfleet_agent import devin_provision as p


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(p._encode(value) if isinstance(value, dict) else value)
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    # Actual process UIDs are never changed. Model root-owned files using the
    # test owner and mock only chown and traversal above the private fixture.
    monkeypatch.setattr(p, "ROOT_UID", os.getuid())
    original = p._directory
    def directory(path):
        path = Path(path)
        if str(path).startswith(str(tmp_path)):
            relative = path.relative_to(tmp_path)
            current = tmp_path
            for part in relative.parts:
                current /= part
                info = current.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022
                        or info.st_uid != os.getuid()):
                    raise p.ProvisionError("unsafe fixture directory")
            return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return original(path)
    monkeypatch.setattr(p, "_directory", directory)
    monkeypatch.setattr(p, "_owned_directory", lambda path, uid: directory(path))
    monkeypatch.setattr(p.os, "fchown", lambda *args: None)
    account = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "",
                                str(tmp_path / "slot-home"), "/bin/dash"))
    verifier = pwd.struct_passwd(("ccfleet-devin-verifier", "x", 54321, 54321, "",
                                 str(tmp_path / "verifier-home"), "/usr/sbin/nologin"))
    for home in (account.pw_dir, verifier.pw_dir):
        (Path(home) / ".local/share/devin").mkdir(parents=True, mode=0o700)
    credential = b"FAKE_NATIVE_CREDENTIAL_BYTES"
    write(Path(account.pw_dir) / ".local/share/devin/credentials.toml", credential)
    binding = {"owner_id": "owner-a", "key_id": "metadata-probe", "key_generation": 1,
        "slot_id": "slot-id", "claim_id": "d"*32, "claim_generation": 2,
        "provider_generation": 1, "provider": "devin"}
    template = {"binding": binding, "enabled": False, "execution_uid": account.pw_uid,
        "home": account.pw_dir, "model_uid": "claude-opus-5-5-max",
        "request_models": ["opus"], "verified_models": ["claude-opus-5-5-max"],
        "files": {"adapter": {"sha256": "b"*64}, "executable": {"sha256": "e"*64}}}
    metadata = {**template, "execution_uid": verifier.pw_uid, "home": verifier.pw_dir}
    runtime = {"bridge_contract": "native-agent-v1", "native_version": "3000.11.92",
               "adapter_revision": "b"*64, "model_map_digest": p._model_digest(template)}
    claim = {k: binding[k] for k in ("owner_id", "slot_id", "claim_id", "claim_generation")}
    control = {"claim": claim, "runtime": runtime,
               "keys": [{"key_id": "endpoint-key", "key_generation": 3, "provider_generation": 4}]}
    directory_path = tmp_path / "policy"
    base = directory_path / account.pw_name
    source = {**runtime, "native_sha256": "e"*64, "live_subject_source_verified": True,
        "collector_sha256": hashlib.sha256(Path(devin_account.__file__).read_bytes()).hexdigest()}
    source_path = tmp_path / "source.json"
    registration = {"version": 1, "enabled": True,
        "config_path": str(base / "metadata.json"), "config_sha256": write(base / "metadata.json", metadata),
        "template_path": str(base / "template.json"), "template_sha256": write(base / "template.json", template),
        "verifier_user": verifier.pw_name, "source_profile_path": str(source_path),
        "source_profile_sha256": write(source_path, source)}
    write(base / "profile.json", registration)
    digest = hashlib.sha256(credential).hexdigest()
    report = {**runtime, "account_fp": "a"*64, "identity_verified": True,
        "capabilities": dict.fromkeys(devin_account.CAPABILITIES, True), "observed_at": 1000}
    output = {"report": report, "credential_sha256": digest, "checked_at": 1000,
              "reason": "verified", "cleanup_confirmed": True}
    calls = []
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0, json.dumps(output)
    c = SimpleNamespace(directory=directory_path, base=base, account=account, verifier=verifier,
        control=control, output=output, registration=registration, template=template,
        metadata=metadata, calls=calls, spawn=spawn, now=1000,
        lookup=lambda name: verifier if name == verifier.pw_name else None,
        groups_of=lambda account: set(), source=source, source_path=source_path)
    c.collect = lambda **changes: p.collect(c.directory, c.account, c.control,
        spawn=changes.get("spawn", c.spawn), lookup=changes.get("lookup", c.lookup),
        groups_of=changes.get("groups_of", c.groups_of), now=c.now)
    return c


def test_dedicated_verifier_snapshot_and_original_guard_are_private(profile):
    c = profile
    observed = c.collect()
    [(argv, kwargs)] = c.calls
    assert argv == ["/usr/bin/python3", "-I", str(Path(devin_account.__file__).resolve()), "--collect-profile"]
    assert kwargs["user"] == c.verifier.pw_uid != c.account.pw_uid
    assert kwargs["extra_groups"] == [] and kwargs["env"]["HOME"] == c.verifier.pw_dir
    assert "FAKE_NATIVE" not in kwargs["input_text"]
    assert not (Path(c.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()
    public = p.public_report(observed)
    assert set(public) == {"claim_id", "claim_generation", "report"}
    assert "credential_sha256" not in json.dumps(public) and "config_sha256" not in json.dumps(public)


def test_metadata_parent_budget_leaves_margin_for_validation_and_cleanup(profile):
    profile.collect()
    timeout = profile.calls[0][1]["timeout"]
    assert timeout == devin_account.METADATA_CHILD_TIMEOUT_S == 90
    cleanup_for_both_commands = 2 * (devin_account.METADATA_TERMINATE_GRACE_S
                                    + devin_account.METADATA_PROCESS_EXIT_TIMEOUT_S)
    assert timeout - devin_account.METADATA_ACQUISITION_TIMEOUT_S > cleanup_for_both_commands


@pytest.mark.parametrize("reason", [
    "native_status_unknown_label_sha256_" + "a" * 64,
    "native_status_duplicate_label_user_id", "native_status_duplicate_label_primary_org",
    "native_auth_status_rejected", "native_models_list_rejected",
])
def test_safe_child_failure_diagnostics_reach_the_log_boundary(profile, reason):
    profile.output.update(reason=reason, report=None, credential_sha256=None)

    def failed_child(argv, **kwargs):
        _, output = profile.spawn(argv, **kwargs)
        return 1, output

    with pytest.raises(p.ProvisionError) as captured:
        profile.collect(spawn=failed_child)
    assert p.metadata_failure_reason(captured.value) == reason
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


@pytest.mark.parametrize("reason", [
    None, True, False, 42, [], {}, b"native_status_duplicate_label_user_id",
    "private-native-output@example.test", "native_status_duplicate_label_private_secret",
    "native_status_duplicate_label_User_ID", "native_status_duplicate_label_user_id\n",
    "native_status_unknown_label_sha256_" + "a" * 63,
    "native_status_unknown_label_sha256_" + "a" * 65,
    "native_status_unknown_label_sha256_" + "A" * 64,
    "native_status_unknown_label_sha256_" + "a" * 64 + "\n",
    "native_status_unknown_label_sha256_" + "a" * 64 + "private-output",
])
def test_parent_rejects_unbounded_or_forged_failure_diagnostic(reason):
    error = p.ProvisionError("private native text", collector_reason=reason)
    assert error.collector_reason is None and p.metadata_failure_reason(error) == "unavailable"
    # The log boundary independently rejects mutation after construction.
    error.collector_reason = reason
    assert p.metadata_failure_reason(error) == "unavailable"


def test_freshness_uses_clock_after_metadata_child_completion(profile):
    c = profile
    c.output["checked_at"] = c.output["report"]["observed_at"] = 1010
    observed = p.collect(c.directory, c.account, c.control, spawn=c.spawn,
        lookup=c.lookup, groups_of=c.groups_of, now=lambda: 1011)
    assert observed["checked_at"] == 1010


@pytest.mark.parametrize("case", ["same_uid", "shell", "group", "missing"])
def test_interactive_or_shared_verifier_never_launches(profile, case):
    c = profile
    fields = list(c.verifier)
    if case == "same_uid":
        fields[2] = c.account.pw_uid
    if case == "shell":
        fields[6] = "/bin/bash"
    verifier = pwd.struct_passwd(fields)
    with pytest.raises(p.ProvisionError):
        c.collect(lookup=lambda name: None if case == "missing" else verifier,
                  groups_of=lambda account: {"ccfleet-slots"} if case == "group" else set())
    assert c.calls == []


@pytest.mark.parametrize("case", ["digest", "source_changed", "cleanup", "credential_changed"])
def test_unverified_or_changed_account_never_returns_readiness(profile, case):
    c = profile
    if case == "digest":
        c.output["credential_sha256"] = "e"*64
    if case == "cleanup":
        c.output["cleanup_confirmed"] = False
    def spawn(argv, **kwargs):
        if case == "source_changed":
            write(c.base / "profile.json", {**c.registration, "enabled": False})
        if case == "credential_changed":
            write(Path(c.account.pw_dir) / ".local/share/devin/credentials.toml", b"CHANGED")
        return c.spawn(argv, **kwargs)
    with pytest.raises((p.ProvisionError, ValueError)):
        c.collect(spawn=spawn)
    snapshot = Path(c.verifier.pw_dir) / ".local/share/devin/credentials.toml"
    assert snapshot.exists() is (case == "cleanup")


@pytest.mark.parametrize("field", ["model_uid", "request_models", "verified_models", "adapter", "executable"])
def test_slot_template_cannot_advertise_a_different_verified_runtime(profile, field):
    c = profile
    if field in {"adapter", "executable"}:
        c.template["files"][field]["sha256"] = "f"*64
    else:
        c.template[field] = "different" if field == "model_uid" else ["different"]
    digest = write(c.base / "template.json", c.template)
    write(c.base / "profile.json", {**c.registration, "template_sha256": digest})
    with pytest.raises((p.ProvisionError, ValueError)):
        c.collect()
    assert c.calls == []


def test_grants_exact_bindings_modes_idempotence_and_revocation(profile):
    c = profile
    observed = c.collect()
    p.reconcile(c.directory, c.account, c.control, observed, now=c.now)
    stem = hashlib.sha256(b"endpoint-key").hexdigest()
    grant_path, config_path = c.base / (stem + ".grant.json"), c.base / (stem + ".json")
    grant = json.loads(grant_path.read_bytes())
    config = json.loads(config_path.read_bytes())
    expected = {**c.control["claim"], **c.control["keys"][0], "provider": "devin"}
    assert grant["binding"] == config["binding"] == expected
    assert config["execution_uid"] == c.account.pw_uid and config["enabled"] is True
    assert grant["runtime"] == {**c.control["runtime"], "provider_account_fp": "a"*64}
    assert stat.S_IMODE(grant_path.stat().st_mode) == stat.S_IMODE(config_path.stat().st_mode) == 0o640
    before = grant_path.stat().st_ino, config_path.stat().st_ino
    p.reconcile(c.directory, c.account, c.control, observed, now=c.now)
    assert (grant_path.stat().st_ino, config_path.stat().st_ino) == before
    p.reconcile(c.directory, c.account, {**c.control, "keys": []}, observed, now=c.now)
    assert not grant_path.exists()


@pytest.mark.parametrize("case", ["disabled", "stale", "missing_observation", "credential", "claim"])
def test_invalid_evidence_removes_existing_grant_before_any_new_authority(profile, case):
    c = profile
    observed = c.collect()
    p.reconcile(c.directory, c.account, c.control, observed, now=c.now)
    if case == "disabled":
        write(c.base / "profile.json", {**c.registration, "enabled": False})
    if case == "stale":
        c.now += 241
    if case == "missing_observation":
        observed = None
    if case == "credential":
        write(Path(c.account.pw_dir) / ".local/share/devin/credentials.toml", b"CHANGED")
    if case == "claim":
        c.control["claim"]["claim_id"] = "f"*32
    p.reconcile(c.directory, c.account, c.control, observed, now=c.now)
    assert list(c.base.glob("*.grant.json")) == []


@pytest.mark.parametrize("case", ["symlink", "hardlink", "public_write"])
def test_untrusted_operator_profile_never_launches(profile, case):
    c = profile
    path = c.base / "profile.json"
    if case == "symlink":
        other = c.base / "other.json"
        path.rename(other)
        path.symlink_to(other)
    elif case == "hardlink":
        os.link(path, c.base / "other.json")
    else:
        path.chmod(0o666)
    with pytest.raises((OSError, ValueError)):
        c.collect()
    assert c.calls == []
