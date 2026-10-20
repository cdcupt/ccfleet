"""One comparison price list must not pretend to recover historical models."""

from copy import deepcopy

import pytest

from ccfleetd import usersite
from ccfleetd.bilingual import project_html
from tests.test_public_usage import fleet_of, measure, reading
from tests.test_ranking_cohort_review import Table


def main(data, language="en"):
    return Table(project_html(usersite.public_usage_panel(data), language, "/")).main()


def test_identical_recorded_categories_rank_equally_without_rewriting_actual_model_estimates():
    models = (None, "other", "claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-4-5")
    data = measure(fleet_of([reading(model=model, input_tokens=1_000_000, output=1_000_000,
        read_tokens=1_000_000, write_tokens=1_000_000) for model in models]))
    assert [row["rank"] for row in data["entries"]] == [1] * 5
    for row in data["entries"]:
        assert row["reference_estimated_usd"] == 29.2
        assert row["reference_tokens"] == row["tokens"]
        assert row["reference_tokens"] is not row["tokens"]
        assert row["reference_cost_state"] == "complete"
    actual = [row["estimated_usd"] for row in data["entries"]]
    assert actual.count(None) == 3 and 29.2 in actual and 22.05 in actual
    assert data["reference_estimated_usd"] == 146
    assert data["estimated_usd"] == 51.25
    assert data["priced_samples"] == 40 and data["usage_samples"] == 100


def test_cache_only_categories_have_their_own_rates_and_are_not_input_again():
    data = measure(fleet_of([reading(model=None, input_tokens=0, output=0,
                                    read_tokens=1_000_000, write_tokens=1_000_000)]))
    row = data["entries"][0]
    assert row["reference_estimated_usd"] == 5.2
    assert row["reference_tokens"]["total_tokens"] == 2_000_000
    assert "$5.20" in main(data) and "2M" in main(data)
    assert row["estimated_usd"] is None and row["models"] == []


def test_zero_measurement_has_a_rank_and_zero_reference_but_absent_measurement_has_neither():
    data = measure(fleet_of([reading(model=None, input_tokens=0, output=0,
        read_tokens=0, write_tokens=0), reading(model=None, samples=0, eligible=1)]))
    zero, absent = data["entries"]
    assert zero["rank"] == 1 and zero["reference_estimated_usd"] == 0
    assert zero["reference_cost_state"] == "complete"
    assert absent["rank"] is None and absent["reference_estimated_usd"] is None
    assert absent["reference_tokens"] is None and absent["reference_cost_state"] == "unknown"
    assert "$0.00" in main(data) and "No measurement yet" in main(data)


def test_missing_cache_categories_are_partial_observed_subtotals_without_synthetic_counts():
    data = measure(fleet_of([reading(model=None, cache=False, input_tokens=1_000_000,
                                    output=1_000_000)]))
    row = data["entries"][0]
    assert row["reference_estimated_usd"] == 24
    assert row["reference_tokens"]["total_tokens"] == 2_000_000
    assert row["reference_tokens"]["cache_read_input_tokens"] is None
    assert row["reference_tokens"]["cache_creation_input_tokens"] is None
    assert row["reference_cost_state"] == "partial"
    assert "$24.00" in main(data) and "Partial" in main(data)


def test_missing_one_reply_is_partial_even_when_every_observed_reply_has_cache_categories():
    # Synthetic reproduction of a complete category subtotal with one unmeasured reply.
    data = measure(fleet_of([reading(model=None, samples=216, eligible=217, native=False,
        input_tokens=8690, output=306169, read_tokens=69311551, write_tokens=8251050)]))
    row = data["entries"][0]
    assert row["reference_tokens"]["total_tokens"] == 77_877_460
    assert row["reference_estimated_usd"] == pytest.approx(61.2757002, abs=1e-12)
    assert row["reference_cost_state"] == "partial" and row["rank"] == 1
    assert row["model_samples"] == row["priced_samples"] == 0 and row["estimated_usd"] is None
    assert "77.88M" in main(data) and "$61.28" in main(data) and "Partial" in main(data)
    assert "77.88M" in main(data, "zh") and "部分覆盖" in main(data, "zh")


@pytest.mark.parametrize("field,value", [
    ("reference_estimated_usd", None), ("reference_estimated_usd", True),
    ("reference_estimated_usd", "5.2"), ("reference_estimated_usd", float("nan")),
    ("reference_estimated_usd", float("inf")), ("reference_estimated_usd", -1),
    ("reference_estimated_usd", 9999), ("reference_model", "other"),
    ("reference_tokens", None), ("reference_tokens", {"total_tokens": 2_000_000}),
    ("reference_cost_state", "unknown"), ("reference_cost_state", "partial"),
    ("usage_samples", 0), ("usage_samples", True),
])
def test_invalid_or_unmatched_reference_cannot_publish_money_or_fall_back_to_actual_model(field, value):
    data = measure(fleet_of([reading(model="claude-opus-5-5", input_tokens=0, output=0,
        read_tokens=1_000_000, write_tokens=1_000_000)]))
    data["entries"][0][field] = value
    text = main(data)
    assert "$" not in text and "Not ranked yet" in text and "2M" in text
    assert data["entries"][0]["estimated_usd"] == 5.2


def test_identically_incomplete_token_maps_fail_closed_without_crashing_the_homepage():
    data = measure(fleet_of([reading(model=None, cache=False)]))
    row = data["entries"][0]
    for field in ("tokens", "reference_tokens"):
        del row[field]["cache_read_input_tokens"]
    assert "Reference unavailable" in main(data) and "$" not in main(data)


def test_reference_rendering_never_mutates_telemetry_or_publishes_untrusted_pricing_metadata():
    data = measure(fleet_of([reading(model=None)]))
    data["reference_pricing"].update(url="javascript:PRIVATE", model="PRIVATE",
                                     date="PRIVATE", rates_usd_per_million={"PRIVATE": 1})
    before = deepcopy(data)
    source = usersite.public_usage_panel(data)
    assert "PRIVATE" not in source and data == before
