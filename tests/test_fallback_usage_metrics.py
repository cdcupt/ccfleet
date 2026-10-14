"""Synthetic documented fallback metadata must not price another model's usage."""

import json
import subprocess
import sys
import time

import pytest

from ccfleet_agent import local_relay as relay
from ccfleet_agent import relay_metrics as metrics
from tests import test_local_relay
from tests.test_local_relay import Upstream, response, run

slot = test_local_relay.slot
START = "claude-opus-5-5"
RETURNED = "claude-haiku-4-5"
SECRET = "PRIVATE_REQUEST_ID_CONTENT_MODEL_EMAIL_SESSION"
INITIAL = {"input_tokens": 535, "output_tokens": 1,
           "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50}
FINAL = {"input_tokens": 412, "output_tokens": 264,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def event(kind, **fields):
    return (f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


def start(usage=None, model=START):
    return event("message_start", message={"id": SECRET, "model": model,
                                          "usage": INITIAL if usage is None else usage})


def boundary():
    return event("content_block_start", index=0, content_block={"type": "fallback",
                 "from": {"model": START}, "to": {"model": RETURNED}, "text": SECRET})


def iterations():
    return [{"type": "message", "model": START, **INITIAL},
            {"type": "fallback_message", "model": RETURNED, **FINAL}]


def complete(chunks, media="text/event-stream"):
    observer = metrics.UsageCollector(media)
    for chunk in chunks:
        observer.feed(chunk)
    usage = observer.finish()
    assert observer.buffer == observer.data == bytearray() and observer.numbers == {}
    assert observer._model == "other" and observer._unpriced is False
    assert observer._fallback is observer._replace_usage is False
    assert SECRET not in json.dumps(vars(observer), default=str)
    return observer, usage


@pytest.mark.parametrize("width", [1, 7, 65536])
def test_documented_midstream_535_to_412_keeps_final_attempt_once_and_unpriced(width):
    raw = b"".join([start(), boundary(), event("content_block_stop", index=0),
                    event("message_delta", usage={**FINAL, "iterations": iterations()}),
                    event("message_stop")])
    observer, usage = complete([raw[index:index + width] for index in range(0, len(raw), width)])
    assert usage == FINAL and observer.public_model == "other"
    assert usage["input_tokens"] != INITIAL["input_tokens"] + FINAL["input_tokens"]
    assert observer.finish() is None and observer.public_model is None


def test_changed_attempt_does_not_keep_initial_cache_when_final_cache_is_absent():
    final = {"input_tokens": 412, "output_tokens": 264}
    observer, usage = complete([start(), boundary(), event("message_delta", usage=final),
                                event("message_stop")])
    assert usage == final and observer.public_model == "other"
    assert not set(usage) & {"cache_read_input_tokens", "cache_creation_input_tokens"}


def test_sticky_fallback_without_content_block_is_unpriced_from_final_iterations():
    observer, usage = complete([start(), event("message_delta", usage={**FINAL,
                               "iterations": [{"type": "fallback_message", "model": RETURNED,
                                               **FINAL}]}), event("message_stop")])
    assert usage == FINAL and observer.public_model == "other"


def test_sticky_fallback_already_known_at_start_allows_ordinary_partial_final_output():
    numbers = {"input_tokens": 412, "output_tokens": 1}
    observer, usage = complete([start({**numbers, "iterations": [
        {"type": "fallback_message", "model": RETURNED, **numbers}]}, RETURNED),
        event("message_delta", usage={"output_tokens": 264}), event("message_stop")])
    assert usage == {"input_tokens": 412, "output_tokens": 264}
    assert observer.public_model == "other"


def test_repeated_fallback_metadata_does_not_reset_or_double_count_returned_attempt_again():
    observer, usage = complete([start(), boundary(), event("message_delta", usage={**FINAL,
                                "iterations": iterations()}),
                                event("message_delta", usage={"output_tokens": 265,
                                                              "iterations": iterations()}),
                                event("message_stop")])
    assert usage == {**FINAL, "output_tokens": 265} and observer.public_model == "other"


def test_second_explicit_fallback_boundary_replaces_prior_attempt_again():
    observer, usage = complete([start(), boundary(), event("message_delta", usage=FINAL),
                                boundary(), event("message_delta", usage={"input_tokens": 10,
                                                                         "output_tokens": 2}),
                                event("message_stop")])
    assert usage == {"input_tokens": 10, "output_tokens": 2}
    assert observer.public_model == "other"


@pytest.mark.parametrize("final", [{"output_tokens": 264}, {"input_tokens": 412},
                                  {"input_tokens": True, "output_tokens": 264},
                                  {"input_tokens": 412, "output_tokens": -1},
                                  {"input_tokens": 412, "output_tokens": float("nan")}])
def test_incomplete_or_invalid_final_fallback_is_unsampled_instead_of_mixed(final):
    observer, usage = complete([start(), boundary(), event("message_delta", usage=final),
                                event("message_stop")])
    assert usage is None and observer.public_model is None


def test_fallback_after_last_usage_has_no_complete_returned_attempt_to_sample():
    observer, usage = complete([start(), event("message_delta", usage={"output_tokens": 264}),
                                boundary(), event("message_stop")])
    assert usage is None and observer.public_model is None


@pytest.mark.parametrize("metadata", [None, [], SECRET, {}, [None],
    [{"type": "message"}], [{"type": "unknown", "model": START}],
    [{"type": "message", "model": SECRET}], [{"type": "message", "model": RETURNED}],
    [{"type": "message", "model": START, "output_tokens": True}],
    [{"type": "message", "model": START, "output_tokens": 265}],
    [{"type": "message", "model": START}, {"type": "message", "model": START}]])
def test_ambiguous_iterations_unprice_without_losing_otherwise_valid_numeric_usage(metadata):
    observer, usage = complete([start(), event("message_delta", usage={"output_tokens": 264,
                                                                       "iterations": metadata}),
                                event("message_stop")])
    assert usage == {**INITIAL, "output_tokens": 264} and observer.public_model == "other"


def test_ambiguous_nonfallback_iterations_do_not_waive_ordinary_monotonic_guard():
    observer, usage = complete([start(), event("message_delta", usage={**FINAL,
                                "iterations": [{"type": "message", "model": START}] * 2}),
                                event("message_stop")])
    assert usage is None and observer.public_model is None


def test_one_consistent_ordinary_iteration_keeps_known_model_priceability():
    observer, usage = complete([start(), event("message_delta", usage={"output_tokens": 264,
        "iterations": [{"type": "message", "model": START + "-20260101", "input_tokens": 535,
                        "output_tokens": 264}]}), event("message_stop")])
    assert usage == {**INITIAL, "output_tokens": 264} and observer.public_model == START


def test_unprice_flag_stays_sticky_when_later_metadata_looks_consistent():
    observer, usage = complete([start(), event("message_delta", usage={"output_tokens": 263,
                                                                       "iterations": []}),
        event("message_delta", usage={"output_tokens": 264,
              "iterations": [{"type": "message", "model": START}]}), event("message_stop")])
    assert usage == {**INITIAL, "output_tokens": 264} and observer.public_model == "other"


@pytest.mark.parametrize("blocks,metadata", [
    ([{"type": "fallback", "from": {"model": START}, "to": {"model": RETURNED}}], None),
    ([], iterations()), ([], [{"type": "fallback_message", "model": RETURNED, **FINAL}]),
    ([], [{"type": "message"}]), ([], [None]), (None, None),
])
def test_json_fallback_and_ambiguous_metadata_preserve_final_numeric_tuple(blocks, metadata):
    raw = {"type": "message", "model": RETURNED, "stop_reason": "end_turn",
           "usage": dict(FINAL), "content": blocks, "id": SECRET}
    if metadata is not None:
        raw["usage"]["iterations"] = metadata
    observer, usage = complete([json.dumps(raw).encode()], "application/json")
    assert usage == FINAL and observer.public_model == "other"


def test_json_one_consistent_ordinary_iteration_is_still_priced():
    raw = {"type": "message", "model": RETURNED, "stop_reason": "end_turn",
           "usage": {**FINAL, "iterations": [{"type": "message", "model": RETURNED, **FINAL}]},
           "content": [{"type": "text", "text": SECRET}]}
    observer, usage = complete([json.dumps(raw).encode()], "application/json")
    assert usage == FINAL and observer.public_model == RETURNED


def test_malformed_content_metadata_is_only_unpriced_and_does_not_disable_forwarding_sample():
    observer, usage = complete([start(), b'event: content_block_start\ndata: {INVALID\n\n',
                                event("message_delta", usage={"output_tokens": 264}),
                                event("message_stop")])
    assert usage == {**INITIAL, "output_tokens": 264} and observer.public_model == "other"


def test_model_change_in_final_delta_is_unpriced_even_without_fallback_block():
    observer, usage = complete([start(), event("message_delta", delta={"model": RETURNED},
                                               usage={"output_tokens": 264}),
                                event("message_stop")])
    assert usage == {**INITIAL, "output_tokens": 264} and observer.public_model == "other"


def test_actual_relay_forwards_once_records_final_tuple_and_stores_only_other(slot):
    parts = [start(), boundary(), event("message_delta", usage={**FINAL, "iterations": iterations()}),
             event("message_stop")]
    upstream = Upstream(chunks=parts)
    code, raw, _ = run(slot, upstream=upstream)
    assert code == 0 and response(raw)[1] == b"".join(parts)
    assert len(upstream.calls) == 1
    state = metrics.report(slot, relay.bound_account(slot), time.time())
    assert state["requests"] == state["token_usage"]["samples"] == 1
    assert {key: state["token_usage"][key] for key in FINAL} == FINAL
    assert state["model_usage"]["models"][0]["model"] == "other"
    saved = (slot / ".config/ccfleet" / metrics.FILE_NAME).read_text()
    assert not any(value in saved for value in (SECRET, START, RETURNED, "iterations", "fallback"))


def test_isolated_root_sibling_import_supports_fallback_without_package_or_shadow(tmp_path):
    (tmp_path / "model_usage.py").write_text("raise RuntimeError('unsafe shadow')\n")
    code = ("import importlib.util,json,sys; s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "c=m.UsageCollector('text/event-stream');c.feed(sys.stdin.buffer.read());"
            "print(json.dumps({'usage':c.finish(),'model':c.public_model}))")
    raw = b"".join([start(), boundary(), event("message_delta", usage=FINAL), event("message_stop")])
    result = subprocess.run([sys.executable, "-I", "-c", code, metrics.__file__], cwd=tmp_path,
                            input=raw, capture_output=True, timeout=5, check=True)
    assert json.loads(result.stdout) == {"usage": FINAL, "model": "other"}
    assert result.stderr == b""
