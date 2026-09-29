"""Bounded, explicit project sharing without copying host filesystem metadata.

Only relative names, file contents, digests, and an executable flag cross the
wire. Git-ignored and sensitive files are not shared. Symlinks, hardlinks, special
files, and ambiguous cross-platform names fail closed. Filesystem operations use
directory descriptors and do not follow symlinks, including root ancestors.

This is a file-transfer boundary, not an OS sandbox against a malicious process
running as the same user. Callers must serialize apply operations and avoid
concurrent project edits: a race detected during apply can leave earlier changes
applied, with their originals retained in the separate backup directory.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Optional

MAX_FILES = 1000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_SCANNED_ENTRIES = 10000
EXCLUDED_PARTS = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".config", ".claude", ".codex", ".agents",
    ".ccfleet", ".idea", ".vscode", "node_modules", ".venv", "venv", "build", "dist",
    "__pycache__", ".ds_store", ".netrc", ".npmrc", ".pypirc", ".bash_history",
    ".zsh_history", "known_hosts", "authorized_keys",
})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class ProjectError(ValueError):
    """An unsafe project, invalid transfer, or conflicting update was rejected."""


def _excluded(path: str) -> bool:
    for part in path.split("/"):
        name = part.casefold()
        if (name in EXCLUDED_PARTS or name.startswith((".env", "id_rsa", "id_ed25519",
                                                     "credentials", "secrets"))
                or name.endswith((".pem", ".key"))):
            return True
    return False


def _path(path: Any) -> str:
    if not isinstance(path, str) or not path:
        raise ProjectError("invalid project path")
    try:
        path_bytes = path.encode("utf-8")
    except UnicodeError as exc:
        raise ProjectError("invalid project filename encoding") from exc
    if len(path_bytes) > MAX_PATH_BYTES:
        raise ProjectError("invalid project path")
    parts = path.split("/")
    if len(parts) > 64:
        raise ProjectError("project path is too deep")
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((" ", "."))
                or len(part.encode("utf-8")) > 255 or _RESERVED.match(part)
                or any(c in '<>:"\\|?*' or unicodedata.category(c).startswith("C")
                       for c in part)):
            raise ProjectError("unsafe or nonportable project path")
    if _excluded(path):
        raise ProjectError("sensitive or excluded project path")
    return path


def _paths(paths: Any) -> list[str]:
    if not isinstance(paths, dict) or len(paths) > MAX_FILES:
        raise ProjectError("invalid or oversized project file list")
    aliases = set()
    for path in paths:
        _path(path)
        alias = unicodedata.normalize("NFC", path).casefold()
        if alias in aliases:
            raise ProjectError("ambiguous project paths")
        aliases.add(alias)
    for alias in aliases:
        parts = alias.split("/")
        if any("/".join(parts[:i]) in aliases for i in range(1, len(parts))):
            raise ProjectError("project file is also a parent directory")
    return sorted(paths)


def validate_manifest(files: Any) -> dict[str, dict[str, Any]]:
    """Validate hash/execute-only state; reject metadata or unrecognized fields."""
    result = {}
    for path in _paths(files):
        item = files[path]
        if (not isinstance(item, dict) or set(item) != {"sha256", "executable"}
                or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])
                or type(item["executable"]) is not bool):
            raise ProjectError("invalid project manifest entry")
        result[path] = {"sha256": item["sha256"], "executable": item["executable"]}
    return result


def validate_snapshot(files: Any) -> dict[str, dict[str, Any]]:
    """Validate bounded canonical base64 payloads and their content digests."""
    result = {}
    total = 0
    for path in _paths(files):
        item = files[path]
        if (not isinstance(item, dict) or set(item) != {"data", "sha256", "executable"}
                or not isinstance(item["data"], str)
                or len(item["data"]) > 4 * ((MAX_FILE_BYTES + 2) // 3)):
            raise ProjectError("invalid project snapshot entry")
        state = validate_manifest({path: {key: item[key] for key in ("sha256", "executable")}})
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProjectError("invalid project file encoding") from exc
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ProjectError("project exceeds file transfer size limit")
        if (base64.b64encode(data).decode("ascii") != item["data"]
                or hashlib.sha256(data).hexdigest() != item["sha256"]):
            raise ProjectError("project content digest or encoding mismatch")
        result[path] = {"data": item["data"], **state[path]}
    return result


def manifest(files: Any) -> dict[str, dict[str, Any]]:
    return {path: {key: item[key] for key in ("sha256", "executable")}
            for path, item in validate_snapshot(files).items()}


def changes(base: Any, current: Any) -> list[dict[str, str]]:
    base, current = validate_manifest(base), validate_manifest(current)
    return [{"path": path, "status": "added" if path not in base else
             "deleted" if path not in current else "modified"}
            for path in sorted(base.keys() | current.keys()) if base.get(path) != current.get(path)]


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_root(root: Path, *, create: bool = False) -> int:
    """Walk every ancestor without following links; never resolve() untrusted paths."""
    root = _absolute(root)
    fd = os.open(os.path.sep, _DIR_FLAGS)
    try:
        for component in root.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        raise ProjectError("project directory is unavailable or contains a symlink") from exc


@contextlib.contextmanager
def _parent(root_fd: int, path: str, *, create: bool = False) -> Iterator[Optional[int]]:
    fd = os.dup(root_fd)
    try:
        for component in path.split("/")[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                yield None
                return
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mode, st.st_nlink,
            st.st_mtime_ns, st.st_ctime_ns)


def _read(root_fd: int, path: str) -> Optional[dict[str, Any]]:
    with _parent(root_fd, path) as parent:
        if parent is None:
            return None
        try:
            fd = os.open(path.split("/")[-1], _FILE_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ProjectError("project contains a hardlink or nonregular file")
            if before.st_size > MAX_FILE_BYTES:
                raise ProjectError("project file exceeds transfer size limit")
            pieces = []
            size = 0
            while size <= MAX_FILE_BYTES:
                part = os.read(fd, min(64 * 1024, MAX_FILE_BYTES + 1 - size))
                if not part:
                    break
                pieces.append(part)
                size += len(part)
            after = os.stat(path.split("/")[-1], dir_fd=parent, follow_symlinks=False)
            if (size > MAX_FILE_BYTES or _identity(before) != _identity(os.fstat(fd))
                    or _identity(before) != _identity(after)):
                raise ProjectError("project file changed during read")
            data = b"".join(pieces)
            return {"data": base64.b64encode(data).decode("ascii"),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "executable": bool(before.st_mode & 0o111)}
        finally:
            os.close(fd)


def _walk(fd: int, prefix: str = "", budget: Optional[list[int]] = None) -> list[str]:
    if budget is None:
        budget = [MAX_SCANNED_ENTRIES]
    paths = []
    with os.scandir(fd) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise ProjectError("project directory scan exceeds limit")
            name = entry.name
            path = f"{prefix}/{name}" if prefix else name
            if _excluded(path):
                continue
            _path(path)
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                try:
                    paths.extend(_walk(child, path, budget))
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                paths.append(path)
            else:
                raise ProjectError("project contains a symlink, hardlink, or special file")
            if len(paths) > MAX_FILES:
                raise ProjectError("project exceeds file count limit")
    return sorted(paths)


def _git_output(command: list[str], root: Path, env: dict[str, str], limit: int) -> bytes:
    """Bound output while reading, not after buffering an untrusted repository listing."""
    with subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as child:
        deadline = time.monotonic() + 10
        try:
            result = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ProjectError("project Git listing timed out")
                    part = os.read(child.stdout.fileno(), min(65536, limit + 1 - len(result)))
                    if not part:
                        break
                    result.extend(part)
                    if len(result) > limit:
                        raise ProjectError("project file listing exceeds limit")
            if child.wait(timeout=max(0.01, deadline - time.monotonic())):
                raise ProjectError("cannot safely read this project's Git ignore rules")
            return bytes(result)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def _git_configuration() -> tuple[list[str], dict[str, str]]:
    env = {"PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_OPTIONAL_LOCKS": "0"}
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-c", f"core.excludesFile={os.devnull}", "-c", f"core.hooksPath={os.devnull}"]
    return command, env


def _git_paths(root: Path, root_fd: int) -> Optional[list[str]]:
    """Use only this repository's ignore rules, not personal Git configuration."""
    try:
        marker = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(marker.st_mode):
        raise ProjectError("project Git metadata must not be a symlink")
    command, env = _git_configuration()
    try:
        top = _git_output(command + ["rev-parse", "--show-toplevel"], root, env,
                          MAX_PATH_BYTES * 4)
        if os.fsdecode(top).rstrip("\n") != str(root):
            raise ProjectError("Git repository must match the selected project root")
        result = _git_output(command + ["ls-files", "-co", "--exclude-standard", "-z"],
                             root, env, MAX_FILES * MAX_PATH_BYTES)
        paths = {os.fsdecode(name) for name in result.split(b"\0") if name}
        return sorted(path for path in paths if not _excluded(path))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError("cannot safely read this project's Git ignore rules") from exc


def _shared_paths(root: Path, tracked: Iterable[str]) -> list[str]:
    """Apply shared .gitignore rules without trusting or copying any real Git metadata."""
    if isinstance(tracked, (str, bytes)):
        raise ProjectError("invalid shared project file list")
    explicit = {}
    for index, path in enumerate(tracked):
        if index >= MAX_FILES:
            raise ProjectError("shared project file list exceeds limit")
        explicit[_path(path)] = None
    _paths(explicit)
    command, env = _git_configuration()
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    if os.path.commonpath((str(root), str(temporary_parent))) == str(root):
        temporary_parent = root.parent
    if root == temporary_parent:
        raise ProjectError("project root leaves no separate temporary directory")
    try:
        # Temp metadata contains no index, personal config, remotes, hooks, or
        # laptop filesystem paths. Only the project .gitignore files are used.
        with tempfile.TemporaryDirectory(prefix="ccfleet-ignore-", dir=temporary_parent) as temp:
            git_dir = str(Path(temp) / ".git")
            _git_output(command + ["init", "--bare", "--quiet", "--template=", git_dir],
                        root, env, MAX_PATH_BYTES)
            result = _git_output(
                command + [f"--git-dir={git_dir}", f"--work-tree={root}", "-c", "core.bare=false",
                           "ls-files", "--others", "--exclude-standard", "-z"],
                root, env, MAX_FILES * MAX_PATH_BYTES)
        names = {os.fsdecode(name) for name in result.split(b"\0") if name}
        names = {name for name in names if not _excluded(name)} | explicit.keys()
        return _paths(dict.fromkeys(names))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError("cannot safely read shared project ignore rules") from exc


def snapshot(root: Path, *, tracked: Optional[Iterable[str]] = None) -> dict[str, dict[str, Any]]:
    """Read selected contents without ownership, timestamps, or host environment.

    The slot supplies its independently stored tracked names. Shared .gitignore
    rules then exclude new generated files while previously approved names remain
    shareable, including tracked-but-ignored files. Real .git metadata is unused.
    With tracked=None, local Git selection (or a plain directory scan) is used.
    """
    root = _absolute(root)
    fd = _open_root(root)
    try:
        names = _shared_paths(root, tracked) if tracked is not None else _git_paths(root, fd)
        if names is None:
            names = _walk(fd)
        _paths(dict.fromkeys(names))
        files = {}
        total = 0
        for path in names:
            item = _read(fd, path)
            if item is None:
                # Tracked files deleted locally are intentionally absent in this snapshot.
                continue
            total += len(base64.b64decode(item["data"]))
            if total > MAX_TOTAL_BYTES:
                raise ProjectError("project exceeds transfer size limit")
            files[path] = item
        return files
    except (OSError, UnicodeError) as exc:
        raise ProjectError("project contains an inaccessible or unsafe path") from exc
    finally:
        os.close(fd)


def _state(item: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if item is None:
        return None
    return {key: item[key] for key in ("sha256", "executable")}


def _write(parent: int, name: str, item: dict[str, Any], *, replace: bool) -> None:
    temp = f".ccfleet-write-{uuid.uuid4().hex}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                 dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(base64.b64decode(item["data"]))
            stream.flush()
            os.fchmod(stream.fileno(), 0o700 if item["executable"] else 0o600)
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            # link/unlink is an atomic no-clobber install on both Linux and macOS.
            os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temp, dir_fd=parent)
    finally:
        try:
            os.unlink(temp, dir_fd=parent)
        except FileNotFoundError:
            pass


def _backup(root: Path, directory: Path) -> int:
    directory = _absolute(directory)
    if os.path.commonpath((str(root), str(directory))) in {str(root), str(directory)}:
        raise ProjectError("backup directory must be separate from the project")
    fd = _open_root(directory, create=True)
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or st.st_mode & 0o077 or os.listdir(fd):
        os.close(fd)
        raise ProjectError("backup directory must be empty, private, and owned by this user")
    return fd


def apply_snapshot(root: Path, files: Any, expected_manifest: Any, backup_dir: Path) -> None:
    """Apply a reviewed snapshot with conflict preflight and recoverable tracked deletes.

    Files absent from both manifests are untouched. Changed/deleted originals are
    copied to backup_dir before any project file is modified. File/directory shape
    changes and modifications since the expected manifest are rejected, not merged.
    No transaction is promised across a concurrent edit, I/O failure, or power loss.
    """
    files = validate_snapshot(files)
    expected = validate_manifest(expected_manifest)
    desired = manifest(files)
    root = _absolute(root)
    fd = _open_root(root)
    backup_fd = None
    try:
        originals = {}
        original_bytes = 0
        for path in sorted(expected.keys() | files.keys()):
            original = _read(fd, path)
            if _state(original) != expected.get(path):
                raise ProjectError(f"project conflict: {path}")
            if original is not None:
                original_bytes += len(base64.b64decode(original["data"]))
                if original_bytes > MAX_TOTAL_BYTES:
                    raise ProjectError("existing project exceeds transfer size limit")
            originals[path] = original
        pending = changes(expected, desired)
        if not pending:
            return
        backup_fd = _backup(root, backup_dir)
        for change in pending:
            path = change["path"]
            original = originals[path]
            if original is not None:
                with _parent(backup_fd, path, create=True) as parent:
                    _write(parent, path.split("/")[-1], original, replace=False)
        for change in pending:
            path = change["path"]
            if _state(_read(fd, path)) != expected.get(path):
                raise ProjectError(f"project changed during apply: {path}")
            with _parent(fd, path, create=path in files) as parent:
                name = path.split("/")[-1]
                if path not in files:
                    os.unlink(name, dir_fd=parent)
                else:
                    _write(parent, name, files[path], replace=path in expected)
    except OSError as exc:
        raise ProjectError("unsafe path or I/O failure; any originals are in the backup") from exc
    finally:
        if backup_fd is not None:
            os.close(backup_fd)
        os.close(fd)
