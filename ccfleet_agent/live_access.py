"""Resident, single-slot live filesystem transport over forced SSH commands.

The SSH link carries bounded filesystem packets, not model-authentication requests
or client-selected shell commands. User-selected file contents can contain secrets.
A resident sshfs process keeps its pipes and mount across network interruptions.
Exactly one request is outstanding; a replacement link for the same local
connector instance receives that request again. The local connector must retain
its SFTP handles and last response so a mutation is never replayed there.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import importlib.util
import json
import os
import pwd
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Optional

try:
    from . import project_access as project
except ImportError:
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_project_access", Path(__file__).with_name("project_access.py"))
    assert _spec is not None and _spec.loader is not None
    project = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = project
    _spec.loader.exec_module(project)

MAX_FRAME = 4 * 1024 * 1024
MAX_PACKET = 2 * 1024 * 1024
SSHFS = "/usr/bin/sshfs"
FUSERMOUNT = "/usr/bin/fusermount3"
ID = re.compile(r"[0-9a-f]{32}\Z")


class LiveError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def error_code(error: Exception) -> str:
    if isinstance(error, project.ProjectAccessError):
        return {"account": "account_unavailable", "disabled": "disabled", "busy": "busy",
                "request": "invalid", "filesystem": "invalid", "limit": "invalid",
                "session": "session"}.get(error.code, "unavailable")
    return error.code if isinstance(error, LiveError) else "unavailable"


def identifier(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise LiveError("invalid", "invalid live workspace identifier")
    return value


def exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


def read_frame(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack("!I", exact(stream, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise LiveError("invalid", "invalid live frame size")
    try:
        value = json.loads(exact(stream, size), object_pairs_hook=project.reject_duplicates)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise LiveError("invalid", "invalid live frame") from exc
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1):
        raise LiveError("invalid", "unsupported live protocol")
    return value


def write_frame(stream: BinaryIO, value: dict[str, Any]) -> None:
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_FRAME:
        raise LiveError("invalid", "live response exceeds protocol limit")
    data = memoryview(struct.pack("!I", len(raw)) + raw)
    while data:
        sent = stream.write(data)
        if not sent:
            raise OSError("live frame write failed")
        data = data[sent:]
    stream.flush()


def read_packet(stream: BinaryIO) -> bytes:
    header = exact(stream, 4)
    size = struct.unpack("!I", header)[0]
    if not 1 <= size <= MAX_PACKET:
        raise LiveError("invalid", "invalid filesystem packet size")
    return header + exact(stream, size)


def response_packet(value: dict[str, Any], sequence: int) -> bytes:
    if (set(value) != {"version", "sequence", "data"}
            or type(value.get("sequence")) is not int or value["sequence"] != sequence
            or not isinstance(value.get("data"), str)):
        raise LiveError("invalid", "unexpected filesystem response")
    try:
        packet = base64.b64decode(value["data"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LiveError("invalid", "invalid filesystem response encoding") from exc
    if (not 5 <= len(packet) <= MAX_PACKET + 4
            or struct.unpack("!I", packet[:4])[0] != len(packet) - 4):
        raise LiveError("invalid", "invalid filesystem response packet")
    return packet


def private_directory(path: Path, *, create: bool = True) -> Path:
    if not create and not os.path.lexists(path):
        raise FileNotFoundError
    project.safe_directory(path, create=create)
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LiveError("invalid", "unsafe live workspace state directory")
    return path


def state_directory(home: Path, workspace: str, *, create: bool = True) -> Path:
    return private_directory(home / ".config/ccfleet/live" / identifier(workspace), create=create)


def socket_path(home: Path, workspace: str, *, create: bool = True) -> Path:
    directory = private_directory(home / ".config/ccfleet/live-sockets", create=create)
    path = directory / (identifier(workspace) + ".sock")
    if len(os.fsencode(path)) >= 104:
        raise LiveError("unavailable", "slot path is too long for its private live socket")
    return path


def mount_path(home: Path, workspace: str) -> Path:
    return home / "workspace/live" / identifier(workspace)


def mounted(path: Path) -> bool:
    """Read kernel metadata, not stat() on an offline FUSE filesystem."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 7:
                    continue
                decoded = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
                if decoded == str(path) and "fuse.sshfs" in fields:
                    return True
    except OSError:
        return False
    return False


def check_access(home: Path, account: Optional[str] = None) -> str:
    project.require_enabled()
    current = project.bound_account(home)
    if account is not None and current != account:
        raise LiveError("account_unavailable", "slot account changed")
    return current


def dependencies() -> bool:
    return (os.access(SSHFS, os.X_OK) and os.access(FUSERMOUNT, os.X_OK)
            and os.access("/dev/fuse", os.R_OK | os.W_OK))


def live_sessions(home: Path, workspace: str) -> list[str]:
    prefix = "l_" + identifier(workspace) + "_"
    return sorted(name[len(prefix):] for name in project.sessions(home)
                  if name.startswith(prefix) and project.SESSION.fullmatch(name[len(prefix):]))


def marker(home: Path, workspace: str) -> Optional[str]:
    try:
        path = state_directory(home, workspace, create=False) / "instance"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise LiveError("invalid", "unsafe live connector state")
        raw = stream.read(34)
    try:
        return identifier(raw.decode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise LiveError("invalid", "invalid live connector state") from exc


def connect(home: Path, workspace: str) -> socket.socket:
    path = socket_path(home, workspace, create=False)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LiveError("invalid", "unsafe live connector socket")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer.settimeout(10)
        peer.connect(str(path))
        return peer
    except BaseException:
        peer.close()
        raise


def ensure_daemon(home: Path, workspace: str, instance: str) -> socket.socket:
    check_access(home)
    identifier(instance)
    try:
        return connect(home, workspace)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    if not dependencies():
        raise LiveError("unavailable", "the slot needs its live filesystem dependencies installed")
    with project.project_lock(home, "live-start-" + workspace):
        try:
            return connect(home, workspace)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        if marker(home, workspace) is not None or mounted(mount_path(home, workspace)):
            raise LiveError("stale", "stop the previous live workspace before starting another")
        subprocess.Popen([project.PYTHON, "-I", str(Path(__file__).resolve()), "_daemon",
                          workspace, instance], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env=project.slot_environment(home))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                return connect(home, workspace)
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.05)
    raise LiveError("unavailable", "could not start the slot live connector")


def cleanup(home: Path, workspace: str, process: Optional[subprocess.Popen] = None, *,
            clear_state: bool = True) -> bool:
    """End only this live mount and its sessions, even if filesystem I/O is blocked."""
    root = mount_path(home, workspace)
    good = True
    if mounted(root):
        try:
            result = subprocess.run([FUSERMOUNT, "-u", "-z", str(root)],
                                    capture_output=True, timeout=10,
                                    env=project.slot_environment(home))
            good = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            good = False
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    try:
        for name in live_sessions(home, workspace):
            result = subprocess.run([project.TMUX, "kill-session", "-t",
                                     "=l_" + workspace + "_" + name],
                                    capture_output=True, timeout=10,
                                    env=project.slot_environment(home))
            if result.returncode:
                good = False
    except (OSError, project.ProjectAccessError, subprocess.SubprocessError):
        good = False
    if mounted(root):
        good = False
    if good and clear_state:
        try:
            (state_directory(home, workspace, create=False) / "instance").unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError:
            good = False
    return good


class Supervisor:
    def __init__(self, home: Path, workspace: str, instance: str, process: subprocess.Popen):
        self.home, self.workspace, self.instance, self.process = home, workspace, instance, process
        self.account = check_access(home)
        self.condition = threading.Condition()
        self.peer: Optional[socket.socket] = None
        self.stream: Optional[BinaryIO] = None
        self.stopped = threading.Event()
        self.sequence = 0
        self.pending: Optional[dict[str, Any]] = None
        self.underlay_protected = False
        self.cleanup_lock = threading.Lock()
        self.cleaned: Optional[bool] = None

    def attach(self, peer: socket.socket, stream: BinaryIO, instance: str) -> None:
        if instance != self.instance:
            raise LiveError("stale", "another local connector owns this workspace; stop it first")
        check_access(self.home, self.account)
        with self.condition:
            if self.stopped.is_set():
                raise LiveError("stale", "the live workspace is stopping")
            write_frame(stream, {"version": 1, "ok": True, "ready": True})
            old, old_stream = self.peer, self.stream
            self.peer, self.stream = peer, stream
            if old is not None:
                try:
                    old.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                old.close()
                if old_stream is not None:
                    old_stream.close()
            self.condition.notify_all()

    def drop(self, peer: socket.socket, stream: Optional[BinaryIO] = None) -> None:
        with self.condition:
            if self.peer is peer:
                stream = self.stream
                self.peer, self.stream = None, None
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.close()
            if stream is not None:
                stream.close()

    def relay(self) -> None:
        try:
            while not self.stopped.is_set():
                packet = read_packet(self.process.stdout)
                self.sequence += 1
                self.pending = {"version": 1, "sequence": self.sequence,
                                "data": base64.b64encode(packet).decode("ascii")}
                while not self.stopped.is_set():
                    with self.condition:
                        self.condition.wait_for(
                            lambda: self.peer is not None or self.stopped.is_set())
                        if self.stopped.is_set():
                            return
                        peer, stream = self.peer, self.stream
                    try:
                        write_frame(stream, self.pending)
                        response = response_packet(read_frame(stream), self.sequence)
                    except (OSError, ValueError, EOFError):
                        self.drop(peer, stream)
                        continue
                    self.process.stdin.write(response)
                    self.process.stdin.flush()
                    self.pending = None
                    break
        except (OSError, ValueError, EOFError):
            self.stopped.set()

    def status(self) -> dict[str, Any]:
        return {"version": 1, "ok": True, "ready": dependencies(),
                "mounted": self.underlay_protected and mounted(
                    mount_path(self.home, self.workspace)),
                "connected": self.peer is not None, "instance": self.instance,
                "sessions": live_sessions(self.home, self.workspace)}

    def shutdown(self, *, explicit: bool = False) -> bool:
        with self.cleanup_lock:
            if self.cleaned is not None:
                if self.cleaned and explicit:
                    self.cleaned = cleanup(self.home, self.workspace, clear_state=True)
                return self.cleaned
            self.stopped.set()
            with self.condition:
                if self.peer is not None:
                    self.drop(self.peer)
                self.condition.notify_all()
            # An unexpected supervisor/FUSE failure loses sequence state. Keep
            # the binding as a stale marker until an explicit stop acknowledges
            # the reset; never replay from sequence one on a resident client.
            self.cleaned = cleanup(self.home, self.workspace, self.process, clear_state=explicit)
            return self.cleaned


def handle_peer(supervisor: Supervisor, peer: socket.socket) -> None:
    stream = peer.makefile("rwb", buffering=0)
    handed_off = False
    try:
        peer.settimeout(10)
        value = read_frame(stream)
        operation = value.get("operation")
        if operation == "link" and set(value) == {"version", "operation", "instance"}:
            peer.settimeout(None)
            supervisor.attach(peer, stream, identifier(value["instance"]))
            handed_off = True
        elif operation == "status" and set(value) == {"version", "operation"}:
            check_access(supervisor.home, supervisor.account)
            write_frame(stream, supervisor.status())
        elif operation == "stop" and set(value) == {"version", "operation"}:
            good = supervisor.shutdown(explicit=True)
            write_frame(stream, {"version": 1, "ok": good, "stopped": good})
        else:
            raise LiveError("invalid", "unsupported live control operation")
    except (LiveError, project.ProjectAccessError) as exc:
        try:
            write_frame(stream, {"version": 1, "ok": False, "error": error_code(exc),
                                 "message": "live connector request was refused"})
        except OSError:
            pass
    except (OSError, ValueError, EOFError):
        pass
    finally:
        if not handed_off:
            stream.close()
            peer.close()


def run_daemon(home: Path, workspace: str, instance: str) -> int:
    account = check_access(home)
    if not dependencies():
        raise LiveError("unavailable", "live filesystem dependencies are missing")
    path = socket_path(home, workspace)
    state = state_directory(home, workspace)
    descriptor = os.open(state / "daemon.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    root = mount_path(home, workspace)
    if marker(home, workspace) is not None or mounted(root):
        raise LiveError("stale", "previous live connector needs explicit cleanup")
    project.safe_directory(root, create=True)
    os.chmod(root, 0o700)
    if any(root.iterdir()):
        raise LiveError("invalid", "live mount directory must be empty")
    underlay = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    instance_fd = os.open(state / "instance", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600)
    with os.fdopen(instance_fd, "w") as output:
        output.write(instance)
        output.flush()
        os.fsync(output.fileno())
    if os.path.lexists(path):
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise LiveError("invalid", "unsafe old live socket")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(8)
    listener.settimeout(0.5)
    process = subprocess.Popen([SSHFS, "ccfleet:/", str(root), "-f", "-o",
                                "slave,sshfs_sync,no_readahead,dir_cache=yes,dcache_timeout=1,"
                                "direct_io,attr_timeout=1,entry_timeout=1,negative_timeout=0,"
                                "idmap=user,default_permissions,nosuid,nodev"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=project.slot_environment(home))
    supervisor = Supervisor(home, workspace, instance, process)
    worker = threading.Thread(target=supervisor.relay, daemon=True)
    worker.start()
    handlers: set[threading.Thread] = set()
    handlers_lock = threading.Lock()

    def serve_control(incoming: socket.socket) -> None:
        try:
            handle_peer(supervisor, incoming)
        finally:
            with handlers_lock:
                handlers.discard(threading.current_thread())

    signal.signal(signal.SIGTERM, lambda *_: supervisor.stopped.set())
    signal.signal(signal.SIGINT, lambda *_: supervisor.stopped.set())
    try:
        while not supervisor.stopped.is_set():
            check_access(home, account)
            if process.poll() is not None:
                break
            if not supervisor.underlay_protected and mounted(root):
                os.fchmod(underlay, 0o000)
                supervisor.underlay_protected = True
            peer = supervisor.peer
            if peer is not None:
                try:
                    if (select.select([peer], [], [], 0)[0]
                            and not peer.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)):
                        supervisor.drop(peer)
                except BlockingIOError:
                    pass
                except (OSError, ValueError):
                    supervisor.drop(peer)
            try:
                incoming, _ = listener.accept()
            except socket.timeout:
                continue
            handler = threading.Thread(target=serve_control, args=(incoming,), daemon=True)
            with handlers_lock:
                handlers.add(handler)
            handler.start()
    finally:
        supervisor.shutdown()
        listener.close()
        # Explicit stop runs in a control handler. Its shutdown flag wakes this
        # main loop before it has written the acknowledgment. Keep the process
        # alive until accepted handlers finish their bounded writes/handshakes.
        deadline = time.monotonic() + 15
        with handlers_lock:
            finishing = list(handlers)
        for handler in finishing:
            handler.join(timeout=max(0, deadline - time.monotonic()))
        path.unlink(missing_ok=True)
        os.close(underlay)
        os.close(descriptor)
        worker.join(timeout=2)
    return 0


def control(home: Path, workspace: str, operation: str) -> dict[str, Any]:
    if operation != "stop":
        check_access(home)
    try:
        peer = connect(home, workspace)
    except (FileNotFoundError, ConnectionRefusedError):
        if operation == "stop":
            good = cleanup(home, workspace)
            return {"version": 1, "ok": good, "stopped": good}
        return {"version": 1, "ok": True, "ready": dependencies(),
                "mounted": mounted(mount_path(home, workspace)), "connected": False,
                "instance": marker(home, workspace) or "",
                "sessions": live_sessions(home, workspace)}
    with peer, peer.makefile("rwb", buffering=0) as stream:
        peer.settimeout(45)
        write_frame(stream, {"version": 1, "operation": operation})
        try:
            return read_frame(stream)
        except (EOFError, ConnectionResetError, BrokenPipeError) as exc:
            # A previous supervisor may exit after cleanup but before its stop
            # ACK. Only positively verified completion can replace that ACK.
            if (operation == "stop" and not mounted(mount_path(home, workspace))
                    and not live_sessions(home, workspace) and marker(home, workspace) is None):
                return {"version": 1, "ok": True, "stopped": True}
            raise LiveError("unavailable", "live stop was not confirmed; retry cleanup") from exc


def stop_all(home: Path) -> bool:
    """Account refresh cleanup; usable even while account restart is owed."""
    try:
        directory = private_directory(home / ".config/ccfleet/live", create=False)
    except FileNotFoundError:
        return True
    good = True
    for child in directory.iterdir():
        if not ID.fullmatch(child.name):
            continue
        try:
            if control(home, child.name, "stop").get("stopped") is not True:
                good = False
        except (OSError, ValueError, EOFError, subprocess.SubprocessError):
            good = False
    return good


def link(home: Path, workspace: str, instance: str, input_: BinaryIO, output: BinaryIO) -> int:
    peer = ensure_daemon(home, workspace, instance)
    with peer, peer.makefile("rwb", buffering=0) as stream:
        write_frame(stream, {"version": 1, "operation": "link", "instance": instance})
        answer = read_frame(stream)
        write_frame(output, answer)
        if answer.get("ok") is not True:
            return 2
        peer.settimeout(None)
        while True:
            ready, _, _ = select.select([input_, peer], [], [])
            if input_ in ready:
                data = os.read(input_.fileno(), 64 * 1024)
                if not data:
                    return 0
                peer.sendall(data)
            if peer in ready:
                data = peer.recv(64 * 1024)
                if not data:
                    return 0
                output.write(data)
                output.flush()


def run_claude(home: Path, workspace: str, mode: str, model: str, effort: str) -> None:
    check_access(home)
    status = control(home, workspace, "status")
    if not status.get("mounted") or not status.get("connected"):
        raise LiveError("offline", "connect the local filesystem before starting Claude")
    root = mount_path(home, workspace)
    os.chdir(root)
    if not mounted(root):
        raise LiveError("offline", "the live filesystem is no longer mounted")
    command = project.claude_command(home, mode, model, effort)
    os.umask(0o077)
    os.execve(command[0], command, project.slot_environment(home))


def open_session(home: Path, workspace: str, action: str, name: str,
                 mode: str, model: str, effort: str) -> None:
    project.validate_session(workspace, action, name, mode, model, effort)
    check_access(home)
    status = control(home, workspace, "status")
    if not status.get("mounted") or not status.get("connected"):
        raise LiveError("offline", "connect the local filesystem before opening this session")
    target = "l_" + workspace + "_" + name
    with project.project_lock(home, "live-session-" + workspace):
        exists = target in project.sessions(home)
        if exists and action == "new":
            raise LiveError("session", "session already exists; choose another name")
        if not exists:
            result = subprocess.run([project.TMUX, "new-session", "-d", "-s", target,
                                     "-c", str(home), project.PYTHON, "-I",
                                     str(Path(__file__).resolve()), "_run", workspace,
                                     mode, model, effort], capture_output=True, timeout=15,
                                    env=project.slot_environment(home))
            if result.returncode:
                raise LiveError("session", "could not start the live slot Claude session")
    os.execve(project.TMUX, [project.TMUX, "attach-session", "-t", "=" + target],
              project.slot_environment(home))


def main(argv: list[str]) -> int:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        if argv == ["_stop-all"]:
            return 0 if stop_all(home) else 2
        if len(argv) < 2:
            raise LiveError("invalid", "unsupported live entry point")
        action, workspace = argv[0], identifier(argv[1])
        if action == "link" and len(argv) == 3:
            return link(home, workspace, identifier(argv[2]), sys.stdin.buffer, sys.stdout.buffer)
        if action in {"status", "stop"} and len(argv) == 2:
            answer = control(home, workspace, action)
            write_frame(sys.stdout.buffer, answer)
            return 0 if answer.get("ok") is True else 2
        if action == "_daemon" and len(argv) == 3:
            return run_daemon(home, workspace, identifier(argv[2]))
        if action == "session" and len(argv) == 7:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise LiveError("invalid", "live sessions require a terminal")
            open_session(home, *argv[1:])
        elif action == "_run" and len(argv) == 5:
            project.validate_session(workspace, "open", "main", *argv[2:])
            run_claude(home, *argv[1:])
        else:
            raise LiveError("invalid", "unsupported live entry point")
    except (LiveError, project.ProjectAccessError, OSError, ValueError,
            subprocess.SubprocessError) as exc:
        code = error_code(exc)
        if argv and argv[0] in {"link", "status", "stop"}:
            write_frame(sys.stdout.buffer, {"version": 1, "ok": False, "error": code,
                                            "message": "live workspace operation unavailable"})
        else:
            print("live workspace operation unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
