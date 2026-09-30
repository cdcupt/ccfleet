"""Synthetic response usage only; no provider calls, content logging or identity metrics."""

import copy
import io
import json
import socket
import threading
import time

import pytest

from ccfleet_agent import local_relay as relay
from ccfleet_agent import relay_metrics as metrics
from ccfleetd import heartbeat, usersite
from tests import test_local_relay
from tests.test_local_relay import Upstream, request, response, run

slot = test_local_relay.slot
BOUND = "0123456789abcdef"
SECRET = "PRIVATE_CONTENT_MODEL_SESSION_PATH_EMAIL"


def event(kind, **fields):
    return (f"event: {kind}\n" + "data: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


def stream(*, cache=True, final=9):
    usage = {"input_tokens": 20, "output_tokens": 1}
    if cache:
        usage.update(cache_read_input_tokens=8, cache_creation_input_tokens=3)
    return (
        event("message_start", message={"id": SECRET, "model": SECRET, "usage": usage}),
        event("content_block_delta", delta={"text": SECRET}),
        event("message_delta", usage={"output_tokens": final}),
        event("message_stop"),
    )


def collect(chunks, media="text/event-stream"):
    observer = metrics.UsageCollector(media)
    for chunk in chunks:
        observer.feed(chunk)
    return observer.finish()


@pytest.mark.parametrize("width", [1, 2, 7, 37, 65536])
def test_fragmented_sse_reports_cumulative_usage_not_delta_sum(width):
    chunks = stream()
    raw = b"".join(chunks[:2]) + event("message_delta", usage={"output_tokens": 5}) + b"".join(chunks[2:])
    result = collect([raw[i:i + width] for i in range(0, len(raw), width)])
    assert result == {"input_tokens": 20, "output_tokens": 9,
                      "cache_read_input_tokens": 8, "cache_creation_input_tokens": 3}
    assert SECRET not in json.dumps(result)


def test_duplicate_cumulative_delta_is_not_double_counted():
    chunks = stream()
    assert collect([*chunks[:-1], chunks[-2], chunks[-1]])["output_tokens"] == 9


@pytest.mark.parametrize("case", ["duplicate_start", "duplicate_stop", "no_stop", "delta_first",
                                  "decreasing", "error", "trailing_partial", "missing_delta"])
def test_incomplete_ambiguous_or_failed_stream_has_no_sample(case):
    chunks = list(stream())
    if case == "duplicate_start":
        chunks.insert(1, chunks[0])
    elif case == "duplicate_stop":
        chunks.append(chunks[-1])
    elif case == "no_stop":
        chunks.pop()
    elif case == "delta_first":
        chunks.insert(0, chunks[-2])
    elif case == "decreasing":
        chunks.insert(-1, event("message_delta", usage={"output_tokens": 2}))
    elif case == "error":
        chunks.insert(-1, event("error", error={"message": SECRET}))
    elif case == "trailing_partial":
        chunks.append(b"data: {")
    else:
        chunks.pop(-2)
    assert collect(chunks) is None


@pytest.mark.parametrize("value", [True, -1, 1.5, "9", None, 10**400, float("nan")])
def test_noninteger_negative_or_unbounded_usage_is_unknown(value):
    chunks = list(stream())
    chunks[-2] = event("message_delta", usage={"output_tokens": value})
    assert collect(chunks) is None


def test_duplicate_json_keys_do_not_silently_choose_a_token_count():
    chunks = list(stream())
    chunks[-2] = b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7,"output_tokens":9}}\n\n'
    assert collect(chunks) is None


def test_crlf_and_multiline_sse_data_are_supported():
    raw = b"".join(stream())
    raw = raw.replace(b'data: {"type": "message_stop"}',
                      b'data: {"type":\ndata: "message_stop"}').replace(b"\n", b"\r\n")
    assert collect([raw])["output_tokens"] == 9


@pytest.mark.parametrize("kind", ["line", "event", "json"])
def test_large_responses_disable_sampling_with_bounded_memory(kind):
    media = "application/json" if kind == "json" else "text/event-stream"
    observer = metrics.UsageCollector(media)
    if kind == "event":
        observer.feed(b"event: message_start\n")
        for _ in range(100):
            observer.feed(b"data: " + b"x" * 1024 + b"\n")
    else:
        for _ in range(400):
            observer.feed(b"x" * 1024)
    assert observer.bad and len(observer.buffer) == len(observer.data) == 0
    assert observer.finish() is None


@pytest.mark.parametrize("changes", [{"input_tokens": 12, "output_tokens": 4},
                                     {"input_tokens": 0, "output_tokens": 0,
                                      "cache_read_input_tokens": 0}])
def test_complete_json_usage_preserves_missing_cache_as_unknown(changes):
    raw = json.dumps({"type": "message", "id": SECRET, "content": [{"text": SECRET}],
                      "stop_reason": "end_turn", "usage": changes}).encode()
    result = collect([raw[:5], raw[5:]], "application/json")
    assert result == changes
    assert "cache_creation_input_tokens" not in result


@pytest.mark.parametrize("body", [
    {"type": "message", "stop_reason": None, "usage": {"input_tokens": 2, "output_tokens": 3}},
    {"type": "message", "stop_reason": "end_turn", "usage": {"input_tokens": 2}},
    {"type": "error", "stop_reason": "end_turn", "usage": {"input_tokens": 2, "output_tokens": 3}},
])
def test_json_without_completed_message_and_complete_core_usage_is_unsampled(body):
    assert collect([json.dumps(body).encode()], "application/json") is None


def test_numeric_usage_accumulates_once_and_cache_coverage_is_explicit(slot):
    now = time.time()
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 20, "output_tokens": 9, "cache_read_input_tokens": 8})
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True)
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 2, "output_tokens": 3, "cache_creation_input_tokens": 0})
    result = metrics.report(slot, BOUND, now)
    usage = result["token_usage"]
    assert result["requests"] == result["success"] == 3
    assert usage == {"eligible": 3, "samples": 2, "input_tokens": 22, "output_tokens": 12,
                     "cache_read_input_tokens": 8, "cache_creation_input_tokens": 0,
                     "cache_read_samples": 1, "cache_creation_samples": 1}
    saved = (slot / ".config/ccfleet" / metrics.FILE_NAME).read_text()
    assert SECRET not in saved
    assert BOUND not in json.dumps(heartbeat.relay_report(result, now))


def test_legacy_state_and_report_remain_readable_without_fabricated_usage(slot):
    now = time.time()
    metrics.record(slot, BOUND, "success", {}, now)
    original = metrics.report(slot, BOUND, now)
    assert "token_usage" not in original
    assert "token_usage" not in metrics.validate_report(original)
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 2, "output_tokens": 1})
    result = metrics.report(slot, BOUND, now)
    assert result["success"] == 2
    assert result["token_usage"]["eligible"] == result["token_usage"]["samples"] == 1


@pytest.mark.parametrize("field,value", [("samples", True), ("eligible", -1),
                                         ("input_tokens", "SECRET"), ("samples", 2),
                                         ("cache_read_samples", 2), ("extra", SECRET)])
def test_server_drops_invalid_or_identifying_usage_fields(slot, field, value):
    now = time.time()
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 2, "output_tokens": 1})
    result = metrics.report(slot, BOUND, now)
    result["token_usage"][field] = value
    assert heartbeat.relay_report(result, now) is None


def test_non_successful_or_unattributed_usage_cannot_be_recorded(slot):
    now = time.time()
    for changes in ({"outcome": "cancelled", "usage_eligible": True},
                    {"outcome": "success", "usage_eligible": False}):
        with pytest.raises(metrics.MetricsError):
            metrics.record(slot, BOUND, changes["outcome"], {}, now,
                           usage={"input_tokens": 2, "output_tokens": 1},
                           usage_eligible=changes["usage_eligible"])


def test_completed_relay_keeps_stream_identical_and_persists_only_numeric_usage(slot):
    chunks = stream()
    code, raw, upstream = run(slot, upstream=Upstream(chunks=chunks))
    assert code == 0 and response(raw)[1] == b"".join(chunks)
    assert len(upstream.calls) == 1
    bound = relay.bound_account(slot)
    result = metrics.report(slot, bound, time.time())
    assert result["token_usage"]["output_tokens"] == 9
    saved = (slot / ".config/ccfleet" / metrics.FILE_NAME).read_text()
    assert SECRET not in saved and "slot-test-token" not in saved


def test_partial_or_cancelled_relay_does_not_save_observed_usage(slot):
    code, _, _ = run(slot, upstream=Upstream(chunks=stream(), failure="truncated"))
    assert code == 2
    result = metrics.report(slot, relay.bound_account(slot), time.time())
    assert result["connection_errors"] == 1 and "token_usage" not in result


def test_cancelled_stream_discards_usage_already_seen_in_message_start(slot):
    reader, writer = socket.socketpair()
    source = reader.makefile("rb")
    upstream, output, result = Upstream(), io.BytesIO(), []
    waiting = threading.Event()
    first = [True]

    def read(size):
        if first[0]:
            first[0] = False
            return stream()[0]
        waiting.set()
        assert upstream.sock.cancelled.wait(3)
        return b""

    upstream.read1 = read
    worker = threading.Thread(target=lambda: result.append(relay.serve_one(
        source, output, slot, policy=lambda: None, connect=lambda: upstream, watch=True)))
    writer.sendall(request())
    worker.start()
    try:
        assert waiting.wait(2)
        writer.shutdown(socket.SHUT_WR)
        worker.join(timeout=4)
        assert result == [2] and not worker.is_alive()
        summary = metrics.report(slot, relay.bound_account(slot), time.time())
        assert summary["cancelled"] == 1 and "token_usage" not in summary
    finally:
        writer.close()
        worker.join(timeout=3)
        source.close()
        reader.close()


@pytest.mark.parametrize("media", ["text/event-stream", "application/json"])
def test_oversized_observation_never_truncates_or_rewrites_forwarded_response(slot, media):
    raw = (b"data: " + b"x" * (metrics.USAGE_JSON_BYTES + 1) + b"\n\n")
    code, output, upstream = run(slot, upstream=Upstream(
        chunks=[raw], headers=[("content-type", media)]))
    assert code == 0 and response(output)[1] == raw and len(upstream.calls) == 1
    usage = metrics.report(slot, relay.bound_account(slot), time.time())["token_usage"]
    assert usage["eligible"] == 1 and usage["samples"] == 0


def test_inband_error_sse_is_passed_through_but_usage_stays_unsampled(slot):
    chunks = [*stream()[:-1], event("error", error={"message": SECRET}), stream()[-1]]
    code, raw, _ = run(slot, upstream=Upstream(chunks=chunks))
    assert code == 0 and response(raw)[1] == b"".join(chunks)
    usage = metrics.report(slot, relay.bound_account(slot), time.time())["token_usage"]
    assert usage["eligible"] == 1 and usage["samples"] == 0


def test_count_tokens_is_not_a_usage_eligible_model_response(slot):
    raw = json.dumps({"type": "message", "stop_reason": "end_turn",
                      "usage": {"input_tokens": 20, "output_tokens": 3}}).encode()
    code, _, _ = run(slot, request(path="/v1/messages/count_tokens"),
                    upstream=Upstream(chunks=[raw], headers=[("content-type", "application/json")]))
    assert code == 0
    assert "token_usage" not in metrics.report(slot, relay.bound_account(slot), time.time())


def test_collector_failure_never_changes_stream_result(slot, monkeypatch):
    class Broken:
        def __init__(self, _): pass
        def feed(self, _): raise RuntimeError(SECRET)
        def finish(self): raise RuntimeError(SECRET)
    monkeypatch.setattr(metrics, "UsageCollector", Broken)
    code, raw, _ = run(slot, upstream=Upstream(chunks=stream()))
    assert code == 0 and SECRET.encode() in response(raw)[1]
    usage = metrics.report(slot, relay.bound_account(slot), time.time())["token_usage"]
    assert usage["samples"] == 0


def test_usage_resets_with_account_binding_and_late_writer_guard(slot):
    now = time.time()
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 22, "output_tokens": 5})
    other = "fedcba9876543210"
    assert "token_usage" not in metrics.report(slot, other, now)
    metrics.record(slot, other, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 1, "output_tokens": 2})
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 999, "output_tokens": 999}, still_bound=lambda: False)
    assert metrics.report(slot, other, now)["token_usage"]["input_tokens"] == 1


def test_ui_displays_usage_coverage_units_and_unknown_cache_not_fake_zero(slot):
    now = time.time()
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True,
                   usage={"input_tokens": 20, "output_tokens": 9})
    summary = metrics.report(slot, BOUND, now)
    body = usersite._relay_usage(summary, now, now, 300)
    assert "1 of 1 completed Messages responses" in body and "20 input tokens" in body
    assert body.count("unknown (no explicit cache samples)") == 2
    assert "not a bill" in body and "No model/session IDs" in body
    legacy = copy.deepcopy(summary)
    legacy.pop("token_usage")
    assert "not measured by this report" in usersite._relay_usage(legacy, now, now, 300)
