"""Finite public usage observations from synthetic current-holder snapshots."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ccfleet_agent import model_usage, relay_metrics
from ccfleetd import performance, usage
from ccfleetd.config import Config
from tests.test_performance import BOUND, NOW, SECRET, Fleet, gateway_reading, report

MODEL = "claude-sonnet-4-5"
FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def reading(samples=20, *, model=MODEL, eligible=None, cache=True, native=True, output=200,
            elapsed=20000, input_tokens=100, read_tokens=10, write_tokens=20):
    eligible = samples if eligible is None else eligible
    raw = report(max(20, eligible), max(20, eligible))
    numbers = {"samples": samples, "input_tokens": input_tokens if samples else 0,
               "output_tokens": output if samples else 0,
               "cache_read_input_tokens": read_tokens if cache and samples else 0,
               "cache_creation_input_tokens": write_tokens if cache and samples else 0,
               "cache_read_samples": samples if cache else 0,
               "cache_creation_samples": samples if cache else 0}
    raw["token_usage"] = {"eligible": eligible, **numbers}
    if model is not None:
        raw["model_usage"] = {"eligible": eligible, "samples": samples,
                              "models": [{"model": model, **numbers}] if samples else []}
    if native:
        raw["stream_output"] = {"samples": samples, "output_tokens": output if samples else 0,
                                "stream_ms": elapsed if samples else 0}
    return relay_metrics.validate_report(raw)


def fleet_of(readings, holders=None, handles=None):
    fleet = Fleet(readings, holders)
    fleet.list_accounts = lambda: [{"id": value, "handle": (handles or {}).get(value),
                                   "email": SECRET} for value in
                                  {slot["held_by"] for slot in fleet.slots}]
    return fleet


def measure(fleet, now=NOW):
    return usage.public_summary(fleet, Config(heartbeat_max_age_s=300), now)


def rate(fleet):
    return performance.summary(fleet, Config(heartbeat_max_age_s=300), NOW)["throughput"]


def test_catalogue_is_finite_authored_and_conservative_about_unknown_tiers():
    assert len(model_usage.PUBLIC_MODELS) == 16
    assert set(model_usage.LABELS) == set(model_usage.PRICES) == set(model_usage.PUBLIC_MODELS)
    assert model_usage.PRICES["claude-haiku-5-5"] is None
    assert model_usage.PRICES["other"] is None
    assert model_usage.PRICES["claude-opus-5-5"] == (4, 20, 5, 8, .2)
    assert model_usage.PRICES["claude-sonnet-4-5"] == (3, 15, 3.75, 6, .3)
    assert model_usage.LABELS["claude-fable-5-1"] == "Claude Fable 5.1"


@pytest.mark.parametrize("value", [SECRET, None, True, 7, {}, "claude-opus-4-1",
                                    "claude-sonnet-4-5\n", " claude-sonnet-4-5",
                                    "claude-sonnet-4-5:customer", "x" * 1000])
def test_model_normalization_never_returns_arbitrary_labels(value):
    assert model_usage.normalize(value) == "other"


@pytest.mark.parametrize("value", [MODEL, MODEL + "-20250929"])
def test_documented_family_and_dated_response_id_return_same_public_key(value):
    assert model_usage.normalize(value) == MODEL


@pytest.mark.parametrize("key", [key for key in model_usage.PUBLIC_MODELS if key != "other"])
def test_single_and_minor_version_families_accept_snapshot_suffix_without_retaining_it(key):
    assert model_usage.normalize(key + "-20260101") == key


def test_catalogue_is_standalone_under_isolated_python(tmp_path):
    (tmp_path / "re.py").write_text("raise RuntimeError('unsafe shadow')\n")
    code = ("import importlib.util,sys; s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(m.normalize('claude-sonnet-4-5-20250929'))")
    result = subprocess.run([sys.executable, "-I", "-c", code, model_usage.__file__],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == MODEL


def test_legacy_tokens_remain_measured_without_inferred_model_cost_or_name():
    fleet = fleet_of([reading(model=None)], handles={"holder-0": "alice"})
    result = measure(fleet)
    entry = result["entries"][0]
    assert entry["masked_handle"] == "a***e" and entry["rank"] is None
    assert result["tokens"] == {"input_tokens": 100, "output_tokens": 200,
                                "cache_read_input_tokens": 10,
                                "cache_creation_input_tokens": 20, "total_tokens": 330}
    assert entry["models"] == [] and result["model_samples"] == 0
    assert result["estimated_usd"] is None and result["cost_state"] == "unknown"
    assert result["unpriced_samples"] == result["usage_samples"] == 20
    assert result["pricing"]["historical_model_backfill"] is False


def test_known_model_estimate_uses_matched_tokens_and_explicit_five_minute_cache_assumption():
    result = measure(fleet_of([reading()], handles={"holder-0": "alice"}))
    expected = (100 * 3 + 200 * 15 + 10 * .3 + 20 * 3.75) / 1e6
    assert result["state"] == "ready" and result["estimated_usd"] == pytest.approx(expected)
    assert result["cost_state"] == "complete"
    assert result["priced_samples"] == 20 and result["unpriced_samples"] == 0
    model = result["entries"][0]["models"][0]
    assert model["model"] == MODEL and model["label"] == "Claude Sonnet 4.5"
    assert model["estimated_usd"] == result["estimated_usd"]
    assert result["pricing"]["cache_write_ttl_assumption"] == "5m"
    assert result["pricing"]["subscription_charges"] is False


@pytest.mark.parametrize("model", ["other", "claude-haiku-5-5"])
def test_unknown_or_context_tier_model_is_unpriced_not_zero(model):
    result = measure(fleet_of([reading(model=model)]))
    assert result["tokens"]["total_tokens"] == 330
    assert result["cost_state"] == "unknown" and result["estimated_usd"] is None
    assert result["entries"][0]["rank"] is None
    assert result["entries"][0]["models"][0]["estimated_usd"] is None


def test_mixed_legacy_and_new_coverage_keeps_unpriced_tokens_separate():
    result = measure(fleet_of([reading(), reading(model=None), reading(model="other")],
                             ["same"] * 3, {"same": "alice"}))
    entry = result["entries"][0]
    assert result["usage_samples"] == 60 and result["model_samples"] == 40
    assert result["priced_samples"] == 20 and result["unpriced_samples"] == 40
    assert result["tokens"]["total_tokens"] == 990
    assert sum(row["tokens"]["total_tokens"] for row in entry["models"]) == 660
    assert entry["estimated_usd"] == pytest.approx(.003378)
    assert entry["cost_state"] == "partial" and result["state"] == "partial"


def test_absent_cache_fields_are_unknown_and_never_fabricated_zero():
    result = measure(fleet_of([reading(cache=False)]))
    assert result["tokens"]["cache_read_input_tokens"] is None
    assert result["tokens"]["cache_creation_input_tokens"] is None
    assert result["tokens"]["total_tokens"] == 300 and result["token_state"] == "partial"
    assert result["cost_state"] == "partial" and result["estimated_usd"] == .0033
    assert result["priced_samples"] == 20 and result["unpriced_samples"] == 0
    assert result["cache_read_samples"] == result["cache_creation_samples"] == 0
    assert result["entries"][0]["models"][0]["token_state"] == "partial"


def test_known_zero_is_measured_zero_and_missing_usage_is_unknown():
    result = measure(fleet_of([reading(output=0, input_tokens=0, read_tokens=0, write_tokens=0)]))
    assert result["estimated_usd"] == 0 and result["cost_state"] == "complete"
    assert result["tokens"]["total_tokens"] == 0 and result["entries"][0]["rank"] == 1
    empty = measure(fleet_of([report()]))
    assert empty["state"] == "no_data" and empty["tokens"] is None and empty["entries"] == []


def test_zero_sample_known_model_never_becomes_fake_zero_cost_or_rank():
    fleet = fleet_of([reading(), reading(samples=0, eligible=1)])
    result = measure(fleet)
    unmeasured = next(row for row in result["entries"] if row["usage_samples"] == 0)
    assert unmeasured["tokens"] is None and unmeasured["estimated_usd"] is None
    assert unmeasured["models"] == [] and unmeasured["rank"] is None
    zero = {"model": MODEL, "samples": 0, "cache_read_samples": 0,
            "cache_creation_samples": 0, **dict.fromkeys(FIELDS, 0)}
    fleet.entry(1)["relay"]["model_usage"]["models"] = [zero]
    tainted = measure(fleet)
    assert tainted["reporting_slots"] == 1 and len(tainted["entries"]) == 1
    assert tainted["estimated_usd"] > 0 and tainted["state"] == "partial"
    # Defense in depth even if a future caller bypasses canonical report validation.
    row = usage._model_row(MODEL, zero)
    assert row["tokens"] is None and row["estimated_usd"] is None


def test_new_model_coverage_is_a_subset_not_a_backfilled_legacy_mix():
    raw = reading()
    raw["token_usage"] = {**raw["token_usage"], "samples": 30, "eligible": 30,
                          "input_tokens": 1000, "output_tokens": 2000,
                          "cache_read_samples": 30, "cache_creation_samples": 30}
    raw.update(requests=30, success=30, connection_errors=0)
    result = measure(fleet_of([relay_metrics.validate_report(raw)]))
    assert result["usage_samples"] == 30 and result["model_samples"] == 20
    assert result["priced_samples"] == 20 and result["unpriced_samples"] == 10
    assert result["entries"][0]["models"][0]["tokens"]["input_tokens"] == 100
    assert result["tokens"]["input_tokens"] == 1000
    assert result["cost_state"] == "partial"


@pytest.mark.parametrize("handle,expected", [("alice", "a***e"), ("erik", "e***k"),
                                          ("a", "***"), ("ab", "***"), ("abc", "***"),
                                          (None, None), (SECRET, None),
                                          ("private@example.test", None), ("<svg>", None)])
def test_only_chosen_valid_handle_is_masked(handle, expected):
    result = measure(fleet_of([reading()], handles={"holder-0": handle}))
    assert result["entries"][0]["masked_handle"] == expected


@pytest.mark.parametrize("change", [
    lambda f: f.beats["node-0"].update(ts=NOW - 301),
    lambda f: f.entry()["relay"].update(observed_at=NOW - 301),
    lambda f: f.entry()["credentials"].update(account_fp="fedcba9876543210"),
    lambda f: f.entry()["credentials"].update(logged_in=False),
    lambda f: f.entry().update(provisioned_for=NOW - 86401),
    lambda f: f.slots[0].update(account_switched_at=NOW + 1),
    lambda f: f.nodes[0].update(enabled=0),
    lambda f: f.slots[0].update(state="free"),
])
def test_usage_and_native_rate_require_fresh_current_holder_identity(change):
    fleet = fleet_of([reading()])
    change(fleet)
    result = measure(fleet)
    assert result["usage_samples"] == 0 and result["entries"] == []
    assert rate(fleet)["state"] == "no_data"


def test_ranking_is_cost_descending_unknown_last_and_ties_share_rank():
    readings = [reading(output=value, model=model) for value, model in
                [(200, MODEL), (9999, "other"), (400, MODEL), (400, MODEL)]]
    result = measure(fleet_of(readings))
    rows = result["entries"]
    assert [row["rank"] for row in rows] == [1, 1, 3, None]
    assert rows[0]["estimated_usd"] == rows[1]["estimated_usd"] > rows[2]["estimated_usd"]


def test_top_list_is_bounded_with_explicit_omission_and_global_totals():
    result = measure(fleet_of([reading() for _ in range(53)]))
    assert len(result["entries"]) == 50 and result["omitted_entries"] == 3
    assert result["usage_samples"] == 53 * 20


def test_public_dto_has_no_raw_identity_or_private_payload_and_does_not_mutate():
    fleet = fleet_of([reading()], handles={"holder-0": "alice"})
    before = copy.deepcopy((fleet.slots, fleet.beats))
    result = measure(fleet)
    raw = json.dumps(result, allow_nan=False)
    for secret in (SECRET, BOUND, "holder-", "node-", "slot-id-", "alice", "email", "bound_fp",
                   "observed_at", "account_switched_at", "unix_user", "access_host"):
        assert secret not in raw
    assert (fleet.slots, fleet.beats) == before
    entry = result["entries"][0]
    assert set(entry) == {"rank", "masked_handle", "member_code", *usage.COUNTERS,
                          "unpriced_samples", "tokens",
                          "priced_tokens", "priced_token_state", "unpriced_tokens",
                          "unpriced_token_state",
                          "token_state", "estimated_usd", "cost_state", "models"}


@pytest.mark.parametrize("method", ["list_slots", "latest_heartbeats", "list_nodes"])
def test_store_errors_do_not_break_homepage_or_disclose_exception(method):
    fleet = fleet_of([reading()])
    def broken():
        raise ValueError(SECRET)
    setattr(fleet, method, broken)
    result = measure(fleet)
    assert result["state"] == "no_data" and SECRET not in json.dumps(result)


def test_account_lookup_failure_preserves_measurements_with_generic_handle():
    fleet = fleet_of([reading()])
    def broken():
        raise ValueError(SECRET)
    fleet.list_accounts = broken
    result = measure(fleet)
    assert result["state"] == "ready" and result["entries"][0]["masked_handle"] is None


def test_public_member_codes_are_stable_purpose_specific_and_independent_of_email():
    fleet = fleet_of([reading(), reading()], holders=["holder-a", "holder-b"])
    cfg = Config(heartbeat_max_age_s=300, cookie_secret="PRIVATE_SIGNING_SECRET")
    first = usage.public_summary(fleet, cfg, NOW)
    codes = [entry["member_code"] for entry in first["entries"]]
    assert len(set(codes)) == 2
    assert all(isinstance(code, str) and len(code) == 8 and code.isascii()
               and code.isdigit() for code in codes)
    assert [entry["member_code"] for entry in usage.public_summary(fleet, cfg, NOW)["entries"]] == codes
    changed = usage.public_summary(fleet, Config(heartbeat_max_age_s=300,
                                  cookie_secret="DIFFERENT_SIGNING_SECRET"), NOW)
    assert [entry["member_code"] for entry in changed["entries"]] != codes
    raw = json.dumps(first)
    assert "PRIVATE_SIGNING_SECRET" not in raw and SECRET not in raw and "holder-" not in raw
    fleet.list_accounts = lambda: []
    assert [entry["member_code"] for entry in usage.public_summary(fleet, cfg, NOW)["entries"]] == codes


def test_missing_public_member_secret_does_not_hash_private_ids_with_a_public_salt():
    assert _first_member_code(Config()) is None


def _first_member_code(cfg):
    return usage.public_summary(fleet_of([reading()]), cfg, NOW)["entries"][0]["member_code"]


@pytest.mark.parametrize("now", [True, -1, float("nan"), float("inf")])
def test_invalid_clock_is_nonfatal_unknown(now):
    assert measure(fleet_of([reading()]), now)["state"] == "no_data"


def test_duplicate_slot_snapshot_is_not_double_counted_or_assigned_to_either_holder():
    fleet = fleet_of([reading()])
    fleet.slots.append({**fleet.slots[0], "held_by": "another-private-holder"})
    assert measure(fleet)["usage_samples"] == 0
    assert rate(fleet)["state"] == "no_data"


def test_native_only_rate_works_without_gateway_support_and_has_no_invented_start_date():
    fleet = fleet_of([reading(output=1000, elapsed=10000)])
    result = rate(fleet)
    assert result == {"state": "ready", "samples": 20, "tokens_per_second": 100,
                      "window_start_day": gateway_reading()["window_start_day"],
                      "collection_start_day": None}
    assert "output_tokens" not in result and "stream_ms" not in result


def test_mixed_rate_is_ratio_of_disjoint_native_and_gateway_sums():
    fleet = fleet_of([reading(samples=10, output=1000, elapsed=10000)])
    fleet.gateway_throughput_summary = lambda _: gateway_reading(10, 2000, 100000)
    result = rate(fleet)
    assert result["state"] == "ready" and result["samples"] == 20
    assert result["tokens_per_second"] == pytest.approx(3000 / 110)
    assert result["collection_start_day"] is None


def test_zero_native_contribution_preserves_known_gateway_start_day():
    fleet = fleet_of([reading(samples=0)])
    fleet.gateway_throughput_summary = lambda _: gateway_reading()
    assert rate(fleet)["collection_start_day"] == gateway_reading()["collection_start_day"]


@pytest.mark.parametrize("samples,state", [(0, "no_data"), (19, "limited_data"), (20, "ready")])
def test_native_minimum_samples_are_independent_and_hide_partial_counts(samples, state):
    result = rate(fleet_of([reading(samples=samples)]))
    assert result["state"] == state
    assert result["samples"] == (20 if state == "ready" else None)
    assert result["tokens_per_second"] == (10 if state == "ready" else None)


def test_measured_zero_native_output_is_real_zero_rate():
    assert rate(fleet_of([reading(output=0)]))["tokens_per_second"] == 0


@pytest.mark.parametrize("corrupt_gateway", [None, {}, {"samples": True},
                                            {**gateway_reading(), "stream_ms": float("nan")},
                                            {**gateway_reading(), "collection_start_day": None}])
def test_corrupt_gateway_does_not_suppress_valid_native_rate(corrupt_gateway):
    fleet = fleet_of([reading()])
    fleet.gateway_throughput_summary = lambda _: corrupt_gateway
    assert rate(fleet)["tokens_per_second"] == 10


def test_failing_gateway_does_not_suppress_valid_native_rate():
    fleet = fleet_of([reading()])
    def broken(_):
        raise ValueError(SECRET)
    fleet.gateway_throughput_summary = broken
    assert rate(fleet)["state"] == "ready"


@pytest.mark.parametrize("field,value", [("samples", True), ("output_tokens", -1),
                                         ("stream_ms", float("nan")), ("stream_ms", 1999)])
def test_corrupt_native_snapshot_does_not_suppress_valid_gateway(field, value):
    fleet = fleet_of([reading()])
    fleet.entry()["relay"]["stream_output"][field] = value
    fleet.gateway_throughput_summary = lambda _: gateway_reading()
    assert rate(fleet)["tokens_per_second"] == 40
    assert measure(fleet)["usage_samples"] == 0


def test_gateway_only_node_report_does_not_add_same_stream_twice():
    fleet = fleet_of([reading(native=False)])
    fleet.gateway_throughput_summary = lambda _: gateway_reading()
    assert rate(fleet)["samples"] == 20 and rate(fleet)["tokens_per_second"] == 40


def test_public_rate_overflow_is_unknown_instead_of_infinite_or_wrapped():
    source = {"samples": 1, "output_tokens": 1, "stream_ms": 100}
    fake = [{"stream_output": source}] * 2
    original = performance.MAX_THROUGHPUT_TOTAL
    try:
        performance.MAX_THROUGHPUT_TOTAL = 150
        result = performance._throughput_summary(Fleet(), NOW, gateway_reading()["window_start_day"], fake)
        assert result["state"] == "no_data" and result["samples"] is None
    finally:
        performance.MAX_THROUGHPUT_TOTAL = original


def test_usage_overflow_is_unknown_not_wrapped(monkeypatch):
    monkeypatch.setattr(usage, "MAX_COUNTER", 200)
    result = measure(fleet_of([reading(), reading()]))
    assert result["state"] == "no_data" and result["estimated_usd"] is None


def test_owned_source_files_exist_for_installer_and_server():
    assert Path(model_usage.__file__).is_file()
    assert Path(usage.__file__).is_file()
