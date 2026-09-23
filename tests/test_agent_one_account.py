"""A slot's one Claude account: the agent's half.

One Claude account, one node — and on a shared machine the node is the slot.
The agent keeps it in ~/.claude, where Claude Code always keeps it, and never
points Claude Code anywhere else. What it adds to what an owner node reports is
the account's address and how long its sign-in lasts, for the holder's page.
Two behaviours from the short-lived several-accounts agent stay, because they
are about any sign-in: Remote Control moves onto a sign-in the moment it
finishes, and a sign-in only counts as finished once the credential it writes
is really there.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess

import pytest

from ccfleet_agent import agent

from . import test_agent

# The same signed-in slot home the other slot tests use (a fixture).
slot_home = test_agent.slot_home

NOW = 1_800_000_000.0
DAY = 86400


class SlotFake:
    """The commands a slot's agent runs, answered the way a slot answers them.

    tmux serves a pane, `claude auth status` whatever `auth` says, and
    systemctl what Remote Control is doing. Typing a code into the sign-in
    pane writes a new credential — what Claude Code does when a code works.
    """

    def __init__(self, home, *, rc="active", enabled="enabled", restart_rc=0,
                 logged_in=True):
        self.home, self.rc, self.enabled, self.restart_rc = home, rc, enabled, restart_rc
        self.logged_in, self.pane, self.calls = logged_in, "", []
        self.writes_on_code = True

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        out, code = "", 0
        if argv[0] == "tmux" and "capture-pane" in argv:
            out = self.pane
        elif argv[0] == "tmux" and "send-keys" in argv and argv[-1] != "Enter":
            if self.writes_on_code:
                write_credentials(self.home, "sk-ant-oat01-NEW", expires_ms=int(NOW + 30 * DAY) * 1000)
        elif argv[1:3] == ["auth", "status"]:
            out = json.dumps({"loggedIn": self.logged_in, "authMethod": "claude.ai",
                              "subscriptionType": "max"})
        elif argv[1:] == ["--version"]:
            out = "2.1.278 (Claude Code)"
        elif argv[:3] == ["systemctl", "--user", "is-active"]:
            out = self.rc
        elif argv[:3] == ["systemctl", "--user", "is-enabled"]:
            out = self.enabled
        elif argv[:3] == ["systemctl", "--user", "restart"]:
            code = self.restart_rc
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    def restarts(self):
        return [a for a in self.calls if a[:3] == ["systemctl", "--user", "restart"]]


def write_credentials(home, access, *, expires_ms):
    path = home / ".claude" / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": "sk-ant-ort01-SECRET",
        "expiresAt": 1_900_000_000_000, "refreshTokenExpiresAt": expires_ms,
        "subscriptionType": "max"}}))


def state_file(home):
    return home / ".config" / "ccfleet" / "slot-state.json"


def walk_to_code_sent(fake, requested_at=100.0):
    """A sign-in from the holder's page, taken to the point where their code
    has been typed. Returns what each step reported."""
    wanted = {"requested_at": requested_at, "email": "holder@example.com"}
    said = [agent.slot_facts({"login": wanted}, fake, now=NOW).get("login")]
    fake.pane = "Visit https://claude.com/cai/oauth/authorize?code=true&x=1 to continue"
    said.append(agent.slot_facts({"login": wanted}, fake, now=NOW).get("login"))
    said.append(agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake,
                                 now=NOW).get("login"))
    return wanted, said


# -- which account, and for how long --------------------------------------------------

def test_a_slot_says_which_account_it_is_and_how_long_its_sign_in_lasts(slot_home):
    write_credentials(slot_home, "sk-ant-oat01-X", expires_ms=int(NOW + 28 * DAY) * 1000)
    creds = agent.slot_facts({}, SlotFake(slot_home), now=NOW)["credentials"]
    assert creds["email"] == "holder@example.com"
    assert creds["refresh_expires_at"] == NOW + 28 * DAY, "sent in seconds, not milliseconds"


def test_an_address_too_long_to_be_one_is_not_sent_at_all(slot_home):
    long_one = "a" * 250 + "@example.com"
    (slot_home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": long_one}}))
    assert agent.slot_account_labels(slot_home / ".claude")["email"] == ""


def test_a_slot_nobody_signed_into_names_nobody(tmp_path):
    labels = agent.slot_account_labels(tmp_path / ".claude")
    assert labels == {"email": "", "refresh_expires_at": None}


@pytest.mark.parametrize("raw", [True, "soon", 0, -5, None])
def test_an_expiry_claude_code_did_not_write_as_a_time_is_not_sent(slot_home, raw):
    path = slot_home / ".claude" / ".credentials.json"
    data = json.loads(path.read_text())
    data["claudeAiOauth"]["refreshTokenExpiresAt"] = raw
    path.write_text(json.dumps(data))
    assert agent.slot_account_labels(slot_home / ".claude")["refresh_expires_at"] is None


def test_nothing_else_about_the_account_leaves_with_its_address(slot_home):
    facts = agent.slot_facts({}, SlotFake(slot_home), now=NOW)
    blob = json.dumps(facts)
    for private in ("sk-ant-oat01", "sk-ant-ort01", "Holder Org"):
        assert private not in blob


# -- a sign-in, finished --------------------------------------------------------------

def test_a_sign_in_is_finished_only_once_its_credential_is_written(slot_home):
    """The slot is still signed in with its old credential while the new code
    is being typed, so "the CLI says signed in" is true too early. Finished is
    that, AND a credential file that is not the one there when it began."""
    fake = SlotFake(slot_home)
    fake.writes_on_code = False
    wanted, said = walk_to_code_sent(fake)
    assert [s["state"] for s in said] == ["requested", "url_ready", "code_sent"]
    assert agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake,
                            now=NOW).get("login") is None, "done before anything was written"
    write_credentials(slot_home, "sk-ant-oat01-NEW", expires_ms=int(NOW + 30 * DAY) * 1000)
    done = agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake, now=NOW)
    assert done["login"] == {"state": "done", "requested_at": 100.0}


def test_remote_control_moves_onto_a_finished_sign_in_even_while_running(slot_home):
    """It used to be started only when it was not running, so signing in again
    under a running one left it serving the sign-in from before."""
    fake = SlotFake(slot_home, rc="active")
    wanted, _ = walk_to_code_sent(fake)
    assert fake.restarts() == []
    agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake, now=NOW)
    assert fake.restarts() == [["systemctl", "--user", "restart",
                                agent.DEFAULT_RC_SERVICE]]


def test_a_finished_sign_in_drops_the_old_windows_and_counts_as_the_owed_restart(slot_home):
    state_file(slot_home).parent.mkdir(parents=True)
    state_file(slot_home).write_text(json.dumps({
        "quota": {"session": {"used_pct": 90}, "ts": NOW}, "restart": "waiting"}))
    fake = SlotFake(slot_home)
    wanted, _ = walk_to_code_sent(fake)
    agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake, now=NOW)
    kept = json.loads(state_file(slot_home).read_text())
    assert "quota" not in kept, "windows read before the sign-in were kept"
    assert "restart" not in kept, "a version restart stayed owed after one happened"


def test_a_restart_that_fails_is_owed_and_tried_again(slot_home):
    fake = SlotFake(slot_home, restart_rc=1)
    wanted, _ = walk_to_code_sent(fake)
    agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake, now=NOW)
    assert json.loads(state_file(slot_home).read_text())["account_restart"] == "owed"
    fake.restart_rc = 0
    agent.slot_facts({}, fake, now=NOW)
    assert len(fake.restarts()) == 2
    assert "account_restart" not in json.loads(state_file(slot_home).read_text())


def test_remote_control_switched_off_and_stopped_is_left_that_way(slot_home):
    fake = SlotFake(slot_home, rc="inactive", enabled="disabled")
    wanted, _ = walk_to_code_sent(fake)
    agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake, now=NOW)
    assert fake.restarts() == []
    assert "account_restart" not in json.loads(state_file(slot_home).read_text())


def test_switched_off_but_still_running_is_restarted_all_the_same(slot_home):
    """Disabling a unit does not stop it."""
    fake = SlotFake(slot_home, rc="active", enabled="disabled")
    wanted, _ = walk_to_code_sent(fake)
    agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake, now=NOW)
    assert len(fake.restarts()) == 1


def test_a_device_token_is_not_a_sign_in_and_moves_nothing(slot_home):
    fake = SlotFake(slot_home)
    wanted = {"requested_at": 7.0, "kind": "token"}
    agent.slot_facts({"login": wanted}, fake, now=NOW)
    fake.pane = "https://claude.com/cai/oauth/authorize?code=true"
    agent.slot_facts({"login": wanted}, fake, now=NOW)
    agent.slot_facts({"login": {**wanted, "code": "c"}}, fake, now=NOW)
    fake.pane = "Your token: sk-ant-oat01-" + "A" * 80
    said = agent.slot_facts({"login": {**wanted, "code": "c"}}, fake, now=NOW)
    assert said["login"]["state"] == "ready"
    assert fake.restarts() == []


# -- ~/.claude, and nowhere else ---------------------------------------------------------

def other_account(home):
    """What the several-accounts agent left behind on a slot that switched:
    a second account's directory, and the env line running Remote Control as it."""
    place = home / ".config" / "ccfleet" / "claude-accounts" / "2"
    place.mkdir(parents=True)
    (place / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": "someone.else@example.org"}}))
    (place / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-OTHER", "subscriptionType": "pro"}}))
    env = home / ".config" / "ccfleet" / "remote-control.env"
    env.write_text(f"CCFLEET_RC_ARGS=--verbose\nCLAUDE_CONFIG_DIR={place}\n")
    return place, env


def test_remote_control_is_taken_off_another_directory_and_back_onto_claude(slot_home):
    place, env = other_account(slot_home)
    fake = SlotFake(slot_home)
    facts = agent.slot_facts({}, fake, now=NOW)
    assert env.read_text() == "CCFLEET_RC_ARGS=--verbose\n", "the other lines must stay"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert len(fake.restarts()) == 1, "Remote Control went on as the other account"
    assert facts["credentials"]["email"] == "holder@example.com"


def test_the_other_directory_is_neither_read_nor_deleted(slot_home):
    place, _ = other_account(slot_home)
    before = sorted(p.name for p in place.iterdir())
    blob = json.dumps(agent.slot_facts({}, SlotFake(slot_home), now=NOW))
    assert "someone.else" not in blob and "sk-ant-oat01-OTHER" not in blob
    assert sorted(p.name for p in place.iterdir()) == before


def test_an_env_file_without_that_line_is_left_exactly_as_it_is(slot_home):
    env = slot_home / ".config" / "ccfleet" / "remote-control.env"
    env.parent.mkdir(parents=True)
    env.write_text("# mine\nCCFLEET_RC_ARGS=--verbose\n")
    fake = SlotFake(slot_home)
    agent.slot_facts({}, fake, now=NOW)
    assert env.read_text() == "# mine\nCCFLEET_RC_ARGS=--verbose\n"
    assert fake.restarts() == []


def test_a_directory_inherited_from_whoever_started_the_agent_is_dropped(slot_home,
                                                                          monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
    seen = []

    def run(argv, **kwargs):
        seen.append(os.environ.get("CLAUDE_CONFIG_DIR"))
        return SlotFake(slot_home)(argv, **kwargs)

    out = io.StringIO()
    assert agent.slot_facts_main(io.StringIO("{}"), out, run) == 0
    assert seen and set(seen) == {None}
    assert json.loads(out.getvalue())["credentials"]["email"] == "holder@example.com"
