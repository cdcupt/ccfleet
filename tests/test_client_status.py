"""Health observations do not export identities or establish live model readiness."""

import json

import pytest

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
    ({"expires_at": (NOW - 1000) * 1000}, "sign_in_required"),
    ({"expires_at": None}, "degraded"),
    ({"expires_at": (NOW + 100) * 1000}, "renewal_pending"),
    ({"account_fp": "b" * 16}, "switching"),
    ({"bound_fp": None}, "degraded"),
    ({"renewal": {"state": "retrying", "checked_at": NOW}}, "renewal_pending"),
])
def test_recovery_states_are_fixed_and_do_not_claim_readiness(values, expected):
    result = state(credentials(**values))
    assert result["health"] == expected and result["ready"] is False


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
