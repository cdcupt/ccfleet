"""Fixed protocol-2 node entry for the explicitly enabled native-agent provider.

Default CLI mode is inert. The root-installed ccfleet-devin-gateway-v1 entry
invokes this exact file with --gateway-protocol-v1. The current Unix UID selects
the policy directory; HTTP metadata can select only its hashed key filename.
Status proves fresh root-attested eligibility, not a model request or MCP probe.
The adapter still requires a genuine live MCP handshake before generation.

Root grant schema: version=1, enabled=true, identity_verified=true, binding,
runtime, config_path, config_sha256, credential_sha256, observed_at. The native
credential hash is a private change guard, never a stable account identity.
Neither credential bytes nor owner/account identifiers are sent in responses.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import math
import os
import pwd
import re
import select
import signal
import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

if __package__:
    from . import devin_native as native
else:
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_pinned_native_relay", Path(__file__).resolve().with_name("devin_native.py"))
    native = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = native
    exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, "exec"), native.__dict__)

POLICY_ROOT = Path("/etc/ccfleet/devin")
MAX_META = 32 * 1024
MAX_BODY = native.MAX_BODY
MAX_CHUNK = 64 * 1024
INPUT_TIMEOUT = 30
OUTPUT_TIMEOUT = 15
MAX_GRANT_AGE = 300
CONTRACT = "native-agent-v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST = re.compile(r"[0-9a-f]{32}\Z")
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}\Z")


class WireError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode()


def _context(value):
    if not isinstance(value, dict) or set(value) != {"binding", "runtime", "request_id"}:
        raise WireError("invalid_native_context")
    try:
        binding = native.OwnershipBinding.from_dict(value["binding"])
    except (ValueError, TypeError):
        raise WireError("invalid_native_context") from None
    runtime = value["runtime"]
    if (not isinstance(runtime, dict) or set(runtime) != {"bridge_contract", "native_version",
            "adapter_revision", "model_map_digest", "provider_account_fp"}
            or runtime["bridge_contract"] != CONTRACT
            or not isinstance(runtime["native_version"], str)
            or not _VERSION.fullmatch(runtime["native_version"])
            or any(not isinstance(runtime[field], str) or not _SHA.fullmatch(runtime[field])
                   for field in ("adapter_revision", "model_map_digest", "provider_account_fp"))
            or not isinstance(value["request_id"], str)
            or not _REQUEST.fullmatch(value["request_id"])):
        raise WireError("invalid_native_context")
    return {"binding": asdict(binding), "runtime": dict(runtime), "request_id": value["request_id"]}


@dataclass(frozen=True)
class PolicySession:
    config: native.NativeConfig
    binding: native.OwnershipBinding
    runtime_json: str
    refresh: Callable[[], bool]

    def authorize(self, current):
        try:
            return current == self.binding and self.refresh() is True
        except Exception:
            return False


def load_policy(context, *, now=time.time):
    """Load only the current slot UID's root policy and fresh root grant."""
    context = _context(context)
    if os.geteuid() == 0:
        raise WireError("native_slot_identity_required", 403)
    user = pwd.getpwuid(os.geteuid()).pw_name
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise WireError("native_slot_identity_required", 403)
    stem = hashlib.sha256(context["binding"]["key_id"].encode()).hexdigest()
    config_path = POLICY_ROOT / user / (stem + ".json")
    grant_path = config_path.with_name(stem + ".grant.json")
    binding = native.OwnershipBinding.from_dict(context["binding"])

    def grant():
        native._root_ancestry(grant_path)
        value = native.mcp._decode(native._read(grant_path, uid=0, maximum=64 * 1024))
        required = {"version", "enabled", "identity_verified", "binding", "runtime", "config_path",
                    "config_sha256", "credential_sha256", "observed_at"}
        if (not isinstance(value, dict) or set(value) != required
                or type(value["version"]) is not int or value["version"] != 1
                or value["enabled"] is not True or value["identity_verified"] is not True
                or value["binding"] != context["binding"] or value["runtime"] != context["runtime"]
                or value["config_path"] != str(config_path)
                or any(not isinstance(value[field], str) or not _SHA.fullmatch(value[field])
                       for field in ("config_sha256", "credential_sha256"))):
            raise WireError("native_grant_rejected", 403)
        observed = value["observed_at"]
        instant = now()
        if (type(observed) not in (int, float) or not 0 <= observed <= 10**11
                or not math.isfinite(observed) or type(instant) not in (int, float)
                or not math.isfinite(instant) or not 0 <= instant - observed <= MAX_GRANT_AGE):
            raise WireError("native_grant_stale", 403)
        return value

    initial = grant()
    config = native.NativeConfig.load(config_path, expected_sha256=initial["config_sha256"])
    status = config.status(binding)
    if status.get("configured") is not True or status.get("enabled") is not True:
        raise WireError("native_profile_disabled", 503)
    data = config.data
    # Same canonical effective model map as devin_account.model_map_digest;
    # a root grant cannot accidentally attest another installed adapter/map.
    model_map = {"model_uid": data["model_uid"], "request_models": sorted(data["request_models"]),
                 "verified_models": sorted(data["verified_models"])}
    if (context["runtime"]["adapter_revision"] != data["files"]["adapter"]["sha256"]
            or context["runtime"]["model_map_digest"]
                != hashlib.sha256(_json(model_map)).hexdigest()):
        raise WireError("native_runtime_profile_mismatch", 403)
    credential_path = Path(config.data["home"]) / ".local/share/devin/credentials.toml"

    def refresh():
        current = grant()
        if current["config_sha256"] != config.policy_sha256:
            return False
        raw = native._read(config_path, uid=0, maximum=64 * 1024)
        if hashlib.sha256(raw).hexdigest() != config.policy_sha256:
            return False
        native._plain(credential_path)
        credential = native._read(credential_path, uid=os.geteuid(),
                                  maximum=64 * 1024, private=True)
        return hashlib.sha256(credential).hexdigest() == current["credential_sha256"]

    session = PolicySession(config, binding, _json(context["runtime"]).decode(), refresh)
    if not session.authorize(binding):
        raise WireError("native_credential_or_grant_changed", 403)
    return session


class _Input:
    def __init__(self, stream, cancelled):
        self.stream = stream
        self.cancelled = cancelled
        try:
            self.fd = stream.fileno()
        except (AttributeError, io.UnsupportedOperation):
            self.fd = None
        self.deadline = time.monotonic() + INPUT_TIMEOUT

    def read(self, size):
        if self.cancelled.is_set():
            raise WireError("native_cancelled", 403)
        if self.fd is None:
            return self.stream.read(size)
        while not self.cancelled.is_set():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise WireError("native_input_timeout", 408)
            if select.select([self.fd], [], [], min(0.1, remaining))[0]:
                return os.read(self.fd, size)
        raise WireError("native_cancelled", 403)

    def exact(self, size):
        value = bytearray()
        while len(value) < size:
            raw = self.read(size - len(value))
            if not raw:
                raise EOFError
            value.extend(raw)
        return bytes(value)

    def frame(self, *, eof_ok=False):
        first = self.read(4)
        if not first and eof_ok:
            return None
        if len(first) < 4:
            first += self.exact(4 - len(first))
        size = struct.unpack("!I", first)[0]
        if not 0 < size <= MAX_META:
            raise WireError("invalid_native_frame")
        value = native.mcp._decode(self.exact(size))
        if (not isinstance(value, dict) or type(value.get("version")) is not int
                or value["version"] != 2):
            raise WireError("invalid_native_frame")
        return value

    def disconnected(self):
        if self.fd is None:
            return False  # Injected test streams are explicitly in-memory, never native clients.
        if select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 1)  # EOF or unexpected additional input both revoke this one request.
            return True
        return False


class _Output:
    def __init__(self, stream):
        self.stream = stream

    def write(self, raw):
        try:
            fd = self.stream.fileno()
        except (AttributeError, io.UnsupportedOperation):
            remaining = memoryview(raw)
            while remaining:
                written = self.stream.write(remaining)
                if not isinstance(written, int) or written <= 0:
                    raise OSError("native output closed") from None
                remaining = remaining[written:]
            self.stream.flush()
            return
        blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        deadline = time.monotonic() + OUTPUT_TIMEOUT
        try:
            remaining = memoryview(raw)
            while remaining:
                wait = deadline - time.monotonic()
                if wait <= 0 or not select.select([], [fd], [], wait)[1]:
                    raise OSError("native output deadline")
                try:
                    written = os.write(fd, remaining)
                except (BlockingIOError, InterruptedError):
                    continue
                if written <= 0:
                    raise OSError("native output closed")
                remaining = remaining[written:]
        finally:
            os.set_blocking(fd, blocking)

    def response(self, status, content_type, parts):
        meta = _json({"version": 2, "status": status, "headers": {"content-type": content_type},
                      "retryable": False})
        self.write(struct.pack("!I", len(meta)) + meta)
        for part in parts:
            for offset in range(0, len(part), MAX_CHUNK):
                chunk = part[offset:offset + MAX_CHUNK]
                self.write(struct.pack("!I", len(chunk)) + chunk)
        self.write(b"\0\0\0\0")


def _usage(result):
    accounting = result.get("accounting")
    values = accounting.get("usage") if isinstance(accounting, dict) else None
    if values is None:
        return None
    if not isinstance(values, dict):
        raise WireError("invalid_native_accounting", 502)
    mapped = {}
    for source, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                           ("cache_read_tokens", "cache_read_input_tokens"),
                           ("cache_creation_tokens", "cache_creation_input_tokens")):
        value = values.get(source)
        if value is not None:
            if type(value) is not int or not 0 <= value <= 10**12:
                raise WireError("invalid_native_accounting", 502)
            mapped[target] = value
    return mapped if {"input_tokens", "output_tokens"} <= mapped.keys() else None


def _message(result, config, body):
    if (not isinstance(result, dict) or result.get("error_code")
            or result.get("native_cleanup_confirmed") is not True
            or result.get("mode") != "native_agent_compatibility"
            or type(result.get("prompt_requests")) is not int or result["prompt_requests"] != 1
            or result.get("model") != config.data["model_uid"]
            or result.get("raw_Messages_semantic_parity") is not False
            or result.get("contract") != CONTRACT):
        raise WireError("native_request_failed", 502)
    if result.get("verified_model") is not None:
        accounting = result.get("accounting")
        if (result["verified_model"] != result["model"] or not isinstance(accounting, dict)
                or accounting.get("served_model_verified") is not True):
            raise WireError("native_model_evidence_mismatch", 502)
    content, stop = result.get("content"), result.get("stop_reason")
    if not isinstance(content, list) or not content:
        raise WireError("invalid_native_result", 502)
    if stop == "tool_use":
        if len(content) != 1:
            raise WireError("invalid_native_result", 502)
        block = content[0]
        declared = {item["name"] for item in native._declared_tools(body)}
        if (not isinstance(block, dict) or set(block) != {"type", "id", "name", "input"}
                or block.get("type") != "tool_use" or block.get("name") not in declared
                or not isinstance(block.get("id"), str)
                or not re.fullmatch(r"toolu_devin_mcp_[a-f0-9]{32}", block["id"])
                or not isinstance(block.get("input"), dict)):
            raise WireError("invalid_native_result", 502)
    elif stop == "end_turn":
        native._text_blocks(content)
    else:
        raise WireError("invalid_native_result", 502)
    usage = _usage(result)
    return {"id": "msg_native_" + os.urandom(12).hex(), "type": "message", "role": "assistant",
            "model": result["model"], "content": content, "stop_reason": stop,
            "stop_sequence": None, "usage": usage}


def _sse(message):
    def event(kind, **fields):
        return ("event: " + kind + "\ndata: ").encode() + _json({"type": kind, **fields}) + b"\n\n"

    yield event("message_start", message={**message, "content": [], "stop_reason": None})
    for index, block in enumerate(message["content"]):
        if block["type"] == "tool_use":
            yield event("content_block_start", index=index, content_block={**block, "input": {}})
            yield event("content_block_delta", index=index, delta={"type": "input_json_delta",
                        "partial_json": _json(block["input"]).decode()})
        else:
            yield event("content_block_start", index=index,
                        content_block={"type": "text", "text": ""})
            yield event("content_block_delta", index=index,
                        delta={"type": "text_delta", "text": block["text"]})
        yield event("content_block_stop", index=index)
    yield event("message_delta", delta={"stop_reason": message["stop_reason"],
                                        "stop_sequence": None}, usage=message["usage"])
    yield event("message_stop")


def serve(input_, output, *, policy_loader=None, runner=None,
          cancelled: Optional[threading.Event] = None):
    """Exactly status then one request; injected loaders/runners permit fake-only tests."""
    policy_loader = load_policy if policy_loader is None else policy_loader
    runner = native.run_step if runner is None else runner
    cancelled = threading.Event() if cancelled is None else cancelled
    reader, writer = _Input(input_, cancelled), _Output(output)
    response_started = False
    try:
        meta = reader.frame()
        if set(meta) != {"version", "operation", "native"} or meta["operation"] != "status":
            raise WireError("native_status_required")
        context = _context(meta["native"])
        try:
            session = policy_loader(context)
        except WireError:
            raise
        except Exception:
            raise WireError("native_policy_unavailable", 503) from None
        binding = native.OwnershipBinding.from_dict(context["binding"])
        if (session.binding != binding or session.runtime_json != _json(context["runtime"]).decode()
                or not session.authorize(binding)):
            raise WireError("native_authorization_changed", 403)
        ready = {"ready": True, "protocol": 2, "provider": "devin", "bridge_contract": CONTRACT,
                 "binding_digest": binding.digest}
        writer.response(200, "application/json", [_json(ready)])
        reader.deadline = time.monotonic() + INPUT_TIMEOUT
        request = reader.frame(eof_ok=True)
        if request is None:
            return 0
        if (set(request) != {"version", "operation", "method", "path", "headers",
                            "body_size", "native"}
                or request["operation"] != "request" or request["method"] != "POST"
                or _context(request["native"]) != context):
            raise WireError("invalid_native_request")
        if request["path"] in {"/v1/messages/count_tokens", "/v1/messages/count_tokens?beta=true"}:
            raise WireError("native_count_tokens_unavailable", 501)
        if request["path"] != "/v1/messages":
            raise WireError("invalid_native_path")
        size = request["body_size"]
        if type(size) is not int or not 0 < size <= MAX_BODY:
            raise WireError("native_body_limit", 413)
        # Reuse the established header/privacy boundary; never send these
        # headers, Fleet credentials or structured client identity to Devin.
        native.relay.validate_request({key: value for key, value in request.items()
                                       if key != "native"})
        body = native.mcp._decode(reader.exact(size))
        body = native.validate_native_request(body,
            request_models=session.config.data["request_models"],
            effort=session.config.data["thought_level"])

        def authorize(current):
            try:
                if cancelled.is_set() or reader.disconnected():
                    cancelled.set()
                    return False
                return session.authorize(current)
            except Exception:
                cancelled.set()
                return False

        if not authorize(binding):
            raise WireError("native_authorization_changed", 403)
        result = runner(session.config, binding, context["request_id"], body,
                        authorize=authorize, cancel_event=cancelled)
        if not authorize(binding):
            raise WireError("native_authorization_changed", 403)
        message = _message(result, session.config, body)
        response_started = True
        if body.get("stream") is True:
            writer.response(200, "text/event-stream", _sse(message))
        else:
            writer.response(200, "application/json", [_json(message)])
        return 0
    except Exception as error:
        cancelled.set()
        if not response_started:
            status = error.status if isinstance(error, WireError) else (
                400 if isinstance(error, (native.NativeError, native.mcp.Rejected,
                                          native.relay.RelayError, EOFError)) else 503)
            try:
                message = "Native request rejected; do not retry automatically."
                writer.response(status, "application/json", [_json({"type": "error", "error": {
                    "type": "ccfleet_native_error", "message": message}})])
            except (OSError, ValueError):
                pass
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-protocol-v1", action="store_true")
    args = parser.parse_args(argv)
    if not args.gateway_protocol_v1:
        print(json.dumps({"execute": False, "protocol": 2, "model_requests": 0,
                          "fixed_command": "ccfleet-devin-gateway-v1"}))
        return 0
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: cancelled.set())
    return serve(sys.stdin.buffer, sys.stdout.buffer, cancelled=cancelled)


if __name__ == "__main__":
    raise SystemExit(main())
