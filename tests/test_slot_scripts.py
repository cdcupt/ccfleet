"""slot-add and slot-remove: adding and releasing one person on a shared machine.

Both need root to do their work, so what is testable here is everything they do
*before* that: the argument validation, and the refusals that stop the release
script removing somebody who is not a slot. Those refusals are the whole safety
story, and they run before any privileged check.

The privileged half was proven on a live machine instead: four slots created,
isolation verified, a file planted and wiped, and the machine returned to its
original state. That is recorded in the PR rather than here, because a test
that needs root on a real box is not a test anyone will run.
"""

import subprocess
from pathlib import Path

import pytest

NODE = Path(__file__).resolve().parent.parent / "node"
ADD = NODE / "slot-add.sh"
REMOVE = NODE / "slot-remove.sh"


def run(script, *args):
    return subprocess.run([str(script), *args], capture_output=True, text=True, timeout=30)


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
        assert "slot" in result.stderr.lower()


@pytest.mark.parametrize("script", [ADD, REMOVE])
def test_the_name_is_checked_before_anything_else(script):
    """Validation must not sit behind the root check, or a machine where
    somebody runs this unprivileged never learns the name was wrong."""
    result = run(script, "--slot", "a;b")
    assert "not a valid slot name" in result.stderr or "must start with" in result.stderr


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


def test_a_valid_cap_gets_past_validation_to_the_root_check():
    """Proves the refusals above are about the value and not about everything."""
    for good in ("2G", "1500M", "512000K", "2000"):
        result = run(ADD, "--slot", "slot01", "--memory-max", good)
        assert "run this as root" in result.stderr, f"{good!r} should have been accepted"


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
