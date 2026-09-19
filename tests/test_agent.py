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
