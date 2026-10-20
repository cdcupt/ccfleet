"""Independent synthetic checks of public usage attribution and matching."""

import copy
import json

import pytest

from ccfleet_agent import relay_metrics
from ccfleetd import performance, usage
from ccfleetd.config import Config
from tests.test_performance import BOUND, NOW, OTHER, Fleet, gateway_reading, report

PRIVATE = "private_person_email@example.test"
MODEL = "claude-sonnet-4-5"


def reading(samples=1, *, labeled=None, eligible=None, cache_read=True, cache_write=True,
            native=None):
    eligible = samples if eligible is None else eligible
    core = report(eligible, eligible)
    counts = {"input_tokens": 100, "output_tokens": 30,
              "cache_read_input_tokens": 200 if cache_read else 0,
              "cache_creation_input_tokens": 40 if cache_write else 0}
    core["token_usage"] = {"eligible": eligible, "samples": samples,
        **{key: value * samples for key, value in counts.items()},
        "cache_read_samples": samples if cache_read else 0,
        "cache_creation_samples": samples if cache_write else 0}
    if labeled is not False:
        labeled = samples if labeled is None else labeled
        core["model_usage"] = {"eligible": labeled, "samples": labeled, "models": [{
            "model": MODEL, "samples": labeled,
            **{key: value * labeled for key, value in counts.items()},
            "cache_read_samples": labeled if cache_read else 0,
            "cache_creation_samples": labeled if cache_write else 0}] if labeled else []}
    if native is not None:
        core["stream_output"] = {"samples": native, "output_tokens": native * 30,
                                 "stream_ms": native * 1000}
    return relay_metrics.validate_report(core)


class PublicFleet(Fleet):
    def __init__(self, reports=(), holders=None, handle="chosen-name"):
        super().__init__(reports, holders)
        self.accounts = [{"id": slot["held_by"], "handle": handle,
                          "email": PRIVATE, "nickname": "private-nickname"}
                         for slot in self.slots]

    def list_accounts(self):
        return self.accounts


def public(fleet, now=NOW):
    return usage.public_summary(fleet, Config(heartbeat_max_age_s=300), now)


def test_uncached_input_cache_read_and_cache_write_are_distinct_once_and_correctly_priced():
    result = public(PublicFleet([reading()]))
    row = result["entries"][0]
    assert row["tokens"] == {"input_tokens": 100, "output_tokens": 30,
                             "cache_read_input_tokens": 200,
                             "cache_creation_input_tokens": 40, "total_tokens": 370}
    # Sonnet 4.5 standard API: 3/15/3.75/0.30 USD per million tokens.
    # Cache-read tokens get the cache rate, not another ordinary input charge.
    assert row["estimated_usd"] == pytest.approx(0.00096)
    assert row["rank"] == 1 and row["cost_state"] == row["token_state"] == "complete"
    assert result["pricing"]["cache_write_ttl_assumption"] == "5m"
    assert result["pricing"]["subscription_charges"] is False


@pytest.mark.parametrize("missing", ["cache_read", "cache_write"])
def test_missing_cache_coverage_is_partial_unknown_not_an_explicit_zero(missing):
    result = public(PublicFleet([reading(**{missing: False})]))
    row = result["entries"][0]
    field = "cache_read_input_tokens" if missing == "cache_read" else "cache_creation_input_tokens"
    assert row["tokens"][field] is None
    assert row["token_state"] == row["cost_state"] == "partial"
    assert result["state"] == "partial"


def test_new_model_coverage_never_assigns_legacy_tokens_or_prices_to_the_observed_model():
    result = public(PublicFleet([reading(3, labeled=1)]))
    row = result["entries"][0]
    assert row["tokens"]["total_tokens"] == 1110
    assert row["models"][0]["tokens"]["total_tokens"] == 370
    assert row["estimated_usd"] == pytest.approx(0.00096)
    assert row["usage_samples"] == 3 and row["model_samples"] == row["priced_samples"] == 1
    assert row["unpriced_samples"] == 2 and row["cost_state"] == "partial"
    legacy = public(PublicFleet([reading(3, labeled=False)]))["entries"][0]
    assert legacy["estimated_usd"] is None and legacy["rank"] is not None
    assert legacy["models"] == [] and legacy["unpriced_samples"] == 3


def test_unlabeled_response_keeps_observed_usage_but_never_a_fake_zero_cost():
    raw = reading()
    raw["model_usage"]["models"][0]["model"] = "other"
    row = public(PublicFleet([raw]))["entries"][0]
    assert row["tokens"]["total_tokens"] == 370
    assert row["estimated_usd"] is None and row["rank"] == 1
    assert row["cost_state"] == "unknown" and row["unpriced_samples"] == 1


@pytest.mark.parametrize("handle,mask", [("chosen-name", "c***e"), ("bob", "***"),
                                        (PRIVATE, None), (None, None), ("", None)])
def test_only_valid_chosen_handle_is_masked_with_no_email_or_nickname_fallback(handle, mask):
    result = public(PublicFleet([reading()], handle=handle))
    assert result["entries"][0]["masked_handle"] == mask
    body = json.dumps(result)
    for secret in (PRIVATE, "private-nickname", "holder-0", "node-0", "slot-id-0",
                   "chosen-name", BOUND, OTHER):
        assert secret not in body


@pytest.mark.parametrize("change", [
    lambda fleet: fleet.beats["node-0"].update(ts=NOW - 301),
    lambda fleet: fleet.entry()["relay"].update(observed_at=NOW - 301),
    lambda fleet: fleet.entry()["credentials"].update(account_fp=OTHER),
    lambda fleet: fleet.entry()["credentials"].update(logged_in=False),
    lambda fleet: fleet.slots[0].update(account_switched_at=NOW + 1),
    lambda fleet: fleet.entry().update(provisioned_for=NOW - 86401),
    lambda fleet: fleet.beats["node-0"]["payload"]["slots"].append(copy.deepcopy(fleet.entry())),
])
def test_stale_mismatched_or_duplicate_account_snapshot_never_enters_public_ranking(change):
    fleet = PublicFleet([reading()])
    change(fleet)
    result = public(fleet)
    assert result["entries"] == [] and result["tokens"] is None
    assert result["estimated_usd"] is None and result["reporting_slots"] == 0


def test_latest_snapshots_are_not_mutated_or_accumulated_on_each_page_read():
    fleet = PublicFleet([reading()])
    before = copy.deepcopy(fleet.beats)
    first = public(fleet)
    assert public(fleet) == first and fleet.beats == before
    fleet.entry()["relay"] = reading(2)
    assert public(fleet)["usage_samples"] == 2


def test_current_holder_claim_does_not_inherit_the_previous_holder_snapshot():
    fleet = PublicFleet([reading()])
    fleet.slots[0].update(held_by="new-holder", claimed_at=NOW)
    fleet.accounts.append({"id": "new-holder", "handle": "new-name", "email": PRIVATE})
    result = public(fleet)
    assert result["entries"] == [] and result["reporting_slots"] == 0


def test_aggregate_numeric_overflow_is_unknown_and_does_not_publish_invalid_costs():
    raw = reading(labeled=False)
    maximum = relay_metrics.MAX_COUNTER
    samples = maximum // relay_metrics.MAX_USAGE_TOKENS + 1
    raw = report(samples, samples)
    raw["token_usage"] = {"eligible": samples, "samples": samples,
        "input_tokens": maximum, "output_tokens": maximum,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_samples": 0, "cache_creation_samples": 0}
    raw = relay_metrics.validate_report(raw)
    result = public(PublicFleet([raw]))
    assert result["tokens"] is None and result["estimated_usd"] is None
    assert result["entries"] == [] and result["state"] == "no_data"


def test_same_current_slot_snapshot_is_never_counted_twice():
    fleet = PublicFleet([reading()])
    fleet.slots.append(copy.deepcopy(fleet.slots[0]))
    result = public(fleet)
    assert result["usage_samples"] <= 1 and result["usage_eligible_messages"] <= 1
    assert result["reporting_slots"] <= 1


def test_native_and_gateway_matched_samples_are_combined_without_duplicating_node_usage():
    fleet = PublicFleet([reading(20, native=10)])
    # Node token_usage/model_usage also cover gateway traffic, but stream_output
    # deliberately includes only native/bridge traffic. Add that block alone.
    fleet.gateway_throughput_summary = lambda _: gateway_reading(10, tokens=700, elapsed=5000)
    summary = performance.summary(fleet, Config(heartbeat_max_age_s=300), NOW)
    assert summary["throughput"]["samples"] == 20
    assert summary["throughput"]["tokens_per_second"] == pytest.approx(1000 / 15)
    result = public(fleet)
    assert result["usage_samples"] == result["model_samples"] == 20


@pytest.mark.parametrize("tainted", [False, True])
def test_measured_user_beside_unsampled_user_never_creates_a_fake_zero_model_cost(tainted):
    unsampled = report(1, 1)
    unsampled["token_usage"] = {**dict.fromkeys(relay_metrics.USAGE_KEYS, 0), "eligible": 1}
    unsampled["model_usage"] = {"eligible": 1, "samples": 0, "models": []}
    if tainted:
        unsampled["model_usage"]["models"] = [{"model": MODEL,
            **dict.fromkeys(relay_metrics.MODEL_ROW_KEYS - {"model"}, 0)}]
        with pytest.raises(relay_metrics.MetricsError):
            relay_metrics.validate_report(unsampled)
    fleet = PublicFleet([reading(), unsampled])
    result = public(fleet)
    assert result["state"] == "partial"
    assert result["estimated_usd"] == pytest.approx(0.00096)
    assert result["reporting_slots"] == (1 if tainted else 2)
    assert len(result["entries"]) == (1 if tainted else 2)
    for entry in result["entries"]:
        if entry["usage_samples"] == 0:
            assert entry["tokens"] is entry["estimated_usd"] is entry["rank"] is None
            assert entry["models"] == []
        else:
            assert entry["estimated_usd"] > 0


def test_corrupt_gateway_source_does_not_hide_valid_native_rate_or_disclose_extra_fields():
    fleet = PublicFleet([reading(20, native=20)])
    # A malformed duration remains independent from the valid native source.
    fleet.gateway_throughput_summary = lambda _: {**gateway_reading(), "stream_ms": float("nan"),
                                                  "email": PRIVATE, "model": "private-model"}
    throughput = performance.summary(fleet, Config(heartbeat_max_age_s=300), NOW)["throughput"]
    assert throughput["state"] == "ready" and throughput["samples"] == 20
    assert throughput["tokens_per_second"] == 30
    assert throughput["collection_start_day"] is None
    assert PRIVATE not in json.dumps(throughput) and "private-model" not in json.dumps(throughput)


def test_stale_native_snapshot_does_not_suppress_independent_valid_gateway_rate():
    fleet = PublicFleet([reading(20, native=20)])
    fleet.beats["node-0"]["ts"] = NOW - 301
    fleet.gateway_throughput_summary = lambda _: gateway_reading(20, tokens=2000, elapsed=20000)
    summary = performance.summary(fleet, Config(heartbeat_max_age_s=300), NOW)
    assert summary["reporting_slots"] == 0
    assert summary["throughput"]["samples"] == 20
    assert summary["throughput"]["tokens_per_second"] == 100
    assert public(fleet)["entries"] == []
