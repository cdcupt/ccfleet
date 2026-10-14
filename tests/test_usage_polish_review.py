"""Independent cost-scope and compact-display checks; synthetic telemetry only."""

import copy
import json
import time
from html.parser import HTMLParser

import pytest

from ccfleet_agent import local_relay, relay_metrics
from ccfleetd import slots, usage, usersite
from ccfleetd.bilingual import project_html
from tests import test_local_relay
from tests.test_local_relay import Upstream, response, run
from tests.test_performance import NOW, report
from tests.test_public_usage import fleet_of, measure, reading

MODEL = "claude-opus-5-5"
FIELDS = usage.TOKEN_FIELDS
slot = test_local_relay.slot


class Parsed(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.words = []
        self.attrs = []
        self.estimate_words = []
        self.in_estimate = False
        self.feed(source)

    def handle_data(self, data):
        self.words.append(data)
        if self.in_estimate:
            self.estimate_words.append(data)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.attrs.append((tag, attributes))
        if tag == "td" and "usage-estimate" in attributes.get("class", "").split():
            self.in_estimate = True

    def handle_endtag(self, tag):
        if tag == "td":
            self.in_estimate = False

    def text(self):
        return " ".join(" ".join(self.words).split())

    def estimate_text(self):
        return " ".join(" ".join(self.estimate_words).split())


def exact_subset():
    # The unpriced mix is deliberately synthetic: its true historical model and
    # category mix is not inferred. Only its observed subtotal is reproduced.
    known = {"input_tokens": 1207, "output_tokens": 5025,
             "cache_read_input_tokens": 1054178, "cache_creation_input_tokens": 140393}
    raw = report(6118, 6118)
    numbers = {**known, "cache_read_input_tokens": known["cache_read_input_tokens"] + 2145853715}
    raw["token_usage"] = {"eligible": 6118, "samples": 6118, **numbers,
                          "cache_read_samples": 6118, "cache_creation_samples": 6118}
    raw["model_usage"] = {"eligible": 11, "samples": 11, "models": [{
        "model": MODEL, "samples": 11, **known,
        "cache_read_samples": 11, "cache_creation_samples": 11}]}
    return relay_metrics.validate_report(raw)


def test_historical_one_dollar_estimate_is_for_one_point_two_million_priced_tokens_only():
    result = measure(fleet_of([exact_subset()]))
    for scope in (result, result["entries"][0]):
        assert scope["tokens"]["total_tokens"] == 2147054518
        assert scope["priced_tokens"] == {"input_tokens": 1207, "output_tokens": 5025,
            "cache_read_input_tokens": 1054178, "cache_creation_input_tokens": 140393,
            "total_tokens": 1200803}
        assert scope["unpriced_tokens"]["total_tokens"] == 2145853715
        assert scope["priced_samples"] == 11 and scope["unpriced_samples"] == 6107
        assert scope["priced_token_state"] == scope["unpriced_token_state"] == "complete"
        assert scope["cost_state"] == "partial"
        # Official standard Opus5.5 USD: .004828+.1005+.2108356+.701965.
        assert scope["estimated_usd"] == pytest.approx(1.0181286, abs=1e-12)
        assert scope["priced_tokens"]["total_tokens"] + scope["unpriced_tokens"][
            "total_tokens"] == scope["tokens"]["total_tokens"]


def test_priced_and_unpriced_categories_are_additive_matched_observed_subtotals():
    readings = [reading(model=MODEL), reading(model=None), reading(model="other")]
    result = measure(fleet_of(readings, holders=["same"] * 3))
    row = result["entries"][0]
    for field in (*FIELDS, "total_tokens"):
        assert row["tokens"][field] == row["priced_tokens"][field] + row["unpriced_tokens"][field]
        assert result["priced_tokens"][field] == row["priced_tokens"][field]
        assert result["unpriced_tokens"][field] == row["unpriced_tokens"][field]
    assert row["priced_tokens"]["total_tokens"] == 330
    assert row["unpriced_tokens"]["total_tokens"] == 660
    assert row["estimated_usd"] == pytest.approx(.004502)


@pytest.mark.parametrize("model", [None, "other", "claude-haiku-5-5"])
def test_unpriced_models_have_unknown_priced_scope_not_zero(model):
    result = measure(fleet_of([reading(model=model)]))
    assert result["priced_tokens"] is None and result["priced_token_state"] == "unknown"
    assert result["unpriced_tokens"] == result["tokens"]
    assert result["unpriced_tokens"]["total_tokens"] == 330
    assert result["estimated_usd"] is None


def test_measured_zero_token_partition_is_distinct_from_missing_observations():
    zero = measure(fleet_of([reading(model=MODEL, output=0, input_tokens=0,
                                    read_tokens=0, write_tokens=0)]))
    assert zero["priced_tokens"]["total_tokens"] == zero["unpriced_tokens"]["total_tokens"] == 0
    assert zero["estimated_usd"] == 0 and zero["cost_state"] == "complete"
    unknown = measure(fleet_of([report()]))
    assert unknown["priced_tokens"] is unknown["unpriced_tokens"] is None
    assert unknown["priced_token_state"] == unknown["unpriced_token_state"] == "unknown"


def test_partial_cache_scope_keeps_unknown_categories_without_adding_cache_to_input_twice():
    result = measure(fleet_of([reading(model=MODEL, cache=False)]))
    assert result["priced_tokens"] == {"input_tokens": 100, "output_tokens": 200,
        "cache_read_input_tokens": None, "cache_creation_input_tokens": None, "total_tokens": 300}
    assert result["unpriced_tokens"]["total_tokens"] == 0
    assert result["unpriced_tokens"]["cache_read_input_tokens"] is None
    assert result["priced_token_state"] == result["unpriced_token_state"] == "partial"
    assert result["estimated_usd"] == .0044


def test_corrupt_priced_scope_cannot_exceed_observed_tokens_or_expose_negative_remainder():
    raw = exact_subset()
    raw["model_usage"]["models"][0]["input_tokens"] = raw["token_usage"]["input_tokens"] + 1
    result = measure(fleet_of([raw]))
    assert result["priced_tokens"] is result["unpriced_tokens"] is result["estimated_usd"] is None
    assert result["entries"] == []


@pytest.mark.parametrize("count,compact", [(0, "0"), (999, "999"), (1000, "1K"),
    (1005, "1.01K"), (1234, "1.23K"), (999994, "999.99K"), (999995, "1M"),
    (1000000, "1M"), (1005000, "1.01M"), (999994000, "999.99M"),
    (999995000, "1B"), (2147054518, "2.15B")])
def test_compact_formatter_uses_half_up_rounding_and_promotes_large_unit_boundaries(count, compact):
    raw = usersite._public_usage_token(count)
    english = Parsed(project_html(raw, "en", "/"))
    chinese = Parsed(project_html(raw, "zh", "/"))
    assert english.text() == chinese.text() == compact
    exact = f"{count:,}"
    for shown in (english, chinese):
        attrs = [attrs for _, attrs in shown.attrs]
        assert any(exact in value for attrs in attrs for key, value in attrs.items() if key == "title")
        assert any(exact in value for attrs in attrs for key, value in attrs.items() if key == "aria-label")


@pytest.mark.parametrize("count", [None, True, False, -1, "1000", 1.0, float("nan"),
                                  float("inf"), 2**63, "<script>alert(1)</script>"])
def test_invalid_token_display_is_unknown_without_html_or_numeric_coercion(count):
    raw = usersite._public_usage_token(count)
    assert Parsed(project_html(raw, "en", "/")).text() == "—"
    assert "<script" not in raw


def test_rendered_partial_estimate_names_the_matching_priced_tokens_and_separate_unpriced_scope():
    result = measure(fleet_of([exact_subset()]))
    before = copy.deepcopy(result)
    raw = usersite.public_usage_panel(result)
    english = Parsed(project_html(raw, "en", "/")).text()
    chinese = Parsed(project_html(raw, "zh", "/")).text()
    assert "$1.02" in english and "1.2M" in english
    assert "priced tokens" in english and "Unpriced" in english
    assert "2.15B" in english and "for all" not in english
    assert "$1.02" in chinese and "1.2M" in chinese and "2.15B" in chinese
    assert result == before


def test_money_is_unknown_when_matching_token_scope_is_missing_or_invalid():
    result = measure(fleet_of([exact_subset()]))
    for value in (None, {}, {"total_tokens": True}, {"total_tokens": "1200803"}):
        changed = copy.deepcopy(result)
        changed["entries"][0]["priced_tokens"] = value
        parsed = Parsed(project_html(usersite.public_usage_panel(changed), "en", "/"))
        # Model details may independently retain a valid scoped estimate.
        assert "$1.02" not in parsed.estimate_text()


def test_untrusted_public_fields_cannot_inject_html_or_become_full_identity_labels():
    result = measure(fleet_of([exact_subset()]))
    row = result["entries"][0]
    injected = '<script>alert("private-email@example.test")</script>'
    row.update(masked_handle=injected, email=injected, nickname=injected)
    row["models"][0].update(model=injected, label=injected)
    result["pricing"].update(url="javascript:alert(1)", date=injected)
    raw = usersite.public_usage_panel(result)
    assert injected not in raw and "private-email@example.test" not in raw
    assert "javascript:" not in raw and "<script" not in raw


def event(kind, **fields):
    return (f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


@pytest.mark.parametrize("duplicates", [1, 50])
def test_full_collector_to_daily_file_records_final_cumulative_usage_once(slot, monkeypatch, duplicates):
    core = {"input_tokens": 20, "cache_read_input_tokens": 350000,
            "cache_creation_input_tokens": 5000, "output_tokens": 9}
    chunks = [event("message_start", message={"model": MODEL, "usage": {**core, "output_tokens": 1}}),
              event("message_delta", usage={**core, "output_tokens": 5}),
              *[event("message_delta", usage=core)] * duplicates, event("message_stop")]
    calls = []
    original = relay_metrics.record
    def counted(*args, **kwargs):
        calls.append(kwargs.get("usage"))
        return original(*args, **kwargs)
    monkeypatch.setattr(relay_metrics, "record", counted)
    code, raw, upstream = run(slot, upstream=Upstream(chunks=chunks))
    assert code == 0 and len(upstream.calls) == 1 and response(raw)[1] == b"".join(chunks)
    assert calls == [core]
    binding = local_relay.bound_account(slot)
    saved = slot / ".config/ccfleet" / relay_metrics.FILE_NAME
    original_bytes = saved.read_bytes()
    for _ in range(25):
        current = relay_metrics.report(slot, binding, time.time())
        assert current["token_usage"]["samples"] == 1
        assert {key: current["token_usage"][key] for key in FIELDS} == core
    assert saved.read_bytes() == original_bytes


def test_numeric_recording_crosses_two_to_the_thirty_one_without_saturation_or_wrap(slot):
    binding = local_relay.bound_account(slot)
    now = time.time()
    # These synthetic counts test numeric storage limits only; they are not a
    # claim that a provider supports a billion-token request.
    for read in (1000000000, 1000000000, 147483647, 11):
        relay_metrics.record(slot, binding, "success", {}, now, usage_eligible=True,
                             usage={"input_tokens": 0, "output_tokens": 0,
                                    "cache_read_input_tokens": read,
                                    "cache_creation_input_tokens": 0})
    measured = relay_metrics.report(slot, binding, now)
    assert measured["token_usage"]["samples"] == 4
    assert measured["token_usage"]["cache_read_input_tokens"] == 2**31 + 10
    assert type(measured["token_usage"]["cache_read_input_tokens"]) is int


def test_repeated_received_heartbeats_and_page_reads_never_sum_the_same_snapshot_history(store):
    store.add_node("synthetic-node", "operator", now=NOW - 86400)
    store.add_account("synthetic-holder", "synthetic-sub", "synthetic@example.test",
                      slot_quota=1, now=NOW - 86400)
    store.add_slot("synthetic-slot", "synthetic-node", "slot01", now=NOW - 86400)
    store.apply_slot_report("synthetic-node", [{"unix_user": "slot01", "present": False}],
                            now=NOW - 86400)
    selected = store.claim_slot("synthetic-holder", node_id="synthetic-node", now=NOW - 3600)
    store.move_slot(selected["id"], slots.CLAIMED)
    store.move_slot(selected["id"], slots.ACTIVE)
    payload = fleet_of([reading(samples=1, native=False)]).beats["node-0"]["payload"]
    payload["node_id"] = "synthetic-node"
    payload["slots"][0].update(unix_user="slot01", provisioned_for=selected["claimed_at"])
    for _ in range(50):
        store.insert_heartbeat("synthetic-node", NOW, payload)
    for _ in range(25):
        result = measure(store)
        assert result["usage_samples"] == result["priced_samples"] == 1
        assert result["tokens"]["total_tokens"] == 330
    payload["slots"][0]["relay"] = reading(samples=2, native=False)
    store.insert_heartbeat("synthetic-node", NOW, payload)
    assert measure(store)["usage_samples"] == 2


@pytest.mark.parametrize("marker", ["block", "iterations", "both"])
def test_official_midstream_fallback_uses_returned_attempt_numbers_and_never_start_model_price(marker):
    final = {"input_tokens": 412, "output_tokens": 264,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    initial = {"input_tokens": 535, "output_tokens": 20,
               "cache_read_input_tokens": 350000, "cache_creation_input_tokens": 1000}
    chunks = [event("message_start", message={"model": MODEL, "usage": initial})]
    if marker in {"block", "both"}:
        chunks += [event("content_block_start", index=1, content_block={
            "type": "fallback", "from": {"model": MODEL},
            "to": {"model": "claude-opus-4-8"}}), event("content_block_stop", index=1)]
    usage_ = dict(final)
    if marker in {"iterations", "both"}:
        usage_["iterations"] = [{"type": "message", "model": MODEL, **initial},
            {"type": "fallback_message", "model": "claude-opus-4-8", **final}]
    chunks += [event("message_delta", usage=usage_), event("message_stop")]
    observer = relay_metrics.UsageCollector("text/event-stream")
    for part in chunks:
        observer.feed(part)
    assert observer.finish() == final
    assert observer.public_model == "other"
    assert not observer.buffer and not observer.data and observer.numbers == {}


@pytest.mark.parametrize("marker", ["block", "iterations", "both"])
def test_json_fallback_and_sticky_iterations_preserve_numeric_usage_without_model_attribution(marker):
    final = {"input_tokens": 412, "output_tokens": 264,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    usage_ = dict(final)
    body = {"type": "message", "model": "claude-opus-4-8", "stop_reason": "end_turn",
            "usage": usage_, "content": []}
    if marker in {"block", "both"}:
        body["content"].append({"type": "fallback", "from": {"model": MODEL},
                                "to": {"model": "claude-opus-4-8"}})
    if marker in {"iterations", "both"}:
        usage_["iterations"] = [{"type": "fallback_message", "model": "claude-opus-4-8",
                                  **final}]
    observer = relay_metrics.UsageCollector("application/json")
    observer.feed(json.dumps(body).encode())
    assert observer.finish() == final and observer.public_model == "other"


def test_ordinary_text_mentioning_fallback_is_never_classified_from_content():
    final = {"input_tokens": 412, "output_tokens": 264,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    body = {"type": "message", "model": MODEL, "stop_reason": "end_turn", "usage": final,
            "content": [{"type": "text", "text": 'synthetic {"type":"fallback"} text'}]}
    observer = relay_metrics.UsageCollector("application/json")
    observer.feed(json.dumps(body).encode())
    assert observer.finish() == final and observer.public_model == MODEL
