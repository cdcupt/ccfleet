"""The terminal entry owns local tools and never turns subscription pairing into paid access."""

import io
import runpy
from pathlib import Path

import pytest

from ccfleet_agent import client_experience as experience
from ccfleet_agent import gemini_preview as preview

ROOT = Path(__file__).resolve().parents[1]


def test_public_commands_and_replacement(capsys, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    parser = cli["parser"]()
    assert parser.parse_args(["claude", "--continue"]).continue_session
    assert parser.parse_args(["gemini", "--preview"]).preview
    with pytest.raises(SystemExit) as retired:
        parser.parse_args(["local"])
    assert retired.value.code == 2 and "use ccfleet claude" in capsys.readouterr().err
    monkeypatch.setattr(cli["sys"].stdin, "isatty", lambda: False)
    assert cli["main"](["gemini"]) == 2
    assert "No API billing" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["agy", "claude"])
def test_up_to_date_launch_checks_vendor_each_time(name, tmp_path, monkeypatch):
    checks = []
    monkeypatch.setattr(
        experience, "_latest_agent", lambda agent, stage, env: (checks.append(agent), "1.2.3")[1]
    )
    monkeypatch.setattr(experience, "_verify_google_binary", lambda _: None)
    monkeypatch.setattr(experience, "_installed_agent_version", lambda *args: (1, 2, 3))
    monkeypatch.setattr(experience.subprocess, "run", lambda *a, **kw: pytest.fail("reinstall"))
    for _ in range(2):
        assert experience.prepare_agent(name, tmp_path, lambda: "/native/agent") == "/native/agent"
    assert checks == [name, name]


@pytest.mark.parametrize("installed", [True, False])
@pytest.mark.parametrize("name", ["agy", "claude"])
def test_install_or_upgrade_then_verify(name, installed, tmp_path, monkeypatch):
    actions = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-only")

    def latest(agent, stage, env):
        assert "ANTHROPIC_API_KEY" not in env
        return "2.0.0"

    monkeypatch.setattr(experience, "_latest_agent", latest)
    monkeypatch.setattr(experience, "_verify_google_binary", lambda _: None)
    monkeypatch.setattr(
        experience, "_install_google", lambda *args: actions.append("google archive")
    )
    monkeypatch.setattr(experience, "_vendor_download", lambda url, *args: actions.append(url))
    monkeypatch.setattr(experience.subprocess, "run", lambda cmd, **kw: actions.append(cmd))
    versions = iter([(1, 0, 0), (2, 0, 0)] if installed else [(2, 0, 0)])
    monkeypatch.setattr(experience, "_installed_agent_version", lambda *a: next(versions))
    binaries = iter(["/fixture/native" if installed else None, "/fixture/native"])
    assert experience.prepare_agent(name, tmp_path, lambda: next(binaries)) == "/fixture/native"
    if installed:
        assert actions == [["/fixture/native", "update"]]
    elif name == "agy":
        assert actions == ["google archive"]
    else:
        assert actions[0] == "https://claude.ai/install.sh" and actions[-1][-1] == "latest"


def test_gateway_preview_exchanges_gemini_tool_calls_without_a_provider():
    nonce, marker = "a" * 32, "ccfleet-input-" + "e" * 40
    file = "/project/.ccfleet-preview-" + nonce + "/" + preview.PROOF
    inp, out = io.BytesIO(), io.BytesIO()
    preview.send(inp, {"nonce": nonce, "file": file, "version": 1})
    for contents in ([], [{"parts": [{"functionResponse": {"response": {"text": marker}}}]}], []):
        preview.send(
            inp,
            {
                "path": "/v1beta/models/gemini-test:streamGenerateContent",
                "body": {"contents": contents},
            },
        )
    inp.seek(0)
    assert preview.slot(inp, out, lambda: True) == 0
    out.seek(0)
    assert preview.receive(out) == {"version": 1, "mode": "synthetic"}
    for turn in range(3):
        assert preview._allowed_reply(preview.receive(out), turn, file, marker, nonce)
    assert preview.slot(io.BytesIO(), io.BytesIO(), lambda: False) == 2


def test_preview_rejects_another_file_or_tool():
    reply = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "read_file", "args": {"Path": "/other-project"}}}
                    ]
                }
            }
        ]
    }
    assert not preview._allowed_reply(reply, 0, "/selected-file", "value", "a" * 32)
