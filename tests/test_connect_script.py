"""ccfleet-connect: wiring a device to a token, and unwiring it again.

The script handles a live credential, so the tests that matter are the ones
about what it writes and what it prints. A fake `claude` on PATH stands in for
the real one, which keeps these hermetic and lets the refusal path be exercised
without a revoked token to hand.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "laptop" / "ccfleet-connect.sh"
GOOD_TOKEN = "sk-ant-oat01-" + "A" * 80
LOGGED_IN = '{"loggedIn": true, "authMethod": "oauth_token"}'


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".config" / "ccfleet").mkdir(parents=True)
    (tmp_path / ".zshrc").write_text("# the user's own line\n")
    return tmp_path


def fake_claude(tmp_path, output=LOGGED_IN):
    """A `claude` on PATH that prints whatever auth status we want to test."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    binary = bindir / "claude"
    binary.write_text(f'#!/bin/sh\ncat <<\'EOF\'\n{output}\nEOF\n')
    binary.chmod(0o755)
    return bindir


def run(home, tmp_path, *args, token_env=None, claude_output=LOGGED_IN):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "SHELL": "/bin/zsh",
        "CCFLEET_TOKEN_FILE": str(home / ".config" / "ccfleet" / "token"),
        "PATH": f"{fake_claude(tmp_path, claude_output)}:{env['PATH']}",
    })
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if token_env is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token_env
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=60)


def token_path(home):
    return home / ".config" / "ccfleet" / "token"


def test_a_token_that_is_not_one_is_refused_before_anything_is_written(home, tmp_path):
    result = run(home, tmp_path, "not-a-token")
    assert result.returncode != 0
    assert "does not look like" in result.stderr
    assert not token_path(home).exists(), "nothing may be written before validation"


def test_a_rejected_token_is_never_persisted(home, tmp_path):
    """The CLI is the judge. If it will not accept the token, it does not land."""
    result = run(home, tmp_path, GOOD_TOKEN, claude_output='{"loggedIn": false}')
    assert result.returncode != 0
    assert "refused" in result.stderr
    assert not token_path(home).exists()
    assert "ccfleet connect" not in (home / ".zshrc").read_text()


def test_connect_writes_a_private_file_and_a_guarded_block(home, tmp_path):
    result = run(home, tmp_path, GOOD_TOKEN)
    assert result.returncode == 0, result.stderr

    path = token_path(home)
    assert path.read_text().strip() == GOOD_TOKEN
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, must be 0600"

    rc = (home / ".zshrc").read_text()
    assert "# the user's own line" in rc, "the user's own rc content must survive"
    assert rc.count(">>> ccfleet connect >>>") == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN" in rc
    # The rc must reference the file, never contain the credential itself.
    assert GOOD_TOKEN not in rc


def test_connecting_twice_does_not_stack_blocks(home, tmp_path):
    run(home, tmp_path, GOOD_TOKEN)
    run(home, tmp_path, GOOD_TOKEN)
    rc = (home / ".zshrc").read_text()
    assert rc.count(">>> ccfleet connect >>>") == 1
    assert rc.count("<<< ccfleet connect <<<") == 1


def test_status_never_prints_the_token(home, tmp_path):
    """`${VAR:-default}` expands to the VALUE when set, so the obvious one-liner
    here printed the whole credential. Only the branch with the variable set
    could show it, which is exactly the branch a happy-path test misses."""
    run(home, tmp_path, GOOD_TOKEN)
    for env_token in (None, GOOD_TOKEN):
        result = run(home, tmp_path, "--status", token_env=env_token)
        assert result.returncode == 0
        blob = result.stdout + result.stderr
        assert GOOD_TOKEN not in blob, f"token leaked with env set={env_token is not None}"
    # And it still reports the useful facts.
    said = run(home, tmp_path, "--status", token_env=GOOD_TOKEN).stdout
    assert "in shell   : yes" in said
    assert "oauth_token" in said
    assert "Remote Control is not available" in said


def test_status_on_a_device_that_was_never_connected(home, tmp_path):
    result = run(home, tmp_path, "--status")
    assert result.returncode == 0
    assert "not connected" in result.stdout
    # Same trap in this branch: it must not print the value either.
    leaked = run(home, tmp_path, "--status", token_env=GOOD_TOKEN)
    assert GOOD_TOKEN not in leaked.stdout + leaked.stderr


def test_remove_puts_the_device_back(home, tmp_path):
    run(home, tmp_path, GOOD_TOKEN)
    result = run(home, tmp_path, "--remove")
    assert result.returncode == 0
    assert not token_path(home).exists()
    rc = (home / ".zshrc").read_text()
    assert "ccfleet connect" not in rc
    assert "# the user's own line" in rc, "removal must not eat the user's rc"


def test_remove_is_safe_when_nothing_was_connected(home, tmp_path):
    result = run(home, tmp_path, "--remove")
    assert result.returncode == 0
    assert "# the user's own line" in (home / ".zshrc").read_text()


def test_help_states_the_one_thing_a_token_cannot_do(home, tmp_path):
    out = run(home, tmp_path, "--help").stdout
    assert "Remote Control" in out
    assert "setup-token" in out


# The sentinel lives under tmp_path so the test cannot collide with a
# leftover file from anything else on the machine.
HOSTILE_FMT = "weird home; touch {sentinel} $USER"


def test_a_path_with_spaces_and_metacharacters_is_quoted(tmp_path):
    """The generated line lands in a file every new shell sources. An unquoted
    path with a space breaks the shell; one with a metacharacter runs it."""
    sentinel = tmp_path / "pwned"
    home = tmp_path / HOSTILE_FMT.format(sentinel=sentinel)
    (home / ".config" / "ccfleet").mkdir(parents=True)
    (home / ".zshrc").write_text("# the user's own line\n")

    result = run(home, tmp_path, GOOD_TOKEN)
    assert result.returncode == 0, result.stderr

    line = [ln for ln in (home / ".zshrc").read_text().splitlines()
            if "CLAUDE_CODE_OAUTH_TOKEN" in ln][0]
    # The whole path sits inside single quotes, so none of it is interpreted.
    assert "'" in line and "; touch" not in line.replace(f"'{home}", "")
    assert str(home) in line

    # And a real shell sourcing it loads the token rather than choking.
    probe = subprocess.run(
        ["/bin/sh", "-c", f". '{home}/.zshrc'; printf '%s' \"${{CLAUDE_CODE_OAUTH_TOKEN:+set}}\""],
        capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == "set"
    assert not sentinel.exists(), "the path was executed, not quoted"


def test_a_path_containing_a_quote_is_still_safe(tmp_path):
    home = tmp_path / "it's here"
    (home / ".config" / "ccfleet").mkdir(parents=True)
    (home / ".zshrc").write_text("# keep me\n")
    assert run(home, tmp_path, GOOD_TOKEN).returncode == 0
    probe = subprocess.run(
        ["/bin/sh", "-c", f". \"{home}/.zshrc\"; printf '%s' \"${{CLAUDE_CODE_OAUTH_TOKEN:+set}}\""],
        capture_output=True, text=True, timeout=30)
    assert probe.stdout == "set", probe.stderr


def test_the_rc_files_permissions_are_preserved(home, tmp_path):
    """Rewriting via a temp file gives it default permissions, so a 0600 rc
    would quietly widen to 0644 — and people keep secrets in their rc."""
    rc = home / ".zshrc"
    rc.chmod(0o600)
    assert run(home, tmp_path, GOOD_TOKEN).returncode == 0
    assert stat.S_IMODE(rc.stat().st_mode) == 0o600, "connect widened the rc"
    assert run(home, tmp_path, "--remove").returncode == 0
    assert stat.S_IMODE(rc.stat().st_mode) == 0o600, "remove widened the rc"


def test_a_shell_whose_config_directory_does_not_exist_yet(tmp_path):
    """fish keeps its config under ~/.config/fish, which may not exist. Failing
    there would leave the token written and the device half-connected."""
    home = tmp_path / "fishhome"
    (home / ".config" / "ccfleet").mkdir(parents=True)
    env_overrides = {"SHELL": "/usr/local/bin/fish"}

    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CCFLEET_TOKEN_FILE": str(home / ".config" / "ccfleet" / "token"),
        "PATH": f"{fake_claude(tmp_path)}:{env['PATH']}",
        **env_overrides,
    })
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    result = subprocess.run([str(SCRIPT), GOOD_TOKEN], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    config = home / ".config" / "fish" / "config.fish"
    assert config.exists(), "the fish config directory was not created"
    body = config.read_text()
    assert "set -gx CLAUDE_CODE_OAUTH_TOKEN" in body
    assert GOOD_TOKEN not in body


def test_an_rc_with_no_trailing_newline_is_not_corrupted(home, tmp_path):
    """The invariant: the marker owns its own line whatever the rc looked like.

    Note this passes with or without the explicit newline guard, because
    strip_block rewrites through awk and awk terminates every line. The test is
    here for the invariant, not as a regression test for the guard.
    """
    rc = home / ".zshrc"
    rc.write_text("export PATH=/opt/bin:$PATH")          # deliberately no \n
    assert run(home, tmp_path, GOOD_TOKEN).returncode == 0

    lines = rc.read_text().splitlines()
    assert lines[0] == "export PATH=/opt/bin:$PATH", "the user's last line was mangled"
    assert "# >>> ccfleet connect >>>" in lines, "the marker must own its own line"

    # And because it owns a line, removal can find it again.
    assert run(home, tmp_path, "--remove").returncode == 0
    body = rc.read_text()
    assert "ccfleet connect" not in body
    assert "export PATH=/opt/bin:$PATH" in body


def test_an_unfinished_block_is_refused_not_truncated(home, tmp_path):
    """A begin marker with no end means an interrupted write. Stripping from
    there to EOF would delete everything the user wrote after it."""
    rc = home / ".zshrc"
    rc.write_text("# before\n# >>> ccfleet connect >>>\n"
                  "export SOMETHING_IMPORTANT=1\nalias deploy='make ship'\n")
    for args in (["--remove"], [GOOD_TOKEN]):
        result = run(home, tmp_path, *args)
        assert result.returncode != 0, f"{args} should refuse"
        assert "unfinished ccfleet block" in result.stderr
    body = rc.read_text()
    assert "SOMETHING_IMPORTANT" in body and "make ship" in body, "user content destroyed"
    # And the refusal must land before the credential does, or the device is
    # left with a token on disk and no rc line that uses it.
    assert not token_path(home).exists(), "token persisted despite the refusal"


def test_the_token_can_be_given_without_a_command_line(home, tmp_path):
    """On a command line the credential lands in shell history and in `ps`."""
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "SHELL": "/bin/zsh",
        "CCFLEET_TOKEN_FILE": str(token_path(home)),
        "PATH": f"{fake_claude(tmp_path)}:{env['PATH']}",
    })
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    result = subprocess.run([str(SCRIPT), "--stdin"], input=GOOD_TOKEN + "\n",
                            capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert token_path(home).read_text().strip() == GOOD_TOKEN


def test_the_printed_hint_quotes_the_path(tmp_path):
    home = tmp_path / "spaced home"
    (home / ".config" / "ccfleet").mkdir(parents=True)
    (home / ".zshrc").write_text("# keep\n")
    out = run(home, tmp_path, GOOD_TOKEN).stdout
    hint = [ln for ln in out.splitlines() if "export CLAUDE_CODE_OAUTH_TOKEN" in ln][0]
    # Copy-pasteable on a path with a space means the path must be quoted.
    assert "'" in hint and str(home) in hint


def test_bash_gets_the_file_an_interactive_terminal_reads(tmp_path):
    """An ordinary terminal starts a non-login bash, which reads .bashrc.
    Writing only to .bash_profile means new terminals never see the token."""
    home = tmp_path / "bashhome"
    (home / ".config" / "ccfleet").mkdir(parents=True)
    (home / ".bash_profile").write_text("# login only\n")
    (home / ".bashrc").write_text("# interactive\n")
    env = dict(os.environ)
    env.update({"HOME": str(home), "SHELL": "/bin/bash",
                "CCFLEET_TOKEN_FILE": str(home / ".config" / "ccfleet" / "token"),
                "PATH": f"{fake_claude(tmp_path)}:{env['PATH']}"})
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    assert subprocess.run([str(SCRIPT), GOOD_TOKEN], capture_output=True, text=True,
                          env=env, timeout=60).returncode == 0
    assert "CLAUDE_CODE_OAUTH_TOKEN" in (home / ".bashrc").read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in (home / ".bash_profile").read_text()


def run_piped(home, tmp_path, stdin, *, claude_output=LOGGED_IN, curl=None):
    """Run the way the documented one-liner does: through `bash -c`, with the
    script's text as the command rather than a file.

    This is the path that matters and the one the other tests miss — with no
    readable `$0`, install_self takes its download branch, which is the branch
    the published command relies on.
    """
    env = dict(os.environ)
    bindir = fake_claude(tmp_path, claude_output)
    if curl is not None:
        fake = bindir / "curl"
        fake.write_text(curl)
        fake.chmod(0o755)
    env.update({
        "HOME": str(home),
        "SHELL": "/bin/zsh",
        "CCFLEET_TOKEN_FILE": str(home / ".config" / "ccfleet" / "token"),
        "PATH": f"{bindir}:{env['PATH']}",
    })
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return subprocess.run(["bash", "-c", SCRIPT.read_text()], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60)


def test_the_published_one_liner_leaves_the_command_behind(home, tmp_path):
    """The install path the one-liner actually uses: no file to copy, so it
    downloads. Every other test runs the local script and exercises the copy
    branch instead, which is not the branch people will hit."""
    downloaded = "#!/usr/bin/env bash\n# ccfleet-connect, freshly downloaded\n"
    curl = ('#!/bin/sh\n'
            '# stand in for curl: the last argument after -o is the destination\n'
            'out=""\n'
            'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { out="$2"; shift; }; shift; done\n'
            f'[ -n "$out" ] && printf %s {downloaded!r} > "$out"\n')
    result = run_piped(home, tmp_path, GOOD_TOKEN + "\n", curl=curl)
    dest = home / ".local" / "bin" / "ccfleet-connect"
    assert dest.exists(), "the one-liner must leave the command behind"
    assert "freshly downloaded" in dest.read_text(), "and it came from the download branch"
    assert dest.stat().st_mode & stat.S_IXUSR
    assert "installed ccfleet-connect" in result.stdout


def test_a_failed_install_says_so_rather_than_claiming_success(home, tmp_path):
    """Silently carrying on leaves someone typing a command that is not there."""
    curl = '#!/bin/sh\nexit 22\n'          # as curl does for an HTTP error
    result = run_piped(home, tmp_path, GOOD_TOKEN + "\n", curl=curl)
    assert not (home / ".local" / "bin" / "ccfleet-connect").exists()
    assert "was not installed" in result.stdout
    assert "could not download" in result.stdout
    # The token is still set up: a missing convenience is not a failed connect.
    assert token_path(home).exists()
    assert result.returncode == 0


def test_nothing_is_installed_for_a_token_that_is_refused(home, tmp_path):
    """The guidebook says it checks the token before writing anything. That has
    to cover the executable too, or the sentence is not true."""
    result = run_piped(home, tmp_path, "not-a-token\n")
    assert result.returncode != 0
    assert not (home / ".local" / "bin" / "ccfleet-connect").exists()
    assert not token_path(home).exists()


def test_it_does_not_overwrite_a_copy_already_there(home, tmp_path):
    """Someone may have their own, edited or newer. Installing is a courtesy,
    not a claim on the path."""
    dest = home / ".local" / "bin" / "ccfleet-connect"
    dest.parent.mkdir(parents=True)
    dest.write_text("#!/bin/sh\n# their own copy\n")
    dest.chmod(0o755)
    run(home, tmp_path, GOOD_TOKEN)
    assert "their own copy" in dest.read_text(), "left alone"
