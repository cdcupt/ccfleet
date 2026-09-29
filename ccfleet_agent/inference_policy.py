"""Root-managed inference gates, independent of holder-writable slot homes.

An empty gate is an older, manually enabled slot. New managed gates additionally
require the machine's explicit opt-in on every relay check, so removing that
opt-in denies them immediately, even before the next machine heartbeat.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path
from typing import Optional

ROOT_UID = 0
MANAGED_MARKER = b"ccfleet-managed-inference-v1\n"
OPT_IN_NAME = "inference-enabled"
USER_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")
MAX_POLICY_BYTES = 128


class PolicyError(ValueError):
    """An operator policy is missing, unsafe or disabled."""


def _directory(path, *, dir_fd=None, path_only=False):
    # Slot users may traverse /etc/ccfleet (0711), but cannot list it. Linux
    # O_PATH obtains a dirfd without requiring read access. Gates themselves
    # are readable and use O_RDONLY because root enumerates and fsyncs them.
    access = getattr(os, "O_PATH", os.O_RDONLY) if path_only else os.O_RDONLY
    flags = access | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags, dir_fd=dir_fd)


def _trusted_directory(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID
            or info.st_mode & 0o022):
        raise PolicyError("inference policy directory is not root-controlled")


def _open_base(path: Path) -> int:
    """Walk without following any symlink; trust only the final policy base."""
    if not path.is_absolute() or ".." in path.parts:
        raise PolicyError("inference policy directory must be an absolute plain path")
    fd = _directory(path.anchor, path_only=True)
    try:
        for part in path.parts[1:]:
            child = _directory(part, dir_fd=fd, path_only=True)
            os.close(fd)
            fd = child
        _trusted_directory(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID
                or info.st_mode & 0o022 or info.st_nlink != 1
                or info.st_size > MAX_POLICY_BYTES):
            raise PolicyError("inference policy file is not root-controlled")
        content = os.read(fd, MAX_POLICY_BYTES + 1)
        if len(content) > MAX_POLICY_BYTES:
            raise PolicyError("inference policy file is too large")
        return content
    finally:
        os.close(fd)


def _mode(base: int) -> str:
    try:
        content = _read_file(base, OPT_IN_NAME)
    except FileNotFoundError:
        return "absent"
    except (OSError, PolicyError):
        return "invalid"
    if content == b"enabled\n":
        return "enabled"
    if content == b"disabled\n":
        return "disabled"
    return "invalid"


def require_enabled(policy_dir: Path, user: str) -> None:
    """Authorize a legacy manual or currently opted-in managed slot gate."""
    if not USER_RE.fullmatch(user):
        raise PolicyError("invalid inference slot user")
    base: Optional[int] = None
    gates: Optional[int] = None
    try:
        base = _open_base(policy_dir.parent)
        gates = _directory(policy_dir.name, dir_fd=base)
        _trusted_directory(gates)
        marker = _read_file(gates, user)
        mode = _mode(base)
        if mode == "disabled":
            raise PolicyError("local inference is disabled on this machine")
        if marker == MANAGED_MARKER and mode == "enabled":
            return
        if marker == b"" and mode in ("absent", "enabled"):
            return
        raise PolicyError("local inference is not enabled for this slot")
    except OSError as exc:
        raise PolicyError("local inference policy is unavailable") from exc
    finally:
        if gates is not None:
            os.close(gates)
        if base is not None:
            os.close(base)


def _replace_gate(directory: int, name: str) -> None:
    temporary = f".inference-{uuid.uuid4().hex}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o644, dir_fd=directory)
    try:
        remaining = memoryview(MANAGED_MARKER)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("incomplete inference policy write")
            remaining = remaining[count:]
        os.fchmod(fd, 0o644)
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def reconcile(policy_dir: Path, eligible_users: set[str]) -> None:
    """Converge an opted-in machine; opt-out revokes previously managed gates.

    Missing opt-in preserves *only* legacy empty manual gates, never creates a
    gate, and removes tagged managed gates. Explicit disabled revokes every
    slot gate. Invalid configuration cannot grant new access.
    """
    if len(eligible_users) > 1 or any(not USER_RE.fullmatch(u) for u in eligible_users):
        eligible_users = set()
    base: Optional[int] = None
    gates: Optional[int] = None
    try:
        try:
            base = _open_base(policy_dir.parent)
        except FileNotFoundError:
            return
        mode = _mode(base)
        try:
            gates = _directory(policy_dir.name, dir_fd=base)
        except FileNotFoundError:
            if mode != "enabled" or not eligible_users:
                return
            os.mkdir(policy_dir.name, 0o755, dir_fd=base)
            gates = _directory(policy_dir.name, dir_fd=base)
        _trusted_directory(gates)
        wanted = eligible_users if mode == "enabled" else set()
        errors = []
        for name in os.listdir(gates):
            if not USER_RE.fullmatch(name):
                continue
            try:
                try:
                    marker = _read_file(gates, name)
                except (OSError, PolicyError):
                    marker = None
                if name in wanted:
                    continue
                if mode in ("enabled", "disabled") or marker != b"":
                    os.unlink(name, dir_fd=gates)
            except OSError as exc:
                errors.append(exc)
        # Revoke every stale gate before creating a new one; a failed cleanup
        # must never leave two working account gates behind.
        if errors:
            raise PolicyError("could not revoke an obsolete inference gate")
        for user in wanted:
            try:
                current = _read_file(gates, user)
            except (OSError, PolicyError):
                current = None
            if current != MANAGED_MARKER:
                _replace_gate(gates, user)
        os.fsync(gates)
    except OSError as exc:
        raise PolicyError("could not reconcile inference gates") from exc
    finally:
        if gates is not None:
            os.close(gates)
        if base is not None:
            os.close(base)
