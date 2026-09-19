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
import shutil
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


def run_snippet_recording_term(tmp_path, term, tmux_exit=0, hide_infocmp=False):
    """Like the above, but the stub records the TERM it was handed, and can fail.

    Both matter: the snippet rewrites an unknown TERM before calling tmux, and a
    tmux that refuses to start must not end the login.
    """
    marker = tmp_path / "term-seen"
    survived = tmp_path / "survived"
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "tmux").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$TERM" >> "{marker}"\nexit {tmux_exit}\n')
    (stub / "tmux").chmod(0o755)

    # PATH of only the stub hides infocmp while still finding our tmux, which is
    # the case where the snippet cannot check the terminal in advance. bash has
    # to be resolved before PATH is stripped, or it cannot be spawned at all.
    bash = shutil.which("bash") or "/bin/bash"
    env_path = str(stub) if hide_infocmp else f"{stub}:{os.environ['PATH']}"
    env = {**os.environ, "PATH": env_path, "HOME": str(tmp_path)}
    if term is None:
        env.pop("TERM", None)
    else:
        env["TERM"] = term
    env.pop("TMUX", None)
    env.pop("CCFLEET_NO_ATTACH", None)

    primary, secondary = pty.openpty()
    try:
        proc = subprocess.Popen(
            [bash, "-ic",
             f'PS1="$ "\n. "{SNIPPET}"\nprintf "%s" "$TERM" > "{survived}"\n'],
            stdin=secondary, stdout=secondary, stderr=secondary, env=env, close_fds=True)
        proc.wait(timeout=30)
    finally:
        os.close(secondary)
        os.close(primary)
    calls = marker.read_text().split() if marker.exists() else []
    left_with = survived.read_text() if survived.exists() else None
    return (calls[-1] if calls else None), survived.exists(), calls, left_with


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
    # Was `exec tmux`. That ended the login whenever tmux refused to start, which
    # a terminal the node has no terminfo for reliably causes. `&& exit` keeps the
    # same outcome on success, where leaving tmux ends the ssh session, while a
    # failure now falls through to an ordinary shell.
    assert "exec tmux" not in text, "exec turns any tmux failure into a disconnect"
    assert 'tmux new-session -A -s cc -c "$HOME/workspace" && exit' in text, \
        "on success the shell must still exit, or leaving tmux leaves a stray prompt"


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


def test_a_terminal_the_node_does_not_know_still_attaches(tmp_path):
    """Ghostty, kitty, wezterm and alacritty are all absent from a stock Debian.

    Before this, tmux refused with "missing or unsuitable terminal" and the exec
    meant the owner was disconnected rather than dropped to a shell.
    """
    # Not "xterm-ghostty": a laptop running Ghostty has that entry, so the test
    # would pass or fail by accident depending on where it runs. This name is
    # unknown everywhere, which is the condition under test.
    seen, _, _, _ = run_snippet_recording_term(tmp_path, "ghostty-like-but-unknown-xyz")
    assert seen == "xterm-256color", \
        "an unknown TERM must be replaced with one every node has"


def test_a_terminal_the_node_does_know_is_left_alone(tmp_path):
    """Rewriting a good TERM would throw away colour and key handling for nothing."""
    seen, _, _, _ = run_snippet_recording_term(tmp_path, "xterm-256color")
    assert seen == "xterm-256color"
    seen, _, _, _ = run_snippet_recording_term(tmp_path, "screen-256color")
    assert seen == "screen-256color", "a known TERM must survive untouched"


def test_a_tmux_that_will_not_start_leaves_the_owner_a_shell(tmp_path):
    """The old exec turned any tmux failure into a disconnect."""
    _, survived, _, _ = run_snippet_recording_term(tmp_path, "xterm-256color", tmux_exit=1)
    assert survived, "a failed tmux must fall through to a normal shell, not end the login"


def test_a_successful_tmux_still_ends_the_login(tmp_path):
    """Leaving the session should log you out, as it did before."""
    _, survived, _, _ = run_snippet_recording_term(tmp_path, "xterm-256color", tmux_exit=0)
    assert not survived, "on success the shell must exit rather than drop to a prompt"


def test_an_unset_terminal_is_replaced_rather_than_passed_on_empty(tmp_path):
    """infocmp treats an unset TERM as "dumb" and succeeds, but tmux gets nothing."""
    seen, _, _, _ = run_snippet_recording_term(tmp_path, None)
    assert seen == "xterm-256color"


def test_an_option_shaped_terminal_cannot_reach_infocmp(tmp_path):
    """TERM arrives from the ssh client, so it is not ours to trust.

    `TERM=-V` makes infocmp print its version and exit 0, so the check would
    wave through a value tmux cannot use.
    """
    for hostile in ("-V", "-x", "--help"):
        seen, _, _, _ = run_snippet_recording_term(tmp_path, hostile)
        assert seen == "xterm-256color", f"{hostile} must never be passed through"


def test_without_infocmp_a_failed_tmux_is_retried_with_a_safe_terminal(tmp_path):
    """Where the terminal cannot be checked in advance, try, then try safely."""
    _, survived, calls, _ = run_snippet_recording_term(
        tmp_path, "ghostty-like-but-unknown-xyz", tmux_exit=1, hide_infocmp=True)
    assert calls == ["ghostty-like-but-unknown-xyz", "xterm-256color"], \
        "it should attempt the real terminal, then fall back once"
    assert survived, "and still leave a shell when both attempts fail"


def test_the_retry_does_not_repeat_a_terminal_that_already_failed(tmp_path):
    """A second identical attempt would only produce a second identical error."""
    _, _, calls, _ = run_snippet_recording_term(
        tmp_path, "xterm-256color", tmux_exit=1, hide_infocmp=True)
    assert calls == ["xterm-256color"], "no point retrying the fallback with itself"


def test_a_failed_retry_hands_back_the_owners_own_terminal(tmp_path):
    """If tmux fails for some reason other than the terminal, the speculative
    downgrade must not be left behind to degrade the rest of the session."""
    _, survived, calls, left_with = run_snippet_recording_term(
        tmp_path, "screen-256color", tmux_exit=1)
    assert calls == ["screen-256color", "xterm-256color"], "it should try both"
    assert survived
    assert left_with == "screen-256color", \
        "the shell must keep the terminal the owner actually has"


def test_a_terminal_the_node_cannot_use_is_not_handed_back(tmp_path):
    """Unlike the retry, replacing an unusable TERM is permanent and should be."""
    _, survived, _, left_with = run_snippet_recording_term(
        tmp_path, "ghostty-like-but-unknown-xyz", tmux_exit=1)
    assert survived
    assert left_with == "xterm-256color", \
        "handing back a terminal the node has no terminfo for would break the shell too"
