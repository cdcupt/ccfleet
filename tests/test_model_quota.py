"""The native Fable cap survives collection, attribution and private transport."""
from __future__ import annotations

import hashlib
import json

import pytest

from ccfleet_agent import agent
from ccfleetd import client_status, heartbeat
from tests.test_quota_cache import NOW, RESET_7D, UUID, at, kept, probe, saved  # noqa: F401


def scoped(name="Fable", percent=100, **changes):
    return {"kind": "weekly_scoped", "group": "weekly", "percent": percent,
            "resets_at": RESET_7D, "scope": {"model": {"id": None, "display_name": name},
                                            "surface": None}, **changes}


def save_windows(config, windows):
    for path in (config / ".claude.json", config.parent / ".claude.json"):
        data = json.loads(path.read_text())
        data[agent.QUOTA_CACHE_KEY]["utilization"].update(windows)
        path.write_text(json.dumps(data))


def test_current_native_array_preserves_fable_cap_separate_from_overall(tmp_path):
    config = saved(tmp_path, five=None, seven=51)
    save_windows(config, {"limits": [scoped(), scoped("Sonnet", 23),
        {"kind": "weekly_all", "group": "weekly", "percent": 51,
         "resets_at": RESET_7D, "scope": None}]})
    native = agent.cached_usage(config)
    assert native == {"week": {"used_pct": 51, "resets_at": at(RESET_7D)},
                      "models": {"fable": {"used_pct": 100, "resets_at": at(RESET_7D)},
                                 "sonnet": {"used_pct": 23, "resets_at": at(RESET_7D)}},
                      "checked_at": NOW - 60}
    payload = {"node_id": "synthetic", "ts": NOW, "quota": native,
               "mode": "machine", "slots": [{"unix_user": "slot01", "quota": native}]}
    validated = heartbeat.validate_heartbeat(payload, "synthetic")
    quota = validated["slots"][0]["quota"]
    assert quota == validated["quota"]
    observed = client_status.quota_observation(quota, {"claimed_at": NOW - 120}, NOW)
    assert observed == native
    assert UUID not in json.dumps(validated) and "display_name" not in json.dumps(validated)


def test_current_array_can_supply_overall_without_old_seven_day_field(tmp_path):
    config = saved(tmp_path, five=None, seven=None)
    save_windows(config, {"limits": [scoped(), {"kind": "weekly_all", "group": "weekly",
        "percent": 51, "resets_at": RESET_7D, "scope": None}]})
    native = agent.cached_usage(config)
    assert native["week"]["used_pct"] == 51 and native["models"]["fable"]["used_pct"] == 100


def test_model_cap_alone_is_still_a_reading(tmp_path):
    config = saved(tmp_path, five=None, seven=None)
    save_windows(config, {"limits": [scoped()]})
    assert set(agent.cached_usage(config)) == {"models", "checked_at"}


def test_legacy_model_windows_and_current_authoritative_array(tmp_path):
    config = saved(tmp_path)
    save_windows(config, {"seven_day_overage_included": {"utilization": 12, "resets_at": RESET_7D},
        "seven_day_opus": {"utilization": 45, "resets_at": RESET_7D}, "limits": [scoped()]})
    models = agent.cached_usage(config)["models"]
    assert models["fable"]["used_pct"] == 100 and models["opus"]["used_pct"] == 45


@pytest.mark.parametrize("entry", [
    scoped("Secret Account"), scoped("Fable 5.2"), scoped(percent=True),
    scoped(percent="100"), scoped(percent=101), scoped(percent=float("nan")),
    scoped(group="daily"), scoped(kind="custom"),
    scoped(scope={"model": {"display_name": "Fable"}, "surface": "Claude Code"}),
    scoped(scope={"model": {"display_name": "Fable", "id": "claude-fable-5-2"}}),
    scoped(scope={"model": {"display_name": "Fable"}, "accountUuid": UUID}),
])
def test_unrecognized_or_narrow_scopes_never_become_family_quotas(tmp_path, entry):
    config = saved(tmp_path)
    save_windows(config, {"limits": [entry]})
    assert "models" not in agent.cached_usage(config)


def test_duplicate_family_limits_keep_strictest_without_adding(tmp_path):
    config = saved(tmp_path)
    save_windows(config, {"limits": [scoped(percent=50), scoped(percent=100), scoped(percent=20)]})
    assert agent.cached_usage(config)["models"]["fable"]["used_pct"] == 100
    save_windows(config, {"limits": [scoped(), scoped(resets_at="unknown")]})
    assert agent.cached_usage(config)["models"]["fable"] == {"used_pct": 100}


def test_unbounded_array_is_ignored_and_invalid_resets_stay_unknown(tmp_path):
    config = saved(tmp_path)
    save_windows(config, {"limits": [scoped()] * 65})
    assert "models" not in agent.cached_usage(config)
    save_windows(config, {"limits": [scoped(resets_at="private-location")]})
    assert agent.cached_usage(config)["models"]["fable"] == {"used_pct": 100}


@pytest.mark.parametrize("stamp", [0, -1, float("inf"), float("nan"), 10 ** 400])
def test_invalid_cache_instants_never_escape(tmp_path, stamp):
    config = saved(tmp_path)
    path = config.parent / ".claude.json"
    data = json.loads(path.read_text())
    data[agent.QUOTA_CACHE_KEY]["fetchedAtMs"] = stamp
    path.write_text(json.dumps(data))
    assert agent.cached_usage(config) is None


def test_private_owner_cache_must_match_current_bound_fingerprint(tmp_path):
    config = saved(tmp_path)
    save_windows(config, {"limits": [scoped()]})
    assert agent.cached_usage(config, account_fp="0" * 16) is None
    fingerprint = hashlib.sha256(UUID.encode()).hexdigest()[:16]
    assert agent.cached_usage(config, account_fp=fingerprint)["models"]["fable"]["used_pct"] == 100


def test_new_native_model_cache_beats_partial_screen_and_keeps_original_stamp(tmp_path, probe):  # noqa: F811
    calls, found = probe
    config = saved(tmp_path, fetched=NOW - 500)
    def write():
        saved(tmp_path, fetched=NOW - 10)
        save_windows(config, {"limits": [scoped()]})
    found["writes"] = write
    report, stored = agent.quota_summary(kept(NOW - 500), now=NOW, config_dir=config)
    assert calls == [NOW]
    assert report["models"]["fable"]["used_pct"] == 100
    assert report["checked_at"] == stored["checked_at"] == NOW - 10


def test_tui_model_limits_do_not_contaminate_overall_or_other_windows():
    pane = ("Current week (all models)\n51% used\nResets Wednesday\n"
            "Fable limit\n100% used\nResets Wednesday\n"
            "Current week (Sonnet)\n12% used\nResets Friday\n")
    got = agent.parse_quota(pane)
    assert got["week"]["used_pct"] == 51
    assert got["models"] == {"fable": {"used_pct": 100, "resets": "Wednesday"},
                             "sonnet": {"used_pct": 12, "resets": "Friday"}}
    assert agent.parse_quota("Current session\nCurrent week (Unrecognized)\n99% used") == {}


def test_heartbeat_and_client_export_only_finite_numeric_model_fields():
    quota = {"checked_at": NOW, "models": {
        "fable": {"used_pct": 100, "resets_at": NOW + 60, "accountUuid": "SECRET"},
        "secret-model": {"used_pct": 90}, "opus": {"used_pct": float("inf")},
        "sonnet": {"used_pct": 0, "resets_at": -1}}}
    got = heartbeat._quota(quota)
    assert set(got["models"]) == {"fable", "sonnet"}
    assert "SECRET" not in json.dumps(got) and "secret-model" not in json.dumps(got)
    assert client_status.quota_observation(got, {}, NOW) == {"checked_at": NOW, "models": {
        "fable": {"used_pct": 100, "resets_at": NOW + 60}, "sonnet": {"used_pct": 0}}}
    assert client_status.quota_observation(got, {"account_switched_at": NOW + 1}, NOW) == {}
