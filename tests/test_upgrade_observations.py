"""Upgrade results must retain provenance without exposing diagnostic content."""

import json

import pytest

from ccfleet_agent.compatibility import CHECKS
from ccfleetd import client_status, compatibility, rules, status, usersite
from ccfleetd.credential_health import recovery_guidance, sign_in_reason
from ccfleetd.heartbeat import validate_heartbeat
from ccfleetd.monitor import Monitor
from tests.test_cli_access import active_slot
from tests.test_monitor import Recorder

NOW = 1_800_000_000.0
NODE = {"id": "node-a", "owner": "erik", "region": "", "enabled": True,
        "pinned_version": "", "rc_expected": False, "created_at": NOW - 86400}
SLOT = {"id": "node-a", "node_id": "node-a", "unix_user": "slot01", "state": "active",
        "kind": "machine", "claimed_at": NOW - 120, "account_switched_at": None}


def credentials(**changes):
    return {"present": True, "logged_in": True, "checked_at": NOW,
            "account_fp": "a" * 16, "bound_fp": "a" * 16,
            "expires_at": (NOW + 7200) * 1000, **changes}


def validation(**changes):
    return {"state": "passed", "native_version": "2.1.295", "runtime_fp": "b" * 64,
            "account_fp": "a" * 16, "checked_at": NOW - 60,
            "usage_observed_at": NOW - 30, "checks": dict.fromkeys(CHECKS, True), **changes}


def observed(raw=None, *, creds=None, **changes):
    args = {"heard": NOW, "now": NOW, "max_age": 900, "since": NOW - 120, **changes}
    return compatibility.observation(raw or validation(), {"version": "2.1.295"},
                                     creds or credentials(), **args)


def payload(raw):
    return {"mode": "machine", "node_id": "node-a", "ts": NOW, "slots": [{
        "unix_user": "slot01", "present": True, "claude": {"version": "2.1.295"},
        "credentials": credentials(), "compatibility": raw}]}


def test_heartbeat_removes_unknown_private_values_and_device_status_keeps_safe_checks():
    sent = validation(raw_stdout="PRIVATE-OUTPUT", authorization="PRIVATE-TOKEN",
                      metadata={"user_id": "PRIVATE-ID"})
    clean = validate_heartbeat(payload(sent), "node-a", now=NOW)
    report = clean["slots"][0]
    assert "PRIVATE-" not in json.dumps(clean)
    state = client_status.health(SLOT, report, None, heard=NOW, now=NOW, max_age=900)
    assert state["ready"] is True and state["compatibility"]["state"] == "passed"
    assert state["compatibility"]["checks"] == dict.fromkeys(CHECKS, True)
    assert "runtime_fp" not in state["compatibility"]
    assert "account_fp" not in state["compatibility"]


@pytest.mark.parametrize("changes", [
    {"heard": NOW - 901}, {"heard": NOW + 61}, {"heard": True},
    {"listening_since": NOW + 1}, {"since": NOW - 20},
])
def test_stale_or_unattributed_observations_never_report_passed(changes):
    assert observed(**changes) == {}


@pytest.mark.parametrize("changes", [
    {"native_version": "2.1.294"}, {"account_fp": "c" * 16},
    {"checked_at": NOW + 61}, {"usage_observed_at": NOW + 61},
    {"checks": dict.fromkeys(CHECKS, False)}, {"runtime_fp": "PRIVATE-HASH"},
    {"checked_at": True}, {"state": "PRIVATE-STATE"},
])
def test_invalid_or_wrong_generation_success_is_not_inherited(changes):
    assert observed(validation(**changes)) == {}


def test_old_actual_test_time_can_be_cached_under_a_current_matching_observation():
    raw = validation(checked_at=NOW - 86400, usage_observed_at=NOW - 86390)
    assert observed(raw, since=NOW - 2 * 86400)["checked_at"] == NOW - 86400
    assert observed(raw, since=NOW - 100) == {}


def test_failed_validation_warns_without_fabricating_sign_in_failure(cfg):
    raw = validation(state="failed", reason="native_interface_changed",
                     checks={"native_version": True, "auth_interface": False})
    report = payload(raw)["slots"][0]
    state = client_status.health(SLOT, report, None, heard=NOW, now=NOW, max_age=900)
    assert state["ready"] is True and state["reason"] == "credentials_current"
    assert state["compatibility"]["state"] == "failed"
    page = usersite._account_health(SLOT, report, {}, NOW, cfg, NOW)
    assert "Update checks need attention" in page
    assert "Sign-in required" not in page
    found = rules.evaluate(NODE, {"ts": NOW, "payload": payload(raw)}, None,
                           NOW, cfg, [SLOT])
    assert any(f.rule == "slot_native_compatibility:slot01" for f in found)
    assert status.rule_state("slot_native_compatibility:slot01", "warn") is None


@pytest.mark.parametrize("state,reason", [
    ("pending", "usage_pending"), ("blocked", "sign_in_pending"), ("passed", None),
])
def test_normal_pending_and_transition_checks_are_not_new_failure_alerts(cfg, state, reason):
    raw = validation(state=state, reason=reason)
    found = rules.evaluate(NODE, {"ts": NOW, "payload": payload(raw)}, None,
                           NOW, cfg, [SLOT])
    assert not any(f.rule.startswith("slot_native_compatibility") for f in found)


def test_unknown_reason_text_cannot_be_reflected_in_recovery_copy(cfg):
    raw = validation(state="failed", reason="PRIVATE-TOKEN-IN-ERROR")
    report = payload(raw)["slots"][0]
    page = usersite._account_health(SLOT, report, {}, NOW, cfg, NOW)
    assert "PRIVATE-" not in page
    assert "Waiting for confirmed update validation" in page


def test_blocked_native_auth_source_is_actionable_without_model_or_account_switch(cfg):
    raw = validation(state="blocked", reason="native_auth_source")
    found = rules.evaluate(NODE, {"ts": NOW, "payload": payload(raw)}, None,
                           NOW, cfg, [SLOT])
    assert any(f.rule == "slot_native_compatibility:slot01" for f in found)


@pytest.mark.parametrize("error", ["PRIVATE-INSTALLER-TOKEN", {"secret": "PRIVATE"},
                                  ["PRIVATE"], True, 7])
def test_legacy_or_malformed_installer_diagnostics_are_not_persisted(error):
    sent = payload(validation())
    sent["slots"][0]["upgrade"] = {"ok": False, "error": error, "ts": NOW}
    sent["slots"][0]["claude_update"] = {"state": "failed", "requested_at": NOW,
                                         "detail": "PRIVATE-INSTALLER-BODY"}
    clean = validate_heartbeat(sent, "node-a", now=NOW)
    assert "PRIVATE" not in json.dumps(clean)
    assert clean["slots"][0]["upgrade"]["error"] == "native_install_failed"


@pytest.mark.parametrize("raw", [None, {"state": "pending", "checked_at": NOW,
    "native_version": "2.1.295", "reason": "usage_pending"},
    {"state": "blocked", "checked_at": NOW, "native_version": "2.1.295",
     "reason": "probe_busy"}])
def test_monitor_retains_failure_until_confirmed_recovery(store, cfg, raw):
    active_slot(store)
    rule = "slot_native_compatibility:slot01"
    store.open_alert("m1", rule, "warn", "prior failed validation", NOW - 20)
    monitor = Monitor(store, cfg, Recorder(), clock=lambda: NOW)
    incoming = payload(raw)
    events = monitor.record_heartbeat(store.get_node("m1"), incoming, now=NOW)
    assert not any(e["event"] == "closed" and e["alert"]["rule"] == rule for e in events)
    assert any(a["rule"] == rule for a in store.open_alerts("m1"))
    incoming = payload(validation(checked_at=NOW + 1, usage_observed_at=NOW + 2))
    events = monitor.record_heartbeat(store.get_node("m1"), incoming, now=NOW + 3)
    assert any(e["event"] == "closed" and e["alert"]["rule"] == rule for e in events)


def test_validation_alert_is_not_inherited_after_account_adoption(store, cfg):
    active_slot(store)
    rule = "slot_native_compatibility:slot01"
    store.open_alert("m1", rule, "warn", "prior account validation", NOW - 20)
    store._conn.execute("UPDATE slots SET account_switched_at = ? WHERE id = ?", (NOW - 5, "s1"))
    monitor = Monitor(store, cfg, Recorder(), clock=lambda: NOW)
    events = monitor.record_heartbeat(store.get_node("m1"), payload(None), now=NOW)
    assert any(e["event"] == "closed" and e["alert"]["rule"] == rule for e in events)


@pytest.mark.parametrize("reason", ["native_auth_source", "native_extensions"])
def test_configuration_blocker_never_invents_login_revocation(reason):
    creds = credentials(renewal={"state": "blocked", "reason": reason, "outcome": reason,
                                 "checked_at": NOW, "probe_started": False})
    assert sign_in_reason(creds, NOW) is None
    assert "operator review" in recovery_guidance(creds, NOW)
    assert "fresh Claude sign-in is required" not in recovery_guidance(creds, NOW)
    state = client_status.health(SLOT, {"credentials": creds}, None,
                                heard=NOW, now=NOW, max_age=900)
    assert state["ready"] is True and state["renewal_warning"] == "native_renewal_pending"
