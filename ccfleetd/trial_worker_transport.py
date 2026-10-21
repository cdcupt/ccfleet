"""One fixed-command, pinned-host trial SSH turn; no fallback or replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import time

from ccfleet_agent import inference_client as wire

from . import cli_access
from .gateway_transport import GatewayTransportError, SSHAttempt

MAX_RESULT = 2 * 1024 * 1024
MODEL = "claude-opus-5-5"
# Readiness performs no model work. After admission, leave time for bounded
# SSH teardown (two seconds) and ledger settlement before the 160-second permit.
READINESS_SECONDS = 10
TURN_SECONDS = 155


class _Reader:
    def __init__(self, stream, deadline, authorized):
        self.stream, self.deadline, self.authorized = stream, deadline, authorized

    def close(self):
        self.stream.close()

    def read(self, size):
        while time.monotonic() < self.deadline:
            if self.authorized() is not True:
                raise GatewayTransportError("trial authorization expired", status=403)
            if select.select([self.stream], [], [], 0.05)[0]:
                return os.read(self.stream.fileno(), size)
        raise GatewayTransportError("trial worker deadline", status=504)


class TrialAttempt(SSHAttempt):
    def _remote_command(self):
        return "ccfleet-devin-trial-v1"

    def _ready_body(self):
        return {
            "ready": True,
            "protocol": 2,
            "trial": True,
            "model": MODEL,
            "policy_sha256": self.target["policy_sha256"],
        }

    def _spawn(self, command):
        process = super()._spawn(command)
        process.stdout = _Reader(process.stdout, self._deadline, self._authorized)
        return process

    def prepare(self, authorized):
        self._deadline = time.monotonic() + READINESS_SECONDS
        self._authorized = authorized
        try:
            self.start({}, b"", before_model_send=lambda: False)
        except GatewayTransportError:
            if not self.ready:
                raise
        # Keep this same SSH connection open. The worker waits for its request
        # frame; no body or model request has been sent during readiness.

    def perform(self, body, authorized):
        # Wrap the actual child stdout at creation, so readiness, response and
        # terminal framing all share one wall deadline and revocation poll.
        self._deadline = time.monotonic() + TURN_SECONDS
        self._authorized = authorized
        if self.ready:
            self.process.stdout.deadline = self._deadline
            self.process.stdout.authorized = authorized
            self._send_request({}, body, before_model_send=authorized)
        else:
            self.start({}, body, before_model_send=authorized)
        metadata = self.metadata()
        if metadata["status"] != 200:
            raise GatewayTransportError("trial worker rejected", status=503)
        raw = bytearray()
        for part in self.chunks():
            raw.extend(part)
            if len(raw) > MAX_RESULT:
                raise GatewayTransportError("trial worker result bound", status=502)
        self.finish(timeout=max(0, min(1, self._deadline - time.monotonic())))
        return json.loads(bytes(raw))


def protected_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or not 0 < info.st_size <= 8192
        ):
            raise ValueError("trial_worker_file_guard")
        raw = os.read(fd, 8193)
        after, current = os.fstat(fd), os.lstat(path)
        attributes = (
            "st_dev",
            "st_ino",
            "st_uid",
            "st_gid",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != info.st_size
            or any(getattr(info, name) != getattr(after, name) for name in attributes)
            or any(getattr(info, name) != getattr(current, name) for name in attributes)
        ):
            raise ValueError("trial_worker_file_guard")
        result = wire._json(raw)
        if (
            not isinstance(result, dict)
            or set(result)
            != {"access_host", "access_port", "unix_user", "ssh_host_key", "policy_sha256"}
            or not isinstance(result["access_host"], str)
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", result["access_host"]) is None
            or result["access_host"].startswith("-")
            or type(result["access_port"]) is not int
            or not 1 <= result["access_port"] <= 65535
            or not isinstance(result["unix_user"], str)
            or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", result["unix_user"]) is None
            or cli_access.normalize_public_key(result["ssh_host_key"]) is None
            or not isinstance(result["policy_sha256"], str)
            or len(result["policy_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in result["policy_sha256"])
        ):
            raise ValueError("trial_worker_file_guard")
        return result, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)
