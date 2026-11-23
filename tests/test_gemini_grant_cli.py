"""Project access can be granted or revoked locally without opening a slot."""
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from ccfleet_agent import project_files

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def controls(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    project.mkdir()
    fleet = tmp_path / "ccfleet"
    monkeypatch.setenv("CCFLEET_HOME", str(fleet))
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["main"].__globals__
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(scope, "project_files", lambda: project_files)
    for name in ("pair_gemini", "load_config", "ssh_transport_command", "slot_component"):
        monkeypatch.setitem(scope, name, lambda *_a, **_k: pytest.fail("no slot call for a grant"))
    def change(*args):
        return cli["main"](["gemini", "--project", str(project), *args])
    return project_files.ProjectAccess(project.resolve(), fleet.resolve()), change, cli


def test_grant_requires_real_local_terminal_then_revoke_is_offline(controls, monkeypatch, capsys):
    access, change, _ = controls
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert change("--full-access") != 0 and not access.enabled()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr("termios.tcflush", lambda *_: None)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert change("--full-access") != 0 and not access.enabled()
    monkeypatch.setattr("builtins.input", lambda _: "ENABLE")
    assert change("--full-access", "--publish-to", "git@github.com:example/project.git") == 0
    assert access.enabled() and access.remote() == "https://github.com/example/project"
    assert "https://github.com/example/project" in capsys.readouterr().out
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert change("--revoke-full-access") == 0 and not access.enabled()
    assert change("--revoke-full-access") == 0


def test_origin_reads_only_literal_checkout_config(controls, tmp_path):
    access, _, cli = controls
    subprocess.run(["git", "init", "-q", str(access.project)], check=True)
    f = access.project / ".git" / "config"
    original = f.read_text()
    secret = tmp_path / "unrelated-config"
    secret.write_text('[remote "origin"]\nurl = https://github.com/other/untrusted\n')
    f.write_text(original + f'[include]\npath = {secret}\n[remote "origin"]\n'
                 'url = git@gitlab.com:team/subgroup/demo.git\n')
    assert cli["project_origin"](access) == "https://gitlab.com/team/subgroup/demo"
    with f.open("a") as out:
        out.write("pushurl = https://github.com/destination/approved.git\n")
    assert cli["project_origin"](access) == "https://github.com/destination/approved"
    with f.open("a") as out:
        out.write("pushurl = https://github.com/other/extra\n")
    assert cli["project_origin"](access) is None
    f.unlink()
    f.symlink_to(secret)
    with pytest.raises(OSError):
        cli["project_origin"](access)


@pytest.mark.parametrize("raw", ["file:///tmp/repo", "https://github.com/a/b?ref=x",
                                 "https://token@github.com/org/repo", "git@github.com:../repo",
                                 "https://github.com/org/repo/extra", "https://github.com/a/%2f",
                                 "https://gitlab.com/a/../repo", "https://github.com:443/a/b"])
def test_remote_rejections_do_not_echo_user_input(controls, raw):
    _, _, cli = controls
    with pytest.raises(cli["CliError"]) as exc:
        cli["canonical_publish_remote"](raw)
    assert raw not in str(exc.value)


def test_mac_opt_in_from_home_and_no_repeat_approval(tmp_path, monkeypatch, capsys):
    import os
    import pwd
    from types import SimpleNamespace

    personal = tmp_path.resolve() / "personal"
    personal.mkdir(mode=0o700)
    project = personal / "development" / "calculator"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='calculator'\n")
    fleet = personal / ".config" / "ccfleet"
    monkeypatch.setenv("CCFLEET_HOME", str(fleet))
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(personal),
                                                                     pw_uid=os.getuid()))
    monkeypatch.setattr(sys, "platform", "darwin")
    tmp_cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = tmp_cli["main"].__globals__
    monkeypatch.setitem(scope, "project_files", lambda: project_files)
    for name in ("load_config", "pair_gemini", "ssh_transport_command", "slot_component"):
        monkeypatch.setitem(scope, name, lambda *_a, **_k: pytest.fail("unexpected slot call"))

    def call(folder, *flags):
        return tmp_cli["main"](["gemini", "--project", str(folder), *flags])

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert call(personal, "--full-access") != 0
    assert call(personal, "--full-access", "--publish-to", "https://github.com/owner/repo") != 0
    assert not project_files.ProjectAccess(project, fleet).enabled()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr("termios.tcflush", lambda *_: None)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda question: prompts.append(question) or "ENABLE")
    assert call(personal, "--full-access") == 0
    assert project_files.ProjectAccess(project, fleet).enabled()
    assert project_files.ProjectAccess(project, fleet).remote() is None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert call(personal, "--full-access") == 0 and call(project, "--full-access") == 0
    assert len(prompts) == 1
    assert call(personal, "--revoke-full-access") == 0
    assert not project_files.ProjectAccess(project, fleet).enabled()
    assert "All-projects access revoked" in capsys.readouterr().out
