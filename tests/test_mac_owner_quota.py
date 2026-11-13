"""Private Mac quota combines its bound cache and bounded provider metadata reads."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ccfleet_agent import agent, mac_owner_agent, mac_policy
from tests.test_mac_owner_runtime import owner as synthetic_owner  # noqa: F401
from tests.test_private_owner_nodes import NOW, private  # noqa: F401


def facts(owner):
    return {"logged_in": True, "account_fp": owner[3], "bound_fp": owner[3],
            "expires_at": (NOW + 3600) * 1000}


def cache(owner, at, percent=100, account="synthetic-account"):
    path = owner[0] / ".claude.json"
    data = json.loads(path.read_text())
    data[agent.QUOTA_CACHE_KEY] = {"accountUuid": account, "fetchedAtMs": at * 1000,
        "utilization": {"seven_day": {"utilization": 51}, "limits": [
            {"kind": "weekly_scoped", "group": "weekly", "percent": percent,
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}}]}}
    path.write_text(json.dumps(data))


@pytest.fixture
def quota_runtime(synthetic_owner, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: {
        "mac_policy": mac_policy, "agent": agent}[name])
    monkeypatch.setattr(mac_owner_agent.time, "time", lambda: NOW)
    return synthetic_owner


def test_private_mac_reads_cached_fable_without_native_launch(quota_runtime, monkeypatch):
    owner = quota_runtime
    cache(owner, NOW - 20)
    monkeypatch.setattr(mac_owner_agent, "_provider_quota", lambda *a: pytest.fail("no probe"))
    got = mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW)
    assert got["week"]["used_pct"] == 51 and got["models"]["fable"]["used_pct"] == 100
    assert "synthetic-account" not in json.dumps(got) and "never-report-this" not in json.dumps(got)


def test_private_mac_polls_structured_usage_at_most_every_five_minutes(quota_runtime, monkeypatch):
    owner = quota_runtime
    calls = []
    def probe(*args):
        calls.append(args)
        cache(owner, NOW)
        return {"outcome": "quota_read", "reading": agent.cached_usage(owner[0] / ".claude", global_config=owner[0] / ".claude.json")}
    monkeypatch.setattr(mac_owner_agent, "_provider_quota", probe)
    got = mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW)
    assert got["models"]["fable"]["used_pct"] == 100 and len(calls) == 1
    mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW + 299)
    assert len(calls) == 1
    mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW + 301)
    assert len(calls) == 2
    state = mac_policy.read_json(owner[0], "maintenance.json")
    assert state["quota_api_attempted_at"] == NOW + 301
    assert "never-report-this" not in json.dumps(state)


@pytest.mark.parametrize("prior", [
    {"cleanup_confirmed": False}, {"cleanup_confirmed": True, "quota_api_attempted_at": NOW - 10},
])
def test_quota_poll_respects_metadata_budget_and_failed_native_cleanup(quota_runtime, monkeypatch, prior):
    owner = quota_runtime
    cache(owner, NOW - 1000)
    mac_policy.write_json(owner[0], "maintenance.json", {"version": 1, "bound_fp": owner[3], **prior})
    monkeypatch.setattr(mac_owner_agent, "_provider_quota", lambda *a: pytest.fail("no probe"))
    got = mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW)
    assert got["checked_at"] == NOW - 1000


def test_failed_native_quota_read_preserves_observation_age_and_cleanup_guard(quota_runtime, monkeypatch):
    owner = quota_runtime
    cache(owner, NOW - 1000)
    monkeypatch.setattr(mac_owner_agent, "_provider_quota", lambda *a:
                        {"outcome": "native_network_error"})
    mac_owner_agent.remember_quota_request(owner[0], {"requested_at": NOW - 1}, now=NOW)
    got = mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW)
    assert got["checked_at"] == NOW - 1000
    assert got["refresh"]["outcome"] == "native_network_error"
    assert mac_policy.read_json(owner[0], "maintenance.json").get("cleanup_confirmed") is not False


def test_explicit_request_uses_fresh_bound_cache_without_extra_native_probe(quota_runtime, monkeypatch):
    owner = quota_runtime
    cache(owner, NOW - 20)
    monkeypatch.setattr(mac_owner_agent, "_provider_quota", lambda *a: pytest.fail("no probe"))
    mac_owner_agent.remember_quota_request(owner[0], {"requested_at": NOW - 1}, now=NOW)
    got = mac_owner_agent.quota_report({}, owner[0], facts(owner), NOW)
    assert got["checked_at"] == NOW - 20
    assert got["refresh"] == {"requested_at": NOW - 1, "attempted_at": NOW, "outcome": "quota_read"}
    mac_owner_agent.remember_quota_request(owner[0], {"requested_at": NOW - 10}, now=NOW)
    assert mac_policy.read_json(owner[0], "maintenance.json")["quota_requested_at"] == NOW - 1


@pytest.mark.parametrize("incoming", [None, {}, {"requested_at": True}, {"requested_at": NOW + 61},
    {"requested_at": -1}, {"requested_at": float("nan")}, {"requested_at": 10 ** 400},
    {"requested_at": NOW, "command": "private-command"}])
def test_private_mac_accepts_only_bounded_exact_refresh_request(quota_runtime, incoming):
    owner = quota_runtime
    mac_owner_agent.remember_quota_request(owner[0], incoming, now=NOW)
    assert not (owner[1] / "maintenance.json").exists()


@pytest.mark.parametrize("change", [{"logged_in": False}, {"account_fp": "0" * 16},
                                    {"bound_fp": None}])
def test_private_mac_does_not_report_a_different_or_signed_out_account(quota_runtime, change):
    owner = quota_runtime
    cache(owner, NOW)
    assert mac_owner_agent.quota_report({}, owner[0], {**facts(owner), **change}, NOW) == {}


def test_private_mac_payload_reports_only_validated_quota_on_its_private_slot(quota_runtime, monkeypatch):
    owner = quota_runtime
    cache(owner, NOW)
    fake_metrics = SimpleNamespace(report=lambda *a: {}, validate_report=lambda value: value)
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: {
        "mac_policy": mac_policy, "agent": agent, "relay_metrics": fake_metrics}[name])
    monkeypatch.setattr(mac_owner_agent, "native_status", lambda *a: ({"logged_in": True}, "2.1.323"))
    monkeypatch.setattr(mac_owner_agent, "credential_facts", lambda *a: facts(owner))
    payload = mac_owner_agent.build_payload({"node_id": "mac-private"}, owner[0], now=NOW,
                                            maintenance=False)
    assert payload["quota"] == payload["slots"][0]["quota"]
    assert payload["quota"]["models"]["fable"]["used_pct"] == 100
    assert "synthetic-account" not in json.dumps(payload) and "never-report-this" not in json.dumps(payload)


def test_controller_private_quota_request_is_typed_and_owner_scoped(private):  # noqa: F811
    store, _ = private
    assert store.private_owner_quota("mac-private") is None
    store.set_private_owner_inference("mac-private", "operator", True, now=NOW)
    assert store.private_owner_quota("mac-private") == {}
    assert store.private_owner_quota("unknown") is None
    store._conn.execute("UPDATE slots SET quota_wanted_at=? WHERE id='mac-private'", (NOW,))
    store._conn.commit()
    assert store.private_owner_quota("mac-private") == {"requested_at": NOW}
    store._conn.execute("UPDATE slots SET held_by='other-admin' WHERE id='mac-private'")
    store._conn.commit()
    assert store.private_owner_quota("mac-private") is None
