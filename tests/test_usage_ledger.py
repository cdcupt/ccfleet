"""Daily account history survives offline slots without adding rolling snapshots twice."""
from __future__ import annotations

import copy
import json

import pytest

from ccfleet_agent import relay_metrics as metrics
from ccfleetd import usage
from ccfleetd.config import Config
from ccfleetd.store import Store, StoreError
from tests.test_gateway_access import add_held_slot

DAY = metrics.DAY_SECONDS
TODAY = 23000
NOW = TODAY * DAY + 43200
FP = "a" * 16


def daily(samples):
    tokens = {"eligible": samples, "samples": samples, "input_tokens": samples * 10,
              "output_tokens": samples * 2, "cache_read_input_tokens": samples * 3,
              "cache_creation_input_tokens": samples * 4, "cache_read_samples": samples,
              "cache_creation_samples": samples}
    return {"success": samples, "token_usage": tokens,
            "model_usage": {"eligible": samples, "samples": samples,
                            "models": [{"model": "claude-sonnet-4-5",
                                        **{k: v for k, v in tokens.items() if k != "eligible"}}]}}


def reading(days, observed=NOW):
    snapshots = {str(day): daily(samples) for day, samples in days.items()}
    success = sum(days.values())
    raw = {"version": 1, "observed_at": observed, "window_days": 7,
           **dict.fromkeys(metrics.COUNTERS, 0), "requests": success, "success": success,
           "last_success_at": observed if success else None,
           "latencies": {key: {"count": 0, "mean": 0, "max": 0} for key in metrics.TIMINGS},
           "token_usage": {key: sum(row["token_usage"][key] for row in snapshots.values())
                           for key in metrics.USAGE_KEYS}, "usage_days": snapshots}
    if snapshots:
        raw["model_usage"] = metrics._merge_models([row["model_usage"] for row in snapshots.values()])
    return metrics.validate_report(raw)


def payload(slot, report, fp=FP):
    return {"node_id": slot["node_id"],
            "mode": "private_owner" if slot["kind"] == "owner" else "machine",
            "slots": [{"unix_user": slot["unix_user"], "present": True,
                       "provisioned_for": slot["claimed_at"],
                       "credentials": {"present": True, "logged_in": True,
                                       "bound_fp": fp, "account_fp": fp}, "relay": report}]}


@pytest.fixture
def fleet(store):
    store.add_account("a1", "sub-one", "meas@example.test", role="admin", slot_quota=10, now=1)
    store.add_account("a2", "sub-two", "other@example.test", slot_quota=10, now=1)
    add_held_slot(store, 1, "a1", now=NOW - 10 * DAY)
    return store


def publish(store, slot_id, report, *, now=NOW, fp=FP, max_age=300):
    slot = store.get_slot(slot_id)
    body = payload(slot, report, fp)
    store.insert_heartbeat(slot["node_id"], now, body)
    return store.observe_usage_days(slot["node_id"], body, now=now, max_age_s=max_age)


def public(store, now=NOW):
    return usage.public_summary(store, Config(heartbeat_max_age_s=300, cookie_secret="s" * 32), now)


def imported(store, slot_id, report, *, now=NOW, **extra):
    slot = store.get_slot(slot_id)
    return store.import_usage_days(slot_id, slot["held_by"], slot["claimed_at"], FP,
                                   report, now=now, **extra)


def mac(store):
    store.register_private_owner_node("mac-admin", "a1", "synthetic",
        monthly_cost_usd_cents=17000, now=NOW - 100)
    slot = store.get_slot("mac-admin")
    zero = reading({}, NOW - 90)
    store.insert_heartbeat("mac-admin", NOW - 90, payload(slot, zero))
    return slot


def test_replays_out_of_order_and_counter_decreases_do_not_add_or_erase(fleet):
    first = reading({TODAY - 1: 10, TODAY: 20})
    assert publish(fleet, "s1", first) == 2
    assert public(fleet)["usage_samples"] == 30
    assert publish(fleet, "s1", first) == 0
    assert publish(fleet, "s1", reading({TODAY - 1: 9, TODAY: 10}, NOW - 1)) == 0
    assert public(fleet)["usage_samples"] == 30
    assert publish(fleet, "s1", reading({TODAY - 1: 10, TODAY: 30}, NOW + 1), now=NOW + 1) == 1
    assert public(fleet, NOW + 1)["usage_samples"] == 40
    publish(fleet, "s1", reading({TODAY - 1: 10, TODAY: 3}, NOW + 2), now=NOW + 2)
    assert public(fleet, NOW + 2)["usage_samples"] == 40
    assert public(fleet, NOW + 2)["state"] == "partial"


def test_reset_review_stays_until_the_affected_day_expires(fleet):
    publish(fleet, "s1", reading({TODAY: 20}))
    publish(fleet, "s1", reading({TODAY: 3}, NOW + 1), now=NOW + 1)
    publish(fleet, "s1", reading({TODAY: 21}, NOW + 2), now=NOW + 2)
    source = fleet.public_usage_ledger(NOW + 2)["sources"][0]
    assert source["needs_review"] and source["review_until_day"] == TODAY
    assert public(fleet, NOW + 2)["state"] == "partial"
    later = NOW + DAY
    publish(fleet, "s1", reading({TODAY: 21, TODAY + 1: 2}, later), now=later)
    fleet.set_enabled("m1", False)
    after_expiry = (TODAY + 7) * DAY
    source = fleet.public_usage_ledger(after_expiry)["sources"][0]
    assert source["days"] and not source["needs_review"]
    assert public(fleet, after_expiry)["usage_samples"] == 2
    assert public(fleet, after_expiry)["state"] == "ready"
    # A retained source record with no remaining window data cannot taint
    # unrelated accounts' current, complete observations.
    add_held_slot(fleet, 2, "a2", now=NOW - 10 * DAY)
    current = (TODAY + 8) * DAY + 100
    publish(fleet, "s2", reading({TODAY + 8: 5}, current), now=current, fp="b" * 16)
    old = next(s for s in fleet.public_usage_ledger(current)["sources"] if s["slot_id"] == "s1")
    assert old["days"] == [] and not old["needs_review"]
    assert public(fleet, current)["state"] == "ready"
    assert public(fleet, current)["usage_samples"] == 5


def test_saved_days_survive_disable_and_stale_or_logged_out_heartbeat(fleet):
    publish(fleet, "s1", reading({TODAY: 20}))
    before = public(fleet)["entries"][0]
    fleet.set_enabled("m1", False)
    slot = fleet.get_slot("s1")
    body = payload(slot, reading({TODAY: 20}))
    body["slots"][0]["credentials"].update(present=False, logged_in=False)
    fleet.insert_heartbeat("m1", NOW + 600, body)
    after = public(fleet, NOW + 600)
    assert after["usage_samples"] == 20 and after["entries"][0] == before
    assert after["eligible_slots"] == after["reporting_slots"] == 0


def test_days_expire_on_utc_boundary_without_freezing_a_rolling_total(fleet):
    publish(fleet, "s1", reading({TODAY - 6: 5, TODAY - 1: 10, TODAY: 20}))
    fleet.set_enabled("m1", False)
    assert public(fleet)["usage_samples"] == 35
    assert public(fleet, (TODAY + 1) * DAY)["usage_samples"] == 30
    assert fleet.prune_usage_ledger((TODAY + 1) * DAY) == 1
    assert public(fleet, (TODAY + 7) * DAY)["entries"] == []


def test_private_consent_merges_only_masked_account_totals_without_inventory_counts(fleet):
    publish(fleet, "s1", reading({TODAY: 20}))
    fleet.set_enabled("m1", False)
    mac(fleet)
    current = reading({TODAY: 7})
    assert publish(fleet, "mac-admin", current) == 0
    assert public(fleet)["usage_samples"] == 20
    fleet.set_private_owner_public_usage("mac-admin", "a1", True, now=NOW)
    assert publish(fleet, "mac-admin", current) == 1
    combined = public(fleet)
    assert combined["usage_samples"] == 27 and len(combined["entries"]) == 1
    assert combined["tokens"]["total_tokens"] == 27 * 19
    assert combined["eligible_slots"] == combined["reporting_slots"] == 0
    assert all(word not in json.dumps(combined) for word in
               ("mac-admin", "monthly_cost", "node_id", FP, "account_id"))
    fleet.set_enabled("mac-admin", False)
    assert public(fleet)["usage_samples"] == 27
    fleet.set_private_owner_public_usage("mac-admin", "a1", False, now=NOW + 1)
    assert public(fleet)["usage_samples"] == 20
    fleet.set_private_owner_public_usage("mac-admin", "a1", True, now=NOW + 2)
    fleet.set_account_role("a1", "user")
    assert public(fleet)["usage_samples"] == 20


def test_atomic_backfill_consent_and_two_sources_rollback_on_failed_verification(fleet):
    old = fleet.get_slot("s1")
    old_report = reading({TODAY - 1: 20})
    fleet.insert_heartbeat("m1", NOW, payload(old, old_report))
    fleet.set_enabled("m1", False)
    private = mac(fleet)
    new_report = reading({TODAY: 7})
    imports = [dict(slot_id=slot["id"], account_id="a1", claimed_at=slot["claimed_at"],
                    bound_fp=FP, report=report, **extra)
               for slot, report, extra in ((old, old_report, {}),
                   (private, new_report, {"include_claim_day": True, "claim_day_proof": "b" * 64}))]
    consent = {"node_id": "mac-admin", "account_id": "a1", "enabled": True}
    with pytest.raises(StoreError, match="verification"):
        fleet.import_usage_days_batch(imports, now=NOW, private_consent=consent,
                                      validate=lambda: False)
    assert fleet.public_usage_ledger(NOW)["sources"] == []
    assert fleet.private_owner_node("mac-admin")["public_usage_enabled"] == 0
    fleet.import_usage_days_batch(imports, now=NOW, private_consent=consent,
                                  validate=lambda: public(fleet)["usage_samples"] == 27)
    assert public(fleet)["usage_samples"] == 27
    fleet.import_usage_days_batch(imports, now=NOW, private_consent=consent)
    assert public(fleet)["usage_samples"] == 27


def test_archival_binding_and_zero_proof_required_for_operator_import(fleet):
    report = reading({TODAY: 8})
    with pytest.raises(StoreError, match="archived"):
        imported(fleet, "s1", report)
    publish(fleet, "s1", report)
    slot = fleet.get_slot("s1")
    with pytest.raises(StoreError):
        fleet.import_usage_days("s1", "a2", slot["claimed_at"], FP, report, now=NOW)
    with pytest.raises(StoreError):
        fleet.import_usage_days("s1", "a1", slot["claimed_at"] + 1, FP, report, now=NOW)
    mac(fleet)
    fleet.set_private_owner_public_usage("mac-admin", "a1", True, now=NOW)
    with pytest.raises(StoreError, match="proof"):
        imported(fleet, "mac-admin", report, include_claim_day=True)
    assert imported(fleet, "mac-admin", report, include_claim_day=True,
                    claim_day_proof="c" * 64)["claim_day_authorized"]


@pytest.mark.parametrize("observed", [NOW - 200, NOW - 95])
def test_claim_day_import_cannot_precede_assignment_or_its_zero_evidence(fleet, observed):
    mac(fleet)
    fleet.set_private_owner_public_usage("mac-admin", "a1", True, now=NOW)
    with pytest.raises(StoreError, match="predates"):
        imported(fleet, "mac-admin", reading({TODAY: 7}, observed), include_claim_day=True,
                 claim_day_proof="c" * 64)
    assert fleet.public_usage_ledger(NOW)["sources"] == []
    imported(fleet, "mac-admin", reading({TODAY: 7}), include_claim_day=True,
             claim_day_proof="c" * 64)
    # An already-authorized source is not an escape from the same temporal proof.
    with pytest.raises(StoreError, match="predates"):
        imported(fleet, "mac-admin", reading({TODAY: 9}, observed))
    assert public(fleet)["usage_samples"] == 7


def test_automatic_observation_before_claim_day_zero_does_not_authorize_it(fleet):
    mac(fleet)
    fleet.set_private_owner_public_usage("mac-admin", "a1", True, now=NOW)
    assert publish(fleet, "mac-admin", reading({TODAY: 7}, NOW - 95)) == 0
    assert fleet.public_usage_ledger(NOW)["sources"] == []


def test_operator_import_cannot_precede_a_later_account_switch(fleet):
    fleet._conn.execute("UPDATE slots SET account_switched_at=? WHERE id='s1'", (NOW - 100,))
    fleet._conn.commit()
    slot = fleet.get_slot("s1")
    fleet.insert_heartbeat("m1", NOW, payload(slot, reading({TODAY: 1})))
    with pytest.raises(StoreError, match="predates"):
        imported(fleet, "s1", reading({TODAY: 8}, NOW - 200))
    assert fleet.public_usage_ledger(NOW)["sources"] == []


def test_unproven_claim_day_does_not_create_coverage_or_suppress_legacy(fleet):
    add_held_slot(fleet, 2, "a2", now=NOW - 100)
    assert publish(fleet, "s2", reading({TODAY: 8})) == 0
    assert fleet.public_usage_ledger(NOW)["sources"] == []
    assert public(fleet)["usage_samples"] == 8
    # A later reset-to-zero does not become a fictional initial zero baseline.
    publish(fleet, "s2", reading({}, NOW + 1), now=NOW + 1)
    assert publish(fleet, "s2", reading({TODAY: 9}, NOW + 2), now=NOW + 2) == 0


def test_unsafe_days_before_claim_are_never_persisted(fleet):
    add_held_slot(fleet, 2, "a2", now=(TODAY - 1) * DAY + 100)
    publish(fleet, "s2", reading({TODAY - 2: 100, TODAY - 1: 50, TODAY: 3}))
    source = fleet.public_usage_ledger(NOW)["sources"][0]
    assert sum(day["token_usage"]["samples"] for day in source["days"]) == 3


def test_reassignment_never_moves_historical_rows_to_the_new_holder(fleet):
    publish(fleet, "s1", reading({TODAY: 20}))
    original = copy.deepcopy(fleet.public_usage_ledger(NOW))
    fleet.begin_release("s1", held_by="a1")
    fleet.finish_release("s1", now=NOW + 1)
    fleet.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW + 1)
    new = fleet.claim_slot("a2", node_id="m1", now=NOW + 2)
    fleet.move_slot("s1", "claimed")
    fleet.move_slot("s1", "active")
    # Replaying the old claim cannot become the new holder's observation.
    old = {**new, "claimed_at": NOW - 10 * DAY}
    assert fleet.observe_usage_days("m1", payload(old, reading({TODAY: 99})),
                                    now=NOW + 3, max_age_s=300) == 0
    assert fleet.public_usage_ledger(NOW) == original


def test_source_identity_ignores_private_authorization_claim_rotation(fleet):
    mac(fleet)
    fleet.set_private_owner_public_usage("mac-admin", "a1", True, now=NOW)
    publish(fleet, "mac-admin", reading({TODAY: 8}))
    fleet._conn.execute("UPDATE slots SET gateway_claim_id='new-authorization' WHERE id='mac-admin'")
    fleet._conn.commit()
    publish(fleet, "mac-admin", reading({TODAY: 9}, NOW + 1), now=NOW + 1)
    assert len(fleet.public_usage_ledger(NOW)["sources"]) == 1
    assert public(fleet, NOW + 1)["usage_samples"] == 9


def test_configured_freshness_not_a_private_hardcoded_timeout(fleet):
    assert publish(fleet, "s1", reading({TODAY: 8}, NOW - 240), max_age=300) == 1
    another = payload(fleet.get_slot("s1"), reading({TODAY: 9}, NOW - 181))
    assert fleet.observe_usage_days("m1", another, now=NOW, max_age_s=180) == 0
    assert public(fleet)["usage_samples"] == 8


def test_corrupt_stored_day_does_not_blank_other_valid_accounts(fleet):
    publish(fleet, "s1", reading({TODAY: 8}))
    add_held_slot(fleet, 2, "a2", now=NOW - 10 * DAY)
    publish(fleet, "s2", reading({TODAY: 5}), fp="b" * 16)
    source = next(row for row in fleet.public_usage_ledger(NOW)["sources"] if row["account_id"] == "a1")
    fleet._conn.execute("UPDATE usage_ledger_days SET snapshot_json=? WHERE source_key=?",
                        ('{"secret":"MUST_NOT_LEAK"}', source["source_key"]))
    fleet._conn.commit()
    fleet.set_enabled("m1", False)
    answer = public(fleet)
    assert answer["usage_samples"] == 5 and "MUST_NOT_LEAK" not in json.dumps(answer)


def test_ledger_resource_ids_cannot_be_renamed_to_recount_snapshots(fleet):
    publish(fleet, "s1", reading({TODAY: 8}))
    with pytest.raises(StoreError, match="Recorded usage"):
        fleet.rename_node("m1", "renamed-node")
    with pytest.raises(StoreError, match="Recorded usage"):
        fleet.rename_slot("s1", "renamed-slot")
    assert public(fleet)["usage_samples"] == 8
    assert ("usage_ledger_sources", "node_id") in fleet.IMMUTABLE_RESOURCE_ID_COLUMNS
    assert ("usage_ledger_sources", "slot_id") in fleet.IMMUTABLE_RESOURCE_ID_COLUMNS


def test_ledger_and_consent_survive_reopening_database(tmp_path):
    path = tmp_path / "usage.db"
    store = Store(path)
    store.add_account("a1", "sub", "meas@example.test", role="admin", slot_quota=2, now=1)
    add_held_slot(store, 1, "a1", now=NOW - 10 * DAY)
    publish(store, "s1", reading({TODAY: 8}))
    store.set_enabled("m1", False)
    store.close()
    store = Store(path)
    try:
        assert public(store)["usage_samples"] == 8
    finally:
        store.close()
