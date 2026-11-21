"""Read the default native Claude credential from the owner's Mac Keychain.

No token is written, copied to another store, refreshed, or included in an
exception. The native Claude application remains the sole credential writer.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from . import mac_policy
except ImportError:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("ccfleet_mac_policy",
                                                  Path(__file__).with_name("mac_policy.py"))
    assert _spec is not None and _spec.loader is not None
    mac_policy = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mac_policy)

SECURITY = "/usr/bin/security"
SERVICE = "Claude Code-credentials"
MAX_BYTES = 64 * 1024
KEYCHAIN_TIMEOUT = 5.0


class CredentialError(ValueError):
    """Only fixed, non-secret messages may leave this adapter."""


def native_environment(home: Path) -> dict[str, str]:
    owner = mac_policy.authenticated_owner(home)
    return {"HOME": str(home), "USER": owner.pw_name, "LOGNAME": owner.pw_name,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "DISABLE_AUTOUPDATER": "1"}


def bounded_output(argv: list[str], home: Path, *, timeout: float,
                   limit: int = MAX_BYTES) -> tuple[int, bytes]:
    """Capture bounded private stdout; discard stderr and reap only our child."""
    process: Optional[subprocess.Popen] = None
    collected = bytearray()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=native_environment(home),
                                   cwd=home, close_fds=True)
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if not selector.select(max(0, remaining)):
                    break
                part = os.read(process.stdout.fileno(), min(8192, limit + 1 - len(collected)))
                if not part:
                    code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    return code, bytes(collected)
                collected.extend(part)
                if len(collected) > limit:
                    raise CredentialError("native credential output is oversized")
        raise CredentialError("native credential access timed out")
    except (OSError, subprocess.SubprocessError):
        raise CredentialError("native credential access is unavailable") from None
    finally:
        collected.clear()
        if process is not None:
            if process.poll() is None:
                process.kill()
            if process.stdout is not None:
                process.stdout.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError("native credential has duplicate fields")
        result[key] = value
    return result


def _nonfinite(_value):
    raise CredentialError("native credential has invalid numbers")


def read_oauth(home: Path) -> dict:
    if sys.platform != "darwin":
        raise CredentialError("native Keychain credentials require macOS")
    owner = mac_policy.authenticated_owner(home)
    code, raw = bounded_output(
        [SECURITY, "find-generic-password", "-s", SERVICE, "-a", owner.pw_name, "-w"],
        home, timeout=KEYCHAIN_TIMEOUT)
    if code != 0 or not raw or len(raw) > MAX_BYTES:
        raise CredentialError("native Keychain credential is unavailable")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise CredentialError("native Keychain credential is invalid") from None
    oauth = value.get("claudeAiOauth") if isinstance(value, dict) else None
    if not isinstance(oauth, dict):
        raise CredentialError("native Keychain credential is invalid")
    # Only the access credential and expiry are consumed by the relay. A refresh
    # credential is never returned to callers, persisted, or used by this code.
    return {key: oauth.get(key) for key in ("accessToken", "expiresAt", "subscriptionType")}
