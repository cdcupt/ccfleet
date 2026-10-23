"""Root-only model-free renewal from existing trusted native observations.

An operator must first install the dedicated UID, directories, static policies
and this command. Default invocation is inert. No provider command, account,
grant authority or policy is discovered or created automatically.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import importlib.util
import json
import math
import os
import pwd
import stat
import sys
import time
import uuid
from pathlib import Path

if __package__:
    from . import devin_provision as provision
else:
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_trial_pinned_provision", Path(__file__).resolve().with_name("devin_provision.py")
    )
    provision = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = provision
    exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, "exec"), provision.__dict__)

native = provision.devin_native
ROOT_UID = 0
REGISTRATION = Path("/etc/ccfleet/devin-trial/provision.json")
POLICY = REGISTRATION.with_name("worker.json")
TEMPLATE = REGISTRATION.with_name("template.json")
GRANT = REGISTRATION.with_name("grant.json")
MODEL = "claude-opus-5-5"
MAX_SECONDS = 4.5
PRIVILEGED = frozenset(
    {
        "sudo",
        "admin",
        "wheel",
        "root",
        "docker",
        "lxd",
        "libvirt",
        "kvm",
        "adm",
        "disk",
        "shadow",
        "staff",
        "ccfleet-slots",
    }
)


def need(value):
    if not value:
        raise ValueError("trial_refresh_refused")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def root_json(path, maximum=65536, *, private=False):
    raw = provision._read(path, uid=ROOT_UID, maximum=maximum)
    if private:
        need(stat.S_IMODE(path.lstat().st_mode) == 0o600)
    return native.mcp._decode(raw), raw


def registration(value):
    fields = {
        "version",
        "enabled",
        "source_user",
        "source_uid",
        "trial_user",
        "trial_uid",
        "state_path",
        "policy_dir",
        "worker_policy_sha256",
    }
    need(
        type(value) is dict
        and set(value) == fields
        and type(value["version"]) is int
        and value["version"] == 1
        and value["enabled"] is True
        and type(value["source_user"]) is str
        and provision._USER.fullmatch(value["source_user"])
        and value["trial_user"] == "ccfleet-trial"
        and value["source_user"] != value["trial_user"]
        and all(type(value[key]) is int and value[key] > 0 for key in ("source_uid", "trial_uid"))
        and value["source_uid"] != value["trial_uid"]
        and type(value["worker_policy_sha256"]) is str
        and provision._HEX.fullmatch(value["worker_policy_sha256"])
    )
    for key in ("state_path", "policy_dir"):
        need(
            type(value[key]) is str
            and Path(value[key]).is_absolute()
            and ".." not in Path(value[key]).parts
        )
    return value


def worker_policy(value):
    fields = {
        "version",
        "enabled",
        "config_path",
        "config_sha256",
        "grant_path",
        "expected_account_fp",
        "source_claim",
        "source_profile_sha256",
        "source_runtime",
    }
    need(
        type(value) is dict
        and set(value) == fields
        and type(value["version"]) is int
        and value["version"] == 1
        and value["enabled"] is True
        and value["config_path"] == str(TEMPLATE)
        and value["grant_path"] == str(GRANT)
        and all(
            type(value[key]) is str and provision._HEX.fullmatch(value[key])
            for key in ("config_sha256", "expected_account_fp", "source_profile_sha256")
        )
    )
    provision.clean_control(
        {"claim": value["source_claim"], "runtime": value["source_runtime"], "keys": []}
    )
    return value


def group_names(account):
    gids = os.getgrouplist(account.pw_name, account.pw_gid)
    need(len(gids) <= 64)
    return {grp.getgrgid(gid).gr_name for gid in gids}


def accounts(reg, lookup, groups_of):
    source, trial = lookup(reg["source_user"]), lookup(reg["trial_user"])
    groups = groups_of(trial)
    need(
        source.pw_name == reg["source_user"]
        and source.pw_uid == reg["source_uid"]
        and trial.pw_name == reg["trial_user"]
        and trial.pw_uid == reg["trial_uid"]
        and trial.pw_gid > 0
        and trial.pw_gid != source.pw_gid
        and not groups.intersection(PRIVILEGED)
        and grp.getgrgid(source.pw_gid).gr_name not in groups
    )
    for account in (source, trial):
        need(Path(account.pw_dir).is_absolute() and ".." not in Path(account.pw_dir).parts)
    need(Path(source.pw_dir) != Path(trial.pw_dir))
    return source, trial


def source_proof(reg, policy, source, state, *, now):
    """No collection: cached evidence keeps its original native timestamp."""
    need(
        type(state) is dict
        and state.get("slots") == [source.pw_name]
        and (state.get("slot_states") or {}).get(source.pw_name) in {"claimed", "active"}
    )
    control = provision.clean_control((state.get("devin_controls") or {}).get(source.pw_name))
    need(
        control["claim"] == policy["source_claim"]
        and control["runtime"] == policy["source_runtime"]
    )
    observed = provision.clean_observation(
        (state.get("devin_observations") or {}).get(source.pw_name), control, now=now
    )
    need(
        observed["report"]["account_fp"] == policy["expected_account_fp"]
        and observed["source_profile_sha256"] == policy["source_profile_sha256"]
    )
    _, profile, template, _, _ = provision._profile(
        Path(reg["policy_dir"]), source.pw_name, control
    )
    need(
        profile["source_profile_sha256"] == policy["source_profile_sha256"]
        and observed["config_sha256"] == profile["template_sha256"]
        and template["execution_uid"] == source.pw_uid
        and template["home"] == source.pw_dir
    )
    credential = provision._credential(source)
    need(sha(credential) == observed["credential_sha256"])
    return observed, template, credential


def owned_directory(path, uid):
    fd = provision._owned_directory(path, uid)
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
        os.close(fd)
        raise ValueError("trial_refresh_refused")
    return fd


def trial_template(policy, source_template, trial):
    config, raw = root_json(TEMPLATE)
    need(sha(raw) == policy["config_sha256"] and type(config) is dict)
    binding = native.OwnershipBinding.from_dict(config["binding"])
    need(
        type(config.get("version")) is int
        and config["version"] == 1
        and config.get("enabled") is True
        and type(config.get("execution_uid")) is int
        and config["execution_uid"] == trial.pw_uid
        and config.get("home") == trial.pw_dir
        and binding.owner_id == "ccfleet-trial"
        and binding.key_id == "trial-worker"
        and binding.slot_id == "virtual-trial"
        and config.get("request_models") == [MODEL]
        and config.get("thought_level") == "max"
        and type(config.get("timeout_s")) in (int, float)
        and math.isfinite(config["timeout_s"])
        and 1 <= config["timeout_s"] <= 300
        and config.get("model_uid") == source_template["model_uid"]
        and config.get("verified_models") == source_template["verified_models"]
        and config["files"]["executable"] == source_template["files"]["executable"]
    )
    # Source evidence attests the current provider account, catalogue and ELF;
    # it need not share this independently installed trial adapter. Bind the
    # trial's root manifest to this exact module instead of changing the paid
    # source runtime (which would invalidate unrelated endpoint generations).
    native._pinned(config["files"]["adapter"], expected=Path(native.__file__).resolve(),
                   uid=ROOT_UID)
    home, state = Path(config["home"]), Path(config["state_dir"])
    for path in (home, state, home / ".local/share/devin"):
        fd = owned_directory(path, trial.pw_uid)
        os.close(fd)
    return state


def revoke():
    try:
        fd = provision._directory(GRANT.parent)
    except FileNotFoundError:
        return
    try:
        try:
            os.unlink(GRANT.name, dir_fd=fd)
            os.fsync(fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(fd)


def pending_artifacts(state_fd):
    names = os.listdir(state_fd)
    need(len(names) <= 4096)
    return any(name.startswith("trial-") for name in names)


def copy_credential(trial, raw):
    directory = Path(trial.pw_dir) / ".local/share/devin"
    base = owned_directory(directory, trial.pw_uid)
    temporary = ".trial-credential-" + uuid.uuid4().hex
    fd = None
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=base
        )
        os.fchown(fd, trial.pw_uid, trial.pw_gid)
        remaining = memoryview(raw)
        while remaining:
            count = os.write(fd, remaining)
            need(count > 0)
            remaining = remaining[count:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, "credentials.toml", src_dir_fd=base, dst_dir_fd=base)
        os.fsync(base)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=base)
        except FileNotFoundError:
            pass
        os.close(base)


def current_grant_matches(expected, *, now):
    """Only an exact, still-valid grant can stay visible during renewal."""
    try:
        current, _ = root_json(GRANT, 8192)
        observed_at = current.get("observed_at")
        return (
            type(observed_at) in (int, float)
            and math.isfinite(observed_at)
            and 0 <= now - observed_at <= 300
            and provision._encode(current)
            == provision._encode({**expected, "observed_at": observed_at})
        )
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return False


def publish_grant(grant, trial):
    base = provision._directory(GRANT.parent)
    try:
        provision._replace(base, GRANT.name, provision._encode(grant), trial.pw_gid)
        os.fsync(base)
    finally:
        os.close(base)


def refresh(*, now=time.time, monotonic=time.monotonic, lookup=pwd.getpwnam, groups_of=group_names):
    """One bounded reconciliation. BAD evidence revokes before any worker lock."""
    need(os.geteuid() == ROOT_UID)
    deadline = monotonic() + MAX_SECONDS
    lock = state_fd = None
    copy_attempted = False

    def bounded():
        need(monotonic() <= deadline)

    try:
        reg, reg_raw = root_json(REGISTRATION, 8192, private=True)
        reg = registration(reg)
        policy, policy_raw = root_json(POLICY, 8192)
        need(sha(policy_raw) == reg["worker_policy_sha256"])
        policy = worker_policy(policy)
        source, trial = accounts(reg, lookup, groups_of)
        state, _ = root_json(Path(reg["state_path"]), 4 * 1024 * 1024, private=True)
        observed, source_template, credential = source_proof(reg, policy, source, state, now=now())
        bounded()
        state_path = trial_template(policy, source_template, trial)
        state_fd = owned_directory(state_path, trial.pw_uid)
        grant = {
            "version": 1,
            "enabled": True,
            "config_sha256": policy["config_sha256"],
            "credential_sha256": observed["credential_sha256"],
            "observed_at": observed["checked_at"],
            "account_fp": policy["expected_account_fp"],
            "source_claim": policy["source_claim"],
            "source_profile_sha256": policy["source_profile_sha256"],
            "source_runtime": policy["source_runtime"],
        }

        def confirm():
            # Every path uses current source authority again before publication;
            # a previous GOOD grant never substitutes for fresh native evidence.
            need(
                root_json(REGISTRATION, 8192, private=True)[1] == reg_raw
                and root_json(POLICY, 8192)[1] == policy_raw
            )
            current, _ = root_json(Path(reg["state_path"]), 4 * 1024 * 1024, private=True)
            repeated, _, current_credential = source_proof(reg, policy, source, current, now=now())
            need(repeated == observed and current_credential == credential)
            need(trial_template(policy, source_template, trial) == state_path)
            need(provision._credential(trial) == credential)
            bounded()

        try:
            unchanged = provision._credential(trial) == credential
        except FileNotFoundError:
            unchanged = False
        if (unchanged and current_grant_matches(grant, now=now())
                and not pending_artifacts(state_fd)):
            confirm()
            # A worker can start during confirmation. In that case the lock
            # check below distinguishes active work from idle quarantine.
            if not pending_artifacts(state_fd):
                publish_grant(grant, trial)
                return {"outcome": "refreshed", "credential_copy_attempted": False,
                        "provider_calls": 0}
        try:
            lock = os.open(
                ".trial.lock",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=state_fd,
            )
            os.fchown(lock, trial.pw_uid, trial.pw_gid)
        except FileExistsError:
            lock = os.open(
                ".trial.lock",
                os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=state_fd,
            )
        info = os.fstat(lock)
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == trial.pw_uid
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and info.st_size == 0
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if unchanged and current_grant_matches(grant, now=now()):
                # Update authority only, never credentials or the running
                # HOME. This permits longer turns while retaining the original
                # timestamp from independently refreshed source evidence.
                confirm()
                publish_grant(grant, trial)
                return {"outcome": "refreshed", "credential_copy_attempted": False,
                        "provider_calls": 0}
            return {"outcome": "busy", "credential_copy_attempted": False, "provider_calls": 0}
        need(not pending_artifacts(state_fd))
        bounded()
        # Revoke before a credential change; no worker can begin while locked.
        revoke()
        copy_attempted = True
        copy_credential(trial, credential)
        bounded()
        confirm()
        publish_grant(grant, trial)
        return {
            "outcome": "refreshed",
            "credential_copy_attempted": copy_attempted,
            "provider_calls": 0,
        }
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        revoke()
        return {
            "outcome": "revoked",
            "credential_copy_attempted": copy_attempted,
            "provider_calls": 0,
        }
    finally:
        if lock is not None:
            os.close(lock)
        if state_fd is not None:
            os.close(state_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    if not args.refresh:
        print('{"execute":false,"provider_calls":0,"model_requests":0}')
        return 0
    try:
        result = refresh()
        print(json.dumps({**result, "model_requests": 0}, allow_nan=False))
        return 0
    except Exception:
        print('{"outcome":"refresh_unconfirmed","provider_calls":0,"model_requests":0}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
