"""Serve the owner's Fleet inference entry through outbound encrypted connections."""

import contextlib
import hashlib
import hmac
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import fleet_wire
import mac_keys
import mac_owner_agent as agent
import mac_policy as policy


def native_binding(home):
    # Reduce the local account identifier to the fingerprint used by the existing slot policy.
    relay = agent.sibling("local_relay")
    try:
        value = relay.read_object(home / ".claude.json", 4 * 1024 * 1024)["oauthAccount"][
            "accountUuid"
        ]
        if isinstance(value, str) and value.isascii() and 0 < len(value.strip()) <= 256:
            return hashlib.sha256(value.strip().encode()).hexdigest()[:16]
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return None


def provision(home, folder, user, node, token, origin, write):
    # Refuse to take over an existing operator installation. The ownership marker is
    # written before services start and must match this enrollment on every launch.
    private = policy.directory(home)
    private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = private / "home-registration"
    if not private.exists():
        with tempfile.TemporaryDirectory(dir=private.parent) as staging:
            initial = Path(staging) / "install"
            initial.mkdir(mode=0o700)
            write(initial / marker.name, node.encode())
            os.rename(initial, private)
    if marker.is_file() and marker.read_text() == "removed":
        write(marker, node.encode())
    if not marker.is_file() or marker.read_text() != node:
        raise ValueError("This Mac already has a private Fleet runtime.")
    descriptor = policy.open_directory(private, private=True)
    os.close(descriptor)
    current = private / "policy.json"
    if not current.exists() or not policy.policy(home)["enabled"]:
        bound = native_binding(home)
        write(
            current,
            json.dumps(
                {
                    "version": 1,
                    "enabled": bool(bound),
                    "unix_user": user,
                    "bound_fp": bound or "0" * 16,
                }
            ).encode(),
        )
    reporting = hmac.digest(bytes.fromhex(token), b"ccfleet-home-report-v1", "sha256").hex()
    native = next(
        (
            p
            for p in (
                home / ".local/bin/claude",
                Path("/opt/homebrew/bin/claude"),
                Path("/usr/local/bin/claude"),
            )
            if p.is_file()
        ),
        home / ".local/bin/claude",
    )
    cfg = {
        "version": 1,
        "url": origin,
        "node_id": node,
        "node_token": reporting,
        "native_claude": str(native),
    }
    write(private / "agent.json", json.dumps(cfg).encode())
    return cfg


def write_grants(home, folder, user, snapshot, write):
    snapshot = mac_keys.validate_snapshot(snapshot)
    if snapshot["unix_user"] != user:
        raise ValueError("Fleet returned another slot owner.")
    # Home registrations only expose the private inference entry. Never edit the
    # Mac user's normal authorized_keys or hand a workspace key an interactive shell.
    workspace = set(snapshot.pop("workspace_public_keys", []))
    snapshot["device_public_keys"] = [
        key for key in snapshot["device_public_keys"] if key not in workspace
    ]
    entry = mac_keys._entrypoint(
        Path(__file__).resolve().with_name("mac_slot_entry.py"), os.getuid()
    )
    keys = mac_keys._render(b"", snapshot, entry)
    write(folder / "authorized_keys", keys)
    policy.write_json(
        home,
        "grants.json",
        {
            "version": 1,
            "clock": policy.LEASE_CLOCK_FORMAT,
            "received_at": time.time(),
            "received_monotonic": policy.lease_clock(),
            "access": snapshot,
        },
    )


def sshd_config(folder, user, write):
    def quote(path):
        return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("Unsupported Mac login.")
    path = folder / "sshd_config"
    text = f"""HostKey {quote(folder / "slot_host")}
AuthorizedKeysFile {quote(folder / "authorized_keys")}
AllowUsers {user}
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
HostbasedAuthentication no
DisableForwarding yes
PermitUserRC no
PermitUserEnvironment no
PermitTTY no
MaxSessions 1
LoginGraceTime 12
UsePAM no
LogLevel ERROR
"""
    write(path, text.encode())
    return path


def local_pair():
    # Apple's sshd uses the IP peer for its audit session. Create the two ends
    # ourselves and close the ephemeral listener before starting authentication.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        client = socket.create_connection(listener.getsockname(), timeout=2)
        try:
            listener.settimeout(2)
            for _ in range(4):
                server, address = listener.accept()
                if address == client.getsockname():
                    client.settimeout(None)
                    return client, server
                server.close()
            raise OSError("Local transport unavailable.")
        except BaseException:
            client.close()
            raise


def serve_stream(origin, token, ticket, config):
    if not isinstance(ticket, str) or not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise ValueError("Invalid stream ticket.")
    endpoint = origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    remote, initial = fleet_wire.open_websocket(endpoint + "/api/home-macs/stream/" + ticket, token)
    with remote, contextlib.ExitStack() as stack:
        local, child = local_pair()
        stack.enter_context(local)
        stack.enter_context(child)
        process = subprocess.Popen(
            ["/usr/sbin/sshd", "-i", "-e", "-f", str(config)],
            stdin=child,
            stdout=child,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        child.close()
        try:
            with os.fdopen(os.dup(local.fileno()), "wb", buffering=0) as output:
                fleet_wire._proxy_stream(
                    remote, initial, input_fd=local.fileno(), output_fd=output.fileno()
                )
        finally:
            local.close()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run(home, folder, user, token, origin, post, write, rejected):
    node = (folder / "registration").read_text()
    cfg = provision(home, folder, user, node, token, origin, write)
    config = sshd_config(folder, user, write)
    # Erase stale grants before admitting streams after a restart.
    write(folder / "authorized_keys", b"")
    stopped = threading.Event()

    def report():
        while not stopped.is_set():
            try:
                provision(home, folder, user, node, token, origin, write)
                code, reply = agent.post_heartbeat(cfg, agent.build_payload(cfg, home))
                if code == 200:
                    write_grants(home, folder, user, reply["private_owner_access"], write)
                    agent.remember_quota_request(home, reply.get("private_owner_quota"))
                elif code in (401, 403):
                    write(folder / "authorized_keys", b"")
            except (OSError, KeyError, ValueError, subprocess.SubprocessError):
                write(folder / "authorized_keys", b"")
            stopped.wait(40)

    capacity = threading.BoundedSemaphore(3)

    def session(ticket):
        try:
            serve_stream(origin, token, ticket, config)
        except (OSError, EOFError, ValueError, fleet_wire.CliError, subprocess.SubprocessError):
            pass
        finally:
            capacity.release()

    def poll():
        delay = 1
        while not stopped.is_set():
            if not capacity.acquire(timeout=1):
                continue
            started = False
            try:
                answer = post(origin, "wait", token)
                if answer.get("ticket"):
                    worker = threading.Thread(target=session, args=(answer["ticket"],), daemon=True)
                    worker.start()
                    started = True
                else:
                    stopped.wait(random.uniform(1, 2))
                delay = 1
            except rejected as exc:
                if exc.status == 403:
                    stopped.set()
                    return
                stopped.wait(delay)
                delay = min(15, delay * 2)
            except (OSError, ValueError):
                stopped.wait(random.uniform(delay / 2, delay))
                delay = min(15, delay * 2)
            finally:
                if not started:
                    capacity.release()

    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).with_name("mac_relay_service.py"))],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        threading.Thread(target=report, daemon=True).start()
        threading.Thread(target=poll, daemon=True).start()
        while not stopped.is_set() and process.poll() is None:
            try:
                post(origin, "check-in", token)
            except rejected as exc:
                if exc.status == 403:
                    stopped.set()
                    write(folder / "revoked", b"")
            except (OSError, ValueError):
                pass
            stopped.wait(35)
        if stopped.is_set():
            write(folder / "revoked", b"")
    finally:
        stopped.set()
        write(folder / "authorized_keys", b"")
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
