"""Bounded, unprivileged supervision of explicit local Claude background jobs.

No account credentials are stored here. The calling CLI supplies authorization
and a foreground command; this module never retries or resurrects that command.
Control is authenticated with a private instance nonce, never a saved PID.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hmac
import json
import math
import os
import re
import secrets
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Optional

VERSION = 1
DEFAULT_TIMEOUT_S = 3600
MAX_TIMEOUT_S = 86400
MAX_CONCURRENT = 16
MAX_RECORDS = 256
MAX_SPEC_BYTES = 256 * 1024
MAX_STATE_BYTES = 16 * 1024
MAX_LOG_BYTES = 4 * 1024 * 1024
CONTROL_TIMEOUT_S = 2.0
AUTH_INTERVAL_S = 15.0
AUTH_TIMEOUT_S = 25.0
STOP_GRACE_S = 3.0
JOB_RE = re.compile(r"[0-9a-f]{32}")
INSTANCE_RE = re.compile(r"[0-9a-f]{64}")
TERMINAL = frozenset({"completed", "failed", "stopped", "timed_out"})
PUBLIC_FIELDS = frozenset({"job_id", "device_id", "slot_id", "state", "created_at", "started_at",
                           "finished_at", "exit_code", "reason", "timeout_s", "log_truncated",
                           "cleanup_confirmed"})
OPTION_FIELDS = frozenset({"mode", "model", "effort", "name", "resume", "continue_session",
                           "fork_session", "legacy_history", "extra_args"})
GUARD_FD_ENV = "CCFLEET_JOB_GUARD_FD"
LIVENESS_FD_ENV = "CCFLEET_JOB_LIVENESS_FD"
# This small same-group guardian stays alive after the foreground CLI exits.
# Besides crash cleanup, it ensures the final group SIGKILL has a known-owned
# live member: macOS can return EPERM for a group containing only zombies.
_GUARD_PROGRAM = """
import os, signal, sys
guard, ready = int(sys.argv[1]), int(sys.argv[2])
for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, signal.SIG_IGN)
os.write(ready, b'R')
os.close(ready)
try:
    while os.read(guard, 1):
        pass
finally:
    os.killpg(os.getpgrp(), signal.SIGKILL)
"""


class JobError(ValueError):
    pass


def _number(value: Any, low: float, high: float, label: str) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not low <= value <= high):
        raise JobError(f"invalid {label}")
    return float(value)


def _name(value: Any, label: str, maximum: int = 128) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise JobError(f"invalid {label}")
    return value


def _job_id(value: Any) -> str:
    if not isinstance(value, str) or not JOB_RE.fullmatch(value):
        raise JobError("invalid local job id")
    return value


def _private(info: os.stat_result, kind: str, *, allow_unlinked: bool = False) -> None:
    correct = stat.S_ISDIR(info.st_mode) if kind == "directory" else stat.S_ISREG(info.st_mode)
    if (not correct or info.st_uid != os.getuid() or info.st_mode & 0o077
            or (kind != "directory" and info.st_nlink not in
                ((0, 1) if allow_unlinked else (1,)))):
        raise JobError(f"unsafe local job {kind}")


def _directory(path, *, dir_fd=None) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


@contextlib.contextmanager
def _root(path: Path, *, create: bool = False):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise JobError("local job directory must be a plain absolute path")
    fd = _directory(path.anchor)
    try:
        for part in path.parts[1:]:
            try:
                child = _directory(part, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = _directory(part, dir_fd=fd)
            os.close(fd)
            fd = child
        _private(os.fstat(fd), "directory")
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _job(root: Path, job_id: str):
    with _root(root) as base:
        fd = _directory(_job_id(job_id), dir_fd=base)
        try:
            _private(os.fstat(fd), "directory")
            yield fd
        finally:
            os.close(fd)


def _open_file(directory: int, name: str, flags: int, *, create: bool = False) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0),
                 0o600, dir_fd=directory)
    try:
        # A readonly state snapshot can be replaced atomically after open but
        # before fstat. Its now-unlinked inode has no hardlink aliases and is
        # still a safe private snapshot. Never relax locks or writable files.
        _private(os.fstat(fd), "file",
                 allow_unlinked=name == "state.json" and flags == os.O_RDONLY and not create)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _decode(data: bytes) -> dict[str, Any]:
    def unique(pairs):
        answer = {}
        for key, value in pairs:
            if key in answer:
                raise JobError("duplicate local job field")
            answer[key] = value
        return answer
    try:
        value = json.loads(data, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise JobError("invalid local job data") from exc
    if not isinstance(value, dict):
        raise JobError("invalid local job data")
    return value


def _read(directory: int, name: str, limit: int) -> dict[str, Any]:
    fd = _open_file(directory, name, os.O_RDONLY)
    try:
        if os.fstat(fd).st_size > limit:
            raise JobError("local job data is too large")
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        if len(data) > limit:
            raise JobError("local job data is too large")
        return _decode(bytes(data))
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError("local job write made no progress")
        remaining = remaining[count:]


def _save(directory: int, name: str, value: dict[str, Any], limit: int) -> None:
    data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > limit:
        raise JobError("local job data is too large")
    temporary = ".job-" + secrets.token_hex(12)
    fd = _open_file(directory, temporary, os.O_WRONLY | os.O_EXCL, create=True)
    try:
        _write_all(fd, data)
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


@contextlib.contextmanager
def _lock(directory: int, name: str, *, nonblocking: bool = False):
    fd = _open_file(directory, name, os.O_RDWR, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield fd
    finally:
        os.close(fd)


def _running(directory: int) -> bool:
    try:
        with _lock(directory, "run.lock", nonblocking=True):
            return False
    except BlockingIOError:
        return True


def _spec(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"device_id", "slot_id", "project", "prompt", "options"}:
        raise JobError("invalid local job specification fields")
    _name(value["device_id"], "device id")
    _name(value["slot_id"], "slot id")
    project = _name(value["project"], "project", 4096)
    if not Path(project).is_absolute():
        raise JobError("local job project must be absolute")
    prompt = value["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt:
        raise JobError("a background job needs an explicit prompt")
    options = value["options"]
    if not isinstance(options, dict) or set(options) - OPTION_FIELDS:
        raise JobError("invalid background job options")
    for key, item in options.items():
        if key in {"continue_session", "fork_session", "legacy_history"}:
            if type(item) is not bool:
                raise JobError("invalid background job option")
        elif key == "extra_args":
            if (not isinstance(item, list) or len(item) > 128
                    or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 8192
                           for arg in item)):
                raise JobError("invalid native background arguments")
        elif item is not None and (not isinstance(item, str) or "\0" in item or len(item) > 256):
            raise JobError("invalid background job option")
    return value


def read_spec(root: Path, job_id: str) -> dict[str, Any]:
    with _job(root, job_id) as directory:
        return _spec(_read(directory, "spec.json", MAX_SPEC_BYTES))


def _state(directory: int, job_id: str, device_id: Optional[str] = None) -> dict[str, Any]:
    value = _read(directory, "state.json", MAX_STATE_BYTES)
    if (type(value.get("version")) is not int or value.get("version") != VERSION
            or value.get("job_id") != job_id
            or not isinstance(value.get("instance"), str)
            or not INSTANCE_RE.fullmatch(value["instance"])
            or not isinstance(value.get("state"), str)
            or value.get("state") not in TERMINAL | {"starting", "running", "stopping"}):
        raise JobError("invalid local job state")
    _name(value.get("device_id"), "device id")
    _name(value.get("slot_id"), "slot id")
    if device_id is not None and value["device_id"] != device_id:
        raise JobError("local job belongs to another paired device")
    return value


def _public(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in PUBLIC_FIELDS if key in value}


def _remaining(connection: socket.socket, deadline: Optional[float]) -> None:
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("local job control deadline expired")
        connection.settimeout(left)


def _exact(connection: socket.socket, size: int, deadline: Optional[float] = None) -> bytes:
    data = bytearray()
    while len(data) < size:
        _remaining(connection, deadline)
        part = connection.recv(size - len(data))
        if not part:
            raise EOFError("local job control connection ended")
        data.extend(part)
    return bytes(data)


def _receive(connection: socket.socket, deadline: Optional[float] = None) -> dict[str, Any]:
    size = struct.unpack("!I", _exact(connection, 4, deadline))[0]
    if not 0 < size <= MAX_STATE_BYTES:
        raise JobError("invalid local job control frame")
    return _decode(_exact(connection, size, deadline))


def _send(connection: socket.socket, value: dict[str, Any]) -> None:
    data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_STATE_BYTES:
        raise JobError("oversized local job control frame")
    connection.sendall(struct.pack("!I", len(data)) + data)


def _control(value: dict[str, Any], operation: str, *,
             deadline: Optional[float] = None) -> dict[str, Any]:
    deadline = min(deadline, time.monotonic() + CONTROL_TIMEOUT_S) if deadline is not None \
        else time.monotonic() + CONTROL_TIMEOUT_S
    address = value.get("socket")
    if not isinstance(address, str) or not Path(address).is_absolute():
        raise JobError("local job control is not ready")
    path = Path(address)
    with _root(path.parent) as parent:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (path.name != "control" or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise JobError("unsafe local job control socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        _remaining(connection, deadline)
        connection.connect(address)
        _remaining(connection, deadline)
        _send(connection, {"version": VERSION, "job_id": value["job_id"],
                           "instance": value["instance"], "operation": operation})
        result = _receive(connection, deadline)
    if (set(result) != {"version", "job_id", "instance", "status"}
            or type(result["version"]) is not int or result["version"] != VERSION
            or result["job_id"] != value["job_id"]
            or not isinstance(result["instance"], str)
            or not hmac.compare_digest(result["instance"], value["instance"])
            or not isinstance(result["status"], dict)
            or set(result["status"]) - PUBLIC_FIELDS):
        raise JobError("local job control identity did not match")
    return result["status"]


def status(root: Path, job_id: str, *, device_id: Optional[str] = None) -> dict[str, Any]:
    with _job(root, job_id) as directory:
        value = _state(directory, job_id, device_id)
        running = _running(directory)
        if not running:
            answer = _public(value)
            if value["state"] not in TERMINAL:
                answer.update(state="interrupted", reason="supervisor_lost",
                              cleanup_confirmed=False)
            return answer
        if value["state"] in TERMINAL:
            return {**_public(value), "state": "stopping", "cleanup_confirmed": False}
    try:
        return _control(value, "status")
    except (OSError, EOFError, JobError):
        return {**_public(value), "state": "unresponsive", "cleanup_confirmed": False}


def list_jobs(root: Path, *, device_id: Optional[str] = None) -> list[dict[str, Any]]:
    try:
        with _root(root) as directory:
            names = sorted(name for name in os.listdir(directory) if JOB_RE.fullmatch(name))
    except FileNotFoundError:
        return []
    if len(names) > MAX_RECORDS:
        raise JobError("too many local job records; archive completed jobs before continuing")
    result = []
    for name in names:
        with _job(root, name) as directory:
            value = _state(directory, name)
        if device_id is None or value["device_id"] == device_id:
            result.append(status(root, name, device_id=device_id))
    return sorted(result, key=lambda item: item.get("created_at", 0), reverse=True)


def stop(root: Path, job_id: str, *, device_id: Optional[str] = None,
         timeout_s: float = 15) -> dict[str, Any]:
    timeout_s = _number(timeout_s, 0.1, 60, "stop timeout")
    return _stop_until(root, job_id, device_id=device_id, deadline=time.monotonic() + timeout_s)


def _stop_until(root: Path, job_id: str, *, device_id: Optional[str],
                deadline: float) -> dict[str, Any]:
    with _job(root, job_id) as directory:
        value = _state(directory, job_id, device_id)
        if not _running(directory):
            if value["state"] in TERMINAL and value.get("cleanup_confirmed") is True:
                return _public(value)
            raise JobError("job supervisor is gone; shutdown cannot be confirmed from a saved PID")
    try:
        _control(value, "stop", deadline=deadline)
    except (OSError, EOFError, JobError):
        # EOF during shutdown is normal, but is not itself proof of cleanup.
        pass
    while time.monotonic() < deadline:
        with _job(root, job_id) as directory:
            latest = _state(directory, job_id, device_id)
            if latest["instance"] != value["instance"]:
                raise JobError("local job instance changed while stopping")
            if not _running(directory):
                if latest["state"] in TERMINAL and latest.get("cleanup_confirmed") is True:
                    return _public(latest)
                raise JobError("job supervisor ended without confirmed cleanup")
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise JobError("local job shutdown is unconfirmed; no saved PID was signalled")


def stop_device(root: Path, device_id: str, *, timeout_s: float = 15) -> list[dict[str, Any]]:
    timeout_s = _number(timeout_s, 0.1, 15, "device shutdown timeout")
    deadline = time.monotonic() + timeout_s
    results = []
    uncertain = []
    # Listing live status would spend one socket timeout per record before
    # shutdown even began. Read the bounded private records directly instead.
    try:
        with _root(root) as base, _lock(base, "jobs.lock", nonblocking=True):
            names = sorted(name for name in os.listdir(base) if JOB_RE.fullmatch(name))
            if len(names) > MAX_RECORDS:
                raise JobError("too many local job records to stop safely")
            for job_id in names:
                try:
                    with _job(root, job_id) as directory:
                        item = _state(directory, job_id)
                        if item["device_id"] != device_id:
                            continue
                        if (item["state"] in TERMINAL and item.get("cleanup_confirmed") is True
                                and not _running(directory)):
                            continue
                except (JobError, OSError):
                    # A damaged record is not permission to signal anything,
                    # but must not prevent stopping other verifiable jobs.
                    uncertain.append(job_id)
                    continue
                if time.monotonic() >= deadline:
                    uncertain.append(job_id)
                    continue
                try:
                    results.append(_stop_until(root, job_id, device_id=device_id,
                                               deadline=deadline))
                except (JobError, OSError):
                    uncertain.append(job_id)
    except FileNotFoundError:
        return results
    if uncertain:
        raise JobError("some local job shutdowns remain unconfirmed; inspect jobs "
                       + ", ".join(uncertain))
    return results


def _rename_exclusive(source_dir: int, target_dir: int, name: str) -> None:
    """Atomic no-replace directory move on the supported Linux/macOS clients."""
    library = ctypes.CDLL(None, use_errno=True)
    operation = getattr(library, "renameatx_np" if sys.platform == "darwin" else "renameat2",
                        None)
    if operation is None:
        raise JobError("this system cannot archive a job without risking an overwrite")
    operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_uint)
    operation.restype = ctypes.c_int
    # Darwin RENAME_EXCL (sys/stdio.h); Linux RENAME_NOREPLACE.
    flag = 4 if sys.platform == "darwin" else 1
    if operation(source_dir, name.encode("ascii"), target_dir, name.encode("ascii"), flag) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise JobError("this job already has an archive; neither copy was changed")
        raise OSError(error, "could not atomically archive the local job")


def archive(root: Path, job_id: str, *, device_id: Optional[str] = None,
            acknowledge_unconfirmed: bool = False) -> dict[str, Any]:
    """Recoverably move one inactive record, never stop a process or delete data.

    Explicit acknowledgement archives uncertain *records*, not a claim that any
    old work was stopped. A held lock or responding/ambiguous control socket
    always refuses archiving, even when acknowledgement was supplied.
    """
    _job_id(job_id)
    if type(acknowledge_unconfirmed) is not bool:
        raise JobError("invalid unconfirmed-job acknowledgement")
    try:
        with _root(root) as base, _lock(base, "jobs.lock", nonblocking=True), \
                _job(root, job_id) as directory, \
                _lock(directory, "run.lock", nonblocking=True) as resident_lock:
            identity = os.fstat(directory)
            lock_identity = os.fstat(resident_lock)
            value = _state(directory, job_id, device_id)
            confirmed = value["state"] in TERMINAL and value.get("cleanup_confirmed") is True
            if not confirmed and not acknowledge_unconfirmed:
                raise JobError("job cleanup is unconfirmed; explicit acknowledgement is required "
                               "to archive its record without claiming the old work stopped")
            if value.get("socket") is not None:
                try:
                    _control(value, "status")
                except (FileNotFoundError, ConnectionRefusedError):
                    pass
                except (OSError, EOFError, JobError) as exc:
                    raise JobError("job control is ambiguous; stop it before archiving") from exc
                else:
                    raise JobError("job control is still active; stop it before archiving")
            with contextlib.suppress(FileExistsError):
                os.mkdir("archive", 0o700, dir_fd=base)
            archived = _directory("archive", dir_fd=base)
            try:
                _private(os.fstat(archived), "directory")
                current = os.stat(job_id, dir_fd=base, follow_symlinks=False)
                lock_current = os.stat("run.lock", dir_fd=directory, follow_symlinks=False)
                if ((current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
                        or (lock_current.st_dev, lock_current.st_ino) !=
                        (lock_identity.st_dev, lock_identity.st_ino)
                        or _state(directory, job_id, device_id) != value):
                    raise JobError("job identity changed while archiving; nothing was moved")
                _rename_exclusive(base, archived, job_id)
                try:
                    os.fsync(archived)
                    os.fsync(base)
                except OSError as exc:
                    raise JobError("job files were moved to the private archive, but durability "
                                   "could not be confirmed") from exc
            finally:
                os.close(archived)
    except BlockingIOError as exc:
        raise JobError("job or job management is still active; stop it before archiving") from exc
    return {**_public(value), "archived": True,
            "acknowledged_unconfirmed": not confirmed,
            "cleanup_confirmed": value.get("cleanup_confirmed") is True and confirmed}


def logs(root: Path, job_id: str, *, stream: str = "stdout", max_bytes: int = 65536,
         device_id: Optional[str] = None) -> bytes:
    if (stream not in {"stdout", "stderr"} or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_LOG_BYTES):
        raise JobError("invalid local job log request")
    with _job(root, job_id) as directory:
        _state(directory, job_id, device_id)
        try:
            fd = _open_file(directory, stream + ".log", os.O_RDONLY)
        except FileNotFoundError:
            return b""
        try:
            size = os.fstat(fd).st_size
            if size > MAX_LOG_BYTES:
                raise JobError("unsafe oversized local job log")
            os.lseek(fd, max(0, size - max_bytes), os.SEEK_SET)
            return os.read(fd, max_bytes)
        finally:
            os.close(fd)


def start(root: Path, spec: dict[str, Any], runner_command: Sequence[str], *,
          timeout_s: float = DEFAULT_TIMEOUT_S, max_jobs: int = 4,
          startup_timeout: float = 35) -> dict[str, Any]:
    timeout_s = _number(timeout_s, 1, MAX_TIMEOUT_S, "job timeout")
    startup_timeout = _number(startup_timeout, 0.1, 60, "startup timeout")
    if type(max_jobs) is not int or not 1 <= max_jobs <= MAX_CONCURRENT:
        raise JobError("invalid maximum concurrent job count")
    spec = _spec(dict(spec))
    if len(json.dumps(spec, allow_nan=False).encode()) > MAX_SPEC_BYTES:
        raise JobError("local job specification is too large")
    if not Path(spec["project"]).is_dir():
        raise JobError("local job project does not exist")
    command = list(runner_command)
    if not command or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command):
        raise JobError("invalid local job runner")
    with _root(root, create=True) as base, _lock(base, "jobs.lock", nonblocking=True):
        previous = list_jobs(root)
        if len(previous) >= MAX_RECORDS:
            raise JobError("local job history is full; archive completed jobs before starting more")
        if sum(not item.get("cleanup_confirmed", False) for item in previous) >= max_jobs:
            raise JobError("maximum concurrent local jobs reached")
        job_id = secrets.token_hex(16)
        os.mkdir(job_id, 0o700, dir_fd=base)
        directory = _directory(job_id, dir_fd=base)
        try:
            _save(directory, "spec.json", spec, MAX_SPEC_BYTES)
            state = {"version": VERSION, "job_id": job_id, "instance": secrets.token_hex(32),
                     "device_id": spec["device_id"], "slot_id": spec["slot_id"],
                     "state": "starting", "created_at": time.time(), "timeout_s": timeout_s,
                     "cleanup_confirmed": False, "log_truncated": False}
            _save(directory, "state.json", state, MAX_STATE_BYTES)
            try:
                process = subprocess.Popen([*command, job_id], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True, close_fds=True)
            except OSError as exc:
                _save(directory, "state.json", {
                    **state, "state": "failed", "reason": "runner_failed",
                    "finished_at": time.time(), "cleanup_confirmed": True}, MAX_STATE_BYTES)
                raise JobError("could not start the local job supervisor") from exc
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                current = _state(directory, job_id)
                if current["state"] != "starting":
                    return status(root, job_id)
                if process.poll() is not None:
                    raise JobError(f"local job supervisor ended during startup; inspect {job_id}")
                time.sleep(0.05)
            # This Popen is our child, not a PID read from storage. Until wait
            # reaps it its PID cannot be reused. Its guard closes any job child.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise JobError(f"local job startup timed out; inspect job {job_id}")
        finally:
            os.close(directory)


@contextlib.contextmanager
def parent_guard():
    """Used by the hidden foreground job command, around the ordinary CLI.

    The liveness writer intentionally stays open until process exit, not context
    exit. The supervisor detects EOF without reaping the process, so it can kill
    remaining members of this process group before PID reuse becomes possible.
    """
    try:
        guard = int(os.environ.pop(GUARD_FD_ENV))
        alive = int(os.environ.pop(LIVENESS_FD_ENV))
    except (KeyError, ValueError) as exc:
        raise JobError("foreground job execution needs its live supervisor") from exc
    if guard == alive or min(guard, alive) < 3 or os.getpgrp() != os.getpid():
        raise JobError("unsafe local job process group")
    for fd in (guard, alive):
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise JobError("invalid local job supervisor pipe")
        os.set_inheritable(fd, False)
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    ready_read, ready_write = os.pipe()
    try:
        # No new session: the guardian must live in this exact child group.
        # It inherits only its two protocol fds, never the liveness writer.
        subprocess.Popen([sys.executable, "-I", "-c", _GUARD_PROGRAM,
                          str(guard), str(ready_write)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, pass_fds=(guard, ready_write))
        os.close(ready_write)
        ready_write = None
        with selectors.DefaultSelector() as ready:
            ready.register(ready_read, selectors.EVENT_READ)
            if not ready.select(3) or os.read(ready_read, 1) != b"R":
                raise JobError("local job lifetime guardian did not become ready")
        _write_all(alive, b"R")
        yield
    finally:
        os.close(ready_read)
        if ready_write is not None:
            os.close(ready_write)
        # Do not disarm the guardian at context exit. The process may still be
        # closing native history or waiting for a non-daemon thread to finish.
        signal.signal(signal.SIGTERM, previous)


def run(root: Path, job_id: str, *, command: Callable[[dict[str, Any]], Sequence[str]],
        check_authorization: Callable[[dict[str, Any]], bool]) -> int:
    """Run once as a detached supervisor. No restart or inference retry exists."""
    with _job(root, job_id) as directory, _lock(directory, "run.lock", nonblocking=True):
        state = _state(directory, job_id)
        if state["state"] != "starting":
            raise JobError("a local job can only be started once")
        spec = _spec(_read(directory, "spec.json", MAX_SPEC_BYTES))
        if (spec["device_id"] != state["device_id"] or spec["slot_id"] != state["slot_id"]):
            raise JobError("local job assignment changed")
        timeout = _number(state.get("timeout_s"), 1, MAX_TIMEOUT_S, "job timeout")
        stop_requested = threading.Event()
        state_guard = threading.Lock()
        finished = threading.Event()
        process = None
        guard_read = guard_write = alive_read = alive_write = None
        log_fds = {}
        selector = selectors.DefaultSelector()
        temporary = Path(tempfile.mkdtemp(prefix="ccfleet-job-", dir="/tmp")).resolve()
        address = temporary / "control"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reason = "runner_failed"
        exit_code = 1
        cleanup = True

        def save(**fields):
            with state_guard:
                state.update(fields)
                _save(directory, "state.json", state, MAX_STATE_BYTES)

        def controls():
            while not finished.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with connection:
                    connection.settimeout(CONTROL_TIMEOUT_S)
                    try:
                        request = _receive(connection, time.monotonic() + CONTROL_TIMEOUT_S)
                        if (set(request) != {"version", "job_id", "instance", "operation"}
                                or type(request["version"]) is not int
                                or request["version"] != VERSION or request["job_id"] != job_id
                                or not isinstance(request["instance"], str)
                                or not hmac.compare_digest(request["instance"], state["instance"])
                                or not isinstance(request["operation"], str)
                                or request["operation"] not in {"status", "stop"}):
                            continue
                        if request["operation"] == "stop":
                            stop_requested.set()
                        with state_guard:
                            answer = _public(state)
                        _send(connection, {"version": VERSION, "job_id": job_id,
                                           "instance": state["instance"], "status": answer})
                    except (OSError, EOFError, JobError):
                        continue

        auth_guard = threading.Lock()
        auth = {"started": time.monotonic(), "completed": 0.0,
                "allowed": None, "failed": False}

        def authorize():
            with auth_guard:
                auth.update(started=time.monotonic(), allowed=None, failed=False)
            try:
                allowed = check_authorization(spec) is True
                failed = False
            except Exception:
                allowed, failed = False, True
            with auth_guard:
                auth.update(completed=time.monotonic(), allowed=allowed, failed=failed)

        prior_signals = {}

        def interrupted(_signal, _frame):
            stop_requested.set()

        try:
            for number in (signal.SIGTERM, signal.SIGINT):
                prior_signals[number] = signal.signal(number, interrupted)
            prior_signals[signal.SIGHUP] = signal.signal(signal.SIGHUP, signal.SIG_IGN)
            listener.bind(str(address))
            os.chmod(address, 0o600)
            listener.listen(4)
            listener.settimeout(0.2)
            save(socket=str(address))
            controller = threading.Thread(target=controls, name="ccfleet-job-control", daemon=True)
            controller.start()
            authorization = threading.Thread(target=authorize, name="ccfleet-job-auth", daemon=True)
            authorization.start()
            deadline = time.monotonic() + timeout
            alive_ended = False
            guard_ready = False
            child_started = None
            sizes = {"stdout": 0, "stderr": 0}
            while True:
                now = time.monotonic()
                if stop_requested.is_set():
                    reason = "stopped"
                    break
                if now >= deadline:
                    reason = "timed_out"
                    break
                with auth_guard:
                    decision = dict(auth)
                if decision["allowed"] is False:
                    reason = ("authorization_unavailable" if decision["failed"]
                              else "authorization_denied")
                    break
                if decision["allowed"] is None and now - decision["started"] > AUTH_TIMEOUT_S:
                    reason = "authorization_unavailable"
                    break
                if process is None and decision["allowed"] is True:
                    arguments = list(command(spec))
                    if not arguments or any(not isinstance(arg, str) or not arg or "\0" in arg
                                            for arg in arguments):
                        raise JobError("invalid foreground job command")
                    guard_read, guard_write = os.pipe()
                    alive_read, alive_write = os.pipe()
                    environment = {**os.environ, GUARD_FD_ENV: str(guard_read),
                                   LIVENESS_FD_ENV: str(alive_write)}
                    for stream in ("stdout", "stderr"):
                        log_fds[stream] = _open_file(directory, stream + ".log",
                                                     os.O_WRONLY | os.O_EXCL, create=True)
                    process = subprocess.Popen(arguments, cwd=spec["project"], env=environment,
                                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, start_new_session=True,
                                               close_fds=True, pass_fds=(guard_read, alive_write))
                    os.close(guard_read)
                    os.close(alive_write)
                    guard_read = alive_write = None
                    for stream in ("stdout", "stderr"):
                        source = getattr(process, stream)
                        os.set_blocking(source.fileno(), False)
                        selector.register(source, selectors.EVENT_READ, stream)
                    selector.register(alive_read, selectors.EVENT_READ, "alive")
                    child_started = time.monotonic()
                if (decision["allowed"] is not None
                        and now - decision["completed"] >= AUTH_INTERVAL_S
                        and not authorization.is_alive()):
                    authorization = threading.Thread(target=authorize, name="ccfleet-job-auth",
                                                     daemon=True)
                    authorization.start()
                for key, _ in selector.select(0.05):
                    fd = key.fd
                    data = os.read(fd, 65536)
                    if key.data == "alive":
                        if not data:
                            alive_ended = True
                            selector.unregister(fd)
                        elif data == b"R" and not guard_ready:
                            guard_ready = True
                            save(state="running", started_at=time.time())
                        else:
                            raise JobError("invalid foreground job guard handshake")
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = MAX_LOG_BYTES - sizes[key.data]
                    if remaining:
                        _write_all(log_fds[key.data], data[:remaining])
                        sizes[key.data] += min(remaining, len(data))
                    if len(data) > remaining and not state["log_truncated"]:
                        save(log_truncated=True)
                if alive_ended:
                    reason = "completed" if guard_ready else "guard_missing"
                    break
                if child_started is not None and not guard_ready and now - child_started > 5:
                    reason = "guard_missing"
                    break
                if process is None:
                    time.sleep(0.05)
        except (OSError, JobError, ValueError):
            reason = "runner_failed"
        finally:
            if process is not None:
                # Do not poll()/wait() before signalling this process group.
                # Its unreaped leader pins the PID, including after child exit.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    cleanup = False
                limit = time.monotonic() + STOP_GRACE_S
                try:
                    while not alive_ended and time.monotonic() < limit:
                        for key, _ in selector.select(0.05):
                            data = os.read(key.fd, 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                if key.data == "alive":
                                    alive_ended = True
                            elif key.data != "alive":
                                remaining = MAX_LOG_BYTES - sizes[key.data]
                                if remaining:
                                    try:
                                        _write_all(log_fds[key.data], data[:remaining])
                                        sizes[key.data] += min(remaining, len(data))
                                    except OSError:
                                        state["log_truncated"] = True
                                if len(data) > remaining:
                                    state["log_truncated"] = True
                except (OSError, ValueError):
                    # Broken output/control pipes must not skip the final group
                    # kill. Cleanup is proved by termination, not readable logs.
                    state["log_truncated"] = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    cleanup = False
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cleanup = False
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        with contextlib.suppress(OSError):
                            stream.close()
            terminal = reason if reason in {"stopped", "timed_out"} else (
                "completed" if reason == "completed" and exit_code == 0 else "failed")
            if reason == "completed" and exit_code != 0:
                reason = "command_failed"
            try:
                save(state=terminal, reason=reason, exit_code=exit_code,
                     finished_at=time.time(), cleanup_confirmed=cleanup, socket=None)
            finally:
                finished.set()
                listener.close()
                selector.close()
                for fd in (guard_read, guard_write, alive_read, alive_write, *log_fds.values()):
                    if fd is not None:
                        with contextlib.suppress(OSError):
                            os.close(fd)
                with contextlib.suppress(FileNotFoundError):
                    address.unlink()
                temporary.rmdir()
                for number, handler in prior_signals.items():
                    signal.signal(number, handler)
        return 0 if terminal == "completed" else 1
