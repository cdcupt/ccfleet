"""Bounded trial transport/ledger clocks; no sockets or native model calls."""

import io
import json
from types import SimpleNamespace

import pytest

from ccfleet_agent import devin_mcp as mcp
from ccfleetd import devin_trial as ledger
from ccfleetd import trial_worker_transport as transport
from ccfleetd.gateway_transport import GatewayTransportError
from tests.test_devin_mcp import INITIALIZED, LIST, PROCESS, TimedInput, call, initialize
from tests.test_devin_mcp import private as private
from tests.test_devin_trial_runtime import receipt


def test_mcp_startup_does_not_consume_later_prompt_handoff_time(private):
    path, _, _, bind, _, _ = private
    bind()
    now, observed = [0.0], []
    arguments = {"command": "git log --since='7 days ago' --oneline"}

    def inspect_ready(instant):
        if instant == 130:
            observed.append(mcp.read_lifecycle(path))

    # Setup/handshake finish at20s. A tool selected110s into the allowed120s
    # prompt arrives after the old shim's120s-from-start deadline.
    incoming = TimedInput(now, [
        (5, initialize()), (15, INITIALIZED), (20, LIST),
        (130, call("Bash", arguments)), (131, None),
    ], inspect_ready)
    output = io.BytesIO()
    assert mcp.serve(path, incoming, output, clock=lambda: now[0],
                     identity=lambda: dict(PROCESS)) == 1
    assert observed[0]["ready"] and observed[0]["tools_list_count"] == 1
    record = json.loads((path / "call.json").read_text())
    assert record["frontend_tool_use"]["name"] == "Bash"
    assert record["frontend_tool_use"]["input"] == arguments
    assert record["context"]["session_id"] == "native-owned"
    assert [json.loads(line)["id"] for line in output.getvalue().splitlines()] == [1, 2]


def test_prepared_transport_uses_one155_second_deadline_through_terminal_framing(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    attempt = transport.TrialAttempt({}, object())
    attempt.ready = True
    attempt.process = SimpleNamespace(stdout=SimpleNamespace())
    sent, finish = [], []
    def authorized():
        return True

    def send(headers, body, *, before_model_send):
        assert before_model_send is authorized
        sent.append((headers, body))

    def chunks():
        now[0] += 154.5
        yield json.dumps(receipt()).encode()

    monkeypatch.setattr(attempt, "_send_request", send)
    monkeypatch.setattr(attempt, "metadata", lambda: {"status": 200})
    monkeypatch.setattr(attempt, "chunks", chunks)
    monkeypatch.setattr(attempt, "finish", lambda *, timeout: finish.append(timeout))
    assert attempt.perform(b"synthetic request", authorized) == receipt()
    assert sent == [({}, b"synthetic request")]
    assert attempt.process.stdout.deadline == 1155
    assert attempt.process.stdout.authorized is authorized
    assert finish == [0.5]
    assert transport.TURN_SECONDS + 2 < ledger.MAX_STEP_S < ledger.MAX_RUNTIME_S == 180


def test_transport_still_refuses_reads_at_its_absolute_deadline(monkeypatch):
    monkeypatch.setattr(transport.time, "monotonic", lambda: 1155.0)
    reader = transport._Reader(object(), 1155.0, lambda: True)
    with pytest.raises(GatewayTransportError) as error:
        reader.read(1)
    assert error.value.status == 504
