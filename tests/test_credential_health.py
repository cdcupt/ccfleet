"""Hosted credential health never forwards secrets or equates a file with readiness."""
import json

import pytest

from ccfleetd import credential_health as health
from ccfleetd import heartbeat, rules, usersite
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from tests.test_cli_access import active_slot
from tests.test_monitor import Recorder

NOW = 2_000_000_000.0
FP = "a" * 16


def report(**changes):
    return {"present": True, "logged_in": True, "account_fp": FP, "bound_fp": FP,
            "expires_at": (NOW + 3600) * 1000, **changes}


def findings(creds, *, state="active", heard=NOW, listening=None, raised=frozenset(),
             login=None):
    payload = {"mode": "machine", "slots": [{"unix_user": "slot01", "present": True,
               "credentials": creds, "login": login}]}
    return rules.evaluate({"id": "m1"}, {"ts": heard, "payload": payload}, None,
                          NOW, Config(), [{"id": "s1", "unix_user": "slot01", "state": state}],
                          listening_since=listening, raised=raised)


def renewal(state="retrying", reason="native_refresh_unconfirmed", **fields):
    return {"state": state, "reason": reason, "checked_at": NOW,
            "last_attempt_at": NOW - 60, "next_attempt_at": NOW + 60, **fields}


def test_heartbeat_retains_only_allowlisted_renewal_facts():
    raw = renewal(raw_output="SECRET", accessToken="SECRET", accountUuid="SECRET",
                  last_success_at=NOW - 100)
    checked = heartbeat.validate_heartbeat({"node_id": "m1", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": report(renewal=raw)}]}, "m1")
    actual = checked["slots"][0]["credentials"]["renewal"]
    assert actual == renewal(last_success_at=NOW - 100)
    assert "SECRET" not in json.dumps(checked)


@pytest.mark.parametrize("bad", [True, False, None, "SECRET", float("nan"), float("inf"),
                                 -1, 0, 10**400, {}, []])
def test_renewal_timestamps_and_access_expiry_are_bounded(bad):
    assert health.renewal_report(renewal(checked_at=bad)) == {}
    assert health.access_expiry({"expires_at": bad}) is None
    result = health.renewal_report(renewal(next_attempt_at=bad))
    assert "next_attempt_at" not in result


@pytest.mark.parametrize("state", [None, [], {}, True, "SECRET"])
def test_unknown_renewal_states_are_not_a_raw_error_channel(state):
    assert health.renewal_report(renewal(state=state)) == {}


def test_unknown_reason_is_dropped_without_losing_safe_state():
    assert health.renewal_report(renewal(reason="SECRET")) == {
        "state": "retrying", "checked_at": NOW, "last_attempt_at": NOW - 60,
        "next_attempt_at": NOW + 60}


def test_current_native_expiry_has_no_credential_alert():
    assert findings(report(renewal=renewal(state="current", reason=None))) == ()


@pytest.mark.parametrize("seconds,level", [(-60, "critical"), (0, "critical"), (30, "warn")])
def test_expired_or_unusable_hosted_token_is_not_hidden_by_logged_in(seconds, level):
    [found] = findings(report(expires_at=(NOW + seconds) * 1000, renewal=renewal()))
    assert found.rule == "slot_token_expired:slot01" and found.level == level
    assert "model relay is unavailable" in found.message
    page = usersite._in_use({"credentials": report(
        expires_at=(NOW + seconds) * 1000, renewal=renewal())}, NOW)
    assert "Model access needs credential renewal" in page and "Signed in" not in page
    assert "pairing, files and history are kept" in page


def test_unconfirmed_early_renewal_warns_without_claiming_authentication_failure():
    creds = report(expires_at=(NOW + 120) * 1000, renewal=renewal())
    [found] = findings(creds)
    assert found.rule == "slot_credential_renewal:slot01" and found.level == "warn"
    assert "not yet confirmed" in usersite._in_use({"credentials": creds}, NOW)
    assert "Not signed in" not in usersite._in_use({"credentials": creds}, NOW)
    assert findings(report(renewal=renewal())) == ()


@pytest.mark.parametrize("state", ["free", "claiming", "claimed", "releasing"])
def test_only_active_slots_receive_credential_maintenance_alerts(state):
    result = findings(report(expires_at=(NOW - 60) * 1000), state=state)
    assert not any(f.rule.startswith("slot_token") for f in result)


@pytest.mark.parametrize("reason", sorted(health.TRANSITION_REASONS))
def test_expected_account_transition_is_not_a_renewal_failure(reason):
    creds = report(expires_at=(NOW - 60) * 1000, renewal=renewal(state="blocked", reason=reason))
    assert findings(creds) == ()
    assert "account maintenance is in progress" in usersite._in_use({"credentials": creds}, NOW)


def test_pending_login_or_account_mismatch_does_not_add_duplicate_renewal_failures():
    creds = report(expires_at=(NOW - 60) * 1000)
    assert findings(creds, login={"state": "waiting"}) == ()
    changed = findings({**creds, "account_fp": "b" * 16})
    assert all(not f.rule.startswith("slot_token") for f in changed)


def test_restart_waits_for_fresh_credential_facts_without_closing_existing_failure():
    creds = report(expires_at=(NOW - 60) * 1000)
    assert findings(creds, heard=NOW - 60, listening=NOW - 5) == ()
    [found] = findings(creds, heard=NOW - 60, listening=NOW - 5,
                       raised=frozenset({"slot_token_expired:slot01"}))
    assert found.rule == "slot_token_expired:slot01"
    stale = findings(creds, heard=NOW - 3600)
    assert [f.rule for f in stale] == ["no_heartbeat"]


def test_missing_and_unknown_credentials_are_not_reported_as_ready():
    [missing] = findings({"present": False, "logged_in": False})
    assert missing.rule == "slot_credentials_missing:slot01"
    creds = report(expires_at=None, renewal=renewal(state="blocked", reason="expiry_unknown"))
    [unknown] = findings(creds)
    assert unknown.rule == "slot_credential_renewal:slot01"
    assert "could not be verified" in usersite._in_use({"credentials": creds}, NOW)
    assert findings({}) == ()  # An old/missing report is not fabricated into a failure.


def test_observed_renewal_does_not_render_raw_error_or_account_id():
    creds = report(renewal=renewal(state="renewed", reason="SECRET", last_success_at=NOW - 60))
    page = usersite._in_use({"credentials": creds}, NOW)
    assert "Last observed native credential renewal" in page
    assert "SECRET" not in page and FP not in page


@pytest.mark.parametrize("prior_rule", ["slot_token_expired:slot01", "slot_credential_renewal:slot01"])
@pytest.mark.parametrize("old_heartbeat,cached_slot", [(True, False), (False, True)])
def test_monitor_keeps_actual_credential_alert_until_fresh_recovery(
        store, prior_rule, old_heartbeat, cached_slot):
    active_slot(store)
    store.open_alert("m1", prior_rule, "warn", "last observed failure", NOW - 4000)
    stale_creds = report(expires_at=(NOW - 60) * 1000,
                         renewal=renewal(checked_at=NOW - 3600 if cached_slot else NOW - 4000))
    store.insert_heartbeat("m1", NOW - 3600 if old_heartbeat else NOW, {
        "mode": "machine", "slots": [{"unix_user": "slot01", "present": True,
                                         "credentials": stale_creds}]})
    store.set_listening_since(NOW - 5)
    notifier = Recorder()
    monitor = Monitor(store, Config(), notifier, clock=lambda: NOW)
    events = monitor.check_node(store.get_node("m1"), now=NOW)
    assert not any(e["event"] == "closed" and e["alert"]["rule"] == prior_rule for e in events)
    assert any(a["rule"] == prior_rule for a in store.open_alerts("m1"))
    # A fresh, healthy observation can resolve the actual previous alert.
    events = monitor.record_heartbeat(store.get_node("m1"), {
        "mode": "machine", "slots": [{"unix_user": "slot01", "present": True,
            "credentials": report(renewal=renewal(state="current", checked_at=NOW + 1))}]},
        now=NOW + 1)
    assert any(e["event"] == "closed" and e["alert"]["rule"] == prior_rule for e in events)


def test_no_credential_observation_cannot_resolve_an_existing_failure(store):
    active_slot(store)
    rule = "slot_token_expired:slot01"
    store.open_alert("m1", rule, "critical", "last observed failure", NOW - 60)
    store.insert_heartbeat("m1", NOW, {"mode": "machine", "slots": [
        {"unix_user": "slot01", "present": True}]})
    monitor = Monitor(store, Config(), Recorder(), clock=lambda: NOW)
    monitor.check_node(store.get_node("m1"))
    assert any(a["rule"] == rule for a in store.open_alerts("m1"))


def test_stale_user_page_makes_no_current_readiness_claim():
    for creds in (report(), report(expires_at=(NOW - 60) * 1000)):
        page = usersite._in_use({"credentials": creds}, NOW, fresh=False)
        assert "Waiting for fresh credential status" in page
        assert "Signed in" not in page and "needs credential renewal" not in page
        assert not health.fresh_observation(creds, NOW - 3600, NOW, 900)


def test_malformed_credential_report_has_a_distinct_uncertainty_warning():
    creds = report(expires_at=None, renewal=renewal(state="blocked", reason="credential_unavailable"))
    [found] = findings(creds)
    assert found.rule == "slot_credential_renewal:slot01"
    assert "could not be verified" in usersite._in_use({"credentials": creds}, NOW)
