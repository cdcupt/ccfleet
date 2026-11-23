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
import fcntl
import hashlib
import json
import os
import pwd
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


def _account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)


def _private_record(registry, name):
    """An absent record can inherit; an invalid or inaccessible record must fail closed."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=registry)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                or st.st_mode & 0o077 or not 0 < st.st_size <= 2048):
            raise ProjectError("invalid local project access record")
        data = json.loads(os.read(fd, 2049))
        if not isinstance(data, dict):
            raise ProjectError("invalid local project access record")
        return data
    finally:
        os.close(fd)


def _write_private_record(registry, name, data):
    temp = "." + uuid.uuid4().hex
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                 dir_fd=registry)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, name, src_dir_fd=registry, dst_dir_fd=registry)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=registry)


def _generation(record):
    value = record.get("generation")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ProjectError("invalid local project access generation")
    return value


_PROJECT_MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Makefile")
_PERSONAL_COLLECTIONS = {"applications", "bin", "sbin", "desktop", "documents", "downloads",
                         "library", "movies", "music", "pictures", "public"}


def _owned_marker(directory, name, *, text=False):
    """Inspect one ordinary user-owned marker; never follow a link or block on a FIFO."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=directory)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_mode & 0o022):
            raise ProjectError("unsafe project marker")
        if text:
            if not 0 < st.st_size <= 4096:
                raise ProjectError("invalid Git checkout marker")
            return os.read(fd, 4097).decode("utf-8").rstrip("\n")
        return st
    finally:
        os.close(fd)


def _git_directory(directory):
    # HEAD, refs and the object store distinguish a checkout from an empty .git directory.
    st = os.fstat(directory)
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        return False
    head = _owned_marker(directory, "HEAD", text=True)
    if not re.fullmatch(r"(?:ref: refs/heads/[^\x00-\x20]+|[0-9a-f]{40}|[0-9a-f]{64})", head):
        return False
    for entry in ("objects", "refs"):
        child = os.stat(entry, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISDIR(child.st_mode) or child.st_uid != os.getuid() or child.st_mode & 0o022:
            return False
    return True


class ProjectAccess:
    """Resolve local access for a checkout and for projects selected on this Mac."""
    _ALL = "all-projects.json"

    def __init__(self, project: Path, fleet_home: Path):
        self.project = Path(os.path.abspath(project))
        self.fleet_home = Path(os.path.abspath(fleet_home))
        self._file = hashlib.sha256(os.fsencode(self.project)).hexdigest() + ".json"

    @contextlib.contextmanager
    def project_fd(self):
        # Never follow a replaced project or path component at execution time.
        project = self.project
        homes = {Path.home().resolve(), _account_home()}
        protected = [self.fleet_home.resolve(), Path("/Library/Keychains")]
        protected.extend(h / name for h in homes for name in (
            "Library", ".local", ".ssh", ".config", ".ccfleet", ".agents", ".claude",
            ".codex", ".gemini", ".gnupg", ".aws", ".azure", ".kube", ".password-store",
        ))
        def folded(path):
            return Path(unicodedata.normalize("NFD", os.fspath(path)).casefold())
        root = folded(project)
        for path in protected:
            target = folded(path)
            if root == target or target in root.parents or root in target.parents:
                raise ProjectError("choose a project outside personal or CCFleet settings")
        temp_root = Path(tempfile.gettempdir()).resolve()
        if (project in (temp_root, *temp_root.parents) or os.path.ismount(project)
                or _excluded(os.fspath(project))
                or any(root.is_relative_to(p) for p in ("/bin", "/sbin", "/usr", "/system",
                                                        "/library", "/applications", "/opt"))):
            raise ProjectError("choose an ordinary project folder for development access")
        fd = _open_root(project)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.getuid():
                raise ProjectError("development access requires a project owned by this user")
            # The same directory can have alternate names through macOS firmlinks.
            for ancestor in {p for path in [*protected, temp_root] for p in (path, *path.parents)}:
                try:
                    if os.path.samestat(st, ancestor.stat()):
                        raise ProjectError("choose a project outside personal or CCFleet settings")
                except FileNotFoundError:
                    pass
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _project_id(fd):
        st = os.fstat(fd)
        if hasattr(st, "st_birthtime"):
            # The system Python and newer Python clients must encode the same
            # birth time. Only newer Python exposes the precise nanosecond field.
            born = round(st.st_birthtime * 1_000_000_000)
        else:
            # The supported client is macOS. Linux fixtures use statx birth time
            # via coreutils rather than accepting an inode recycled by the filesystem.
            try:
                born = subprocess.check_output(
                    ["/usr/bin/stat", "-L", "-c", "%w", f"/proc/self/fd/{fd}"],
                    env={"LC_ALL": "C"}, pass_fds=(fd,), stderr=subprocess.DEVNULL,
                    timeout=3, text=True,
                ).strip()
            except (OSError, subprocess.SubprocessError) as exc:
                raise ProjectError("this filesystem cannot identify a project grant") from exc
            if not born or born == "-":
                raise ProjectError("this filesystem cannot identify a project grant")
        return [st.st_uid, st.st_dev, st.st_ino, born]

    @contextlib.contextmanager
    def _registry(self, create=False, *, write=False):
        # The directory is separate from files the stock project editor may write.
        # All child names are fixed or hashed, and all operations are relative to a held fd.
        root = _open_root(self.fleet_home, create=create)
        try:
            st = os.fstat(root)
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                raise ProjectError("the CCFleet settings directory must be private to this user")
            if create:
                try:
                    os.mkdir("project-access", 0o700, dir_fd=root)
                except FileExistsError:
                    pass
            fd = os.open("project-access", _DIR_FLAGS, dir_fd=root)
            try:
                st = os.fstat(fd)
                if st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise ProjectError("the project access registry must be private")
                fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(root)

    def is_home(self):
        return self.project == _account_home()

    @staticmethod
    def _home_id():
        fd = _open_root(_account_home())
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise ProjectError("the home directory belongs to another user")
            return ProjectAccess._project_id(fd)
        finally:
            os.close(fd)

    def _all_generation(self, registry):
        data = _private_record(registry, self._ALL)
        if data is None or data.get("home") != self._home_id():
            return None
        return _generation(data)

    def all_enabled(self):
        try:
            with self._registry() as registry:
                return self._all_generation(registry) is not None
        except (OSError, ValueError, RuntimeError, RecursionError):
            return False

    def navigation(self):
        return self.is_home() and self.all_enabled()

    def enable_all(self):
        if not self.is_home():
            raise ProjectError("enable all-projects access from your home directory")
        home_id = self._home_id()
        with self._registry(create=True, write=True) as registry:
            if self._all_generation(registry) is None:
                _write_private_record(registry, self._ALL,
                                      {"home": home_id, "generation": uuid.uuid4().hex})

    def disable_all(self):
        if not self.is_home():
            raise ProjectError("revoke all-projects access from your home directory")
        try:
            with self._registry(write=True) as registry:
                os.unlink(self._ALL, dir_fd=registry)
        except FileNotFoundError:
            pass
        except ProjectError:
            if self.fleet_home.exists() or self.fleet_home.is_symlink():
                raise

    def _ordinary_project_path(self):
        home = _account_home()
        if not self.project.is_relative_to(home):
            return False
        parts = tuple(p.casefold() for p in self.project.relative_to(home).parts)
        if (not parts or any(p.startswith(".") or p.endswith(".app") for p in parts)
                or parts[0] in ("applications", "bin", "sbin", "library")
                or parts[:2] in (("go", "bin"), ("go", "pkg"))):
            return False
        if len(parts) == 1 and parts[0] in _PERSONAL_COLLECTIONS:
            return False
        # An untrusted shared parent must not be able to redirect the grant to a
        # different checkout. Explicit grants continue to use the original policy.
        for ancestor in self.project.parents:
            if ancestor == home:
                break
            info = os.stat(ancestor, follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022):
                return False
        return True

    def _linked_checkout(self, project):
        marker = _owned_marker(project, ".git", text=True)
        if not marker.startswith("gitdir: "):
            return False
        target = self.project / marker.removeprefix("gitdir: ")
        gitdir = target.resolve(strict=True)
        if (gitdir != _absolute(target) or gitdir.parent.name != "worktrees"
                or gitdir.parent.parent.name != ".git"):
            return False
        main = ProjectAccess(gitdir.parents[2], self.fleet_home)
        if not main._ordinary_project_path():
            return False
        with main.project_fd() as common, _parent(common, ".git/worktrees/slot") as registry:
            if registry is None:
                return False
            fd = os.open(gitdir.name, _DIR_FLAGS, dir_fd=registry)
            try:
                st = os.fstat(fd)
                if st.st_uid != os.getuid() or st.st_mode & 0o022:
                    return False
                backlink = gitdir / _owned_marker(fd, "gitdir", text=True)
                if (_absolute(backlink) != self.project / ".git"
                        or backlink.resolve(strict=True) != self.project / ".git"
                        or _owned_marker(fd, "commondir", text=True) != "../.."
                        or not _owned_marker(fd, "HEAD", text=True)):
                    return False
            finally:
                os.close(fd)
            meta = os.open(".git", _DIR_FLAGS, dir_fd=common)
            try:
                return _git_directory(meta)
            finally:
                os.close(meta)

    def _eligible(self, project):
        if not self._ordinary_project_path() or os.fstat(project).st_mode & 0o022:
            return False
        try:
            marker = os.stat(".git", dir_fd=project, follow_symlinks=False)
        except FileNotFoundError:
            for name in _PROJECT_MANIFESTS:
                try:
                    if _owned_marker(project, name).st_size:
                        return True
                except FileNotFoundError:
                    continue
            return False
        if stat.S_ISREG(marker.st_mode):
            return self._linked_checkout(project)
        if not stat.S_ISDIR(marker.st_mode):
            return False
        fd = os.open(".git", _DIR_FLAGS, dir_fd=project)
        try:
            return _git_directory(fd)
        finally:
            os.close(fd)

    def eligible_checkout(self):
        try:
            with self.project_fd() as project:
                return self._eligible(project)
        except (OSError, ValueError, RuntimeError, RecursionError):
            return False

    def snapshot(self):
        """Return an opaque generation, optional pinned destination and exact project identity."""
        try:
            with self._registry() as registry, self.project_fd() as project:
                local = _private_record(registry, self._file)
                epoch = self._all_generation(registry)
                identity = self._project_id(project)
                if local is not None and "excluded" in local:
                    exclusion = local["excluded"]
                    if (set(local) != {"excluded"} or not isinstance(exclusion, str)
                            or not re.fullmatch(r"[0-9a-f]{32}", exclusion)
                            or epoch is None or exclusion == epoch):
                        return None
                    local = None
                if local is not None:
                    generation = _generation(local)
                    if not self._valid_remote(local.get("remote")):
                        return None
                    if local.get("project") == identity:
                        # Either grant changing invalidates overlapping active work.
                        if epoch:
                            generation = (generation, epoch)
                        return generation, local.get("remote"), identity
                    # A re-clone may independently qualify for the Mac grant.
                    # The previous checkout's publishing destination never follows it.
                if epoch is not None and self._eligible(project):
                    return epoch, None, identity
                return None
        except (OSError, ValueError, RuntimeError, RecursionError):
            return None

    @staticmethod
    def _valid_remote(remote):
        return remote is None or (
            isinstance(remote, str) and len(remote) <= 512
            and re.fullmatch(r"https://(?:github|gitlab)\.com/[A-Za-z0-9_.-]+"
                             r"(?:/[A-Za-z0-9_.-]+)+", remote) is not None
            and all(part not in (".", "..") for part in remote.split("/")[3:])
        )

    def enabled(self):
        return self.snapshot() is not None

    def remote(self):
        grant = self.snapshot()
        return grant[1] if grant else None

    def grant(self, remote=None):
        if not self._valid_remote(remote):
            raise ProjectError("invalid project publishing remote")
        with self.project_fd() as project, self._registry(create=True, write=True) as registry:
            data = {"project": self._project_id(project), "generation": uuid.uuid4().hex,
                    "remote": remote}
            if not os.path.samestat(os.fstat(project), self.project.stat()):
                raise ProjectError("project moved while granting access")
            _write_private_record(registry, self._file, data)

    def revoke(self):
        try:
            with self._registry(write=True) as fd:
                epoch = self._all_generation(fd)
                if epoch is None:
                    os.unlink(self._file, dir_fd=fd)
                else:
                    _write_private_record(fd, self._file, {"excluded": epoch})
        except FileNotFoundError:
            pass
        except ProjectError:
            if self.fleet_home.exists() or self.fleet_home.is_symlink():
                raise


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
