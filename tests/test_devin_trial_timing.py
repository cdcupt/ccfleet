"""Bounded trial transport/ledger clocks; no sockets or native model calls."""

import json
from types import SimpleNamespace

import pytest

from ccfleetd import devin_trial as ledger
from ccfleetd import trial_worker_transport as transport
from ccfleetd.gateway_transport import GatewayTransportError
from tests.test_devin_trial_runtime import receipt


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
