"""A slot moves to another Claude account: the agent's half.

A change of account ("switch") signs in to scratch like any sign-in on a bound
slot, and only one that finishes there replaces the account the slot had: the
credential, the account block of ~/.claude.json with the rest of that file
kept, and the binding. Until then, and whenever it fails, the slot keeps the
account it had exactly as it was, so it never holds two. Only a switch can
keep another account; a plain sign-in, or any word the agent does not know,
still refuses one.
"""

from __future__ import annotations

import json
import stat

import pytest

from ccfleet_agent import agent, machine

from . import test_agent_account_binding
from .test_agent_account_binding import (
    MINE,
    NOW,
    THEIRS,
    Slot,
    fp_of,
    scratch,
    sign_in,
    signed_in_as,
    state,
    write_account,
)

# The slot's home the binding tests use (a fixture).
home = test_agent_account_binding.home

#: What the slot's ~/.claude.json holds besides its account: its holder's.
KEPT = {"numStartups": 12, "theme": "dark",
        "projects": {"/home/slot01/work": {"allowedTools": ["Bash(git:*)"]}}}


def bound_as(home, uuid=MINE):
    """A slot signed in and bound as `uuid`, with settings and history of its
    holder's around the account block, and an account block that says more
    than the one a sign-in writes."""
    signed_in_as(home, uuid)
    config = home / ".claude.json"
    data = json.loads(config.read_text())
    data["oauthAccount"] = {**data["oauthAccount"], "organizationName": "The Old Org"}
    config.write_text(json.dumps({**KEPT, "oauthAccount": data["oauthAccount"],
                                  "hasCompletedOnboarding": True}))
    agent.slot_facts({}, Slot(home), now=NOW)
    assert state(home)["bound_fp"] == fp_of(uuid)


def credential(home):
    return (home / ".claude" / ".credentials.json").read_text()


def profile(home):
    return json.loads((home / ".claude.json").read_text())


# -- moving to another account ---------------------------------------------------------------

def test_a_change_of_account_moves_the_slot_to_the_other_one(home):
    bound_as(home, MINE)
    fake = Slot(home, signs_in_as=THEIRS)
    said = sign_in(fake, kind="switch")
    assert said == {"state": "done", "detail": agent.SWITCHED, "requested_at": 100.0}
    [command] = fake.started()
    assert f"{agent.CONFIG_DIR_VAR}={scratch(home)}" in command, "signed in over ~/.claude"
    assert "sk-ant-oat01-NEW" in credential(home)
    assert stat.S_IMODE((home / ".claude" / ".credentials.json").stat().st_mode) == 0o600
    assert state(home)["bound_fp"] == fp_of(THEIRS)
    assert not scratch(home).exists()
    assert fake.logouts() == [], "the account it moved to was signed out"
    assert len(fake.restarts()) == 1, "Remote Control stayed on the old account"
    # And from then on it reports the one account, kept and had alike.
    creds = agent.slot_facts({}, fake, now=NOW)["credentials"]
    assert creds["account_fp"] == creds["bound_fp"] == fp_of(THEIRS)


def test_the_account_block_is_replaced_whole_and_everything_else_kept(home):
    bound_as(home, MINE)
    order = list(profile(home))
    sign_in(Slot(home, signs_in_as=THEIRS), kind="switch")
    after = profile(home)
    assert after["oauthAccount"] == {"accountUuid": THEIRS, "emailAddress": "holder@example.com"}
    assert {k: after[k] for k in KEPT} == KEPT and after["hasCompletedOnboarding"] is True
    assert list(after) == order, "the file's keys moved"
    assert stat.S_IMODE((home / ".claude.json").stat().st_mode) == 0o600


class CutShort(Exception):
    """The run dying while Remote Control restarts."""


def test_a_run_cut_short_after_the_change_keeps_the_new_binding(home):
    bound_as(home, MINE)

    class Dies(Slot):
        def __call__(self, argv, **kwargs):
            if list(argv[:3]) == ["systemctl", "--user", "restart"]:
                raise CutShort()
            return super().__call__(argv, **kwargs)

    with pytest.raises(CutShort):
        sign_in(Dies(home, signs_in_as=THEIRS), kind="switch")
    assert "sk-ant-oat01-NEW" in credential(home)
    assert state(home)["bound_fp"] == fp_of(THEIRS), "bound to an account it no longer has"


def test_a_change_to_the_account_it_already_has_says_so_and_moves_nothing(home):
    bound_as(home, MINE)
    before = (home / ".claude.json").read_bytes()
    fake = Slot(home, signs_in_as=MINE)
    said = sign_in(fake, kind="switch")
    assert said == {"state": "done", "detail": agent.SAME_ACCOUNT, "requested_at": 100.0}
    assert "sk-ant-oat01-NEW" in credential(home), "its fresh credential was not kept"
    assert (home / ".claude.json").read_bytes() == before, "the account block was rewritten"
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_signing_in_again_says_no_more_than_that_it_is_done(home):
    """The two words are a change of account's alone: the server keeps a row
    and starts a week on them, and a plain sign-in asks for neither."""
    bound_as(home, MINE)
    said = sign_in(Slot(home, signs_in_as=MINE), kind="login")
    assert said == {"state": "done", "requested_at": 100.0}


def test_a_plain_sign_in_still_refuses_another_account(home):
    bound_as(home, MINE)
    before = credential(home), (home / ".claude.json").read_bytes()
    said = sign_in(Slot(home, signs_in_as=THEIRS), kind="login")
    assert said == {"state": "failed", "detail": agent.OTHER_ACCOUNT, "requested_at": 100.0}
    assert (credential(home), (home / ".claude.json").read_bytes()) == before
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_what_may_be_kept_is_what_the_attempt_started_as(home):
    """A sign-in that began as one stays one to its end: a later word calling
    it a change of account does not let another account in."""
    bound_as(home, MINE)
    before = credential(home)
    fake = Slot(home, signs_in_as=THEIRS)
    login = {"requested_at": 100.0, "kind": "login"}
    agent.slot_facts({"login": login}, fake, now=NOW)
    fake.pane = "Visit https://claude.com/cai/oauth/authorize?code=true to continue"
    agent.slot_facts({"login": login}, fake, now=NOW)
    switch = {**login, "kind": "switch", "code": "c"}
    agent.slot_facts({"login": switch}, fake, now=NOW)
    said = agent.slot_facts({"login": switch}, fake, now=NOW)["login"]
    assert said["state"] == "failed" and said["detail"] == agent.OTHER_ACCOUNT
    assert credential(home) == before and state(home)["bound_fp"] == fp_of(MINE)


def test_on_a_slot_not_yet_bound_a_change_is_its_first_sign_in(home):
    fake = Slot(home, signs_in_as=THEIRS)
    said = sign_in(fake, kind="switch")
    assert said == {"state": "done", "requested_at": 100.0}, "a first sign-in is no change"
    assert all(agent.CONFIG_DIR_VAR not in cmd for cmd in fake.started()), "went to scratch"
    assert state(home)["bound_fp"] == fp_of(THEIRS)


# -- a word the agent does not know ------------------------------------------------------------

def test_an_unknown_word_from_the_server_is_a_sign_in_that_refuses_another_account(home):
    bound_as(home, MINE)
    said = sign_in(Slot(home, signs_in_as=THEIRS), kind="rename")
    assert said["state"] == "failed" and said["detail"] == agent.OTHER_ACCOUNT
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_the_machine_passes_a_switch_on_and_makes_an_unknown_word_a_sign_in():
    asked = {"requested_at": 7.0, "kind": "switch"}
    assert machine._login_request(asked)["kind"] == "switch"
    assert machine._login_request({**asked, "kind": "rename"})["kind"] == "login"


def test_a_machine_from_before_switch_leaves_the_slot_as_it_was(home, monkeypatch):
    """The fail-safe with old agents: a machine agent that does not know the
    word hands the slot a sign-in, and the slot refuses the other account."""
    monkeypatch.setattr(machine, "LOGIN_KINDS", ("login", "token"))
    bound_as(home, MINE)
    before = credential(home), (home / ".claude.json").read_bytes()
    kind = machine._login_request({"requested_at": 100.0, "kind": "switch"})["kind"]
    said = sign_in(Slot(home, signs_in_as=THEIRS), kind=kind)
    assert kind == "login" and said["detail"] == agent.OTHER_ACCOUNT
    assert (credential(home), (home / ".claude.json").read_bytes()) == before


# -- a change that cannot be made changes nothing ----------------------------------------------

@pytest.mark.parametrize("config", [None, b"not json", b"[]", b"\xff\xfe{}"])
def test_an_account_block_that_cannot_be_swapped_leaves_the_old_account(home, config):
    bound_as(home, MINE)
    before = credential(home)
    if config is None:
        (home / ".claude.json").unlink()
    else:
        (home / ".claude.json").write_bytes(config)
    said = sign_in(Slot(home, signs_in_as=THEIRS), kind="switch")
    assert said["state"] == "failed" and said["detail"] == agent.NOT_ADOPTED
    assert credential(home) == before and state(home)["bound_fp"] == fp_of(MINE)
    if config is not None:
        assert (home / ".claude.json").read_bytes() == config
    assert not scratch(home).exists()


def refusing(monkeypatch, path):
    """_atomic_write, failing for `path` alone."""
    real = agent._atomic_write

    def write(target, text, mode=0o600):
        return False if target == path else real(target, text, mode)

    monkeypatch.setattr(agent, "_atomic_write", write)


def test_an_account_block_that_cannot_be_written_leaves_the_old_account(home, monkeypatch):
    bound_as(home, MINE)
    before = credential(home), (home / ".claude.json").read_bytes()
    refusing(monkeypatch, home / ".claude.json")
    said = sign_in(Slot(home, signs_in_as=THEIRS), kind="switch")
    assert said["state"] == "failed" and said["detail"] == agent.NOT_ADOPTED
    assert (credential(home), (home / ".claude.json").read_bytes()) == before
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_a_credential_that_cannot_be_written_puts_the_old_block_back(home, monkeypatch):
    bound_as(home, MINE)
    before = credential(home), (home / ".claude.json").read_bytes()
    refusing(monkeypatch, home / ".claude" / ".credentials.json")
    said = sign_in(Slot(home, signs_in_as=THEIRS), kind="switch")
    assert said["state"] == "failed" and said["detail"] == agent.NOT_ADOPTED
    assert (credential(home), (home / ".claude.json").read_bytes()) == before
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_a_credential_that_cannot_be_read_is_not_switched_to(home):
    bound_as(home, MINE)
    before = credential(home), (home / ".claude.json").read_bytes()
    write_account(scratch(home), scratch(home) / ".claude.json", THEIRS, "x")
    (scratch(home) / ".credentials.json").unlink()
    assert agent.adopt_sign_in(fp_of(MINE), Slot(home), switch=True) == (
        agent.NOT_ADOPTED, fp_of(THEIRS))
    assert (credential(home), (home / ".claude.json").read_bytes()) == before
    assert not scratch(home).exists()
