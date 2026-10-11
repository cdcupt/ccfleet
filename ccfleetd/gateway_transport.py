"""Restricted server-to-slot SSH transport for the public model gateway.

Readiness and the model request share one SSH channel. Before readiness succeeds
no model data is sent; after the first request-frame write a transport failure
is ambiguous and must never cause an automatic retry.
"""
from __future__ import annotations

import contextlib
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

from . import cli_access


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
                f"{user}@{host}", "ccfleet-gateway-v1"]

    def start(self, headers: dict[str, str], body: bytes, *, path: str = "/v1/messages",
              before_model_send=lambda: True) -> None:
        try:
            if self.closed.is_set():
                raise GatewayTransportError(retryable=True)
            command = self._command()
            if self.closed.is_set():
                self.close()
                raise GatewayTransportError(retryable=True)
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"})
            if self.closed.is_set():
                self.close()
                raise GatewayTransportError(retryable=True)
            self.process.stdin.write(wire.preamble({"operation": "status"}))
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
            if wire._json(bytes(raw)) != {"ready": True, "protocol": 2}:
                raise ValueError("invalid readiness")
            self.ready = True
            if not before_model_send():
                raise GatewayTransportError("request authorization changed", status=403)
            # No automatic replay is safe after this point, even if a pipe write
            # only partially completes or the peer vanishes before metadata.
            self.model_started = True

            def write_request() -> None:
                try:
                    self.process.stdin.write(wire.preamble({
                        "operation": "request", "method": "POST", "path": path,
                        "headers": headers, "body_size": len(body)}))
                    self.process.stdin.write(body)
                    self.process.stdin.flush()
                except (OSError, ValueError) as exc:
                    self.write_error = exc
                    self.close()

            self.writer = threading.Thread(target=write_request, daemon=True)
            self.writer.start()
        except GatewayTransportError as exc:
            if not self.model_started and exc.status == 502:
                exc.retryable = True
            raise
        except (OSError, ValueError, EOFError, KeyError, TypeError) as exc:
            raise GatewayTransportError(retryable=not self.model_started) from exc

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

    def finish(self, status: int = 200) -> None:
        try:
            code = self.process.wait(timeout=5)
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
