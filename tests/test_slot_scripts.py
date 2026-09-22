"""slot-add and slot-remove: adding and releasing one person on a shared machine.

slot-remove deletes a home directory, so the tests that matter are the ones
about what it refuses. Those refusals run as root, which a test suite is not —
so the system calls they decide from are stubbed on PATH, the same way the
connect-script tests stand in for `claude`.

That stubbing exists because the first version of this file claimed to cover
those refusals and did not: every run was unprivileged and stopped at the root
check, so the assertions passed without the refusal code executing once. A test
that claims coverage it does not have is worse than no test.

The real machine was exercised separately — four slots on one box, isolation
verified, a file planted and wiped, the box returned to its original state.
That belongs in the PR, not here: a test needing root on a live host is one
nobody runs.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

NODE = Path(__file__).resolve().parent.parent / "node"
ADD = NODE / "slot-add.sh"
REMOVE = NODE / "slot-remove.sh"
SLOT_GROUP = "ccfleet-slots"

# Every command the scripts reach for after their refusals. Stubbed to do
# nothing, so a test that gets past a refusal fails on its assertion rather
# than on whatever the real command would have done.
INERT = ("loginctl", "systemctl", "pkill", "deluser", "adduser",
         "usermod", "addgroup", "rmdir", "install", "chmod", "sudo", "curl")


def fake_system(tmp_path, *, uid="1001", groups=SLOT_GROUP, exists=True):
    """A PATH where `id` and friends describe whatever account we want to test.

    The scripts ask three things before touching anything: am I root, does this
    account exist, and what groups is it in. Answering those from stubs is what
    lets the refusals actually run.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    # Stateful on purpose. slot-remove does not trust userdel — it asks again
    # afterwards whether the account is really gone, because a slot that is
    # only mostly released must never be handed to the next person. A stub that
    # always says "exists" makes that check look like a bug; one that notices
    # the deletion is what tests it.
    gone = bindir / "gone.marker"
    (bindir / "id").write_text(textwrap.dedent("""\
        #!/bin/sh
        # `id -u` alone: are we root. Yes, so the script gets past that check.
        if [ "$1" = "-u" ] && [ $# -eq 1 ]; then echo 0; exit 0; fi
        if [ -f "__GONE__" ]; then exit 1; fi
        # `id -u NAME`: the account's uid.
        if [ "$1" = "-u" ]; then echo __UID__; exit 0; fi
        # `id -nG NAME`: its groups.
        if [ "$1" = "-nG" ]; then echo "__GROUPS__"; exit 0; fi
        # `id NAME`: does it exist.
        exit __EXISTS__
        """).replace("__GONE__", str(gone))
             .replace("__UID__", uid)
             .replace("__GROUPS__", groups)
             .replace("__EXISTS__", "0" if exists else "1"))
    (bindir / "userdel").write_text(f'#!/bin/sh\ntouch "{gone}"\nexit 0\n')
    (bindir / "getent").write_text(textwrap.dedent("""\
        #!/bin/sh
        if [ "$1" = "passwd" ]; then echo "$2:x:1001:1001::/home/$2:/bin/sh"; exit 0; fi
        exit 0
        """))
    (bindir / "pgrep").write_text("#!/bin/sh\nexit 1\n")   # nothing running
    for name in INERT:
        (bindir / name).write_text("#!/bin/sh\nexit 0\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir


def as_root(script, tmp_path, *args, **kw):
    env = dict(os.environ)
    env["PATH"] = f"{fake_system(tmp_path, **kw)}:{env['PATH']}"
    return subprocess.run([str(script), *args], capture_output=True, text=True,
                          env=env, timeout=30)


def run(script, *args):
    """Unprivileged, for the checks that happen before the root test."""
    return subprocess.run([str(script), *args], capture_output=True, text=True, timeout=30)


# -- argument handling, which runs before privilege --------------------------------

@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_both_scripts_are_executable_and_parse(script):
    assert script.exists() and script.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0


@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_a_slot_name_that_could_be_a_command_is_refused(script):
    """The name is interpolated into commands run as root and becomes a unix
    account, so anything outside the allowed set is an injection vector rather
    than a cosmetic problem."""
    for nasty in ("a;b", "a b", "../etc", "$(id)", "`id`", "a|b", "-rf", "A", "x" * 40, "1abc"):
        result = run(script, "--slot", nasty)
        assert result.returncode != 0, f"{nasty!r} was not refused"


@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_the_name_is_checked_before_privilege(script):
    """Someone running this without sudo should learn their slot name is wrong,
    rather than only that they are not root and find the second problem after
    fixing the first."""
    result = run(script, "--slot", "a;b")
    assert "run this as root" not in result.stderr
    assert "slot" in result.stderr.lower()


@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_a_missing_slot_is_refused(script):
    assert "required" in run(script).stderr


@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_an_unknown_option_is_refused_rather_than_ignored(script):
    result = run(script, "--wipe-everything")
    assert result.returncode != 0 and "unknown option" in result.stderr


def test_a_memory_cap_that_is_not_one_is_refused():
    for bad in ("lots", "2GB", "-1G", "2 G", ""):
        result = run(ADD, "--slot", "slot01", "--memory-max", bad)
        assert result.returncode != 0, f"{bad!r} was not refused"


def test_a_cap_too_small_to_run_in_is_refused():
    """Zero would write MemoryMax=0 and leave the slot unable to start anything.
    A measured session is about 265 MB, so anything under 512M is a typo rather
    than a small slot."""
    for tiny in ("0", "1", "100M", "511M", "0G", "1K"):
        result = run(ADD, "--slot", "slot01", "--memory-max", tiny)
        assert result.returncode != 0, f"{tiny!r} was accepted"
        assert "at least 512M" in result.stderr


def test_a_valid_cap_gets_past_validation():
    """Proves the refusals above are about the value, not about everything.
    512M is the boundary and must be on the accepted side of it."""
    for good in ("512M", "2G", "1500M", "600000K", "900000000"):
        result = run(ADD, "--slot", "slot01", "--memory-max", good)
        assert "run this as root" in result.stderr, f"{good!r} should have been accepted"


# -- the refusals that stop a home directory being deleted -------------------------

def test_release_refuses_an_account_it_did_not_create(tmp_path):
    """The one that matters. "No sudo and a uid over 1000" describes a great
    many ordinary accounts — a colleague, a service user — and this script
    deletes a home directory. Only membership of the group slot-add creates
    identifies a slot."""
    result = as_root(REMOVE, tmp_path, "--slot", "alice", groups="alice users")
    assert result.returncode != 0
    assert "not a ccfleet slot" in result.stderr
    assert "Refusing" in result.stderr


def test_release_refuses_an_account_with_sudo(tmp_path):
    """Belt and braces below the marker: if it has sudo something was edited by
    hand, and that is not discovered by deleting a home directory."""
    result = as_root(REMOVE, tmp_path, "--slot", "erik",
                     groups=f"erik sudo {SLOT_GROUP}")
    assert result.returncode != 0 and "has sudo" in result.stderr


def test_release_refuses_a_system_account(tmp_path):
    result = as_root(REMOVE, tmp_path, "--slot", "daemon",
                     groups=f"daemon {SLOT_GROUP}", uid="1")
    assert result.returncode != 0 and "system account" in result.stderr


def test_release_proceeds_for_something_that_really_is_a_slot(tmp_path):
    """Proves the three refusals are about their conditions and not a script
    that refuses everything."""
    result = as_root(REMOVE, tmp_path, "--slot", "slot01",
                     groups=f"slot01 {SLOT_GROUP}")
    assert result.returncode == 0, result.stderr
    assert "released" in result.stdout


def test_release_refuses_to_call_a_half_removed_slot_reusable(tmp_path):
    """It asks again afterwards rather than trusting userdel. A slot that is
    only mostly gone must never be handed to the next person, and the command
    reporting success is not the same as the account being gone."""
    env = dict(os.environ)
    bindir = fake_system(tmp_path, groups=f"slot01 {SLOT_GROUP}")
    # A userdel that claims success and does nothing, which is the case this
    # check exists for.
    (bindir / "userdel").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "userdel").chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = subprocess.run([str(REMOVE), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=30)
    assert result.returncode != 0
    assert "release incomplete" in result.stderr
    assert "Do NOT reuse" in result.stderr


def test_release_says_nothing_to_do_for_an_account_that_is_gone(tmp_path):
    result = as_root(REMOVE, tmp_path, "--slot", "slot01", exists=False)
    assert result.returncode == 0 and "nothing to release" in result.stdout


def test_add_refuses_to_take_over_somebody_elses_account(tmp_path):
    """An existing account with the requested name is not ours to reshape: the
    steps after this would tighten its home to 0700 and strip its sudo."""
    result = as_root(ADD, tmp_path, "--slot", "alice", groups="alice users")
    assert result.returncode != 0
    assert "not a ccfleet slot" in result.stderr and "Refusing to take it over" in result.stderr


def test_add_continues_for_a_slot_it_created_before(tmp_path):
    """Re-running must repair rather than refuse, which is how the single-owner
    installer already behaves."""
    result = as_root(ADD, tmp_path, "--slot", "slot01", groups=f"slot01 {SLOT_GROUP}")
    assert "already exists" in result.stdout


# -- what the help promises --------------------------------------------------------

@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_help_works_without_root_and_says_what_matters(script):
    out = run(script, "--help").stdout
    assert out.strip(), "there is help text"
    if script is ADD:
        assert "sudo" in out, "the absence of sudo is the security property; say it"
    else:
        assert "wipe" in out.lower() or "remov" in out.lower()


def test_release_says_the_account_survives_the_slot():
    """The distinction somebody will be anxious about: releasing a slot destroys
    the login on that machine, not their Claude account."""
    assert "account" in run(REMOVE, "--help").stdout.lower()


def test_the_trust_prompt_is_answered_for_the_directory_people_work_in(tmp_path):
    """The prompt is per-directory. slot-add creates ~/workspace and that is
    where work happens, so trusting the home leaves the prompt waiting in the
    one place it matters."""
    text = ADD.read_text()
    assert "~/workspace'), {})['hasTrustDialogAccepted']" in text
    assert "expanduser('~'), {})['hasTrustDialogAccepted']" not in text, \
        "trusting the home is not the same as trusting the workspace"
    assert "remoteDialogSeen" in text, "both prompts, or the step does not do what it says"


def test_a_slot_is_given_a_way_in():
    """Password login is disabled and no SSH key is installed, on purpose. If
    Remote Control is not installed too, the script provisions an account
    nobody can reach — which is not a slot, it is a dead user."""
    text = ADD.read_text()
    assert "claude-remote-control.service" in text, "the stated access path must be installed"
    assert "ccfleet-shell.service" in text, "and the work session it lives in"
    # Enabled rather than started: Remote Control needs an authenticated
    # session, and there is none until the holder signs in.
    assert "enable ccfleet-shell.service claude-remote-control.service" in text
    assert "start ccfleet-shell.service" in text
    assert "start claude-remote-control" not in text, \
        "starting it before a sign-in cannot work and will not retry"


def test_the_units_it_installs_exist_in_the_repo():
    """Fetching a unit that is not there would fail on a real machine long after
    this script said the slot was ready."""
    for unit in ("ccfleet-shell.service", "claude-remote-control.service"):
        assert (NODE / "systemd" / unit).exists(), f"{unit} is missing from node/systemd"


def test_the_closing_message_does_not_promise_what_is_not_installed():
    """It told people to reach the slot through the console and Remote Control
    while installing neither. The message and the script have to agree."""
    text = ADD.read_text()
    promises_rc = "Remote Control" in text or "remote-control" in text
    assert not promises_rc or "claude-remote-control.service" in text
