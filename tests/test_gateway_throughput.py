"""Synthetic completed-stream observations never change the model response."""
from __future__ import annotations

import io
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleet_agent import relay_metrics
from ccfleetd import gateway, throughput
from ccfleetd.gateway_transport import GatewayTransportError

NOW = 2_000_000_123.9


def event(kind, data):
    return f"event: {kind}\ndata: ".encode() + json.dumps(data).encode() + b"\n\n"


def stream(tokens=50):
    return [
        event("message_start", {"type": "message_start", "message": {
            "id": "PRIVATE_MESSAGE", "model": "PRIVATE_MODEL", "content": [],
            "usage": {"input_tokens": 12, "output_tokens": 0}}}),
        event("content_block_delta", {"type": "content_block_delta", "delta": {
            "type": "text_delta", "text": "PRIVATE_REPLY /private/path"}}),
        event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                "usage": {"output_tokens": tokens}})
        + event("message_stop", {"type": "message_stop"}),
    ]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def observe(parts, stamps, media_type="text/event-stream"):
    clock = Clock()
    observer = throughput.StreamObserver(media_type, clock=clock)
    for part, stamp in zip(parts, stamps):
        clock.now = stamp
        observer.feed(part)
    return observer, observer.finish()


@pytest.mark.parametrize("tokens", [0, 50, relay_metrics.MAX_USAGE_TOKENS])
def test_complete_sse_observes_final_cumulative_output_and_body_span_only(tokens):
    observer, sample = observe(stream(tokens), [30.0, 30.4, 31.0])
    assert sample == throughput.Sample(tokens, 1000.0)
    assert observer.collector is None and observer.first is observer.last is None
    assert set(vars(sample)) == {"output_tokens", "stream_ms"}
    assert "PRIVATE_" not in repr(sample)


@pytest.mark.parametrize("parts,stamps", [
    ([b"".join(stream())], [0]),
    (stream(), [0, 0.03, 0.09]),
    (stream(), [0, 1, 3601]),
    (stream(), [1, 0.5, 2]),
    (stream(), [0, float("nan"), 2]),
    (stream(), [0, float("inf"), 2]),
    (stream(), [0, True, 2]),
    (stream()[:-1], [0, 1]),
    (stream()[:2] + [event("error", {"type": "error", "error": "PRIVATE_ERROR"})], [0, 1, 2]),
])
def test_buffered_short_invalid_clock_or_incomplete_stream_has_no_sample(parts, stamps):
    _, sample = observe(parts, stamps)
    assert sample is None


def test_empty_chunks_do_not_supply_samples_and_exact_duration_bounds_are_valid():
    _, sample = observe([stream()[0], b"", b"".join(stream()[1:])], [0, 0.03, 0.1])
    assert sample == throughput.Sample(50, 100.0)
    _, sample = observe(stream(), [0, 1, 3600])
    assert sample == throughput.Sample(50, 3_600_000.0)
    _, sample = observe([b"", b"".join(stream())], [0, 1])
    assert sample is None


@pytest.mark.parametrize("tokens", [True, -1, 1.5, 10**9 + 1, "50", None])
def test_malformed_final_usage_is_unknown(tokens):
    _, sample = observe(stream(tokens), [0, 0.5, 1])
    assert sample is None


def test_json_and_oversized_ambiguous_usage_never_become_stream_samples():
    raw = b'{"type":"message","stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":8}}'
    _, sample = observe([raw[:40], raw[40:]], [0, 1], "application/json")
    assert sample is None
    malformed = b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":1,"output_tokens":50}}\n\n'
    _, sample = observe([stream()[0], malformed, event("message_stop", {"type": "message_stop"})],
                        [0, 0.5, 1])
    assert sample is None
    oversized = b"data: " + b"X" * (relay_metrics.USAGE_EVENT_BYTES + 1) + b"\n\n"
    _, sample = observe([stream()[0], oversized, stream()[-1]], [0, 0.5, 1])
    assert sample is None


class Store:
    def __init__(self):
        self.records = []
        self.recorded = threading.Event()
        self.active = True

    def gateway_targets(self, _key):
        return [{"slot_id": "PRIVATE_SLOT", "claim_generation": "PRIVATE_GENERATION"}]

    def gateway_request_is_active(self, *_args):
        return self.active

    def get_slot(self, identifier):
        return {"id": identifier, "held_by": "PRIVATE_ACCOUNT", "kind": "machine",
                "gateway_claim_id": "PRIVATE_CLAIM", "gateway_generation": "PRIVATE_GENERATION"}

    def paid_slot_through(self, *_args, **_kwargs):
        return None  # This numeric-observer fixture represents a grandfathered rental.

    def slot_subscription_access(self, _account_id, _slot_id, *, claim_id, now):
        return None  # Explicitly no recurring contract in this observer fixture.

    def record_gateway_throughput(self, tokens, stream_ms, *, now):
        self.records.append((tokens, stream_ms, now))
        self.recorded.set()


def wait_for(predicate):
    until = time.monotonic() + 2
    while time.monotonic() < until and not predicate():
        time.sleep(0.005)
    assert predicate()


def test_recorder_is_lazy_and_preserves_only_numeric_day_tokens_and_duration():
    store = Store()
    recorder = throughput.Recorder(store)
    try:
        assert recorder._thread is None and store.records == []
        assert recorder.submit(throughput.Sample(50, 1000.0), now=NOW)
        assert store.recorded.wait(2)
        assert store.records == [(50, 1000.0, int(NOW // 86400) * 86400)]
        assert all(type(value) in (int, float) for value in store.records[0])
        assert NOW not in store.records[0]
    finally:
        assert recorder.close()
    assert recorder.submit(throughput.Sample(50, 1000.0), now=NOW) is False


@pytest.mark.parametrize("sample,now", [
    (throughput.Sample(True, 1000), NOW), (throughput.Sample(1, True), NOW),
    (throughput.Sample(-1, 1000), NOW), (throughput.Sample(1, 99), NOW),
    (throughput.Sample(1, float("inf")), NOW), (throughput.Sample(1, 3600001), NOW),
    (throughput.Sample(1, 1000), float("nan")), (throughput.Sample(1, 1000), True),
    (throughput.Sample(1, 1000), -1),
])
def test_recorder_rejects_invalid_numeric_samples_without_starting_a_worker(sample, now):
    recorder = throughput.Recorder(Store())
    assert recorder.submit(sample, now=now) is False
    assert recorder._thread is None
    assert recorder.close()


def test_queue_is_bounded_nonblocking_and_shutdown_drops_pending_samples():
    entered, release = threading.Event(), threading.Event()
    class BlockingStore(Store):
        def record_gateway_throughput(self, tokens, stream_ms, *, now):
            entered.set()
            assert release.wait(2)
            super().record_gateway_throughput(tokens, stream_ms, now=now)
    store = BlockingStore()
    recorder = throughput.Recorder(store, capacity=2)
    try:
        assert recorder.submit(throughput.Sample(1, 1000), now=NOW)
        assert entered.wait(2)
        assert recorder.submit(throughput.Sample(2, 1000), now=NOW)
        assert recorder.submit(throughput.Sample(3, 1000), now=NOW)
        began = time.monotonic()
        assert recorder.submit(throughput.Sample(4, 1000), now=NOW) is False
        assert time.monotonic() - began < 0.5
        assert recorder._queue.qsize() == 2
        assert recorder.close(timeout=0.01) is False
        assert recorder._queue.qsize() == 0
    finally:
        release.set()
        assert recorder.close()
    assert len(store.records) == 1


def test_storage_failures_are_silent_and_do_not_kill_the_worker(caplog):
    class FailingStore(Store):
        def record_gateway_throughput(self, tokens, stream_ms, *, now):
            if tokens == 1:
                self.recorded.set()
                raise ValueError("PRIVATE_BODY_AND_KEY")
            super().record_gateway_throughput(tokens, stream_ms, now=now)
    store = FailingStore()
    recorder = throughput.Recorder(store)
    try:
        assert recorder.submit(throughput.Sample(1, 1000), now=NOW)
        assert store.recorded.wait(2)
        assert recorder.submit(throughput.Sample(2, 1000), now=NOW)
        wait_for(lambda: len(store.records) == 1)
        assert store.records[0][0] == 2
    finally:
        assert recorder.close()
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize('busy_attempts', [0, 1, 2, 3])
def test_recorder_uses_nonblocking_store_writer_and_retries_only_known_not_written(busy_attempts):
    class TryStore(Store):
        calls = 0
        def try_record_gateway_throughput(self, tokens, stream_ms, *, now):
            self.calls += 1
            if self.calls <= busy_attempts:
                return False
            super().record_gateway_throughput(tokens, stream_ms, now=now)
            return True
        def record_gateway_throughput(self, *_args, **_kwargs):
            raise AssertionError('blocking fallback must not be used')
    store = TryStore()
    recorder = throughput.Recorder(store)
    try:
        assert recorder.submit(throughput.Sample(50, 1000), now=NOW)
        wait_for(lambda: store.calls == min(3, busy_attempts + 1))
        wait_for(lambda: recorder._queue.unfinished_tasks == 0)
        assert len(store.records) == (0 if busy_attempts == 3 else 1)
    finally:
        assert recorder.close()


@pytest.mark.parametrize('outcome', ['exception', None, 0])
def test_nonblocking_writer_uncertain_results_and_errors_are_never_retried(outcome, caplog):
    called = threading.Event()
    class UncertainStore(Store):
        calls = 0
        def try_record_gateway_throughput(self, *_args, **_kwargs):
            self.calls += 1
            called.set()
            if outcome == 'exception':
                raise ValueError('PRIVATE_UNCERTAIN_WRITE')
            return outcome
    store = UncertainStore()
    recorder = throughput.Recorder(store)
    try:
        assert recorder.submit(throughput.Sample(50, 1000), now=NOW)
        assert called.wait(2)
        wait_for(lambda: recorder._queue.unfinished_tasks == 0)
        assert store.calls == 1
    finally:
        assert recorder.close()
    assert 'PRIVATE_' not in caplog.text


def test_shutdown_interrupts_known_busy_retries_between_worker_attempts():
    called = threading.Event()
    class BusyStore(Store):
        calls = 0
        def try_record_gateway_throughput(self, *_args, **_kwargs):
            self.calls += 1
            called.set()
            assert recorder._closed.wait(2)
            return False
    store = BusyStore()
    recorder = throughput.Recorder(store)
    try:
        assert recorder.submit(throughput.Sample(50, 1000), now=NOW)
        assert called.wait(2)
        assert recorder.close()
        assert store.calls == 1
    finally:
        assert recorder.close()


class Writer(io.BytesIO):
    def __init__(self, store, *, fail_terminal=False, fail_body=False, stop=None):
        super().__init__()
        self.store, self.fail_terminal, self.fail_body = store, fail_terminal, fail_body
        self.stop = stop
        self.terminal = False

    def write(self, part):
        if part == b"0\r\n\r\n":
            assert self.store.records == []
            self.terminal = True
        elif self.fail_body:
            raise BrokenPipeError("PRIVATE_CLIENT_DISCONNECT")
        return super().write(part)

    def flush(self):
        if self.terminal and self.fail_terminal:
            raise BrokenPipeError("PRIVATE_TERMINAL_FLUSH")
        if self.terminal and self.stop is not None:
            self.stop()


class Attempt:
    def __init__(self, clock, plan, store):
        self.clock, self.plan, self.store = clock, plan, store
        self.closed = False
        self.finish_called = False

    def start(self, _headers, _body, **kwargs):
        assert kwargs['before_model_send']()

    def metadata(self):
        return {"status": self.plan.get('status', 200), "retryable": False,
                "headers": {"content-type": self.plan.get('media', 'text/event-stream')}}

    def chunks(self):
        for part, stamp in zip(self.plan.get('parts', stream()), self.plan.get('stamps', [0, 0.5, 1])):
            self.clock.now = stamp
            if self.plan.get('cancel') and stamp > 0:
                self.store.active = False
            yield part
        if self.plan.get('truncated'):
            raise GatewayTransportError('PRIVATE_TRUNCATED')

    def finish(self, _status):
        self.finish_called = True
        # Transport-exit wait is outside the first-to-last body span.
        self.clock.now += self.plan.get('finish_delay', 10)
        if self.plan.get('finish_error'):
            raise GatewayTransportError('PRIVATE_FINISH')

    def close(self):
        self.closed = True


@pytest.fixture
def scenario(cfg):
    store, clock, attempts = Store(), Clock(), []
    plan = {}
    def factory(_target, _cfg):
        attempt = Attempt(clock, plan, store)
        attempts.append(attempt)
        return attempt
    runtime = gateway.GatewayRuntime(cfg, store, transport_factory=factory, clock=clock)
    runtime._disconnected = lambda _handler: False
    key = {'id': 'PRIVATE_KEY_ID', 'generation': 1, 'mode': 'priority',
           'account_id': 'PRIVATE_ACCOUNT', 'slot_ids': ['PRIVATE_SLOT']}
    handler = SimpleNamespace(path='/v1/messages', wfile=Writer(store), protocol_version='',
                              send_response=lambda _status: None, send_header=lambda *_args: None,
                              end_headers=lambda: None)
    result = SimpleNamespace(runtime=runtime, store=store, clock=clock, plan=plan,
                             attempts=attempts, key=key, handler=handler)
    yield result
    assert runtime.close()


def forward(scenario):
    scenario.runtime._forward(scenario.handler, scenario.key, '', '', 'PRIVATE_MODEL_HASH',
                              {}, b'PRIVATE_REQUEST_BODY')


def framed(parts, complete=True):
    return b''.join(f'{len(part):x}\r\n'.encode()+part+b'\r\n' for part in parts) + (
        b'0\r\n\r\n' if complete else b'')


@pytest.mark.parametrize('path', ['/v1/messages', '/v1/messages?beta=true'])
def test_successful_gateway_stream_is_byte_identical_and_recorded_after_finish_and_flush(scenario, path):
    assert path in gateway.PATHS
    scenario.handler.path = path
    forward(scenario)
    assert scenario.handler.wfile.getvalue() == framed(stream())
    assert scenario.attempts[0].finish_called
    assert scenario.store.recorded.wait(2)
    assert scenario.store.records[0][:2] == (50, 1000.0)
    assert len(scenario.attempts) == 1
    assert "PRIVATE_" not in json.dumps(scenario.store.records)


@pytest.mark.parametrize('plan', [
    {'truncated': True}, {'finish_error': True}, {'parts': stream()[:-1]},
    {'parts': stream(True)}, {'parts': [b''.join(stream())], 'stamps': [1]},
    {'parts': stream(), 'stamps': [0, 0.02, 0.05]}, {'cancel': True},
    {'status': 403, 'parts': [b'PRIVATE_REFUSAL'], 'stamps': [0]},
    {'media': 'application/json', 'parts': [b'{"output_tokens":50}'], 'stamps': [0]},
    {'parts': stream()[:1] + [event('error', {'type': 'error'})], 'stamps': [0, 1]},
])
def test_nonqualifying_responses_never_enqueue_or_reroute(scenario, plan):
    scenario.plan.update(plan)
    forward(scenario)
    assert scenario.store.records == []
    assert scenario.runtime.throughput._thread is None
    assert len(scenario.attempts) == 1


@pytest.mark.parametrize('failure', ['body_write', 'terminal_flush', 'shutdown'])
def test_failed_or_cancelled_http_delivery_has_no_sample(scenario, failure):
    scenario.handler.wfile = Writer(scenario.store, fail_body=failure == 'body_write',
                                   fail_terminal=failure == 'terminal_flush',
                                   stop=scenario.runtime.stopping.set if failure == 'shutdown' else None)
    forward(scenario)
    assert scenario.store.records == []
    assert scenario.runtime.throughput._thread is None


@pytest.mark.parametrize('path', ['/v1/messages/count_tokens', '/v1/messages/count_tokens?beta=true'])
def test_count_tokens_and_broker_responses_never_create_an_observer(scenario, cfg, monkeypatch, path):
    created = []
    def forbidden(*_args, **_kwargs):
        created.append(True)
        raise AssertionError('observer must not be created')
    monkeypatch.setattr(throughput, 'StreamObserver', forbidden)
    assert path in gateway.PATHS
    scenario.handler.path = path
    forward(scenario)
    assert scenario.handler.wfile.getvalue() == framed(stream())
    assert created == []
    broker = gateway.GatewayRuntime(replace(cfg, broker_only=True), scenario.store,
                                    transport_factory=scenario.runtime.transport_factory,
                                    clock=scenario.clock)
    try:
        scenario.handler.path = '/v1/messages'
        scenario.handler.wfile = Writer(scenario.store)
        broker._disconnected = lambda _handler: False
        broker._forward(scenario.handler, scenario.key, '', '', '', {}, b'{}')
        assert broker.throughput is None and created == []
        assert scenario.handler.wfile.getvalue() == framed(stream())
    finally:
        assert broker.close()


@pytest.mark.parametrize('method', ['constructor', 'feed', 'finish', 'submit', 'full_queue'])
def test_observer_and_enqueue_failures_do_not_change_forwarded_response_or_log_content(
        scenario, monkeypatch, caplog, method):
    def broken(*_args, **_kwargs):
        raise ValueError('PRIVATE_BODY_MODEL_AND_KEY')
    if method == 'constructor':
        monkeypatch.setattr(throughput, 'StreamObserver', broken)
    elif method == 'full_queue':
        monkeypatch.setattr(scenario.runtime.throughput, 'submit', lambda *_args, **_kwargs: False)
    elif method == 'submit':
        monkeypatch.setattr(scenario.runtime.throughput, method, broken)
    else:
        monkeypatch.setattr(throughput.StreamObserver, method, broken)
    forward(scenario)
    assert scenario.handler.wfile.getvalue() == framed(stream())
    assert scenario.store.records == [] and len(scenario.attempts) == 1
    assert 'PRIVATE_' not in caplog.text


def test_storage_failure_after_complete_forwarding_has_no_response_or_log_effect(
        scenario, monkeypatch, caplog):
    called = threading.Event()
    def broken(*_args, **_kwargs):
        called.set()
        raise ValueError('PRIVATE_SQL_CONTENT')
    monkeypatch.setattr(scenario.store, 'record_gateway_throughput', broken)
    forward(scenario)
    assert scenario.handler.wfile.getvalue() == framed(stream())
    assert called.wait(2)
    assert scenario.store.records == [] and 'PRIVATE_' not in caplog.text


def test_deadline_expiring_during_transport_finish_does_not_record_or_change_delivery(scenario):
    scenario.plan['finish_delay'] = scenario.runtime.cfg.gateway_request_timeout_s + 1
    forward(scenario)
    assert scenario.handler.wfile.getvalue() == framed(stream())
    assert scenario.store.records == [] and scenario.runtime.throughput._thread is None
