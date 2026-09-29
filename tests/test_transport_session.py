"""Owned fake SSH processes over real private Unix sockets; no network or model."""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ccfleet_agent.inference_client import RelayError, TransportSession

FAKE_SSH = r"""
import json, os, signal, socket, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
options = {}
i = 0
while i < len(args):
    if args[i] == '-o':
        key, value = args[i + 1].split('=', 1)
        options[key.lower()] = value
        i += 2
    else:
        i += 1
path = options['controlpath']
mode = os.environ.get('FAKE_SSH_MODE', 'normal')
root = Path(os.environ['FAKE_SSH_ROOT'])
if '-M' not in args:
    action = args[args.index('-O') + 1] if '-O' in args else 'channel'
    # Never interpret an unavailable socket as permission for a new transport.
    assert options['proxycommand'] == '/bin/false'
    peer = socket.socket(socket.AF_UNIX)
    try:
        peer.connect(path)
        peer.sendall(action.encode())
        result = peer.recv(32)
        if result == b'OK':
            if action == 'channel':
                sys.stdout.write('channel-ok')
            sys.exit(0)
    except OSError:
        pass
    finally:
        peer.close()
    sys.exit(255)
assert '-N' in args and options['controlpersist'] == 'no'
assert options['controlmaster'] == 'yes'
(root / 'master-started').write_text('1')
if mode == 'exit':
    sys.exit(42)
if mode == 'never_ready':
    time.sleep(30)
if mode == 'regular':
    Path(path).write_text('not a socket')
    time.sleep(30)
listener = socket.socket(socket.AF_UNIX)
listener.bind(path)
os.chmod(path, 0o666 if mode == 'public' else 0o600)
listener.listen(32)
if mode == 'stubborn':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    def terminate(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
proxy = subprocess.Popen([sys.executable, '-c',
    "import sys; from pathlib import Path; sys.stdin.buffer.read(); "
    "Path(sys.argv[1]).write_text('proxy-drained')", str(root / 'proxy-drained')],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    while True:
        peer, _ = listener.accept()
        try:
            action = peer.recv(32)
            if action == b'crash':
                os._exit(9)
            peer.sendall(b'OK')
        finally:
            peer.close()
        if action == b'exit' and mode != 'stubborn':
            break
finally:
    proxy.stdin.close()
    proxy.wait(timeout=3)
    listener.close()
    Path(path).unlink(missing_ok=True)
"""


@pytest.fixture
def setup():
    # pytest's default macOS temp path is too long for OpenSSH's own temporary
    # socket suffix. Keep this synthetic private test root intentionally short.
    directory = Path(tempfile.mkdtemp(prefix="cft-", dir="/tmp")).resolve()
    directory.chmod(0o700)
    executable = directory / "fake-ssh"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_SSH)
    executable.chmod(0o700)
    base = [
        str(executable),
        "-T",
        "-F",
        "/dev/null",
        "-o",
        "ProxyCommand=synthetic-private-proxy",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=synthetic-pin",
        "-o",
        "IdentityFile=synthetic-key",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "slot01@synthetic-host",
    ]
    env = {"PATH": os.defpath, "FAKE_SSH_ROOT": str(directory)}
    sessions = []

    def session(**kwargs):
        instance = TransportSession(
            lambda: list(base),
            lambda: dict(env),
            directory,
            timeout=kwargs.pop("timeout", 1),
            **kwargs,
        )
        sessions.append(instance)
        return instance

    yield directory, base, env, session
    for instance in sessions:
        try:
            instance.close()
        except RelayError:
            pass
    shutil.rmtree(directory)


def run(command, env):
    return subprocess.run(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False
    )


def test_one_owned_foreground_master_serves_concurrent_channels_and_closes_proxy(setup):
    directory, _, env, factory = setup
    session = factory().start()
    master = session._process
    assert master is not None and master.poll() is None
    assert os.getpgid(master.pid) == os.getpgrp()
    control = Path(session._path)
    assert control.parent.stat().st_mode & 0o777 == 0o700
    assert control.stat().st_mode & 0o777 == 0o600
    commands = [session.command(["ccfleet-inference-v1"]) for _ in range(6)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda command: run(command, env), commands))
    assert all(result.returncode == 0 and result.stdout == b"channel-ok" for result in results)
    assert session._process is master and master.poll() is None
    session.close()
    assert master.returncode == 0
    assert (directory / "proxy-drained").read_text() == "proxy-drained"
    assert not control.exists() and not control.parent.exists()
    session.close()  # Idempotent; no retained PID or repeated signal.


def test_command_preserves_pins_and_uses_no_proxy_fallback(setup):
    _, base, _, factory = setup
    session = factory().start()
    command = session.command(["ccfleet-inference-v1"])
    assert "ProxyCommand=synthetic-private-proxy" not in command
    assert command.count("ProxyCommand=/bin/false") == 1
    for item in (
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=synthetic-pin",
        "IdentityFile=synthetic-key",
        "ForwardAgent=no",
        "ClearAllForwardings=yes",
    ):
        assert item in command
    assert command[-2:] == ["slot01@synthetic-host", "ccfleet-inference-v1"]
    base[-1] = "other-account@other-host"
    assert session.command(["ccfleet-inference-v1"])[-2] == "slot01@synthetic-host"


def test_master_crash_fails_future_channels_and_early_argv_without_retry(setup):
    directory, _, env, factory = setup
    session = factory().start()
    command = session.command(["ccfleet-inference-v1"])
    peer = socket.socket(socket.AF_UNIX)
    peer.connect(session._path)
    peer.sendall(b"crash")
    peer.close()
    assert session._process.wait(timeout=3) == 9
    with pytest.raises(RelayError, match="no request was retried"):
        session.command(["ccfleet-inference-v1"])
    result = run(command, env)
    assert result.returncode == 255
    session.close()
    # Owned fake proxy drains on EOF even when its master crashes abruptly.
    deadline = time.monotonic() + 2
    while not (directory / "proxy-drained").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (directory / "proxy-drained").exists()
    assert (directory / "master-started").read_text() == "1"


def test_external_revocation_terminates_master_and_cannot_reconnect(setup):
    _, _, _, factory = setup
    session = factory().start()
    session._process.terminate()
    session._process.wait(timeout=3)
    with pytest.raises(RelayError):
        session.command(["ccfleet-inference-v1"])
    session.close()


@pytest.mark.parametrize("mode", ["exit", "never_ready", "regular", "public"])
def test_startup_failure_is_bounded_and_never_sends_remote_command(setup, mode):
    directory, _, env, factory = setup
    env["FAKE_SSH_MODE"] = mode
    session = factory(timeout=0.15)
    with pytest.raises(RelayError) as error:
        session.start()
    assert "synthetic-private-proxy" not in str(error.value)
    assert session._process is None or session._process.poll() is not None
    if mode in ("exit", "never_ready"):
        assert not Path(session._path).parent.exists()
    if mode == "regular":
        # Never delete a path which failed the socket-type check.
        assert Path(session._path).read_text() == "not a socket"
    assert not (directory / "channel").exists()


def test_stubborn_master_is_killed_but_close_reports_unconfirmed_proxy_cleanup(setup):
    directory, _, env, factory = setup
    env["FAKE_SSH_MODE"] = "stubborn"
    session = factory().start()
    with pytest.raises(RelayError, match="not fully confirmed"):
        session.close()
    assert session._process.poll() is not None
    assert not Path(session._path).parent.exists()
    deadline = time.monotonic() + 2
    while not (directory / "proxy-drained").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (directory / "proxy-drained").exists()


@pytest.mark.parametrize("kind", ["symlink", "public", "long"])
def test_unsafe_or_oversize_parent_fails_before_spawning(setup, kind):
    directory, base, env, _ = setup
    path = directory / "private"
    if kind == "symlink":
        path.symlink_to(directory, target_is_directory=True)
    elif kind == "public":
        path.mkdir(mode=0o755)
    else:
        path = directory / ("x" * 100)
        path.mkdir(mode=0o700)
    session = TransportSession(lambda: base, lambda: env, path, timeout=0.1)
    with pytest.raises(RelayError):
        session.start()
    assert session._process is None
    assert not (directory / "master-started").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["-f"],
        ["-o", "ControlPersist=yes"],
        ["-o", "ControlMaster=auto"],
        ["-o", "ProxyJump=another"],
        ["-o", "RemoteCommand=secret"],
        ["-o", "ControlPath=/tmp/shared"],
    ],
)
def test_competing_transport_options_cannot_override_owned_lifetime(setup, arguments):
    directory, base, env, _ = setup
    bad = base[:-1] + arguments + base[-1:]
    session = TransportSession(lambda: bad, lambda: env, directory)
    with pytest.raises(RelayError):
        session.start()
    assert session._process is None


def test_replaced_control_socket_is_not_used_or_deleted(setup):
    _, _, _, factory = setup
    session = factory().start()
    control = Path(session._path)
    old = control.with_name("original")
    control.rename(old)
    replacement = socket.socket(socket.AF_UNIX)
    replacement.bind(str(control))
    control.chmod(0o600)
    try:
        with pytest.raises(RelayError, match="changed"):
            session.command(["ccfleet-inference-v1"])
        with pytest.raises(RelayError, match="not fully confirmed"):
            session.close()
    finally:
        replacement.close()
    assert session._process.poll() is not None


@pytest.mark.parametrize("remote", [[], "ccfleet-inference-v1", ["bad\ncommand"], [""], [None]])
def test_invalid_channel_args_are_rejected(setup, remote):
    _, _, _, factory = setup
    session = factory().start()
    with pytest.raises(RelayError):
        session.command(remote)


def test_double_start_and_closed_session_are_not_reused(setup):
    _, _, _, factory = setup
    session = factory()
    with pytest.raises(RelayError):
        session.command(["ccfleet-inference-v1"])
    session.start()
    with pytest.raises(RelayError):
        session.start()
    session.close()
    with pytest.raises(RelayError):
        session.start()
    with pytest.raises(RelayError):
        session.command(["ccfleet-inference-v1"])


def test_unknown_files_in_private_control_directory_are_preserved(setup):
    _, _, _, factory = setup
    session = factory().start()
    protected = Path(session._path).parent / "preserve"
    protected.write_text("keep unrelated content")
    with pytest.raises(RelayError, match="not fully confirmed"):
        session.close()
    assert protected.read_text() == "keep unrelated content"
    assert session._process.poll() is not None


def test_keyboard_interrupt_during_start_closes_owned_process(setup, monkeypatch):
    _, _, _, factory = setup
    session = factory()
    original = session._control

    def interrupted(action, timeout):
        if action == "check":
            raise KeyboardInterrupt
        return original(action, timeout)

    monkeypatch.setattr(session, "_control", interrupted)
    with pytest.raises(KeyboardInterrupt):
        session.start()
    assert session._process.poll() is not None
    assert not Path(session._path).parent.exists()


def test_transport_cleanup_never_signals_caller_process_group(setup, monkeypatch):
    _, _, _, factory = setup

    def forbidden(*args, **kwargs):
        pytest.fail("transport must not signal the caller or a saved group id")

    monkeypatch.setattr(os, "killpg", forbidden)
    with factory() as session:
        assert session.command(["ccfleet-inference-v1"])
