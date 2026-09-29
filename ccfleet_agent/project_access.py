"""Bounded project transfer and native slot-only Claude sessions over SSH.

No credentials, client environment, arbitrary destination or shell command are
accepted by this protocol. The authenticated Unix account owns every path. The
operator gate is separate from pairing, so installing this module enables no
customer automatically.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import pwd
import re
import signal
import stat
import struct
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable

try:
    from . import project_files
except ImportError:
    # Python -I intentionally excludes cwd and PYTHONPATH. Load only the sibling
    # installed in the same root-owned agent directory, never a project module.
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_project_files", Path(__file__).with_name("project_files.py"))
    assert _spec is not None and _spec.loader is not None
    project_files = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(project_files)

POLICY_DIR = Path("/etc/ccfleet/project-access")
MAX_FRAME = 32 * 1024 * 1024
MAX_RECORD = 2 * 1024 * 1024
PROJECT = re.compile(r"[0-9a-f]{32}\Z")
SESSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}\Z")
MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z")
MODES = frozenset({"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"})
EFFORTS = frozenset({"default", "low", "medium", "high", "xhigh", "max", "ultracode"})
TMUX = "/usr/bin/tmux"
PYTHON = "/usr/bin/python3"


class ProjectAccessError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require_enabled(policy_dir: Path = POLICY_DIR) -> None:
    name = pwd.getpwuid(os.getuid()).pw_name
    try:
        parent = policy_dir.lstat()
        marker = (policy_dir / name).lstat()
    except OSError as exc:
        raise ProjectAccessError("disabled", "project access is not enabled for this slot") from exc
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022
            or not stat.S_ISREG(marker.st_mode) or marker.st_uid != 0
            or marker.st_mode & 0o022 or marker.st_nlink != 1):
        raise ProjectAccessError("disabled", "invalid operator project-access policy")


def read_object(path: Path, limit: int = 64 * 1024) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("invalid configuration")
            raw = stream.read(limit + 1)
        if len(raw) <= limit:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except (ValueError, OSError):
        pass
    raise ProjectAccessError("account", "slot sign-in is unavailable; use your slot page")


def bound_account(home: Path) -> str:
    """Read only account binding metadata, never Claude's credential file."""
    profile = read_object(home / ".claude.json", 4 * 1024 * 1024).get("oauthAccount")
    state = read_object(home / ".config/ccfleet/slot-state.json")
    account = profile.get("accountUuid") if isinstance(profile, dict) else None
    if not isinstance(account, str) or not account.strip():
        raise ProjectAccessError("account", "the slot has no bound Claude account")
    fingerprint = hashlib.sha256(account.strip().encode()).hexdigest()[:16]
    if state.get("bound_fp") != fingerprint or state.get("account_restart"):
        raise ProjectAccessError("account", "slot account transition is pending; retry later")
    return fingerprint


def slot_environment(home: Path) -> dict[str, str]:
    name = pwd.getpwuid(os.getuid()).pw_name
    return {"HOME": str(home), "USER": name, "LOGNAME": name,
            "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8", "TZ": "UTC", "TERM": "xterm-256color"}


def safe_directory(path: Path, *, create: bool = False) -> Path:
    """Reject symlink components, including when the leaf does not exist yet."""
    if not path.is_absolute():
        raise ProjectAccessError("filesystem", "slot project path is unavailable")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if create:
                current.mkdir(mode=0o700, exist_ok=True)
            info = current.lstat()
        except OSError as exc:
            raise ProjectAccessError("filesystem", "slot project path is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ProjectAccessError("filesystem", "unsafe slot project path")
    return path


def project_root(home: Path, project: str, *, create: bool = False) -> Path:
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise ProjectAccessError("request", "invalid project identifier")
    return safe_directory(home / "workspace" / "projects" / project, create=create)


@contextmanager
def project_lock(home: Path, project: str) -> Iterator[None]:
    directory = safe_directory(home / ".config/ccfleet/project-locks", create=True)
    if directory.stat().st_mode & 0o022:
        raise ProjectAccessError("filesystem", "unsafe project lock directory")
    descriptor = os.open(directory / project,
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ProjectAccessError("filesystem", "unsafe project lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProjectAccessError("busy", "another project operation is in progress") from exc
        yield
    finally:
        os.close(descriptor)


def sessions(home: Path) -> list[str]:
    result = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, env=slot_environment(home), timeout=10)
    if result.returncode:
        # tmux exits 1 when this user has no server. Other failures are not
        # treated as an empty list: a write must fail closed.
        if result.returncode == 1 and (b"no server running" in result.stderr
                                      or ((b"error connecting to" in result.stderr
                                           or b"failed to connect to server" in result.stderr)
                                          and b"No such file or directory" in result.stderr)):
            return []
        raise ProjectAccessError("session", "could not check active slot sessions")
    if len(result.stdout) > 128 * 1024:
        raise ProjectAccessError("limit", "too many active slot sessions")
    return result.stdout.decode("utf-8", "strict").splitlines()


def project_sessions(names: list[str], project: str) -> list[str]:
    prefix = "p_" + project + "_"
    return sorted(name[len(prefix):] for name in names
                  if name.startswith(prefix) and SESSION.fullmatch(name[len(prefix):]))


def read_exact(stream: BinaryIO, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise ProjectAccessError("request", "incomplete project frame")
        result.extend(part)
    return bytes(result)


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def read_frame(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack("!I", read_exact(stream, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ProjectAccessError("request", "invalid project frame size")
    try:
        result = json.loads(read_exact(stream, size), object_pairs_hook=reject_duplicates)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProjectAccessError("request", "invalid project JSON") from exc
    if not isinstance(result, dict):
        raise ProjectAccessError("request", "project request must be an object")
    return result


def write_frame(stream: BinaryIO, result: dict[str, Any]) -> None:
    raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_FRAME:
        raise ProjectAccessError("limit", "project response exceeds the transfer limit")
    stream.write(struct.pack("!I", len(raw)) + raw)
    stream.flush()


def validate_request(request: dict[str, Any]) -> str:
    operation = request.get("operation")
    fields = {"status": {"version", "operation"}, "list": {"version", "operation"},
              "read": {"version", "operation", "project"},
              "write": {"version", "operation", "project", "files", "expected"}}
    if (type(request.get("version")) is not int or request["version"] != 1
            or not isinstance(operation, str) or operation not in fields
            or set(request) != fields[operation]):
        raise ProjectAccessError("request", "unsupported project request")
    if operation in {"read", "write"}:
        if not isinstance(request["project"], str) or not PROJECT.fullmatch(request["project"]):
            raise ProjectAccessError("request", "invalid project identifier")
    if operation == "write":
        project_files.validate_snapshot(request["files"])
        project_files.validate_manifest(request["expected"])
    return operation


@contextmanager
def manifest_directory(home: Path, *, create: bool = False) -> Iterator[int]:
    parent = safe_directory(home / ".config/ccfleet")
    path = parent / "project-manifests"
    try:
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield -1
        return
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProjectAccessError("filesystem", "unsafe project selection directory")
        yield descriptor
    finally:
        os.close(descriptor)


def read_manifest(home: Path, project: str) -> dict[str, dict[str, Any]]:
    """Recover explicit selections without putting Git or host metadata in a project."""
    with manifest_directory(home) as directory:
        if directory == -1:
            return {}
        try:
            descriptor = os.open(project + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return {}
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_RECORD):
                raise ProjectAccessError("filesystem", "unsafe project selection record")
            raw = stream.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise ProjectAccessError("filesystem", "oversized project selection record")
        try:
            result = json.loads(raw, object_pairs_hook=reject_duplicates)
            return project_files.validate_manifest(result)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ProjectAccessError("filesystem", "invalid project selection record") from exc


def save_manifest(home: Path, project: str, manifest: dict[str, dict[str, Any]]) -> None:
    project_files.validate_manifest(manifest)
    raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_RECORD:
        raise ProjectAccessError("limit", "project selection exceeds limit")
    with manifest_directory(home, create=True) as directory:
        if directory == -1:
            raise ProjectAccessError("filesystem", "project selection directory is unavailable")
        temporary = ".selection-" + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, project + ".json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def stable_snapshot(root: Path, tracked: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    first = project_files.snapshot(root, tracked=tracked)
    second = project_files.snapshot(root, tracked=tracked)
    if project_files.manifest(first) != project_files.manifest(second):
        raise ProjectAccessError("busy", "project changed during transfer; retry after it is idle")
    return second


def handle(request: dict[str, Any], home: Path, *,
           policy: Callable[[], None] = require_enabled) -> dict[str, Any]:
    policy()
    account = bound_account(home)
    operation = validate_request(request)
    if operation == "status":
        return {"ready": True, "protocol": 1}
    if operation == "list":
        parent = home / "workspace/projects"
        if not parent.exists():
            return {"projects": []}
        safe_directory(parent)
        names = sessions(home)
        projects = []
        with os.scandir(parent) as children:
            for count, child in enumerate(children):
                if count >= 1000:
                    raise ProjectAccessError("limit", "too many shared projects")
                if PROJECT.fullmatch(child.name) and child.is_dir(follow_symlinks=False):
                    projects.append({"project": child.name,
                                     "sessions": project_sessions(names, child.name)})
        policy()
        if bound_account(home) != account:
            raise ProjectAccessError("account", "slot account changed; retry later")
        return {"projects": sorted(projects, key=lambda item: item["project"])}
    project = request["project"]
    with project_lock(home, project):
        policy()
        if bound_account(home) != account:
            raise ProjectAccessError("account", "slot account changed; retry later")
        tracked = read_manifest(home, project)
        candidate = home / "workspace/projects" / project
        if operation == "read" and not candidate.exists() and not candidate.is_symlink():
            # Reading an as-yet-unshared project creates no remote workspace.
            return {"files": {}, "manifest": {}}
        root = project_root(home, project, create=operation == "write")
        if operation == "write":
            if any(name.startswith("p_" + project + "_") for name in sessions(home)):
                raise ProjectAccessError("busy", "close project Claude sessions before pushing")
            desired = project_files.manifest(request["files"])
            tracked = {**tracked, **request["expected"], **desired}
            project_files.validate_manifest(tracked)
            current = project_files.manifest(stable_snapshot(root, tracked))
            if current != desired:
                if current != request["expected"]:
                    raise project_files.ProjectError("remote project changed since last review")
                # Record the union before mutations. If a disk failure interrupts
                # apply, newly written ignored files remain visible for recovery.
                save_manifest(home, project, tracked)
                backups = safe_directory(home / ".config/ccfleet/project-backups" / project,
                                         create=True)
                project_files.apply_snapshot(root, request["files"], request["expected"],
                                             backups / uuid.uuid4().hex)
        files = stable_snapshot(root, tracked)
        policy()
        if bound_account(home) != account:
            raise ProjectAccessError("account", "slot account changed; retry later")
        if operation == "write":
            if project_files.manifest(files) != desired:
                raise ProjectAccessError(
                    "busy", "project changed during write; review before retry")
            save_manifest(home, project, desired)
        return {"files": files, "manifest": project_files.manifest(files)}


def serve_one(input_: BinaryIO, output: BinaryIO, home: Path, *,
              policy: Callable[[], None] = require_enabled) -> int:
    try:
        # Reject an unenabled user before reading any client-controlled payload.
        policy()
        result = handle(read_frame(input_), home, policy=policy)
        write_frame(output, {"version": 1, "ok": True, **result})
        return 0
    except ProjectAccessError as exc:
        codes = {"disabled": "disabled", "account": "account_unavailable", "busy": "busy",
                 "request": "invalid", "filesystem": "invalid", "limit": "invalid",
                 "session": "internal"}
        error = {"error": codes.get(exc.code, "internal"), "message": str(exc)}
    except project_files.ProjectError:
        error = {"error": "conflict",
                 "message": "unsafe files or project conflict; review and retry"}
    except (OSError, ValueError, subprocess.SubprocessError):
        error = {"error": "internal", "message": "slot project operation is unavailable"}
    write_frame(output, {"version": 1, "ok": False, **error})
    return 2


def validate_session(project: str, action: str, name: str,
                     mode: str, model: str, effort: str) -> None:
    if (not PROJECT.fullmatch(project) or action not in {"open", "new"}
            or not SESSION.fullmatch(name) or mode not in MODES
            or not MODEL.fullmatch(model) or effort not in EFFORTS):
        raise ProjectAccessError("request", "invalid project session request")


def claude_command(home: Path, mode: str, model: str, effort: str) -> list[str]:
    command = [str(home / ".local/bin/claude")]
    command += (["--dangerously-skip-permissions"] if mode == "bypassPermissions"
                else ["--permission-mode", mode])
    if model != "default":
        command += ["--model", model]
    if effort != "default":
        command += ["--effort", effort]
    return command


def run_claude(home: Path, project: str, mode: str, model: str, effort: str) -> None:
    validate_session(project, "open", "main", mode, model, effort)
    require_enabled()
    bound_account(home)
    root = project_root(home, project)
    os.chdir(root)
    os.umask(0o077)
    command = claude_command(home, mode, model, effort)
    os.execve(command[0], command, slot_environment(home))


def open_session(home: Path, project: str, action: str, name: str,
                 mode: str, model: str, effort: str) -> None:
    validate_session(project, action, name, mode, model, effort)
    require_enabled()
    account = bound_account(home)
    target = "p_" + project + "_" + name
    environment = slot_environment(home)
    with project_lock(home, project):
        root = project_root(home, project)
        if bound_account(home) != account:
            raise ProjectAccessError("account", "slot account changed; retry later")
        exists = target in sessions(home)
        if exists and action == "new":
            raise ProjectAccessError("session", "session already exists; choose another name")
        if not exists:
            # A new process repeats the clean environment and binding checks
            # even when tmux's existing server has stale global environment.
            command = [TMUX, "new-session", "-d", "-s", target, "-c", str(root),
                       PYTHON, "-I", str(Path(__file__).resolve()), "_run", project,
                       mode, model, effort]
            result = subprocess.run(command, env=environment, capture_output=True, timeout=15)
            if result.returncode:
                raise ProjectAccessError("session", "could not start the slot Claude session")
    os.execve(TMUX, [TMUX, "attach-session", "-t", "=" + target], environment)


def main(argv: list[str]) -> int:
    # Never trust an SSH-supplied HOME/USER to select the slot or its files.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        if not argv:
            signal.alarm(60)
            return serve_one(sys.stdin.buffer, sys.stdout.buffer, home)
        if argv[0] == "session" and len(argv) == 7:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ProjectAccessError("request", "project sessions need an interactive terminal")
            open_session(home, *argv[1:])
        elif argv[0] == "_run" and len(argv) == 5:
            run_claude(home, *argv[1:])
        else:
            raise ProjectAccessError("request", "unsupported project entry point")
    except (ProjectAccessError, OSError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ProjectAccessError) else "slot session is unavailable"
        print(message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
