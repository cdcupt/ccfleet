"""Health observations do not export identities or establish live model readiness."""

import json

import pytest

from ccfleet_agent import client_experience as ux
from ccfleetd.client_status import health, quota_observation
from tests.test_api import _active_cli_slot, call, server  # noqa: F401

NOW = 10000.0


def credentials(**changes):
    return {"logged_in": True, "present": True, "expires_at": (NOW + 3600) * 1000,
            "bound_fp": "a" * 16, "account_fp": "a" * 16, **changes}


def state(creds=None, *, heard=NOW, login=None):
    return health({"state": "active"}, {"credentials": creds or credentials()}, login,
                  heard=heard, now=NOW, max_age=300)


def test_current_status_is_explicitly_a_reported_observation():
    result = state()
    assert result["health"] == "ready" and result["ready"]
    assert result["readiness_source"] == "reported"


@pytest.mark.parametrize("values,expected", [
    ({"logged_in": False}, "sign_in_required"),
    ({"present": False}, "sign_in_required"),
    ({"expires_at": (NOW - 1000) * 1000}, "renewal_pending"),
    ({"expires_at": None}, "degraded"),
    ({"account_fp": "b" * 16}, "switching"),
    ({"bound_fp": None}, "degraded"),
])
def test_recovery_states_are_fixed_and_do_not_claim_readiness(values, expected):
    result = state(credentials(**values))
    assert result["health"] == expected and result["ready"] is False


@pytest.mark.parametrize("capability,warning", [
    ({"refresh_available": False}, "refresh_unavailable"),
    ({"refresh_expires_at": NOW - 1}, "refresh_expired"),
    ({"refresh_expires_at": NOW + 3 * 86400}, "login_expiring"),
    ({"expires_at": (NOW + 100) * 1000}, "renewal_due"),
    ({"renewal": {"state": "retrying", "checked_at": NOW}}, "native_renewal_pending"),
])
def test_current_access_stays_reported_ready_with_a_separate_renewal_warning(capability, warning):
    result = state(credentials(**capability))
    assert result["health"] == "ready" and result["ready"] is True
    assert result["renewal_warning"] == warning
    assert result["readiness_source"] == "reported"


@pytest.mark.parametrize("capability,warning", [
    ({"refresh_available": False}, "refresh_unavailable"),
    ({"refresh_expires_at": NOW - 1}, "refresh_expired"),
])
def test_unusable_access_with_terminal_refresh_capability_requires_sign_in(capability, warning):
    result = state(credentials(expires_at=(NOW - 6000) * 1000, **capability))
    assert result["health"] == "sign_in_required" and result["reason"] == "credential_expired"
    assert result["renewal_warning"] == warning
    assert result["ready"] is False


@pytest.mark.parametrize("reason", ["native_auth_rejected", "native_login_expired"])
def test_explicit_native_login_failure_overrides_cached_unexpired_access(reason):
    result = state(credentials(renewal={"state": "blocked", "checked_at": NOW,
                                       "reason": reason}))
    assert result["health"] == "sign_in_required"
    assert result["reason"] == ("not_signed_in" if reason == "native_auth_rejected" else
                                "credential_expired")
    assert result["renewal_warning"] == reason
    assert result["ready"] is False


@pytest.mark.parametrize("reason", ["native_network_error", "native_rate_limited",
                                   "native_timeout", "native_launch_failed", "native_probe_busy"])
def test_temporary_failures_during_unusable_access_remain_recoverable(reason):
    result = state(credentials(expires_at=(NOW - 6000) * 1000,
        renewal={"state": "retrying", "checked_at": NOW, "reason": reason,
                 "outcome": reason, "probe_started": False}))
    assert result["health"] == "renewal_pending" and result["ready"] is False
    assert result["renewal_warning"] == reason


def test_legacy_missing_refresh_fields_are_unknown_without_a_sign_in_demand():
    assert "renewal_warning" not in state()
    result = state(credentials(expires_at=(NOW - 6000) * 1000))
    assert result["health"] == "renewal_pending"
    assert "renewal_warning" not in result


def test_stale_terminal_observation_does_not_invent_current_sign_in_demand():
    result = state(credentials(refresh_available=False,
        renewal={"state": "blocked", "checked_at": NOW - 1000,
                 "reason": "native_auth_rejected"}))
    assert result["health"] == "degraded" and result["reason"] == "observation_stale"
    assert "renewal_warning" not in result


def test_temporary_renewal_failure_does_not_suspend_reported_current_access():
    result = state(credentials(renewal={"state": "retrying", "checked_at": NOW,
                                       "reason": "native_network_error"}))
    assert result["health"] == "ready" and result["ready"] is True
    assert result["renewal_warning"] == "native_network_error"


@pytest.mark.parametrize("changes", [
    {}, {"refresh_available": False}, {"refresh_expires_at": NOW - 1},
    {"refresh_expires_at": NOW + 86400}, {"expires_at": (NOW + 100) * 1000},
    {"expires_at": (NOW - 6000) * 1000},
    {"expires_at": (NOW - 6000) * 1000, "refresh_available": False},
    {"expires_at": (NOW - 6000) * 1000, "refresh_expires_at": NOW - 1},
    *[{"renewal": {"state": "blocked" if reason.startswith("native_auth") or
                    reason == "native_login_expired" else "retrying", "checked_at": NOW,
                    "reason": reason}} for reason in (
        "native_auth_rejected", "native_login_expired", "native_network_error",
        "native_rate_limited", "native_timeout", "native_launch_failed", "native_probe_busy")],
])
def test_new_server_health_remains_compatible_with_unchanged_protocol2_management(changes):
    slot = state(credentials(**changes))
    source = {"authenticated": True, "protocol": 2, "device": {"active": True}, "slot": slot}
    projected = ux.management_health(source, now=NOW)
    assert projected["health"] == slot["health"] and projected["reason"] == slot["reason"]
    assert projected["reported_ready"] is slot["ready"]
    assert projected["provider_acceptance_verified"] is False
    assert ux.management_quota(source, now=NOW)["available"] is False
    assert ux.management_relay_usage(source, now=NOW)["available"] is False
    assert "renewal_warning" not in projected  # Older clients safely ignore additive facts.


def test_stale_or_future_observation_cannot_claim_ready():
    for heard in (NOW - 1000, NOW + 1000, None):
        assert state(heard=heard)["ready"] is False


@pytest.mark.parametrize("pending", ["requested", "waiting", "running", "url_ready", "code_sent"])
def test_pending_login_keeps_account_transition_visible(pending):
    assert state(login={"state": pending, "url": "SECRET"})["health"] == "switching"


def test_private_strings_never_appear_in_report():
    result = state(credentials(email="SECRET_EMAIL", token="SECRET_TOKEN", path="SECRET_PATH",
                               renewal={"state": "blocked", "checked_at": NOW,
                                        "detail": "SECRET_BODY"}))
    assert "SECRET" not in json.dumps(result)


def test_cached_quota_retains_only_numeric_observation_and_reset_fields():
    quota = {"checked_at": NOW - 10, "accountUuid": "SECRET_ACCOUNT",
             "session": {"used_pct": 0, "resets_at": NOW + 300, "resets": "SECRET_LOCATION"},
             "week": {"used_pct": 99.5, "resets_at": NOW + 86400, "email": "SECRET_EMAIL"}}
    expected = {"checked_at": NOW - 10, "session": {"used_pct": 0, "resets_at": NOW + 300},
                "week": {"used_pct": 99.5, "resets_at": NOW + 86400}}
    assert quota_observation(quota, {"claimed_at": NOW - 100}, NOW) == expected
    result = health({"state": "active", "claimed_at": NOW - 100},
                    {"credentials": credentials(), "quota": quota}, None,
                    heard=NOW, now=NOW, max_age=300)
    assert result["quota"] == expected and "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("checked", [None, "SECRET", True, float("nan"), float("inf"), 0,
                                     NOW + 61, NOW - 200])
def test_quota_missing_invalid_future_or_before_assignment_is_unknown(checked):
    assert quota_observation({"checked_at": checked, "session": {"used_pct": 20}},
                             {"claimed_at": NOW - 100}, NOW) == {}


def test_quota_before_account_switch_and_during_transition_is_not_exported():
    quota = {"checked_at": NOW - 10, "session": {"used_pct": 20}}
    assert quota_observation(quota, {"account_switched_at": NOW - 5}, NOW) == {}
    result = health({"state": "active"}, {"credentials": credentials(), "quota": quota},
                    {"state": "waiting"}, heard=NOW, now=NOW, max_age=300)
    assert "quota" not in result


@pytest.mark.parametrize("reset", ["SECRET", True, float("nan"), float("inf"), -1,
                                   NOW - 11, NOW + 9 * 86400])
def test_quota_invalid_reset_is_omitted_without_losing_valid_percentage(reset):
    assert quota_observation({"checked_at": NOW - 10,
                              "week": {"used_pct": 50, "resets_at": reset}}, {}, NOW) == {
        "checked_at": NOW - 10, "week": {"used_pct": 50}}


@pytest.mark.parametrize("used", ["SECRET", True, float("nan"), float("inf"), -1, 101])
def test_invalid_quota_numbers_never_become_zero(used):
    assert quota_observation({"checked_at": NOW, "session": {"used_pct": used}}, {}, NOW) == {}


def test_authenticated_endpoint_is_read_only_and_denies_after_revoke(server):  # noqa: F811
    srv, store = server
    slot = _active_cli_slot(store)
    token = store.request_cli_pairing(slot["id"], slot["held_by"], now=NOW)
    device = store.register_cli_device(token, store.get_node(slot["node_id"])["ssh_host_key"],
                                       "SECRET_DEVICE", now=NOW + 1)
    auth = {"Authorization": "Bearer " + device["device_token"]}
    status, raw, _ = call(srv, "GET", "/api/cli/status", headers=auth)
    assert status == 200 and json.loads(raw)["authenticated"] is True
    assert all(value.encode() not in raw for value in
               ("SECRET_DEVICE", device["device_token"], slot["held_by"], slot["node_id"]))
    assert store.list_cli_devices(slot["id"])[0]["last_seen_at"] == 0
    generation = json.loads(raw)["slot"]["account_generation"]
    assert len(generation) == 24
    store._conn.execute("UPDATE slots SET account_switched_at=? WHERE id=?", (NOW, slot["id"]))
    store._conn.commit()
    changed = json.loads(call(srv, "GET", "/api/cli/status", headers=auth)[1])
    assert changed["slot"]["account_generation"] != generation
    store.revoke_cli_token(device["device_token"])
    assert call(srv, "GET", "/api/cli/status", headers=auth)[0] == 401
