"""Root evidence reaches the refresher before slow facts, without new authority."""

import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleet_agent import devin_provision as provision
from ccfleet_agent import machine
from tests.test_machine import Fake
from tests.test_machine_devin import CONTROL, NOW, observation


@pytest.fixture
def scene(tmp_path, monkeypatch):
    cfg = machine.MachineConfig.from_env({
        "CCFLEET_URL": "https://synthetic.invalid", "CCFLEET_NODE_ID": "synthetic",
        "CCFLEET_NODE_TOKEN": "synthetic-only", "CCFLEET_STATE_FILE": str(tmp_path / "machine.json"),
    })
    s = SimpleNamespace(cfg=cfg, now=NOW, delay=150, status=200, bad=False, change=None,
                        profile="f" * 64, credential=b"synthetic credential", events=[], fail=False,
                        collections=0)
    old = observation()
    old["checked_at"] = old["report"]["observed_at"] = NOW - 130
    s.state = {"slots": ["slot01"], "slot_states": {"slot01": "claimed"},
               "devin_controls": {"slot01": copy.deepcopy(CONTROL)},
               "devin_observations": {"slot01": old},
               "unrelated": {"marks": [1, 2, 3]}, "quota_turn": 7}
    assert machine.core.write_state(cfg.state_path, s.state)
    s.desired = {"slots": [{"unix_user": "slot01", "state": "claimed",
                            "inference_allowed": True, "devin_gateway": copy.deepcopy(CONTROL)}]}
    s.system = replace(Fake(["slot01"]).system(), clock=lambda: s.now,
                       hostname=lambda: "synthetic-host")
    s.saved = lambda: machine.core.read_state(cfg.state_path)

    def collect(*args, **kwargs):
        s.collections += 1
        if s.bad:
            raise ValueError("synthetic rejected collection")
        value = observation()
        value["credential_sha256"] = hashlib.sha256(s.credential).hexdigest()
        value["checked_at"] = value["report"]["observed_at"] = s.now
        if s.change == "profile":
            s.profile = "0" * 64
        elif s.change == "credential":
            s.credential = b"changed synthetic credential"
        elif s.change == "control":
            s.state["devin_controls"]["slot01"]["claim"]["claim_generation"] += 1
        return value

    def slot_report(user, *args, **kwargs):
        s.events.append(("slot_facts", s.saved()))
        s.now += s.delay
        return {"unix_user": user, "present": True}

    def heartbeat(*args, **kwargs):
        s.events.append(("heartbeat", s.saved()))
        if s.change == "profile_after_heartbeat":
            s.profile = "0" * 64
        return s.status, json.dumps({"desired": s.desired})

    def take_name(desired, cfg, state, system):
        s.events.append(("lifecycle", s.saved()))
        if s.fail:
            raise RuntimeError("synthetic interrupted lifecycle")
        return state

    monkeypatch.setattr(machine.egress, "native_environment", lambda: {})
    monkeypatch.setattr(machine, "execution_egress_ip", lambda *args: {})
    monkeypatch.setattr(machine.core, "system_info", lambda: {})
    monkeypatch.setattr(machine.core, "disk_info", lambda *args: {})
    monkeypatch.setattr(provision, "collect", collect)
    monkeypatch.setattr(provision, "reconcile", lambda *args, **kwargs: None)
    monkeypatch.setattr(provision, "_profile", lambda *args: (None, {
        "template_sha256": "a" * 64, "source_profile_sha256": s.profile}, None, None, None))
    monkeypatch.setattr(provision, "_credential", lambda account: s.credential)
    monkeypatch.setattr(machine, "slot_report", slot_report)
    monkeypatch.setattr(machine.core, "send_heartbeat", heartbeat)
    monkeypatch.setattr(machine, "_take_name", take_name)
    for name in ("converge_inference_access", "converge_devin_access", "converge_ssh_access"):
        monkeypatch.setattr(machine, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(machine, "act_on_slots", lambda wanted, state, cfg, system: dict(state))
    return s


@pytest.mark.parametrize("status", [200, 503])
def test_fresh_evidence_is_saved_before_slow_facts_and_survives_heartbeat_failure(scene, status):
    s = scene
    s.status = status
    result, _, returned = machine.run_cycle(s.cfg, s.state, s.system)
    assert result == status
    before_facts = s.events[0][1]
    assert s.events[0][0] == "slot_facts"
    assert before_facts["devin_observations"]["slot01"]["checked_at"] == NOW
    assert {k: v for k, v in before_facts.items() if k != "devin_observations"} == {
        k: v for k, v in s.state.items() if k != "devin_observations"}
    for snapshot in (s.saved(), returned):
        observed = snapshot["devin_observations"]["slot01"]
        assert observed["checked_at"] == observed["report"]["observed_at"] == NOW
        assert s.now - observed["checked_at"] == 150
        # Saving evidence never turns its age into publication/refresh time.
        provision.clean_observation(observed, CONTROL, now=s.now)
        assert s.now - observed["checked_at"] > 120


@pytest.mark.parametrize("change", ["bad", "missing", "profile", "credential", "control"])
def test_bad_or_changed_evidence_clears_old_good_before_slow_work_and_failed_heartbeat(
        scene, change):
    s = scene
    s.status = 503
    s.bad, s.change = change == "bad", change
    if change == "missing":
        s.state["slots"] = []
    result, _, returned = machine.run_cycle(s.cfg, s.state, s.system)
    assert result == 503
    assert returned["devin_observations"] == s.saved()["devin_observations"] == {}
    assert all(saved["devin_observations"] == {} for _, saved in s.events)


@pytest.mark.parametrize("change", ["revoke", "release", "login", "claim", "key", "profile"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_authenticated_desired_clears_checkpoint_before_lifecycle_and_cannot_resurrect_it(
        scene, change, interrupted):
    s = scene
    slot = s.desired["slots"][0]
    if change == "revoke":
        slot["inference_allowed"] = False
    elif change == "release":
        slot["state"] = "releasing"
    elif change == "login":
        slot["login"] = {"requested_at": NOW}
    elif change == "claim":
        slot["devin_gateway"]["claim"]["claim_generation"] += 1
    elif change == "key":
        slot["devin_gateway"]["keys"][0]["key_generation"] += 1
    else:
        s.change = "profile_after_heartbeat"
    s.fail = interrupted
    if interrupted:
        with pytest.raises(RuntimeError, match="synthetic interrupted lifecycle"):
            machine.run_cycle(s.cfg, s.state, s.system)
    else:
        assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    assert s.events[0][1]["devin_observations"]["slot01"]["checked_at"] == NOW
    lifecycle = next(saved for phase, saved in s.events if phase == "lifecycle")
    assert lifecycle["devin_observations"] == {}
    if change == "key":
        assert lifecycle["devin_controls"] == {}
    if change == "key" and not interrupted:
        observed = s.saved()["devin_observations"]["slot01"]
        assert observed["checked_at"] == observed["report"]["observed_at"] == NOW
        assert s.saved()["devin_controls"] == {"slot01": slot["devin_gateway"]}
    else:
        assert s.saved()["devin_observations"] == {}


@pytest.mark.parametrize("change", ["revoke", "release", "login"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_known_revocation_cannot_recollect_on_next_run_even_after_interrupted_lifecycle(
        scene, change, interrupted):
    s = scene
    slot = s.desired["slots"][0]
    if change == "revoke":
        slot["inference_allowed"] = False
    elif change == "release":
        slot["state"] = "releasing"
    else:
        slot["login"] = {"requested_at": NOW}
    s.fail = interrupted
    if interrupted:
        with pytest.raises(RuntimeError, match="synthetic interrupted lifecycle"):
            machine.run_cycle(s.cfg, s.state, s.system)
    else:
        assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    assert s.saved()["devin_controls"] == s.saved()["devin_observations"] == {}
    count = s.collections
    s.state, s.status, s.fail = s.saved(), 503, False
    assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 503
    assert s.collections == count
    assert s.saved()["devin_controls"] == s.saved()["devin_observations"] == {}


def test_checkpoint_write_failure_stops_before_facts_or_heartbeat_without_claiming_publication(
        scene, monkeypatch):
    s = scene
    monkeypatch.setattr(machine.core, "write_state", lambda *args: False)
    result, desired, returned = machine.run_cycle(s.cfg, s.state, s.system)
    assert result == 503 and desired == {}
    assert returned == s.state == s.saved() and not s.events


def test_second_checkpoint_failure_stops_before_lifecycle_or_final_save(scene, monkeypatch):
    s = scene
    s.desired["slots"][0]["inference_allowed"] = False
    original, writes = machine.core.write_state, []

    def write(path, state):
        writes.append(copy.deepcopy(state))
        return False if len(writes) == 2 else original(path, state)

    monkeypatch.setattr(machine.core, "write_state", write)
    result, _, returned = machine.run_cycle(s.cfg, s.state, s.system)
    assert result == 503 and len(writes) == 2
    assert writes[-1]["devin_controls"] == writes[-1]["devin_observations"] == {}
    assert s.saved()["devin_observations"]["slot01"]["checked_at"] == NOW
    assert returned == s.saved()
    assert [phase for phase, _ in s.events] == ["slot_facts", "heartbeat"]


def test_removing_last_desired_slot_still_reconciles_its_previous_native_grants(scene, monkeypatch):
    s = scene
    s.desired = {"slots": []}
    calls = []

    def converge(desired, wanted, cfg, system, observations, previous_users):
        calls.append((wanted, observations, previous_users))
        assert s.saved()["devin_controls"] == s.saved()["devin_observations"] == {}

    monkeypatch.setattr(machine, "converge_devin_access", converge)
    assert machine.run_cycle(s.cfg, s.state, s.system)[0] == 200
    assert calls == [([], {}, ("slot01",))]
