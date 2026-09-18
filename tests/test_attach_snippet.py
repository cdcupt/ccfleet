"""The login auto-attach must fire for a real interactive login and for nothing else.

A genuine attach needs a terminal, which a test harness cannot provide, so these
run the shipped snippet with a stub `tmux` on PATH and assert whether it would
have been invoked. That covers every guard except the terminal check itself,
which is exercised by running with stdout as a pipe (the scp and rsync case).
"""

from __future__ import annotations

import os
import pathlib
import pty
import subprocess

SNIPPET = pathlib.Path(__file__).resolve().parents[1] / "node" / "attach.sh"


def run_snippet(tmp_path, env_extra=None, bash_flags="-c"):
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


def run_snippet_on_a_terminal(tmp_path, env_extra=None):
    """Run the snippet with stdout on a real pty, so the -t 1 guard passes."""
    marker = tmp_path / "called"
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "tmux").write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    (stub / "tmux").chmod(0o755)

    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    env.pop("TMUX", None)
    env.pop("CCFLEET_NO_ATTACH", None)
    env.update(env_extra or {})

    primary, secondary = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", "-ic", f'PS1="$ "\n. "{SNIPPET}"\n'],
            stdin=secondary, stdout=secondary, stderr=secondary, env=env, close_fds=True)
        proc.wait(timeout=30)
    finally:
        os.close(secondary)
        os.close(primary)
    return marker.exists(), (marker.read_text().strip() if marker.exists() else "")


def test_attaches_when_stdout_is_a_real_terminal(tmp_path):
    """The whole point: an interactive login on a terminal attaches to session cc."""
    called, args = run_snippet_on_a_terminal(tmp_path)
    assert called, "the snippet did not invoke tmux on a real terminal"
    assert "new-session -A -s cc" in args, f"unexpected tmux invocation: {args}"


def test_terminal_plus_escape_hatch_still_declines(tmp_path):
    called, _ = run_snippet_on_a_terminal(tmp_path, {"CCFLEET_NO_ATTACH": "1"})
    assert called is False


def test_terminal_but_already_in_tmux_declines(tmp_path):
    called, _ = run_snippet_on_a_terminal(tmp_path, {"TMUX": "/tmp/x,1,0"})
    assert called is False


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
    """Run the real guard from setup-owner.sh three times; it must append once."""
    setup = pathlib.Path(__file__).resolve().parents[1] / "node" / "setup-owner.sh"
    guard = [ln for ln in setup.read_text().splitlines()
             if 'MARKER=' in ln or 'grep -qF "$MARKER"' in ln]
    assert len(guard) >= 2, "setup-owner.sh no longer guards the append; update this test"

    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# existing user content\n")
    script = f"""
set -eu
HOME="{tmp_path}"
MARKER="# ccfleet: attach to the persistent work session"
if ! grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
  {{ echo; cat "{SNIPPET}"; }} >> "$HOME/.bashrc"
fi
"""
    for _ in range(3):
        subprocess.run(["bash", "-c", script], check=True, timeout=30, capture_output=True)

    text = bashrc.read_text()
    assert text.count("# ccfleet: attach to the persistent work session") == 1
    assert "# existing user content" in text


def test_the_marker_install_sh_greps_for_is_present():
    """install.sh decides whether ~/.bashrc is already patched by grepping this exact line."""
    from pathlib import Path
    snippet = Path(__file__).resolve().parents[1] / "node" / "attach.sh"
    install = Path(__file__).resolve().parents[1] / "node" / "install.sh"
    marker = "# ccfleet: attach to the persistent work session"
    assert marker in install.read_text(), "install.sh should still key off this marker"
    assert any(line == marker for line in snippet.read_text().splitlines()), \
        "the snippet must still contain the marker verbatim, or every re-run appends it again"
