"""Restricted server-to-slot SSH transport for the public model gateway.

Readiness and the model request share one SSH channel. Before readiness succeeds
no model data is sent; after the first request-frame write a transport failure
is ambiguous and must never cause an automatic retry.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ccfleet_agent import inference_client as wire

from . import cli_access, gateway_providers

NATIVE_MESSAGE_PATHS = frozenset({"/v1/messages", "/v1/messages?beta=true"})


class GatewayTransportError(Exception):
    def __init__(self, message: str = "slot transport failed", *,
                 status: int = 502, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.retryable = status, retryable


def response_metadata(stream) -> dict[str, Any]:
    try:
        size = struct.unpack("!I", wire.read_exact(stream, 4))[0]
        if not 0 < size <= wire.MAX_META:
            raise ValueError("size")
        value = wire._json(wire.read_exact(stream, size))
        if (not isinstance(value, dict)
                or set(value) != {"version", "status", "headers", "retryable"}
                or type(value["version"]) is not int or value["version"] != 2
                or type(value["status"]) is not int or not 200 <= value["status"] <= 599
                or 300 <= value["status"] < 400 or not isinstance(value["headers"], dict)
                or type(value["retryable"]) is not bool
                or (value["status"] < 400 and value["retryable"])):
            raise ValueError("schema")
        for name, item in value["headers"].items():
            if name == "content-type":
                if item not in {"application/json", "text/event-stream"}:
                    raise ValueError("media type")
            elif name == "retry-after":
                if (not isinstance(item, str) or not item.isascii() or not item.isdigit()
                        or len(item) > 6 or int(item) > 86400):
                    raise ValueError("retry after")
            else:
                raise ValueError("header")
        return value
    except (OSError, ValueError, KeyError, TypeError, EOFError, RecursionError) as exc:
        raise GatewayTransportError() from exc


class SSHAttempt:
    """One bounded request, with an inference-only service identity."""

    def __init__(self, target: dict[str, Any], cfg: Any) -> None:
        self.target, self.cfg = target, cfg
        self.process = None
        self.directory = None
        self.model_started = False
        self.writer = None
        self.write_error = None
        self.closed = threading.Event()
        self.ready = False

    def _remote_command(self) -> str:
        return "ccfleet-gateway-v1"

    def _status_fields(self) -> dict[str, Any]:
        return {"operation": "status"}

    def _request_fields(self, headers, body, path) -> dict[str, Any]:
        return {"operation": "request", "method": "POST", "path": path,
                "headers": headers, "body_size": len(body)}

    def _ready_body(self) -> dict[str, Any]:
        return {"ready": True, "protocol": 2}

    def _validate_ready(self, value) -> bool:
        return (isinstance(value, dict) and value.get("ready") is True
                and type(value.get("protocol")) is int and value == self._ready_body())

    def _command(self) -> list[str]:
        key = Path(self.cfg.gateway_ssh_key_path)
        info = key.lstat()
        if (not key.is_absolute() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or not 0 < info.st_size <= 16384):
            raise GatewayTransportError("gateway service key is unavailable", status=503)
        executable = shutil.which("ssh", path=os.defpath)
        if executable is None:
            raise GatewayTransportError("gateway SSH is unavailable", status=503)
        user = self.target["unix_user"]
        host = self.target["access_host"]
        port = self.target["access_port"]
        pin = cli_access.normalize_public_key(self.target["ssh_host_key"])
        if (not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user)
                or not isinstance(host, str) or not host or host.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in host)
                or type(port) is not int or not 1 <= port <= 65535 or pin is None):
            raise GatewayTransportError("slot access is unavailable", status=503,
                                        retryable=True)
        self.directory = tempfile.TemporaryDirectory(prefix="ccfleet-gateway-")
        known = Path(self.directory.name) / "known_hosts"
        known.write_text("ccfleet-gateway-slot " + pin + "\n")
        known.chmod(0o600)
        return [executable, "-T", "-F", "/dev/null", "-p", str(port),
                "-o", "HostKeyAlias=ccfleet-gateway-slot",
                "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
                "-o", "IdentitiesOnly=yes", "-o", f"IdentityFile={key}",
                "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
                "-o", "PermitLocalCommand=no", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                f"{user}@{host}", self._remote_command()]

    def _spawn(self, command):
        return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"})

    def start(self, headers: dict[str, str], body: bytes, *, path: str = "/v1/messages",
              before_model_send=lambda: True) -> None:
        try:
            if self.closed.is_set():
                raise GatewayTransportError(retryable=True)
            command = self._command()
            if self.closed.is_set():
                self.close()
                raise GatewayTransportError(retryable=True)
            self.process = self._spawn(command)
            if self.closed.is_set():
                self.close()
                raise GatewayTransportError(retryable=True)
            self.process.stdin.write(wire.preamble(self._status_fields()))
            self.process.stdin.flush()
            ready = response_metadata(self.process.stdout)
            if ready["status"] != 200:
                raise GatewayTransportError("slot is not ready", status=ready["status"],
                                            retryable=True)
            raw = bytearray()
            for part in self.chunks():
                raw.extend(part)
                if len(raw) > 1024:
                    raise ValueError("oversized readiness")
            if not self._validate_ready(wire._json(bytes(raw))):
                raise ValueError("invalid readiness")
            self.ready = True
            self._send_request(headers, body, path=path, before_model_send=before_model_send)
        except GatewayTransportError as exc:
            if not self.model_started and exc.status == 502:
                exc.retryable = True
            raise
        except (OSError, ValueError, EOFError, KeyError, TypeError) as exc:
            raise GatewayTransportError(retryable=not self.model_started) from exc

    def _send_request(self, headers, body, *, path="/v1/messages", before_model_send):
        if not before_model_send():
            raise GatewayTransportError("request authorization changed", status=403)
        # No automatic replay is safe after this point, even if a pipe write
        # only partially completes or the peer vanishes before metadata.
        self.model_started = True

        def write_request() -> None:
            try:
                self.process.stdin.write(wire.preamble(
                    self._request_fields(headers, body, path)))
                self.process.stdin.write(body)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                self.write_error = exc
                self.close()

        self.writer = threading.Thread(target=write_request, daemon=True)
        self.writer.start()

    def metadata(self) -> dict[str, Any]:
        return response_metadata(self.process.stdout)

    def check(self) -> bool:
        """Readiness only: no model request frame or body is ever written."""
        try:
            self.start({}, b"", before_model_send=lambda: False)
        except GatewayTransportError:
            return self.ready
        finally:
            self.close()
        return False

    def chunks(self) -> Iterator[bytes]:
        try:
            while True:
                part = wire.chunk(self.process.stdout)
                if not part:
                    break
                yield part
        except (OSError, EOFError, ValueError) as exc:
            raise GatewayTransportError(retryable=not self.model_started) from exc

    def finish(self, status: int = 200, *, timeout: float = 5) -> None:
        try:
            code = self.process.wait(timeout=timeout)
            if code != 0 and not (code == 2 and status >= 400):
                raise ValueError("unexpected relay exit")
            if self.process.stdout.read(1):
                raise ValueError("trailing relay output")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise GatewayTransportError() from exc

    def close(self) -> None:
        self.closed.set()
        process = self.process
        if process is not None:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                with contextlib.suppress(OSError, ValueError):
                    if stream is not None:
                        stream.close()
        if self.directory is not None:
            with contextlib.suppress(OSError):
                self.directory.cleanup()


class NativeSSHAttempt(SSHAttempt):
    """A native-agent request bound to one authoritative owner/claim/runtime.

    This is a separate fixed command, never an Anthropic credential relay. The
    context is detached and validated before spawning; status contains no model
    body, and the inherited authorization callback gates the request-frame write.
    """

    def __init__(self, target: dict[str, Any], cfg: Any) -> None:
        super().__init__(target, cfg)
        try:
            value = target["_native_context"]
            if (not isinstance(value, dict) or set(value) != {"binding", "runtime", "request_id"}
                    or not isinstance(value["binding"], dict)
                    or set(value["binding"]) != {"owner_id", "key_id", "key_generation",
                        "slot_id", "claim_id", "claim_generation", "provider_generation",
                        "provider"}
                    or not isinstance(value["request_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", value["request_id"])):
                raise ValueError("context")
            binding = dict(value["binding"])
            if (binding["provider"] != gateway_providers.DEVIN
                    or any(not isinstance(binding[name], str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", binding[name])
                        for name in ("owner_id", "key_id", "slot_id"))
                    or not isinstance(binding["claim_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", binding["claim_id"])
                    or any(type(binding[name]) is not int or not 1 <= binding[name] <= 2**31-1
                        for name in ("key_generation", "claim_generation", "provider_generation"))):
                raise ValueError("binding")
            runtime = value["runtime"]
            if (not isinstance(runtime, dict) or set(runtime) != {
                    "bridge_contract", "native_version", "adapter_revision", "model_map_digest",
                    "provider_account_fp"}):
                raise ValueError("runtime")
            profile = gateway_providers.clean_runtime(
                {name: item for name, item in runtime.items() if name != "provider_account_fp"})
            account_fp = runtime["provider_account_fp"]
            if (profile["bridge_contract"] != gateway_providers.NATIVE_AGENT_CONTRACT
                    or not isinstance(account_fp, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", account_fp)
                    or target.get("provider") != gateway_providers.DEVIN
                    or any(binding[name] != target[field] for name, field in (
                        ("slot_id", "slot_id"), ("claim_id", "claim_id"),
                        ("claim_generation", "claim_generation"),
                        ("provider_generation", "provider_binding_generation")))
                    or any(runtime[name] != target[name] for name in runtime)):
                raise ValueError("target")
            self.native = {"binding": binding, "runtime": {**profile,
                "provider_account_fp": account_fp}, "request_id": value["request_id"]}
            canonical = json.dumps(binding, ensure_ascii=False, allow_nan=False,
                                   sort_keys=True, separators=(",", ":")).encode()
            self.binding_digest = hashlib.sha256(canonical).hexdigest()
        except (ValueError, KeyError, TypeError, RecursionError, UnicodeError) as exc:
            raise GatewayTransportError("native slot context is unavailable", status=503) from exc

    def _remote_command(self) -> str:
        return "ccfleet-devin-gateway-v1"

    def _status_fields(self) -> dict[str, Any]:
        return {**super()._status_fields(), "native": self.native}

    def _request_fields(self, headers, body, path) -> dict[str, Any]:
        return {**super()._request_fields(headers, body, path), "native": self.native}

    def _ready_body(self) -> dict[str, Any]:
        return {"ready": True, "protocol": 2, "provider": gateway_providers.DEVIN,
                "bridge_contract": gateway_providers.NATIVE_AGENT_CONTRACT,
                "binding_digest": self.binding_digest}

    def start(self, headers: dict[str, str], body: bytes, *, path: str = "/v1/messages",
              before_model_send=lambda: True) -> None:
        if path not in NATIVE_MESSAGE_PATHS:
            raise GatewayTransportError("native token counting is unavailable", status=501)
        super().start(headers, body, path=path, before_model_send=before_model_send)


def transport_attempt(target, cfg):
    """Dispatch only from a server-owned provider snapshot; legacy defaults stay put."""
    provider = target.get("provider", gateway_providers.ANTHROPIC)
    if provider == gateway_providers.DEVIN:
        return NativeSSHAttempt(target, cfg)
    if provider != gateway_providers.ANTHROPIC:
        raise GatewayTransportError("slot provider is unavailable", status=503)
    return SSHAttempt(target, cfg)
