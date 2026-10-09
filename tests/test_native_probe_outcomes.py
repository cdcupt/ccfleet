"""Native maintenance uses synthetic screens/files, never native/provider calls."""

import json
import multiprocessing
import os
import subprocess
import threading
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from ccfleet_agent import agent

from .test_native_renewal import NOW, maintain, rotate, runner, slot

slot = slot


def pane_runner(monkeypatch, panes, *, cleanup_timeout=False, launch=None):
    clock = [NOW]
    monkeypatch.setattr(agent.time, "time", lambda: clock[0])
    monkeypatch.setattr(agent.time, "sleep", lambda wait: clock.__setitem__(0, clock[0] + wait))
    monkeypatch.setattr(agent, "find_claude", lambda: "/synthetic/claude")
    captures = list(panes)
    calls = []

    def synthetic(argv, **kwargs):
        calls.append(argv)
        if "new-session" in argv and launch is not None:
            if launch == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="private error")
        if "kill-session" in argv and cleanup_timeout and len(calls) > 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        pane = captures.pop(0) if "capture-pane" in argv and captures else ""
        return subprocess.CompletedProcess(argv, 0, stdout=pane, stderr="")
    return synthetic, calls


@pytest.mark.parametrize("pane,outcome", [
    ("OAuth token revoked · Please run /login", "native_auth_rejected"),
    ("Login expired · Please run /login", "native_login_expired"),
    ("Authentication error · This may be a temporary network issue, please try again", "native_network_error"),
    ("Authentication error\nThis may be a temporary network issue, please try again", "native_network_error"),
    ("Could not refresh your login because another Claude Code process is refreshing it (or exited mid-refresh). Try /login if it persists", "native_probe_busy"),
    ("Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh. This is usually transient; retry in a minute, and if it persists close other Claude Code processes or sign in again", "native_probe_busy"),
    ("API Error: 429 Rate limited\nRetry-After: 90", "native_rate_limited"),
    ("API Error: Connection error", "native_network_error"),
    ("Token refresh is not due.", "native_refresh_not_due"),
    ("API Error: 401 OAuth token expired · Please run /login", "native_refresh_unconfirmed"),
    ("Failed to authenticate: OAuth session expired and could not be refreshed", "native_refresh_unconfirmed"),
    ("invalid_grant\ninvalid refresh token\nauth error expired /login", "native_refresh_unconfirmed"),
])
def test_native_markers_are_conservative_and_redacted(pane, outcome):
    observed = agent.native_probe_outcome(pane + "\nprivate-account@example.test sk-secret", NOW)
    assert observed["outcome"] == outcome
    assert "private-account" not in json.dumps(observed)
    assert "sk-secret" not in json.dumps(observed)


def test_temporary_marker_outranks_login_advice():
    pane = "Login expired · Please run /login\nAuthentication error · This may be a temporary network issue, please try again"
    assert agent.native_probe_outcome(pane, NOW)["outcome"] == "native_network_error"


def test_capability_is_observed_only_from_native_file_and_never_exposes_tokens(slot):
    home, _, _ = slot
    target = home / ".claude/.credentials.json"
    assert agent.credentials_summary(home / ".claude")["refresh_available"] is True
    target.write_text(json.dumps({"mcpOAuth": {"private": "secret"}}))
    assert agent.credentials_summary(home / ".claude")["refresh_available"] is False
    target.write_text("not json")
    assert "refresh_available" not in agent.credentials_summary(home / ".claude")
    target.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "private-access", "refreshToken": "private-refresh",
        "expiresAt": NOW * 1000, "refreshTokenExpiresAt": 10 ** 1000}}))
    facts = agent._slot_credential_facts(home / ".claude", slot[2], {})
    assert facts["refresh_expires_at"] is None
    assert "private-access" not in json.dumps(facts) and "private-refresh" not in json.dumps(facts)


@pytest.mark.parametrize("raw,expected", [
    ("0", 0), ("90", 90), ("99999999", 600), ("-1", None), ("NaN", None),
    ("1e1000", None), ("90 private-token", None),
    (format_datetime(datetime.fromtimestamp(NOW + 120, timezone.utc), usegmt=True), 120),
])
def test_retry_after_is_bounded_without_reflecting_raw_header(raw, expected):
    observed = agent.native_probe_outcome("API Error: 429\nRetry-After: " + raw, NOW)
    assert observed.get("retry_after_s") == expected
    assert set(observed) <= {"outcome", "retry_after_s"}


@pytest.mark.parametrize("launch,expected", [("timeout", "native_timeout"), ("nonzero", "native_launch_failed")])
def test_ambiguous_launch_keeps_start_unknown_and_cleans_up(slot, monkeypatch, launch, expected):
    synthetic, calls = pane_runner(monkeypatch, [], launch=launch)
    observed = {}
    assert agent.read_quota(synthetic, timeout=35, probe_result=observed) is None
    assert observed["outcome"] == expected
    assert "probe_started" not in observed
    assert calls[-1][3] == "kill-session"


def test_terminal_result_survives_cleanup_timeout(slot, monkeypatch):
    synthetic, calls = pane_runner(monkeypatch, ["OAuth token revoked · Please run /login"], cleanup_timeout=True)
    observed = {}
    assert agent.read_quota(synthetic, timeout=35, probe_result=observed) is None
    assert observed == {"probe_started": True, "outcome": "native_auth_rejected"}
    assert calls[-1][3] == "kill-session"
    assert not any("send-keys" in call for call in calls)


def test_quota_contention_does_not_count_launch_or_failure(slot, monkeypatch):
    probe, _ = agent.quota_probe_dir()
    descriptor = agent._native_probe_lock(probe)
    monkeypatch.setattr(agent, "find_claude", lambda: "/synthetic/claude")
    try:
        state, report, attempted = maintain(slot)
    finally:
        os.close(descriptor)
    assert not attempted and report["probe_started"] is False
    assert report["outcome"] == "native_probe_busy"
    assert "last_attempt_at" not in report
    assert state["native_renewal"]["failures"] == 0


def test_interrupted_prelaunch_reservation_retains_backoff_and_unknown_start(slot, monkeypatch):
    def interrupted(*args, **kwargs):
        assert kwargs["before_start"]()
        raise RuntimeError("synthetic process stopped before reporting launch")
    monkeypatch.setattr(agent, "read_quota", interrupted)
    with pytest.raises(RuntimeError):
        maintain(slot)
    saved = agent.read_state(slot[1])["native_renewal"]
    assert saved["next_attempt_at"] == NOW + 60
    assert "probe_started" not in saved and "last_attempt_at" not in saved
    assert saved["failures"] == 0


@pytest.mark.parametrize("field,value,reason", [
    ("refreshToken", "", "refresh_unavailable"),
    ("refreshTokenExpiresAt", (NOW - 1) * 1000, "refresh_expired"),
])
def test_unusable_refresh_never_drives_native_even_with_current_access(slot, monkeypatch, field, value, reason):
    home, _, _ = slot
    rotate(home, NOW + 7200)
    path = home / ".claude/.credentials.json"
    data = json.loads(path.read_text())
    data["claudeAiOauth"][field] = value
    path.write_text(json.dumps(data))
    monkeypatch.setattr(agent, "read_quota", lambda *a, **k: pytest.fail("futile native refresh"))
    _, report, attempted = maintain(slot)
    assert not attempted and report["reason"] == reason
    assert agent.credential_expiry(agent.credentials_summary(home / ".claude")) == NOW + 7200
    monkeypatch.setattr(agent, "quota_summary", lambda *a, **k: pytest.fail("quota bypassed terminal guard"))
    assert agent.slot_facts({"refresh_quota": True}, runner, NOW)["credentials"]["renewal"]["reason"] == reason


def test_native_rotation_before_renewal_lock_avoids_stale_missing_refresh(slot, monkeypatch):
    home, _, _ = slot
    stale = agent._slot_credential_facts(home / ".claude", slot[2], {})
    stale["refresh_available"] = False
    original = agent._native_probe_lock

    def rotate_at_lock(probe, name=".native-probe.lock"):
        descriptor = original(probe, name)
        if name == ".renewal.lock":
            rotate(home, NOW + 7200)
        return descriptor
    monkeypatch.setattr(agent, "_native_probe_lock", rotate_at_lock)
    monkeypatch.setattr(agent, "read_quota", lambda *a, **k: pytest.fail("stale observation launched"))
    _, report, attempted = agent.maintain_slot_credentials(slot[2], {"refresh_quota": True}, stale, runner, NOW, slot[1])
    assert not attempted and report["state"] == "current" and "reason" not in report


def test_rotation_after_quota_lock_suppresses_duplicate_probe(slot, monkeypatch):
    home, _, _ = slot
    original = agent._native_probe_lock

    def rotate_at_lock(probe, name=".native-probe.lock"):
        descriptor = original(probe, name)
        if name == ".native-probe.lock":
            rotate(home, NOW + 7200)
        return descriptor
    monkeypatch.setattr(agent, "_native_probe_lock", rotate_at_lock)
    monkeypatch.setattr(agent, "find_claude", lambda: "/synthetic/claude")
    _, report, attempted = maintain(slot)
    assert not attempted and report["state"] == "current"


@pytest.mark.parametrize("selected", [True, False])
def test_recovered_native_file_clears_stale_terminal_outcome(slot, monkeypatch, selected):
    home, path, state = slot
    agent.write_state(path, {**state, "native_renewal": {
        "state": "blocked", "reason": "native_auth_rejected", "outcome": "native_auth_rejected",
        "probe_started": True, "credential_stamp": agent._credentials_stamp(home / ".claude")}})
    rotate(home, NOW + 7200)
    monkeypatch.setattr(agent, "read_quota", lambda *a, **k: pytest.fail("already renewed"))
    state, report, attempted = maintain(slot, request={"refresh_quota": selected})
    assert not attempted and report["state"] == "current"
    assert "outcome" not in report and "reason" not in report
    assert "reason" not in state["native_renewal"]


def test_verified_rotation_outranks_stale_native_terminal_pane(slot, monkeypatch):
    home, _, _ = slot

    def observed(*args, **kwargs):
        assert kwargs["before_start"]()
        kwargs["probe_result"].update(outcome="native_auth_rejected", probe_started=True)
        rotate(home, NOW + 7200)
    monkeypatch.setattr(agent, "read_quota", observed)
    _, report, attempted = maintain(slot)
    assert attempted and report["state"] == "renewed" and report["outcome"] == "native_refreshed"
    assert "reason" not in report


@pytest.mark.parametrize("outcome", ["native_auth_rejected", "native_login_expired"])
def test_same_expiry_native_rotation_cannot_be_poisoned_by_stale_terminal_pane(slot, monkeypatch, outcome):
    home, _, _ = slot

    def observed(*args, **kwargs):
        assert kwargs["before_start"]()
        kwargs["probe_result"].update(outcome=outcome, probe_started=True)
        rotate(home, NOW + 120)
    monkeypatch.setattr(agent, "read_quota", observed)
    state, report, attempted = maintain(slot)
    assert attempted and report["state"] == "retrying"
    assert report["outcome"] == report["reason"] == "native_refresh_unconfirmed"
    assert "last_success_at" not in report
    assert state["native_renewal"]["reason"] == "native_refresh_unconfirmed"


def test_transient_retry_backoff_jitter_and_retry_after_are_bounded(monkeypatch):
    monkeypatch.setattr(agent.random, "uniform", lambda lo, hi: hi)
    assert agent.native_retry_delay("native_network_error", 0, NOW - 1, NOW) == 72
    assert agent.native_retry_delay("native_network_error", 2, NOW - 1, NOW) == 288
    assert agent.native_retry_delay("native_rate_limited", 0, NOW - 1, NOW, 500) == 500
    assert agent.native_retry_delay("native_timeout", 10, NOW - 1, NOW) == 600
    assert agent.native_retry_delay("native_refresh_not_due", 10, NOW + 30, NOW) == 60
    assert agent.native_retry_delay("native_timeout", 0, NOW - 1, NOW, 10 ** 1000) == 72


@pytest.mark.parametrize("bad", [[], {}, 10 ** 1000, True])
def test_corrupt_nested_state_and_refresh_metadata_are_not_reflected(slot, bad):
    home, path, state = slot
    state["native_renewal"] = {"outcome": bad, "reason": bad, "last_attempt_at": bad}
    agent.write_state(path, state)
    credentials = agent._slot_credential_facts(home / ".claude", state, {})
    credentials["refresh_expires_at"] = bad
    _, report, attempted = agent.maintain_slot_credentials(state, {"refresh_quota": False}, credentials, runner, NOW, path)
    assert not attempted and report["state"] == "needed"
    assert "outcome" not in report and "reason" not in report


def test_absurd_finite_times_cannot_postpone_renewal_or_claim_current(slot, monkeypatch):
    home, path, state = slot
    state["native_renewal"] = {"next_attempt_at": 1e300, "last_success_at": 1e300}
    agent.write_state(path, state)
    called = []

    def synthetic(*args, **kwargs):
        assert kwargs["before_start"]()
        called.append(True)
        kwargs["probe_result"].update(outcome="native_refresh_unconfirmed", probe_started=True)
    monkeypatch.setattr(agent, "read_quota", synthetic)
    _, report, attempted = maintain(slot)
    assert attempted and called == [True]
    assert report["next_attempt_at"] <= NOW + 600 and "last_success_at" not in report
    credentials = agent._slot_credential_facts(home / ".claude", state, {})
    credentials["expires_at"] = 1e300
    assert agent.credential_expiry(credentials) is None
    assert agent.finite_epoch(1e300) is None
    target = home / ".claude/.credentials.json"
    target.write_text(json.dumps({"claudeAiOauth": {
        "refreshToken": "synthetic-refresh", "expiresAt": 1e300,
        "refreshTokenExpiresAt": 1e300}}))
    facts = agent._slot_credential_facts(home / ".claude", state, {})
    assert facts["expires_at"] is None and facts["refresh_expires_at"] is None
    _, report, attempted = maintain(slot)
    assert not attempted and report["state"] == "blocked" and report["reason"] == "expiry_unknown"


def test_locked_current_report_preserves_newer_renewal_timestamps(slot, monkeypatch):
    home, path, state = slot
    stale = {**state, "native_renewal": {
        "last_success_at": NOW - 300, "last_attempt_at": NOW - 400}}
    original = agent._native_probe_lock

    def newer_at_lock(probe, name=".native-probe.lock"):
        descriptor = original(probe, name)
        if name == ".renewal.lock":
            rotate(home, NOW + 7200)
            agent.write_state(path, {**state, "native_renewal": {
                "last_success_at": NOW - 1, "last_attempt_at": NOW - 2}})
        return descriptor
    monkeypatch.setattr(agent, "_native_probe_lock", newer_at_lock)
    credentials = agent._slot_credential_facts(home / ".claude", stale, {})
    updated, report, attempted = agent.maintain_slot_credentials(stale, {"refresh_quota": True}, credentials, runner, NOW, path)
    assert not attempted and report["state"] == "current"
    assert updated["native_renewal"]["last_success_at"] == NOW - 1
    assert updated["native_renewal"]["last_attempt_at"] == NOW - 2


def _contending_process(probe, result):
    descriptor = agent._native_probe_lock(probe, ".renewal.lock")
    result.put(descriptor is None)
    if descriptor is not None:
        os.close(descriptor)


def test_renewal_lock_serializes_real_processes(slot):
    probe, _ = agent.quota_probe_dir()
    descriptor = agent._native_probe_lock(probe, ".renewal.lock")
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    child = context.Process(target=_contending_process, args=(probe, result))
    try:
        child.start()
        assert result.get(timeout=10) is True
        child.join(timeout=10)
        assert child.exitcode == 0
    finally:
        os.close(descriptor)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
        result.close()


def test_parallel_quota_call_never_drives_or_kills_active_probe(slot, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(agent, "find_claude", lambda: "/synthetic/claude")

    def synthetic(argv, **kwargs):
        calls.append(argv)
        if "new-session" in argv:
            entered.set()
            assert release.wait(timeout=10)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    first = threading.Thread(target=agent.read_quota, args=(synthetic,), kwargs={"timeout": 35})
    first.start()
    try:
        assert entered.wait(timeout=10)
        observation = {}
        assert agent.read_quota(lambda *a, **k: pytest.fail("overlapping native command"), probe_result=observation) is None
        assert observation == {"outcome": "native_probe_busy", "probe_started": False}
    finally:
        release.set()
        first.join(timeout=10)
    assert not first.is_alive()
    assert sum("new-session" in call for call in calls) == 1
