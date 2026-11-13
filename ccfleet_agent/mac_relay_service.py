"""GUI LaunchAgent serving bounded per-request native-account relay workers.

Run in the owner's GUI login context. SSH clients provide only a fixed D/G role
and the existing opaque inference protocol. No provider credential crosses UDS.
"""
from __future__ import annotations

import errno
import fcntl
import importlib.util
import os
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
from typing import Optional


def sibling(name):
    if name not in {"mac_policy", "mac_credentials", "mac_relay_client"}:
        raise ValueError("unsupported private GUI relay module")
    spec = importlib.util.spec_from_file_location(
        "ccfleet_gui_service_" + name, Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise ValueError("private GUI relay runtime unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wire = sibling("mac_relay_client")
policy = wire.mac_policy
credentials = sibling("mac_credentials")
LOCK_NAME = "relay-service.lock"
QUARANTINE = "relay-quarantine.json"


class ServiceError(ValueError):
    pass


def cleanup_worker(process: subprocess.Popen) -> tuple[Optional[int], bool]:
    """Kill the owned group before the first wait/poll can release its leader PID."""
    cleaned = True
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        cleaned = False
    try:
        code = process.wait(timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        return None, False
    return code, cleaned


def worker_command(role: bytes, descriptor: int, lease: int) -> list[str]:
    action = {b"D": "--device", b"G": "--gateway", b"S": "--status"}.get(role)
    if action is None:
        raise ServiceError("invalid private relay role")
    return ["/usr/bin/python3", "-I", "-B", str(Path(__file__).absolute().with_name(
            "mac_relay_worker.py")), action, "--status-fd", str(descriptor),
            "--lease-fd", str(lease)]


class RelayService:
    def __init__(self, home: Path, *, max_workers: int = 8, max_pending: int = 8,
                 admission_timeout: float = 10, worker_timeout: float = 905,
                 status_timeout: float = 12):
        if (type(max_workers) is not int or not 1 <= max_workers <= 8
                or type(max_pending) is not int or not 0 <= max_pending <= 8
                or not 0 < admission_timeout <= 10 or not 0 < worker_timeout <= 905
                or not 0 < status_timeout <= 12):
            raise ServiceError("invalid private relay resource bounds")
        self.home = Path(home)
        self.owner = policy.authenticated_owner(self.home)
        self.path = wire.socket_path(self.home)
        self.workers = threading.BoundedSemaphore(max_workers)
        self.capacity = threading.BoundedSemaphore(max_workers + max_pending)
        self.admission_timeout = admission_timeout
        self.worker_timeout, self.status_timeout = worker_timeout, status_timeout
        self.closed = threading.Event()
        self.guard = threading.Lock()
        self.connections = set()
        self.threads = set()
        self.listener = None
        self.base = self.lock = None
        self.socket_identity = None
        self.quarantined = False
        self.quarantine_persisted = False

    def start(self):
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise ServiceError("private relay requires default child ownership")
        if self.listener is not None or self.lock is not None:
            raise ServiceError("private relay service already started")
        self.base = policy.open_directory(policy.directory(self.home), private=True)
        try:
            try:
                os.stat(QUARANTINE, dir_fd=self.base, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ServiceError("private relay cleanup requires operator review")
            self.lock = os.open(LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                0o600, dir_fd=self.base)
            info = os.fstat(self.lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner.pw_uid
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ServiceError("private relay instance lock is unsafe")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                previous = os.stat(wire.SOCKET_NAME, dir_fd=self.base, follow_symlinks=False)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                if (not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != self.owner.pw_uid
                        or stat.S_IMODE(previous.st_mode) != 0o600):
                    raise ServiceError("private relay socket path is unsafe")
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.path))
                except OSError as exc:
                    if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                        raise ServiceError("private relay socket state is unavailable") from None
                else:
                    raise ServiceError("another private relay service is listening")
                finally:
                    probe.close()
                current = os.stat(wire.SOCKET_NAME, dir_fd=self.base, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                    raise ServiceError("private relay socket changed during startup")
                os.unlink(wire.SOCKET_NAME, dir_fd=self.base)
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            previous_mask = os.umask(0o177)
            try:
                self.listener.bind(str(self.path))
            finally:
                os.umask(previous_mask)
            os.chmod(wire.SOCKET_NAME, 0o600, dir_fd=self.base, follow_symlinks=False)
            info = os.stat(wire.SOCKET_NAME, dir_fd=self.base, follow_symlinks=False)
            self.socket_identity = info.st_dev, info.st_ino
            self.listener.listen(16)
            self.listener.settimeout(0.25)
            return self
        except BaseException:
            self.close()
            raise

    def _quarantine(self):
        self.quarantined = True
        try:
            policy.write_json(self.home, QUARANTINE, {
                "version": 1, "observed_at": time.time(), "reason": "worker_cleanup_unconfirmed"})
            self.quarantine_persisted = True
        except (OSError, ValueError):
            pass
        self.closed.set()
        print("private relay worker cleanup requires operator review", file=sys.stderr)

    def serve(self):
        if self.listener is None:
            raise ServiceError("private relay service is not started")
        listener = self.listener
        while not self.closed.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.closed.is_set():
                    break
                raise
            try:
                wire.require_peer(conn, self.owner.pw_uid)
            except (OSError, ValueError):
                conn.close()
                continue
            if not self.capacity.acquire(blocking=False):
                try:
                    conn.settimeout(0.1)
                    conn.recv(1)
                    conn.sendall(b"B")
                except OSError:
                    pass
                conn.close()
                continue
            with self.guard:
                self.connections.add(conn)
                thread = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                self.threads.add(thread)
            thread.start()

    def _pump(self, conn: socket.socket, process, status_fd: int, *, status_only: bool):
        outgoing, incoming, result = bytearray(), bytearray(), bytearray()
        stdout_done = status_done = False
        input_open = process.stdin is not None
        conn.setblocking(False)
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(status_fd, False)
        if input_open:
            os.set_blocking(process.stdin.fileno(), False)
        deadline = time.monotonic() + (self.status_timeout if status_only else self.worker_timeout)
        while not self.closed.is_set() and time.monotonic() < deadline:
            if stdout_done and not outgoing:
                break
            if status_done and len(result) == 4 and not outgoing:
                # A published result follows stdout.flush(). The worker holds
                # its private parent lease, so EAGAIN now proves the data pipe
                # is drained without reaping/exiting the group leader first.
                try:
                    data = os.read(process.stdout.fileno(), wire.CHUNK)
                except BlockingIOError:
                    data = b""
                if not data:
                    stdout_done = True
                    break
                outgoing.extend(wire.frame(b"D", data))
            poller = select.poll()
            poller.register(conn.fileno(), (select.POLLIN if len(incoming) < wire.BUFFER else 0)
                            | (select.POLLOUT if outgoing else 0))
            if not stdout_done:
                room = len(outgoing) <= wire.BUFFER - wire.CHUNK - wire.HEADER.size
                if room:
                    poller.register(process.stdout.fileno(), select.POLLIN)
            if not status_done:
                poller.register(status_fd, select.POLLIN)
            if input_open:
                poller.register(process.stdin.fileno(), select.POLLOUT if incoming else 0)
            ready = dict(poller.poll(100))
            client_events = ready.get(conn.fileno(), 0)
            if client_events & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                raise ServiceError("private relay client disconnected")
            if client_events & select.POLLIN and len(incoming) < wire.BUFFER:
                try:
                    data = conn.recv(min(wire.CHUNK, wire.BUFFER - len(incoming)))
                except BlockingIOError:
                    data = None
                if data == b"":
                    raise ServiceError("private relay client disconnected")
                if data:
                    if status_only or not input_open:
                        raise ServiceError("private relay received unexpected input")
                    incoming.extend(data)
            if outgoing and client_events & select.POLLOUT:
                try:
                    count = conn.send(outgoing[:wire.CHUNK])
                    if count <= 0:
                        raise ServiceError("private relay client disconnected")
                    del outgoing[:count]
                except BlockingIOError:
                    pass
            if input_open and ready.get(process.stdin.fileno(), 0) & (
                    select.POLLERR | select.POLLHUP | select.POLLNVAL):
                input_open = False
                incoming.clear()
            if input_open and incoming and ready.get(process.stdin.fileno(), 0) & select.POLLOUT:
                try:
                    count = os.write(process.stdin.fileno(), incoming[:wire.CHUNK])
                    del incoming[:count]
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    input_open = False
                    incoming.clear()
            if not status_done and ready.get(status_fd, 0) & (select.POLLIN | select.POLLHUP):
                try:
                    data = os.read(status_fd, 5 - len(result))
                except BlockingIOError:
                    data = None
                if data == b"":
                    status_done = True
                elif data:
                    result.extend(data)
                    if len(result) > 4:
                        raise ServiceError("private relay worker status is invalid")
            if not stdout_done and ready.get(process.stdout.fileno(), 0) & (
                    select.POLLIN | select.POLLHUP) and len(outgoing) <= (
                        wire.BUFFER - wire.CHUNK - wire.HEADER.size):
                try:
                    data = os.read(process.stdout.fileno(), wire.CHUNK)
                except BlockingIOError:
                    data = None
                if data == b"":
                    stdout_done = True
                elif data:
                    outgoing.extend(wire.frame(b"D", data))
        if not stdout_done or outgoing or self.closed.is_set():
            raise ServiceError("private relay worker deadline or shutdown")
        # Pipe EOF can arrive one poll before its independent private status FD.
        if len(result) < 4:
            readable, _, _ = select.select([status_fd], [], [], 0.2)
            if readable:
                result.extend(os.read(status_fd, 5 - len(result)))
        return struct.unpack("!i", result)[0] if len(result) == 4 else 255

    def _handle(self, conn: socket.socket):
        acquired = False
        process = None
        status_read = status_write = lease_read = lease_write = None
        result = 255
        completed = False
        try:
            conn.settimeout(3)
            role = conn.recv(1)
            if role not in wire.ROLES:
                return
            if role != b"S":
                try:
                    policy.require_enabled(self.home)
                except (OSError, ValueError):
                    conn.sendall(b"D")
                    return
            deadline = time.monotonic() + self.admission_timeout
            while not self.closed.is_set() and time.monotonic() < deadline:
                if self.workers.acquire(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                    acquired = True
                    break
            if not acquired:
                conn.sendall(b"B")
                return
            if self.closed.is_set():
                return
            # Revalidate after queueing; revocation cannot be hidden by an old
            # admission decision. S is a closed local read-only status RPC.
            if role != b"S":
                try:
                    policy.require_enabled(self.home)
                except (OSError, ValueError):
                    conn.sendall(b"D")
                    return
            readable, _, _ = select.select([conn], [], [], 0)
            if readable and conn.recv(1, socket.MSG_PEEK) == b"":
                return
            status_read, status_write = os.pipe()
            lease_read, lease_write = os.pipe()
            process = subprocess.Popen(worker_command(role, status_write, lease_read),
                stdin=subprocess.DEVNULL if role == b"S" else subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(status_write, lease_read), start_new_session=True, close_fds=True,
                cwd=self.home, env=credentials.native_environment(self.home))
            os.close(status_write)
            status_write = None
            os.close(lease_read)
            lease_read = None
            conn.sendall(b"O")
            result = self._pump(conn, process, status_read, status_only=role == b"S")
            completed = True
        except (OSError, ValueError):
            pass
        finally:
            cleaned = True
            if process is not None:
                _code, cleaned = cleanup_worker(process)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
                if not cleaned:
                    self._quarantine()
            if completed and cleaned:
                try:
                    conn.settimeout(1)
                    conn.sendall(wire.frame(b"X", struct.pack("!i", result)))
                except OSError:
                    pass
            for descriptor in (status_read, status_write, lease_read, lease_write):
                if descriptor is not None:
                    os.close(descriptor)
            conn.close()
            if acquired:
                self.workers.release()
            self.capacity.release()
            with self.guard:
                self.connections.discard(conn)
                self.threads.discard(threading.current_thread())

    def close(self):
        self.closed.set()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        with self.guard:
            connections, threads = list(self.connections), list(self.threads)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            self._quarantine()
        if self.quarantined and not self.quarantine_persisted:
            # Keep the instance lock while a durable fail-closed marker cannot
            # be written. Automatic restart must not admit another worker set.
            return False
        if self.base is not None and self.socket_identity is not None:
            try:
                info = os.stat(wire.SOCKET_NAME, dir_fd=self.base, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == self.socket_identity:
                    os.unlink(wire.SOCKET_NAME, dir_fd=self.base)
            except FileNotFoundError:
                pass
        for descriptor in (self.lock, self.base):
            if descriptor is not None:
                os.close(descriptor)
        self.lock = self.base = None
        return True


def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv) or sys.platform != "darwin":
        return 2
    service = None
    try:
        owner = policy.authenticated_owner()
        home = Path(owner.pw_dir)
        if Path.home() != home or os.environ.get("CLAUDE_CONFIG_DIR"):
            raise ServiceError("private relay requires its native default home")
        service = RelayService(home).start()
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(number, lambda *_args: service.closed.set())
        service.serve()
        return 1 if service.quarantined else 0
    except (OSError, ValueError):
        print("private Mac relay service is unavailable", file=sys.stderr)
        return 2
    finally:
        if service is not None:
            while not service.close():
                time.sleep(1)
                service._quarantine()


if __name__ == "__main__":
    raise SystemExit(main())
