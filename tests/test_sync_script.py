from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "laptop" / "ccfleet-sync.sh"


def fake_mutagen(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "mutagen.log"
    tool = bindir / "mutagen"
    tool.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$CCFLEET_TEST_LOG\"\n")
    tool.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", CCFLEET_TEST_LOG=str(log))
    return env, log


def run(args, *, env=None):
    return subprocess.run(["bash", str(SCRIPT), *args], text=True, capture_output=True,
                          timeout=30, env=env)


def test_script_parses_and_help_needs_no_dependency():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    result = run(["--help"], env={**os.environ, "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0 and "two-way" in result.stdout


def test_start_uses_two_way_safe_and_privacy_ignores(tmp_path):
    env, log = fake_mutagen(tmp_path)
    project = tmp_path / "my project"
    project.mkdir()
    result = run(["start", str(project), "slot01@203.0.113.10"], env=env)
    assert result.returncode == 0, result.stderr
    args = log.read_text().splitlines()
    assert args[:2] == ["sync", "create"]
    assert "--sync-mode=two-way-safe" in args and "--ignore-vcs" in args
    for ignored in ("node_modules", ".venv", "venv", "dist", "build", ".env", ".env.*"):
        assert f"--ignore={ignored}" in args
    assert str(project.resolve()) in args
    assert "slot01@203.0.113.10:workspace/my-project" in args
    assert "Conflicts stop safely" in result.stdout


def test_management_commands_are_scoped_to_ccfleet_sessions(tmp_path):
    env, log = fake_mutagen(tmp_path)
    assert run(["pause", "ccfleet-slot-project-123"], env=env).returncode == 0
    assert log.read_text().splitlines() == ["sync", "pause", "ccfleet-slot-project-123"]
    refused = run(["stop", "somebody-elses-session"], env=env)
    assert refused.returncode != 0 and "refusing" in refused.stderr


def test_remote_path_cannot_escape_the_slot_home(tmp_path):
    env, log = fake_mutagen(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    result = run(["start", str(project), "slot01@host", "../other"], env=env)
    assert result.returncode != 0 and "may not contain .." in result.stderr
    assert not log.exists()
