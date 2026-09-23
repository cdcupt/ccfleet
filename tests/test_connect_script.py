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


def fake_claude(tmp_path, output=LOGGED_IN, script=None):
    """A `claude` on PATH that prints whatever auth status we want to test,
    or runs `script` when a test needs a verdict per token."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    binary = bindir / "claude"
    binary.write_text(script or f'#!/bin/sh\ncat <<\'EOF\'\n{output}\nEOF\n')
    binary.chmod(0o755)
    return bindir


def run(home, tmp_path, *args, token_env=None, claude_output=LOGGED_IN,
        claude_script=None, stdin=None):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "SHELL": "/bin/zsh",
        "CCFLEET_TOKEN_FILE": str(home / ".config" / "ccfleet" / "token"),
        "PATH": f"{fake_claude(tmp_path, claude_output, claude_script)}:{env['PATH']}",
    })
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if token_env is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token_env
    extra = {"input": stdin} if stdin is not None else {}
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=60, **extra)


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
    run(home, tmp_path, GOOD_TOKEN)
    hint = [ln for ln in (home / ".zshrc").read_text().splitlines()
             if "CLAUDE_CODE_OAUTH_TOKEN" in ln][0]
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


def test_it_hands_over_a_shell_that_already_has_the_token(home, tmp_path):
    """A process cannot put a variable into the shell that started it — that is
    what a child process is. So the end of setup is either an export command
    somebody types by hand, or a fresh shell that has already read the rc line.
    """
    bindir = fake_claude(tmp_path)
    # Stand in for the user's shell: prove what it inherited and exit.
    shell = bindir / "zsh"
    # Reports what a real shell would find waiting for it: the export line,
    # which is how the token reaches every later shell too.
    shell.write_text('#!/bin/sh\nexec printf "SHELL-STARTED exports=%s\\n" '
                     '"$(grep -c CLAUDE_CODE_OAUTH_TOKEN "$HOME/.zshrc")"\n')
    shell.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "HOME": str(home), "SHELL": str(shell),
        "CCFLEET_TOKEN_FILE": str(token_path(home)),
        "PATH": f"{bindir}:{env['PATH']}",
    })
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    # A pty so the script sees a terminal on stdout, which is the condition for
    # handing one over at all.
    import pty
    pid, fd = pty.fork()
    if pid == 0:
        os.execve("/bin/bash", ["bash", str(SCRIPT), GOOD_TOKEN], env)
    out = b""
    try:
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            out += chunk
    except OSError:
        pass
    os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert "SHELL-STARTED" in text, "it handed over a shell rather than printing advice"
    assert "exports=1" in text, "and the export it wrote is there for that shell to read"


def test_a_scripted_run_keeps_its_own_shell(home, tmp_path):
    """Replacing the shell is the right end to an interactive setup and the
    wrong one inside somebody's provisioning run."""
    result = run(home, tmp_path, "--no-exec", GOOD_TOKEN)
    assert result.returncode == 0
    assert token_path(home).exists(), "still connected"
    assert 'exec "$SHELL"' in result.stdout, "it says how, rather than doing it"


def test_with_no_terminal_it_explains_instead_of_taking_over(home, tmp_path):
    """run() captures output, so there is no terminal to hand over. Exec-ing a
    shell into a pipe would hang whatever called it."""
    result = run(home, tmp_path, GOOD_TOKEN)
    assert result.returncode == 0
    assert 'exec "$SHELL"' in result.stdout


# -- several accounts: tokens saved under names ----------------------------------------

WORK = "sk-ant-oat01-" + "W" * 80
PERSONAL = "sk-ant-oat01-PRO" + "P" * 77
REVOKED = "sk-ant-oat01-REVOKED" + "R" * 70
ALL_TOKENS = (GOOD_TOKEN, WORK, PERSONAL, REVOKED)

# A claude that judges each token on its own: one containing REVOKED is refused,
# one containing PRO is on the Pro plan, and the rest are on Max.
PER_TOKEN = """#!/bin/sh
case "$CLAUDE_CODE_OAUTH_TOKEN" in
  *REVOKED*) printf '{"loggedIn": false}\\n' ;;
  *PRO*) printf '{"loggedIn": true, "authMethod": "oauth_token", "subscriptionType": "pro"}\\n' ;;
  *) printf '{"loggedIn": true, "authMethod": "oauth_token", "subscriptionType": "max"}\\n' ;;
esac
"""


def tokens_dir(home):
    return home / ".config" / "ccfleet" / "tokens"


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def add(home, tmp_path, name, token, **kw):
    kw.setdefault("claude_script", PER_TOKEN)
    return run(home, tmp_path, "--add", name, stdin=token + "\n", **kw)


def in_use(home):
    return token_path(home).read_text().strip()


def list_rows(stdout):
    """--list's rows by name. A row is '  * name  verdict' for the token in
    use and '    name  verdict' for the rest; notes start with a word."""
    rows = {}
    for line in stdout.splitlines():
        if len(line) > 4 and line[:2] == "  " and line[2] in "* " and line[3] == " ":
            rows[line[4:].split()[0]] = line
    return rows


def test_add_saves_the_token_under_its_name_and_puts_it_in_use(home, tmp_path):
    result = add(home, tmp_path, "work", WORK)
    assert result.returncode == 0, result.stderr
    saved = tokens_dir(home) / "work"
    assert saved.read_text().strip() == WORK
    assert in_use(home) == WORK
    assert mode(tokens_dir(home)) == 0o700
    assert mode(saved) == 0o600 and mode(token_path(home)) == 0o600
    rc = (home / ".zshrc").read_text()
    assert rc.count(">>> ccfleet connect >>>") == 1 and WORK not in rc
    assert WORK not in result.stdout + result.stderr


def test_add_reads_the_token_from_stdin_with_or_without_the_flag(home, tmp_path):
    flagged = run(home, tmp_path, "--add", "work", "--stdin", stdin=WORK + "\n",
                  claude_script=PER_TOKEN)
    assert flagged.returncode == 0, flagged.stderr
    assert add(home, tmp_path, "personal", PERSONAL).returncode == 0
    assert (tokens_dir(home) / "work").read_text().strip() == WORK
    assert (tokens_dir(home) / "personal").read_text().strip() == PERSONAL


def test_a_token_without_a_name_is_saved_as_default(home, tmp_path):
    assert run(home, tmp_path, GOOD_TOKEN).returncode == 0
    assert (tokens_dir(home) / "default").read_text().strip() == GOOD_TOKEN
    assert in_use(home) == GOOD_TOKEN


def test_use_switches_the_device_to_another_account(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)
    assert in_use(home) == PERSONAL, "adding an account switches to it"
    rc = home / ".zshrc"
    # A line of theirs after the block: a switch that rewrote the rc would move
    # the block below it.
    rc.write_text(rc.read_text() + "# a later line of theirs\n")
    before = rc.read_text()

    result = run(home, tmp_path, "--use", "work", claude_script=PER_TOKEN)
    assert result.returncode == 0, result.stderr
    assert in_use(home) == WORK
    assert mode(token_path(home)) == 0o600
    assert rc.read_text() == before, "switching must leave the rc exactly as it was"
    assert 'exec "$SHELL"' in result.stdout, "and it says how to get it in this shell"
    for token in ALL_TOKENS:
        assert token not in result.stdout + result.stderr


def test_a_switch_replaces_the_file_rather_than_writing_into_it(home, tmp_path):
    """A shell starting mid-switch must read one whole token or the other. A
    rename gives that and shows as a new inode; writing into the file in place
    keeps the inode, and has a moment where the file holds half a token."""
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)
    before = token_path(home).stat().st_ino
    assert run(home, tmp_path, "--use", "work", claude_script=PER_TOKEN).returncode == 0
    assert token_path(home).stat().st_ino != before


def test_a_switch_leaves_the_file_in_use_private(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)
    token_path(home).chmod(0o644)           # widened by something else
    assert run(home, tmp_path, "--use", "work", claude_script=PER_TOKEN).returncode == 0
    assert mode(token_path(home)) == 0o600


def test_a_token_that_no_longer_works_is_not_switched_to(home, tmp_path):
    """The CLI judges a token before it goes into use, on a switch as on the
    first connect: a revoked one leaves the device on the account it was on."""
    add(home, tmp_path, "work", WORK)
    old = tokens_dir(home) / "old"
    old.write_text(REVOKED + "\n")
    old.chmod(0o600)
    result = run(home, tmp_path, "--use", "old", claude_script=PER_TOKEN)
    assert result.returncode != 0
    assert "refused" in result.stderr and "Still using 'work'" in result.stderr
    assert in_use(home) == WORK


def test_switching_to_a_name_never_saved_changes_nothing(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    result = run(home, tmp_path, "--use", "nope", claude_script=PER_TOKEN)
    assert result.returncode != 0
    assert "no token saved as 'nope'" in result.stderr
    assert in_use(home) == WORK


def test_a_switch_puts_back_an_rc_line_that_was_taken_out(home, tmp_path):
    """Switching only works if new shells read the file in use."""
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)
    (home / ".zshrc").write_text("# the user's own line\n")
    assert run(home, tmp_path, "--use", "work", claude_script=PER_TOKEN).returncode == 0
    rc = (home / ".zshrc").read_text()
    assert rc.count(">>> ccfleet connect >>>") == 1
    assert "# the user's own line" in rc


@pytest.mark.parametrize("name", ["Work", "../evil", "a/b", "-x", "a_b", "a.b", "..",
                                  "café", "a b", "x" * 33, ""])
def test_a_bad_name_is_refused_before_anything_happens(home, tmp_path, name):
    """A name becomes a path. Nothing is asked for and nothing is written."""
    result = run(home, tmp_path, "--add", name, stdin=WORK + "\n", claude_script=PER_TOKEN)
    assert result.returncode != 0
    assert "token name" in result.stderr
    assert not token_path(home).exists()
    assert not tokens_dir(home).exists()
    assert not (home / ".config" / "evil").exists()
    assert not (home / ".config" / "ccfleet" / "evil").exists()
    assert "ccfleet connect" not in (home / ".zshrc").read_text()


def test_names_at_the_edges_of_the_rule_are_fine(home, tmp_path):
    for name in ("x" * 32, "0", "work-2"):
        assert add(home, tmp_path, name, WORK).returncode == 0, name
        assert (tokens_dir(home) / name).read_text().strip() == WORK


def test_a_bad_name_is_refused_by_every_command_that_takes_one(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    for args in (["--use", "../work"], ["--remove", "../tokens/work"], ["--use", "Work"]):
        result = run(home, tmp_path, *args, claude_script=PER_TOKEN)
        assert result.returncode != 0 and "token name" in result.stderr, args
    assert in_use(home) == WORK and (tokens_dir(home) / "work").exists()


def test_list_marks_the_one_in_use_and_never_prints_a_token(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)          # in use
    old = tokens_dir(home) / "old"
    old.write_text(REVOKED + "\n")
    old.chmod(0o600)
    for env_token in (None, WORK):
        result = run(home, tmp_path, "--list", token_env=env_token, claude_script=PER_TOKEN)
        assert result.returncode == 0, result.stderr
        for token in ALL_TOKENS:
            assert token not in result.stdout + result.stderr
    rows = list_rows(result.stdout)
    assert set(rows) == {"work", "personal", "old"}
    assert rows["personal"][2] == "*", "the one in use is marked"
    assert rows["work"][2] == " " and rows["old"][2] == " "
    assert "pro plan" in rows["personal"] and "max plan" in rows["work"]
    assert "REFUSED" in rows["old"]


def test_list_says_when_nothing_is_saved(home, tmp_path):
    result = run(home, tmp_path, "--list")
    assert result.returncode == 0
    assert "no saved tokens" in result.stdout


def test_status_names_the_token_in_use_and_spots_a_stale_shell(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)
    run(home, tmp_path, "--use", "work", claude_script=PER_TOKEN)
    said = run(home, tmp_path, "--status", token_env=WORK, claude_script=PER_TOKEN)
    assert "token name : work" in said.stdout
    assert "in shell   : yes" in said.stdout
    # A terminal opened before the switch still holds the other account.
    stale = run(home, tmp_path, "--status", token_env=PERSONAL, claude_script=PER_TOKEN)
    assert "open a new terminal" in stale.stdout
    assert "in shell   : yes" not in stale.stdout
    for result in (said, stale):
        for token in ALL_TOKENS:
            assert token not in result.stdout + result.stderr


def test_removing_the_token_in_use_takes_it_out_of_use(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)          # in use
    result = run(home, tmp_path, "--remove", "personal")
    assert result.returncode == 0, result.stderr
    assert not (tokens_dir(home) / "personal").exists()
    assert not token_path(home).exists(), "a removed token must not stay in use"
    assert (tokens_dir(home) / "work").read_text().strip() == WORK
    assert "--use NAME" in result.stdout
    status = run(home, tmp_path, "--status", claude_script=PER_TOKEN).stdout
    assert "not connected" in status and "saved tokens: work." in status


def test_removing_one_not_in_use_leaves_the_device_as_it_was(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)          # in use
    assert run(home, tmp_path, "--remove", "work").returncode == 0
    assert not (tokens_dir(home) / "work").exists()
    assert in_use(home) == PERSONAL


def test_removing_a_name_that_shares_the_token_in_use_keeps_it_in_use(home, tmp_path):
    """The same token under two names: removing one leaves it saved, and in use."""
    add(home, tmp_path, "a", WORK)
    add(home, tmp_path, "b", WORK)
    assert run(home, tmp_path, "--remove", "a").returncode == 0
    assert in_use(home) == WORK


def test_remove_with_no_name_undoes_everything(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)
    leftover = tokens_dir(home) / ".ccfleet-token.abc123"   # an interrupted write
    leftover.write_text(WORK + "\n")
    result = run(home, tmp_path, "--remove")
    assert result.returncode == 0, result.stderr
    assert not token_path(home).exists()
    assert not tokens_dir(home).exists(), "saved tokens must not outlive an undo"
    assert "ccfleet connect" not in (home / ".zshrc").read_text()


def test_a_token_from_before_names_is_kept_as_default(home, tmp_path):
    """A device connected by the previous version has one token and no saved
    ones. The first run of this one keeps it as default, so switching away from
    it cannot be how it is lost."""
    token_path(home).write_text(GOOD_TOKEN + "\n")
    token_path(home).chmod(0o600)
    result = run(home, tmp_path, "--list", claude_script=PER_TOKEN)
    assert result.returncode == 0, result.stderr
    kept = tokens_dir(home) / "default"
    assert kept.read_text().strip() == GOOD_TOKEN
    assert mode(tokens_dir(home)) == 0o700 and mode(kept) == 0o600
    assert list_rows(result.stdout)["default"][2] == "*"
    add(home, tmp_path, "work", WORK)
    assert run(home, tmp_path, "--use", "default", claude_script=PER_TOKEN).returncode == 0
    assert in_use(home) == GOOD_TOKEN


def test_a_refused_token_leaves_a_device_with_an_old_token_exactly_as_it_was(home, tmp_path):
    """Keeping the old token as default is a write. It must not happen for a
    token that is then refused."""
    token_path(home).write_text(GOOD_TOKEN + "\n")
    result = add(home, tmp_path, "work", REVOKED)
    assert result.returncode != 0
    assert not tokens_dir(home).exists()
    assert in_use(home) == GOOD_TOKEN


def test_the_tokens_directory_is_made_private_even_if_it_was_not(home, tmp_path):
    tokens_dir(home).mkdir()
    tokens_dir(home).chmod(0o755)
    assert add(home, tmp_path, "work", WORK).returncode == 0
    assert mode(tokens_dir(home)) == 0o700


def test_extra_arguments_are_refused(home, tmp_path):
    add(home, tmp_path, "work", WORK)
    for args in (["--use", "work", "extra"], ["--use"], ["--remove", "a", "b"]):
        result = run(home, tmp_path, *args, claude_script=PER_TOKEN, stdin="")
        assert result.returncode != 0 and "usage" in result.stderr, args
    assert in_use(home) == WORK


def test_a_token_on_the_add_command_line_is_refused_not_ignored(home, tmp_path):
    """On a command line a credential lands in shell history and in `ps`. The
    new commands never take one there, and say so rather than quietly reading
    another token from stdin."""
    result = run(home, tmp_path, "--add", "personal", PERSONAL, stdin=PERSONAL + "\n",
                 claude_script=PER_TOKEN)
    assert result.returncode != 0
    assert "usage" in result.stderr
    assert not tokens_dir(home).exists() and not token_path(home).exists()


def test_removing_a_saved_token_leaves_an_unsaved_one_in_use_alone(home, tmp_path):
    """The token in use need not be one of the saved ones: somebody may have put
    it there by hand. Removing a saved token must not take out a different one."""
    add(home, tmp_path, "work", WORK)
    add(home, tmp_path, "personal", PERSONAL)
    token_path(home).write_text(GOOD_TOKEN + "\n")
    assert run(home, tmp_path, "--remove", "work").returncode == 0
    assert in_use(home) == GOOD_TOKEN
