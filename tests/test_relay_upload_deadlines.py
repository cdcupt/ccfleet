"""Real-pipe slot intake deadlines with synthetic credentials and provider replies.

No model service or network is contacted. Each child runs the real relay main so
signal deadlines cannot interfere with pytest or another test's threads.
"""

from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PEER = r'''
import json, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from ccfleet_agent import local_relay as relay
state_path = Path(sys.argv[2])
settings = json.loads(sys.argv[3])
for name, value in settings.items():
    setattr(relay, name, value)
state = {"request_calls": 0, "connections": 0}
started = time.monotonic()
def save():
    state_path.write_text(json.dumps({**state, "elapsed": time.monotonic() - started}))
class Upstream:
    status = 200
    length = None
    sock = None
    def __init__(self):
        self.sent = False
    def connect(self):
        state["connections"] += 1
    def request(self, method, path, **kwargs):
        assert method == "POST" and path == "/v1/messages"
        state["request_calls"] += 1
    def getresponse(self):
        return self
    def getheaders(self):
        return [("Content-Type", "application/json")]
    def read1(self, size):
        if self.sent:
            return b""
        self.sent = True
        return b'{"ok":true}'
    def close(self):
        pass
relay.bound_account = lambda home: "synthetic-account"
relay.credential = lambda *args, **kwargs: ("synthetic-unused", time.time() + 3600)
relay._record_metrics = lambda *args, **kwargs: None
relay.pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir="/synthetic-slot")
serve = relay.serve_one
def synthetic_serve(input_, output, home, **kwargs):
    return serve(input_, output, home, policy=lambda: None,
                 connect=Upstream, **kwargs)
relay.serve_one = synthetic_serve
save()
try:
    code = relay.main([sys.argv[4]])
    state["exit_code"] = code
    raise SystemExit(code)
finally:
    save()
'''


def _frame(value):
    raw = json.dumps(value).encode()
    return struct.pack("!I", len(raw)) + raw


def _request_parts(body):
    return _frame({"version": 2, "operation": "request", "method": "POST",
                   "path": "/v1/messages", "headers": {"content-type": "application/json"},
                   "body_size": len(body)}), body


def _read_exact(stream, size, timeout=3):
    deadline = time.monotonic() + timeout
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        assert remaining > 0 and select.select([stream], [], [], remaining)[0], \
            "relay did not return the expected framed response"
        part = os.read(stream.fileno(), size - len(result))
        assert part, "relay ended before completing its framed response"
        result.extend(part)
    return bytes(result)


def _response(stream):
    meta = json.loads(_read_exact(stream, struct.unpack("!I", _read_exact(stream, 4))[0]))
    body = bytearray()
    while True:
        size = struct.unpack("!I", _read_exact(stream, 4))[0]
        if not size:
            return meta, bytes(body)
        body.extend(_read_exact(stream, size))


@pytest.fixture
def peer(tmp_path):
    processes = []

    def start(settings, *, gateway=False):
        state_path = tmp_path / ("state-" + str(len(processes)) + ".json")
        proc = subprocess.Popen(
            [sys.executable, "-c", _PEER, str(Path(__file__).resolve().parents[1]),
             str(state_path), json.dumps(settings),
             "--gateway-protocol-v1" if gateway else "--protocol-v2"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0)
        processes.append(proc)
        deadline = time.monotonic() + 5
        while not state_path.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.monotonic() < deadline, "synthetic relay did not start"
            time.sleep(0.005)
        return proc, state_path

    yield start
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def _finish(proc, state_path, expected_status):
    proc.wait(timeout=4)
    metadata, body = _response(proc.stdout)
    state = json.loads(state_path.read_text())
    assert metadata["status"] == expected_status
    assert proc.returncode == (0 if expected_status == 200 else 2), proc.stderr.read().decode()
    return state, body


def _drip(proc, data, *, delay=0.04, maximum_seconds=3):
    """Write less than a BufferedReader chunk while keeping every gap short."""
    deadline = time.monotonic() + maximum_seconds
    for byte in data:
        assert time.monotonic() < deadline, "relay did not enforce its bounded intake deadline"
        try:
            proc.stdin.write(bytes((byte,)))
        except BrokenPipeError:
            return
        time.sleep(delay)


def test_active_partial_reads_can_upload_beyond_initial_input_deadline(peer):
    # Bytes arrive steadily, but fewer than one full body read is available at
    # any moment. BufferedReader.read() would hide this progress until EOF/full
    # length; the relay must use partial reads to refresh its idle deadline.
    proc, state_path = peer({"INPUT_TIMEOUT": 0.30, "UPLOAD_TIMEOUT": 2,
                             "REQUEST_TIMEOUT": 3})
    metadata, body = _request_parts(b'{"messages":[]}')
    proc.stdin.write(metadata)
    _drip(proc, body)
    state, output = _finish(proc, state_path, 200)
    assert state["elapsed"] > 0.30
    assert state["connections"] == state["request_calls"] == 1
    assert output == b'{"ok":true}'


@pytest.mark.parametrize("phase", ["metadata", "body"])
def test_idle_intake_expires_without_contacting_provider(peer, phase):
    proc, state_path = peer({"INPUT_TIMEOUT": 0.20, "UPLOAD_TIMEOUT": 2,
                             "REQUEST_TIMEOUT": 3})
    if phase == "metadata":
        proc.stdin.write(struct.pack("!I", 100) + b"{")
    else:
        metadata, body = _request_parts(b'{"messages":[]}')
        proc.stdin.write(metadata + body[:1])
    state, output = _finish(proc, state_path, 504)
    assert state["connections"] == state["request_calls"] == 0
    assert 0.10 < state["elapsed"] < 2
    assert json.loads(output)["type"] == "error"


@pytest.mark.parametrize("upload_timeout,request_timeout", [(0.65, 3), (3, 0.65)])
def test_progress_cannot_extend_upload_or_process_hard_deadline(peer,
                                                               upload_timeout,
                                                               request_timeout):
    proc, state_path = peer({"INPUT_TIMEOUT": 0.25, "UPLOAD_TIMEOUT": upload_timeout,
                             "REQUEST_TIMEOUT": request_timeout})
    metadata, body = _request_parts(b'{"messages":[],"padding":"' + b"x" * 200 + b'"}')
    proc.stdin.write(metadata)
    _drip(proc, body, delay=0.04)
    state, output = _finish(proc, state_path, 504)
    assert state["connections"] == state["request_calls"] == 0
    assert 0.50 < state["elapsed"] < 2
    assert json.loads(output)["type"] == "error"


def test_gateway_readiness_then_progressing_upload_keeps_protocol_phases(peer):
    proc, state_path = peer({"INPUT_TIMEOUT": 0.30, "UPLOAD_TIMEOUT": 2,
                             "REQUEST_TIMEOUT": 3}, gateway=True)
    proc.stdin.write(_frame({"version": 2, "operation": "status"}))
    metadata, body = _response(proc.stdout)
    assert metadata["status"] == 200
    assert json.loads(body) == {"ready": True, "protocol": 2}
    time.sleep(0.10)
    metadata, body = _request_parts(b'{"messages":[]}')
    proc.stdin.write(metadata)
    _drip(proc, body)
    state, output = _finish(proc, state_path, 200)
    assert state["elapsed"] > 0.30
    assert state["connections"] == state["request_calls"] == 1
    assert output == b'{"ok":true}'


def test_gateway_waiting_for_request_after_readiness_is_still_bounded(peer):
    proc, state_path = peer({"INPUT_TIMEOUT": 0.20, "UPLOAD_TIMEOUT": 2,
                             "REQUEST_TIMEOUT": 3}, gateway=True)
    proc.stdin.write(_frame({"version": 2, "operation": "status"}))
    metadata, body = _response(proc.stdout)
    assert metadata["status"] == 200 and json.loads(body)["ready"] is True
    state, output = _finish(proc, state_path, 504)
    assert state["connections"] == state["request_calls"] == 0
    assert json.loads(output)["type"] == "error"
