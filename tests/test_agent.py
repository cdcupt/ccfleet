import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from ccfleet_agent import agent


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_runner(stdout="", returncode=0):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
    return run


def test_load_env_file(tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text('# comment\nexport CCFLEET_URL="https://f.example/"\nCCFLEET_NODE_ID=node-a\n'
                        "CCFLEET_NODE_TOKEN='abc'\nBROKEN LINE\n")
    values = agent.load_env_file(env_file)
    assert values == {"CCFLEET_URL": "https://f.example/", "CCFLEET_NODE_ID": "node-a",
                      "CCFLEET_NODE_TOKEN": "abc"}
    assert agent.load_env_file(tmp_path / "missing.env") == {}


def test_agent_config_validation():
    good = agent.AgentConfig.from_env({"CCFLEET_URL": "https://f.example/", "CCFLEET_NODE_ID": "n",
                                       "CCFLEET_NODE_TOKEN": "t", "CCFLEET_EGRESS_TARGETS": "a, b"})
    assert good.url == "https://f.example" and good.egress_targets == ("a", "b")
    for env in ({"CCFLEET_URL": "ftp://x", "CCFLEET_NODE_ID": "n", "CCFLEET_NODE_TOKEN": "t"},
                {"CCFLEET_URL": "https://x", "CCFLEET_NODE_ID": "", "CCFLEET_NODE_TOKEN": "t"},
                {"CCFLEET_URL": "https://x", "CCFLEET_NODE_ID": "n", "CCFLEET_NODE_TOKEN": "t",
                 "CCFLEET_TIMEOUT_S": "soon"}):
        with pytest.raises(agent.AgentConfigError):
            agent.AgentConfig.from_env(env)


def test_credentials_summary_never_contains_tokens(tmp_path):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-SECRET", "refreshToken": "sk-ant-ort01-SECRET",
        "expiresAt": 1700000000000, "scopes": ["user:inference"], "subscriptionType": "max"}}))
    summary = agent.credentials_summary(tmp_path)
    dumped = json.dumps(summary)
    assert "SECRET" not in dumped and "Token" not in dumped
    assert summary["present"] is True and summary["expires_at"] == 1700000000000
    assert summary["subscription_type"] == "max" and summary["mtime"] > 0


def test_credentials_summary_missing_and_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.platform, "system", lambda: "Linux")
    assert agent.credentials_summary(tmp_path) == {"present": False, "store": "file"}
    monkeypatch.setattr(agent.platform, "system", lambda: "Darwin")
    assert agent.credentials_summary(tmp_path) == {"present": None, "store": "keychain"}
    (tmp_path / ".credentials.json").write_text("{not json")
    summary = agent.credentials_summary(tmp_path)
    assert summary["present"] is True and summary.get("parse_error") is True


def test_find_claude_falls_back_to_install_locations(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(agent, "EXTRA_CLAUDE_PATHS", (str(tmp_path / "missing"),))
    assert agent.find_claude() is None

    installed = tmp_path / "claude"
    installed.write_text("#!/bin/sh\necho 2.1.276\n")
    installed.chmod(0o755)
    monkeypatch.setattr(agent, "EXTRA_CLAUDE_PATHS", (str(installed),))
    assert agent.find_claude() == str(installed)

    # a path that exists but is not executable must not be picked
    notexe = tmp_path / "notexe"
    notexe.write_text("x")
    notexe.chmod(0o644)
    monkeypatch.setattr(agent, "EXTRA_CLAUDE_PATHS", (str(notexe),))
    assert agent.find_claude() is None


def test_claude_info_uses_the_fallback_path(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    installed = tmp_path / "claude"
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)
    monkeypatch.setattr(agent, "EXTRA_CLAUDE_PATHS", (str(installed),))
    info = agent.claude_info(fake_runner("2.1.276 (Claude Code)\n"))
    assert info == {"version": "2.1.276", "path": str(installed)}


def test_claude_info(monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(agent, "EXTRA_CLAUDE_PATHS", ())
    assert agent.claude_info(fake_runner()) == {"version": None, "path": None}
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/claude")
    info = agent.claude_info(fake_runner("2.1.92 (Claude Code)\n"))
    assert info == {"version": "2.1.92", "path": "/usr/bin/claude"}

    def failing(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 20)

    assert agent.claude_info(failing)["version"] is None


def test_egress_ip_takes_first_valid_answer():
    answers = iter([urllib.error.URLError("down"), FakeResponse(b"not an ip"),
                    FakeResponse(b" 203.0.113.5\n")])

    def opener(req, timeout):
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    result = agent.egress_ip(["https://a.example", "https://b.example/ip", "https://c.example"],
                             opener)
    assert result == {"ip": "203.0.113.5", "source": "c.example"}
    assert agent.egress_ip(["https://a.example"], lambda req, timeout: FakeResponse(b"x")) == {
        "ip": None, "source": None}


def test_remote_control_and_tmux(monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    assert agent.remote_control_state("svc", fake_runner()) == {"state": "unknown"}
    assert agent.tmux_sessions(fake_runner()) is None
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/bin/" + name)
    assert agent.remote_control_state("svc", fake_runner("active\n")) == {"state": "active"}
    assert agent.tmux_sessions(fake_runner("cc: 1 windows\nrc: 1 windows\n")) == 2
    assert agent.tmux_sessions(fake_runner("no server running on /tmp/tmux", 1)) == 0


def test_build_payload_uses_injected_collectors(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    cfg = agent.AgentConfig(url="https://f.example", node_id="node-a", token="t",
                            claude_config_dir=tmp_path, egress_targets=("https://ip.example",))
    payload = agent.build_payload(cfg, runner=fake_runner(),
                                  opener=lambda req, timeout: FakeResponse(b"203.0.113.9"),
                                  now=lambda: 42.0)
    assert payload["node_id"] == "node-a" and payload["ts"] == 42.0
    assert payload["egress"]["ip"] == "203.0.113.9" and payload["claude"]["version"] is None
    assert payload["disk"]["used_pct"] is None or payload["disk"]["used_pct"] >= 0
    assert "hostname" in payload and payload["agent_version"] == agent.AGENT_VERSION


def test_send_retries_network_errors_but_not_4xx():
    cfg = agent.AgentConfig(url="https://f.example", node_id="node-a", token="t",
                            claude_config_dir=Path("/nonexistent"))
    attempts = {"n": 0}
    sleeps = []

    def flaky(req, timeout):
        attempts["n"] += 1
        assert req.get_header("Authorization") == "Bearer t"
        if attempts["n"] < 3:
            raise urllib.error.URLError("boom")
        return FakeResponse(b'{"ok":true}')

    status, text = agent.send_heartbeat(cfg, {"node_id": "node-a"}, opener=flaky,
                                        sleep=sleeps.append)
    assert (status, text) == (200, '{"ok":true}') and sleeps == [2.0, 4.0]

    def unauthorized(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":"x"}'))

    assert agent.send_heartbeat(cfg, {}, opener=unauthorized, sleep=sleeps.append)[0] == 401
    assert len(sleeps) == 2

    def always_down(req, timeout):
        raise urllib.error.URLError("down")

    assert agent.send_heartbeat(cfg, {}, opener=always_down, sleep=lambda s: None) == (0, "unreachable")


def test_main_print_and_send(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "agent.env"
    env_file.write_text("CCFLEET_URL=https://f.example\nCCFLEET_NODE_ID=node-a\nCCFLEET_NODE_TOKEN=t\n")
    monkeypatch.setattr(agent, "build_payload",
                        lambda cfg, **kw: {"node_id": cfg.node_id})
    assert agent.main(["--env-file", str(env_file), "--print"]) == 0
    assert json.loads(capsys.readouterr().out) == {"node_id": "node-a"}
    monkeypatch.setattr(agent, "send_heartbeat", lambda cfg, payload: (200, "{}"))
    assert agent.main(["--env-file", str(env_file)]) == 0
    monkeypatch.setattr(agent, "send_heartbeat", lambda cfg, payload: (401, "nope"))
    assert agent.main(["--env-file", str(env_file)]) == 1
    assert agent.main(["--env-file", str(tmp_path / "missing.env")]) == 2


def _write_account(tmp_path, **fields):
    """~/.claude.json sits beside the config dir, not inside it."""
    account = {"emailAddress": "someone@example.com", "fullName": "A Person",
               "accountUuid": "uuid-1234", "organizationName": "Acme",
               "profileFetchedAt": 1_700_000_000_000,
               "organizationRateLimitTier": "default_claude_max_20x"}
    account.update(fields)
    sibling = tmp_path.parent / (tmp_path.name + ".json")
    sibling.write_text(json.dumps({"oauthAccount": account}))


def test_account_facts_carry_no_identifiers(tmp_path):
    """The account block holds an email and a name. Neither may leave the machine."""
    cfg = tmp_path / "claude"
    cfg.mkdir()
    _write_account(cfg)
    facts = agent.oauth_account_facts(cfg)
    assert facts == {"account": True, "profile_fetched_at": 1_700_000_000_000,
                     "plan": "default_claude_max_20x"}
    blob = json.dumps(facts)
    for leak in ("example.com", "A Person", "uuid-1234", "Acme"):
        assert leak not in blob, f"{leak} must never reach the payload"


def test_account_facts_find_the_file_beside_a_dotted_config_dir(tmp_path):
    """with_suffix would replace ".work" and read the wrong file entirely."""
    cfg = tmp_path / "claude.work"
    cfg.mkdir()
    _write_account(cfg)
    assert agent.oauth_account_facts(cfg)["profile_fetched_at"] == 1_700_000_000_000
    # And it must not be reading a same-stem neighbour.
    assert not (tmp_path / "claude.json").exists()


def test_account_facts_tolerate_a_missing_or_broken_file(tmp_path):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    assert agent.oauth_account_facts(cfg) == {}
    (cfg.parent / (cfg.name + ".json")).write_text("{not json")
    assert agent.oauth_account_facts(cfg) == {}
    (cfg.parent / (cfg.name + ".json")).write_text(json.dumps({"oauthAccount": "not a dict"}))
    assert agent.oauth_account_facts(cfg) == {}


def test_a_mac_reports_a_login_even_though_there_is_no_file(tmp_path, monkeypatch):
    """Claude Code keeps the credential in the Keychain; the account block is the only signal."""
    monkeypatch.setattr(agent.platform, "system", lambda: "Darwin")
    cfg = tmp_path / "claude"
    cfg.mkdir()
    _write_account(cfg)
    summary = agent.credentials_summary(cfg)
    assert summary["present"] is True, "a signed-in Mac must not look like a missing login"
    assert summary["store"] == "keychain"
    assert summary["profile_fetched_at"] == 1_700_000_000_000
    assert "account" not in summary, "internal flag, not a payload field"


def test_a_mac_with_no_account_reports_unknown_rather_than_missing(tmp_path, monkeypatch):
    """Without the account block there is genuinely nothing to go on; do not cry wolf."""
    monkeypatch.setattr(agent.platform, "system", lambda: "Darwin")
    cfg = tmp_path / "claude"
    cfg.mkdir()
    assert agent.credentials_summary(cfg)["present"] is None


def test_linux_keeps_its_file_facts_and_gains_the_account_ones(tmp_path):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "secret", "expiresAt": 1_700_000_100_000, "subscriptionType": "max"}}))
    _write_account(cfg)
    summary = agent.credentials_summary(cfg)
    assert summary["expires_at"] == 1_700_000_100_000 and summary["subscription_type"] == "max"
    assert summary["profile_fetched_at"] == 1_700_000_000_000
    assert "secret" not in json.dumps(summary)


def test_the_mac_scheduler_carries_no_configuration():
    """agent.env holds the token; the plist must stay free of secrets."""
    import plistlib
    from pathlib import Path
    raw = (Path(__file__).resolve().parents[1] / "laptop" / "com.ccfleet.agent.plist").read_text()
    plist = plistlib.loads(raw.replace("__HOME__", "/Users/example").encode())
    assert plist["Label"] == "com.ccfleet.agent"
    assert plist["ProgramArguments"] == ["/Users/example/.local/bin/ccfleet-agent"]
    assert plist["StartInterval"] == 300, "should match the node's five-minute timer"
    assert "EnvironmentVariables" not in plist, \
        "the agent reads its own env file; putting the token here would publish it"
    assert "TOKEN" not in raw and "CCFLEET_NODE_TOKEN" not in raw


# -- desired state and reconcile ------------------------------------------------


def test_parse_desired_tolerates_anything_a_server_might_send():
    assert agent.parse_desired('{"desired": {"claude_version": "2.1.92"}}') == {
        "claude_version": "2.1.92"}
    # No block, not JSON, or a block of the wrong shape: reconcile nothing rather
    # than crash the heartbeat that already succeeded.
    assert agent.parse_desired('{"ok": true}') == {}
    assert agent.parse_desired("not json") == {}
    assert agent.parse_desired('{"desired": "stable"}') == {}
    assert agent.parse_desired('[1,2,3]') == {}


def test_installable_version_refuses_anything_that_is_not_a_version():
    assert agent.installable_version("2.1.92") == "2.1.92"
    assert agent.installable_version(" stable ") == "stable"
    assert agent.installable_version("latest") == "latest"
    # The server validates too. This is the node declining to hand its own
    # installer an arbitrary argument just because a server asked.
    for junk in ("--force", "-h", "nightly", "v2.1.92", "", "  ", "x" * 41, None, 3, True):
        assert agent.installable_version(junk) is None


def test_state_round_trips_and_a_corrupt_file_is_just_no_state(tmp_path):
    path = tmp_path / "nested" / "reconcile.json"
    agent.write_state(path, {"upgrade": {"to": "2.1.92", "ok": True}})
    assert agent.read_state(path) == {"upgrade": {"to": "2.1.92", "ok": True}}
    path.write_text("{half written")
    assert agent.read_state(path) == {}
    assert agent.read_state(tmp_path / "absent.json") == {}


def _claude_at(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(agent, "find_claude", lambda: str(binary))
    return binary


def test_reconcile_does_nothing_without_a_reason_to_act(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    # No pin at all, an unusable pin, and a pin already satisfied.
    assert agent.reconcile_version({}, "2.1.92", {}, runner) is None
    assert agent.reconcile_version({"claude_version": "--force"}, "2.1.92", {}, runner) is None
    assert agent.reconcile_version({"claude_version": "2.1.92"}, "2.1.92", {}, runner) is None
    assert calls == []


def test_reconcile_installs_a_pin_that_differs_and_reports_what_landed(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        # `install` succeeds; the follow-up `--version` reports the new number.
        out = "2.1.99 (Claude Code)" if argv[1] == "--version" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    result = agent.reconcile_version({"claude_version": "2.1.99"}, "2.1.90", {}, runner, now=10.0)
    assert [c[1] for c in calls] == ["install", "--version"]
    assert calls[0][2] == "2.1.99"
    assert result == {"from": "2.1.90", "to": "2.1.99", "ok": True, "ts": 10.0, "error": None}


def test_a_channel_resolves_once_then_stops_reinstalling(tmp_path, monkeypatch):
    """A channel has no number to compare, so time governs it instead.

    Left to "different from desired", `stable` would reinstall on every beat —
    288 networked installs a day, per node.
    """
    _claude_at(tmp_path, monkeypatch)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        out = "2.1.99 (Claude Code)" if argv[1] == "--version" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    desired = {"claude_version": "stable"}
    first = agent.reconcile_version(desired, "2.1.90", {}, runner, now=1000.0)
    assert calls[0][1:] == ["install", "stable"]
    # Reports the number that landed, not the word that was asked for.
    assert first["to"] == "2.1.99" and first["ok"] is True
    assert first["channel"] == {"target": "stable", "resolved": "2.1.99", "ts": 1000.0}

    state = {"upgrade": {k: v for k, v in first.items() if k != "channel"},
             "channel": first["channel"]}
    calls.clear()

    # The next beat, five minutes later, must do nothing at all.
    assert agent.reconcile_version(desired, "2.1.99", state, runner, now=1300.0) is None
    assert calls == []

    # ...and keep doing nothing, right up to the re-check window.
    just_inside = 1000.0 + agent.CHANNEL_RECHECK_AFTER_S - 1
    assert agent.reconcile_version(desired, "2.1.99", state, runner, now=just_inside) is None
    assert calls == []

    # After the window it looks again, because that is what tracking a channel means.
    assert agent.reconcile_version(desired, "2.1.99", state, runner,
                                   now=1000.0 + agent.CHANNEL_RECHECK_AFTER_S + 1) is not None


def test_a_channel_is_re_resolved_when_the_ground_moves(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)

    def runner(argv, **kw):
        out = "2.2.0 (Claude Code)" if argv[1] == "--version" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    resolved = {"target": "stable", "resolved": "2.1.99", "ts": 1000.0}
    state = {"channel": resolved}
    # Switching channel is a different question.
    assert agent.reconcile_version({"claude_version": "latest"}, "2.1.99", state,
                                   runner, now=1100.0) is not None
    # Something else moved the binary, so the note no longer describes reality.
    assert agent.reconcile_version({"claude_version": "stable"}, "2.0.0", state,
                                   runner, now=1100.0) is not None
    # A note with no usable timestamp is no note.
    for bad_ts in (None, "soon", True):
        broken = {"channel": {**resolved, "ts": bad_ts}}
        assert agent.reconcile_version({"claude_version": "stable"}, "2.1.99", broken,
                                       runner, now=1100.0) is not None


def test_a_failed_install_reports_why_and_then_backs_off(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)

    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such version")

    desired = {"claude_version": "9.9.9"}
    result = agent.reconcile_version(desired, "2.1.90", {}, failing, now=100.0)
    assert result["ok"] is False and result["error"] == "no such version"

    # Retrying every five minutes fixes neither a bad release nor a full disk.
    state = {"upgrade": result}
    assert agent.reconcile_version(desired, "2.1.90", state, failing, now=200.0) is None
    # ...but the back-off is a delay, not a surrender.
    later = agent.reconcile_version(desired, "2.1.90", state, failing,
                                    now=100.0 + agent.INSTALL_RETRY_AFTER_S + 1)
    assert later is not None and later["ok"] is False
    # A different target is a different question, so it is not held back.
    assert agent.reconcile_version({"claude_version": "2.1.95"}, "2.1.90", state,
                                   failing, now=200.0) is not None


def test_an_installer_that_will_not_run_is_reported_not_raised(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)

    def exploding(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 300)

    result = agent.reconcile_version({"claude_version": "2.1.99"}, "2.1.90", {}, exploding, now=5.0)
    assert result["ok"] is False and result["error"] == "TimeoutExpired"


def test_reconcile_is_skipped_when_claude_is_not_installed(monkeypatch):
    monkeypatch.setattr(agent, "find_claude", lambda: None)
    calls = []
    assert agent.reconcile_version({"claude_version": "2.1.99"}, None, {},
                                   lambda argv, **kw: calls.append(argv)) is None
    assert calls == []


def test_the_previous_result_rides_along_on_the_next_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "find_claude", lambda: None)
    cfg = agent.AgentConfig(url="https://f.example", node_id="node-a", token="t",
                            claude_config_dir=tmp_path, state_path=tmp_path / "s.json")
    upgrade = {"from": "2.1.90", "to": "2.1.92", "ok": True, "ts": 1.0, "error": None}
    payload = agent.build_payload(cfg, runner=fake_runner(), opener=lambda *a, **k: FakeResponse(b""),
                                  state={"upgrade": upgrade})
    assert payload["reconcile"] == {"upgrade": upgrade}
    # Nothing to report yet must not invent an empty section.
    assert "reconcile" not in agent.build_payload(
        cfg, runner=fake_runner(), opener=lambda *a, **k: FakeResponse(b""))


def test_an_obsolete_upgrade_record_is_dropped():
    """A failure that stopped being true must not sit in the dashboard forever."""
    failed = {"from": "2.1.90", "to": "9.9.9", "ok": False, "ts": 1.0, "error": "no such version"}

    # The pin was removed, or was never readable.
    assert "upgrade" not in agent.prune_state({"upgrade": failed}, {}, "2.1.90")
    assert "upgrade" not in agent.prune_state({"upgrade": failed},
                                              {"claude_version": "--force"}, "2.1.90")
    # The pin now names something else; the old failure is about a different question.
    assert "upgrade" not in agent.prune_state({"upgrade": failed},
                                              {"claude_version": "2.1.95"}, "2.1.90")
    # Someone installed it by hand, so the failure is over.
    satisfied = {**failed, "to": "2.1.95"}
    assert "upgrade" not in agent.prune_state({"upgrade": satisfied},
                                              {"claude_version": "2.1.95"}, "2.1.95")
    # Still failing, still the live question: keep it, or the back-off is lost.
    kept = agent.prune_state({"upgrade": failed}, {"claude_version": "9.9.9"}, "2.1.90")
    assert kept["upgrade"] == failed
    # Unrelated bookkeeping is never touched.
    assert agent.prune_state({"channel": {"target": "stable"}}, {}, None) == {
        "channel": {"target": "stable"}}


# -- claude auth status ----------------------------------------------------------

AUTH_JSON = json.dumps({
    "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
    "subscriptionType": "max", "analyticsDisabled": False,
    # Everything below is the owner's, and none of it is this agent's business.
    "email": "someone@example.com", "orgId": "a7ba7d79-0aa7-4f6f",
    "orgName": "someone@example.com's Organization",
    "projectsDirectory": "/home/erik/.claude/projects",
    "configDirectory": "/home/erik/.claude",
})


def test_auth_status_reports_the_facts_and_none_of_the_identity(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    out = agent.auth_status(fake_runner(stdout=AUTH_JSON))
    assert out == {"logged_in": True, "auth_method": "claude.ai",
                   "api_provider": "firstParty", "subscription_type": "max"}
    blob = json.dumps(out)
    for private in ("someone@example.com", "a7ba7d79", "Organization", "/home/erik"):
        assert private not in blob


def test_auth_status_says_nothing_rather_than_guessing(tmp_path, monkeypatch):
    """{} means "could not ask", which is not the same as "not signed in"."""
    monkeypatch.setattr(agent, "find_claude", lambda: None)
    assert agent.auth_status(fake_runner(stdout=AUTH_JSON)) == {}
    _claude_at(tmp_path, monkeypatch)
    assert agent.auth_status(fake_runner(stdout="")) == {}
    assert agent.auth_status(fake_runner(stdout="not json")) == {}
    assert agent.auth_status(fake_runner(stdout="[1,2,3]")) == {}
    # A logged-out node is a fact, not a failure to ask.
    assert agent.auth_status(fake_runner(stdout='{"loggedIn": false}')) == {"logged_in": False}


def test_the_cli_answer_overrides_a_file_that_merely_exists(tmp_path, monkeypatch):
    """A credentials file can outlive the login it holds."""
    _claude_at(tmp_path, monkeypatch)
    (tmp_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"expiresAt": 1, "subscriptionType": "max"}}))
    cfg = agent.AgentConfig(url="https://f.example", node_id="node-a", token="t",
                            claude_config_dir=tmp_path, state_path=tmp_path / "s.json")

    def runner(argv, **kw):
        out = '{"loggedIn": false}' if argv[1:3] == ["auth", "status"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    creds = agent.build_payload(cfg, runner=runner,
                               opener=lambda *a, **k: FakeResponse(b""))["credentials"]
    assert creds["logged_in"] is False
    # The file is there, but the login behind it is not, so present follows the CLI.
    assert creds["present"] is False


# -- console-driven sign-in ------------------------------------------------------


class TmuxFake:
    """Records tmux calls and serves whatever the pane should currently show."""

    def __init__(self, pane="", auth='{"loggedIn": false}', rc=0):
        self.pane, self.auth, self.calls, self.rc = pane, auth, [], rc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = ""
        if argv[0] == "tmux" and "capture-pane" in argv:
            out = self.pane
        elif argv[1:3] == ["auth", "status"]:
            out = self.auth
        return subprocess.CompletedProcess(argv, self.rc, stdout=out, stderr="")

    def verbs(self):
        return [a[3] if a[0] == "tmux" else a[1] for a in self.calls]


def test_login_email_must_look_like_one_before_it_reaches_argv():
    assert agent.login_email("a.b+c@example.co.uk") == "a.b+c@example.co.uk"
    for junk in ("--flag", "no-at-sign", "a@b", "; rm -rf /", "", None, 3, "x" * 300):
        assert agent.login_email(junk) is None


def test_find_login_url_reads_the_pane_or_admits_it_cannot():
    assert agent.find_login_url(
        "Visit:\nhttps://claude.ai/oauth/authorize?code=true&client_id=abc\nPaste code:"
    ) == "https://claude.ai/oauth/authorize?code=true&client_id=abc"
    assert agent.find_login_url('see "https://claude.ai/oauth/x?y=1".') == \
        "https://claude.ai/oauth/x?y=1"
    # A prompt change that stops printing a URL must surface, not hang.
    assert agent.find_login_url("no url anywhere") is None


def test_a_sign_in_walks_from_request_to_done(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    tmux = TmuxFake()
    wanted = {"requested_at": 100.0, "email": "owner@example.com"}

    # 1. a new request starts a pane and says so
    progress, state = agent.reconcile_login({"login": wanted}, {}, tmux)
    assert progress == {"state": "requested", "requested_at": 100.0}
    assert "new-session" in tmux.verbs()
    launched = [a for a in tmux.calls if "new-session" in a][0][-1]
    assert "auth login --claudeai --email owner@example.com" in launched

    # 2. nothing to say while the CLI is still printing
    progress, state = agent.reconcile_login({"login": wanted}, state, tmux)
    assert progress is None

    # 3. the URL appears and is carried back once
    tmux.pane = "Visit https://claude.ai/oauth/authorize?code=1 to continue"
    progress, state = agent.reconcile_login({"login": wanted}, state, tmux)
    assert progress["state"] == "url_ready"
    assert progress["url"] == "https://claude.ai/oauth/authorize?code=1"

    # 4. still nothing to say until someone pastes a code
    assert agent.reconcile_login({"login": wanted}, state, tmux)[0] is None

    # 5. the code is typed into the pane
    with_code = {**wanted, "code": " the-code "}
    progress, state = agent.reconcile_login({"login": with_code}, state, tmux)
    assert progress["state"] == "code_sent"
    keys = [a for a in tmux.calls if "send-keys" in a][0]
    assert "the-code" in keys and "Enter" in keys          # stripped, then Enter

    # 6. the CLI, not the pane text, decides whether it worked
    assert agent.reconcile_login({"login": with_code}, state, tmux)[0] is None
    tmux.auth = '{"loggedIn": true}'
    progress, state = agent.reconcile_login({"login": with_code}, state, tmux)
    assert progress["state"] == "done"
    assert "kill-session" in tmux.verbs()
    assert "login" not in state


def test_a_rejected_code_is_reported_rather_than_waited_on(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    tmux = TmuxFake()
    wanted = {"requested_at": 1.0, "email": "a@b.com", "code": "bad"}
    _, state = agent.reconcile_login({"login": wanted}, {}, tmux)
    tmux.pane = "https://claude.ai/oauth/x"
    _, state = agent.reconcile_login({"login": wanted}, state, tmux)
    _, state = agent.reconcile_login({"login": wanted}, state, tmux)   # sends the code
    tmux.pane = "Invalid code. Please try again."
    progress, state = agent.reconcile_login({"login": wanted}, state, tmux)
    assert progress["state"] == "failed" and "not accepted" in progress["detail"]
    assert "login" not in state


def test_a_new_request_supersedes_one_that_is_stuck(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    tmux = TmuxFake()
    _, state = agent.reconcile_login({"login": {"requested_at": 1.0}}, {}, tmux)
    progress, state = agent.reconcile_login({"login": {"requested_at": 2.0}}, state, tmux)
    assert progress == {"state": "requested", "requested_at": 2.0}
    assert state["login"]["requested_at"] == 2.0


def test_cancelling_tidies_up_the_pane(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    tmux = TmuxFake()
    _, state = agent.reconcile_login({"login": {"requested_at": 1.0}}, {}, tmux)
    progress, state = agent.reconcile_login({}, state, tmux)
    assert progress is None and "login" not in state
    assert "kill-session" in tmux.verbs()
    # Nothing in flight and nothing asked for is simply a no-op.
    before = len(tmux.calls)
    assert agent.reconcile_login({}, {}, tmux) == (None, {})
    assert len(tmux.calls) == before


def test_a_node_without_claude_reports_that_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(agent, "find_claude", lambda: None)
    tmux = TmuxFake()
    progress, state = agent.reconcile_login({"login": {"requested_at": 1.0}}, {}, tmux)
    assert progress["state"] == "failed" and "not found" in progress["detail"]


def test_a_tmux_that_fails_is_not_treated_as_a_started_login(tmp_path, monkeypatch):
    """`_run` returns output, not a verdict — a non-zero tmux must not look started."""
    _claude_at(tmp_path, monkeypatch)
    tmux = TmuxFake(rc=1)
    progress, state = agent.reconcile_login({"login": {"requested_at": 1.0}}, {}, tmux)
    assert progress["state"] == "failed"
    assert "login" not in state or state["login"].get("phase") == "failed"


REAL_LOGIN_PANE = """Opening browser to sign in…
If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile&code_challenge=iuN8pMa919pbBIlFZyPT&login_hint=someone%40example.com
Paste code here if prompted:"""


def test_the_url_a_live_sign_in_actually_prints_is_scraped_whole():
    """Both halves of this were wrong until a real node was asked.

    The URL is on claude.com, not claude.ai, and it is ~500 characters, so it
    wraps across pane rows. The original code matched only claude.ai and read the
    pane unjoined, which truncated it to its first 166 characters.
    """
    url = agent.find_login_url(REAL_LOGIN_PANE)
    assert url is not None
    assert url.startswith("https://claude.com/cai/oauth/authorize")
    assert url.endswith("login_hint=someone%40example.com"), "the tail must survive"
    # The live one was 496 characters; this fixture is shorter. What matters is
    # that the whole query string survives, not a particular length.
    assert "code_challenge=" in url and "scope=" in url
    assert len(url) > 250, f"truncated to {len(url)} characters"


def test_the_pane_is_read_with_wrapped_lines_joined(tmp_path, monkeypatch):
    _claude_at(tmp_path, monkeypatch)
    tmux = TmuxFake()
    agent.read_login_pane(tmux)
    capture = [a for a in tmux.calls if "capture-pane" in a][0]
    assert "-J" in capture, "without -J a wrapped URL comes back cut in three"


# -- usage from local transcripts ------------------------------------------------


def _transcript(tmp_path, name, records):
    d = tmp_path / "projects" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_usage_reports_counts_and_no_conversation_content(tmp_path):
    secret = "the user's actual private conversation text"
    _transcript(tmp_path, "a.jsonl", [
        {"timestamp": "2026-09-18T10:00:00Z", "type": "user",
         "message": {"role": "user", "content": secret}},
        {"timestamp": "2026-09-18T10:00:05Z", "message": {
            "role": "assistant", "model": "claude-opus-5", "content": secret,
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_read_input_tokens": 300, "cache_creation_input_tokens": 40}}},
        {"timestamp": "2026-09-19T11:00:00Z", "message": {
            "role": "assistant", "model": "claude-opus-5",
            "usage": {"input_tokens": 5, "output_tokens": 5}}},
    ])
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    assert u["total_tokens"] == 380
    assert u["input_tokens"] == 15 and u["output_tokens"] == 25
    assert u["cache_read_input_tokens"] == 300
    assert u["sessions"] == 1 and u["models"] == ["claude-opus-5"]
    assert u["by_day"] == [{"day": "2026-09-18", "tokens": 370},
                           {"day": "2026-09-19", "tokens": 10}]
    # Parsing decodes the content too — that is unavoidable when reading the
    # record. The guarantee is that none of it is kept or returned, only counts.
    assert secret not in json.dumps(u)


def test_usage_ignores_transcripts_outside_the_window(tmp_path):
    import os
    _transcript(tmp_path, "old.jsonl", [{"timestamp": "2026-01-01T00:00:00Z", "message": {
        "usage": {"input_tokens": 999999}}}])
    old = tmp_path / "projects" / "proj" / "old.jsonl"
    stale = 1789900000.0 - 60 * 86400
    os.utime(old, (stale, stale))
    u = agent.usage_summary(tmp_path, now=1789900000.0, window_days=14)
    assert u["total_tokens"] == 0 and u["sessions"] == 0


def test_usage_survives_a_transcript_it_cannot_parse(tmp_path):
    d = tmp_path / "projects" / "proj"
    d.mkdir(parents=True)
    (d / "broken.jsonl").write_text(
        '{"half written\nnot json at all\n'
        '{"timestamp": "2026-09-19T10:00:00Z", "message": {"usage": {"output_tokens": 7}}}\n')
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    assert u["total_tokens"] == 7, "one bad line must not lose the whole file"


def test_usage_is_empty_rather_than_absent_when_there_is_nothing(tmp_path):
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    assert u["total_tokens"] == 0 and u["by_day"] == [] and u["models"] == []


def test_a_long_lived_transcript_does_not_smuggle_old_usage_into_the_window(tmp_path):
    """Selecting files by mtime is not enough.

    One session transcript touched today can carry records from weeks ago, so a
    figure labelled "last 14 days" would quietly include them.
    """
    _transcript(tmp_path, "long.jsonl", [
        {"timestamp": "2026-07-01T10:00:00Z", "message": {"usage": {"output_tokens": 999999}}},
        {"timestamp": "2026-09-18T10:00:00Z", "message": {"usage": {"output_tokens": 11}}},
        {"timestamp": "2026-09-19T10:00:00Z", "message": {"usage": {"output_tokens": 22}}},
    ])
    # now = 2026-09-20; the July record is far outside a 14-day window.
    u = agent.usage_summary(tmp_path, now=1789900000.0, window_days=14)
    assert u["total_tokens"] == 33, "the July record must not be counted"
    assert [d["day"] for d in u["by_day"]] == ["2026-09-18", "2026-09-19"]


def test_a_record_whose_date_cannot_be_read_is_not_counted(tmp_path):
    """An unplaceable number is worse than a missing one in a windowed figure."""
    _transcript(tmp_path, "undated.jsonl", [
        {"message": {"usage": {"output_tokens": 500}}},
        {"timestamp": 12345, "message": {"usage": {"output_tokens": 500}}},
        {"timestamp": "2026-09-19T10:00:00Z", "message": {"usage": {"output_tokens": 7}}},
    ])
    u = agent.usage_summary(tmp_path, now=1789900000.0, window_days=14)
    assert u["total_tokens"] == 7


def test_sessions_counts_transcripts_that_actually_contributed(tmp_path):
    _transcript(tmp_path, "empty.jsonl", [{"timestamp": "2026-09-19T10:00:00Z",
                                           "type": "user", "message": {"content": "hi"}}])
    _transcript(tmp_path, "real.jsonl", [{"timestamp": "2026-09-19T10:00:01Z",
                                          "message": {"usage": {"output_tokens": 9}}}])
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    assert u["sessions"] == 1 and u["total_tokens"] == 9


def test_a_future_dated_record_is_not_counted(tmp_path):
    """A skewed clock or a wrong timestamp would otherwise inflate the window."""
    _transcript(tmp_path, "skewed.jsonl", [
        {"timestamp": "2027-01-01T00:00:00Z", "message": {"usage": {"output_tokens": 999999}}},
        {"timestamp": "2026-09-19T10:00:00Z", "message": {"usage": {"output_tokens": 8}}},
    ])
    u = agent.usage_summary(tmp_path, now=1789900000.0, window_days=14)
    assert u["total_tokens"] == 8


def test_the_scan_stops_at_a_total_byte_budget(tmp_path, monkeypatch):
    """200 files at the per-file cap would be 800 MiB read every five minutes."""
    monkeypatch.setattr(agent, "USAGE_MAX_BYTES_TOTAL", 2000)
    line = json.dumps({"timestamp": "2026-09-19T10:00:00Z",
                       "message": {"usage": {"output_tokens": 1}}}) + "\n"
    for i in range(12):
        _transcript(tmp_path, f"f{i}.jsonl", [])
        (tmp_path / "projects" / "proj" / f"f{i}.jsonl").write_text(line * 20)
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    # It stops early rather than reading everything, but still reports something.
    assert 0 < u["total_tokens"] < 12 * 20


def test_an_oversized_transcript_is_read_from_its_END(tmp_path, monkeypatch):
    """Transcripts are append-only: the newest records are last.

    Capping from the start would skip exactly the recent usage this reports —
    inverting the answer rather than trimming it.
    """
    monkeypatch.setattr(agent, "USAGE_MAX_BYTES_PER_FILE", 2000)
    d = tmp_path / "projects" / "proj"
    d.mkdir(parents=True)
    old_line = json.dumps({"timestamp": "2026-09-18T10:00:00Z",
                           "message": {"usage": {"output_tokens": 1}}}) + "\n"
    new_line = json.dumps({"timestamp": "2026-09-19T10:00:00Z",
                           "message": {"usage": {"output_tokens": 500}}}) + "\n"
    # Plenty of old records first, then the recent one at the very end.
    (d / "big.jsonl").write_text(old_line * 200 + new_line)

    u = agent.usage_summary(tmp_path, now=1789900000.0)
    days = {p["day"]: p["tokens"] for p in u["by_day"]}
    assert days.get("2026-09-19") == 500, "the newest record must survive the cap"
    # Only the tail was read, so most of the 200 old records were skipped. Reading
    # from the start would have given 200 old and lost the 500 entirely.
    assert days.get("2026-09-18", 0) < 50, f"read too far back: {days}"


def test_the_transcript_walk_itself_is_bounded(tmp_path, monkeypatch):
    """USAGE_MAX_FILES only applies after the walk, so the walk needs its own cap."""
    monkeypatch.setattr(agent, "USAGE_MAX_SCAN", 5)
    d = tmp_path / "projects" / "proj"
    d.mkdir(parents=True)
    line = json.dumps({"timestamp": "2026-09-19T10:00:00Z",
                       "message": {"usage": {"output_tokens": 1}}}) + "\n"
    for i in range(40):
        (d / f"f{i:03d}.jsonl").write_text(line)
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    assert 0 < u["sessions"] <= 5, f"walked more than the cap: {u['sessions']}"


def test_the_window_is_n_calendar_days_and_the_total_matches_the_series(tmp_path):
    """A full window_days of seconds admitted 15 distinct dates while the series
    was trimmed to 14, so the headline total disagreed with the chart under it."""
    # now = 2026-09-20; a 14-day window ending today starts on 2026-09-07.
    _transcript(tmp_path, "span.jsonl", [
        {"timestamp": "2026-09-06T23:59:00Z", "message": {"usage": {"output_tokens": 111}}},
        {"timestamp": "2026-09-07T00:00:00Z", "message": {"usage": {"output_tokens": 7}}},
        {"timestamp": "2026-09-20T00:00:00Z", "message": {"usage": {"output_tokens": 3}}},
    ])
    u = agent.usage_summary(tmp_path, now=1789900000.0, window_days=14)
    days = [p["day"] for p in u["by_day"]]
    assert days == ["2026-09-07", "2026-09-20"], days
    assert len(days) <= 14
    assert u["total_tokens"] == sum(p["tokens"] for p in u["by_day"]) == 10


def test_a_transcript_written_early_on_the_oldest_day_is_still_counted(tmp_path):
    """Files are chosen by mtime, records by calendar day. If the file cutoff is a
    time of day, a transcript last written early on the oldest valid day is
    dropped and the stated window silently undercounts."""
    import os
    _transcript(tmp_path, "edge.jsonl", [
        {"timestamp": "2026-09-07T01:00:00Z", "message": {"usage": {"output_tokens": 42}}},
    ])
    f = tmp_path / "projects" / "proj" / "edge.jsonl"
    # now = 2026-09-20T15:46Z; 14-day window starts 2026-09-07. Touch the file at
    # 02:00 on that day — earlier in the day than "now", which is the trap.
    early = 1788742800.0
    os.utime(f, (early, early))
    u = agent.usage_summary(tmp_path, now=1789900000.0, window_days=14)
    assert u["total_tokens"] == 42, "the oldest valid day must be included in full"


def test_one_enormous_transcript_line_cannot_be_pulled_into_memory(tmp_path, monkeypatch):
    """`for line in fh` materialises a whole line before anything can measure it,
    so a single huge record would defeat every byte cap below it."""
    monkeypatch.setattr(agent, "USAGE_MAX_LINE", 4096)
    d = tmp_path / "projects" / "proj"
    d.mkdir(parents=True)
    monster = json.dumps({"timestamp": "2026-09-19T10:00:00Z",
                          "message": {"content": "x" * 200_000,
                                      "usage": {"output_tokens": 999}}})
    good = json.dumps({"timestamp": "2026-09-19T10:00:01Z",
                       "message": {"usage": {"output_tokens": 5}}})
    (d / "big.jsonl").write_text(monster + "\n" + good + "\n")
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    # The oversized record is dropped; the ordinary one beside it still counts.
    assert u["total_tokens"] == 5


def test_bounded_lines_reports_every_byte_it_reads(tmp_path):
    """Charging only the yielded lines would let a file of oversized records
    consume its whole allowance for free — the exact read the budget prevents."""
    import io
    text = "".join(f"line-{i}\n" for i in range(1000))
    pairs = list(agent._bounded_lines(io.StringIO(text), 200))
    assert pairs and pairs[0][0] == "line-0"
    assert sum(c for _, c in pairs) >= len("".join(ln for ln, _ in pairs if ln))


def test_discarded_oversized_lines_are_still_charged(monkeypatch):
    import io
    monkeypatch.setattr(agent, "USAGE_MAX_LINE", 64)
    monkeypatch.setattr(agent, "USAGE_CHUNK", 1024)
    blob = "x" * 5000 + "\n" + "short\n"
    pairs = list(agent._bounded_lines(io.StringIO(blob), 100_000))
    charged = sum(c for _, c in pairs)
    assert charged >= 5000, f"discarded bytes escaped the budget: {charged}"
    assert [ln for ln, _ in pairs if ln] == ["short"]


def test_skipping_a_partial_tail_record_is_bounded(tmp_path, monkeypatch):
    """readline() is unbounded: one enormous unterminated record would be
    materialised whole, defeating the cap _seek_to_tail exists to apply."""
    monkeypatch.setattr(agent, "USAGE_MAX_BYTES_PER_FILE", 1000)
    monkeypatch.setattr(agent, "USAGE_MAX_LINE", 256)
    monkeypatch.setattr(agent, "USAGE_CHUNK", 128)
    d = tmp_path / "projects" / "proj"
    d.mkdir(parents=True)
    good = json.dumps({"timestamp": "2026-09-19T10:00:00Z",
                       "message": {"usage": {"output_tokens": 6}}})
    # A long unterminated stretch, then a real record at the very end.
    (d / "t.jsonl").write_text("z" * 5000 + "\n" + good + "\n")
    u = agent.usage_summary(tmp_path, now=1789900000.0)
    assert u["total_tokens"] == 6


# -- quota ------------------------------------------------------------------------

# What /usage actually draws, box characters and all. Matching forward from the
# label rather than against this shape is the point of the parser, but a real
# sample is what proves it.
USAGE_PANE = """\
 Usage

 Current session
 ███░░░░░░░░░░░░░░░░░░░░░░░░░  3% used
 Resets 7:50pm (UTC)

 Current week (all models)
 ████░░░░░░░░░░░░░░░░░░░░░░░░  15% used
 Resets Sep 23, 3pm (UTC)

 Current week (Opus)
 ██░░░░░░░░░░░░░░░░░░░░░░░░░░  8% used
 Resets Sep 23, 3pm (UTC)

 Esc to close
"""


def test_parse_quota_reads_both_windows_off_the_usage_screen():
    got = agent.parse_quota(USAGE_PANE)
    assert got == {"session": {"used_pct": 3, "resets": "7:50pm (UTC)"},
                   "week": {"used_pct": 15, "resets": "Sep 23, 3pm (UTC)"}}


def test_parse_quota_survives_a_screen_it_does_not_recognise():
    assert agent.parse_quota("") == {}
    assert agent.parse_quota("Welcome to Claude Code") == {}
    # A label with no bar under it yet: mid-draw, not an answer.
    assert agent.parse_quota("Current session\nResets 7:50pm") == {}
    # Out of range is refused rather than passed up to the server.
    assert agent.parse_quota("Current session\n999% used") == {}


def test_parse_quota_is_not_tied_to_the_bar_characters():
    """The bar is decoration; the label and the percentage carry the meaning."""
    plain = "Current session\n  3% used\nResets in 2 hours\nCurrent week (all models)\n 60% used"
    got = agent.parse_quota(plain)
    assert got["session"] == {"used_pct": 3, "resets": "in 2 hours"}
    assert got["week"]["used_pct"] == 60


def test_parse_quota_does_not_read_past_its_own_block():
    """Each window takes the first percentage after its own label, not a later one."""
    got = agent.parse_quota(USAGE_PANE)
    # "Current week (Opus)" sits after the weekly block with its own 8%; the
    # weekly window must still be 15.
    assert got["week"]["used_pct"] == 15
    # The bound that matters: a label whose own block has not drawn yet must not
    # borrow a number from far below. Half-drawn panes are the normal case here,
    # because the screen is captured while Claude Code is still painting it.
    half_drawn = ("Current session\n" + "\n" * 40 +
                  "Current week (all models)\n 15% used\nResets Sep 23, 3pm (UTC)\n")
    got = agent.parse_quota(half_drawn)
    assert "session" not in got, "a label with no block yet is not an answer"
    assert got["week"]["used_pct"] == 15


def test_parse_quota_bounds_a_hostile_resets_line():
    """The reset string is scraped text and goes on to the server, so it is capped."""
    got = agent.parse_quota("Current session\n5% used\nResets " + "x" * 500)
    assert len(got["session"]["resets"]) == 40
    # It is one line, too: a newline ends the capture rather than swallowing the
    # rest of the screen.
    got = agent.parse_quota("Current session\n5% used\nResets soon\nsecret pane text")
    assert got["session"]["resets"] == "soon"


class QuotaTmux:
    """A tmux that replays a sequence of panes and records what was sent to it."""

    def __init__(self, panes, claude_ok=True):
        self.panes = list(panes)
        self.claude_ok = claude_ok
        self.sent = []
        self.killed = 0

    def __call__(self, argv, **kwargs):
        def done(code=0, out=""):
            return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        if "new-session" in argv:
            return done(0 if self.claude_ok else 1)
        if "kill-session" in argv:
            self.killed += 1
            return done()
        if "send-keys" in argv:
            self.sent.append(argv[-1])
            return done()
        if "capture-pane" in argv:
            return done(0, self.panes.pop(0) if self.panes else "")
        return done()


def test_read_quota_drives_claude_to_its_usage_screen(monkeypatch):
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    tmux = QuotaTmux(["❯ Try \"fix the build\"", USAGE_PANE])
    assert agent.read_quota(tmux)["week"]["used_pct"] == 15
    assert "/usage" in tmux.sent and "Enter" in tmux.sent
    # The session is torn down whether or not it worked, so a stuck claude does
    # not sit on the node until the next beat.
    assert tmux.killed >= 2


def test_read_quota_answers_the_trust_prompt_and_carries_on(monkeypatch):
    """A fresh working directory asks before anything else can happen."""
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    tmux = QuotaTmux(["Do you trust this folder?", "❯ ready", USAGE_PANE])
    assert agent.read_quota(tmux)["session"]["used_pct"] == 3
    assert tmux.sent[:2] == ["Down", "Enter"], "answers the prompt before asking anything"
    assert "/usage" in tmux.sent


def test_read_quota_gives_up_rather_than_hanging(monkeypatch):
    """A node that never draws the screen must not wedge the heartbeat."""
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    clock = iter([0.0] + [float(i) for i in range(1, 400)])
    monkeypatch.setattr(agent.time, "time", lambda: next(clock))
    tmux = QuotaTmux(["nothing useful"] * 200)
    assert agent.read_quota(tmux) is None
    assert tmux.killed >= 2, "still tidies up on the way out"


def test_read_quota_needs_claude_and_a_session(monkeypatch):
    monkeypatch.setattr(agent, "find_claude", lambda: None)
    assert agent.read_quota(fake_runner()) is None
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    assert agent.read_quota(QuotaTmux([], claude_ok=False)) is None


def test_quota_summary_reads_rarely_and_remembers_between_times(monkeypatch):
    """Driving a whole Claude session is expensive; once every 30 minutes is plenty."""
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    reads = []

    def read(runner, now=None):
        reads.append(now)
        return {"session": {"used_pct": 3}}

    monkeypatch.setattr(agent, "read_quota", read)
    report, store = agent.quota_summary({}, fake_runner(), now=1000.0)
    assert report == {"session": {"used_pct": 3}, "checked_at": 1000.0}
    assert store["ts"] == 1000.0 and len(reads) == 1

    # Inside the window: the stored answer is reported, and nothing is driven.
    again, store2 = agent.quota_summary({"quota": store}, fake_runner(), now=1000.0 + 60)
    assert again == {"session": {"used_pct": 3}, "checked_at": 1000.0}
    assert store2 is None and len(reads) == 1, "no second read inside the window"

    # Past it: read again.
    agent.quota_summary({"quota": store}, fake_runner(), now=1000.0 + agent.QUOTA_REFRESH_S + 1)
    assert len(reads) == 2


def test_quota_summary_keeps_the_last_answer_when_a_read_fails(monkeypatch):
    """Blanking the card would read as "no quota", which is a different claim."""
    monkeypatch.setattr(agent, "read_quota", lambda runner, now=None: None)
    stale = {"session": {"used_pct": 3}, "checked_at": 500.0, "ts": 500.0}
    report, store = agent.quota_summary({"quota": stale}, fake_runner(),
                                        now=500.0 + agent.QUOTA_REFRESH_S + 1)
    assert report == {"session": {"used_pct": 3}, "checked_at": 500.0}
    assert store is None, "a failed read must not restamp the cache as fresh"
    # Nothing cached and nothing read is simply nothing.
    assert agent.quota_summary({}, fake_runner(), now=1.0) == (None, None)


def test_quota_summary_ignores_a_corrupt_cache(monkeypatch):
    monkeypatch.setattr(agent, "read_quota", lambda runner, now=None: {"week": {"used_pct": 9}})
    for junk in ("not a mapping", [1, 2], 7, None):
        report, store = agent.quota_summary({"quota": junk}, fake_runner(), now=1.0)
        assert report == {"week": {"used_pct": 9}, "checked_at": 1.0}
        assert store["ts"] == 1.0


def test_read_quota_waits_for_both_windows_before_settling(monkeypatch):
    """A half-painted screen would otherwise be cached as the answer for 30 min."""
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    partial = "Current session\n 3% used\nResets 7:50pm (UTC)\n"
    tmux = QuotaTmux(["❯ ready", partial, partial, USAGE_PANE])
    got = agent.read_quota(tmux)
    assert got is not None and "session" in got and "week" in got
    assert got["week"]["used_pct"] == 15


def test_read_quota_settles_for_a_partial_screen_at_the_deadline(monkeypatch):
    """One window is still worth reporting; a plan may simply not show the other."""
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    clock = iter([0.0] + [float(i) for i in range(1, 400)])
    monkeypatch.setattr(agent.time, "time", lambda: next(clock))
    partial = "Current session\n 3% used\nResets 7:50pm (UTC)\n"
    tmux = QuotaTmux(["❯ ready"] + [partial] * 200)
    got = agent.read_quota(tmux)
    assert got == {"session": {"used_pct": 3, "resets": "7:50pm (UTC)"}}


def test_parse_quota_will_not_borrow_a_number_from_a_window_it_does_not_report():
    """/usage also draws per-model windows. Those are not the weekly figure.

    Isolates the line bound: the block below carries no label this parser knows,
    so only proximity stops the session window from claiming its 8%.
    """
    pane = ("Current session\n" + "\n" * 5 +
            "Current week (Opus)\n ██  8% used\nResets Sep 23, 3pm (UTC)\n")
    assert "session" not in agent.parse_quota(pane)


def test_parse_quota_stops_at_the_next_window_even_when_it_is_adjacent():
    """Isolates the label guard: the next block starts before the line bound would."""
    pane = "Current session\nCurrent week (all models)\n 15% used\nResets Sep 23\n"
    got = agent.parse_quota(pane)
    assert "session" not in got, "the session block never drew; it has no number"
    assert got["week"]["used_pct"] == 15


def test_read_quota_only_ever_trusts_the_owners_home(monkeypatch, tmp_path):
    """The loop answers Claude Code's folder-trust prompt, so the directory that
    answer applies to cannot be left to however the agent happened to be started.
    Run it by hand from a checked-out project and that project would be trusted.
    """
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    monkeypatch.setattr(agent.Path, "home", staticmethod(lambda: tmp_path))
    opened = []

    class Recording(QuotaTmux):
        def __call__(self, argv, **kwargs):
            if "new-session" in argv:
                opened.append(argv)
            return super().__call__(argv, **kwargs)

    tmux = Recording(["❯ ready", USAGE_PANE])
    assert agent.read_quota(tmux) is not None
    assert len(opened) == 1
    argv = opened[0]
    assert "-c" in argv and argv[argv.index("-c") + 1] == str(tmp_path)


def test_read_quota_refuses_rather_than_trusting_an_unknown_directory(monkeypatch):
    monkeypatch.setattr(agent, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(agent, "_quota_home", lambda: None)
    tmux = QuotaTmux(["❯ ready", USAGE_PANE])
    assert agent.read_quota(tmux) is None
    assert tmux.sent == [], "nothing is typed into a session that was never opened"


def test_quota_home_will_not_hand_back_something_that_is_not_a_directory(monkeypatch,
                                                                        tmp_path):
    monkeypatch.setattr(agent.Path, "home", staticmethod(lambda: tmp_path))
    assert agent._quota_home() == str(tmp_path)
    missing = tmp_path / "gone"
    monkeypatch.setattr(agent.Path, "home", staticmethod(lambda: missing))
    assert agent._quota_home() is None

    def boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(agent.Path, "home", staticmethod(boom))
    assert agent._quota_home() is None
