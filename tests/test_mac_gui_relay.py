"""Synthetic UDS/pipe workers exercise the GUI broker without native credentials."""
from __future__ import annotations

import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import mac_policy
from ccfleet_agent import mac_relay_client as client
from ccfleet_agent import mac_relay_service as service
from ccfleet_agent import mac_relay_worker as worker

PEER = r'''
import json,os,struct,sys,time
role,descriptor,lease,mode=sys.argv[1],int(sys.argv[2]),int(sys.argv[3]),sys.argv[4]
os.set_inheritable(descriptor,False)
if mode=='hang':
    while True:time.sleep(1)
if mode=='flood':
    os.write(1,b'x'*(2*1024*1024))
    while True:time.sleep(1)
if role=='S':
    data=json.dumps({'available':True,'expires_in_seconds':3600,'binding_matches':True,'policy_enabled':False}).encode()
else:
    data=b''
    while len(data)<5:
        part=os.read(0,5-len(data))
        if not part:raise SystemExit(2)
        data+=part
    data=b'reply:'+data
os.write(1,data)
if mode=='crash':os._exit(7)
code=2 if mode=='error' else 0
os.write(descriptor,struct.pack('!i',code))
os.close(descriptor)
os.read(lease,1)
os._exit(code)
'''


@pytest.fixture
def owner(monkeypatch):
    # macOS Unix socket paths are short; pytest's nested default temp path is not.
    with tempfile.TemporaryDirectory(prefix='ccfgui-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        home = root / 'h'
        home.mkdir(mode=0o700)
        base = home / '.config/ccfleet/private-owner'
        base.mkdir(parents=True, mode=0o700)
        for path in (home / '.config', home / '.config/ccfleet', base):
            path.chmod(0o700)
        identity = SimpleNamespace(pw_uid=os.getuid(), pw_dir=str(home), pw_name='synthetic')
        monkeypatch.setattr(mac_policy, '_TRUST_ROOT', root)
        monkeypatch.setattr(mac_policy, 'authenticated_owner', lambda selected=None: identity)
        monkeypatch.setattr(mac_policy, 'require_enabled', lambda selected: None)
        monkeypatch.setattr(client, 'mac_policy', mac_policy)
        monkeypatch.setattr(service, 'policy', mac_policy)
        monkeypatch.setattr(service.wire, 'mac_policy', mac_policy)
        monkeypatch.setattr(service.credentials, 'native_environment', lambda selected:
                            {'HOME': str(selected), 'PATH': '/usr/bin:/bin'})
        yield home, base, identity


@pytest.fixture
def running(owner, monkeypatch):
    made = []
    calls = []
    modes = {'value': 'ok'}
    def command(role, descriptor, lease):
        calls.append(role)
        return [sys.executable, '-c', PEER, role.decode(), str(descriptor), str(lease), modes['value']]
    monkeypatch.setattr(service, 'worker_command', command)
    def start(**kwargs):
        instance = service.RelayService(owner[0], **kwargs).start()
        thread = threading.Thread(target=instance.serve, daemon=True)
        thread.start()
        made.append((instance, thread))
        return instance
    yield start, calls, modes
    for instance, thread in made:
        instance.close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def complete(sock):
    received, body = bytearray(), bytearray()
    sock.settimeout(3)
    while True:
        data = sock.recv(client.CHUNK)
        if not data:
            return bytes(body), None
        received.extend(data)
        while True:
            parsed = client.take_frame(received)
            if parsed is None:
                break
            kind, payload = parsed
            if kind == b'X':
                return bytes(body), struct.unpack('!i', payload)[0]
            body.extend(payload)


def test_status_works_policy_off_and_device_role_never_launches(owner, running, monkeypatch):
    start, calls, _modes = running
    monkeypatch.setattr(mac_policy, 'require_enabled', lambda home:
                        (_ for _ in ()).throw(mac_policy.MacPolicyError('disabled')))
    start()
    assert client.status(owner[0]) == {'available': True, 'expires_in_seconds': 3600,
                                      'binding_matches': True, 'policy_enabled': False}
    with pytest.raises(client.RelayClientError) as caught:
        client.connect(owner[0], b'D')
    assert caught.value.status == 403 and calls == [b'S']


@pytest.mark.parametrize('role', [b'D', b'G'])
@pytest.mark.parametrize('mode,code', [('ok', 0), ('error', 2), ('crash', 255)])
def test_worker_exit_status_survives_opaque_protocol_transport(owner, running, role, mode, code):
    start, _calls, modes = running
    modes['value'] = mode
    start()
    sock = client.connect(owner[0], role)
    try:
        sock.sendall(b'hello')
        body, result = complete(sock)
        assert body == b'reply:hello'
        assert result == code if code != 255 else result in (None, 255)
    finally:
        sock.close()


def test_disconnect_cleans_owned_worker_and_restores_bounded_capacity(owner, running):
    start, calls, modes = running
    modes['value'] = 'hang'
    instance = start(max_workers=1, max_pending=1, admission_timeout=0.20)
    sock = client.connect(owner[0], b'D')
    sock.sendall(b'partial request upload')
    with pytest.raises(client.RelayClientError) as caught:
        client.connect(owner[0], b'G')
    assert caught.value.status == 503 and calls == [b'D']
    sock.close()
    deadline = time.monotonic() + 3
    while instance.connections and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not instance.connections
    modes['value'] = 'ok'
    assert client.status(owner[0])['available'] is True


def test_prefix_stalls_count_against_total_pending_bound(owner, running):
    start, calls, _modes = running
    start(max_workers=1, max_pending=0)
    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    held.connect(str(client.socket_path(owner[0])))
    time.sleep(0.05)
    try:
        with pytest.raises(client.RelayClientError) as caught:
            client.connect(owner[0], b'S')
        assert caught.value.status == 503 and calls == []
    finally:
        held.close()


def test_wrong_or_unavailable_peer_identity_is_rejected_both_sides(owner, running, monkeypatch):
    start, calls, _modes = running
    start()
    monkeypatch.setattr(client, 'peer_uid', lambda sock: None)
    with pytest.raises(client.RelayClientError):
        client.connect(owner[0], b'S')
    monkeypatch.setattr(client, 'peer_uid', lambda sock: owner[2].pw_uid)
    monkeypatch.setattr(service.wire, 'peer_uid', lambda sock: owner[2].pw_uid + 1)
    with pytest.raises((client.RelayClientError, OSError)):
        client.connect(owner[0], b'S')
    assert calls == []


def test_socket_permissions_and_single_instance_are_enforced(owner, running):
    start, _calls, _modes = running
    start()
    path = client.socket_path(owner[0])
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises((OSError, ValueError)):
        service.RelayService(owner[0]).start()
    assert path.exists()
    path.chmod(0o666)
    with pytest.raises(client.RelayClientError):
        client.connect(owner[0], b'S')
    path.chmod(0o600)


def test_active_foreign_socket_is_not_unlinked_even_when_instance_lock_is_free(owner):
    path = client.socket_path(owner[0])
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    before = path.stat().st_ino
    try:
        with pytest.raises(service.ServiceError):
            service.RelayService(owner[0]).start()
        assert path.stat().st_ino == before
    finally:
        listener.close()
        path.unlink()


@pytest.mark.parametrize('kind', ['file', 'symlink'])
def test_unsafe_socket_path_is_never_removed(owner, kind):
    path = client.socket_path(owner[0])
    if kind == 'file':
        path.write_text('preserve')
        path.chmod(0o600)
    else:
        path.symlink_to('missing')
    with pytest.raises(service.ServiceError):
        service.RelayService(owner[0]).start()
    assert path.is_symlink() if kind == 'symlink' else path.read_text() == 'preserve'


def test_cleanup_kills_group_before_any_wait_or_poll(monkeypatch):
    events = []
    monkeypatch.setattr(service.os, 'killpg', lambda pid, sig: events.append(('kill', pid)))
    class Process:
        pid = 123456
        def poll(self):
            pytest.fail('must not poll/reap before cleanup')
        def wait(self, timeout):
            events.append(('wait', self.pid))
            return 0
    assert service.cleanup_worker(Process()) == (0, True)
    assert events == [('kill', 123456), ('wait', 123456)]


def test_unknown_worker_cleanup_quarantines_future_service_admission(owner, running, monkeypatch):
    original = service.cleanup_worker
    monkeypatch.setattr(service, 'cleanup_worker', lambda process: (original(process)[0], False))
    start, _calls, _modes = running
    instance = start()
    with pytest.raises(client.RelayClientError):
        client.status(owner[0])
    deadline = time.monotonic() + 2
    while not instance.quarantined and time.monotonic() < deadline:
        time.sleep(0.01)
    assert instance.quarantined and (owner[1] / service.QUARANTINE).exists()
    instance.close()
    with pytest.raises(service.ServiceError):
        service.RelayService(owner[0]).start()


def test_client_pump_preserves_terminal_status_after_large_backpressured_output():
    peer, local = socket.socketpair()
    incoming, writer = os.pipe()
    reader, output = os.pipe()
    data = b'x' * (512 * 1024)
    results = []
    def send():
        try:
            for start in range(0, len(data), client.CHUNK):
                peer.sendall(client.frame(b'D', data[start:start + client.CHUNK]))
            peer.sendall(client.frame(b'X', struct.pack('!i', 0)))
        finally:
            peer.close()
    def pump():
        try:
            results.append(client.pump(local, input_fd=incoming, output_fd=output, timeout=5))
        finally:
            os.close(output)
    sender = threading.Thread(target=send)
    handler = threading.Thread(target=pump)
    sender.start()
    handler.start()
    time.sleep(0.10)
    received = bytearray()
    try:
        while True:
            part = os.read(reader, 65536)
            if not part:
                break
            received.extend(part)
        sender.join(3)
        handler.join(3)
        assert bytes(received) == data and results == [0]
    finally:
        for fd in (incoming, writer, reader):
            os.close(fd)


@pytest.mark.parametrize('payload,code', [(b'', 255), (b'X\x00', 255),
    (client.frame(b'D', b'reply') + client.frame(b'X', struct.pack('!i', 2)), 2)])
def test_client_pump_requires_explicit_completion(payload, code):
    peer, local = socket.socketpair()
    incoming, writer = os.pipe()
    reader, output = os.pipe()
    peer.sendall(payload)
    peer.close()
    try:
        assert client.pump(local, input_fd=incoming, output_fd=output, timeout=1) == code
    finally:
        for fd in (incoming, writer, reader, output):
            os.close(fd)


def test_status_rpc_uses_a_total_deadline_for_empty_dripping_frames(owner, monkeypatch):
    class Peer:
        def settimeout(self, value):
            pass
        def recv(self, size):
            return client.frame(b'D', b'')
        def close(self):
            pass
    ticks = iter(range(100))
    monkeypatch.setattr(client, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(client, 'connect', lambda *a, **kw: Peer())
    with pytest.raises(client.RelayClientError):
        client.status(owner[0])


def test_worker_private_status_fd_is_not_inherited_by_native_subprocesses(monkeypatch):
    read, write = os.pipe()
    lease_read, lease_write = os.pipe()
    monkeypatch.setattr(worker.sys, 'stdout', io.TextIOWrapper(io.BytesIO()))
    def run(role):
        assert role == '--device' and os.get_inheritable(write) is False
        assert os.get_inheritable(lease_read) is False
        return 2
    monkeypatch.setattr(worker, 'run', run)
    monkeypatch.setattr(worker, 'start_parent_watch', lambda descriptor: None)
    try:
        assert worker.main(['--device', '--status-fd', str(write),
                            '--lease-fd', str(lease_read)]) == 2
        assert struct.unpack('!i', os.read(read, 4))[0] == 2
    finally:
        os.close(read)
        os.close(lease_read)
        os.close(lease_write)


def test_readonly_worker_status_never_returns_native_credentials_or_calls_model():
    modules = {
        'mac_policy': SimpleNamespace(policy=lambda home: {'enabled': False, 'bound_fp': 'a'*16}),
        'local_relay': SimpleNamespace(bound_account=lambda home: 'a'*16),
        'mac_credentials': SimpleNamespace(read_oauth=lambda home: {
            'accessToken': 'synthetic-secret', 'refreshToken': 'never-return',
            'expiresAt': (time.time()+3600)*1000}),
    }
    original = worker.sibling
    worker.sibling = modules.__getitem__
    try:
        result = worker.status_snapshot(Path('/synthetic'))
    finally:
        worker.sibling = original
    assert result['available'] and result['binding_matches'] and result['policy_enabled'] is False
    assert 'synthetic-secret' not in json.dumps(result) and 'never-return' not in json.dumps(result)


def test_blocked_output_does_not_spin_and_disconnect_reaps_worker(owner, running, monkeypatch):
    start, _calls, modes = running
    modes['value'] = 'flood'
    real_poll = service.select.poll
    counter = {'polls': 0}
    class CountedPoll:
        def __init__(self):
            self.inner = real_poll()
        def register(self, *args):
            return self.inner.register(*args)
        def poll(self, timeout):
            counter['polls'] += 1
            return self.inner.poll(timeout)
    monkeypatch.setattr(service, 'select', SimpleNamespace(
        poll=CountedPoll, select=service.select.select,
        **{name: getattr(service.select, name) for name in
           ('POLLIN', 'POLLOUT', 'POLLERR', 'POLLHUP', 'POLLNVAL')}))
    instance = start(max_workers=1, max_pending=0)
    sock = client.connect(owner[0], b'D')
    time.sleep(0.15)
    before = counter['polls']
    time.sleep(0.20)
    assert counter['polls'] - before < 20
    sock.close()
    deadline = time.monotonic() + 3
    while instance.connections and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not instance.connections and not instance.quarantined
    modes['value'] = 'ok'
    assert client.status(owner[0])['available']


def test_shutdown_quarantines_unconfirmed_handler_before_releasing_instance(owner):
    instance = service.RelayService(owner[0]).start()
    class Handler:
        def join(self, timeout):
            pass
        def is_alive(self):
            return True
    instance.threads.add(Handler())
    assert instance.close() is True
    assert (owner[1] / service.QUARANTINE).exists()
    with pytest.raises(service.ServiceError):
        service.RelayService(owner[0]).start()


def test_failed_quarantine_write_retains_instance_lock(owner, monkeypatch):
    instance = service.RelayService(owner[0]).start()
    write_json = mac_policy.write_json
    monkeypatch.setattr(mac_policy, 'write_json', lambda *a, **kw:
                        (_ for _ in ()).throw(OSError('synthetic unavailable state')))
    instance._quarantine()
    assert instance.close() is False and instance.lock is not None
    with pytest.raises(OSError):
        service.RelayService(owner[0]).start()
    monkeypatch.setattr(mac_policy, 'write_json', write_json)
    instance._quarantine()
    assert instance.close() is True


@pytest.mark.parametrize('initially_closed', [False, True])
def test_worker_parent_lease_is_checked_before_work_and_cancels_live_group(tmp_path,
                                                                        initially_closed):
    lease_read, lease_write = os.pipe()
    script = r'''
import os,sys,time
sys.path.insert(0,sys.argv[1])
from ccfleet_agent import mac_relay_worker as worker
try:
    worker.start_parent_watch(int(sys.argv[2]))
except ValueError:
    raise SystemExit(2)
os.write(1,b'ready')
time.sleep(30)
'''
    if initially_closed:
        os.close(lease_write)
        lease_write = None
    process = subprocess.Popen([sys.executable, '-c', script,
                                str(Path(__file__).resolve().parents[1]), str(lease_read)],
                               pass_fds=(lease_read,), start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(lease_read)
    try:
        if initially_closed:
            output, errors = process.communicate(timeout=3)
            assert process.returncode == 2 and output == errors == b''
        else:
            assert process.stdout.read(5) == b'ready'
            os.close(lease_write)
            lease_write = None
            assert process.wait(timeout=3) < 0
    finally:
        if lease_write is not None:
            os.close(lease_write)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()


def test_service_refuses_automatic_child_reaping(owner, monkeypatch):
    monkeypatch.setattr(service.signal, 'getsignal', lambda number: service.signal.SIG_IGN)
    with pytest.raises(service.ServiceError):
        service.RelayService(owner[0]).start()
    assert not client.socket_path(owner[0]).exists()


def test_actual_worker_buffered_status_print_is_flushed_before_terminal_frame(owner, running,
                                                                             monkeypatch):
    # Exercise real run()/main(), including print() and its TextIOWrapper,
    # instead of the os.write-based transport fake used by other cases.
    script = r'''
import os,sys,threading
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from ccfleet_agent import mac_relay_worker as worker
home,role,status_fd,lease_fd=sys.argv[2:]
worker.sys.platform='darwin'
worker.sibling=lambda name: SimpleNamespace(authenticated_owner=lambda: SimpleNamespace(pw_dir=home))
worker.status_snapshot=lambda selected: {'available':True,'expires_in_seconds':3600,'binding_matches':True,'policy_enabled':False}
assert role=='S'
code=worker.main(['--status','--status-fd',status_fd,'--lease-fd',lease_fd])
if worker._RESULT_PUBLISHED and worker._WATCH_STARTED:
    threading.Event().wait()
os._exit(code)
'''
    monkeypatch.setattr(service, 'worker_command', lambda role, descriptor, lease:
        [sys.executable, '-c', script, str(Path(__file__).resolve().parents[1]),
         str(owner[0]), role.decode(), str(descriptor), str(lease)])
    start, _calls, _modes = running
    start()
    assert client.status(owner[0]) == {'available': True, 'expires_in_seconds': 3600,
                                      'binding_matches': True, 'policy_enabled': False}
