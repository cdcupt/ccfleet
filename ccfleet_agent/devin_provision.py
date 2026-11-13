"""Root-only native profile collection and expiring per-key grants.

An operator installs profile.json and metadata.json; this module never creates
an account, credential, executable or operator opt-in. Slot-facts JSON is never
an input. Metadata execution occurs only through the machine's explicit spawn
boundary as a separate, unprivileged verification principal.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

if __package__:
    from . import devin_account
else:
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_provision_pinned_account", Path(__file__).resolve().with_name("devin_account.py"))
    devin_account = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = devin_account
    exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, "exec"), devin_account.__dict__)
devin_native = devin_account.native

ROOT_UID = 0
DEFAULT_DIRECTORY = Path("/etc/ccfleet/devin")
MAX_KEYS = 20
_METADATA_CLEANUP_RESERVE_SECONDS = 10
_RETRY_MINIMUM_SECONDS = (devin_account.MIN_METADATA_ACQUISITION_TIMEOUT_S
                          + _METADATA_CLEANUP_RESERVE_SECONDS)
log = logging.getLogger(__name__)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_USER = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z")
# Closed codes, known-label enums or exact label hashes only. Never treat
# exception text or native output as a diagnostic: plausible strings can leak secrets.
# Retain old combined codes for installed collectors while newer collectors
# identify the failing metadata command or label shape without copying output.
COLLECTOR_FAILURE_REASONS = frozenset({
    "account_profile_or_authorization_changed", "invalid_metadata_factory",
    "invalid_native_api_origin", "invalid_source_profile", "invalid_source_profile_origin",
    "metadata_authorization_or_timeout", "metadata_authorization_revoked",
    "metadata_execution_disabled", "metadata_registration_rejected",
    "native_account_changed_during_verification", "native_auth_status_rejected",
    "native_models_list_rejected", "native_credential_api_origin",
    "native_credential_endpoints", "native_credential_format", "native_credential_size",
    "native_credential_unavailable", "native_egress_unavailable",
    "native_metadata_cleanup_unconfirmed", "native_metadata_output_bound",
    "native_metadata_rejected", "native_metadata_stale", "native_metadata_timeout",
    "native_metadata_unverified", "native_model_catalog_unverified", "native_status_controls",
    "native_status_encoding", "native_status_line_bound", "native_status_origin_mismatch",
    "native_status_size", "native_status_subject_invalid", "native_status_subject_unverified",
    "native_status_unknown_line", "native_status_unknown_or_duplicate_label",
    "native_status_unknown_label", "native_status_duplicate_label",
    "native_subject_source_unverified", "source_profile_changed",
})


def _safe_collector_reason(value):
    return (type(value) is str and (value in COLLECTOR_FAILURE_REASONS
            or devin_account.safe_status_diagnostic(value) is not None))


class ProvisionError(ValueError):
    def __init__(self, message, *, collector_reason=None, _retryable_metadata=False,
                 _retry_authority=None):
        super().__init__(message)
        self.collector_reason = (collector_reason
                                 if _safe_collector_reason(collector_reason) else None)
        self._retryable_metadata = _retryable_metadata is True
        self._retry_authority = _retry_authority if self._retryable_metadata else None


def metadata_failure_reason(error):
    """Revalidate the private code at the log boundary; never stringify an error."""
    reason = getattr(error, "collector_reason", None) if type(error) is ProvisionError else None
    return reason if _safe_collector_reason(reason) else "unavailable"


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode()


def clean_control(value):
    """Authenticated desired state only; no file paths, credentials or commands."""
    if not isinstance(value, dict) or set(value) != {"claim", "runtime", "keys"}:
        raise ProvisionError("invalid_native_control")
    claim = value["claim"]
    if (not isinstance(claim, dict) or set(claim) != {
            "owner_id", "slot_id", "claim_id", "claim_generation"}
            or any(not isinstance(claim[k], str) or not _ID.fullmatch(claim[k])
                   for k in ("owner_id", "slot_id"))
            or not isinstance(claim["claim_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", claim["claim_id"])
            or type(claim["claim_generation"]) is not int
            or not 1 <= claim["claim_generation"] <= 2**31-1):
        raise ProvisionError("invalid_native_claim")
    runtime = devin_account._runtime(value["runtime"])
    keys = value["keys"]
    if not isinstance(keys, list) or len(keys) > MAX_KEYS:
        raise ProvisionError("invalid_native_keys")
    seen, cleaned = set(), []
    for key in keys:
        if (not isinstance(key, dict) or set(key) != {
                "key_id", "key_generation", "provider_generation"}
                or not isinstance(key["key_id"], str) or not _ID.fullmatch(key["key_id"])
                or key["key_id"] in seen
                or any(type(key[k]) is not int or not 1 <= key[k] <= 2**31-1
                       for k in ("key_generation", "provider_generation"))):
            raise ProvisionError("invalid_native_key")
        seen.add(key["key_id"])
        cleaned.append(dict(key))
    return {"claim": dict(claim), "runtime": runtime, "keys": cleaned}


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ProvisionError("unsafe_native_directory")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in {0, ROOT_UID} or info.st_mode & 0o022:
                raise ProvisionError("unsafe_native_directory")
        if os.fstat(fd).st_uid != ROOT_UID:
            raise ProvisionError("unsafe_native_directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(path, *, uid=0, maximum=64*1024):
    path = Path(path)
    base = _directory(path.parent)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=base)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022
                or info.st_nlink != 1 or not 0 < info.st_size <= maximum):
            raise ProvisionError("unsafe_native_file")
        raw = os.read(fd, maximum+1)
        if len(raw) > maximum or len(raw) != info.st_size:
            raise ProvisionError("native_file_bound")
        return raw
    finally:
        if fd is not None:
            os.close(fd)
        os.close(base)


def _load(path):
    return devin_native.mcp._decode(_read(path, uid=ROOT_UID))


def _model_digest(config):
    if (not isinstance(config, dict) or not isinstance(config.get("model_uid"), str)
            or not devin_native._MODEL.fullmatch(config["model_uid"])):
        raise ProvisionError("native_model_map_changed")
    for field in ("request_models", "verified_models"):
        values = config.get(field)
        if (not isinstance(values, list) or not 1 <= len(values) <= 256
                or any(not isinstance(v, str) or not devin_native._MODEL.fullmatch(v)
                       for v in values)):
            raise ProvisionError("native_model_map_changed")
    return hashlib.sha256(_encode({"model_uid": config["model_uid"],
        "request_models": sorted(config["request_models"]),
        "verified_models": sorted(config["verified_models"])})).hexdigest()


def _profile(directory, user, control):
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise ProvisionError("invalid_native_user")
    base = Path(directory) / user
    profile = _load(base / "profile.json")
    fields = {"version", "enabled", "config_path", "config_sha256", "template_path",
              "template_sha256", "verifier_user", "source_profile_path", "source_profile_sha256"}
    if (not isinstance(profile, dict) or set(profile) != fields
            or type(profile["version"]) is not int or profile["version"] != 1
            or profile["enabled"] is not True
            or profile["config_path"] != str(base / "metadata.json")
            or profile["template_path"] != str(base / "template.json")
            or not isinstance(profile["verifier_user"], str)
            or not re.fullmatch(r"ccfleet-devin-v[a-z0-9_-]{0,16}", profile["verifier_user"])
            or any(not isinstance(profile[k], str) or not _HEX.fullmatch(profile[k])
                   for k in ("config_sha256", "template_sha256", "source_profile_sha256"))
            or not isinstance(profile["source_profile_path"], str)):
        raise ProvisionError("native_profile_disabled_or_invalid")
    template_raw = _read(base / "template.json", uid=ROOT_UID)
    metadata_raw = _read(base / "metadata.json", uid=ROOT_UID)
    source_raw = _read(profile["source_profile_path"], uid=ROOT_UID)
    if (hashlib.sha256(template_raw).hexdigest() != profile["template_sha256"]
            or hashlib.sha256(metadata_raw).hexdigest() != profile["config_sha256"]
            or hashlib.sha256(source_raw).hexdigest() != profile["source_profile_sha256"]):
        raise ProvisionError("native_profile_changed")
    template = devin_native.mcp._decode(template_raw)
    metadata = devin_native.mcp._decode(metadata_raw)
    source = devin_native.mcp._decode(source_raw)
    binding = devin_native.OwnershipBinding.from_dict(template["binding"])
    if (any(getattr(binding, k) != v for k, v in control["claim"].items())
            or metadata["binding"] != template["binding"]
            or any(_model_digest(c) != control["runtime"]["model_map_digest"]
                   or c["files"]["adapter"]["sha256"] != control["runtime"]["adapter_revision"]
                   or c["files"]["executable"]["sha256"] != source["native_sha256"]
                   for c in (template, metadata))
            or any(source[k] != control["runtime"][k] for k in (
                "native_version", "adapter_revision", "model_map_digest"))
            or source["live_subject_source_verified"] is not True
            or hashlib.sha256(_read(Path(devin_account.__file__).resolve(), uid=ROOT_UID,
                                    maximum=1024*1024)).hexdigest() != source["collector_sha256"]):
        raise ProvisionError("native_profile_claim_or_runtime_changed")
    return base, profile, template, metadata, binding


def _owned_directory(path, uid):
    """Walk a root-registered private home without following holder symlinks."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ProvisionError("unsafe_credential_directory")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in {ROOT_UID, uid} or info.st_mode & 0o022:
                raise ProvisionError("unsafe_credential_directory")
        if os.fstat(fd).st_uid != uid:
            raise ProvisionError("unsafe_credential_directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _credential(account):
    base = _owned_directory(Path(account.pw_dir) / ".local/share/devin", account.pw_uid)
    fd = None
    try:
        fd = os.open("credentials.toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=base)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid
                or info.st_mode & 0o077 or info.st_nlink != 1 or not 0 < info.st_size <= 65536):
            raise ProvisionError("unsafe_native_credential")
        raw = os.read(fd, 65537)
        if len(raw) != info.st_size:
            raise ProvisionError("native_credential_changed")
        return raw
    finally:
        if fd is not None:
            os.close(fd)
        os.close(base)


def _collection_authority(directory, account, control):
    control = clean_control(control)
    _, profile, _, _, _ = _profile(directory, account.pw_name, control)
    return (_encode(control), _encode(profile), hashlib.sha256(_credential(account)).hexdigest())


def _remaining_timeout(deadline, monotonic, minimum):
    remaining = deadline - monotonic()
    if not math.isfinite(remaining) or remaining <= 0 or remaining < minimum:
        raise ProvisionError("native_metadata_child_unverified", collector_reason=(
            "native_models_list_rejected" if minimum else "native_metadata_timeout"))
    return remaining


def _retryable_catalogue_failure(code, value, control, now):
    if (type(code) is not int or code != 1 or not isinstance(value, dict)
            or set(value) != {"report", "credential_sha256", "checked_at", "reason",
                              "cleanup_confirmed"}
            or value["reason"] != "native_models_list_rejected"
            or value["cleanup_confirmed"] is not True or value["credential_sha256"] is not None):
        return False
    report = value["report"]
    return (isinstance(report, dict) and set(report) == {"account_fp", "identity_verified",
            "bridge_contract", "capabilities", "native_version", "adapter_revision",
            "model_map_digest", "observed_at"}
            and report["account_fp"] is None and report["identity_verified"] is False
            and type(value["checked_at"]) in (int, float)
            and 0 <= value["checked_at"] <= 10**11 and math.isfinite(value["checked_at"])
            and type(report["observed_at"]) in (int, float)
            and report["observed_at"] == value["checked_at"]
            and type(now) in (int, float) and 0 <= now - value["checked_at"] <= 240
            and all(report[k] == v for k, v in control["runtime"].items())
            and isinstance(report["capabilities"], dict)
            and set(report["capabilities"]) == devin_account.CAPABILITIES
            and all(item is True for item in report["capabilities"].values()))


def collect(directory, account, control, *, spawn, lookup, groups_of, now, collector_path=None,
            monotonic=time.monotonic):
    """Fresh isolated verification, with one narrowly bounded catalogue retry."""
    deadline = monotonic() + devin_account.METADATA_CHILD_TIMEOUT_S
    options = dict(spawn=spawn, lookup=lookup, groups_of=groups_of, now=now,
                   collector_path=collector_path, deadline=deadline, monotonic=monotonic)
    try:
        return _collect_once(directory, account, control, minimum_timeout=0, expected=None,
                             **options)
    except ProvisionError as error:
        expected = error._retry_authority
        if (not error._retryable_metadata or expected is None
                or deadline - monotonic() < _RETRY_MINIMUM_SECONDS):
            raise
    return _collect_once(directory, account, control, minimum_timeout=_RETRY_MINIMUM_SECONDS,
                         expected=expected, **options)


def _collect_once(directory, account, control, *, spawn, lookup, groups_of, now, collector_path,
                  deadline, monotonic, expected, minimum_timeout):
    original_control = control
    control = clean_control(control)
    base, profile, template, metadata, binding = _profile(directory, account.pw_name, control)
    if (template["execution_uid"] != account.pw_uid or template["home"] != account.pw_dir
            or account.pw_uid <= 0):
        raise ProvisionError("native_uid_changed")
    verifier = lookup(profile["verifier_user"])
    privileged = {"sudo", "admin", "wheel", "root", "docker", "lxd", "libvirt", "kvm",
                  "adm", "disk", "shadow", "staff", "ccfleet-slots"}
    if (verifier is None or verifier.pw_name != profile["verifier_user"]
            or verifier.pw_uid <= 0 or verifier.pw_uid == account.pw_uid or verifier.pw_gid == 0
            or verifier.pw_shell not in {"/bin/false", "/usr/sbin/nologin", "/sbin/nologin"}
            or groups_of(verifier).intersection(privileged)
            or metadata["execution_uid"] != verifier.pw_uid
            or metadata["home"] != verifier.pw_dir):
        raise ProvisionError("isolated_verifier_required")
    path = Path(devin_account.__file__).resolve()
    if collector_path is not None and Path(collector_path) != path:
        raise ProvisionError("native_collector_path_changed")
    request = {k: profile[k] for k in ("config_path", "config_sha256", "source_profile_path",
                                     "source_profile_sha256")}
    request.update(runtime=control["runtime"], binding=metadata["binding"])
    # Only root transfers the fixed credential file into the separate verifier
    # principal. Interactive slot processes cannot replace its stdout or HOME.
    raw = _credential(account)
    credential_sha = hashlib.sha256(raw).hexdigest()
    authority = (_encode(control), _encode(profile), credential_sha)
    if expected is None:
        expected = authority
    elif authority != expected:
        raise ProvisionError("native_metadata_authority_changed",
                             collector_reason="account_profile_or_authorization_changed")
    _remaining_timeout(deadline, monotonic, minimum_timeout)
    snapshot = _owned_directory(Path(verifier.pw_dir) / ".local/share/devin", verifier.pw_uid)
    fd = None
    cleanup = False
    try:
        fd = os.open("credentials.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=snapshot)
        os.fchown(fd, verifier.pw_uid, verifier.pw_gid)
        remaining = memoryview(raw)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("incomplete native snapshot")
            remaining = remaining[count:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            timeout = _remaining_timeout(deadline, monotonic, minimum_timeout)
            # Recompute after snapshot copying. Both attempts share the
            # original 90-second parent deadline; the child may use only the
            # remaining acquisition time after import/validation/cleanup room.
            acquisition_timeout = min(devin_account.METADATA_ACQUISITION_TIMEOUT_S,
                                      timeout - _METADATA_CLEANUP_RESERVE_SECONDS)
            if acquisition_timeout < devin_account.MIN_METADATA_ACQUISITION_TIMEOUT_S:
                raise ProvisionError("native_metadata_child_unverified",
                                     collector_reason="native_metadata_timeout")
            request["acquisition_timeout_s"] = acquisition_timeout
        except ProvisionError:
            cleanup = True  # This exclusive snapshot has not been given to any child.
            raise
        if minimum_timeout:
            log.info("native metadata catalogue verification retry")
        code, output = spawn(["/usr/bin/python3", "-I", str(path), "--collect-profile"],
            input_text=_encode(request).decode(), limit=16*1024,
            timeout=timeout,
            user=verifier.pw_uid, group=verifier.pw_gid, extra_groups=[], cwd="/",
            env={"HOME": verifier.pw_dir, "USER": verifier.pw_name, "LOGNAME": verifier.pw_name,
                 "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        if not isinstance(output, str) or len(output.encode()) > 16*1024:
            raise ProvisionError("native_metadata_child_failed")
        value = devin_native.mcp._decode(output.encode())
        cleanup = isinstance(value, dict) and value.get("cleanup_confirmed") is True
        if _collection_authority(directory, account, original_control) != expected:
            raise ProvisionError("native_metadata_authority_changed",
                                 collector_reason="account_profile_or_authorization_changed")
        if (code != 0 or not cleanup or value.get("credential_sha256") != credential_sha
                or hashlib.sha256(_credential(account)).hexdigest() != credential_sha):
            retryable = _retryable_catalogue_failure(code, value, control,
                now=now() if callable(now) else now)
            raise ProvisionError("native_metadata_child_unverified",
                                 collector_reason=value.get("reason")
                                 if isinstance(value, dict) else None,
                                 _retryable_metadata=retryable, _retry_authority=expected)
        # Re-read root registration after the child, before returning evidence.
        _profile(directory, account.pw_name, control)
    finally:
        if fd is not None:
            os.close(fd)
        if cleanup:
            os.unlink("credentials.toml", dir_fd=snapshot)
        os.close(snapshot)
    observed = clean_observation({**value, "claim": control["claim"],
        "config_sha256": profile["template_sha256"],
        "source_profile_sha256": profile["source_profile_sha256"]}, control,
        now=now() if callable(now) else now)
    _remaining_timeout(deadline, monotonic, 0)
    return observed


def clean_observation(value, control, *, now):
    control = clean_control(control)
    if (not isinstance(value, dict) or set(value) != {"report", "credential_sha256", "checked_at",
            "reason", "cleanup_confirmed", "claim", "config_sha256", "source_profile_sha256"}
            or value["claim"] != control["claim"] or value["cleanup_confirmed"] is not True
            or type(value["checked_at"]) not in (int, float)
            or not math.isfinite(value["checked_at"]) or not 0 <= now-value["checked_at"] <= 240
            or value["reason"] != "verified"
            or any(not isinstance(value[k], str) or not _HEX.fullmatch(value[k])
                   for k in ("credential_sha256", "config_sha256", "source_profile_sha256"))):
        raise ProvisionError("native_observation_unverified")
    report = value["report"]
    if (not isinstance(report, dict) or set(report) != {"account_fp", "identity_verified",
            "bridge_contract", "capabilities", "native_version", "adapter_revision",
            "model_map_digest", "observed_at"}
            or report["identity_verified"] is not True
            or not isinstance(report["account_fp"], str) or not _HEX.fullmatch(report["account_fp"])
            or report["observed_at"] != value["checked_at"]
            or any(report[k] != v for k, v in control["runtime"].items())
            or not isinstance(report["capabilities"], dict)
            or set(report["capabilities"]) != devin_account.CAPABILITIES
            or any(v is not True for v in report["capabilities"].values())):
        raise ProvisionError("native_observation_unverified")
    return json.loads(_encode(value))


def public_report(observation):
    return {"claim_id": observation["claim"]["claim_id"],
            "claim_generation": observation["claim"]["claim_generation"],
            "report": observation["report"]}


def _replace(base, name, raw, gid):
    temporary = ".native-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=base)
    try:
        os.fchown(fd, ROOT_UID, gid)
        os.fchmod(fd, 0o640)
        remaining = memoryview(raw)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("incomplete native grant write")
            remaining = remaining[count:]
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=base, dst_dir_fd=base)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=base)
        except FileNotFoundError:
            pass


def reconcile(directory, account, control, observation, *, now):
    """Revoke first; only current verified observations mint an exact key grant."""
    user = account.pw_name
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise ProvisionError("invalid_native_user")
    base_path = Path(directory) / user
    try:
        base = _directory(base_path)
    except FileNotFoundError:
        return
    desired = {}
    try:
        try:
            control = clean_control(control)
            observed = clean_observation(observation, control, now=now)
            _, profile, template, _, _ = _profile(directory, user, control)
            if (observed["config_sha256"] != profile["template_sha256"]
                    or observed["source_profile_sha256"] != profile["source_profile_sha256"]
                    or template["execution_uid"] != account.pw_uid
                    or template["home"] != account.pw_dir
                    or hashlib.sha256(_credential(account)).hexdigest()
                       != observed["credential_sha256"]):
                raise ProvisionError("native_profile_changed")
            for key in control["keys"]:
                binding = {**control["claim"], **key, "provider": "devin"}
                devin_native.OwnershipBinding.from_dict(binding)
                name = hashlib.sha256(key["key_id"].encode()).hexdigest()
                config = {**template, "enabled": True, "binding": binding}
                raw = _encode(config)
                path = base_path / (name + ".json")
                grant = {"version": 1, "enabled": True, "identity_verified": True,
                    "binding": binding, "runtime": {**control["runtime"],
                        "provider_account_fp": observed["report"]["account_fp"]},
                    "config_path": str(path), "config_sha256": hashlib.sha256(raw).hexdigest(),
                    "credential_sha256": observed["credential_sha256"],
                    "observed_at": observed["checked_at"]}
                desired[name] = (raw, _encode(grant))
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            desired = {}
        # Remove stale authority before replacing any policy. Unlink is safe
        # even for an unexpected symlink; no untrusted target is followed.
        for filename in os.listdir(base):
            if re.fullmatch(r"[0-9a-f]{64}\.grant\.json", filename):
                stem = filename[:-11]
                if stem not in desired:
                    os.unlink(filename, dir_fd=base)
        for name, (raw, grant) in desired.items():
            try:
                same_config = _read(base_path / (name + ".json"), uid=ROOT_UID) == raw
            except (OSError, ValueError):
                same_config = False
            if same_config:
                try:
                    if _read(base_path / (name + ".grant.json"), uid=ROOT_UID) == grant:
                        continue
                except (OSError, ValueError):
                    pass
                # Fresh evidence for the exact same config can be renewed by
                # one atomic grant rename without interrupting active readers.
                _replace(base, name + ".grant.json", grant, account.pw_gid)
                continue
            # Revocation prevents a reader using an old grant during config
            # replacement, including a new key generation with the same token.
            try:
                os.unlink(name + ".grant.json", dir_fd=base)
            except FileNotFoundError:
                pass
            _replace(base, name + ".json", raw, account.pw_gid)
            _replace(base, name + ".grant.json", grant, account.pw_gid)
        os.fsync(base)
    finally:
        os.close(base)
