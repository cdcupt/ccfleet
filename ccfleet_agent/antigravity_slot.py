"""Interactive Google CLI and a session-only MCP channel on a private Mac slot."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

if __package__:
    from . import mac_policy, mac_relay_client
    from . import workspace_io as wire
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mac_policy
    import mac_relay_client
    import workspace_io as wire


def permitted(home, fingerprint):
    mac_policy.require_enabled(home)
    access = mac_policy.read_json(home, "grants.json")["access"]
    return any(
        hmac.compare_digest(hashlib.sha256(k.encode("ascii")).digest(), fingerprint)
        for k in access.get("workspace_public_keys", [])
        if k in access["device_public_keys"]
    )


def dimensions(terminal, data):
    if len(data) != 4:
        raise ValueError("invalid terminal dimensions")
    rows, cols = struct.unpack(">HH", data)
    if not (1 <= rows <= 4096 and 1 <= cols <= 4096):
        raise ValueError("invalid terminal dimensions")
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


RULES = """The user's active project is on their connecting computer. Use the
ccfleet-local-project MCP tools for project listings, reading and saving files, and
running tests. The current directory on this host is an empty session directory.
A request to edit or test the project refers to the workspace offered by MCP.
If the workspace tools are disconnected, tell the user before continuing.
"""


def configure(folder, path):
    cfg = folder / ".agents"
    cfg.mkdir(mode=0o700)
    (cfg / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ccfleet-local-project": {
                        "command": sys.executable,
                        "args": ["-I", "-B", str(Path(__file__).resolve()), "mcp", str(path)],
                    }
                }
            }
        )
    )
    (folder / "GEMINI.md").write_text(RULES)
    rules = cfg / "rules"
    rules.mkdir()
    (rules / "local-project.md").write_text("---\ntrigger: always_on\n---\n" + RULES)


def pump(channel, terminal, listener, authorized):
    pending = bytearray()
    peer = None
    next_check = time.monotonic()
    try:
        while True:
            if time.monotonic() >= next_check:
                if not authorized():
                    return
                next_check = time.monotonic() + 2
            sources = [channel, terminal, listener] + ([peer] if peer else [])
            ready, _, _ = select.select(sources, [], [], 1)
            for src in ready:
                if src == channel:
                    data = channel.recv(65536)
                    if not data:
                        return
                    pending.extend(data)
                    for tag, payload in wire.unpack(pending):
                        if tag == b"K":
                            wire.write(terminal, payload)
                        elif tag == b"R":
                            dimensions(terminal, payload)
                        elif tag == b"M" and peer:
                            peer.sendall(payload)
                        else:
                            raise ValueError("invalid workspace input")
                elif src == terminal:
                    try:
                        data = os.read(terminal, 65536)
                    except OSError:
                        return
                    if not data:
                        return
                    channel.sendall(wire.frame(b"O", data))
                elif src == listener:
                    conn, _ = listener.accept()
                    try:
                        mac_relay_client.require_peer(conn, os.getuid())
                        if peer:
                            conn.close()
                        else:
                            conn.settimeout(8)
                            peer = conn
                            channel.sendall(wire.frame(b"I"))
                    except Exception:
                        conn.close()
                else:
                    data = peer.recv(65536)
                    if data:
                        channel.sendall(wire.frame(b"M", data))
                    else:
                        peer.close()
                        peer = None
    finally:
        if peer:
            peer.close()


def session(channel, folder, executable, authorized):
    # Keep AF_UNIX names short even when the user's home path is long.
    with tempfile.TemporaryDirectory(
        prefix="cfw-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp"
    ) as rendezvous:
        path = Path(rendezvous) / "m"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(2)
            configure(folder, path)
            pid, terminal = pty.fork()
            if pid == 0:
                try:
                    os.chdir(folder)
                    os.environ["TERM"] = "xterm-256color"
                    os.execv(str(executable), [str(executable), "--sandbox"])
                finally:
                    os._exit(127)
            try:
                channel.sendall(wire.frame(b"H", b"ccfleet-antigravity-1"))
                pump(channel, terminal, listener, authorized)
            finally:
                os.close(terminal)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGHUP)
                for _ in range(20):
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.05)
                else:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)


def worker(fd):
    home = Path(mac_policy.authenticated_owner().pw_dir)
    with socket.socket(fileno=fd) as channel:
        mac_relay_client.require_peer(channel, os.getuid())
        channel.settimeout(8)
        fingerprint = channel.recv(32, socket.MSG_WAITALL)
        if len(fingerprint) != 32 or not permitted(home, fingerprint):
            return 2
        with tempfile.TemporaryDirectory(prefix="ccfleet-agy-") as folder:
            session(
                channel, Path(folder), home / ".local/bin/agy", lambda: permitted(home, fingerprint)
            )
    return 0


def serve():
    os.umask(0o077)
    home = Path(mac_policy.authenticated_owner().pw_dir)
    root = mac_policy.directory(home)
    directory = mac_policy.open_directory(root, private=True)
    os.close(directory)
    with (root / "workspace.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / "workspace.sock"
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(4)
            child = None
            while True:
                conn, _ = server.accept()
                with conn:
                    try:
                        mac_relay_client.require_peer(conn, os.getuid())
                        conn.settimeout(1)
                        role = conn.recv(1)
                        if role == b"P":
                            conn.sendall(b"Y")
                            continue
                        if role != b"S":
                            continue
                        conn.settimeout(None)
                    except (OSError, ValueError):
                        continue
                    if child and child.poll() is None:
                        conn.sendall(wire.frame(b"E", b"The Antigravity slot is already in use."))
                        continue
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            str(Path(__file__).resolve()),
                            "worker",
                            str(conn.fileno()),
                        ],
                        pass_fds=(conn.fileno(),),
                    )
    return 0


def main(argv):
    if argv == ["serve"]:
        return serve()
    if len(argv) == 2 and argv[0] == "worker":
        return worker(int(argv[1]))
    if len(argv) == 2 and argv[0] == "mcp":
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.connect(argv[1])
            mac_relay_client.require_peer(peer, os.getuid())
            wire.pipe_socket(peer)
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, EOFError, KeyError):
        sys.exit(2)
