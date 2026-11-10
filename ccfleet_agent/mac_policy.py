"""Explicit single-owner Mac policy; never used to weaken hosted Linux gates.

The Mac's trusted operator owns this opt-in and the native account. Every path
component is checked without symlink traversal; other users may not write it.
"""
from __future__ import annotations

import json
import math
import os
import pwd
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Optional

CONFIG_PARTS = (".config", "ccfleet", "private-owner")
FILES = frozenset({"policy.json", "agent.json", "grants.json", "maintenance.json",
                   "relay-quarantine.json"})
MAX_BYTES = 64 * 1024
GRANT_TTL = 180
_TRUST_ROOT = Path("/")


class MacPolicyError(ValueError):
    """A fixed-message failure of the explicit private-owner boundary."""


def authenticated_owner(home: Optional[Path] = None):
    owner = pwd.getpwuid(os.getuid())
    actual = Path(owner.pw_dir)
    if (os.getuid() == 0 or owner.pw_uid != os.getuid() or not actual.is_absolute()
            or ".." in actual.parts or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", owner.pw_name)
            or (home is not None and Path(home) != actual)):
        raise MacPolicyError("private owner identity is unavailable")
    return owner


def _check_directory(fd: int, *, private: bool = False) -> None:
    info = os.fstat(fd)
    uid = os.getuid()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid)
            or info.st_mode & 0o022
            or private and (info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700)):
        raise MacPolicyError("private owner directory is unsafe")


def open_directory(path: Path, *, private: bool = False) -> int:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise MacPolicyError("private owner path must be absolute")
    try:
        parts = path.relative_to(_TRUST_ROOT).parts
    except ValueError:
        raise MacPolicyError("private owner path is outside its trusted root") from None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(_TRUST_ROOT, flags)
    try:
        _check_directory(fd, private=private and not parts)
        for index, part in enumerate(parts):
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            _check_directory(fd, private=private and index == len(parts) - 1)
        return fd
    except BaseException:
        os.close(fd)
        raise


def directory(home: Path) -> Path:
    authenticated_owner(home)
    return Path(home).joinpath(*CONFIG_PARTS)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MacPolicyError("private owner JSON has duplicate fields")
        result[key] = value
    return result


def _nonfinite(_value):
    raise MacPolicyError("private owner JSON has nonfinite values")


def read_json(home: Path, name: str) -> dict[str, Any]:
    if name not in FILES:
        raise MacPolicyError("private owner file is not supported")
    base = descriptor = None
    try:
        base = open_directory(directory(home), private=True)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=base)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
            raise MacPolicyError("private owner file is unsafe")
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            part = os.read(descriptor, min(8192, MAX_BYTES + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        if len(raw) > MAX_BYTES:
            raise MacPolicyError("private owner file is oversized")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        if not isinstance(value, dict):
            raise MacPolicyError("private owner file is not an object")
        return value
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise MacPolicyError("private owner configuration is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if base is not None:
            os.close(base)


def write_json(home: Path, name: str, value: dict[str, Any]) -> None:
    if name not in {"grants.json", "maintenance.json", "relay-quarantine.json"}:
        raise MacPolicyError("private owner file is not writable by this agent")
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise MacPolicyError("private owner state is oversized")
    base = open_directory(directory(home), private=True)
    temporary = ".private-owner-" + uuid.uuid4().hex
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=base)
        data = memoryview(raw)
        while data:
            count = os.write(descriptor, data)
            if count <= 0:
                raise OSError("incomplete private owner state")
            data = data[count:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.replace(temporary, name, src_dir_fd=base, dst_dir_fd=base)
        os.fsync(base)
    except OSError:
        raise MacPolicyError("private owner state could not be saved") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=base)
        except FileNotFoundError:
            pass
        os.close(base)


def policy(home: Path) -> dict[str, Any]:
    owner = authenticated_owner(home)
    value = read_json(home, "policy.json")
    if (set(value) != {"version", "enabled", "unix_user", "bound_fp"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["enabled"]) is not bool or value["unix_user"] != owner.pw_name
            or not isinstance(value["bound_fp"], str)
            or not re.fullmatch(r"[0-9a-f]{16}", value["bound_fp"])):
        raise MacPolicyError("private owner policy is invalid")
    return value


def require_enabled(home: Path, *, now: Optional[float] = None,
                    monotonic: Optional[float] = None) -> None:
    value = policy(home)
    if value["enabled"] is not True:
        raise MacPolicyError("private owner inference is disabled")
    state = read_json(home, "grants.json")
    now = time.time() if now is None else now
    monotonic = time.monotonic() if monotonic is None else monotonic
    for key, current in (("received_at", now), ("received_monotonic", monotonic)):
        stamp = state.get(key)
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not 0 <= current - stamp <= GRANT_TTL):
            raise MacPolicyError("private owner authorization is stale")
    access = state.get("access")
    if (state.get("version") != 1 or not isinstance(access, dict)
            or access.get("version") != 1 or access.get("enabled") is not True
            or access.get("unix_user") != value["unix_user"]):
        raise MacPolicyError("private owner authorization is disabled")
