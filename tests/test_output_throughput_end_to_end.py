"""Synthetic HTTP -> stream observer -> persisted aggregate -> anonymous Status."""

import json
import threading
import time

import pytest

from tests.test_admin_host import Browser
from tests.test_gateway_end_to_end import request, wait_for
from tests.test_gateway_end_to_end import scenario as scenario  # noqa: F401

START = (b'event: message_start\ndata: {"type":"message_start","message":'
         b'{"type":"message","model":"PRIVATE_MODEL","role":"assistant",'
         b'"content":[],"usage":{"input_tokens":100,"output_tokens":1}}}\n\n')
TEXT = (b'event: content_block_delta\ndata: {"type":"content_block_delta",'
        b'"index":0,"delta":{"type":"text_delta","text":"PRIVATE_REPLY"}}\n\n')
END = (b'event: message_delta\ndata: {"type":"message_delta",'
       b'"usage":{"output_tokens":500}}\n\nevent: message_stop\n'
       b'data: {"type":"message_stop"}\n\n')


class DelayedChunks:
    """Advance the injected gateway clock instead of waiting or calling a model."""

    def __init__(self, clock):
        self.clock = clock

    def __iter__(self):
        yield START
        self.clock.offset += 5
        yield TEXT
        self.clock.offset += 5
        yield END


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages?beta=true"])
def test_new_successful_streams_populate_public_tps_without_model_calls_from_status(
        scenario, path, monkeypatch):  # noqa: F811
    committed = threading.Event()
    actual_writer = scenario.store.try_record_gateway_throughput

    def observe_commit(*args, **kwargs):
        result = actual_writer(*args, **kwargs)
        if result is True:
            committed.set()
        return result

    monkeypatch.setattr(scenario.store, 'try_record_gateway_throughput', observe_commit)
    for _ in range(20):
        committed.clear()
        scenario.pool.plans[scenario.own[0]].append({'chunks': DelayedChunks(scenario.clock)})
        status, body, _headers = request(scenario, path=path)
        assert status == 200 and body == START + TEXT + END
        # This is a delivery-to-persisted-aggregate check, not a contention test.
        # The real recorder is deliberately best-effort: a later request's
        # admission reads can exhaust its three nonblocking storage attempts.
        # Wait for this real commit before launching the next synthetic stream.
        assert committed.wait(3), 'completed stream observation was not committed'
    wait_for(lambda: scenario.store.gateway_throughput_summary(time.time())['samples'] == 20)
    samples = scenario.store.gateway_throughput_summary(time.time())
    assert samples['output_tokens'] == 10_000
    assert 200_000 <= samples['stream_ms'] < 210_000
    before = scenario.store._conn.execute('SELECT * FROM gateway_throughput_days').fetchall()
    attempted = len(scenario.pool.requests)
    response = Browser(scenario.server.server_address[1], 'fleet.example.com').call('GET', '/status')
    assert response.status == 200
    assert 'Streamed output rate' in response.body and '20 streamed reply samples' in response.body
    assert f"{samples['output_tokens'] / (samples['stream_ms'] / 1000):,.2f}" in response.body
    for secret in ('PRIVATE_MODEL', 'PRIVATE_REPLY', 'owner-a@example.com', scenario.key['key_token']):
        assert secret not in response.body
    after = scenario.store._conn.execute('SELECT * FROM gateway_throughput_days').fetchall()
    assert before == after
    assert len(scenario.pool.requests) == attempted == 20
    assert 'PRIVATE_' not in json.dumps([tuple(row) for row in after])
