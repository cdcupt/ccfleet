"""Independent regressions for latest-assignment privacy and partial cache coverage."""
from copy import deepcopy

import pytest

from ccfleetd import slots, traffic
from ccfleetd.config import Config
from tests.test_network_traffic import TrafficFleet, reading
from tests.test_performance import NOW, report
from tests.test_public_usage import reading as usage_reading


def test_slot_api_uses_newest_received_binding_even_after_server_clock_rollback():
    fleet = TrafficFleet([report(10, 8)])
    fleet.beats["node-0"]["payload"]["network"] = reading(NOW, 2500, 5000)
    newest = deepcopy(fleet.beats["node-0"])
    newest["ts"] = NOW - 1
    newest["payload"]["slots"][0]["credentials"]["account_fp"] = "f" * 16
    # Store.recent_heartbeats sorts by ts; Store.latest_heartbeats selects MAX(id).
    # An earlier larger timestamp must not revive the older accepted account snapshot.
    fleet.latest_heartbeats = lambda: {"node-0": newest}
    result = traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)
    assert result["network"]["state"] == "ready"
    assert result["api"]["state"] == "unavailable"
    assert result["api"]["requests"] is None


@pytest.mark.parametrize("state", [slots.CLAIMING, slots.RELEASING])
def test_other_held_slot_keeps_shared_nic_attribution_during_lifecycle_transitions(state):
    fleet = TrafficFleet([report(10, 8)])
    fleet.slots.append({**fleet.slots[0], "id": "another-slot", "unix_user": "other-user",
                        "state": state, "held_by": "other-holder"})
    result = traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)
    assert result["network_attribution"] == "shared_machine"


def test_api_exposes_each_cache_field_sample_denominator_without_filling_missing_bytes():
    fleet = TrafficFleet([usage_reading(), usage_reading(cache=False, samples=3, eligible=5)])
    total = traffic.controller_summary(fleet, Config(), NOW)["api"]
    assert total["usage_samples"] == 23 and total["usage_eligible_messages"] == 25
    assert total["cache_read_samples"] == total["cache_creation_samples"] == 20
    assert total["token_state"] == "partial"
    missing = traffic.slot_summary(fleet, Config(), fleet.slots[1], NOW)["api"]
    assert missing["cache_read_samples"] == missing["cache_creation_samples"] == 0
    assert missing["tokens"]["cache_read_input_tokens"] is None
    assert missing["tokens"]["cache_creation_input_tokens"] is None
    absent = traffic.controller_summary(TrafficFleet([report()]), Config(), NOW)["api"]
    assert absent["cache_read_samples"] is absent["cache_creation_samples"] is None


def test_unheld_free_network_requires_admin_opt_in_and_never_reveals_old_api_cohort():
    fleet = TrafficFleet([report(10, 8)])
    fleet.beats["node-0"]["payload"]["network"] = reading(NOW, 2500, 5000)
    slot = {**fleet.slots[0], "state": slots.FREE, "held_by": None, "claimed_at": None}
    assert traffic.slot_summary(fleet, Config(), slot, NOW)["network"]["state"] == "unavailable"
    for claimed_at in (None, fleet.slots[0]["claimed_at"]):
        # Retained old metadata cannot authorize API attribution for an unheld slot.
        slot["claimed_at"] = claimed_at
        result = traffic.slot_summary(fleet, Config(), slot, NOW, include_unheld=True)
        assert result["network"]["state"] == "ready"
        assert result["network"]["received_bytes"] == 1500
        assert result["api"]["state"] == "unavailable" and result["api"]["requests"] is None
