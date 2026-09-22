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
import pathlib
import re
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
INERT = ("loginctl", "systemctl", "pkill", "adduser",
         "usermod", "addgroup", "rmdir", "install", "chmod", "curl")

# Every group that reaches root, or reaches another slot's work. Both scripts
# carry this list; the test at the bottom of this file checks they agree.
PRIVILEGED_GROUPS = ("sudo", "admin", "wheel", "root", "docker", "lxd",
                     "libvirt", "kvm", "adm", "disk", "shadow", "staff")


@pytest.fixture(autouse=True)
def _never_the_real_system(tmp_path_factory, monkeypatch):
    """Point every absolute system path these scripts write at a sandbox.

    slot-add removes /etc/sudoers.d/90-ccfleet-<slot>, and under test that was
    the real directory. It went unseen because the two platforms hide it
    differently: macOS has it 0755, so `rm -f` on a missing file exits 0 and
    the suite quietly reached outside itself every run; Linux has it 0750, so
    the same line failed with EACCES, which is how CI found what nine review
    rounds and a green local suite did not. Neither outcome is a test.

    Set here rather than in each helper so a path added later is covered by
    default — as_root built its environment without either of these, and only
    luck (its tests refuse before reaching step 2) kept it off the real
    /etc/systemd/system.
    """
    sandbox = tmp_path_factory.mktemp("system")
    (sandbox / "sudoers.d").mkdir()
    monkeypatch.setenv("CCFLEET_SUDOERS_DIR", str(sandbox / "sudoers.d"))
    monkeypatch.setenv("CCFLEET_SLICE_ROOT", str(sandbox / "systemd"))


def fake_system(tmp_path, *, uid="1001", groups=SLOT_GROUP, exists=True,
                slot_home=None):
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
    groupfile = bindir / "groups.txt"
    (bindir / "id").write_text(textwrap.dedent("""\
        #!/bin/sh
        # `id -u` alone: are we root. Yes, so the script gets past that check.
        if [ "$1" = "-u" ] && [ $# -eq 1 ]; then echo 0; exit 0; fi
        if [ -f "__GONE__" ]; then exit 1; fi
        # `id -u NAME`: the account's uid.
        if [ "$1" = "-u" ]; then echo __UID__; exit 0; fi
        # `id -nG NAME`: its groups, from a file deluser edits. A fixed answer
        # would make a group the script removed look like one it kept, so the
        # removal could not be told apart from a no-op.
        if [ "$1" = "-nG" ]; then cat "__GROUPFILE__"; exit 0; fi
        # `id NAME`: does it exist.
        exit __EXISTS__
        """).replace("__GONE__", str(gone))
             .replace("__UID__", uid)
             .replace("__GROUPFILE__", str(groupfile))
             .replace("__EXISTS__", "0" if exists else "1"))
    groupfile.write_text(groups + "\n")
    # `deluser NAME GROUP` and `gpasswd -d NAME GROUP` take the group away for
    # real; `deluser NAME` on its own stays inert.
    for name, argpos in (("deluser", "$2"), ("gpasswd", "$3")):
        (bindir / name).write_text(textwrap.dedent("""\
            #!/bin/sh
            [ $# -lt __MIN__ ] && exit 0
            g="__ARG__"
            tr ' ' '\\n' < "__GROUPFILE__" | grep -vx "$g" | tr '\\n' ' ' > "__GROUPFILE__.new"
            mv "__GROUPFILE__.new" "__GROUPFILE__"
            exit 0
            """).replace("__ARG__", argpos)
                 .replace("__MIN__", "2" if name == "deluser" else "3")
                 .replace("__GROUPFILE__", str(groupfile)))
    (bindir / "userdel").write_text(f'#!/bin/sh\ntouch "{gone}"\nexit 0\n')
    home = str(slot_home) if slot_home else "/home/$2"
    (bindir / "getent").write_text(textwrap.dedent("""\
        #!/bin/sh
        if [ "$1" = "passwd" ]; then echo "$2:x:1001:1001::__HOME__:/bin/sh"; exit 0; fi
        exit 0
        """).replace("__HOME__", home))
    (bindir / "pgrep").write_text("#!/bin/sh\nexit 1\n")   # nothing running
    # The OS answering "who owns this directory", in the same way `id` above
    # answers "what groups is this account in". slot-remove's own decision
    # still runs for real against the answer; only the answer is supplied here,
    # because a test cannot chown a directory to another user without root.
    # Set FAKE_DIR_OWNER to a different uid to play a home someone else owns.
    (bindir / "find").write_text(textwrap.dedent("""\
        #!/bin/sh
        if [ "$2" = "-maxdepth" ] && [ "$3" = "0" ] && [ "$4" = "-uid" ]; then
          [ "${FAKE_DIR_OWNER:-$5}" = "$5" ] && echo "$1"
          exit 0
        fi
        exec /usr/bin/find "$@"
        """))
    (bindir / "find").chmod(0o755)
    if slot_home:
        # A working claude in the sandboxed home. Without it every add test ran
        # against exactly the condition the script exists to refuse — and the
        # tests accepted it, which is how a slot with no Claude Code came to be
        # reported as ready.
        cc = pathlib.Path(slot_home) / ".local" / "bin"
        cc.mkdir(parents=True, exist_ok=True)
        (cc / "claude").write_text("#!/bin/sh\necho '2.1.278 (Claude Code)'\n")
        (cc / "claude").chmod(0o755)
    # sudo runs what it is given rather than swallowing it, so anything the
    # script does *as the slot* is still observable. Without this, every
    # `sudo -u slot systemctl --user ...` vanishes and a test watching for it
    # sees nothing and concludes, wrongly, that it never happened.
    #
    # It APPLIES the VAR=VALUE assignments rather than discarding them, and
    # that is not a detail. The script passes HOME=/home/<slot>; a sudo that
    # drops it runs the rest against whoever is running the tests — and that
    # is not hypothetical, it happened: an earlier version of this harness
    # appended to the real ~/.profile, created a real ~/workspace and wrote
    # hasTrustDialogAccepted into the real ~/.claude.json on the machine the
    # suite was running on.
    (bindir / "sudo").write_text(textwrap.dedent("""\
        #!/bin/sh
        assignments=""
        while [ $# -gt 0 ]; do
          case "$1" in
            -u) shift 2 ;;
            *=*) assignments="$assignments $1"; shift ;;
            *) break ;;
          esac
        done
        [ $# -eq 0 ] && exit 0
        # `env` so the assignments actually reach the command, HOME included.
        exec env $assignments "$@"
        """))
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


@pytest.mark.parametrize("group", PRIVILEGED_GROUPS)
def test_release_refuses_an_account_with_a_route_to_root(tmp_path, group):
    """Belt and braces below the marker: a provisioned slot is in none of these,
    so finding one means the account was edited by hand — and that is not
    discovered by deleting a home directory. sudo is the obvious one and not
    the only one: docker mounts the host root, adm reads every slot's logs."""
    result = as_root(REMOVE, tmp_path, "--slot", "erik",
                     groups=f"erik {group} {SLOT_GROUP}")
    assert result.returncode != 0, f"would have deleted an account in {group}"
    assert f"is in the {group} group" in result.stderr


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


def test_a_slot_is_given_a_way_in(tmp_path):
    """Password login is disabled and no SSH key is installed, on purpose. If
    Remote Control is not installed too, the script provisions an account
    nobody can reach — which is not a slot, it is a dead user.

    Watched through the commands the script runs rather than by looking for
    words in it: the first version of this test searched the source for
    "claude-remote-control.service", which still appears in the enable line
    even when nothing installs the unit at all.
    """
    slot_home = tmp_path / "slothome"
    slot_home.mkdir()
    bindir = fake_system(tmp_path, slot_home=slot_home)
    log = tmp_path / "commands.log"
    # Not mkdir: the slice directory has to really exist, because the script
    # writes the drop-in into it with a redirect.
    for name in ("install", "systemctl", "curl"):
        (bindir / name).write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
        (bindir / name).chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCFLEET_SLICE_ROOT"] = str(tmp_path / "slices")
    result = subprocess.run([str(ADD), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    ran = log.read_text() if log.exists() else ""

    # Per line, not per file. Both unit names appear in the single `enable`
    # line, so a substring check passes even when nothing installs anything —
    # which is exactly how the first two versions of this test let a gutted
    # install loop through.
    placed = [ln for ln in ran.splitlines() if ln.startswith(("install ", "curl "))]
    for unit in ("ccfleet-shell.service", "claude-remote-control.service"):
        assert any(unit in ln for ln in placed), \
            f"{unit} was never put on the machine; only saw: {placed}"
    assert "enable ccfleet-shell.service claude-remote-control.service" in ran, \
        "both are enabled, so they come back after a reboot"
    assert "start ccfleet-shell.service" in ran, "the work session is started now"
    assert "start claude-remote-control" not in ran, \
        "Remote Control needs a sign-in first, and Type=forking means it will not retry"


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


def test_the_harness_never_touches_the_home_of_whoever_runs_it(tmp_path):
    """This is not a hypothetical. An earlier version of this file used a fake
    `sudo` that discarded the `HOME=...` assignment the script passes, so
    everything the script does "as the slot" ran as the developer instead: it
    appended to the real ~/.profile, created a real ~/workspace, and wrote
    hasTrustDialogAccepted into the real ~/.claude.json.

    The sandbox is the assertion. If sudo ever stops applying HOME, this fails
    rather than quietly editing somebody's machine again.
    """
    slot_home = tmp_path / "slothome"
    slot_home.mkdir()
    real_profile = Path.home() / ".profile"
    before = real_profile.read_text() if real_profile.exists() else None

    bindir = fake_system(tmp_path, slot_home=slot_home)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCFLEET_SLICE_ROOT"] = str(tmp_path / "slices")
    subprocess.run([str(ADD), "--slot", "slot01"], capture_output=True,
                   text=True, env=env, timeout=60)

    after = real_profile.read_text() if real_profile.exists() else None
    assert after == before, "the script wrote to the real ~/.profile"
    # And it did do its work — somewhere safe.
    assert (slot_home / ".profile").exists(), "the sandboxed home got the profile line"
    assert "DISABLE_AUTOUPDATER" in (slot_home / ".profile").read_text()


def test_the_soft_memory_ceiling_is_four_fifths_of_the_hard_one(tmp_path):
    """Taking 80% of the number while keeping its unit is wrong whenever the
    number is small: 2G became 1G, which is half, and 1G became 0G, which puts
    a slot under reclaim pressure from its first byte."""
    for cap, low, high in (("1G", 780, 860), ("2G", 1560, 1720),
                           ("512M", 390, 430), ("4G", 3100, 3400)):
        slot_home = tmp_path / f"home-{cap}"
        slot_home.mkdir()
        slices = tmp_path / f"slices-{cap}"
        env = dict(os.environ)
        env["PATH"] = f"{fake_system(tmp_path, slot_home=slot_home)}:{env['PATH']}"
        env["CCFLEET_SLICE_ROOT"] = str(slices)
        subprocess.run([str(ADD), "--slot", "slot01", "--memory-max", cap],
                       capture_output=True, text=True, env=env, timeout=60)
        conf = next(slices.rglob("50-ccfleet.conf"))
        written = dict(ln.split("=", 1) for ln in conf.read_text().splitlines()
                       if "=" in ln and not ln.startswith("#"))
        assert written["MemoryMax"] == cap
        got = int(written["MemoryHigh"].rstrip("M"))
        assert low <= got <= high, f"{cap} gave MemoryHigh={got}M, want ~80%"
        assert got > 0, "a soft ceiling of zero is not a ceiling"


def test_a_slot_without_claude_code_is_not_called_ready(tmp_path):
    """`claude --version | head -1` exits with head's status, so a missing
    claude produced an empty version from a pipeline that succeeded — and the
    guard compared it against the literal "unknown", which it could never be.
    The script said the slot was ready with nothing installed on it."""
    slot_home = tmp_path / "slothome"
    slot_home.mkdir()          # deliberately no ~/.local/bin/claude in it
    bindir = fake_system(tmp_path)
    (bindir / "getent").write_text(
        f'#!/bin/sh\n[ "$1" = "passwd" ] && {{ echo "$2:x:1001:1001::{slot_home}:/bin/sh"; exit 0; }}\nexit 0\n')
    (bindir / "getent").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCFLEET_SLICE_ROOT"] = str(tmp_path / "slices")
    result = subprocess.run([str(ADD), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode != 0, "a slot with no Claude Code is not a ready slot"
    assert "did not install" in result.stderr
    assert "is ready" not in result.stdout


def _remove_env(tmp_path, slot_home, **extra):
    """A removal run whose passwd entry points at `slot_home`."""
    bindir = fake_system(tmp_path)
    (bindir / "getent").write_text(
        f'#!/bin/sh\n[ "$1" = "passwd" ] && {{ echo "$2:x:1001:1001::{slot_home}:/bin/sh"; exit 0; }}\nexit 0\n')
    (bindir / "getent").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCFLEET_SLICE_ROOT"] = str(tmp_path / "slices")
    env.update(extra)
    return bindir, env


@pytest.mark.parametrize("home,why", [
    ("/", "the root directory"),
    ("/home", "a top-level directory shared by every account"),
    ("/root", "another account's home"),
    ("", "no home at all"),
    ("/home/../..", "the root by another spelling"),
    ("relative/path", "not absolute"),
])
def test_a_home_that_is_not_a_slot_home_is_never_deleted(tmp_path, home, why):
    """The path comes from passwd, which is edited by hand, and both
    `userdel -r` and the rm behind it delete whatever it names. Releasing one
    slot must not be able to wipe the machine, so an implausible home is
    refused before anything is stopped, let alone removed."""
    bindir, env = _remove_env(tmp_path, home)
    result = subprocess.run([str(REMOVE), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode != 0, f"deleted {home!r} — {why}"
    assert "Refusing" in result.stderr
    # And refused early: the account is still there, nothing was stopped.
    assert not (bindir / "gone.marker").exists(), "userdel ran before the path was judged"


def test_a_home_shared_with_another_account_is_refused(tmp_path):
    """Shape alone cannot see this: /home/alice looks exactly like a slot home.
    Ownership is what separates the slot's own directory from somebody's."""
    slot_home = tmp_path / "alice"
    slot_home.mkdir()
    bindir, env = _remove_env(tmp_path, slot_home, FAKE_DIR_OWNER="2002")
    result = subprocess.run([str(REMOVE), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode != 0, "deleted a directory belonging to another account"
    assert "not owned by uid 1001" in result.stderr
    assert not (bindir / "gone.marker").exists()
    assert slot_home.is_dir(), "the other account's home was removed"


def test_a_symlinked_home_is_not_followed(tmp_path):
    """`rm -rf` on a symlink takes the link, but `userdel -r` is under no such
    promise, and a home that is a link to somewhere else is not a slot home."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "slothome"
    link.symlink_to(real)
    bindir, env = _remove_env(tmp_path, link)
    result = subprocess.run([str(REMOVE), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode != 0
    # Not the bare word "symlink": pytest names this test's tmp directory after
    # the test, so "symlink" appears in any message that quotes the path — and
    # a script that had deleted the account and only then complained matched it
    # just as well as the refusal. Assert the sentence the guard actually says,
    # and the thing that distinguishes refusing from reporting: nothing ran.
    assert "Refusing to delete through it" in result.stderr
    assert not (bindir / "gone.marker").exists(), "userdel ran before the path was judged"
    assert real.is_dir()


@pytest.mark.parametrize("uid", ["65534", "60000", "999"])
def test_a_system_account_in_the_slot_group_is_refused_by_both_scripts(tmp_path, uid):
    """`nobody` is uid 65534, which is comfortably >= 1000. Put it in
    ccfleet-slots by hand and a bare lower-bound check waves it through — to
    slot-remove, which deletes the account and its home, and to slot-add, which
    chmods that home to 0700. Ordinary logins are 1000..59999; both ends matter."""
    for script, verb in ((REMOVE, "delete"), (ADD, "reshape")):
        slot_home = tmp_path / f"home-{uid}-{verb}"
        slot_home.mkdir()
        sysdir = tmp_path / f"sys-{uid}-{verb}"
        sysdir.mkdir()
        bindir = fake_system(sysdir, uid=uid, slot_home=slot_home)
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["CCFLEET_SLICE_ROOT"] = str(tmp_path / f"slices-{uid}-{verb}")
        result = subprocess.run([str(script), "--slot", "slot01"], capture_output=True,
                                text=True, env=env, timeout=60)
        assert result.returncode != 0, f"{script.name} would {verb} uid {uid}"
        assert "Refusing" in result.stderr
        assert not (bindir / "gone.marker").exists(), f"{script.name} ran userdel on uid {uid}"
        assert slot_home.is_dir(), f"{script.name} removed the home of uid {uid}"


def test_provisioning_takes_away_every_route_to_root(tmp_path):
    """Adopting an existing slot must not leave it holding privileges. docker
    is the sharp one — a member can start a container that mounts the host root
    — but each of these reaches either root or another slot's files."""
    slot_home = tmp_path / "slothome"
    slot_home.mkdir()
    held = " ".join(PRIVILEGED_GROUPS)
    bindir = fake_system(tmp_path, groups=f"slot01 {held} {SLOT_GROUP}",
                         slot_home=slot_home)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CCFLEET_SLICE_ROOT"] = str(tmp_path / "slices")
    result = subprocess.run([str(ADD), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    left = (bindir / "groups.txt").read_text().split()
    assert not [g for g in left if g in PRIVILEGED_GROUPS], f"still holds {left}"
    assert SLOT_GROUP in left, "stripped the group that marks it as a slot"


def test_both_scripts_agree_on_what_counts_as_privileged(tmp_path):
    """Two standalone scripts, one list. Drift means slot-add hands out a slot
    holding a group slot-remove will then refuse to clean up."""
    lists = {}
    for script in (ADD, REMOVE):
        line = [ln for ln in script.read_text().splitlines()
                if "for PRIVILEGED in " in ln]
        assert len(line) == 1, f"{script.name} has {len(line)} privileged lists"
        lists[script.name] = line[0].split("for PRIVILEGED in ", 1)[1].split(";")[0].split()
    names = list(lists)
    assert lists[names[0]] == lists[names[1]], lists
    assert lists[names[0]] == list(PRIVILEGED_GROUPS), "this file has drifted too"


def test_provisioning_clears_the_sudoers_drop_in_where_it_is_told_to(tmp_path):
    """Proves the override is honoured rather than decorative: the file the
    script removes is the one under CCFLEET_SUDOERS_DIR. Without this the
    script could go on reading the real /etc/sudoers.d and every test would
    still pass, which is exactly what happened."""
    slot_home = tmp_path / "slothome"
    slot_home.mkdir()
    drop_in = pathlib.Path(os.environ["CCFLEET_SUDOERS_DIR"]) / "90-ccfleet-slot01"
    drop_in.write_text("slot01 ALL=(ALL) NOPASSWD: ALL\n")
    bindir = fake_system(tmp_path, slot_home=slot_home)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = subprocess.run([str(ADD), "--slot", "slot01"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not drop_in.exists(), "left a sudoers drop-in granting the slot root"


@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_no_system_path_is_written_without_a_way_to_redirect_it(script):
    """A bare /etc path in these scripts is a path the test suite writes to on
    the machine running it. Every one must come from an overridable variable,
    so adding the next one cannot quietly re-open this."""
    offenders = []
    for n, line in enumerate(script.read_text().splitlines(), 1):
        bare = line.split("#", 1)[0]
        if "/etc/" not in bare:
            continue
        # Fine: the line that *defines* the overridable default.
        if re.search(r'\$\{CCFLEET_\w+:-/etc/', bare):
            continue
        offenders.append(f"{script.name}:{n}: {line.strip()}")
    assert not offenders, "unredirectable system paths:\n" + "\n".join(offenders)
