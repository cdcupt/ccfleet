"""Synthetic aggregate telemetry only; no inference, network or credential reads."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from ccfleet_agent import relay_metrics
from ccfleetd import performance, slots
from ccfleetd.config import Config
from ccfleetd.heartbeat import validate_heartbeat

NOW = 1_790_042_040.0
BOUND = "0123456789abcdef"
OTHER = "fedcba9876543210"
SECRET = "private-model-session-email-slot-host-request-error-content"
KEYS = {"state", "window_days", "window_start_day", "eligible_slots", "reporting_slots",
        "reporting_users", "oldest_observed_at", "requests", "completed", "completion_rate",
        "latencies"}


def report(requests=10, completed=8, observed=NOW, timings=None):
    raw = {"version": 1, "observed_at": observed, "window_days": 7,
           **dict.fromkeys(relay_metrics.COUNTERS, 0),
           "last_success_at": observed if completed else None,
           "latencies": {key: {"count": 0, "mean": 0.0, "max": 0.0}
                         for key in relay_metrics.TIMINGS}}
    raw.update(requests=requests, success=completed, connection_errors=requests - completed)
    for stage, (count, mean) in (timings or {}).items():
        raw["latencies"][stage] = {"count": count, "mean": mean, "max": mean}
    return relay_metrics.validate_report(raw)


class Fleet:
    """A store-shaped read-only fixture, with private unrelated heartbeat fields."""

    def __init__(self, reports=(), holders=None):
        self.nodes = []
        self.slots = []
        self.beats = {}
        holders = holders or [f"holder-{index}" for index in range(len(reports))]
        for index, reading in enumerate(reports):
            node_id, user = f"node-{index}", f"slot{index:02}"
            self.nodes.append({"id": node_id, "enabled": 1, "access_host": SECRET})
            self.slots.append({"id": f"slot-id-{index}", "name": SECRET, "node_id": node_id,
                               "unix_user": user, "kind": slots.MACHINE_SLOT,
                               "state": slots.ACTIVE, "held_by": holders[index],
                               "claimed_at": NOW - 86400, "account_switched_at": None})
            self.beats[node_id] = {"ts": NOW, "payload": {
                "node_id": node_id, "mode": slots.MACHINE_MODE, "hostname": SECRET,
                "usage": {"total_tokens": 10**9, "models": [SECRET]},
                "slots": [{"unix_user": user, "present": True, "provisioned_for": NOW - 86400,
                           "credentials": {"present": True, "logged_in": True,
                                           "bound_fp": BOUND, "account_fp": BOUND,
                                           "email": SECRET},
                           "relay": copy.deepcopy(reading)}]}}

    def list_nodes(self):
        return self.nodes

    def list_slots(self):
        return self.slots

    def latest_heartbeats(self):
        return self.beats

    def entry(self, index=0):
        return self.beats[f"node-{index}"]["payload"]["slots"][0]


def measure(fleet, now=NOW, cfg=None):
    return performance.summary(fleet, cfg or Config(heartbeat_max_age_s=300), now)


def assert_hidden(result):
    assert result["requests"] is result["completed"] is result["completion_rate"] is None
    assert result["oldest_observed_at"] is None
    assert all(value == {"count": None, "mean": None}
               for value in result["latencies"].values())


def test_empty_and_zero_traffic_are_no_data_not_perfect_or_zero_latency():
    for fleet in (Fleet(), Fleet([report(0, 0) for _ in range(3)])):
        result = measure(fleet)
        assert result["state"] == "no_data"
        assert result["reporting_users"] == 0
        assert_hidden(result)


def test_ready_reports_exact_counts_weighted_rate_and_each_timing_denominator():
    fleet = Fleet([
        report(100, 70, timings={"first_byte_ms": (100, 10), "connect_ms": (80, 1),
                                "total_ms": (100, 100)}),
        report(2, 1, timings={"first_byte_ms": (1, 1000), "connect_ms": (2, 100),
                             "total_ms": (1, 1000)}),
        report(3, 0, timings={"first_byte_ms": (1, 100), "connect_ms": (3, 100),
                             "total_ms": (3, 200)}),
    ])
    result = measure(fleet)
    assert result["state"] == "ready"
    assert result["eligible_slots"] == result["reporting_slots"] == result["reporting_users"] == 3
    assert result["requests"] == 105 and result["completed"] == 71
    assert result["completion_rate"] == pytest.approx(71 / 105)
    assert result["latencies"]["first_byte_ms"] == {"count": 102, "mean": 2100 / 102}
    assert result["latencies"]["connect_ms"] == {"count": 85, "mean": 580 / 85}
    assert result["latencies"]["total_ms"] == {"count": 104, "mean": 11600 / 104}


def test_ready_request_cohort_does_not_reveal_a_single_holders_timing():
    fleet = Fleet([
        report(100, 99, timings={"first_byte_ms": (100, 50), "connect_ms": (100, 10)}),
        report(10, 9, timings={"connect_ms": (10, 20)}),
        report(10, 9, timings={"connect_ms": (10, 30)}),
    ])
    result = measure(fleet)
    assert result["state"] == "ready" and result["requests"] == 120
    assert result["latencies"]["first_byte_ms"] == {"count": None, "mean": None}
    assert result["latencies"]["connect_ms"] == {"count": 120, "mean": 12.5}
    assert result["latencies"]["total_ms"] == {"count": 0, "mean": None}


def test_positive_timing_with_too_few_samples_is_gated_independently():
    readings = [report(10, 10, timings={"first_byte_ms": (count, 100)}) for count in (10, 8, 1)]
    result = measure(Fleet(readings))
    assert result["state"] == "ready"
    assert result["latencies"]["first_byte_ms"] == {"count": None, "mean": None}
    readings[2] = report(10, 10, timings={"first_byte_ms": (2, 100)})
    assert measure(Fleet(readings))["latencies"]["first_byte_ms"] == {"count": 20, "mean": 100}


def test_timing_cohort_counts_holders_not_number_of_slots():
    readings = [report(10, 10, timings={"first_byte_ms": (10, 100)}) for _ in range(3)]
    readings.append(report(10, 10))
    result = measure(Fleet(readings, holders=["a", "a", "b", "c"]))
    assert result["state"] == "ready" and result["reporting_users"] == 3
    assert result["latencies"]["first_byte_ms"] == {"count": None, "mean": None}


def test_valid_reports_without_timings_preserve_no_samples():
    result = measure(Fleet([report() for _ in range(3)]))
    assert result["state"] == "ready"
    assert result["latencies"] == {stage: {"count": 0, "mean": None}
                                  for stage in relay_metrics.TIMINGS}


@pytest.mark.parametrize("counts,holders,state", [
    ([100], ["a"], "limited_data"),
    ([100, 100, 100], ["a", "a", "a"], "limited_data"),
    ([100, 100, 100], ["a", "b", "a"], "limited_data"),
    ([1, 1, 1], ["a", "b", "c"], "limited_data"),
    ([18, 1, 0], ["a", "b", "c"], "limited_data"),
    ([19, 1, 0], ["a", "b", "c"], "limited_data"),
    ([18, 1, 1], ["a", "b", "c"], "ready"),
])
def test_minimum_cohort_counts_only_distinct_holders_with_measured_traffic(counts, holders, state):
    result = measure(Fleet([report(count, count) for count in counts], holders))
    assert result["state"] == state
    if state != "ready":
        assert_hidden(result)


def test_zero_traffic_slot_counts_for_coverage_but_not_privacy_cohort():
    result = measure(Fleet([report(10, 9), report(10, 9), report(0, 0)]))
    assert result["eligible_slots"] == result["reporting_slots"] == 3
    assert result["reporting_users"] == 2 and result["state"] == "limited_data"
    assert_hidden(result)


def test_oldest_report_exposes_coverage_age_not_individual_event_time():
    fleet = Fleet([report(observed=NOW - 200), report(observed=NOW - 150), report()])
    result = measure(fleet)
    assert result["oldest_observed_at"] == NOW - 200
    assert "last_success_at" not in result


@pytest.mark.parametrize("field,value", [
    ("state", slots.FREE), ("state", slots.CLAIMING), ("state", slots.CLAIMED),
    ("state", slots.RELEASING), ("kind", slots.OWNER_SLOT),
    ("held_by", None), ("held_by", ""), ("held_by", True),
    ("unix_user", ""), ("unix_user", None), ("node_id", "unknown"),
])
def test_not_currently_held_hosted_active_slot_is_not_eligible(field, value):
    fleet = Fleet([report()])
    fleet.slots[0][field] = value
    result = measure(fleet)
    assert result["eligible_slots"] == result["reporting_slots"] == 0
    assert result["state"] == "no_data"


@pytest.mark.parametrize("enabled", [0, False, None, "yes", 2, 1.0])
def test_disabled_and_invalid_nodes_do_not_contribute(enabled):
    fleet = Fleet([report()])
    fleet.nodes[0]["enabled"] = enabled
    result = measure(fleet)
    assert result["eligible_slots"] == 0 and result["state"] == "no_data"


@pytest.mark.parametrize("field,value", [
    ("logged_in", False), ("logged_in", 1), ("logged_in", None),
    ("present", False), ("bound_fp", OTHER), ("bound_fp", None),
    ("bound_fp", "bad"), ("account_fp", None), ("account_fp", "bad"),
])
def test_absent_signed_out_or_mismatched_binding_is_not_a_report(field, value):
    fleet = Fleet([report()])
    fleet.entry()["credentials"][field] = value
    result = measure(fleet)
    assert result["eligible_slots"] == 1 and result["reporting_slots"] == 0
    assert_hidden(result)


def test_trailing_newline_does_not_make_a_valid_account_binding():
    fleet = Fleet([report()])
    fleet.entry()["credentials"].update(bound_fp=BOUND + "\n", account_fp=BOUND + "\n")
    assert measure(fleet)["reporting_slots"] == 0


@pytest.mark.parametrize("change", [
    lambda fleet: fleet.beats.clear(),
    lambda fleet: fleet.beats.update({"node-0": None}),
    lambda fleet: fleet.beats["node-0"].update(payload=None),
    lambda fleet: fleet.beats["node-0"]["payload"].update(node_id="different"),
    lambda fleet: fleet.beats["node-0"]["payload"].update(mode="owner"),
    lambda fleet: fleet.beats["node-0"]["payload"].update(slots=None),
    lambda fleet: fleet.entry().update(present=False),
    lambda fleet: fleet.entry().update(credentials=None),
    lambda fleet: fleet.entry().update(unix_user="wrong"),
    lambda fleet: fleet.beats["node-0"]["payload"]["slots"].append(copy.deepcopy(fleet.entry())),
])
def test_missing_or_ambiguous_slot_snapshot_fails_closed(change):
    fleet = Fleet([report()])
    change(fleet)
    result = measure(fleet)
    assert result["reporting_slots"] == 0 and result["state"] == "no_data"


@pytest.mark.parametrize("value", [None, True, "now", -1, float("inf"), float("nan"), 10**400,
                                    NOW - 301, NOW + 61])
def test_heartbeat_receive_clock_must_be_finite_fresh_and_bounded(value):
    fleet = Fleet([report()])
    fleet.beats["node-0"]["ts"] = value
    result = measure(fleet)
    assert result["reporting_slots"] == 0


@pytest.mark.parametrize("offset,accepted", [(-301, False), (-300, True), (60, True), (61, False)])
def test_heartbeat_receive_clock_freshness_edges(offset, accepted):
    fleet = Fleet([report()])
    fleet.beats["node-0"]["ts"] = NOW + offset
    assert bool(measure(fleet)["reporting_slots"]) is accepted


@pytest.mark.parametrize("offset,accepted", [(-301, False), (-300, True), (60, True), (61, False)])
def test_report_observation_clock_freshness_edges(offset, accepted):
    fleet = Fleet([report(observed=NOW + offset)])
    assert bool(measure(fleet)["reporting_slots"]) is accepted


@pytest.mark.parametrize("field", ["claimed_at", "account_switched_at"])
def test_report_predating_current_assignment_is_not_counted(field):
    fleet = Fleet([report(observed=NOW - 1)])
    fleet.slots[0][field] = NOW
    if field == "claimed_at":
        fleet.entry()["provisioned_for"] = NOW
    assert measure(fleet)["reporting_slots"] == 0
    fleet.entry()["relay"] = report(observed=NOW)
    assert measure(fleet)["reporting_slots"] == 1


@pytest.mark.parametrize("provisioned", [None, True, "now", float("nan"), float("inf"),
                                        10**400, NOW - 86401, NOW - 86399])
def test_provision_marker_must_confirm_the_current_claim_exactly(provisioned):
    fleet = Fleet([report()])
    fleet.entry()["provisioned_for"] = provisioned
    assert measure(fleet)["reporting_slots"] == 0
    assert measure(fleet)["eligible_slots"] == 1


def test_missing_provision_marker_fails_closed_for_legacy_snapshot():
    fleet = Fleet([report()])
    del fleet.entry()["provisioned_for"]
    assert measure(fleet)["reporting_slots"] == 0


def test_old_claim_clock_ahead_of_new_claim_does_not_leak_cached_traffic():
    fleet = Fleet([report(observed=NOW + 30)])
    fleet.slots[0]["claimed_at"] = NOW
    # The report is timestamped after the new claim due to permissible clock
    # skew, but the root-owned provisioning marker still names the old claim.
    assert measure(fleet)["reporting_slots"] == 0


@pytest.mark.parametrize("field,value", [
    ("claimed_at", None), ("claimed_at", True), ("claimed_at", "now"),
    ("account_switched_at", float("nan")), ("account_switched_at", True),
])
def test_invalid_assignment_clock_does_not_bypass_binding_guard(field, value):
    fleet = Fleet([report()])
    fleet.slots[0][field] = value
    assert measure(fleet)["reporting_slots"] == 0


def test_utc_midnight_omits_previous_day_window_even_if_clock_fresh():
    midnight = int(NOW // 86400) * 86400
    fleet = Fleet([report(observed=midnight - 1)])
    fleet.beats["node-0"]["ts"] = midnight
    fleet.slots[0]["claimed_at"] = midnight - 86400
    fleet.entry()["provisioned_for"] = midnight - 86400
    result = measure(fleet, now=midnight + 1)
    assert result["reporting_slots"] == 0
    fleet.entry()["relay"] = report(observed=midnight)
    assert measure(fleet, now=midnight + 1)["reporting_slots"] == 1


@pytest.mark.parametrize("value", [None, True, {}, "SECRET"])
def test_non_report_telemetry_fails_closed(value):
    fleet = Fleet([report()])
    fleet.entry()["relay"] = value
    assert measure(fleet)["reporting_slots"] == 0


@pytest.mark.parametrize("field,value", [
    ("requests", True), ("requests", -1), ("requests", "SECRET"),
    ("requests", float("nan")), ("requests", float("inf")), ("requests", 10**400),
    ("requests", 99), ("body", SECRET), ("headers", {"authorization": SECRET}),
    ("model", SECRET), ("email", SECRET), ("path", SECRET),
])
def test_invalid_or_identity_tainted_report_is_revalidated(field, value):
    fleet = Fleet([report()])
    fleet.entry()["relay"][field] = value
    assert measure(fleet)["reporting_slots"] == 0


@pytest.mark.parametrize("field,value", [
    ("count", 11), ("count", True), ("mean", -1), ("mean", float("nan")),
    ("mean", float("inf")), ("max", -1), ("max", float("inf")),
])
def test_invalid_stage_sample_invalidates_report(field, value):
    fleet = Fleet([report()])
    fleet.entry()["relay"]["latencies"]["first_byte_ms"][field] = value
    assert measure(fleet)["reporting_slots"] == 0


def test_window_is_calendar_dates_in_utc_and_output_is_fixed_numeric_allowlist():
    fleet = Fleet([report() for _ in range(3)])
    before = copy.deepcopy(fleet.beats)
    result = measure(fleet)
    assert set(result) == KEYS and result["window_days"] == 7
    day = datetime.fromtimestamp((int(NOW // 86400) - 6) * 86400, timezone.utc).date()
    assert result["window_start_day"] == day.isoformat()
    assert set(result["latencies"]) == set(relay_metrics.TIMINGS)
    assert all(set(value) == {"count", "mean"} for value in result["latencies"].values())
    raw = json.dumps(result, allow_nan=False)
    for private in (SECRET, BOUND, OTHER, "holder-", "node-", "slot-id-", "last_success_at",
                    "token_usage", "models", "headers", "max"):
        assert private not in raw
    assert fleet.beats == before


def test_prevalidated_heartbeat_is_compatible_with_summary():
    fleet = Fleet([report() for _ in range(3)])
    for node, beat in fleet.beats.items():
        beat["payload"] = validate_heartbeat(beat["payload"], node, now=NOW)
    assert measure(fleet)["state"] == "ready"


def test_latest_snapshot_only_does_not_sum_history_or_native_transcript_usage(store):
    for index in range(3):
        node, account, slot = f"machine-{index}", f"account-{index}", f"id-{index}"
        store.add_node(node, "operator", now=NOW - 86400)
        store.add_account(account, f"sub-{index}", f"holder{index}@example.com",
                          slot_quota=1, now=NOW - 86400)
        store.add_slot(slot, node, "slot01", now=NOW - 86400)
        store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}],
                                now=NOW - 86400)
        claimed = store.claim_slot(account, node_id=node, now=NOW - 3600)
        store.move_slot(claimed["id"], slots.CLAIMED)
        store.move_slot(claimed["id"], slots.ACTIVE)
        reading = Fleet([report()]).beats["node-0"]["payload"]
        reading["node_id"] = node
        reading["slots"][0]["unix_user"] = "slot01"
        reading["slots"][0]["provisioned_for"] = claimed["claimed_at"]
        reading = validate_heartbeat(reading, node, now=NOW)
        store.insert_heartbeat(node, NOW - 1, reading)
        store.insert_heartbeat(node, NOW, reading)
    result = measure(store)
    assert result["requests"] == 30 and result["completed"] == 24
    assert result["eligible_slots"] == result["reporting_slots"] == 3
    # A later beat without usable telemetry wins, even though history has it.
    store.insert_heartbeat("machine-0", NOW + 1, {"node_id": "machine-0", "mode": "machine"})
    result = measure(store, now=NOW + 1)
    assert result["reporting_slots"] == 2 and result["state"] == "limited_data"
    assert_hidden(result)


def test_configured_freshness_is_used_without_restart_grace():
    fleet = Fleet([report(observed=NOW - 301)])
    fleet.nodes[0]["listening_since"] = NOW
    fleet.beats["node-0"]["ts"] = NOW - 301
    assert measure(fleet)["reporting_slots"] == 0
    assert measure(fleet, cfg=replace(Config(), heartbeat_max_age_s=600))["reporting_slots"] == 1


@pytest.mark.parametrize("now", [None, True, "now", float("inf"), float("nan"), -1, 10**400])
def test_invalid_server_clock_is_rejected(now):
    with pytest.raises(ValueError, match="observation clock"):
        measure(Fleet(), now=now)
