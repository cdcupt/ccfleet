"""Private Mac runtime boundaries use synthetic Keychain and native CLI fixtures."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from ccfleet_agent import local_relay, mac_credentials, mac_owner_agent, mac_policy, mac_slot_entry


@pytest.fixture
def owner(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    base = home / ".config" / "ccfleet" / "private-owner"
    base.mkdir(mode=0o700, parents=True)
    for path in (home / ".config", home / ".config/ccfleet", base):
        path.chmod(0o700)
    identity = SimpleNamespace(pw_uid=os.getuid(), pw_dir=str(home), pw_name="synthetic")
    monkeypatch.setattr(mac_policy, "_TRUST_ROOT", tmp_path)
    monkeypatch.setattr(mac_policy, "authenticated_owner",
                        lambda requested=None: identity if requested in (None, home)
                        else (_ for _ in ()).throw(mac_policy.MacPolicyError("wrong owner")))
    fingerprint = hashlib.sha256(b"synthetic-account").hexdigest()[:16]
    policy = {"version": 1, "enabled": True, "unix_user": "synthetic", "bound_fp": fingerprint}
    (base / "policy.json").write_text(json.dumps(policy))
    (base / "policy.json").chmod(0o600)
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "synthetic-account"},
        "projects": {"private-project": {"secret": "never-report-this"}}}))
    (home / ".claude.json").chmod(0o600)
    return home, base, identity, fingerprint


def _grant(owner, *, enabled=True, received_at=None, received_monotonic=None):
    home, _base, identity, _fp = owner
    now = time.time() if received_at is None else received_at
    mono = time.monotonic() if received_monotonic is None else received_monotonic
    mac_policy.write_json(home, "grants.json", {"version": 1, "received_at": now,
        "received_monotonic": mono, "access": {"version": 1, "enabled": enabled,
        "unix_user": identity.pw_name, "device_public_keys": [], "gateway_public_keys": []}})
    return now, mono


def test_mac_policy_requires_explicit_opt_in_and_fresh_typed_grant(owner):
    home, base, _identity, _fp = owner
    with pytest.raises(mac_policy.MacPolicyError):
        mac_policy.require_enabled(home)
    now, mono = _grant(owner)
    mac_policy.require_enabled(home, now=now, monotonic=mono)
    for wall, monotonic in ((now + 181, mono + 181), (now - 1, mono), (now, mono - 1)):
        with pytest.raises(mac_policy.MacPolicyError):
            mac_policy.require_enabled(home, now=wall, monotonic=monotonic)
    _grant(owner, enabled=False)
    with pytest.raises(mac_policy.MacPolicyError):
        mac_policy.require_enabled(home)
    _grant(owner)
    policy = json.loads((base / "policy.json").read_text())
    policy["enabled"] = False
    (base / "policy.json").write_text(json.dumps(policy))
    with pytest.raises(mac_policy.MacPolicyError):
        mac_policy.require_enabled(home)


@pytest.mark.parametrize("mutation", ["parent_writable", "base_public", "file_public",
                                        "symlink", "hardlink"])
def test_mac_policy_rejects_unsafe_path_or_private_file(owner, mutation):
    home, base, _identity, _fp = owner
    if mutation == "parent_writable":
        (home / ".config").chmod(0o777)
    elif mutation == "base_public":
        base.chmod(0o755)
    elif mutation == "file_public":
        (base / "policy.json").chmod(0o644)
    elif mutation == "hardlink":
        os.link(base / "policy.json", base / "another-link")
    else:
        original = base / "policy.json"
        original.rename(base / "actual.json")
        original.symlink_to("actual.json")
    with pytest.raises(mac_policy.MacPolicyError):
        mac_policy.policy(home)


def test_policy_no_symlink_ancestor_even_when_target_is_owned(owner):
    home, base, _identity, _fp = owner
    parent = home / ".config/ccfleet"
    parent.rename(home / "actual-config")
    parent.symlink_to(home / "actual-config")
    with pytest.raises(mac_policy.MacPolicyError):
        mac_policy.read_json(home, "policy.json")


def test_agent_can_write_only_grants_and_maintenance(owner):
    home, base, _identity, _fp = owner
    for name in ("agent.json", "policy.json", "../elsewhere"):
        with pytest.raises(mac_policy.MacPolicyError):
            mac_policy.write_json(home, name, {})
    mac_policy.write_json(home, "maintenance.json", {"version": 1})
    assert mac_policy.read_json(home, "maintenance.json") == {"version": 1}
    assert (base / "maintenance.json").stat().st_mode & 0o777 == 0o600


def test_keychain_selector_is_fixed_and_refresh_secret_never_returned(owner, monkeypatch):
    home, _base, _identity, _fp = owner
    monkeypatch.setattr(mac_credentials.sys, "platform", "darwin")
    called = []
    raw = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-access-secret",
        "refreshToken": "synthetic-refresh-never-return", "expiresAt": 123000,
        "subscriptionType": "max"}}).encode()
    def output(argv, selected_home, **kwargs):
        called.append((argv, selected_home, kwargs))
        return 0, raw
    monkeypatch.setattr(mac_credentials, "bounded_output", output)
    oauth = mac_credentials.read_oauth(home)
    assert oauth == {"accessToken": "synthetic-access-secret", "expiresAt": 123000,
                     "subscriptionType": "max"}
    assert called == [(["/usr/bin/security", "find-generic-password", "-s",
                        "Claude Code-credentials", "-a", "synthetic", "-w"], home,
                       {"timeout": mac_credentials.KEYCHAIN_TIMEOUT})]
    assert "synthetic-refresh" not in repr(oauth)


@pytest.mark.parametrize("code,raw", [(1, b"PRIVATE-KEYCHAIN-ERROR"), (0, b"PRIVATE-BAD-JSON"),
                                     (0, b'{"claudeAiOauth":{},"claudeAiOauth":{}}'),
                                     (0, b"x" * (mac_credentials.MAX_BYTES + 1))])
def test_keychain_failures_are_fixed_messages(owner, monkeypatch, code, raw):
    home = owner[0]
    monkeypatch.setattr(mac_credentials.sys, "platform", "darwin")
    monkeypatch.setattr(mac_credentials, "bounded_output", lambda *a, **kw: (code, raw))
    with pytest.raises(mac_credentials.CredentialError) as caught:
        mac_credentials.read_oauth(home)
    assert "PRIVATE" not in str(caught.value)


def test_keychain_access_is_not_available_on_linux(owner, monkeypatch):
    monkeypatch.setattr(mac_credentials.sys, "platform", "linux")
    monkeypatch.setattr(mac_credentials, "bounded_output", lambda *a, **kw: pytest.fail("ran reader"))
    with pytest.raises(mac_credentials.CredentialError):
        mac_credentials.read_oauth(owner[0])


@pytest.mark.parametrize("script,limit,timeout", [
    ("import sys;sys.stdout.write('x'*10000)", 128, 2),
    ("import time;time.sleep(5)", 128, 0.10),
])
def test_native_subprocess_has_real_size_and_time_bounds(owner, script, limit, timeout):
    started = time.monotonic()
    with pytest.raises(mac_credentials.CredentialError):
        mac_credentials.bounded_output([sys.executable, "-c", script], owner[0],
                                       limit=limit, timeout=timeout)
    assert time.monotonic() - started < 3


def test_native_subprocess_environment_drops_provider_overrides(owner, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "DO-NOT-FORWARD")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://wrong.invalid")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/other/account")
    env = mac_credentials.native_environment(owner[0])
    assert not any(key.startswith("ANTHROPIC_") for key in env)
    assert "CLAUDE_CONFIG_DIR" not in env and env["HOME"] == str(owner[0])


def test_private_mac_relay_uses_native_keychain_and_bound_profile(owner, monkeypatch):
    home, _base, _identity, _fp = owner
    monkeypatch.setattr(local_relay, "MAC_PRIVATE_OWNER", True)
    adapter = SimpleNamespace(read_oauth=lambda home: {
        "accessToken": "synthetic-token", "expiresAt": (time.time() + 3600) * 1000})
    monkeypatch.setattr(local_relay, "_mac_module", lambda name:
                        mac_policy if name == "mac_policy" else adapter)
    token, expiry = local_relay.credential(home, time.time())
    assert token == "synthetic-token" and expiry > time.time() + 3000
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "different"}}))
    with pytest.raises(local_relay.RelayError) as caught:
        local_relay.credential(home, time.time())
    assert caught.value.status == 409


def test_default_relay_does_not_read_keychain(owner, monkeypatch):
    monkeypatch.setattr(local_relay, "MAC_PRIVATE_OWNER", False)
    monkeypatch.setattr(local_relay, "_mac_module", lambda name: pytest.fail("Mac adapter activated"))
    with pytest.raises(local_relay.RelayError):
        local_relay.credential(owner[0], time.time())


@pytest.mark.parametrize("args,command,accepted", [
    ([], "ccfleet-inference-v1", True),
    (["--gateway-only"], "ccfleet-gateway-v1", True),
    ([], "ccfleet-gateway-v1", False),
    (["--gateway-only"], "ccfleet-inference-v1", False),
    ([], "ccfleet-session open ccfleet", False),
    ([], "ccfleet-project-v1", False),
    ([], "ccfleet-inference-v1\nid", False),
])
def test_mac_forced_entry_allows_only_exact_inference_protocols(owner, monkeypatch,
                                                              args, command, accepted):
    home = owner[0]
    calls = []
    fake_policy = SimpleNamespace(authenticated_owner=lambda: owner[2],
        open_directory=lambda path: os.open(home, os.O_RDONLY), require_enabled=lambda selected: None)
    relay = SimpleNamespace(main=lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(mac_slot_entry, "sibling", lambda name:
                        fake_policy if name == "mac_policy" else relay)
    monkeypatch.setattr(mac_slot_entry.sys, "platform", "darwin")
    monkeypatch.setattr(mac_slot_entry.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(mac_slot_entry.sys, "stdout", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    assert (mac_slot_entry.main(args) == 0) == accepted
    assert bool(calls) == accepted
    if accepted:
        assert relay.MAC_PRIVATE_OWNER is True


def test_mac_owner_payload_does_not_scan_history_or_report_identity_secrets(owner, monkeypatch):
    home, _base, identity, fingerprint = owner
    credential = {"store": "keychain", "present": True, "logged_in": True,
                  "account_fp": fingerprint, "bound_fp": fingerprint,
                  "expires_at": (time.time() + 3600) * 1000}
    monkeypatch.setattr(mac_owner_agent, "native_status", lambda cfg, selected:
                        ({"logged_in": True}, "2.1.0"))
    monkeypatch.setattr(mac_owner_agent, "credential_facts", lambda *a: dict(credential))
    monkeypatch.setattr(mac_owner_agent, "maintain_native", lambda *a: {"state": "current"})
    fake_metrics = SimpleNamespace(report=lambda *a: {}, validate_report=lambda value: value)
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name:
                        mac_policy if name == "mac_policy" else fake_metrics if name == "relay_metrics"
                        else pytest.fail("unexpected collector"))
    payload = mac_owner_agent.build_payload({"node_id": "mac-private"}, home)
    assert payload["mode"] == "private_owner"
    assert payload["slots"][0]["unix_user"] == identity.pw_name
    assert set(payload) == {"node_id", "ts", "agent_version", "mode", "claude", "credentials", "slots"}
    assert "never-report-this" not in json.dumps(payload)
    assert "usage" not in payload and "hostname" not in payload


def test_expiring_native_credential_uses_existing_probe_and_only_saves_facts(owner, monkeypatch):
    home, _base, _identity, fingerprint = owner
    now = time.time()
    facts = {"bound_fp": fingerprint, "expires_at": (now + 100) * 1000,
             "logged_in": True, "auth_method": "claude.ai", "api_provider": "firstParty"}
    calls = []
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: mac_policy)
    monkeypatch.setattr(mac_owner_agent, "_native_renewal_probe", lambda *a:
                        calls.append(a) or {"outcome": "native_refresh_unconfirmed", "probe_started": True,
                                           "cleanup_confirmed": True})
    monkeypatch.setattr(mac_owner_agent, "credential_facts", lambda *a:
                        {**facts, "expires_at": (now + 3600) * 1000})
    report = mac_owner_agent.maintain_native({}, home, facts, now)
    assert report["state"] == "renewed" and len(calls) == 1
    saved = mac_policy.read_json(home, "maintenance.json")
    assert set(saved) == {"version", "bound_fp", "last_attempt_at", "next_attempt_at",
                          "state", "checked_at", "outcome", "cleanup_confirmed"}
    mac_owner_agent.maintain_native({}, home, facts, now + 1)
    assert len(calls) == 1


def test_mac_agent_ignores_generic_desired_and_only_converges_typed_grant(owner, monkeypatch, capsys):
    home, base, identity, _fp = owner
    token = "synthetic-node-secret-never-log"
    cfg = {"version": 1, "url": "https://fleet.example", "node_id": "private-mac",
           "node_token": token, "native_claude": str(home / "native-claude")}
    (base / "agent.json").write_text(json.dumps(cfg))
    (base / "agent.json").chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(mac_owner_agent.sys, "platform", "darwin")
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: mac_policy)
    monkeypatch.setattr(mac_owner_agent, "build_payload", lambda config, selected:
                        {"mode": "private_owner", "node_id": config["node_id"]})
    grant = {"version": 1, "unix_user": identity.pw_name, "enabled": True,
             "device_public_keys": [], "gateway_public_keys": []}
    calls = []
    monkeypatch.setattr(mac_owner_agent, "post_heartbeat", lambda *a: (200, {
        "desired": {"login": {"secret": "PRIVATE"}, "wipe": True,
                    "commands": ["must never execute"], "claude_version": "999.0.0"},
        "pinned_version": "999.0.0", "private_owner_access": grant}))
    monkeypatch.setattr(mac_owner_agent, "converge_access", lambda selected, value:
                        calls.append((selected, value)))
    assert mac_owner_agent.main([]) == 0
    assert calls == [(home, grant)]
    output = capsys.readouterr()
    assert token not in output.err and "PRIVATE" not in output.err
    assert output.out == ""


@pytest.mark.parametrize("bad_home,bad_config", [(True, False), (False, True)])
def test_mac_agent_refuses_ambient_account_namespace(owner, monkeypatch, bad_home, bad_config):
    home = owner[0]
    monkeypatch.setattr(mac_owner_agent.sys, "platform", "darwin")
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: mac_policy)
    monkeypatch.setenv("HOME", str(home / "other" if bad_home else home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/other/account" if bad_config else "")
    monkeypatch.setattr(mac_owner_agent, "post_heartbeat", lambda *a: pytest.fail("sent heartbeat"))
    assert mac_owner_agent.main([]) == 2


def test_mac_access_convergence_fails_closed_for_absent_or_malformed_grants(owner, monkeypatch):
    home, _base, identity, _fp = owner
    converged = []
    def validate(value):
        if value != "valid":
            raise ValueError("invalid")
        return {"version": 1, "unix_user": identity.pw_name, "enabled": True,
                "device_public_keys": [], "gateway_public_keys": []}
    keys = SimpleNamespace(validate_snapshot=validate,
                           reconcile=lambda value, **kwargs: converged.append(value) or {})
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name:
                        mac_policy if name == "mac_policy" else keys)
    for value in (None, {"commands": ["not supported"]}):
        mac_owner_agent.converge_access(home, value)
        assert converged[-1]["enabled"] is False
        with pytest.raises(mac_policy.MacPolicyError):
            mac_policy.require_enabled(home)
    mac_owner_agent.converge_access(home, "valid")
    mac_policy.require_enabled(home)


def test_mac_renewal_uses_guarded_native_safe_mode_probe_without_model_prompt(owner, monkeypatch):
    home, base, _identity, fingerprint = owner
    executable = home / "native-cli"
    executable.write_text("synthetic executable, never run")
    executable.chmod(0o700)
    calls = []
    def probe(path, selected_home, directory, **kwargs):
        assert kwargs["before_start"]() is True
        calls.append((path, selected_home, directory, kwargs))
        return {"outcome": "native_refresh_unconfirmed", "probe_started": True,
                "cleanup_confirmed": True, "usage_seen": True}
    modules = {"mac_policy": mac_policy,
               "local_relay": SimpleNamespace(bound_account=lambda selected: fingerprint),
               "mac_native_probe": SimpleNamespace(run=probe)}
    monkeypatch.setattr(mac_owner_agent, "sibling", modules.__getitem__)
    result = mac_owner_agent._native_renewal_probe(
        {"native_claude": str(executable)}, home, fingerprint)
    assert result["probe_started"] and result["cleanup_confirmed"]
    assert (base / "native-probe").stat().st_mode & 0o777 == 0o700
    assert len(calls) == 1 and calls[0][3]["timeout"] == 45
    assert calls[0][1] == home
    assert "prompt" not in calls[0][3]


def test_private_owner_identity_is_pwd_bound_and_never_root(monkeypatch):
    monkeypatch.setattr(mac_policy.os, "getuid", lambda: 1234)
    identity = SimpleNamespace(pw_uid=1234, pw_dir="/Users/synthetic", pw_name="synthetic")
    monkeypatch.setattr(mac_policy.pwd, "getpwuid", lambda uid: identity)
    assert mac_policy.authenticated_owner().pw_name == "synthetic"
    with pytest.raises(mac_policy.MacPolicyError):
        mac_policy.authenticated_owner(mac_policy.Path("/Users/another"))
    monkeypatch.setattr(mac_policy.os, "getuid", lambda: 0)
    with pytest.raises(mac_policy.MacPolicyError):
        mac_policy.authenticated_owner()


@pytest.mark.parametrize("platform,args,tty", [("linux", [], False),
                                               ("darwin", ["--other"], False),
                                               ("darwin", [], True)])
def test_private_forced_entry_rejects_platform_arguments_and_terminal(monkeypatch,
                                                                    platform, args, tty):
    monkeypatch.setattr(mac_slot_entry.sys, "platform", platform)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "ccfleet-inference-v1")
    monkeypatch.setattr(mac_slot_entry.sys, "stdin", SimpleNamespace(isatty=lambda: tty))
    monkeypatch.setattr(mac_slot_entry.sys, "stdout", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(mac_slot_entry, "sibling", lambda name: pytest.fail("opened runtime"))
    assert mac_slot_entry.main(args) == 2


@pytest.mark.parametrize("url", ["http://fleet.example", "https://user:password@fleet.example",
                                  "https://fleet.example/?token=secret", "https://fleet.example/#x",
                                  "https://fleet.example/arbitrary/path"])
def test_private_agent_accepts_only_plain_verified_https_controller_origin(owner, monkeypatch, url):
    home, base, _identity, _fp = owner
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: mac_policy)
    (base / "agent.json").write_text(json.dumps({"version": 1, "url": url,
        "node_id": "private-mac", "node_token": "synthetic-private-node-token",
        "native_claude": "/usr/local/bin/claude"}))
    (base / "agent.json").chmod(0o600)
    with pytest.raises(ValueError):
        mac_owner_agent.load_config(home)


def test_nonexpiring_credentials_do_not_run_native_probe(owner, monkeypatch):
    home, _base, _identity, fingerprint = owner
    now = time.time()
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: mac_policy)
    monkeypatch.setattr(mac_owner_agent, "_native_renewal_probe", lambda *a:
                        pytest.fail("unnecessary native probe"))
    result = mac_owner_agent.maintain_native({}, home,
        {"bound_fp": fingerprint, "expires_at": (now + 3600) * 1000}, now)
    assert result == {"state": "current", "checked_at": now}


def test_advanced_expiry_does_not_hide_unconfirmed_native_cleanup(owner, monkeypatch):
    home, _base, _identity, fingerprint = owner
    now = time.time()
    facts = {"bound_fp": fingerprint, "expires_at": (now + 10) * 1000,
             "logged_in": True, "auth_method": "claude.ai", "api_provider": "firstParty"}
    calls = []
    monkeypatch.setattr(mac_owner_agent, "sibling", lambda name: mac_policy)
    monkeypatch.setattr(mac_owner_agent, "_native_renewal_probe", lambda *a:
        calls.append(a) or {"outcome": "native_timeout", "probe_started": True,
                           "cleanup_confirmed": False})
    current = {**facts, "expires_at": (now + 3600) * 1000}
    monkeypatch.setattr(mac_owner_agent, "credential_facts", lambda *a: current)
    report = mac_owner_agent.maintain_native({}, home, facts, now)
    assert report["state"] == "blocked" and report["reason"] == "native_probe_busy"
    assert mac_policy.read_json(home, "maintenance.json")["cleanup_confirmed"] is False
    report = mac_owner_agent.maintain_native({}, home, current, now + 600)
    assert report["state"] == "blocked" and len(calls) == 1
