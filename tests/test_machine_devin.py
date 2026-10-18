"""Machine native intent is independent of holder facts and ordinary Claude state."""
from __future__ import annotations

import copy
import json

import pytest

from ccfleet_agent import devin_account, machine
from ccfleet_agent import devin_provision as p
from ccfleetd.desired import desired_state
from tests.test_gateway_access import public_key
from tests.test_machine import NOW, Fake

CONTROL = {"claim": {"owner_id": "owner-a", "slot_id": "s1", "claim_id": "d"*32,
                     "claim_generation": 3},
    "runtime": {"bridge_contract": "native-agent-v1", "native_version": "3000.11.92",
                "adapter_revision": "b"*64, "model_map_digest": "c"*64},
    "keys": [{"key_id": "endpoint", "key_generation": 2, "provider_generation": 1}]}


@pytest.fixture
def cfg(tmp_path):
    return machine.MachineConfig.from_env({"CCFLEET_URL": "https://fleet.example",
        "CCFLEET_NODE_ID": "shared-1", "CCFLEET_NODE_TOKEN": "t"*64,
        "CCFLEET_STATE_FILE": str(tmp_path / "machine.json"),
        "CCFLEET_DEVIN_POLICY_DIR": str(tmp_path / "policy")})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(machine, "execution_egress_ip", lambda *args: {})


def observation():
    return {"claim": copy.deepcopy(CONTROL["claim"]), "credential_sha256": "e"*64,
        "checked_at": NOW, "reason": "verified", "cleanup_confirmed": True,
        "config_sha256": "a"*64, "source_profile_sha256": "f"*64,
        "report": {**CONTROL["runtime"], "account_fp": "9"*64, "identity_verified": True,
            "capabilities": dict.fromkeys(devin_account.CAPABILITIES, True), "observed_at": NOW}}


def test_claimed_native_control_keeps_claude_state_and_separate_key_privileges():
    slot = {"id": "s1", "kind": "machine", "unix_user": "slot01", "state": "claimed",
            "held_by": "owner-a"}
    desired = desired_state({"pinned_version": "2.1.284"}, slots=[slot],
        slot_gateway_keys={"s1": [public_key()]}, slot_devin_gateways={"s1": CONTROL})
    [wanted] = machine.wanted_slots(desired)
    assert wanted["state"] == "claimed" and wanted["devin_gateway"] == CONTROL
    assert wanted["ssh_gateway_public_keys"] == [public_key()]
    assert wanted["ssh_public_keys"] == [] and wanted["inference_allowed"] is True
    legacy = desired_state({"pinned_version": "2.1.284"}, slots=[slot],
                           slot_gateway_keys={"s1": [public_key()]})
    assert machine.wanted_slots(legacy)[0]["ssh_gateway_public_keys"] == []
    assert "devin_gateway" not in legacy["slots"][0]


@pytest.mark.parametrize("state", ["free", "claiming", "releasing"])
def test_nonheld_lifecycle_cannot_carry_native_intent_or_service_keys(state):
    entry = {"unix_user": "slot01", "state": state, "claimed_at": NOW,
        "inference_allowed": True, "devin_gateway": CONTROL,
        "ssh_gateway_public_keys": [public_key()]}
    [wanted] = machine.wanted_slots({"slots": [entry]})
    assert "devin_gateway" not in wanted and wanted["ssh_gateway_public_keys"] == []


@pytest.mark.parametrize("bad", [{}, {**CONTROL, "keys": [{"key_id": "x", "key_generation": True,
    "provider_generation": 1}]}, {**CONTROL, "command": "/bin/sh"}])
def test_malformed_native_intent_fails_closed_before_root_reconcile(bad):
    [wanted] = machine.wanted_slots({"slots": [{"unix_user": "slot01", "state": "claimed",
        "inference_allowed": True, "devin_gateway": bad, "ssh_gateway_public_keys": [public_key()]}]})
    assert "devin_gateway" not in wanted and wanted["ssh_gateway_public_keys"] == []


def test_holder_and_cached_gateway_provider_are_stripped_on_every_payload_path(cfg):
    fake = Fake(["slot01"], facts={"gateway_provider": {"identity_verified": True}})
    state = {"slots": ["slot01"], "heard": {"slot01": {
        "gateway_provider": {"PRIVATE_CACHED_FACT": True}}}}
    for fast in (False, True):
        payload = machine.machine_payload(cfg, state, fake.system(), fast=fast)
        assert "gateway_provider" not in payload["slots"][0]
    public = p.public_report(observation())
    payload = machine.machine_payload(cfg, state, fake.system(), fast=True,
                                      provider_reports={"slot01": public})
    assert payload["slots"][0]["gateway_provider"] == public
    assert "credential_sha256" not in json.dumps(payload)


def test_only_root_observation_can_supply_report_and_missing_profile_revokes(cfg, monkeypatch):
    fake = Fake(["slot01"])
    state = {"slots": ["slot01"], "slot_states": {"slot01": "claimed"},
             "devin_controls": {"slot01": CONTROL}}
    seen = []
    observed = observation()
    def collect(*args, **kwargs):
        seen.append(kwargs)
        return observed
    monkeypatch.setattr(p, "collect", collect)
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=False)
    assert reports == {"slot01": p.public_report(observed)} and private == {"slot01": observed}
    assert callable(seen[0]["now"]) and seen[0]["spawn"] == fake.spawn
    monkeypatch.setattr(p, "collect", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=False)
    assert private == {} and reports["slot01"]["report"]["identity_verified"] is False
    assert reports["slot01"]["report"]["account_fp"] is None


def test_fast_cache_rechecks_profile_and_original_credential(cfg, monkeypatch):
    fake = Fake(["slot01"])
    state = {"slots": ["slot01"], "slot_states": {"slot01": "claimed"},
        "devin_controls": {"slot01": CONTROL}, "devin_observations": {"slot01": observation()}}
    monkeypatch.setattr(p, "_profile", lambda *args: (None, {
        "template_sha256": "a"*64, "source_profile_sha256": "f"*64}, None, None, None))
    monkeypatch.setattr(p, "_credential", lambda account: b"DIFFERENT_CURRENT_CREDENTIAL")
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=True)
    assert private == {} and reports["slot01"]["report"]["identity_verified"] is False


def test_native_reconcile_uses_current_single_slot_and_revokes_on_release(cfg, monkeypatch):
    fake = Fake(["slot01"])
    calls = []
    monkeypatch.setattr(p, "reconcile", lambda *args, **kwargs: calls.append((args, kwargs)))
    declared = {"unix_user": "slot01", "state": "claimed", "inference_allowed": True,
                "devin_gateway": CONTROL}
    desired = {"slots": [declared]}
    machine.converge_devin_access(desired, machine.wanted_slots(desired), cfg, fake.system(),
                                  {"slot01": observation()})
    assert calls[-1][0][2] == CONTROL
    release = {"slots": [{"unix_user": "slot01", "state": "releasing"}]}
    machine.converge_devin_access(release, machine.wanted_slots(release), cfg, fake.system(),
                                  {"slot01": observation()}, previous_users=["slot01"])
    assert calls[-1][0][2:4] == (None, None)


def test_collector_never_runs_for_multiple_or_privileged_slots(cfg, monkeypatch):
    monkeypatch.setattr(p, "collect", lambda *a, **kw: pytest.fail("must not collect"))
    fake = Fake(["slot01", "slot02"])
    state = {"slots": ["slot01", "slot02"], "devin_controls": {"slot01": CONTROL}}
    assert machine.native_provider_observations(cfg, state, fake.system(), fast=False) == ({}, {})
    fake = Fake(["slot01"], groups={"slot01": {"ccfleet-slots", "docker"}})
    state["slots"] = ["slot01"]
    assert machine.native_provider_observations(cfg, state, fake.system(), fast=False) == ({}, {})


def test_installer_includes_native_dependency_closure_without_creating_profiles():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "node/machine-setup.sh").read_text()
    assert "for module in devin_acp devin_tools devin_mcp devin_native devin_account devin_provision devin_relay" in source
    assert "profile.json" not in source and "credentials.toml" not in source
    assert machine.MachineConfig.from_env({"CCFLEET_URL": "https://f", "CCFLEET_NODE_ID": "m",
        "CCFLEET_NODE_TOKEN": "t"}).devin_policy_dir == p.DEFAULT_DIRECTORY
