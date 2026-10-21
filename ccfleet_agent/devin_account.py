"""Provider-specific native Devin account metadata, separate from Claude login.

Native User ID is private on-node data. Neither email, tenant/team display,
credential presence nor a credential digest establishes account identity. A
parsed display is not proof by itself: readiness needs the root-reviewed fresh
credential-only native metadata acquisition profile and current authorization.
No native command, login, model request or credential read occurs on import.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

if __package__:
    from . import devin_native as native
else:
    # Python -I executes this exact root-pinned source. Native's direct-source
    # loader compiles its pinned siblings too; no user import/bytecode path.
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_account_pinned_native", Path(__file__).resolve().with_name("devin_native.py"))
    native = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = native
    exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, "exec"), native.__dict__)

MAX_STATUS_BYTES = 64 * 1024
MAX_STATUS_LINES = 128
_SUBJECT = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_LABELS = frozenset({"Credentials", "File", "API server", "Devin webapp", "Devin API", "User",
    "Name", "Email", "User ID", "Account", "Tier", "Plan", "Team ID", "Team membership",
    "Enterprise", "Primary org", "Telemetry", "Team settings", "Allowed models", "Default model",
    "MCP servers", "Sandbox", "Disabled tools", "Fetched at"})
_SECTIONS = frozenset({"Credentials", "User", "Account", "Team settings"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}\Z")
CAPABILITIES = frozenset({"labeled_visible_context", "text", "client_tools", "streaming",
                          "cancellation", "unknown_usage", "revocation"})
_IMPORTS = ("agents_standard", "cursor", "windsurf", "claude", "copilot", "opencode", "zed")
# Exact pinned CLI credential profile, independently inspected without exposing
# the opaque token. Alternate/enterprise endpoints need a separately reviewed
# profile; a holder-controlled URL is never trusted for native identity proof.
_CREDENTIAL_ENDPOINTS = {"api_server_url": "https://server.codeium.com",
                         "devin_webapp_host": "app.devin.ai",
                         "devin_api_url": "https://api.devin.ai"}
_CREDENTIAL_KEYS = frozenset({"windsurf_api_key", *_CREDENTIAL_ENDPOINTS})
_CREDENTIAL_LINE = re.compile(r'([a-z_]+)[ \t]*=[ \t]*"([^"\\]*)"\Z')


class AccountError(ValueError):
    """Static reason codes only; no status, subject, credential or filesystem text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical_origin(value):
    """Canonical HTTPS origin only, never a credential-bearing URL or endpoint path."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 512 or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise AccountError("invalid_native_api_origin")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (parts.scheme != "https" or not host or not _HOST.fullmatch(host)
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or port is not None and not 1 <= port <= 65535):
            raise ValueError
        host = host.lower().rstrip(".")
        if not host:
            raise ValueError
    except ValueError:
        raise AccountError("invalid_native_api_origin") from None
    return "https://" + host + (":" + str(port) if port not in (None, 443) else "")


@dataclass(frozen=True)
class ParsedNativeSubject:
    """Private extracted fields; parsing alone does not assert authentication/liveness."""

    api_origin: str
    user_id: str = field(repr=False)

    def fingerprint(self):
        material = ("ccfleet:devin:native-user:v1\0" + self.api_origin + "\0" + self.user_id)
        return hashlib.sha256(material.encode("ascii")).hexdigest()


def parse_auth_status(raw, *, expected_api_origin):
    """Strict parser for the pinned native plain-text status label profile.

    The caller separately proves the status was acquired live by the audited
    official runtime. Name/email/team/plan values are deliberately never copied
    into the result. Fetched at is cached team metadata, not identity freshness.
    """
    origin = canonical_origin(expected_api_origin)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_STATUS_BYTES:
        raise AccountError("native_status_size")
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeError:
        raise AccountError("native_status_encoding") from None
    if any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in text):
        raise AccountError("native_status_controls")
    lines = text.splitlines()
    if len(lines) > MAX_STATUS_LINES:
        raise AccountError("native_status_line_bound")
    seen = set()
    header_seen = False
    subject = api_origin = None
    for line in lines:
        line = line.strip(" ")
        if not line:
            continue
        if line == "Logged in (via Devin).":
            if header_seen or seen:
                raise AccountError("native_status_unknown_line")
            header_seen = True
            continue
        if ":" in line:
            label, value = line.split(":", 1)
            label, value = label.strip(" "), value.strip(" ")
        elif line in _SECTIONS:
            label, value = line, ""
        else:
            raise AccountError("native_status_unknown_line")
        if label not in _LABELS:
            raise AccountError("native_status_unknown_label")
        if label in seen:
            raise AccountError("native_status_duplicate_label")
        seen.add(label)
        if label == "User ID":
            if not _SUBJECT.fullmatch(value):
                raise AccountError("native_status_subject_invalid")
            subject = value
        elif label == "API server":
            api_origin = canonical_origin(value)
            if api_origin != origin:
                raise AccountError("native_status_origin_mismatch")
    if subject is None or api_origin is None:
        raise AccountError("native_status_subject_unverified")
    return ParsedNativeSubject(api_origin, subject)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _model_map_digest(data):
    """Canonical model identity from data validated by the immediate caller."""
    value = {"model_uid": data["model_uid"], "request_models": sorted(data["request_models"]),
             "verified_models": sorted(data["verified_models"])}
    return hashlib.sha256(_json(value).encode()).hexdigest()


def model_map_digest(config):
    """Effective alias/model choice from the already root-pinned NativeConfig."""
    if type(config) is not native.NativeConfig:
        raise AccountError("root_native_config_required")
    config.validate()
    return _model_map_digest(config.data)


@dataclass(frozen=True)
class SubjectSourceProfile:
    """Root-reviewed acquisition/bridge evidence; never an account identity itself."""

    path: Path = field(repr=False)
    sha256: str
    _json: str = field(repr=False)

    @classmethod
    def load(cls, path, *, expected_sha256):
        if not isinstance(expected_sha256, str) or not _DIGEST.fullmatch(expected_sha256):
            raise AccountError("source_profile_pin_required")
        path = native._plain(path)
        native._root_ancestry(path)
        raw = native._read(path, uid=0, maximum=64 * 1024)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise AccountError("source_profile_changed")
        value = native.mcp._decode(raw)
        result = cls(path, expected_sha256, _json(value))
        result.validate()
        return result

    @property
    def data(self):
        return json.loads(self._json)

    def validate(self):
        native._root_ancestry(self.path)
        raw = native._read(self.path, uid=0, maximum=64 * 1024)
        if (hashlib.sha256(raw).hexdigest() != self.sha256
                or _json(native.mcp._decode(raw)) != self._json):
            raise AccountError("source_profile_changed")
        data = self.data
        required = {"version", "metadata_profile", "native_version", "native_sha256",
                    "api_origin", "collector_sha256", "adapter_revision", "model_map_digest",
                    "capabilities", "live_subject_source_verified", "proof_sha256"}
        if (not isinstance(data, dict) or set(data) != required or type(data["version"]) is not int
                or data["version"] != 1
                or data["metadata_profile"] != "native-auth-status-fresh-home-v1"
                or type(data["live_subject_source_verified"]) is not bool
                or not isinstance(data["native_version"], str)
                or not _VERSION.fullmatch(data["native_version"])
                or any(not isinstance(data[name], str) or not _DIGEST.fullmatch(data[name])
                       for name in ("native_sha256", "collector_sha256", "adapter_revision",
                                    "model_map_digest", "proof_sha256"))
                or not isinstance(data["capabilities"], dict)
                or data["capabilities"].keys() - CAPABILITIES
                or any(type(value) is not bool for value in data["capabilities"].values())):
            raise AccountError("invalid_source_profile")
        if canonical_origin(data["api_origin"]) != data["api_origin"]:
            raise AccountError("invalid_source_profile_origin")
        native._pinned({"path": str(Path(__file__).resolve()), "sha256": data["collector_sha256"]},
                       expected=Path(__file__).resolve())
        return data


@dataclass(frozen=True)
class VerificationResult:
    """Only report is public; credential digest is a private node-side change guard."""

    _report_json: str = field(repr=False)
    credential_sha256: Optional[str] = field(default=None, repr=False)
    checked_at: float = 0
    reason: str = "unverified"
    cleanup_confirmed: bool = True

    @property
    def report(self):
        return json.loads(self._report_json)


def _runtime(value):
    if (not isinstance(value, Mapping) or set(value) != {
            "bridge_contract", "native_version", "adapter_revision", "model_map_digest"}
            or value["bridge_contract"] != "native-agent-v1"
            or not isinstance(value["native_version"], str)
            or not _VERSION.fullmatch(value["native_version"])
            or any(not isinstance(value[name], str) or not _DIGEST.fullmatch(value[name])
                   for name in ("adapter_revision", "model_map_digest"))):
        raise AccountError("invalid_account_runtime")
    return dict(value)


def _instant(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**11:
        raise AccountError("invalid_observation_time")
    return float(value)


def _credential(path):
    try:
        return native._read(path, uid=os.geteuid(), maximum=64 * 1024, private=True)
    except (OSError, ValueError):
        raise AccountError("native_credential_unavailable") from None


def validate_native_credentials(raw, *, expected_api_origin):
    """Validate the pinned four-string native TOML subset without exposing it.

    The native CLI parses this file itself. Accept only its inspected canonical
    scalar-string profile so the same bytes cannot introduce an alternate
    metadata origin, TOML table, escaped value, override or unknown option.
    No token or parsed credential mapping is returned, logged or printed.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 64 * 1024:
        raise AccountError("native_credential_size")
    try:
        text = raw.decode("ascii").replace("\r\n", "\n")
    except UnicodeError:
        raise AccountError("native_credential_format") from None
    if (any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in text)
            or len(text.splitlines()) > 16):
        raise AccountError("native_credential_format")
    values = {}
    for line in text.splitlines():
        line = line.strip(" \t")
        if not line:
            continue
        match = _CREDENTIAL_LINE.fullmatch(line)
        if (match is None or match[1] not in _CREDENTIAL_KEYS or match[1] in values
                or any(ord(char) < 32 for char in match[2])):
            raise AccountError("native_credential_format")
        values[match[1]] = match[2]
    if set(values) != _CREDENTIAL_KEYS or not 1 <= len(values["windsurf_api_key"]) <= 16384:
        raise AccountError("native_credential_format")
    if any(values[key] != expected for key, expected in _CREDENTIAL_ENDPOINTS.items()):
        raise AccountError("native_credential_endpoints")
    if canonical_origin(expected_api_origin) != _CREDENTIAL_ENDPOINTS["api_server_url"]:
        raise AccountError("native_credential_api_origin")


def _mkdir(path):
    path.mkdir(mode=0o700)


def _fresh_home(state, credential):
    home = Path(tempfile.mkdtemp(prefix=".devin-account-", dir=state))
    try:
        for relative in (".config", ".config/devin", ".cache", ".local", ".local/share",
                         ".local/share/devin"):
            _mkdir(home / relative)
        for relative, raw in (
                (".local/share/devin/credentials.toml", credential),
                (".config/devin/config.json", _json({"auto_update": False,
                    "subagents_enabled": False,
                    "read_config_from": dict.fromkeys(_IMPORTS, False)}).encode())):
            fd = os.open(home / relative,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
        return home
    except BaseException:
        shutil.rmtree(home)
        raise


def _stop_group(process):
    """Keep the leader unreaped until its owned group has been terminated."""
    try:
        try:
            if os.getpgid(process.pid) != process.pid:
                return False
            os.killpg(process.pid, signal.SIGTERM)
            time.sleep(0.05)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (OSError, ValueError):
            return False
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return process.returncode is not None
    finally:
        for stream in (process.stdout, process.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _capture(process, *, maximum, deadline, permitted, cancelled, clock):
    """Bound stdout/stderr; only explicit in-memory fake streams skip selectors."""
    if isinstance(process.stdout, io.BytesIO) and isinstance(process.stderr, io.BytesIO):
        if cancelled.is_set() or permitted() is not True or clock() >= deadline:
            raise AccountError("metadata_authorization_or_timeout")
        raw, error = process.stdout.read(maximum + 1), process.stderr.read(maximum + 1)
        if len(raw) + len(error) > maximum:
            raise AccountError("native_metadata_output_bound")
        return raw
    selector = selectors.DefaultSelector()
    chunks, total = [], 0
    try:
        for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        while selector.get_map():
            if cancelled.is_set() or permitted() is not True:
                raise AccountError("metadata_authorization_revoked")
            remaining = deadline - clock()
            if remaining <= 0:
                raise AccountError("native_metadata_timeout")
            for key, _ in selector.select(min(0.1, remaining)):
                try:
                    raw = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not raw:
                    selector.unregister(key.fileobj)
                    continue
                total += len(raw)
                if total > maximum:
                    raise AccountError("native_metadata_output_bound")
                if key.data == "stdout":
                    chunks.append(raw)
        return b"".join(chunks)
    finally:
        selector.close()


def _models_present(raw, uid):
    try:
        data = native.mcp._decode(raw)
        families = data["families"]
        if not isinstance(families, list) or len(families) > 4096:
            raise ValueError
        seen = set()
        for family in families:
            variants = family["variants"]
            if not isinstance(variants, list) or len(variants) > 4096:
                raise ValueError
            for variant in variants:
                model = variant["model_uid"]
                if not isinstance(model, str) or not native._MODEL.fullmatch(model):
                    raise ValueError
                seen.add(model)
        if uid not in seen:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise AccountError("native_model_catalog_unverified") from None


def collect_readiness(config, binding, source_profile, *, runtime, authorize,
                      popen_factory=None, cancel_event=None, clock=time.monotonic, now=time.time):
    """Collect live metadata only, with a fresh temporary credential-only HOME.

    Source profile must be root-owned and hash pinned. NativeConfig pins all
    runtime files and the slot UID; authorize checks the current owner/claim/
    grant before and after acquisition and during I/O. The default factory does
    not execute. A real metadata command needs an explicit Popen factory.
    Private credential_sha256 is only a change guard and never account identity.
    """
    runtime = _runtime(runtime)
    if type(config) is not native.NativeConfig or type(binding) is not native.OwnershipBinding:
        raise AccountError("root_native_config_required")
    if type(source_profile) is not SubjectSourceProfile or not callable(authorize):
        raise AccountError("root_subject_source_required")
    cancelled = threading.Event() if cancel_event is None else cancel_event
    if not isinstance(cancelled, threading.Event):
        raise AccountError("invalid_account_cancellation")
    capabilities = {}
    credential_sha = account = None
    home = process = None
    cleanup = True
    reason = "metadata_execution_disabled"

    def permitted():
        try:
            return not cancelled.is_set() and authorize(binding) is True
        except Exception:
            return False

    def validate():
        source = source_profile.validate()
        original_home, state = config.validate()
        data = config.data
        if (native.OwnershipBinding.from_dict(data["binding"]) != binding or not permitted()
                or runtime["native_version"] != source["native_version"]
                or runtime["adapter_revision"] != data["files"]["adapter"]["sha256"]
                or runtime["adapter_revision"] != source["adapter_revision"]
                or runtime["model_map_digest"] != _model_map_digest(data)
                or runtime["model_map_digest"] != source["model_map_digest"]
                or data["files"]["executable"]["sha256"] != source["native_sha256"]):
            raise AccountError("account_profile_or_authorization_changed")
        return source, original_home, state, data

    started_at = clock()
    try:
        source, original_home, state, data = validate()
        capabilities = dict(source["capabilities"])
        if source["live_subject_source_verified"] is not True:
            raise AccountError("native_subject_source_unverified")
        if popen_factory is not None:
            if not callable(popen_factory):
                raise AccountError("invalid_metadata_factory")
            original_path = original_home / ".local/share/devin/credentials.toml"
            credential = _credential(original_path)
            validate_native_credentials(credential, expected_api_origin=source["api_origin"])
            credential_sha = hashlib.sha256(credential).hexdigest()
            home = _fresh_home(state, credential)
            environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                "TERM": "dumb", "NO_COLOR": "1", "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache")}
            try:
                environment.update(native.egress.protected_native_environment())
            except native.egress.EgressError:
                raise AccountError("native_egress_unavailable") from None
            outputs = []
            for arguments, limit, rejected in (
                    (["auth", "status"], MAX_STATUS_BYTES, "native_auth_status_rejected"),
                    (["models", "list", "--format", "json"], 2 * 1024 * 1024,
                     "native_models_list_rejected")):
                if not permitted():
                    raise AccountError("metadata_authorization_revoked")
                process = popen_factory([data["files"]["executable"]["path"], *arguments],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(home), env=environment, start_new_session=True, bufsize=0)
                raw = _capture(process, maximum=limit, deadline=started_at + 30,
                               permitted=permitted, cancelled=cancelled, clock=clock)
                cleanup = _stop_group(process)
                if not cleanup:
                    raise AccountError("native_metadata_cleanup_unconfirmed")
                if process.returncode != 0:
                    raise AccountError(rejected)
                process = None
                outputs.append(raw)
            subject = parse_auth_status(outputs[0], expected_api_origin=source["api_origin"])
            _models_present(outputs[1], data["model_uid"])
            validate()
            if hashlib.sha256(_credential(original_path)).hexdigest() != credential_sha:
                raise AccountError("native_account_changed_during_verification")
            if clock() - started_at > 300:
                raise AccountError("native_metadata_stale")
            account, reason = subject.fingerprint(), "verified"
    except AccountError as error:
        reason = error.code
    except (OSError, ValueError, TypeError, KeyError):
        reason = "native_metadata_unverified"
    finally:
        if process is not None:
            cleanup = _stop_group(process)
        if cleanup and home is not None:
            shutil.rmtree(home)
        if not cleanup:
            account, reason = None, "native_metadata_cleanup_unconfirmed"
    checked = _instant(now())
    report = {**runtime, "account_fp": account, "identity_verified": account is not None,
              "capabilities": capabilities, "observed_at": checked}
    return VerificationResult(_json(report), credential_sha if account else None, checked,
                              reason, cleanup)


def _read_request(stream):
    maximum = 64 * 1024
    if isinstance(stream, io.BytesIO):
        raw = stream.read(maximum + 1)
    else:
        selector = selectors.DefaultSelector()
        parts, total, deadline = [], 0, time.monotonic() + 5
        try:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AccountError("metadata_registration_timeout")
                if not selector.select(remaining):
                    continue
                part = os.read(stream.fileno(), min(65536, maximum + 1 - total))
                if not part:
                    break
                parts.append(part)
                total += len(part)
                if total > maximum:
                    raise AccountError("metadata_registration_size")
            raw = b"".join(parts)
        finally:
            selector.close()
    if not raw or len(raw) > maximum:
        raise AccountError("metadata_registration_size")
    return native.mcp._decode(raw)


def main(argv=None, *, stdin=None, stdout=None, popen_factory=None):
    """Private slot-UID metadata pipe, never a customer or heartbeat API.

    Root must authenticate the registration and current server claim before
    launching this pinned script as the slot UID. The local authorize callback
    revalidates the root policy/profile and binding; it is not a server lease
    check. Stdout's credential digest is node-private and must not be forwarded
    with the public report. Default/--plan never reads stdin or credentials.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--collect-profile", action="store_true")
    modes.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    output = sys.stdout if stdout is None else stdout
    if not args.collect_profile:
        output.write(_json({"execute": False, "metadata_only": True,
                            "native_model_calls": False}) + "\n")
        return 0
    cancelled = threading.Event()
    prior_handlers = {}
    collection_started = False
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                prior_handlers[sig] = signal.signal(sig, lambda *_: cancelled.set())
        payload = _read_request(sys.stdin.buffer if stdin is None else stdin)
        fields = {"config_path", "config_sha256", "source_profile_path", "source_profile_sha256",
                  "runtime", "binding"}
        if (not isinstance(payload, dict) or set(payload) != fields
                or any(not isinstance(payload[name], str) or not _DIGEST.fullmatch(payload[name])
                       for name in ("config_sha256", "source_profile_sha256"))):
            raise AccountError("invalid_metadata_registration")
        config = native.NativeConfig.load(payload["config_path"],
                                         expected_sha256=payload["config_sha256"])
        source = SubjectSourceProfile.load(payload["source_profile_path"],
                                          expected_sha256=payload["source_profile_sha256"])
        binding = native.OwnershipBinding.from_dict(payload["binding"])

        def authorized(actual):
            try:
                config.validate()
                source.validate()
                return (not cancelled.is_set() and actual == binding
                        and native.OwnershipBinding.from_dict(config.data["binding"]) == binding)
            except (OSError, ValueError, TypeError, KeyError):
                return False

        collection_started = True
        result = collect_readiness(config, binding, source, runtime=payload["runtime"],
            authorize=authorized, cancel_event=cancelled,
            popen_factory=subprocess.Popen if popen_factory is None else popen_factory)
        value = {"report": result.report, "credential_sha256": result.credential_sha256,
                 "checked_at": result.checked_at, "reason": result.reason,
                 "cleanup_confirmed": result.cleanup_confirmed}
        output.write(_json(value) + "\n")
        return 0 if result.report["identity_verified"] else 1
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        output.write(_json({"report": None, "credential_sha256": None, "checked_at": None,
                            "reason": "metadata_registration_rejected",
                            "cleanup_confirmed": not collection_started}) + "\n")
        return 2
    finally:
        for sig, handler in prior_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
