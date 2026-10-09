"""Expiry maintenance drives native Claude only, with synthetic credentials."""

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from ccfleet_agent import agent

NOW = 1_800_000_000.0


@pytest.fixture
def slot(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(agent.random, "uniform", lambda *args: 0)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "synthetic-native-owner"}}))
    rotate(tmp_path, NOW + 120)
    state_path = tmp_path / ".config/ccfleet/slot-state.json"
    state = {"bound_fp": agent.account_fingerprint(tmp_path / ".claude.json"),
             "quota": {"ts": NOW, "week": {"used_pct": 12}}}
    agent.write_state(state_path, state)
    monkeypatch.setattr(agent, "auth_status", lambda *a: {"logged_in": True})
    monkeypatch.setattr(agent, "claude_info", lambda *a: {"version": "2.1.284"})
    monkeypatch.setattr(agent, "remote_control_state", lambda *a: {"state": "inactive"})
    monkeypatch.setattr(agent, "sync_slot_terminal_unit", lambda *a: None)
    monkeypatch.setattr(agent, "retire_slot_remote_control", lambda *a: None)
    return tmp_path, state_path, state


def install_native(monkeypatch, effect):
    """A synthetic native writer honours the real prelaunch observation contract."""
    def probe(*args, **kwargs):
        before = kwargs.pop("before_start", None)
        report = kwargs.pop("probe_result", None)
        if before is not None and not before():
            if report is not None:
                report.update(outcome="native_probe_busy", probe_started=False)
            return None
        if report is not None:
            report.update(outcome="native_refresh_unconfirmed", probe_started=True)
        return effect(*args, **kwargs)
    monkeypatch.setattr(agent, "read_quota", probe)


def rotate(home, expiry):
    target = home / ".claude/.credentials.json"
    temporary = target.with_suffix(".new")
    temporary.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "synthetic-private-token", "refreshToken": "synthetic-private-refresh",
        "expiresAt": expiry * 1000}}))
    temporary.replace(target)


def runner(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def maintain(slot, now=NOW, request=None):
    home, path, _ = slot
    state = agent.read_state(path)
    credentials = agent._slot_credential_facts(home / ".claude", state, {"logged_in": True})
    return agent.maintain_slot_credentials(
        state, {"refresh_quota": True} if request is None else request,
        credentials, runner, now, path)


def test_expiring_local_only_slot_forces_native_probe_despite_fresh_quota(slot, monkeypatch):
    home, path, _ = slot
    calls = []

    def native(*args, **kwargs):
        calls.append(kwargs)
        rotate(home, NOW + 7200)
        return {"week": {"used_pct": 99}}

    install_native(monkeypatch, native)
    facts = agent.slot_facts({"refresh_quota": True}, runner, NOW)
    assert calls == [{"timeout": agent.NATIVE_RENEWAL_TIMEOUT_S}]
    assert facts["credentials"]["expires_at"] == (NOW + 7200) * 1000
    assert facts["credentials"]["renewal"]["state"] == "renewed"
    assert facts["credentials"]["renewal"]["last_success_at"] == NOW
    assert agent.read_state(path)["bound_fp"] == slot[2]["bound_fp"]
    assert "synthetic-private" not in json.dumps(facts)
    assert "synthetic-native-owner" not in json.dumps(facts)


def test_successful_quota_screen_is_not_proof_of_renewal_and_backoff_is_durable(slot, monkeypatch):
    calls = []
    install_native(monkeypatch, lambda *a, **k: calls.append(k) or {"week": {}})
    original = (slot[0] / ".claude/.credentials.json").read_bytes()
    state, report, attempted = maintain(slot)
    assert attempted and report["state"] == "retrying"
    assert report["next_attempt_at"] == NOW + 60
    assert "last_success_at" not in report
    assert maintain(slot, NOW + 10)[2] is False
    assert len(calls) == 1
    _, report, attempted = maintain(slot, NOW + 60)
    assert attempted and report["next_attempt_at"] == NOW + 120
    assert (slot[0] / ".claude/.credentials.json").read_bytes() == original


def test_retry_backoff_caps_and_current_expiry_does_not_launch_native(slot, monkeypatch):
    install_native(monkeypatch, lambda *a, **k: None)
    now = NOW
    for _ in range(8):
        _, report, _ = maintain(slot, now)
        assert report["next_attempt_at"] <= now + 600
        now = report["next_attempt_at"]
    rotate(slot[0], now + 7200)
    install_native(monkeypatch, lambda *a, **k: pytest.fail("unneeded renewal"))
    assert maintain(slot, now)[1]["state"] == "current"


def test_unconfirmed_native_probes_converge_on_a_narrow_refresh_window(slot, monkeypatch):
    expiry = NOW + 9 * 60
    rotate(slot[0], expiry)
    attempts = []
    install_native(monkeypatch, lambda *a, **k: attempts.append(True))
    now = NOW
    deadlines = []
    while now <= expiry:
        _, report, attempted = maintain(slot, now)
        assert attempted
        next_at = report["next_attempt_at"]
        assert next_at >= now + 60, "no tight retry loops"
        deadlines.append(next_at)
        now = next_at
    assert len(attempts) >= 5, "early refusal still converges toward native refresh eligibility"
    assert any(expiry - 60 <= at <= expiry for at in deadlines)
    assert deadlines[-1] <= expiry + 60, "backoff must not jump minutes beyond expiry"
    _, report, attempted = maintain(slot, now)
    assert attempted and report["next_attempt_at"] == now + 60


@pytest.mark.parametrize("change, reason", [
    ({"bound_fp": None}, "account_unbound"),
    ({"bound_fp": "another-owner"}, "account_transition"),
    ({"account_restart": "owed"}, "account_transition"),
    ({"login": {"requested_at": NOW}}, "sign_in_pending"),
])
def test_binding_and_sign_in_guards_never_drive_native(slot, monkeypatch, change, reason):
    _, path, state = slot
    agent.write_state(path, {**state, **change})
    install_native(monkeypatch, lambda *a, **k: pytest.fail("guard bypassed"))
    _, report, attempted = maintain(slot)
    assert not attempted and report["state"] == "blocked" and report["reason"] == reason


def test_slot_not_chosen_for_maintenance_waits_even_if_expired(slot, monkeypatch):
    rotate(slot[0], NOW - 100)
    install_native(monkeypatch, lambda *a, **k: pytest.fail("not selected"))
    _, report, attempted = maintain(slot, request={"refresh_quota": False})
    assert not attempted and report["state"] == "needed"


def test_native_can_renew_expired_credential_without_auth_status_claim(slot, monkeypatch):
    home, _, _ = slot
    rotate(home, NOW - 100)
    monkeypatch.setattr(agent, "auth_status", lambda *a: {"logged_in": False})
    install_native(monkeypatch, lambda *a, **k: rotate(home, NOW + 7200))
    facts = agent.slot_facts({"refresh_quota": True}, runner, NOW)
    assert facts["credentials"]["renewal"]["state"] == "renewed"
    assert facts["credentials"]["expires_at"] == (NOW + 7200) * 1000


def test_account_change_during_probe_is_not_success_or_overwritten(slot, monkeypatch):
    home, path, state = slot

    def native(*a, **k):
        rotate(home, NOW + 7200)
        agent.write_state(path, {**state, "bound_fp": "new-account", "account_restart": "owed"})

    install_native(monkeypatch, native)
    state, report, attempted = maintain(slot)
    assert attempted and report["state"] == "blocked"
    assert report["reason"] == "account_transition"
    assert agent.read_state(path)["bound_fp"] == state["bound_fp"] == "new-account"
    assert state["account_restart"] == "owed"


def test_account_transition_before_locked_reread_survives_callers_final_write(slot, monkeypatch):
    _, path, state = slot
    original_lock = agent._native_probe_lock

    def lock_then_transition(probe, name=".native-probe.lock"):
        held = original_lock(probe, name)
        if name == ".renewal.lock":
            agent.write_state(path, {**state, "bound_fp": "next-owner", "account_restart": "owed"})
        return held

    monkeypatch.setattr(agent, "_native_probe_lock", lock_then_transition)
    install_native(monkeypatch, lambda *a, **k: pytest.fail("transition was ignored"))
    facts = agent.slot_facts({"refresh_quota": True}, runner, NOW)
    saved = agent.read_state(path)
    assert saved["bound_fp"] == "next-owner" and saved["account_restart"] == "owed"
    assert facts["credentials"]["bound_fp"] == "next-owner"
    assert facts["credentials"]["renewal"]["reason"] == "account_transition"


def test_newer_backoff_discovered_under_lock_is_not_overwritten(slot, monkeypatch):
    _, path, state = slot
    original_lock = agent._native_probe_lock

    def lock_after_another_attempt(probe, name=".native-probe.lock"):
        held = original_lock(probe, name)
        if name == ".renewal.lock":
            agent.write_state(path, {**state, "native_renewal": {
                "last_attempt_at": NOW, "next_attempt_at": NOW + 60, "failures": 3}})
        return held

    monkeypatch.setattr(agent, "_native_probe_lock", lock_after_another_attempt)
    install_native(monkeypatch, lambda *a, **k: pytest.fail("overlapping attempt"))
    agent.slot_facts({"refresh_quota": True}, runner, NOW)
    assert agent.read_state(path)["native_renewal"]["failures"] == 3


def test_maintenance_lock_prevents_overlapping_native_probes(slot, monkeypatch):
    probe, _ = agent.quota_probe_dir()
    lock = agent._native_probe_lock(probe, ".renewal.lock")
    assert lock is not None
    install_native(monkeypatch, lambda *a, **k: pytest.fail("overlapping probe"))
    try:
        _, report, attempted = maintain(slot)
        assert not attempted and report["reason"] == "native_probe_busy"
    finally:
        os.close(lock)


def test_quota_lock_never_stops_another_active_probe(slot, monkeypatch):
    probe, _ = agent.quota_probe_dir()
    lock = agent._native_probe_lock(probe)
    monkeypatch.setattr(agent, "find_claude", lambda: "/test/claude")
    try:
        assert agent.read_quota(lambda *a, **k: pytest.fail("touched another tmux")) is None
    finally:
        os.close(lock)


def test_probe_lock_rejects_symlinks_and_does_not_modify_target(slot):
    probe, _ = agent.quota_probe_dir()
    target = slot[0] / "keep"
    target.write_text("unchanged")
    Path(probe, ".native-probe.lock").symlink_to(target)
    assert agent._native_probe_lock(probe) is None
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("expiry", [None, True, float("inf"), float("nan"), 10 ** 1000, -1])
def test_invalid_expiry_never_schedules_native(slot, monkeypatch, expiry):
    rotate(slot[0], 1)
    path = slot[0] / ".claude/.credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "refreshToken": "synthetic-private-refresh", "expiresAt": expiry}}))
    install_native(monkeypatch, lambda *a, **k: pytest.fail("invalid expiry"))
    _, report, attempted = maintain(slot)
    assert not attempted and report["reason"] == "expiry_unknown"


def test_failed_state_write_does_not_launch_native(slot, monkeypatch):
    monkeypatch.setattr(agent, "write_state", lambda *a, **k: False)
    install_native(monkeypatch, lambda *a, **k: pytest.fail("no durable backoff"))
    assert maintain(slot)[2] is False


def test_completed_sign_in_clears_prior_renewal_backoff():
    assert "native_renewal" not in agent._moved_on({
        "bound_fp": "kept", "native_renewal": {"next_attempt_at": NOW + 600}})


def test_slot_entry_serializes_login_adoption_with_native_maintenance(slot, monkeypatch):
    monkeypatch.setattr(agent.os, "geteuid", lambda: 1001)
    probe, _ = agent.quota_probe_dir()
    lock = agent._native_probe_lock(probe, ".slot-facts.lock")
    monkeypatch.setattr(agent, "slot_facts", lambda *a, **k: pytest.fail("racing slot run"))
    try:
        output = io.StringIO()
        assert agent.slot_facts_main(io.StringIO("{}"), output, runner) == 2
        assert output.getvalue() == ""
    finally:
        os.close(lock)


def test_short_native_probe_deadline_bounds_every_tmux_call_and_cleans_up(slot, monkeypatch):
    monkeypatch.setattr(agent, "find_claude", lambda: "/test/claude")
    current = [NOW]
    monkeypatch.setattr(agent.time, "time", lambda: current[0])
    monkeypatch.setattr(agent.time, "sleep", lambda wait: current.__setitem__(0, current[0] + wait))
    calls = []

    def bounded(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        current[0] += kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert agent.read_quota(bounded, timeout=35) is None
    assert current[0] <= NOW + 40
    assert calls[-1][0][-3:] == ["kill-session", "-t", agent.QUOTA_SESSION]
    assert calls[-1][1] == 5
    assert all(wait <= 15 for _, wait in calls)


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_ambiguous_native_probe_launch_always_cleans_up(slot, monkeypatch, failure):
    monkeypatch.setattr(agent, "find_claude", lambda: "/test/claude")
    calls = []

    def ambiguous(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        if "new-session" in argv:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert agent.read_quota(ambiguous, timeout=35) is None
    assert [argv[5] for argv, _ in calls] == ["kill-session", "new-session", "kill-session"]
    assert calls[-1][1] == 5


def test_normal_quota_probe_reports_rotated_expiry_same_heartbeat(slot, monkeypatch):
    home, path, state = slot
    rotate(home, NOW + 7200)
    agent.write_state(path, {**state, "quota": {"ts": NOW - 7200}})
    install_native(monkeypatch, lambda *a, **k: rotate(home, NOW + 14400))
    facts = agent.slot_facts({"refresh_quota": True}, runner, NOW)
    assert facts["credentials"]["expires_at"] == (NOW + 14400) * 1000
