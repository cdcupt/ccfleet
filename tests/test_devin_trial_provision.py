"""Actual temporary files/flocks, fake NSS/source-profile boundary; no providers."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import devin_trial_provision as t


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = t.provision._encode(value) if isinstance(value, dict) else value
    path.write_bytes(raw)
    path.chmod(mode)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(t.provision, "ROOT_UID", os.geteuid())
    # The fixture is a synthetic protected root. Linux's /tmp is correctly
    # world writable, whereas macOS's per-user temp ancestry is private. Skip
    # only ancestors ABOVE this fixture, retaining real no-follow/owner/mode
    # checks for every directory the refresher actually operates inside it.
    original_directory = t.provision._directory

    def fixture_directory(path):
        path = Path(path)
        try:
            parts = path.relative_to(tmp_path).parts
        except ValueError:
            return original_directory(path)
        fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o022
            ):
                raise t.provision.ProvisionError("unsafe fixture directory")
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o022
                ):
                    raise t.provision.ProvisionError("unsafe fixture directory")
            return fd
        except BaseException:
            os.close(fd)
            raise

    monkeypatch.setattr(t.provision, "_directory", fixture_directory)
    for name, filename in (
        ("REGISTRATION", "provision.json"),
        ("POLICY", "worker.json"),
        ("TEMPLATE", "template.json"),
        ("GRANT", "grant.json"),
    ):
        monkeypatch.setattr(t, name, tmp_path / "policy" / filename)
    source_home, home, state = (tmp_path / name for name in ("owner", "trial", "state"))
    for path in (source_home, home, state, home / ".local/share/devin"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner_credential = source_home / "PRIVATE_credentials"
    write(owner_credential, b"OPAQUE_SOURCE_TOKEN")
    write(home / ".local/share/devin/credentials.toml", b"OLD_TRIAL_TOKEN")
    source = SimpleNamespace(
        pw_name="slot01",
        pw_uid=os.geteuid() + 10000,
        pw_gid=os.getegid() + 10000,
        pw_dir=str(source_home),
    )
    trial = SimpleNamespace(
        pw_name="ccfleet-trial", pw_uid=os.geteuid(), pw_gid=os.getegid(), pw_dir=str(home)
    )

    def owned_directory(path, uid):
        assert uid == trial.pw_uid
        return t.provision._directory(path)

    monkeypatch.setattr(t.provision, "_owned_directory", owned_directory)
    monkeypatch.setattr(t.grp, "getgrgid", lambda _: SimpleNamespace(gr_name="source-private"))
    claim = {
        "owner_id": "owner-a",
        "slot_id": "source-slot",
        "claim_id": "d" * 32,
        "claim_generation": 2,
    }
    runtime = {
        "bridge_contract": "native-agent-v1",
        "native_version": "3000.11.92",
        "adapter_revision": "b" * 64,
        "model_map_digest": "c" * 64,
    }
    control = {"claim": claim, "runtime": runtime, "keys": []}
    source_template = {
        "execution_uid": source.pw_uid,
        "home": source.pw_dir,
        "model_uid": "claude-opus-5-5-max",
        "verified_models": ["claude-opus-5-5-max"],
        "files": {"executable": {"path": "/fixed/devin", "sha256": "e" * 64}},
    }
    profile = {"source_profile_sha256": "f" * 64, "template_sha256": "1" * 64}
    source_calls = []

    def read_credential(actual):
        assert actual is source
        source_calls.append("credential-read")
        return owner_credential.read_bytes()

    monkeypatch.setattr(t.provision, "_credential", read_credential)

    def source_profile(directory, user, actual):
        assert directory == tmp_path / "source-policy" and user == source.pw_name
        assert actual == control
        source_calls.append("profile-read")
        return directory, profile, source_template, {}, None

    monkeypatch.setattr(t.provision, "_profile", source_profile)
    template = {
        "version": 1,
        "enabled": True,
        "execution_uid": trial.pw_uid,
        "home": trial.pw_dir,
        "state_dir": str(state),
        "model_uid": source_template["model_uid"],
        "verified_models": source_template["verified_models"],
        "thought_level": "max",
        "timeout_s": 40,
        "request_models": [t.MODEL],
        "binding": {
            "owner_id": "ccfleet-trial",
            "key_id": "trial-worker",
            "key_generation": 1,
            "slot_id": "virtual-trial",
            "claim_id": "a" * 32,
            "claim_generation": 1,
            "provider_generation": 1,
            "provider": "devin",
        },
        "files": {
            "adapter": {"sha256": "b" * 64},
            "executable": source_template["files"]["executable"],
        },
    }
    policy = {
        "version": 1,
        "enabled": True,
        "config_path": str(t.TEMPLATE),
        "config_sha256": write(t.TEMPLATE, template, 0o640),
        "grant_path": str(t.GRANT),
        "expected_account_fp": "a" * 64,
        "source_claim": claim,
        "source_profile_sha256": "f" * 64,
        "source_runtime": runtime,
    }
    reg = {
        "version": 1,
        "enabled": True,
        "source_user": source.pw_name,
        "source_uid": source.pw_uid,
        "trial_user": trial.pw_name,
        "trial_uid": trial.pw_uid,
        "state_path": str(tmp_path / "machine.json"),
        "policy_dir": str(tmp_path / "source-policy"),
        "worker_policy_sha256": write(t.POLICY, policy, 0o640),
    }
    write(t.REGISTRATION, reg)
    observation = {
        "claim": claim,
        "reason": "verified",
        "checked_at": 1000,
        "cleanup_confirmed": True,
        "config_sha256": "1" * 64,
        "credential_sha256": t.sha(owner_credential.read_bytes()),
        "source_profile_sha256": "f" * 64,
        "report": {
            **runtime,
            "account_fp": "a" * 64,
            "identity_verified": True,
            "observed_at": 1000,
            "capabilities": dict.fromkeys(t.provision.devin_account.CAPABILITIES, True),
        },
    }
    machine = {
        "slots": [source.pw_name],
        "slot_states": {source.pw_name: "claimed"},
        "devin_controls": {source.pw_name: control},
        "devin_observations": {source.pw_name: observation},
    }
    write(Path(reg["state_path"]), machine)
    write(t.GRANT, b"PREVIOUS_GRANT", 0o640)
    def lookup(name):
        return {source.pw_name: source, trial.pw_name: trial}[name]

    def run(**kw):
        return t.refresh(
            now=kw.pop("now", lambda: 1020),
            lookup=lookup,
            groups_of=kw.pop("groups_of", lambda _: set()),
            **kw,
        )
    return SimpleNamespace(**locals())


def test_default_is_inert_and_nonroot_refuses_before_io(scene, monkeypatch, capsys):
    monkeypatch.setattr(t, "refresh", lambda: pytest.fail("default refreshed"))
    assert t.main([]) == 0 and json.loads(capsys.readouterr().out)["execute"] is False
    monkeypatch.undo()
    monkeypatch.setattr(t, "root_json", lambda *a, **k: pytest.fail("non-root read"))
    monkeypatch.setattr(t, "ROOT_UID", os.geteuid() + 1)
    with pytest.raises(ValueError):
        t.refresh()


def test_good_refresh_copies_only_credential_and_preserves_original_native_timestamp(scene):
    s = scene
    original = s.owner_credential.read_bytes(), s.owner_credential.stat().st_mtime_ns
    result = s.run()
    grant = json.loads(t.GRANT.read_bytes())
    assert result == {
        "outcome": "refreshed",
        "credential_copy_attempted": True,
        "provider_calls": 0,
    }
    assert grant["observed_at"] == 1000 != 1020
    assert set(grant) == {
        "version",
        "enabled",
        "config_sha256",
        "credential_sha256",
        "observed_at",
        "account_fp",
        "source_claim",
        "source_profile_sha256",
        "source_runtime",
    }
    assert (s.home / ".local/share/devin/credentials.toml").read_bytes() == original[0]
    assert (s.owner_credential.read_bytes(), s.owner_credential.stat().st_mtime_ns) == original
    assert list(s.state.iterdir()) == [s.state / ".trial.lock"]
    assert s.run(now=lambda: 1030)["outcome"] == "refreshed"
    assert json.loads(t.GRANT.read_bytes()) == grant
    assert b"OPAQUE" not in t.GRANT.read_bytes() and "owner-a" not in json.dumps(result)
    assert t.REGISTRATION.read_bytes() == t.provision._encode(s.reg)


def test_active_source_state_matches_machine_native_observation_contract(scene):
    machine = copy.deepcopy(scene.machine)
    machine["slot_states"][scene.source.pw_name] = "active"
    write(Path(scene.reg["state_path"]), machine)
    assert scene.run()["outcome"] == "refreshed"
    assert json.loads(t.GRANT.read_bytes())["observed_at"] == 1000


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "signed-out",
        "stale",
        "future",
        "fingerprint",
        "claim",
        "runtime",
        "credential",
        "profile",
        "template",
        "unclaimed",
        "many-slots",
    ],
)
def test_bad_source_revokes_before_worker_lock_without_target_mutation(scene, monkeypatch, bad):
    s = scene
    machine = copy.deepcopy(s.machine)
    obs = machine["devin_observations"][s.source.pw_name]
    if bad == "missing":
        machine["devin_observations"] = {}
    elif bad == "signed-out":
        obs["report"]["identity_verified"] = False
    elif bad == "stale":
        obs["checked_at"] = obs["report"]["observed_at"] = 700
    elif bad == "future":
        obs["checked_at"] = obs["report"]["observed_at"] = 1021
    elif bad == "fingerprint":
        obs["report"]["account_fp"] = "2" * 64
    elif bad == "claim":
        machine["devin_controls"][s.source.pw_name]["claim"]["claim_generation"] = 3
    elif bad == "runtime":
        machine["devin_controls"][s.source.pw_name]["runtime"]["native_version"] = "3000.12.0"
    elif bad == "credential":
        obs["credential_sha256"] = "2" * 64
    elif bad == "profile":
        s.profile["source_profile_sha256"] = "2" * 64
    elif bad == "template":
        s.profile["template_sha256"] = "2" * 64
    elif bad == "unclaimed":
        machine["slot_states"][s.source.pw_name] = "releasing"
    else:
        machine["slots"].append("slot02")
    write(Path(s.reg["state_path"]), machine)
    monkeypatch.setattr(t.fcntl, "flock", lambda *_: pytest.fail("BAD tried worker lock"))
    result = s.run()
    assert result["outcome"] == "revoked" and not result["credential_copy_attempted"]
    assert not t.GRANT.exists()
    assert (s.home / ".local/share/devin/credentials.toml").read_bytes() == b"OLD_TRIAL_TOKEN"
    assert s.owner_credential.read_bytes() == b"OPAQUE_SOURCE_TOKEN"


def test_good_busy_skips_all_mutation_but_bad_busy_still_revokes(scene):
    s = scene
    lock = os.open(s.state / ".trial.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert s.run() == {
            "outcome": "busy",
            "credential_copy_attempted": False,
            "provider_calls": 0,
        }
        assert t.GRANT.read_bytes() == b"PREVIOUS_GRANT"
        assert (s.home / ".local/share/devin/credentials.toml").read_bytes() == b"OLD_TRIAL_TOKEN"
        machine = copy.deepcopy(s.machine)
        machine["devin_observations"] = {}
        write(Path(s.reg["state_path"]), machine)
        assert s.run()["outcome"] == "revoked" and not t.GRANT.exists()
    finally:
        os.close(lock)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_any_pending_trial_artifact_revokes_without_reuse_or_deletion(scene, kind):
    path = scene.state / "trial-unconfirmed"
    if kind == "directory":
        path.mkdir()
    elif kind == "file":
        path.write_bytes(b"PRIVATE_OLD_VISITOR_PROMPT")
    else:
        path.symlink_to(scene.source_home)
    assert scene.run()["outcome"] == "revoked" and not t.GRANT.exists()
    assert path.exists() and scene.owner_credential.read_bytes() == b"OPAQUE_SOURCE_TOKEN"
    assert (scene.home / ".local/share/devin/credentials.toml").read_bytes() == b"OLD_TRIAL_TOKEN"


@pytest.mark.parametrize(
    "change", ["source-uid", "trial-uid", "same-uid", "disabled", "extra", "sha"]
)
def test_registration_is_explicit_closed_and_cannot_select_another_identity(scene, change):
    value = dict(scene.reg)
    if change == "source-uid":
        value["source_uid"] += 1
    elif change == "trial-uid":
        value["trial_uid"] += 1
    elif change == "same-uid":
        value["trial_uid"] = value["source_uid"]
    elif change == "disabled":
        value["enabled"] = False
    elif change == "extra":
        value["new_authority"] = True
    else:
        value["worker_policy_sha256"] = "0" * 64
    write(t.REGISTRATION, value)
    assert scene.run()["outcome"] == "revoked" and not t.GRANT.exists()


@pytest.mark.parametrize("group", ["sudo", "docker", "ccfleet-slots", "source-private"])
def test_trial_role_cannot_inherit_privileged_or_owner_group(scene, group):
    assert scene.run(groups_of=lambda _: {group})["outcome"] == "revoked"
    assert not t.GRANT.exists()


@pytest.mark.parametrize(
    "change", ["model", "owner-home", "owner-uid", "binding", "timeout", "version"]
)
def test_trial_template_cannot_become_the_source_owner_or_change_runtime(scene, change):
    template = copy.deepcopy(scene.template)
    if change == "model":
        template["model_uid"] = "different-model"
    elif change == "owner-home":
        template["home"] = scene.source.pw_dir
    elif change == "owner-uid":
        template["execution_uid"] = scene.source.pw_uid
    elif change == "binding":
        template["binding"]["owner_id"] = "owner-a"
    elif change == "timeout":
        template["timeout_s"] = 41
    else:
        template["version"] = True
    policy = dict(scene.policy, config_sha256=write(t.TEMPLATE, template, 0o640))
    reg = dict(scene.reg, worker_policy_sha256=write(t.POLICY, policy, 0o640))
    write(t.REGISTRATION, reg)
    assert scene.run()["outcome"] == "revoked" and not t.GRANT.exists()
    assert scene.owner_credential.read_bytes() == b"OPAQUE_SOURCE_TOKEN"


def test_symlink_lock_cannot_overwrite_owner_and_deadline_fails_closed(scene):
    (scene.state / ".trial.lock").symlink_to(scene.owner_credential)
    assert scene.run()["outcome"] == "revoked"
    assert scene.owner_credential.read_bytes() == b"OPAQUE_SOURCE_TOKEN"
    (scene.state / ".trial.lock").unlink()
    ticks = iter([0, 5])
    assert scene.run(monotonic=lambda: next(ticks))["outcome"] == "revoked"
    assert not t.GRANT.exists()


@pytest.mark.parametrize("change", ["state", "policy", "credential"])
def test_concurrent_change_after_copy_never_reissues_grant(scene, monkeypatch, change):
    original = t.copy_credential

    def copy_then_change(*args):
        original(*args)
        if change == "state":
            write(Path(scene.reg["state_path"]), dict(scene.machine, devin_observations={}))
        elif change == "policy":
            write(t.POLICY, dict(scene.policy, enabled=False), 0o640)
        else:
            scene.owner_credential.write_bytes(b"CHANGED_BY_OWNER")

    monkeypatch.setattr(t, "copy_credential", copy_then_change)
    result = scene.run()
    assert result["outcome"] == "revoked" and result["credential_copy_attempted"]
    assert not t.GRANT.exists()


def test_missing_grant_directory_is_already_revoked(scene):
    old = t.GRANT
    t.GRANT = old.parent / "not-created" / "grant.json"
    try:
        t.revoke()
    finally:
        t.GRANT = old
