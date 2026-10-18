"""Real newline MCP clients and durable at-most-once starts; no provider calls."""

from __future__ import annotations

import hashlib
import io
import json
import os
import selectors
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ccfleet_agent import local_jobs
from ccfleet_agent import task_mcp as mcp
from ccfleet_agent import task_router as router
from tests.test_task_router import eventually, finished
from tests.test_task_router import pilot as pilot


def request(method, params=None, identifier=1):
    return {"jsonrpc": "2.0", "id": identifier, "method": method,
            "params": {} if params is None else params}


def initialize(version="2025-11-25"):
    return request("initialize", {"protocolVersion": version, "capabilities": {},
                                 "clientInfo": {"name": "synthetic-test", "version": "1"}})


def arguments(request_id="synthetic-request-1", prompt="finish", backend="claude"):
    return {"backend": backend, "workspace": "pilot", "prompt": prompt,
            "request_id": request_id}


class Client:
    def __init__(self, config: Path, cwd: Path):
        self.process = subprocess.Popen([sys.executable, "-I", str(Path(mcp.__file__).resolve()),
            "--config", str(config)], cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.identifier = 1

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=True).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(12), "MCP response timed out"
        raw = self.process.stdout.readline()
        assert raw.endswith(b"\n"), raw
        value = json.loads(raw)
        assert value["jsonrpc"] == "2.0"
        return value

    def setup(self, version="2025-11-25"):
        self.send(initialize(version))
        value = self.receive()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return value

    def call(self, name, args=None):
        self.identifier += 1
        self.send(request("tools/call", {"name": name, "arguments": args or {}}, self.identifier))
        value = self.receive()
        assert value["id"] == self.identifier
        return value

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
            raise
        assert self.process.stderr.read() == b""


@pytest.fixture
def clients(pilot):
    created = []

    def make():
        client = Client(pilot["path"], pilot["project"])
        client.setup()
        created.append(client)
        return client

    yield make
    for client in created:
        client.close()


def result(message):
    assert "error" not in message
    assert message["result"]["isError"] is False, message
    return json.loads(message["result"]["content"][0]["text"])


def error(message):
    assert message["result"]["isError"] is True
    return message["result"]["content"][0]["text"]


def launch(client, pilot, args=None):
    value = result(client.call("fleet_task_start", args or arguments()))
    pilot["launched"].append(value["job_id"])
    return value


@pytest.mark.parametrize("backend", ["claude", "devin"])
def test_real_stdio_initialize_discovery_start_status_logs_and_metadata(clients, pilot, backend):
    client = clients()
    client.send(request("tools/list"))
    tools = client.receive()["result"]["tools"]
    assert {item["name"] for item in tools} == {
        "fleet_agents", "fleet_task_start", "fleet_task_status", "fleet_task_logs",
        "fleet_task_stop", "fleet_tasks"}
    assert next(item for item in tools if item["name"] == "fleet_task_start")[
        "inputSchema"]["required"] == ["backend", "workspace", "prompt", "request_id"]
    metadata = result(client.call("fleet_agents"))
    assert metadata["concurrency"] == 1 and metadata["enabled"] is True
    assert metadata["workspaces"] == [{"workspace": "pilot", "operator_trusted": True}]
    assert str(pilot["binary"]) not in json.dumps(metadata)
    assert "credentials" not in json.dumps(metadata)
    value = launch(client, pilot, arguments(backend=backend))
    job_id = value["job_id"]
    assert local_jobs.JOB_RE.fullmatch(job_id)
    assert finished(pilot, job_id)["state"] == "completed"
    status = result(client.call("fleet_task_status", {"job_id": job_id}))
    assert status["state"] == "completed" and status["backend"] == backend
    assert not {"device_id", "slot_id", "project", "prompt", "socket", "instance"} & set(status)
    tasks = result(client.call("fleet_tasks"))["tasks"]
    assert len(tasks) == 1 and tasks[0]["job_id"] == job_id
    assert result(client.call("fleet_task_logs", {"job_id": job_id}))["text"] == (
        "synthetic task stdout\n")
    assert result(client.call("fleet_task_logs", {"job_id": job_id,
        "stream": "stderr", "bytes": 9}))["bytes"] == 9
    assert not list((pilot["root"] / "mcp-requests").glob(".prompt-*"))
    reservations = list((pilot["root"] / "mcp-requests").glob("*.json"))
    assert len(reservations) == 1 and (reservations[0].stat().st_mode & 0o777) == 0o600
    assert json.loads(reservations[0].read_text())["state"] == "submitted"
    assert "finish" not in reservations[0].read_text()


def test_repeated_request_after_restart_returns_same_task_without_native_replay(clients, pilot):
    first = launch(clients(), pilot)
    finished(pilot, first["job_id"])
    # Even a missing native executable must not turn a saved submission into a retry.
    pilot["binary"].unlink()
    repeated = result(clients().call("fleet_task_start", arguments()))
    assert first == repeated
    assert len((pilot["project"] / ".launches").read_text().splitlines()) == 1


@pytest.mark.parametrize("change", ["prompt", "backend", "workspace", "configuration"])
def test_request_id_cannot_be_reused_for_different_parameters(clients, pilot, change):
    client = clients()
    launched = launch(client, pilot)
    finished(pilot, launched["job_id"])
    args = arguments()
    if change == "configuration":
        pilot["value"]["timeout_s"] = 11
        pilot["configure"]()
    else:
        args[change] = {"prompt": "other", "backend": "devin", "workspace": "other"}[change]
    assert "different task parameters" in error(client.call("fleet_task_start", args))
    assert len((pilot["project"] / ".launches").read_text().splitlines()) == 1


def test_concurrent_stdio_clients_cannot_launch_the_same_request_twice(clients, pilot):
    first, second = clients(), clients()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda client: client.call("fleet_task_start", arguments()),
                                (first, second)))
    successful = [result(reply) for reply in replies if not reply["result"]["isError"]]
    assert successful
    job_id = successful[0]["job_id"]
    pilot["launched"].append(job_id)
    assert all(item["job_id"] == job_id for item in successful)
    for reply in replies:
        if reply["result"]["isError"]:
            assert "submission is in progress" in error(reply)
    assert result(second.call("fleet_task_start", arguments()))["job_id"] == job_id
    finished(pilot, job_id)
    assert len((pilot["project"] / ".launches").read_text().splitlines()) == 1


def test_single_job_capacity_failure_reserves_uncertain_key_and_never_retries(clients, pilot):
    client = clients()
    first = launch(client, pilot, arguments("first", "sleep"))
    eventually((pilot["project"] / ".running").exists)
    args = arguments("second")
    assert "unconfirmed" in error(client.call("fleet_task_start", args))
    stopped = result(client.call("fleet_task_stop", {"job_id": first["job_id"]}))
    assert stopped["cleanup_confirmed"] is True and stopped["state"] == "stopped"
    assert "unconfirmed" in error(client.call("fleet_task_start", args))
    assert len((pilot["project"] / ".launches").read_text().splitlines()) == 1


def test_uncertain_launch_exception_and_saved_starting_record_never_resubmit(pilot, monkeypatch):
    attempts = []

    def fail(*args):
        attempts.append(args)
        raise OSError("PRIVATE_PROVIDER_ERROR never belongs on stdio")

    monkeypatch.setattr(router, "start", fail)
    config = router.load_config(pilot["path"])
    for _ in range(2):
        with pytest.raises(mcp.ToolError, match="unconfirmed"):
            mcp.start(pilot["path"], config, arguments())
    assert len(attempts) == 1
    assert not list((pilot["root"] / "mcp-requests").glob(".prompt-*"))


def test_existing_starting_reservation_over_stdio_is_never_resubmitted(clients, pilot):
    args = arguments()
    config = router.load_config(pilot["path"])
    name = hashlib.sha256(args["request_id"].encode()).hexdigest() + ".json"
    with mcp._reservations(pilot["root"]) as directory:
        local_jobs._save(directory, name, {"version": 1,
            "digest": mcp._request_digest(config, args), "state": "starting"},
            local_jobs.MAX_STATE_BYTES)
    assert "unconfirmed" in error(clients().call("fleet_task_start", args))
    assert not (pilot["project"] / ".launches").exists()


@pytest.mark.parametrize("changes", [
    {"request_id": "../invalid"}, {"request_id": ""}, {"request_id": "x" * 129},
    {"request_id": 1}, {"prompt": ""}, {"prompt": "  "}, {"prompt": "nul\0byte"},
    {"prompt": "x" * 65537}, {"prompt": "中" * 22000}, {"backend": "automatic"},
    {"workspace": "../project"}, {"model": "other"}, {"flags": ["--cloud"]},
    {"binary": "/tmp/unsafe"},
])
def test_invalid_start_arguments_are_protocol_errors_and_create_no_state(clients, pilot, changes):
    args = {**arguments(), **changes}
    reply = clients().call("fleet_task_start", args)
    assert reply["error"]["code"] == -32602
    assert not pilot["root"].exists()


@pytest.mark.parametrize("args", [{}, {"job_id": "../invalid"}, {"job_id": "a" * 31},
    {"job_id": "a" * 32, "bytes": 65537}, {"job_id": "a" * 32, "bytes": True},
    {"job_id": "a" * 32, "stream": "private-file"},
    {"job_id": "a" * 32, "path": "/tmp/secret"}])
def test_invalid_log_arguments_do_not_read_arbitrary_paths(clients, args):
    assert clients().call("fleet_task_logs", args)["error"]["code"] == -32602


def test_logs_default_to_8192_bytes_and_are_bounded_to_65536(pilot, monkeypatch):
    seen = []

    def logs(config, job_id, *, stream, max_bytes):
        seen.append((stream, max_bytes))
        return {"text": "x" * max_bytes}

    monkeypatch.setattr(router, "logs", logs)
    server = mcp.Server(pilot["path"])
    server.handle(initialize())
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    for args in ({"job_id": "a" * 32}, {"job_id": "a" * 32, "bytes": 65536}):
        assert "error" not in server.handle(request("tools/call", {
            "name": "fleet_task_logs", "arguments": args}))
    assert seen == [("stdout", 8192), ("stdout", 65536)]


def test_tool_failures_are_minimal_and_do_not_echo_provider_exceptions(clients, pilot):
    client = clients()
    reply = client.call("fleet_task_status", {"job_id": "a" * 32})
    assert "Task operation failed" in error(reply)
    assert str(pilot["root"]) not in json.dumps(reply)
    pilot["path"].write_text("PRIVATE_INVALID_CONFIGURATION")
    assert "PRIVATE" not in error(client.call("fleet_agents"))


def test_unexpected_tool_exception_is_an_error_result_without_private_text(pilot, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_PROVIDER_DETAIL")

    monkeypatch.setattr(router, "logs", fail)
    server = mcp.Server(pilot["path"])
    server.handle(initialize())
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    reply = server.handle(request("tools/call", {"name": "fleet_task_logs",
        "arguments": {"job_id": "a" * 32}}))
    assert "Task operation failed" in error(reply)
    assert "PRIVATE" not in json.dumps(reply)


def test_management_and_stop_work_after_native_binary_disappears(clients, pilot):
    client = clients()
    launched = launch(client, pilot, arguments(prompt="sleep"))
    eventually((pilot["project"] / ".running").exists)
    pilot["binary"].unlink()
    assert result(client.call("fleet_agents"))["enabled"] is True
    assert result(client.call("fleet_task_status", {"job_id": launched["job_id"]}))["state"] in {
        "running", "stopping", "stopped"}
    assert result(client.call("fleet_task_stop", {"job_id": launched["job_id"]}))[
        "cleanup_confirmed"] is True


def test_notifications_never_submit_tasks_and_protocol_lifecycle_is_explicit(pilot):
    server = mcp.Server(pilot["path"])
    assert server.handle(request("tools/list"))["error"]["code"] == -32002
    assert server.handle(request("ping"))["result"] == {}
    assert server.handle(initialize("unsupported"))["result"]["protocolVersion"] == "2025-11-25"
    assert server.handle(request("tools/list"))["error"]["code"] == -32002
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert server.handle(request("initialize"))["error"]["code"] == -32600
    assert server.handle(request("unknown"))["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {
        "name": "fleet_task_start", "arguments": arguments()}}) is None
    assert not pilot["root"].exists()


@pytest.mark.parametrize("raw,code", [
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/call"}\n', -32700),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1,"x":2}}\n', -32700),
    (b'{invalid}\n', -32700), (b'[]\n', -32600), (b'null\n', -32600),
    (b'\xff\n', -32700), ('{"method":"ping"}\n'.encode("utf-16"), -32700),
    (b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n', -32600),
    (b'{"jsonrpc":"2.0","id":null,"method":"ping"}\n', -32600),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":NaN}\n', -32700),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping"}', -32700),
])
def test_malformed_stdio_messages_return_only_fixed_jsonrpc_errors(pilot, raw, code):
    output = io.BytesIO()
    mcp.serve(pilot["path"], io.BytesIO(raw), output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == code
    assert all(set(reply) == {"jsonrpc", "id", "error"} for reply in replies)
    assert not pilot["root"].exists()


def test_oversize_stdio_frame_stops_without_reading_or_running_following_request(pilot):
    raw = b'x' * (mcp.MAX_REQUEST_BYTES + 1) + b'\n'
    raw += json.dumps(initialize()).encode() + b'\n'
    output = io.BytesIO()
    assert mcp.serve(pilot["path"], io.BytesIO(raw), output) == 2
    replies = output.getvalue().splitlines()
    assert len(replies) == 1 and json.loads(replies[0])["error"]["code"] == -32600
    assert not pilot["root"].exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_unsafe_idempotency_record_cannot_authorize_replay(pilot, monkeypatch, kind):
    config = router.load_config(pilot["path"])
    monkeypatch.setattr(router, "start", lambda *args: {"job_id": "a" * 32})
    mcp.start(pilot["path"], config, arguments())
    record = next((pilot["root"] / "mcp-requests").glob("*.json"))
    if kind == "symlink":
        other = record.with_name("private-record")
        record.rename(other)
        record.symlink_to(other)
    elif kind == "hardlink":
        os.link(record, record.with_name("record-copy"))
    else:
        record.chmod(0o644)
    with pytest.raises((OSError, local_jobs.JobError)):
        mcp.start(pilot["path"], config, arguments())


def test_stdio_disconnect_does_not_cancel_detached_task(clients, pilot):
    client = clients()
    launched = launch(client, pilot, arguments(prompt="sleep"))
    eventually((pilot["project"] / ".running").exists)
    client.process.stdin.close()
    client.process.wait(timeout=3)
    assert local_jobs.status(pilot["root"], launched["job_id"])["state"] == "running"
    # Client fixture closes the stream again safely; stop remains explicit.
    assert local_jobs.stop(pilot["root"], launched["job_id"])["cleanup_confirmed"] is True
