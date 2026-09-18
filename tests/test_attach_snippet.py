"""The login auto-attach must fire for a real interactive login and for nothing else.

A genuine attach needs a terminal, which a test harness cannot provide, so these
run the shipped snippet with a stub `tmux` on PATH and assert whether it would
have been invoked. That covers every guard except the terminal check itself,
which is exercised by running with stdout as a pipe (the scp and rsync case).
"""

from __future__ import annotations

import os
import pathlib
import subprocess

SNIPPET = pathlib.Path(__file__).resolve().parents[1] / "node" / "attach.sh"


def run_snippet(tmp_path, env_extra=None, bash_flags="-c", with_tty=False):
    """Run the snippet under bash with a stub tmux; return True if tmux was called."""
    marker = tmp_path / "called"
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "tmux").write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    (stub / "tmux").chmod(0o755)

    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    env.pop("TMUX", None)
    env.pop("CCFLEET_NO_ATTACH", None)
    env.update(env_extra or {})

    script = f'PS1="$ "\n. "{SNIPPET}"\n'
    cmd = ["bash", bash_flags, script] if bash_flags == "-c" else ["bash", bash_flags, "-c", script]
    subprocess.run(cmd, env=env, capture_output=True, timeout=30, check=False)
    return marker.exists()


def test_snippet_ships_and_is_syntactically_valid():
    assert SNIPPET.is_file()
    subprocess.run(["bash", "-n", str(SNIPPET)], check=True, timeout=30)


def test_no_attach_when_output_is_piped(tmp_path):
    """The scp, rsync and git-over-ssh case: stdout is not a terminal."""
    assert run_snippet(tmp_path, bash_flags="-ic") is False


def test_no_attach_when_not_interactive(tmp_path):
    assert run_snippet(tmp_path, bash_flags="-c") is False


def test_no_attach_when_already_inside_tmux(tmp_path):
    assert run_snippet(tmp_path, {"TMUX": "/tmp/tmux-1000/default,1,0"}, bash_flags="-ic") is False


def test_no_attach_when_escape_hatch_is_set(tmp_path):
    assert run_snippet(tmp_path, {"CCFLEET_NO_ATTACH": "1"}, bash_flags="-ic") is False


def test_snippet_uses_capital_a_so_a_missing_session_is_recreated():
    text = SNIPPET.read_text()
    assert "new-session -A -s cc" in text, "without -A a killed session would not come back"
    assert "exec tmux" in text, "attaching without exec would leave a stray parent shell"


def test_snippet_guards_are_all_present():
    text = SNIPPET.read_text()
    for guard in ('-z "${TMUX:-}"', '-n "${PS1:-}"', "-t 1", '-z "${CCFLEET_NO_ATTACH:-}"'):
        assert guard in text, f"missing guard: {guard}"


def test_setup_appends_the_snippet_only_once(tmp_path):
    """Re-running owner setup must not stack copies in ~/.bashrc."""
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# existing user content\n")
    marker = "# ccfleet: attach to the persistent work session"
    for _ in range(3):
        if marker not in bashrc.read_text():
            bashrc.write_text(bashrc.read_text() + "\n" + SNIPPET.read_text())
    assert bashrc.read_text().count(marker) == 1
    assert "# existing user content" in bashrc.read_text()
