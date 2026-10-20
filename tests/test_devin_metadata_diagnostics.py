"""Closed metadata failure codes with fake profiles/spawns; no native execution."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ccfleet_agent import devin_provision as p
from ccfleet_agent import machine
from tests import test_devin_provision, test_machine_devin
from tests.test_machine import NOW, Fake

profile = test_devin_provision.profile
cfg = test_machine_devin.cfg
SECRET = "PRIVATE_PROVIDER_REPLY /private/credential USER_SUBJECT owner@example.invalid"


class UntrustedString(str):
    def __str__(self):
        raise AssertionError("untrusted diagnostic was stringified")


def failed_spawn(profile, reason, *, cleanup=True):
    profile.output.update(reason=reason, cleanup_confirmed=cleanup, private_debug=SECRET)
    profile.output["report"].update(account_fp=None, identity_verified=False, capabilities={})

    def spawn(argv, **kwargs):
        _, output = profile.spawn(argv, **kwargs)
        return 1, output

    return spawn


@pytest.mark.parametrize("reason", sorted(p.COLLECTOR_FAILURE_REASONS))
def test_collector_preserves_only_allowlisted_reason_without_changing_cleanup(profile, reason):
    with pytest.raises(p.ProvisionError) as caught:
        profile.collect(spawn=failed_spawn(profile, reason))
    assert str(caught.value) == "native_metadata_child_unverified"
    assert caught.value.collector_reason == reason
    assert p.metadata_failure_reason(caught.value) == reason
    assert SECRET not in str(caught.value)
    assert len(profile.calls) == 1
    assert not (Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml").exists()


@pytest.mark.parametrize("reason", [
    SECRET, "unknown_native_error", "native_metadata_timeout\n" + SECRET,
    "verified", None, True, [], {"reason": SECRET},
])
def test_unknown_or_raw_child_reason_is_not_preserved(profile, reason):
    with pytest.raises(p.ProvisionError) as caught:
        profile.collect(spawn=failed_spawn(profile, reason))
    assert caught.value.collector_reason is None
    assert p.metadata_failure_reason(caught.value) == "unavailable"
    assert SECRET not in str(caught.value)


def machine_scenario(monkeypatch, control=None):
    fake = Fake(["slot01"])
    control = copy.deepcopy(test_machine_devin.CONTROL if control is None else control)
    state = {"slots": ["slot01"], "slot_states": {"slot01": "claimed"},
             "devin_controls": {"slot01": control}}
    monkeypatch.setattr(machine, "_native_provision", lambda: p)
    return fake, state, control


def bad_report(control):
    return {"slot01": {"claim_id": control["claim"]["claim_id"],
        "claim_generation": control["claim"]["claim_generation"],
        "report": {**control["runtime"], "account_fp": None, "identity_verified": False,
                   "capabilities": {}, "observed_at": NOW}}}


def log_messages(caplog):
    records = [record for record in caplog.records if record.name == machine.log.name]
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    return [record.getMessage() for record in records]


@pytest.mark.parametrize("cleanup", [True, False])
def test_failed_collection_logs_code_only_and_preserves_bad_report_and_quarantine(
        profile, cfg, monkeypatch, caplog, cleanup):
    spawn = failed_spawn(profile, "native_metadata_timeout", cleanup=cleanup)
    original_collect = p.collect

    def collect(*args, **kwargs):
        return original_collect(profile.directory, profile.account, profile.control, spawn=spawn,
            lookup=profile.lookup, groups_of=profile.groups_of, now=profile.now)

    monkeypatch.setattr(p, "collect", collect)
    fake, state, control = machine_scenario(monkeypatch, profile.control)
    original = copy.deepcopy(state)
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=False)
    assert reports == bad_report(control) and private == {}
    assert state == original and len(profile.calls) == 1
    assert log_messages(caplog) == [
        "native metadata collection failed reason=native_metadata_timeout"]
    snapshot = Path(profile.verifier.pw_dir) / ".local/share/devin/credentials.toml"
    assert snapshot.exists() is not cleanup
    text = caplog.text
    for value in (SECRET, profile.account.pw_name, profile.account.pw_dir,
                  profile.verifier.pw_dir, control["claim"]["claim_id"], "FAKE_NATIVE"):
        assert value not in text


@pytest.mark.parametrize("error", [
    OSError(SECRET), ValueError(SECRET), TypeError(SECRET), KeyError(SECRET),
    RecursionError(SECRET), p.ProvisionError(SECRET),
    p.ProvisionError(SECRET, collector_reason=SECRET),
    p.ProvisionError(SECRET, collector_reason=UntrustedString("native_metadata_timeout")),
])
def test_machine_never_logs_exception_text_or_unknown_diagnostics(cfg, monkeypatch, caplog, error):
    fake, state, control = machine_scenario(monkeypatch)

    def collect(*args, **kwargs):
        raise error

    monkeypatch.setattr(p, "collect", collect)
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=False)
    assert reports == bad_report(control) and private == {}
    assert log_messages(caplog) == ["native metadata collection failed reason=unavailable"]
    assert SECRET not in caplog.text


@pytest.mark.parametrize("tampered", [SECRET, [], UntrustedString("native_metadata_timeout")])
def test_log_boundary_rechecks_exception_reason(cfg, monkeypatch, caplog, tampered):
    error = p.ProvisionError(SECRET, collector_reason="native_metadata_timeout")
    error.collector_reason = tampered
    fake, state, control = machine_scenario(monkeypatch)

    def collect(*args, **kwargs):
        raise error

    monkeypatch.setattr(p, "collect", collect)
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=False)
    assert reports == bad_report(control) and private == {}
    assert log_messages(caplog) == ["native metadata collection failed reason=unavailable"]
    assert SECRET not in caplog.text


def test_fast_cached_failures_stay_quiet_and_bad(cfg, monkeypatch, caplog):
    fake, state, control = machine_scenario(monkeypatch)

    def observation(*args, **kwargs):
        raise p.ProvisionError(SECRET, collector_reason="native_metadata_timeout")

    monkeypatch.setattr(p, "clean_observation", observation)
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=True)
    assert reports == bad_report(control) and private == {}
    assert log_messages(caplog) == []


def test_successful_metadata_has_no_failure_log_or_public_diagnostic_fields(cfg, monkeypatch, caplog):
    fake, state, _ = machine_scenario(monkeypatch)
    observation = test_machine_devin.observation()
    monkeypatch.setattr(p, "collect", lambda *args, **kwargs: observation)
    reports, private = machine.native_provider_observations(cfg, state, fake.system(), fast=False)
    assert private == {"slot01": observation}
    assert reports == {"slot01": p.public_report(observation)}
    assert set(reports["slot01"]) == {"claim_id", "claim_generation", "report"}
    assert "reason" not in json.dumps(reports) and log_messages(caplog) == []
