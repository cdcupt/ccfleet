"""Converge only the private Mac owner's restricted inference-key block.

The already signed-in local owner is trusted. Other administrator SSH entries
are retained byte-for-byte; this module never edits sshd configuration, creates
accounts, changes the account binding, or accepts a command from the server.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import os
import re
import secrets
import stat
import struct
import time
from pathlib import Path
from typing import Any, Optional

try:
    from . import mac_policy
except ImportError:
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_mac_policy", Path(__file__).with_name("mac_policy.py"))
    assert _spec is not None and _spec.loader is not None
    mac_policy = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mac_policy)

MAX_AUTHORIZED_KEYS = 1024 * 1024
MAX_DEVICE_KEYS = 10
MAX_GATEWAY_KEYS = 8
BEGIN = b"# BEGIN CCFLEET PRIVATE OWNER INFERENCE KEYS v1"
END = b"# END CCFLEET PRIVATE OWNER INFERENCE KEYS v1"
FIELDS = {"version", "unix_user", "enabled", "device_public_keys", "gateway_public_keys"}


class AccessError(ValueError):
    """A fixed access snapshot or owner-controlled SSH path is unsafe."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AccessError(message)


def _key(value: Any) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 1024 and value.isascii()
             and not any(ord(char) < 32 or ord(char) == 127 for char in value),
             "invalid private-owner public key")
    fields = value.split()
    _require(len(fields) >= 2 and fields[0] == "ssh-ed25519", "Ed25519 public key required")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except ValueError:
        raise AccessError("invalid Ed25519 public key") from None
    prefix = struct.pack("!I", 11) + b"ssh-ed25519" + struct.pack("!I", 32)
    _require(len(blob) == len(prefix) + 32 and blob.startswith(prefix),
             "invalid Ed25519 public key")
    _require(base64.b64encode(blob).decode() == fields[1], "noncanonical Ed25519 public key")
    return "ssh-ed25519 " + fields[1]


def validate_snapshot(value: Any) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == FIELDS,
             "invalid private-owner access fields")
    _require(type(value["version"]) is int and value["version"] == 1
             and type(value["enabled"]) is bool, "invalid private-owner access version or state")
    _require(isinstance(value["unix_user"], str)
             and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value["unix_user"]) is not None,
             "invalid private-owner Unix user")
    result = {name: value[name] for name in ("version", "unix_user", "enabled")}
    all_keys: set[str] = set()
    for name, maximum in (("device_public_keys", MAX_DEVICE_KEYS),
                          ("gateway_public_keys", MAX_GATEWAY_KEYS)):
        supplied = value[name]
        _require(isinstance(supplied, list) and len(supplied) <= maximum,
                 "private-owner key count exceeds its bound")
        keys = [_key(key) for key in supplied]
        _require(len(set(keys)) == len(keys) and not all_keys.intersection(keys),
                 "duplicate private-owner public key")
        all_keys.update(keys)
        result[name] = sorted(keys)
    return result


def _parts(raw: bytes) -> tuple[bytes, bytes]:
    """Return the unmanaged prefix/suffix, rejecting ambiguous marker placement."""
    starts, ends = raw.count(BEGIN), raw.count(END)
    _require(starts == ends and starts <= 1, "ambiguous private-owner SSH key block")
    if starts == 0:
        return raw, b""
    first, last = raw.index(BEGIN), raw.index(END)
    after = last + len(END)
    _require(first < last and (first == 0 or raw[first - 1:first] == b"\n")
             and raw[first + len(BEGIN):first + len(BEGIN) + 1] == b"\n"
             and raw[last - 1:last] == b"\n"
             and (after == len(raw) or raw[after:after + 1] == b"\n"),
             "malformed private-owner SSH key block")
    if raw[after:after + 1] == b"\n":
        after += 1
    return raw[:first], raw[after:]


def _unmanaged_keys(raw: bytes) -> set[str]:
    result = set()
    for line in raw.splitlines():
        if line.lstrip().startswith(b"#"):
            continue
        pattern = rb"(?:^|[ \t])ssh-ed25519[ \t]+([A-Za-z0-9+/]+={0,2})(?=[ \t]|$)"
        for match in re.finditer(pattern, line):
            result.add("ssh-ed25519 " + match.group(1).decode())
    return result


def _render(raw: bytes, snapshot: dict[str, Any], entrypoint: Path) -> bytes:
    prefix, suffix = _parts(raw)
    enabled = snapshot["enabled"]
    devices = snapshot["device_public_keys"] if enabled else []
    gateways = snapshot["gateway_public_keys"] if enabled else []
    _require(not _unmanaged_keys(prefix + suffix).intersection(devices + gateways),
             "a managed inference key already exists outside its restricted block")
    command = "/usr/bin/python3 -I -B " + str(entrypoint)
    lines = [BEGIN]
    for key in devices:
        lines.append(f'restrict,command="{command}" {key} ccfleet-private-owner-device'.encode())
    for key in gateways:
        line = (f'restrict,command="{command} --gateway-only" '
                f'{key} ccfleet-private-owner-gateway')
        lines.append(line.encode())
    lines.append(END)
    separator = b"\n" if prefix and not prefix.endswith(b"\n") else b""
    return prefix + separator + b"\n".join(lines) + b"\n" + suffix


def _file_info(info: os.stat_result, uid: int) -> None:
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, "unsafe owner SSH file")


def _read(directory: int, uid: int) -> tuple[bytes, Optional[tuple[int, int, int, int]]]:
    try:
        fd = os.open("authorized_keys", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
    except FileNotFoundError:
        return b"", None
    try:
        info = os.fstat(fd)
        _file_info(info, uid)
        _require(info.st_size <= MAX_AUTHORIZED_KEYS, "owner SSH key file exceeds its bound")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_AUTHORIZED_KEYS + 1)
        _require(len(raw) <= MAX_AUTHORIZED_KEYS, "owner SSH key file exceeds its bound")
        return raw, (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    finally:
        os.close(fd)


def _lock(directory: int, uid: int) -> int:
    fd = os.open(".ccfleet-private-owner-keys.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                 dir_fd=directory)
    try:
        _file_info(os.fstat(fd), uid)
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                _require(time.monotonic() < deadline, "owner SSH key update is busy")
                time.sleep(0.05)
    except BaseException:
        os.close(fd)
        raise


def _entrypoint(path: Path, uid: int) -> Path:
    _require(path.is_absolute() and ".." not in path.parts
             and re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)) is not None
             and path.name == "mac_slot_entry.py", "invalid fixed Mac inference entrypoint")
    directory = mac_policy.open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, uid)
                     and info.st_nlink == 1 and not info.st_mode & 0o022,
                     "unsafe fixed Mac inference entrypoint")
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    return path


def reconcile(value: Any, *, home: Optional[Path] = None,
              entrypoint: Optional[Path] = None) -> dict[str, Any]:
    snapshot = validate_snapshot(value)
    owner = mac_policy.authenticated_owner(home)
    _require(snapshot["unix_user"] == owner.pw_name,
             "access snapshot targets a different Unix user")
    home = Path(owner.pw_dir)
    entry = _entrypoint(entrypoint or Path(__file__).absolute().with_name("mac_slot_entry.py"),
                        owner.pw_uid)
    home_fd = mac_policy.open_directory(home)
    try:
        try:
            os.mkdir(".ssh", 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
    finally:
        os.close(home_fd)
    directory = mac_policy.open_directory(home / ".ssh", private=True)
    lock = None
    temporary = None
    try:
        lock = _lock(directory, owner.pw_uid)
        original, identity = _read(directory, owner.pw_uid)
        updated = _render(original, snapshot, entry)
        _require(len(updated) <= MAX_AUTHORIZED_KEYS,
                 "updated owner SSH key file exceeds its bound")
        changed = updated != original
        if changed:
            temporary = ".ccfleet-keys-" + secrets.token_hex(12)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            current, current_identity = _read(directory, owner.pw_uid)
            _require(current_identity == identity and hashlib.sha256(current).digest()
                     == hashlib.sha256(original).digest(), "owner SSH keys changed during update")
            os.replace(temporary, "authorized_keys", src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
        return {"device_keys": len(snapshot["device_public_keys"]) if snapshot["enabled"] else 0,
                "gateway_keys": len(snapshot["gateway_public_keys"]) if snapshot["enabled"] else 0,
                "changed": changed}
    except OSError:
        raise AccessError("owner SSH key update could not be completed safely") from None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
        if lock is not None:
            os.close(lock)
        os.close(directory)
