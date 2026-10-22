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
        groups_of=changes.get("groups_of", c.groups_of), now=c.now,
        monotonic=changes.get("monotonic", lambda: 0.0))
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


def catalogue_rejection(profile):
    value = json.loads(json.dumps(profile.output))
    value.update(reason="native_models_list_rejected", credential_sha256=None)
    value["report"].update(identity_verified=False, account_fp=None)
    return value


@pytest.mark.parametrize("elapsed,attempts", [
    (19, 2), (20, 2), (21.5, 2), (25, 2), (70, 2), (70.001, 1),
])
def test_early_clean_catalogue_rejection_gets_only_one_full_fresh_child_retry(
        profile, caplog, elapsed, attempts):
    now = [0.0]
    failed = catalogue_rejection(profile)

    def spawn(argv, **kwargs):
        result = profile.spawn(argv, **kwargs)
        assert (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()
        if len(profile.calls) == 1:
            now[0] = elapsed
            return 1, json.dumps(failed)
        return result

    with caplog.at_level("INFO", logger=p.log.name):
        if attempts == 2:
            assert profile.collect(spawn=spawn, monotonic=lambda: now[0])["reason"] == "verified"
        else:
            with pytest.raises(p.ProvisionError) as captured:
                profile.collect(spawn=spawn, monotonic=lambda: now[0])
            assert p.metadata_failure_reason(captured.value) == "native_models_list_rejected"
    assert len(profile.calls) == attempts
    assert profile.calls[0][1]["timeout"] == 90
    first_request = json.loads(profile.calls[0][1]["input_text"])
    assert first_request.pop("acquisition_timeout_s") == 60
    if attempts == 2:
        assert profile.calls[1][1]["timeout"] == 90 - elapsed
        assert profile.calls[0][0] == profile.calls[1][0]
        second_request = json.loads(profile.calls[1][1]["input_text"])
        assert second_request.pop("acquisition_timeout_s") == min(60, 80 - elapsed)
        assert first_request == second_request
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()
    assert [record.message for record in caplog.records] == (
        ["native metadata catalogue verification retry"] if attempts == 2 else [])


def test_catalogue_retry_cannot_accept_good_after_original_parent_deadline(profile):
    now = [0.0]

    def spawn(argv, **kwargs):
        result = profile.spawn(argv, **kwargs)
        if len(profile.calls) == 1:
            now[0] = 19
            return 1, json.dumps(catalogue_rejection(profile))
        assert kwargs["timeout"] == 71
        now[0] = 91
        return result

    with pytest.raises(p.ProvisionError) as captured:
        profile.collect(spawn=spawn, monotonic=lambda: now[0])
    assert p.metadata_failure_reason(captured.value) == "native_metadata_timeout"
    assert len(profile.calls) == 2
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


def test_final_observation_validation_cannot_publish_after_parent_deadline(profile, monkeypatch):
    now = [0.0]
    validate = p.clean_observation

    def slow_validation(*args, **kwargs):
        observed = validate(*args, **kwargs)
        now[0] = 91
        return observed

    monkeypatch.setattr(p, "clean_observation", slow_validation)
    with pytest.raises(p.ProvisionError) as captured:
        profile.collect(monotonic=lambda: now[0])
    assert p.metadata_failure_reason(captured.value) == "native_metadata_timeout"
    assert len(profile.calls) == 1
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


def test_second_catalogue_failure_is_bad_without_a_third_child(profile):
    def spawn(argv, **kwargs):
        profile.spawn(argv, **kwargs)
        return 1, json.dumps(catalogue_rejection(profile))

    with pytest.raises(p.ProvisionError) as captured:
        profile.collect(spawn=spawn)
    assert p.metadata_failure_reason(captured.value) == "native_models_list_rejected"
    assert len(profile.calls) == 2
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


@pytest.mark.parametrize("code,reason,cleanup,schema", [
    (1, "native_auth_status_rejected", True, "valid"),
    (1, "native_model_catalog_unverified", True, "valid"),
    (1, "metadata_authorization_revoked", True, "valid"),
    (1, "native_status_subject_invalid", True, "valid"),
    (1, "native_status_unknown_label_sha256_" + "a" * 64, True, "valid"),
    (1, "native_models_list_rejected", False, "valid"),
    (2, "native_models_list_rejected", True, "valid"),
    (-15, "native_models_list_rejected", True, "valid"),
    (True, "native_models_list_rejected", True, "valid"),
    (None, "native_models_list_rejected", True, "valid"),
    (1, "native_models_list_rejected", True, "missing-report"),
    (1, "native_models_list_rejected", True, "extra-field"),
    (1, "native_models_list_rejected", True, "negative-time"),
    (1, "native_models_list_rejected", True, "future-time"),
    (1, "native_models_list_rejected", True, "stale-time"),
    (1, "native_models_list_rejected", True, "bool-observed"),
])
def test_only_exact_clean_code_one_catalogue_failure_is_retryable(
        profile, code, reason, cleanup, schema):
    failed = catalogue_rejection(profile)
    failed.update(reason=reason, cleanup_confirmed=cleanup)
    if schema == "missing-report":
        failed["report"] = None
    if schema == "extra-field":
        failed["unexpected"] = "private-output"
    if schema in {"negative-time", "future-time", "stale-time"}:
        timestamp = {"negative-time": -1, "future-time": profile.now + 1,
                     "stale-time": profile.now - 241}[schema]
        failed["checked_at"] = failed["report"]["observed_at"] = timestamp
    if schema == "bool-observed":
        profile.now = failed["checked_at"] = 1
        failed["report"]["observed_at"] = True

    def spawn(argv, **kwargs):
        profile.spawn(argv, **kwargs)
        return code, json.dumps(failed)

    with pytest.raises(p.ProvisionError):
        profile.collect(spawn=spawn)
    assert len(profile.calls) == 1
    snapshot = Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml"
    assert snapshot.exists() is (not cleanup)


@pytest.mark.parametrize("change", ["credential", "profile", "source-pin", "control",
                                    "verifier-uid", "group"])
def test_catalogue_retry_revalidates_original_source_and_complete_verifier_setup(profile, change):
    def spawn(argv, **kwargs):
        profile.spawn(argv, **kwargs)
        if change == "credential":
            write(Path(profile.account.pw_dir) / ".local/share/devin/credentials.toml", b"changed")
        elif change == "profile":
            write(profile.base / "profile.json", {**profile.registration, "enabled": False})
        elif change == "source-pin":
            changed = write(profile.source_path, {**profile.source, "proof_sha256": "e" * 64})
            write(profile.base / "profile.json", {**profile.registration,
                                                 "source_profile_sha256": changed})
        elif change == "control":
            profile.control["keys"] = []
        return 1, json.dumps(catalogue_rejection(profile))

    def lookup(name):
        verifier = profile.lookup(name)
        if profile.calls and change == "verifier-uid":
            fields = list(verifier)
            fields[2] += 1
            return pwd.struct_passwd(fields)
        return verifier

    def groups(account):
        return {"sudo"} if profile.calls and change == "group" else set()

    with pytest.raises((p.ProvisionError, ValueError)):
        profile.collect(spawn=spawn, lookup=lookup, groups_of=groups)
    assert len(profile.calls) == 1
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


@pytest.mark.parametrize("case", ["source-uid", "source-home", "verifier-group", "verifier-home"])
def test_original_identity_refusal_still_precedes_source_credential_read(profile, monkeypatch, case):
    if case in {"source-uid", "source-home"}:
        profile.template["execution_uid" if case == "source-uid" else "home"] = (
            profile.account.pw_uid + 1 if case == "source-uid" else "/unexpected-home")
        changed = write(profile.base / "template.json", profile.template)
        write(profile.base / "profile.json", {**profile.registration, "template_sha256": changed})

    def lookup(name):
        verifier = profile.lookup(name)
        if case == "verifier-home":
            fields = list(verifier)
            fields[5] = "/unexpected-verifier-home"
            return pwd.struct_passwd(fields)
        return verifier

    monkeypatch.setattr(p, "_credential", lambda *_: pytest.fail("refused identity read credential"))
    with pytest.raises(p.ProvisionError):
        profile.collect(lookup=lookup, groups_of=lambda _: {"sudo"} if case == "verifier-group"
                        else set())
    assert not profile.calls


def test_budget_drop_during_second_snapshot_copy_prevents_spawn_and_removes_unlaunched_snapshot(
        profile, monkeypatch):
    now, copies = [0.0], []

    def sync(fd):
        copies.append(fd)
        if len(copies) == 2:
            now[0] = 70.001

    def spawn(argv, **kwargs):
        profile.spawn(argv, **kwargs)
        now[0] = 19
        return 1, json.dumps(catalogue_rejection(profile))

    monkeypatch.setattr(p.os, "fsync", sync)
    with pytest.raises(p.ProvisionError) as captured:
        profile.collect(spawn=spawn, monotonic=lambda: now[0])
    assert len(profile.calls) == 1 and len(copies) == 2
    assert p.metadata_failure_reason(captured.value) == "native_models_list_rejected"
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


def test_first_snapshot_cannot_launch_without_minimum_acquisition_and_cleanup_room(
        profile, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(p.os, "fsync", lambda _: now.__setitem__(0, 70.001))
    with pytest.raises(p.ProvisionError) as captured:
        profile.collect(monotonic=lambda: now[0])
    assert p.metadata_failure_reason(captured.value) == "native_metadata_timeout"
    assert not profile.calls
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


def test_parent_setup_time_shortens_first_child_without_extending_hard_deadline(
        profile, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(p.os, "fsync", lambda _: now.__setitem__(0, 25))
    assert profile.collect(monotonic=lambda: now[0])["reason"] == "verified"
    request = json.loads(profile.calls[0][1]["input_text"])
    assert request["acquisition_timeout_s"] == 55
    assert profile.calls[0][1]["timeout"] == 65


@pytest.mark.parametrize("failure", ["exception", "missing-result"])
def test_uncertain_spawn_or_missing_result_is_not_retried_and_keeps_quarantine(profile, failure):
    def spawn(argv, **kwargs):
        profile.spawn(argv, **kwargs)
        if failure == "exception":
            raise OSError("synthetic uncertain spawn")
        return None, None

    with pytest.raises((p.ProvisionError, OSError)):
        profile.collect(spawn=spawn)
    assert len(profile.calls) == 1
    assert (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


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
