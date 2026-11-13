"""Successful keys-only convergence retains this cycle's verified identity proof."""

import copy

import pytest

from ccfleet_agent import devin_provision as provision
from ccfleet_agent import machine
from tests.test_machine_devin import NOW
from tests.test_machine_native_checkpoint import scene as scene


def changed_keys(s, change="generation"):
    s.delay = 0
    keys = s.desired["slots"][0]["devin_gateway"]["keys"]
    if change == "generation":
        keys[0]["key_generation"] += 1
    elif change == "provider_generation":
        keys[0]["provider_generation"] += 1
    elif change == "add":
        keys.append({**keys[0], "key_id": "second"})
    else:
        keys.clear()


@pytest.mark.parametrize("change", ["generation", "provider_generation", "add", "remove"])
def test_run_cycle_restores_same_cycle_good_only_after_successful_keys_convergence(scene, change):
    s = scene
    changed_keys(s, change)
    before = copy.deepcopy(s.state)
    status, _, returned = machine.run_cycle(s.cfg, s.state, s.system)
    assert status == 200 and s.collections == 1
    lifecycle = next(saved for phase, saved in s.events if phase == "lifecycle")
    assert lifecycle["devin_controls"] == lifecycle["devin_observations"] == {}
    expected_control = s.desired["slots"][0]["devin_gateway"]
    assert expected_control != before["devin_controls"]["slot01"]
    for snapshot in (s.saved(), returned):
        assert snapshot["devin_controls"] == {"slot01": expected_control}
        observed = snapshot["devin_observations"]["slot01"]
        provision.clean_observation(observed, expected_control, now=s.now)
        assert observed["checked_at"] == observed["report"]["observed_at"] == NOW
        assert observed["report"]["identity_verified"] is True
        assert snapshot["unrelated"] == before["unrelated"]


def test_run_cycle_never_mints_new_key_grants_at_the_early_checkpoint(scene, monkeypatch):
    s = scene
    changed_keys(s)
    reconciliations = []

    def reconcile(desired, wanted, cfg, system, observations, previous_users):
        reconciliations.append(copy.deepcopy(observations))
        assert s.saved()["devin_controls"] == s.saved()["devin_observations"] == {}

    monkeypatch.setattr(machine, "converge_devin_access", reconcile)
    assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    assert reconciliations == [{}]
    assert s.saved()["devin_observations"]["slot01"]["checked_at"] == NOW


@pytest.mark.parametrize("bad", ["failed", "missing", "identity", "cleanup", "reason", "future", "runtime", "capability"])
def test_run_cycle_never_falls_back_to_prior_good_after_current_bad(scene, monkeypatch, bad):
    s = scene
    changed_keys(s)
    collect = provision.collect

    def rejected(*args, **kwargs):
        if bad == "failed":
            raise ValueError("synthetic rejected collection")
        if bad == "missing":
            return {}
        observed = collect(*args, **kwargs)
        if bad == "identity":
            observed["report"]["identity_verified"] = False
        elif bad == "cleanup":
            observed["cleanup_confirmed"] = False
        elif bad == "reason":
            observed["reason"] = "synthetic_bad"
        elif bad == "future":
            observed["checked_at"] = observed["report"]["observed_at"] = s.now + 1
        elif bad == "runtime":
            observed["report"]["native_version"] = "9.9.9"
        else:
            name = next(iter(observed["report"]["capabilities"]))
            observed["report"]["capabilities"][name] = False
        return observed

    monkeypatch.setattr(provision, "collect", rejected)
    assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    assert s.saved()["devin_observations"] == {}


@pytest.mark.parametrize("change", ["profile", "credential", "missing_account", "slot_state", "multiple_slots"])
def test_run_cycle_rechecks_final_profile_credential_and_slot_after_lifecycle(scene, monkeypatch, change):
    s = scene
    changed_keys(s)

    def act(wanted, state, cfg, system):
        assert s.saved()["devin_controls"] == s.saved()["devin_observations"] == {}
        result = dict(state)
        if change == "profile":
            s.profile = "0" * 64
        elif change == "credential":
            s.credential = b"changed synthetic credential"
        elif change == "missing_account":
            monkeypatch.setattr(machine, "_native_account", lambda *args: False)
        elif change == "slot_state":
            result["slot_states"] = {"slot01": "releasing"}
        else:
            result["slots"] = ["slot01", "slot02"]
        return result

    monkeypatch.setattr(machine, "act_on_slots", act)
    assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    assert s.saved()["devin_observations"] == {}


@pytest.mark.parametrize("change", ["claim", "runtime", "revoke", "release", "login"])
def test_run_cycle_does_not_reuse_proof_for_other_authority_or_ineligible_control(scene, change):
    s = scene
    changed_keys(s)
    slot = s.desired["slots"][0]
    if change == "claim":
        slot["devin_gateway"]["claim"]["claim_generation"] += 1
    elif change == "runtime":
        slot["devin_gateway"]["runtime"]["native_version"] = "9.9.9"
    elif change == "revoke":
        slot["inference_allowed"] = False
    elif change == "release":
        slot["state"] = "releasing"
    else:
        slot["login"] = {"requested_at": NOW}
    assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    assert s.saved()["devin_observations"] == {}


def test_run_cycle_interrupted_lifecycle_leaves_revocation_durable(scene):
    s = scene
    changed_keys(s)
    s.fail = True
    with pytest.raises(RuntimeError, match="synthetic interrupted lifecycle"):
        machine.run_cycle(s.cfg, s.state, s.system)
    assert s.saved()["devin_controls"] == s.saved()["devin_observations"] == {}


@pytest.mark.parametrize("delay, kept", [(15, True), (240, True), (241, False)])
def test_run_cycle_keeps_original_timestamp_and_existing_240_second_bound(scene, delay, kept):
    s = scene
    changed_keys(s)
    s.delay = delay
    assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    observed = s.saved()["devin_observations"]
    assert bool(observed) is kept
    if kept:
        assert observed["slot01"]["checked_at"] == observed["slot01"]["report"]["observed_at"] == NOW
        assert s.now - observed["slot01"]["checked_at"] == delay
