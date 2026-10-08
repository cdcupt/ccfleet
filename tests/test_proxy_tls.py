"""Exercise the SSH byte pump over trusted, synthetic, loopback TLS."""

from __future__ import annotations

import contextlib
import os
import runpy
import select
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest


def _payload(size, offset=0):
    pattern = bytes((number + offset) % 256 for number in range(256))
    return (pattern * (size // len(pattern) + 1))[:size]


def _server_frame(payload, opcode=2):
    size = len(payload)
    if size < 126:
        length = bytes((size,))
    elif size <= 65535:
        length = b"\x7e" + struct.pack(">H", size)
    else:
        length = b"\x7f" + struct.pack(">Q", size)
    return bytes((0x80 | opcode,)) + length + payload


def _client_frames(buffer):
    """Decode independently of the client and require masked, complete frames."""
    frames = []
    while len(buffer) >= 2:
        first, second = buffer[:2]
        assert first & 0x80 and not first & 0x70
        assert second & 0x80, "the TLS client must mask every outgoing frame"
        size, cursor = second & 0x7f, 2
        extra = 2 if size == 126 else 8 if size == 127 else 0
        if len(buffer) < cursor + extra + 4:
            break
        if extra:
            size = int.from_bytes(buffer[cursor:cursor + extra], "big")
            cursor += extra
        assert size <= 64 * 1024, "stdin must be framed in at most 64 KiB chunks"
        if len(buffer) < cursor + 4 + size:
            break
        mask = buffer[cursor:cursor + 4]
        cursor += 4
        body = bytes(byte ^ mask[index % 4]
                     for index, byte in enumerate(buffer[cursor:cursor + size]))
        frames.append((first & 0x0f, body))
        del buffer[:cursor + size]
    return frames


class _OwnedTLS:
    """Record the actual SSL I/O owner and observed socket backpressure."""

    def __init__(self, sock):
        self.sock = sock
        self.owners = set()
        self.want_write = threading.Event()

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def _io(self, operation, *args):
        self.owners.add(threading.get_ident())
        assert len(self.owners) == 1, "SSL send and recv must have one owner"
        assert not self.sock.getblocking(), "the TLS pump must not block in SSL I/O"
        try:
            return operation(*args)
        except ssl.SSLWantWriteError:
            self.want_write.set()
            raise

    def send(self, data):
        return self._io(self.sock.send, data)

    def recv(self, size):
        return self._io(self.sock.recv, size)


class _CrossWantTLS(_OwnedTLS):
    """Make the rare TLS readiness reversals deterministic over a real socket."""

    def __init__(self, sock):
        super().__init__(sock)
        self.injected = set()

    def _once(self, marker, error):
        self.injected.add(marker)
        code = ssl.SSL_ERROR_WANT_READ if error is ssl.SSLWantReadError else ssl.SSL_ERROR_WANT_WRITE
        raise error(code, "synthetic TLS readiness reversal")

    def send(self, data):
        if "send-wants-read" not in self.injected:
            return self._io(self._once, "send-wants-read", ssl.SSLWantReadError)
        return super().send(data)

    def recv(self, size):
        if "recv-wants-write" not in self.injected:
            return self._io(self._once, "recv-wants-write", ssl.SSLWantWriteError)
        return super().recv(size)


class _Worker:
    def __init__(self, operation, *args):
        self.value = self.error = None

        def run():
            try:
                self.value = operation(*args)
            except BaseException as exc:
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self, timeout=10):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "synthetic TLS stream did not finish promptly"
        if self.error is not None:
            raise self.error
        return self.value


class _Stream:
    def __init__(self, client, peer, local_client):
        self.socket, self.peer = _OwnedTLS(client), peer
        self.local_client = local_client
        self.input_read, self.input_write = os.pipe()
        self.output_read, self.output_write = os.pipe()
        os.set_blocking(self.input_write, False)
        os.set_blocking(self.output_read, False)
        self.stop = threading.Event()
        self.workers = []

    def worker(self, operation, *args):
        worker = _Worker(operation, *args)
        self.workers.append(worker)
        return worker

    def start(self, initial=b"", *, input_blocking=True, output_blocking=True):
        os.set_blocking(self.input_read, input_blocking)
        os.set_blocking(self.output_write, output_blocking)
        self.original_flags = (input_blocking, output_blocking)
        pump = self.local_client["_proxy_stream"]
        return self.worker(lambda: pump(self.socket, initial, input_fd=self.input_read,
                                       output_fd=self.output_write))

    def assert_released(self):
        assert self.socket.fileno() == -1, "the pump must close its TLS socket"
        assert (os.get_blocking(self.input_read), os.get_blocking(self.output_write)) \
            == self.original_flags

    def close_input(self):
        os.close(self.input_write)
        self.input_write = None

    def close_output(self):
        os.close(self.output_read)
        self.output_read = None

    def close(self):
        self.stop.set()
        for fd in (self.input_write, self.output_read, self.input_read, self.output_write):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self.socket.close()
        self.peer.close()
        for worker in self.workers:
            worker.thread.join(1)


@pytest.fixture
def tls_stream(tmp_path):
    """Generate test-only credentials; no provider or user credential is consulted."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to generate the synthetic localhost certificate")
    certificate, key = tmp_path / "localhost.pem", tmp_path / "localhost.key"
    configuration = tmp_path / "openssl.cnf"
    configuration.write_text(
        "[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=extensions\n"
        "[subject]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
        "extendedKeyUsage=serverAuth\n")
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-config", str(configuration), "-keyout", str(key), "-out", str(certificate)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(certificate), str(key))
    client_context = ssl.create_default_context(cafile=str(certificate))
    assert client_context.check_hostname and client_context.verify_mode == ssl.CERT_REQUIRED
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def accept():
            raw, _ = listener.accept()
            raw.settimeout(5)
            return server_context.wrap_socket(raw, server_side=True)

        accepted = _Worker(accept)
        raw = socket.create_connection(listener.getsockname(), timeout=5)
        client = client_context.wrap_socket(raw, server_hostname="localhost")
        peer = accepted.join()
    client.settimeout(None)
    peer.settimeout(None)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    local_client = runpy.run_path(str(Path(__file__).parents[1] / "laptop" / "ccfleet"))
    stream = _Stream(client, peer, local_client)
    try:
        yield stream
    finally:
        stream.close()


def _feed(stream, payload, stopped=None):
    cursor = 0
    while cursor < len(payload) and not stream.stop.is_set():
        if stopped is not None and stopped.is_set():
            break
        try:
            cursor += os.write(stream.input_write, payload[cursor:cursor + 16381])
        except BlockingIOError:
            select.select([], [stream.input_write], [], 0.02)
    # Keep stdin open after the known bytes; its EOF means SSH cancellation.
    return cursor


def _collect(stream, size, delay=0):
    stream.stop.wait(delay)
    answer = bytearray()
    deadline = time.monotonic() + 10
    while len(answer) < size and not stream.stop.is_set():
        assert time.monotonic() < deadline, "SSH output stalled"
        try:
            chunk = os.read(stream.output_read, min(1999, size - len(answer)))
        except BlockingIOError:
            select.select([stream.output_read], [], [], 0.02)
            continue
        assert chunk, "SSH output closed before all expected bytes"
        answer.extend(chunk)
    return bytes(answer)


def _broker(stream, wire, expected_input, ping=None, *, delayed=False, eof=False):
    """One TLS owner concurrently produces and consumes fragmented wire bytes."""
    stream.peer.setblocking(False)
    received, buffered, pongs = bytearray(), bytearray(), []
    cursor, ending = 0, False
    deadline = time.monotonic() + 10
    # Let the client fill its tiny socket buffer before the peer begins reading.
    if not delayed:
        stream.stop.wait(0.15)
    while not stream.stop.is_set():
        assert time.monotonic() < deadline, "synthetic TLS broker stalled"
        progress = False
        try:
            chunk = stream.peer.recv(8191)
            assert chunk, "TLS client closed before the broker finished"
            buffered.extend(chunk)
            for opcode, payload in _client_frames(buffered):
                if opcode == 2:
                    received.extend(payload)
                else:
                    assert opcode == 10
                    pongs.append(payload)
            progress = True
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass
        ready = len(received) == expected_input
        assert len(received) <= expected_input
        if delayed and ready:
            stream.stop.wait(0.1)
            delayed = False
        if not delayed and cursor < len(wire):
            try:
                cursor += stream.peer.send(memoryview(wire)[cursor:cursor + 8191])
                progress = True
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                pass
        if ready and cursor == len(wire) and (ping is None or pongs == [ping]):
            if eof or ending:
                stream.peer.close()
                return bytes(received), pongs
            wire += _server_frame(b"", 8)
            ending = True
        if not progress:
            select.select([stream.peer], [stream.peer] if cursor < len(wire) else [], [], 0.02)


def test_trusted_tls_is_byte_exact_full_duplex_with_pipe_and_socket_backpressure(
        tls_stream, monkeypatch):
    stream = tls_stream
    source = _payload(4 * 1024 * 1024 + 333, 3)
    response = _payload(4 * 1024 * 1024 + 517, 71)
    initial_output = b"SSH bytes already buffered after HTTP Upgrade\x00\xff"
    first = _server_frame(response[:1024 * 1024])
    wire = first[5:] + _server_frame(b"harmless unsolicited pong", 10)
    ping = b"synthetic keepalive\x00\xff"
    wire += _server_frame(ping, 9)
    wire += b"".join(_server_frame(response[cursor:cursor + 1024 * 1024])
                     for cursor in range(1024 * 1024, len(response), 1024 * 1024))
    real_write, short_writes = os.write, []

    def record_write(fd, data):
        written = real_write(fd, data)
        if fd == stream.output_write and written < len(data):
            short_writes.append(written)
        return written

    monkeypatch.setattr(os, "write", record_write)
    pump = stream.start(_server_frame(initial_output) + first[:5])
    broker = stream.worker(_broker, stream, wire, len(source), ping)
    writer = stream.worker(_feed, stream, source)
    reader = stream.worker(_collect, stream, len(initial_output) + len(response), 0.2)
    assert writer.join() == len(source)
    assert broker.join() == (source, [ping])
    assert reader.join() == initial_output + response
    assert pump.join() == 0
    assert short_writes, "the real output pipe must exert partial-write backpressure"
    assert stream.socket.want_write.is_set(), "the real TLS socket must exert send backpressure"
    assert len(stream.socket.owners) == 1
    stream.assert_released()


@pytest.mark.parametrize("eof", [False, True], ids=["websocket-close", "tls-eof"])
def test_delayed_reply_flushes_complete_frames_before_peer_close(tls_stream, eof):
    stream = tls_stream
    source = _payload(256 * 1024 + 11, 19)
    # Exercise the full accepted frame size and a subsequent complete frame.
    response = _payload(2 * 1024 * 1024, 97) + b"last complete SSH bytes\x00\xff"
    wire = _server_frame(response[:2 * 1024 * 1024]) + _server_frame(response[2 * 1024 * 1024:])
    pump = stream.start(input_blocking=False, output_blocking=True)
    broker = stream.worker(lambda: _broker(stream, wire, len(source), delayed=True, eof=eof))
    writer = stream.worker(_feed, stream, source)
    reader = stream.worker(_collect, stream, len(response), 0.2)
    assert writer.join() == len(source)
    assert broker.join() == (source, [])
    assert reader.join() == response
    assert pump.join() == 0
    stream.assert_released()


@pytest.mark.parametrize("eof", [False, True], ids=["websocket-close", "tls-eof"])
def test_complete_maximum_frame_waiting_behind_output_is_not_mistaken_for_truncation(
        tls_stream, monkeypatch, eof):
    stream = tls_stream
    # Fill the actual output pipe so even a small first frame remains pending.
    # The complete following MAX_FRAME cannot fit until that first frame drains.
    # Socket send backpressure is covered separately; let this EOF regression
    # reach the complete frame without depending on tiny-buffer TCP throughput.
    stream.peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
    os.set_blocking(stream.output_write, False)
    existing = bytearray()
    while True:
        try:
            count = os.write(stream.output_write, b"synthetic pipe backlog" * 400)
        except BlockingIOError:
            break
        existing.extend((b"synthetic pipe backlog" * 400)[:count])
    first, tail = _payload(100, 13), _payload(2 * 1024 * 1024, 43)
    wire = _server_frame(tail)
    arrived = threading.Event()
    received = [0]
    real_receive = stream.socket.recv

    def observe_receive(size):
        data = real_receive(size)
        received[0] += len(data)
        if received[0] >= len(wire):
            arrived.set()
        return data

    monkeypatch.setattr(stream.socket, "recv", observe_receive)
    pump = stream.start(_server_frame(first))
    broker = stream.worker(lambda: _broker(stream, wire, 0, eof=eof))
    assert arrived.wait(9), (
        f"maximum frame arrival {received[0]}/{len(wire)}; "
        f"pump error={type(pump.error).__name__}, broker error={type(broker.error).__name__}")
    assert broker.join() == (b"", [])
    pump.thread.join(0.2)
    assert pump.thread.is_alive(), "complete output was dropped before the SSH pipe drained"
    expected = bytes(existing) + first + tail
    reader = stream.worker(_collect, stream, len(expected))
    assert reader.join() == expected
    assert pump.join() == 0
    stream.assert_released()


def test_closed_stdout_cancels_an_idle_tls_peer_with_fixed_error(tls_stream):
    stream = tls_stream
    pump = stream.start(input_blocking=True, output_blocking=False)
    stream.close_output()
    with pytest.raises(stream.local_client["CliError"]) as failure:
        pump.join(2)
    assert str(failure.value) == "SSH input closed during the broker stream"
    stream.assert_released()


def test_tls_wants_can_reverse_the_send_and_receive_readiness(tls_stream):
    stream = tls_stream
    stream.socket = _CrossWantTLS(stream.socket.sock)
    source, response, ping = _payload(32003, 29), _payload(21013, 107), b"retry keepalive"
    wire = _server_frame(response) + _server_frame(ping, 9)
    pump = stream.start(input_blocking=False, output_blocking=False)
    broker = stream.worker(_broker, stream, wire, len(source), ping)
    writer = stream.worker(_feed, stream, source)
    reader = stream.worker(_collect, stream, len(response))
    assert writer.join() == len(source)
    assert broker.join() == (source, [ping])
    assert reader.join() == response
    assert pump.join() == 0
    assert stream.socket.injected == {"send-wants-read", "recv-wants-write"}
    assert len(stream.socket.owners) == 1
    stream.assert_released()


@pytest.mark.parametrize("stalled_sender", [False, True], ids=["idle", "blocked-tls-send"])
def test_stdin_hup_cancels_even_when_tls_peer_is_idle(tls_stream, stalled_sender):
    stream = tls_stream
    pump = stream.start(input_blocking=False, output_blocking=True)
    if stalled_sender:
        stop_writing = threading.Event()
        source = _payload(8 * 1024 * 1024, 41)
        writer = stream.worker(_feed, stream, source, stop_writing)
        assert stream.socket.want_write.wait(2), "TLS send did not encounter expected backpressure"
        stop_writing.set()
        assert writer.join(2) < len(source)
    stream.close_input()
    assert pump.join(2) == 0
    stream.assert_released()
