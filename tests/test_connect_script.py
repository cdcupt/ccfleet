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


# -- one computer, one Claude account -------------------------------------------------

WORK = "sk-ant-oat01-" + "W" * 80
PERSONAL = "sk-ant-oat01-PRO" + "P" * 77
REVOKED = "sk-ant-oat01-REVOKED" + "R" * 70
ALL_TOKENS = (GOOD_TOKEN, WORK, PERSONAL, REVOKED)

# A claude that judges each token on its own: one containing REVOKED is refused.
PER_TOKEN = """#!/bin/sh
case "$CLAUDE_CODE_OAUTH_TOKEN" in
  *REVOKED*) printf '{"loggedIn": false}\\n' ;;
  *) printf '{"loggedIn": true, "authMethod": "oauth_token"}\\n' ;;
esac
"""


def tokens_dir(home):
    """Where the version that kept several accounts saved them under names."""
    return home / ".config" / "ccfleet" / "tokens"


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def in_use(home):
    return token_path(home).read_text().strip()


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)


def legacy_device(home, *, in_use_token=PERSONAL,
                  saved=(("default", GOOD_TOKEN), ("work", WORK), ("personal", PERSONAL)),
                  interrupted=True):
    """A computer set up by the version that saved a token per account: a 0700
    tokens directory, a 0600 file per name, and the one in use copied over the
    token file. `interrupted` adds the temp file a write cut short left behind."""
    tokens_dir(home).mkdir()
    tokens_dir(home).chmod(0o700)
    for name, token in saved:
        private(tokens_dir(home) / name, token + "\n")
    if interrupted:
        private(tokens_dir(home) / ".ccfleet-token.abc123", WORK + "\n")
    if in_use_token is not None:
        private(token_path(home), in_use_token + "\n")


def leaked(result):
    blob = result.stdout + result.stderr
    return [t for t in ALL_TOKENS if t in blob]


def snapshot(home):
    """Everything a refused command must leave exactly as it was."""
    files = {}
    for path in [token_path(home), *sorted(tokens_dir(home).glob("*")),
                 *sorted(tokens_dir(home).glob(".*"))]:
        if path.exists():
            files[path.name] = (path.read_text(), mode(path), path.stat().st_ino)
    return files, (home / ".zshrc").read_text()


@pytest.mark.parametrize("args", [["--add", "work"], ["--use", "work"], ["--list"], ["-l"],
                                  ["--no-exec", "--use", "work"], ["--add", "work", "--stdin"]])
def test_switching_between_accounts_is_refused_and_changes_nothing(home, tmp_path, args):
    """A computer uses one Claude account. What used to keep several and switch
    between them says so, and says what to do instead, before touching a thing:
    not even the saved tokens it would otherwise retire."""
    legacy_device(home)
    before = snapshot(home)
    result = run(home, tmp_path, *args, stdin=WORK + "\n", claude_script=PER_TOKEN)
    assert result.returncode != 0
    assert "one Claude account" in result.stderr
    assert "replaces the one in use" in result.stderr, "and it says what to do instead"
    assert snapshot(home) == before
    assert not leaked(result)


def test_remove_with_a_name_is_refused_and_changes_nothing(home, tmp_path):
    legacy_device(home)
    before = snapshot(home)
    result = run(home, tmp_path, "--remove", "work")
    assert result.returncode != 0
    assert "takes no name" in result.stderr and "one Claude account" in result.stderr
    assert snapshot(home) == before
    assert not leaked(result)


def test_saved_tokens_are_retired_keeping_the_one_in_use(home, tmp_path):
    """The first run of this version on a computer that kept several accounts:
    the token in use stays exactly as it was, and the others go."""
    legacy_device(home)                     # personal is in use
    kept = token_path(home)
    ino = kept.stat().st_ino
    result = run(home, tmp_path, "--status", claude_script=PER_TOKEN)
    assert result.returncode == 0, result.stderr
    assert in_use(home) == PERSONAL
    assert mode(kept) == 0o600 and kept.stat().st_ino == ino, "not rewritten, not widened"
    assert not tokens_dir(home).exists(), "the saved tokens are gone, temp file and all"
    # default and work were other accounts; personal was the one in use, and the
    # interrupted write was a copy, not an account.
    assert "removed 2 other saved token(s)" in result.stdout
    assert "one Claude account" in result.stdout and "Revoke them" in result.stdout
    assert not leaked(result)


def test_a_computer_that_saved_only_the_token_in_use_loses_nothing_it_would_miss(home,
                                                                                tmp_path):
    legacy_device(home, in_use_token=GOOD_TOKEN, saved=(("default", GOOD_TOKEN),),
                  interrupted=False)
    result = run(home, tmp_path, "--status", claude_script=PER_TOKEN)
    assert result.returncode == 0, result.stderr
    assert in_use(home) == GOOD_TOKEN
    assert not tokens_dir(home).exists()
    assert "removed" not in result.stdout, "nothing but a copy of the token in use went"


def test_with_no_token_in_use_every_saved_one_is_retired(home, tmp_path):
    """The earlier version could leave a computer with saved tokens and none in
    use. There is no account to keep, so none is kept."""
    legacy_device(home, in_use_token=None)
    result = run(home, tmp_path, "--status", claude_script=PER_TOKEN)
    assert result.returncode == 0, result.stderr
    assert not tokens_dir(home).exists() and not token_path(home).exists()
    assert "removed 3 other saved token(s)" in result.stdout
    assert "not connected" in result.stdout
    assert not leaked(result)


def test_retiring_leaves_anything_that_is_not_a_saved_token_alone(tmp_path):
    """The directory is found next to the token file, whose place can be moved.
    Moved into a home directory, "tokens" may be somebody's own folder: only
    what the earlier version wrote there is removed."""
    home = tmp_path / "home"
    (home / ".config" / "ccfleet").mkdir(parents=True)
    (home / ".zshrc").write_text("# the user's own line\n")
    theirs = home / "tokens"
    theirs.mkdir()
    keep = {
        "notes.txt": "my own notes\n",
        # A capital: not a name the script used. Not "Work": on a filesystem
        # that ignores case, that is the same file as the "work" below.
        "Personal": PERSONAL + "\n",
        "report": "quarterly figures\n",      # a valid name, but not a token
        "x" * 33: WORK + "\n",                # longer than a name could be
    }
    for name, text in keep.items():
        (theirs / name).write_text(text)
    elsewhere = tmp_path / "their-token"
    elsewhere.write_text(WORK + "\n")
    (theirs / "link").symlink_to(elsewhere)   # never follow a link into someone's file
    private(theirs / "work", WORK + "\n")     # this one does look like ours
    token = home / "token"
    private(token, PERSONAL + "\n")

    env = dict(os.environ)
    env.update({"HOME": str(home), "SHELL": "/bin/zsh", "CCFLEET_TOKEN_FILE": str(token),
                "PATH": f"{fake_claude(tmp_path, script=PER_TOKEN)}:{env['PATH']}"})
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    result = subprocess.run([str(SCRIPT), "--status"], capture_output=True, text=True,
                            env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    for name, text in keep.items():
        assert (theirs / name).read_text() == text, name
    assert (theirs / "link").is_symlink() and elsewhere.read_text() == WORK + "\n"
    assert not (theirs / "work").exists()
    assert token.read_text().strip() == PERSONAL
    assert not leaked(result)


def test_a_refused_token_does_not_retire_anything(home, tmp_path):
    """Connecting refuses before it writes, and retiring is a write."""
    legacy_device(home)
    before = snapshot(home)
    result = run(home, tmp_path, REVOKED, claude_script=PER_TOKEN)
    assert result.returncode != 0 and "refused" in result.stderr
    assert snapshot(home) == before


def test_connecting_on_a_computer_that_saved_several_keeps_only_the_new_one(home, tmp_path):
    legacy_device(home)
    result = run(home, tmp_path, "--no-exec", WORK, claude_script=PER_TOKEN)
    assert result.returncode == 0, result.stderr
    assert in_use(home) == WORK and mode(token_path(home)) == 0o600
    assert not tokens_dir(home).exists()
    assert "removed 2 other saved token(s)" in result.stdout
    assert "replaces the token this computer had" in result.stdout
    assert not leaked(result)


def test_connecting_again_replaces_the_token_and_says_so(home, tmp_path):
    assert run(home, tmp_path, GOOD_TOKEN, claude_script=PER_TOKEN).returncode == 0
    result = run(home, tmp_path, WORK, claude_script=PER_TOKEN)
    assert result.returncode == 0, result.stderr
    assert in_use(home) == WORK
    assert "replaces the token this computer had" in result.stdout
    assert not tokens_dir(home).exists(), "nothing is kept on the side to switch back to"
    assert not leaked(result)
    same = run(home, tmp_path, WORK, claude_script=PER_TOKEN)
    assert "replaces the token this computer had" not in same.stdout, \
        "the same token again replaces nothing"


def test_replacing_the_token_is_a_rename_not_a_rewrite(home, tmp_path):
    """A shell starting while the token is replaced must read one whole token or
    the other. A rename gives that and shows as a new inode; writing into the
    file in place keeps the inode, and has a moment where it holds half a token."""
    run(home, tmp_path, GOOD_TOKEN, claude_script=PER_TOKEN)
    before = token_path(home).stat().st_ino
    assert run(home, tmp_path, WORK, claude_script=PER_TOKEN).returncode == 0
    assert token_path(home).stat().st_ino != before


def test_a_widened_token_file_is_made_private_again(home, tmp_path):
    run(home, tmp_path, GOOD_TOKEN, claude_script=PER_TOKEN)
    token_path(home).chmod(0o644)           # widened by something else
    assert run(home, tmp_path, WORK, claude_script=PER_TOKEN).returncode == 0
    assert mode(token_path(home)) == 0o600


def test_status_spots_a_shell_that_still_holds_the_older_token(home, tmp_path):
    run(home, tmp_path, GOOD_TOKEN, claude_script=PER_TOKEN)
    run(home, tmp_path, WORK, claude_script=PER_TOKEN)
    current = run(home, tmp_path, "--status", token_env=WORK, claude_script=PER_TOKEN)
    assert "in shell   : yes" in current.stdout
    stale = run(home, tmp_path, "--status", token_env=GOOD_TOKEN, claude_script=PER_TOKEN)
    assert "older token" in stale.stdout and "open a new terminal" in stale.stdout
    assert "in shell   : yes" not in stale.stdout
    assert not leaked(current) and not leaked(stale)


def test_remove_undoes_it_all_saved_tokens_included(home, tmp_path):
    legacy_device(home)
    stray = token_path(home).parent / ".ccfleet-token.def456"   # an interrupted write
    private(stray, WORK + "\n")
    result = run(home, tmp_path, "--remove")
    assert result.returncode == 0, result.stderr
    assert not token_path(home).exists() and not stray.exists()
    assert not tokens_dir(home).exists(), "saved tokens must not outlive an undo"
    assert "removed 2 other saved token(s)" in result.stdout
    assert "ccfleet connect" not in (home / ".zshrc").read_text()
    assert not leaked(result)


def test_nothing_makes_a_tokens_directory_any_more(home, tmp_path):
    for args in ([GOOD_TOKEN], ["--status"], [WORK], ["--remove"], [GOOD_TOKEN]):
        assert run(home, tmp_path, *args, claude_script=PER_TOKEN).returncode == 0, args
        assert not tokens_dir(home).exists(), args


def test_help_says_a_computer_uses_one_account(home, tmp_path):
    out = run(home, tmp_path, "--help").stdout
    assert "One computer, one Claude account" in out
    assert "--add" not in out and "--use" not in out and "--list" not in out
