"""Hosted credential health never forwards secrets or equates a file with readiness."""
import json

import pytest

from ccfleetd import credential_health as health
from ccfleetd import heartbeat, rules, status, usersite
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


@pytest.mark.parametrize("prior_rule", ["slot_token_expired:slot01", "slot_credential_renewal:slot01",
                                      "slot_sign_in_required:slot01", "slot_login_expiring:slot01"])
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


@pytest.mark.parametrize("outcome", sorted(health.RENEWAL_OUTCOMES))
@pytest.mark.parametrize("started", [True, False])
def test_safe_native_probe_outcome_and_confirmed_launch_are_allowlisted(outcome, started):
    value = renewal(outcome=outcome, probe_started=started, raw_output="SECRET")
    checked = heartbeat.validate_heartbeat({"node_id": "m1", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": report(refresh_available=True,
            refresh_expires_at=NOW + 86400, refreshToken="SECRET", renewal=value)}]}, "m1")
    facts = checked["slots"][0]["credentials"]
    assert facts["refresh_available"] is True
    assert facts["refresh_expires_at"] == NOW + 86400
    assert facts["renewal"]["outcome"] == outcome
    assert facts["renewal"]["probe_started"] is started
    assert "SECRET" not in json.dumps(checked)


@pytest.mark.parametrize("bad", [True, False, None, "SECRET", float("nan"), float("inf"),
                                 -1, 0, 10**400, {}, []])
def test_longer_lived_expiry_is_a_bounded_positive_epoch_in_seconds(bad):
    checked = heartbeat.validate_heartbeat({"node_id": "m1", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": report(refresh_expires_at=bad)}]}, "m1")
    assert checked["slots"][0]["credentials"]["refresh_expires_at"] is None
    assert health.sign_in_reason(report(refresh_expires_at=bad), NOW) is None


@pytest.mark.parametrize("bad", [None, "SECRET", 0, 1, {}, []])
def test_unknown_probe_launch_and_refresh_capability_are_not_fabricated(bad):
    value = health.renewal_report(renewal(outcome=bad, probe_started=bad))
    assert "outcome" not in value and "probe_started" not in value
    checked = heartbeat.validate_heartbeat({"node_id": "m1", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": report(refresh_available=bad)}]}, "m1")
    assert checked["slots"][0]["credentials"]["refresh_available"] is None
    assert health.sign_in_reason(report(refresh_available=bad), NOW) is None


def test_legacy_reports_do_not_claim_refresh_capability_or_probe_launch():
    checked = heartbeat.validate_heartbeat({"node_id": "m1", "mode": "machine", "slots": [
        {"unix_user": "slot01", "credentials": report(renewal=renewal())}]}, "m1")
    facts = checked["slots"][0]["credentials"]
    assert facts["refresh_available"] is None and facts["refresh_expires_at"] is None
    assert "probe_started" not in facts["renewal"] and "outcome" not in facts["renewal"]


@pytest.mark.parametrize("seconds,expected", [(3 * 86400 + 1, None),
                                            (3 * 86400, "slot_login_expiring:slot01"),
                                            (1, "slot_login_expiring:slot01"),
                                            (0, "slot_sign_in_required:slot01")])
def test_longer_lived_sign_in_warns_three_days_before_expiry(seconds, expected):
    found = findings(report(refresh_available=True, refresh_expires_at=NOW + seconds))
    assert [f.rule for f in found] == ([expected] if expected else [])
    if found:
        assert found[0].level == "warn"
        assert status.rule_state(found[0].rule, found[0].level) is None
    if seconds > 0 and expected:
        assert "within 3 days" in usersite._in_use({"credentials": report(
            refresh_available=True, refresh_expires_at=NOW + seconds)}, NOW)


@pytest.mark.parametrize("state", ["free", "claiming", "claimed", "releasing"])
def test_unheld_or_transitional_slots_have_no_login_expiry_warning(state):
    found = findings(report(refresh_available=False, refresh_expires_at=NOW + 1), state=state)
    assert all(not f.rule.startswith(("slot_login_expiring", "slot_sign_in_required"))
               for f in found)


def test_upcoming_login_expiry_waits_for_fresh_unchanging_account_facts():
    creds = report(refresh_available=True, refresh_expires_at=NOW + 1)
    assert findings(creds, login={"state": "waiting"}) == ()
    assert [f.rule for f in findings({**creds, "account_fp": "b" * 16})] == ["account_changed:slot01"]
    assert findings(creds, heard=NOW - 60, listening=NOW - 5) == ()
    assert [f.rule for f in findings(creds, heard=NOW - 3600)] == ["no_heartbeat"]


@pytest.mark.parametrize("reason", ["native_network_error", "native_rate_limited",
                                   "native_timeout", "native_launch_failed", "native_probe_busy"])
def test_temporary_renewal_failure_has_fixed_automatic_recovery_copy(reason):
    creds = report(expires_at=(NOW - 60) * 1000,
                   renewal=renewal(reason=reason, outcome=reason, probe_started=False))
    [found] = findings(creds)
    assert found.rule == "slot_token_expired:slot01" and found.level == "critical"
    assert "temporary" in found.message and "retried automatically" in found.message
    page = usersite._in_use({"credentials": creds}, NOW)
    assert "temporary" in page and "retried automatically" in page
    assert "Use Sign in again" not in page and "revoked" not in page


@pytest.mark.parametrize("reason", ["native_auth_rejected", "native_login_expired"])
def test_native_login_failure_requires_fresh_sign_in_despite_cached_access_expiry(reason):
    creds = report(renewal=renewal(state="blocked", reason=reason, outcome=reason))
    [found] = findings(creds)
    assert found.rule == "slot_sign_in_required:slot01"
    page = usersite._in_use({"credentials": creds}, NOW)
    assert "Native Claude requires a fresh sign-in" in page and "Signed in" not in page
    assert "Your pairing, files and history are kept" in page


@pytest.mark.parametrize("capability", [{"refresh_available": False},
                                      {"refresh_expires_at": NOW - 1}])
def test_capability_only_warning_keeps_current_access_distinct_from_native_login_failure(capability):
    creds = report(**capability)
    [found] = findings(creds)
    assert found.rule == "slot_sign_in_required:slot01" and found.level == "warn"
    assert "has not expired" in found.message and "live model access is unverified" in found.message
    page = usersite._in_use({"credentials": creds}, NOW)
    assert "Signed in" in page and "has not expired" in page
    assert "restore automatic renewal" in page and "usable" not in page


def test_legacy_access_expiry_is_a_renewal_need_without_revocation_evidence():
    creds = report(expires_at=(NOW - 6000) * 1000)
    [found] = findings(creds)
    assert found.rule == "slot_token_expired:slot01"
    assert "Sign in again" not in found.message
    page = usersite._in_use({"credentials": creds}, NOW)
    assert "Access expiry alone does not mean a revoked login" in page


def test_ready_holder_card_warns_about_capability_without_claiming_provider_acceptance():
    page = usersite._account_health({"state": "active"}, {"credentials": report(
        refresh_available=False)}, {}, NOW, Config(), NOW)
    assert 'data-health="ready"' in page and "Reported ready" in page
    assert "has not expired" in page and "live model access is unverified" in page
    assert "restore automatic renewal" in page and "remains usable" not in page


def test_unusable_holder_card_distinguishes_temporary_failure_from_sign_in_required():
    transient = report(expires_at=(NOW - 60) * 1000,
                       renewal=renewal(reason="native_network_error", raw_output="SECRET"))
    page = usersite._account_health({"state": "active"}, {"credentials": transient}, {},
                                   NOW, Config(), NOW)
    assert 'data-health="renewal_pending"' in page and "retried automatically" in page
    assert "SECRET" not in page
    terminal = {**transient, "renewal": renewal(state="blocked", reason="native_auth_rejected")}
    page = usersite._account_health({"state": "active"}, {"credentials": terminal}, {},
                                   NOW, Config(), NOW)
    assert 'data-health="sign_in_required"' in page and "Sign-in required" in page
    assert "retried automatically" not in page and "saved local conversations are kept" in page


def test_a_file_with_unknown_native_sign_in_does_not_establish_a_signed_in_holder():
    page = usersite._in_use({"credentials": report(logged_in=None)}, NOW)
    assert "sign-in could not be verified" in page and "Signed in" not in page
