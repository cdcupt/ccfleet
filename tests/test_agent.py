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


def test_claude_info(monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
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
    monkeypatch.setattr(agent, "build_payload", lambda cfg: {"node_id": cfg.node_id})
    assert agent.main(["--env-file", str(env_file), "--print"]) == 0
    assert json.loads(capsys.readouterr().out) == {"node_id": "node-a"}
    monkeypatch.setattr(agent, "send_heartbeat", lambda cfg, payload: (200, "{}"))
    assert agent.main(["--env-file", str(env_file)]) == 0
    monkeypatch.setattr(agent, "send_heartbeat", lambda cfg, payload: (401, "nope"))
    assert agent.main(["--env-file", str(env_file)]) == 1
    assert agent.main(["--env-file", str(tmp_path / "missing.env")]) == 2
