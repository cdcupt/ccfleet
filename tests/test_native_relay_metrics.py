"""New numeric relay observations only; synthetic responses and no model calls."""

import copy
import io
import json
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ccfleet_agent import local_relay as relay
from ccfleet_agent import relay_metrics as metrics
from tests import test_local_relay
from tests.test_local_relay import Upstream, request, response, run

slot = test_local_relay.slot
BOUND = "0123456789abcdef"
OTHER = "fedcba9876543210"
SECRET = "PRIVATE_MODEL_TEXT_SESSION_EMAIL_PATH_TOKEN"
PROVIDER_MODEL = "claude-sonnet-4-5-20250929"
USAGE = {"input_tokens": 20, "output_tokens": 9, "cache_read_input_tokens": 8}


def event(kind, **fields):
    return (f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


def stream(model=PROVIDER_MODEL):
    return [event("message_start", message={"id": SECRET, "model": model,
                   "usage": {**USAGE, "output_tokens": 1}, "content": [{"text": SECRET}]}),
            event("content_block_delta", delta={"text": SECRET}),
            event("message_delta", usage={"output_tokens": 9}), event("message_stop")]


def complete(chunks=None, media="text/event-stream"):
    collector = metrics.UsageCollector(media)
    for part in stream() if chunks is None else chunks:
        collector.feed(part)
        assert collector.public_model is None
    return collector, collector.finish()


def record(slot, *, now=None, **changes):
    args = {"usage_eligible": True, "usage": USAGE, "model_eligible": True,
            "public_model": metrics._public_model(PROVIDER_MODEL),
            "stream_output": {"samples": 1, "output_tokens": 9, "stream_ms": 1000}}
    args.update(changes)
    metrics.record(slot, BOUND, "success", {}, time.time() if now is None else now, **args)


def test_completed_model_is_canonical_response_label_and_numeric_api_is_unchanged():
    observer, usage = complete()
    assert usage == USAGE
    assert observer.public_model == metrics._model_catalogue().normalize(PROVIDER_MODEL)
    assert observer.public_model != "other"
    assert observer.buffer == observer.data == bytearray() and observer.numbers == {}
    assert observer._model == "other" and observer.event == ""
    assert SECRET not in json.dumps({"usage": usage, "model": observer.public_model})
    assert observer.finish() is None and observer.public_model is None


@pytest.mark.parametrize("model", [SECRET, None, 7, True, {}, [SECRET], "claude-UNKNOWN", "x" * 4096])
def test_unknown_model_is_folded_without_retaining_a_raw_label(model):
    observer, usage = complete(stream(model))
    assert usage == USAGE and observer.public_model == "other"
    assert observer._model == "other" and not observer.buffer and not observer.data


@pytest.mark.parametrize("case", ["truncated", "error", "invalid_usage", "trailing", "json_error"])
def test_invalid_completed_usage_never_exposes_a_model(case):
    parts, media = stream(), "text/event-stream"
    if case == "truncated":
        parts.pop()
    elif case == "error":
        parts.insert(-1, event("error", error={"message": SECRET}))
    elif case == "invalid_usage":
        parts[-2] = event("message_delta", usage={"output_tokens": True})
    elif case == "trailing":
        parts.append(b"data: {PRIVATE_PARTIAL")
    else:
        parts, media = [json.dumps({"type": "error", "model": PROVIDER_MODEL,
                                   "usage": USAGE}).encode()], "application/json"
    observer, usage = complete(parts, media)
    assert usage is None and observer.public_model is None
    assert observer.buffer == observer.data == bytearray() and observer.numbers == {}


def test_json_response_model_is_normalized_without_any_request_label():
    raw = json.dumps({"type": "message", "stop_reason": "end_turn", "usage": USAGE,
                      "model": PROVIDER_MODEL, "id": SECRET, "content": [{"text": SECRET}]}).encode()
    observer, usage = complete([raw[:5], raw[5:]], "application/json")
    assert usage == USAGE and observer.public_model == metrics._public_model(PROVIDER_MODEL)


@pytest.mark.parametrize("entrypoint", ["metrics", "relay"])
def test_isolated_node_sibling_import_does_not_load_holder_modules(tmp_path, entrypoint):
    (tmp_path / "model_usage.py").write_text("raise RuntimeError('holder import shadow')\n")
    script = (
        "import importlib.util,json,sys; "
        "spec=importlib.util.spec_from_file_location('isolated_metrics',sys.argv[1]); "
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m); "
        + ("m=m._metrics_module(); " if entrypoint == "relay" else "") +
        "o=m.UsageCollector('text/event-stream');o.feed(sys.stdin.buffer.read()); "
        "u=o.finish(); ticks=iter([0,1]); s=m.StreamOutputCollector(clock=lambda:next(ticks)); "
        "s.forwarded();s.forwarded(); "
        "print(json.dumps({'usage':u,'model':o.public_model,'stream':s.finish(u['output_tokens'])}))"
    )
    source = relay.__file__ if entrypoint == "relay" else metrics.__file__
    result = subprocess.run([sys.executable, "-I", "-c", script, source],
                            input=b"".join(stream()), cwd=tmp_path, capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout) == {"usage": USAGE,
        "model": metrics._public_model(PROVIDER_MODEL),
        "stream": {"samples": 1, "output_tokens": 9, "stream_ms": 1000}}


@pytest.mark.parametrize("stamps,expected", [([0, .1], 100), ([10, 11, 12], 2000),
                                           ([10], None), ([0, .05], None), ([0, 3601], None),
                                           ([1, 0], None), ([0, float("nan")], None),
                                           ([0, True], None), ([0, float("inf")], None)])
def test_stream_duration_uses_only_successfully_forwarded_chunks(stamps, expected):
    sequence = iter(stamps)
    observer = metrics.StreamOutputCollector(clock=lambda: next(sequence))
    for _ in stamps:
        observer.forwarded()
    sample = observer.finish(0)
    if expected is None:
        assert sample is None
    else:
        assert sample["samples"] == 1 and sample["output_tokens"] == 0
        assert sample["stream_ms"] == pytest.approx(expected)
    assert observer.first is observer.last is None and observer.chunks == 0
    assert observer.finish(0) is None


@pytest.mark.parametrize("tokens", [True, -1, 1.5, "9", None, 10**9 + 1])
def test_stream_duration_requires_a_bounded_final_output_count(tokens):
    stamps = iter([0, 1])
    observer = metrics.StreamOutputCollector(clock=lambda: next(stamps))
    observer.forwarded()
    observer.forwarded()
    assert observer.finish(tokens) is None


def test_optional_totals_are_matched_and_legacy_samples_are_not_invented(slot):
    now = time.time()
    metrics.record(slot, BOUND, "success", {}, now, usage_eligible=True, usage=USAGE)
    old = metrics.report(slot, BOUND, now)
    assert "model_usage" not in old and "stream_output" not in old
    record(slot, now=now)
    record(slot, now=now, usage=None, public_model=None, stream_output=None)
    result = metrics.report(slot, BOUND, now)
    assert result["requests"] == 3 and result["token_usage"]["samples"] == 2
    assert result["model_usage"]["eligible"] == 2
    assert result["model_usage"]["samples"] == 1
    assert result["model_usage"]["models"] == [{"model": metrics._public_model(PROVIDER_MODEL),
        "samples": 1, **USAGE, "cache_creation_input_tokens": 0,
        "cache_read_samples": 1, "cache_creation_samples": 0}]
    assert result["stream_output"] == {"samples": 1, "output_tokens": 9, "stream_ms": 1000}
    detached = metrics.validate_report(result)
    detached["model_usage"]["models"][0]["output_tokens"] = 0
    assert result["model_usage"]["models"][0]["output_tokens"] == 9
    assert SECRET not in (slot / ".config/ccfleet" / metrics.FILE_NAME).read_text()


def test_new_totals_prune_seven_utc_dates_and_reset_on_account_change(slot):
    now = 20000 * metrics.DAY_SECONDS + 100
    for day in range(8):
        record(slot, now=now + day * metrics.DAY_SECONDS)
    summary = metrics.report(slot, BOUND, now + 7 * metrics.DAY_SECONDS)
    assert summary["stream_output"]["samples"] == summary["model_usage"]["samples"] == 7
    assert summary["model_usage"]["models"][0]["input_tokens"] == 140
    assert "model_usage" not in metrics.report(slot, OTHER, now + 7 * metrics.DAY_SECONDS)
    metrics.record(slot, OTHER, "success", {}, now + 7 * metrics.DAY_SECONDS)
    metrics.record(slot, BOUND, "success", {}, now + 7 * metrics.DAY_SECONDS,
                   usage_eligible=True, usage=USAGE, model_eligible=True,
                   public_model="other", still_bound=lambda: False)
    assert "stream_output" not in metrics.report(slot, OTHER, now + 7 * metrics.DAY_SECONDS)


@pytest.mark.parametrize("block,key,value", [
    ("model_usage", "eligible", True), ("model_usage", "samples", 2),
    ("model_usage", "model", SECRET), ("model_usage", "model", "claude-sonnet-4-5-20250929"),
    ("model_usage", "output_tokens", 10), ("model_usage", "cache_read_samples", 2),
    ("model_usage", "extra", SECRET), ("stream_output", "samples", True),
    ("stream_output", "samples", 2), ("stream_output", "output_tokens", 10),
    ("stream_output", "stream_ms", 99), ("stream_output", "stream_ms", float("nan")),
    ("stream_output", "stream_ms", 3600001), ("stream_output", "extra", SECRET),
])
def test_report_rejects_private_inconsistent_or_unmatched_new_fields(slot, block, key, value):
    now = time.time()
    record(slot, now=now)
    report = metrics.report(slot, BOUND, now)
    target = report[block]
    if block == "model_usage" and key not in {"eligible", "samples"}:
        target = target["models"][0]
    target[key] = value
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(report)


def test_duplicate_excess_models_and_missing_matched_token_block_are_rejected(slot):
    now = time.time()
    record(slot, now=now)
    report = metrics.report(slot, BOUND, now)
    for count in (2, 17):
        tainted = copy.deepcopy(report)
        tainted["model_usage"]["models"] *= count
        with pytest.raises(metrics.MetricsError):
            metrics.validate_report(tainted)
    report.pop("token_usage")
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(report)


@pytest.mark.parametrize("changes", [
    {"model_eligible": False}, {"public_model": SECRET}, {"public_model": None},
    {"usage": None}, {"usage_eligible": False},
    {"stream_output": {"samples": 1, "output_tokens": 8, "stream_ms": 1000}},
    {"stream_output": {"samples": 0, "output_tokens": 0, "stream_ms": 0}},
])
def test_invalid_sample_does_not_modify_previous_local_file(slot, changes):
    record(slot)
    target = slot / ".config/ccfleet" / metrics.FILE_NAME
    original = target.read_bytes()
    with pytest.raises(metrics.MetricsError):
        record(slot, **changes)
    assert target.read_bytes() == original


def test_model_bound_folds_excess_known_labels_into_other_without_losing_numbers(slot, monkeypatch):
    monkeypatch.setattr(metrics, "MAX_MODELS", 3)
    monkeypatch.setattr(metrics, "_catalogue", SimpleNamespace(normalize=lambda value: value))
    now = 20000 * metrics.DAY_SECONDS + 100
    for i in range(5):
        record(slot, now=now + i * metrics.DAY_SECONDS, public_model=f"public-{i}")
    result = metrics.report(slot, BOUND, now + 4 * metrics.DAY_SECONDS)
    assert len(result["model_usage"]["models"]) == 3
    assert {row["model"] for row in result["model_usage"]["models"]} == {
        "public-0", "public-1", "other"}
    assert sum(row["output_tokens"] for row in result["model_usage"]["models"]) == 45


def timed_collector(monkeypatch):
    clock = [0]
    original = metrics.StreamOutputCollector
    monkeypatch.setattr(metrics, "StreamOutputCollector", lambda: original(clock=lambda: clock[0]))
    return clock


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages?beta=true"])
@pytest.mark.parametrize("gateway", [False, True])
def test_real_relay_forwards_once_and_separates_gateway_speed_samples(slot, monkeypatch, path, gateway):
    clock = timed_collector(monkeypatch)
    parts, upstream = stream(), Upstream(chunks=stream())
    original = upstream.read1
    def read(size):
        clock[0] += 0.5
        return original(size)
    upstream.read1 = read
    code, raw, _ = run(slot, request(path=path), upstream=upstream, gateway=gateway)
    assert code == 0 and response(raw)[1] == b"".join(parts) and len(upstream.calls) == 1
    summary = metrics.report(slot, relay.bound_account(slot), time.time())
    assert summary["model_usage"]["samples"] == summary["token_usage"]["samples"] == 1
    if gateway:
        assert "stream_output" not in summary
    else:
        assert summary["stream_output"] == {"samples": 1, "output_tokens": 9, "stream_ms": 1500}
    saved = (slot / ".config/ccfleet" / metrics.FILE_NAME).read_text()
    assert SECRET not in saved and "slot-test-token" not in saved


def test_output_observation_follows_successful_write_and_flush(slot, monkeypatch):
    events = []
    original = metrics.StreamOutputCollector
    class Tracking(original):
        def forwarded(self):
            assert events[-1] == "flush"
            events.append("observed")
            super().forwarded()
    class Output(io.BytesIO):
        def write(self, data):
            events.append("write")
            return super().write(data)
        def flush(self):
            events.append("flush")
    monkeypatch.setattr(metrics, "StreamOutputCollector", Tracking)
    output, upstream = Output(), Upstream(chunks=stream())
    code = relay.serve_one(io.BytesIO(request()), output, slot,
                           policy=lambda: None, connect=lambda: upstream)
    assert code == 0 and events.count("observed") == 4
    assert response(output.getvalue())[1] == b"".join(stream())


@pytest.mark.parametrize("case", ["single_chunk", "too_short", "truncated", "sse_error",
                                  "count_tokens", "count_tokens_beta", "http_error", "json"])
def test_nonqualifying_relay_has_no_native_speed_sample(slot, monkeypatch, case):
    clock = timed_collector(monkeypatch)
    parts, kwargs, path = stream(), {}, "/v1/messages"
    if case == "single_chunk":
        parts = [b"".join(parts)]
    elif case == "truncated":
        kwargs["failure"] = "truncated"
    elif case == "sse_error":
        parts.insert(-1, event("error", error={"message": SECRET}))
    elif case.startswith("count_tokens"):
        path = "/v1/messages/count_tokens" + ("?beta=true" if case.endswith("beta") else "")
    elif case == "http_error":
        kwargs["status"] = 403
    elif case == "json":
        parts = [json.dumps({"type": "message", "stop_reason": "end_turn", "model": PROVIDER_MODEL,
                             "usage": USAGE}).encode()]
        kwargs["headers"] = [("content-type", "application/json")]
    upstream = Upstream(chunks=parts, **kwargs)
    original = upstream.read1
    def read(size):
        clock[0] += .001 if case == "too_short" else .5
        return original(size)
    upstream.read1 = read
    code, raw, _ = run(slot, request(path=path), upstream=upstream)
    assert code == (2 if case in {"truncated", "http_error"} else 0)
    summary = metrics.report(slot, relay.bound_account(slot), time.time())
    assert "stream_output" not in summary and len(upstream.calls) == 1
    if case == "json":
        assert summary["model_usage"]["samples"] == 1
    elif case in {"sse_error"}:
        assert summary["model_usage"]["eligible"] == 1 and summary["model_usage"]["samples"] == 0
    elif case.startswith("count_tokens") or case in {"truncated", "http_error"}:
        assert "model_usage" not in summary
    if case not in {"truncated", "http_error"}:
        assert response(raw)[1] == b"".join(parts)


@pytest.mark.parametrize("failure", ["observer_construct", "observer_feed", "observer_finish", "storage"])
def test_new_observation_failures_preserve_inference_bytes_and_do_not_log(slot, monkeypatch, caplog, failure):
    def broken(*_args, **_kwargs):
        raise RuntimeError(SECRET)
    if failure == "observer_construct":
        monkeypatch.setattr(metrics, "StreamOutputCollector", broken)
    elif failure == "observer_feed":
        monkeypatch.setattr(metrics.StreamOutputCollector, "forwarded", broken)
    elif failure == "observer_finish":
        monkeypatch.setattr(metrics.StreamOutputCollector, "finish", broken)
    else:
        monkeypatch.setattr(metrics, "record", broken)
    code, raw, upstream = run(slot, upstream=Upstream(chunks=stream()))
    assert code == 0 and response(raw)[1] == b"".join(stream()) and len(upstream.calls) == 1
    assert SECRET not in caplog.text


@pytest.mark.parametrize("failure", ["body", "terminal"])
def test_failed_body_or_terminal_flush_never_records_completed_usage_or_speed(slot, monkeypatch, failure):
    timed_collector(monkeypatch)
    class Output(io.BytesIO):
        fail = False
        def write(self, data):
            part = bytes(data)
            self.fail = (part == struct.pack("!I", 0) if failure == "terminal"
                         else b"event: message_start" in part)
            return super().write(data)
        def flush(self):
            if self.fail:
                raise BrokenPipeError(SECRET)
    upstream, output = Upstream(chunks=stream()), Output()
    code = relay.serve_one(io.BytesIO(request()), output, slot,
                           policy=lambda: None, connect=lambda: upstream)
    assert code == 2 and len(upstream.calls) == 1
    summary = metrics.report(slot, relay.bound_account(slot), time.time())
    assert summary["connection_errors"] == 1
    assert not summary.keys() & {"token_usage", "model_usage", "stream_output"}


def test_failed_model_accessor_does_not_discard_numeric_usage(slot, monkeypatch):
    class BrokenModel(metrics.UsageCollector):
        @property
        def public_model(self):
            raise RuntimeError(SECRET)
    monkeypatch.setattr(metrics, "UsageCollector", BrokenModel)
    code, raw, upstream = run(slot, upstream=Upstream(chunks=stream()))
    assert code == 0 and response(raw)[1] == b"".join(stream()) and len(upstream.calls) == 1
    summary = metrics.report(slot, relay.bound_account(slot), time.time())
    assert summary["token_usage"]["samples"] == 1
    assert summary["model_usage"]["models"][0]["model"] == "other"
