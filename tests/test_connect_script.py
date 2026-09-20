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
