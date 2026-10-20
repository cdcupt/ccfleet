"""The price and token subtotal must describe the same observed categories."""

import json

import pytest

from ccfleet_agent import relay_metrics
from ccfleetd import usage
from tests.test_public_usage import FIELDS, MODEL, fleet_of, measure, reading


def assert_partition(result):
    total, priced, unpriced = (result[key] for key in ("tokens", "priced_tokens", "unpriced_tokens"))
    if total is None:
        assert priced is unpriced is None
        return
    assert unpriced is not None
    for field in (*FIELDS, "total_tokens"):
        if total[field] is None:
            assert unpriced[field] is None
        else:
            assert total[field] == ((priced or {}).get(field) or 0) + unpriced[field]
    assert total["total_tokens"] == (priced or {}).get("total_tokens", 0) + unpriced["total_tokens"]


def test_exact_historical_total_and_one_dollar_price_are_distinct_matching_populations():
    # Synthetic snapshot reproduces the observed historical totals and matched
    # Opus row. Unlabelled tokens are placed in one synthetic numeric category;
    # this does not infer the historical production model/cache distribution.
    legacy = 2_145_853_715
    matched = {"input_tokens": 1207, "output_tokens": 5025,
               "cache_read_input_tokens": 1_054_178, "cache_creation_input_tokens": 140_393}
    raw = reading(samples=11, model="claude-opus-5-5", native=False,
                  input_tokens=matched["input_tokens"], output=matched["output_tokens"],
                  read_tokens=matched["cache_read_input_tokens"],
                  write_tokens=matched["cache_creation_input_tokens"])
    raw.update(requests=6118, success=6118, connection_errors=0)
    raw["token_usage"].update(samples=6118, eligible=6118,
                              input_tokens=legacy + matched["input_tokens"],
                              cache_read_samples=6118, cache_creation_samples=6118)
    result = measure(fleet_of([relay_metrics.validate_report(raw)]))
    assert result["tokens"]["total_tokens"] == 2_147_054_518
    assert result["priced_tokens"] == {**matched, "total_tokens": 1_200_803}
    assert result["unpriced_tokens"]["total_tokens"] == legacy
    assert result["usage_samples"] == 6118 and result["priced_samples"] == 11
    assert result["unpriced_samples"] == 6107
    assert result["estimated_usd"] == pytest.approx(1.0181286)
    assert f'{result["estimated_usd"]:.2f}' == "1.02"
    assert result["cost_state"] == "partial" and result["priced_token_state"] == "complete"
    assert result["entries"][0]["priced_tokens"] == result["priced_tokens"]
    assert result["entries"][0]["unpriced_tokens"] == result["unpriced_tokens"]
    assert result["entries"][0]["estimated_usd"] == result["estimated_usd"]
    assert_partition(result)


def test_known_unknown_and_legacy_models_partition_only_actual_priced_rows():
    result = measure(fleet_of([reading(), reading(model="other"), reading(model=None)],
                             holders=["same"] * 3))
    assert result["tokens"]["total_tokens"] == 990
    assert result["priced_tokens"]["total_tokens"] == 330
    assert result["unpriced_tokens"]["total_tokens"] == 660
    assert result["priced_token_state"] == result["unpriced_token_state"] == "complete"
    assert result["cost_state"] == "partial"
    assert result["entries"][0]["priced_tokens"] == result["priced_tokens"]
    assert_partition(result)


@pytest.mark.parametrize("model", [None, "other", "claude-haiku-5-5"])
def test_unpriced_legacy_unknown_and_tiered_usage_is_not_fake_zero_price(model):
    result = measure(fleet_of([reading(model=model)]))
    assert result["priced_tokens"] is None and result["priced_token_state"] == "unknown"
    assert result["unpriced_tokens"] == result["tokens"]
    assert result["unpriced_token_state"] == "complete"
    assert result["estimated_usd"] is None and result["entries"][0]["rank"] == 1
    assert_partition(result)


def test_all_known_observed_tokens_are_priced_and_unpriced_zero_is_measured():
    result = measure(fleet_of([reading()]))
    assert result["priced_tokens"] == result["tokens"]
    assert result["unpriced_tokens"] == {**dict.fromkeys(FIELDS, 0), "total_tokens": 0}
    assert result["priced_token_state"] == result["unpriced_token_state"] == "complete"
    assert result["cost_state"] == "complete"
    assert_partition(result)


def test_positive_samples_with_zero_tokens_are_real_zero_not_unknown():
    result = measure(fleet_of([reading(input_tokens=0, output=0, read_tokens=0, write_tokens=0)]))
    assert result["priced_samples"] == 20 and result["priced_tokens"]["total_tokens"] == 0
    assert result["unpriced_tokens"]["total_tokens"] == 0
    assert result["estimated_usd"] == 0 and result["priced_token_state"] == "complete"
    assert_partition(result)


def test_unsampled_and_empty_inputs_keep_both_token_partitions_unknown():
    for fleet in (fleet_of([]), fleet_of([reading(samples=0, eligible=1)])):
        result = measure(fleet)
        assert result["priced_tokens"] is result["unpriced_tokens"] is None
        assert result["priced_token_state"] == result["unpriced_token_state"] == "unknown"
        assert result["estimated_usd"] is None
        assert_partition(result)


def test_missing_caches_are_partial_observed_subtotals_not_zero_cache_claims():
    result = measure(fleet_of([reading(cache=False)]))
    assert result["priced_tokens"] == {"input_tokens": 100, "output_tokens": 200,
                                       "cache_read_input_tokens": None,
                                       "cache_creation_input_tokens": None, "total_tokens": 300}
    assert result["unpriced_tokens"] == {"input_tokens": 0, "output_tokens": 0,
                                         "cache_read_input_tokens": None,
                                         "cache_creation_input_tokens": None, "total_tokens": 0}
    assert result["priced_token_state"] == result["unpriced_token_state"] == "partial"
    assert result["estimated_usd"] == pytest.approx(.0033)
    assert result["cost_state"] == "partial"
    assert_partition(result)


def test_observed_cache_without_matched_model_cache_stays_unpriced_even_on_priced_samples():
    raw = reading()
    row = raw["model_usage"]["models"][0]
    row.update(cache_read_samples=0, cache_creation_samples=0,
               cache_read_input_tokens=0, cache_creation_input_tokens=0)
    result = measure(fleet_of([relay_metrics.validate_report(raw)]))
    assert result["priced_samples"] == 20 and result["unpriced_samples"] == 0
    assert result["priced_tokens"]["total_tokens"] == 300
    assert result["priced_tokens"]["cache_read_input_tokens"] is None
    assert result["unpriced_tokens"]["total_tokens"] == 30
    assert result["unpriced_tokens"]["cache_read_input_tokens"] == 10
    assert result["unpriced_tokens"]["cache_creation_input_tokens"] == 20
    assert result["priced_token_state"] == "partial"
    assert result["unpriced_token_state"] == "complete"
    assert result["estimated_usd"] == pytest.approx(.0033)
    assert_partition(result)


def test_cache_categories_are_added_once_and_price_matches_each_separate_rate():
    result = measure(fleet_of([reading(input_tokens=100, output=30, read_tokens=200,
                                      write_tokens=40)]))
    assert result["priced_tokens"]["total_tokens"] == 370
    assert result["estimated_usd"] == pytest.approx(.00096)
    assert result["priced_tokens"]["total_tokens"] == sum(
        result["priced_tokens"][field] for field in FIELDS)
    assert_partition(result)


def test_top_level_is_sum_of_holder_categories_and_does_not_assume_one_model_mix():
    result = measure(fleet_of([reading(model=MODEL), reading(model="claude-opus-5-5"),
                              reading(model="other")]))
    assert result["priced_samples"] == 40 and result["unpriced_samples"] == 20
    assert result["priced_tokens"]["total_tokens"] == sum(
        (row["priced_tokens"] or {}).get("total_tokens", 0) for row in result["entries"])
    assert result["unpriced_tokens"]["total_tokens"] == sum(
        row["unpriced_tokens"]["total_tokens"] for row in result["entries"])
    assert result["estimated_usd"] == pytest.approx(sum(
        row["estimated_usd"] or 0 for row in result["entries"]))
    assert_partition(result)


@pytest.mark.parametrize("bad", [True, -1, relay_metrics.MAX_COUNTER + 1])
def test_invalid_category_never_publishes_estimate_or_partition(bad):
    fleet = fleet_of([reading()])
    fleet.entry()["relay"]["model_usage"]["models"][0]["input_tokens"] = bad
    result = measure(fleet)
    assert result["tokens"] is result["priced_tokens"] is result["unpriced_tokens"] is None
    assert result["estimated_usd"] is None
    json.dumps(result, allow_nan=False)


def test_aggregate_overflow_returns_unknown_without_publishing_partial_arithmetic(monkeypatch):
    monkeypatch.setattr(usage, "MAX_COUNTER", 400)
    result = measure(fleet_of([reading(), reading()]))
    assert result["state"] == "no_data" and result["estimated_usd"] is None
    assert result["priced_tokens"] is result["unpriced_tokens"] is None


def test_stale_binding_does_not_get_matching_priced_categories():
    fleet = fleet_of([reading()])
    fleet.entry()["credentials"]["account_fp"] = "fedcba9876543210"
    result = measure(fleet)
    assert result["entries"] == [] and result["priced_tokens"] is None
