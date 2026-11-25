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
    assert "interactive terminal" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["claude"])
def test_up_to_date_launch_checks_vendor_each_time(name, tmp_path, monkeypatch):
    checks = []
    monkeypatch.setattr(
        experience, "_latest_agent", lambda agent, stage, env: (checks.append(agent), "1.2.3")[1]
    )
    monkeypatch.setattr(experience, "_installed_agent_version", lambda *args: (1, 2, 3))
    monkeypatch.setattr(experience.subprocess, "run", lambda *a, **kw: pytest.fail("reinstall"))
    for _ in range(2):
        assert experience.prepare_agent(name, tmp_path, lambda: "/native/agent") == "/native/agent"
    assert checks == [name, name]


@pytest.mark.parametrize("installed", [True, False])
@pytest.mark.parametrize("name", ["claude"])
def test_install_or_upgrade_then_verify(name, installed, tmp_path, monkeypatch):
    actions = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-only")

    def latest(agent, stage, env):
        assert "ANTHROPIC_API_KEY" not in env
        return "2.0.0"

    monkeypatch.setattr(experience, "_latest_agent", latest)
    monkeypatch.setattr(experience, "_vendor_download", lambda url, *args: actions.append(url))
    monkeypatch.setattr(experience.subprocess, "run", lambda cmd, **kw: actions.append(cmd))
    monkeypatch.setattr(experience, "_confirm_agent_update", lambda *_: True)
    versions = iter([(1, 0, 0), (1, 0, 0), (2, 0, 0)] if installed else [(2, 0, 0)])
    monkeypatch.setattr(experience, "_installed_agent_version", lambda *a: next(versions))
    binaries = iter((["/fixture/native"] if installed else [None]) * 2 + ["/fixture/native"])
    assert experience.prepare_agent(name, tmp_path, lambda: next(binaries)) == "/fixture/native"
    if installed:
        assert actions == [["/fixture/native", "update"]]
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


@pytest.mark.parametrize("answer,accept", [("y\n", True), ("YES\n", True), ("n\n", False),
                                           ("\n", False), ("", False)])
def test_update_prompt_defaults_to_no_and_names_versions(monkeypatch, capsys, answer, accept):
    inp = io.StringIO(answer)
    monkeypatch.setattr(inp, "isatty", lambda: True)
    monkeypatch.setattr(experience.sys, "stdin", inp)
    for stream in (experience.sys.stdout, experience.sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True)
    assert experience._confirm_agent_update("agy", (1, 2, 3), "1.2.4") is accept
    out = capsys.readouterr()
    assert not out.out and "agy 1.2.3 is installed; 1.2.4 is available" in out.err


@pytest.mark.parametrize("agent", ["claude", "agy"])
def test_piped_launch_keeps_outdated_harness_and_preserves_input(tmp_path, monkeypatch, agent):
    binary = tmp_path / agent
    binary.touch()
    pipe = io.StringIO('{"type":"user"}\n')
    monkeypatch.setattr(experience.sys, "stdin", pipe)
    monkeypatch.setattr(experience.sys, "platform", "darwin")
    monkeypatch.setattr(experience, "_installed_agent_version", lambda *_: (1, 0, 0))
    monkeypatch.setattr(experience, "_google_version", lambda *_: (1, 0, 0))
    monkeypatch.setattr(experience, "_latest_agent", lambda *_: "2.0.0")
    monkeypatch.setattr(experience, "_google_manifest", lambda *_: {"version":"2.0.0"})
    monkeypatch.setattr(experience, "_install_slot_google", lambda *_: pytest.fail("unapproved install"))
    monkeypatch.setattr(experience.subprocess, "run", lambda *_a, **_kw: pytest.fail("unapproved update"))
    assert experience.prepare_agent(agent, tmp_path, lambda: str(binary), slot_command=["slot"]) == str(binary)
    assert pipe.tell() == 0


@pytest.mark.parametrize("agent", ["claude", "agy"])
def test_failed_release_lookup_launches_a_verified_local_copy_only(tmp_path, monkeypatch, agent):
    binary = tmp_path / agent
    binary.touch()
    monkeypatch.setattr(experience.sys, "platform", "darwin")
    monkeypatch.setattr(experience, "_google_version", lambda *_: (1, 0, 0))
    monkeypatch.setattr(experience, "_installed_agent_version", lambda *_: (1, 0, 0))
    def unavailable(*_):
        raise experience.ExperienceError("release offline")
    monkeypatch.setattr(experience, "_google_manifest", unavailable)
    monkeypatch.setattr(experience, "_latest_agent", unavailable)
    assert experience.prepare_agent(agent, tmp_path, lambda: str(binary), slot_command=["slot"]) == str(binary)
    with pytest.raises(experience.ExperienceError, match="offline"):
        experience.prepare_agent(agent, tmp_path, lambda: None, slot_command=["slot"])


def test_concurrent_install_is_rechecked_without_upgrading_it(tmp_path, monkeypatch):
    found = iter([None, "/other/installed/claude"])
    monkeypatch.setattr(experience, "_latest_agent", lambda *_: "2.0.0")
    monkeypatch.setattr(experience, "_installed_agent_version", lambda *_: (1, 0, 0))
    monkeypatch.setattr(experience, "_vendor_download", lambda *_: pytest.fail("second install"))
    assert experience.prepare_agent("claude", tmp_path, lambda: next(found)) == "/other/installed/claude"
