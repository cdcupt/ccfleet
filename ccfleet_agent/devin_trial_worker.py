"""Dedicated trial worker: fixed root policy, fresh credential-only HOME per turn.

Install only as a forced SSH command for a dedicated non-root trial UID. This
never loads a visitor-selected policy, slot binding, credential or executable.
Default invocation is inert. Native node tools remain disabled; tool calls are
returned to the visitor's local Claude Code after native child cleanup.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

if __package__:
    from . import devin_relay as relay
else:
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_trial_pinned_relay", Path(__file__).resolve().with_name("devin_relay.py")
    )
    relay = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = relay
    exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, "exec"), relay.__dict__)

native = relay.native
POLICY = Path("/etc/ccfleet/devin-trial/worker.json")
MODEL = "claude-opus-5-5"
MAX_BODY = 192 * 1024


def policy():
    if os.geteuid() == 0:
        raise native.NativeError("dedicated_trial_uid_required")
    native._root_ancestry(POLICY)
    raw = native._read(POLICY, uid=0, maximum=8192)
    value = native.mcp._decode(raw)
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
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["enabled"] is not True
        or any(
            not isinstance(value[name], str) or not native._DIGEST.fullmatch(value[name])
            for name in ("config_sha256", "expected_account_fp", "source_profile_sha256")
        )
        or value["grant_path"] != str(POLICY.with_name("grant.json"))
        or value["config_path"] != str(POLICY.with_name("template.json"))
    ):
        raise native.NativeError("trial_policy_rejected")
    claim = value["source_claim"]
    if (
        not isinstance(claim, dict)
        or set(claim) != {"owner_id", "slot_id", "claim_id", "claim_generation"}
        or any(
            not isinstance(claim[key], str) or not native._ID.fullmatch(claim[key])
            for key in ("owner_id", "slot_id")
        )
        or not isinstance(claim["claim_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", claim["claim_id"])
        or type(claim["claim_generation"]) is not int
        or not 1 <= claim["claim_generation"] <= 2**31 - 1
    ):
        raise native.NativeError("trial_source_claim_rejected")
    runtime = value["source_runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {"bridge_contract", "native_version", "adapter_revision", "model_map_digest"}
        or runtime["bridge_contract"] != "native-agent-v1"
        or not isinstance(runtime["native_version"], str)
        or not relay._VERSION.fullmatch(runtime["native_version"])
        or any(
            not isinstance(runtime[key], str) or not native._DIGEST.fullmatch(runtime[key])
            for key in ("adapter_revision", "model_map_digest")
        )
    ):
        raise native.NativeError("trial_source_runtime_rejected")

    def grant():
        path = Path(value["grant_path"])
        native._root_ancestry(path)
        current = native.mcp._decode(native._read(path, uid=0, maximum=8192))
        if (
            not isinstance(current, dict)
            or set(current)
            != {
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
            or type(current["version"]) is not int
            or current["version"] != 1
            or current["enabled"] is not True
            or current["config_sha256"] != value["config_sha256"]
            or current["account_fp"] != value["expected_account_fp"]
            or current["source_claim"] != value["source_claim"]
            or current["source_profile_sha256"] != value["source_profile_sha256"]
            or current["source_runtime"] != runtime
            or not isinstance(current["credential_sha256"], str)
            or not native._DIGEST.fullmatch(current["credential_sha256"])
            or type(current["observed_at"]) not in (int, float)
            or not 0 <= time.time() - current["observed_at"] <= 300
        ):
            raise native.NativeError("trial_grant_stale_or_changed")
        return current

    initial = grant()
    # A full admitted turn has a sixty-second lease. Do not begin from a grant
    # already near its 300-second freshness boundary.
    if time.time() - initial["observed_at"] > 240:
        raise native.NativeError("trial_grant_needs_refresh")
    config = native.NativeConfig.load(value["config_path"], expected_sha256=value["config_sha256"])
    data = config.data
    binding = native.OwnershipBinding.from_dict(data["binding"])
    if (
        binding.owner_id != "ccfleet-trial"
        or binding.slot_id != "virtual-trial"
        or binding.key_id != "trial-worker"
        or data["request_models"] != [MODEL]
        or data["thought_level"] != "max"
        or data["timeout_s"] > 40
        or data["enabled"] is not True
    ):
        raise native.NativeError("trial_virtual_policy_required")
    credential_path = Path(data["home"]) / ".local/share/devin/credentials.toml"
    if (
        hashlib.sha256(
            native._read(credential_path, uid=os.geteuid(), maximum=65536, private=True)
        ).hexdigest()
        != initial["credential_sha256"]
    ):
        raise native.NativeError("trial_credential_needs_refresh")

    def refresh():
        return grant()["credential_sha256"] == initial["credential_sha256"]

    return (
        config,
        {**value, "credential_sha256": initial["credential_sha256"], "_refresh": refresh},
        hashlib.sha256(raw).hexdigest(),
    )


class _FreshConfig(native.NativeConfig):
    """The original full source/policy guard plus private per-turn copies.

    This narrow subclass changes only native HOME/state and their two config
    records. Every validation still rehashes the full original native profile;
    a visitor never controls either snapshot or binding.
    """

    def validate(self):
        self.source.validate()
        if self.grant_refresh() is not True:
            raise native.NativeError("trial_grant_changed")
        if hashlib.sha256(native._read(POLICY, uid=0, maximum=8192)).hexdigest() != self.policy_sha:
            raise native.NativeError("trial_policy_changed")
        if (
            hashlib.sha256(
                native._read(self.credential, uid=os.geteuid(), maximum=65536, private=True)
            ).hexdigest()
            != self.credential_sha
        ):
            raise native.NativeError("trial_credential_changed")
        for path, digest in self.fresh_files:
            if (
                hashlib.sha256(
                    native._read(path, uid=os.geteuid(), maximum=65536, private=True)
                ).hexdigest()
                != digest
            ):
                raise native.NativeError("trial_snapshot_changed")
        return native._private_directory(self.data["home"]), native._private_directory(
            self.data["state_dir"]
        )


def fresh_config(source, settings, policy_sha):
    home, state = source.validate()
    credential = home / ".local/share/devin/credentials.toml"
    raw = native._read(credential, uid=os.geteuid(), maximum=65536, private=True)
    if hashlib.sha256(raw).hexdigest() != settings["credential_sha256"]:
        raise native.NativeError("trial_credential_changed")
    # No owner directories or existing history are copied: exactly credentials
    # and the two already pinned, tool-disabled native configuration files.
    root = Path(tempfile.mkdtemp(prefix="trial-", dir=state))
    copied = []
    try:
        temporary_home, temporary_state = root / "home", root / "state"
        temporary_home.mkdir(mode=0o700)
        temporary_state.mkdir(mode=0o700)
        value = source.data
        originals = [(".local/share/devin/credentials.toml", raw)]
        for name in ("native_config", "native_mcp_config"):
            relative = ".config/devin/" + (
                "config.json" if name == "native_config" else "mcp_config.json"
            )
            originals.append(
                (
                    relative,
                    native._read(
                        Path(value["files"][name]["path"]),
                        uid=os.geteuid(),
                        maximum=65536,
                        private=True,
                    ),
                )
            )
        for relative, contents in originals:
            destination = temporary_home / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # All intermediate homes are private regardless of process umask.
            for parent in destination.parents:
                if parent == root:
                    break
                parent.chmod(0o700)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(contents)
            except BaseException:
                raise
            copied.append((destination, hashlib.sha256(contents).hexdigest()))
        value["home"], value["state_dir"] = str(temporary_home), str(temporary_state)
        for name, relative in (
            ("native_config", ".config/devin/config.json"),
            ("native_mcp_config", ".config/devin/mcp_config.json"),
        ):
            value["files"][name] = {
                "path": str(temporary_home / relative),
                "sha256": hashlib.sha256((temporary_home / relative).read_bytes()).hexdigest(),
            }
        config = _FreshConfig(
            source.policy_path, source.policy_sha256, native._encode(value).decode()
        )
        for key, item in {
            "source": source,
            "policy_sha": policy_sha,
            "credential": credential,
            "credential_sha": settings["credential_sha256"],
            "fresh_files": copied,
            "grant_refresh": settings.get("_refresh", lambda: False),
        }.items():
            object.__setattr__(config, key, item)
        config.validate()
        return config, root
    except BaseException:
        shutil.rmtree(root)
        raise


def serve(input_, output, *, loader=policy, runner=None, cancelled=None):
    runner = native.run_step if runner is None else runner
    cancelled = threading.Event() if cancelled is None else cancelled
    reader, writer = relay._Input(input_, cancelled), relay._Output(output)
    root, clean, lock = None, False, None
    try:
        source, settings, policy_sha = loader()
        first = reader.frame()
        if first != {"version": 2, "operation": "status"}:
            raise native.NativeError("trial_status_required")
        writer.response(
            200,
            "application/json",
            [
                relay._json(
                    {
                        "ready": True,
                        "protocol": 2,
                        "trial": True,
                        "model": MODEL,
                        "policy_sha256": policy_sha,
                    }
                )
            ],
        )
        reader.deadline = time.monotonic() + 10
        request = reader.frame(eof_ok=True)
        if request is None:
            return 0
        if (
            not isinstance(request, dict)
            or set(request) != {"version", "operation", "method", "path", "headers", "body_size"}
            or request["version"] != 2
            or request["operation"] != "request"
            or request["method"] != "POST"
            or request["path"] != "/v1/messages"
            or request["headers"] != {}
            or type(request["body_size"]) is not int
            or not 0 < request["body_size"] <= MAX_BODY
        ):
            raise native.NativeError("trial_request_rejected")
        body = native.validate_native_request(
            native.mcp._decode(reader.exact(request["body_size"])),
            request_models=[MODEL],
            effort="max",
        )
        _, shared_state = source.validate()
        lock = os.open(
            shared_state / ".trial.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
        )
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise native.NativeError("trial_lock_guard")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # No old uncertain turn may share this Unix principal with a new one.
        if list(shared_state.glob("trial-*")):
            raise native.NativeError("trial_cleanup_review_required")
        source, settings, reloaded_sha = loader()
        if reloaded_sha != policy_sha:
            raise native.NativeError("trial_policy_changed")
        # Root may have renewed the credential after readiness but before this
        # lock; reload its verified grant inside the lock before copying.
        config, root = fresh_config(source, settings, policy_sha)
        binding = native.OwnershipBinding.from_dict(source.data["binding"])

        def authorized(current):
            try:
                return (
                    current == binding
                    and not cancelled.is_set()
                    and not reader.disconnected()
                    and config.validate() is not None
                )
            except Exception:
                return False

        result = runner(
            config,
            binding,
            os.urandom(16).hex(),
            body,
            authorize=authorized,
            cancel_event=cancelled,
        )
        clean = result.get("native_cleanup_confirmed") is True
        accounting = result.get("accounting")
        complete = (
            isinstance(accounting, dict)
            and accounting.get("served_model_verified") is True
            and accounting.get("model_mismatch_detected") is False
            and type(accounting.get("unique_finalized_provider_ids")) is int
            and 1 <= accounting["unique_finalized_provider_ids"] <= 512
        )
        counts = accounting.get("usage") if complete else None
        metering = "unconfirmed"
        fields = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"}
        if isinstance(counts, dict) and set(counts) == fields:
            known = all(
                type(value) is int and 0 <= value <= 10**12
                for key, value in counts.items()
                if key != "cache_read_tokens"
            )
            if (
                known
                and type(counts["cache_read_tokens"]) is int
                and (0 <= counts["cache_read_tokens"] <= 10**12)
            ):
                metering = "complete"
            elif known and counts["cache_read_tokens"] is None:
                metering = "cache_read_unmeasured"
        message, error = None, "native_request_failed"
        try:
            message = relay._message(result, config, body)
            # Expose the fixed public model, not the private catalogue UID.
            message["model"] = MODEL
            error = None
        except (relay.WireError, ValueError, TypeError):
            pass
        if clean:
            shutil.rmtree(root)
            root = None
        writer.response(
            200,
            "application/json",
            [
                relay._json(
                    {
                        "version": 1,
                        "cleanup_confirmed": clean and root is None,
                        "metering": metering
                        if clean and root is None and error is None
                        else "unconfirmed",
                        "usage": counts,
                        "message": message,
                        "error": error,
                    }
                )
            ],
        )
        return 0
    except Exception:
        # No provider text, paths, subjects, credentials or request content.
        writer.response(503, "application/json", [relay._json({"error": "trial_worker_failed"})])
        return 2
    finally:
        if root is not None and clean:
            shutil.rmtree(root)
        if lock is not None:
            os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    if not args.serve:
        print(
            json.dumps(
                {"execute": False, "model_requests": 0, "fixed_command": "ccfleet-devin-trial-v1"}
            )
        )
        return 0
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: cancelled.set())
    return serve(sys.stdin.buffer, sys.stdout.buffer, cancelled=cancelled)


if __name__ == "__main__":
    raise SystemExit(main())
