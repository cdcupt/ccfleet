"""Several Claude accounts on one slot: adding one, switching, removing one.

A slot's holder keeps up to three of their own Claude accounts signed in and
moves between them with one click. Each account is its own Claude Code
directory, chosen with CLAUDE_CONFIG_DIR, and which one is in use is written in
the env file the Remote Control unit reads. These tests run the slot's agent
against a real home directory and a stand-in for its processes, so what they
check is what would be on disk and what would have been run.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccfleet_agent import agent

NOW = 1_800_000_000.0
LATER_MS = int((NOW + 20 * 86400) * 1000)     # a sign-in good for twenty more days
GONE_MS = int((NOW - 86400) * 1000)           # one that ran out yesterday
RC = "claude-remote-control.service"
RESTART = ["systemctl", "--user", "restart", RC]
URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A slot's home as slot-add.sh leaves it, with nobody signed in yet."""
    h = tmp_path / "slot01"
    (h / ".config" / "ccfleet").mkdir(parents=True)
    (h / ".claude").mkdir()
    (h / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(agent, "find_claude", lambda: str(h / ".local/bin/claude"))
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)
    return h


def place(home, account_id):
    """Where an account lives: its directory, and its .claude.json."""
    if account_id == "1":
        return home / ".claude", home / ".claude.json"
    directory = home / ".config" / "ccfleet" / "claude-accounts" / account_id
    return directory, directory / ".claude.json"


def sign_in(home, account_id, email, *, uuid=None, plan="max", refresh_ms=LATER_MS):
    """What `claude auth login` leaves behind, in one account's directory."""
    directory, global_config = place(home, account_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": f"sk-ant-oat01-SECRET-{account_id}",
        "refreshToken": f"sk-ant-ort01-SECRET-{account_id}",
        "expiresAt": int((NOW + 3600) * 1000), "refreshTokenExpiresAt": refresh_ms,
        "subscriptionType": plan}}))
    known = json.loads(global_config.read_text()) if global_config.exists() else {}
    known["oauthAccount"] = {"emailAddress": email, "accountUuid": uuid or f"uuid-{email}",
                             "organizationName": "Holder Org"}
    global_config.write_text(json.dumps(known))


def env_file(home):
    return home / ".config" / "ccfleet" / "remote-control.env"


def use(home, account_id):
    """Point Remote Control at an account, as an earlier switch would have."""
    env_file(home).write_text(f"CLAUDE_CONFIG_DIR={place(home, account_id)[0]}\n")


class Slot:
    """The slot's processes: Claude Code, as whichever account it is started
    as; systemd for Remote Control; tmux for the sign-in pane."""

    def __init__(self, home, *, rc="active", enabled="enabled", restart_code=0,
                 stop_code=0, broken=(), logout_deletes=True):
        self.home = home
        self.calls = []                 # (argv, the directory Claude Code ran in)
        self.rc = rc
        self.enabled = enabled
        self.restart_code = restart_code
        self.stop_code = stop_code      # None: systemctl hangs and is killed
        self.broken = {str(d) for d in broken}   # sign-ins the CLI itself rejects
        self.logout_deletes = logout_deletes
        self.pane = ""
        self.started = []               # the command each tmux session was opened with

    def directory(self, env):
        value = (env if env is not None else os.environ).get("CLAUDE_CONFIG_DIR")
        return Path(value) if value else self.home / ".claude"

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        directory = self.directory(kwargs.get("env"))
        self.calls.append((argv, str(directory)))

        def done(out="", code=0):
            return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

        credentials = directory / ".credentials.json"
        if argv[1:] == ["auth", "status"]:
            signed = credentials.exists() and str(directory) not in self.broken
            plan = (json.loads(credentials.read_text())["claudeAiOauth"]["subscriptionType"]
                    if signed else None)
            return done(json.dumps({"loggedIn": signed, "subscriptionType": plan,
                                    "email": "cli-said@example.com", "orgName": "CLI Org"}))
        if argv[1:] == ["auth", "logout"]:
            if self.logout_deletes:
                credentials.unlink(missing_ok=True)
            return done(code=0 if self.logout_deletes else 1)
        if argv[1:] == ["--version"]:
            return done("2.1.280 (Claude Code)")
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return done(self.enabled)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return done(self.rc)
        if argv[:3] == ["systemctl", "--user", "restart"]:
            if self.restart_code == 0:
                self.rc = "active"
            return done(code=self.restart_code)
        if argv[:3] == ["systemctl", "--user", "start"]:
            self.rc = "active"
            return done()
        if argv[:3] == ["systemctl", "--user", "stop"]:
            if self.stop_code is None:
                raise subprocess.TimeoutExpired(argv, 60)
            if self.stop_code == 0:
                self.rc = "inactive"
            return done(code=self.stop_code)
        if argv[:1] == ["tmux"]:
            if "new-session" in argv:
                self.started.append(argv[-1])
            if "capture-pane" in argv:
                return done(self.pane)
            return done()
        if argv[:1] == ["pgrep"]:
            return done(code=1)
        return done()

    def systemctl(self, verb):
        return [argv for argv, _ in self.calls if argv[:3] == ["systemctl", "--user", verb]]

    def claude(self, *words):
        """The directories Claude Code was started in, for these words."""
        return [where for argv, where in self.calls if argv[1:] == list(words)]


def command_words(command):
    """The words of a pane's command, without the sleep that holds it open."""
    return shlex.split(command.split(";")[0])


def request(requested_at=42.0, **login):
    return {"login": {"requested_at": requested_at, "kind": "login", **login}}


def intent(action="use", account_id="2", requested_at=7.0):
    return {"account": {"action": action, "id": account_id, "requested_at": requested_at}}


def finish(home, slot, account_id, email, *, uuid=None, **login):
    """A whole sign-in: the link, the code, and what the CLI then writes."""
    agent.slot_facts(request(**login), slot, now=NOW)
    slot.pane = f"If the browser didn't open, visit: {URL}\nPaste code here if prompted >"
    assert agent.slot_facts(request(**login), slot, now=NOW)["login"]["state"] == "url_ready"
    facts = agent.slot_facts(request(code="c0de", **login), slot, now=NOW)
    assert facts["login"]["state"] == "code_sent"
    sign_in(home, account_id, email, uuid=uuid)
    return agent.slot_facts(request(code="c0de", **login), slot, now=NOW)


def listed(facts):
    return [(a["id"], a["active"]) for a in facts["accounts"]]


def slot_state(home):
    return json.loads((home / ".config" / "ccfleet" / "slot-state.json").read_text())


# -- what a slot says about its accounts -----------------------------------------------

def test_each_signed_in_account_is_reported_with_its_address_and_plan(home):
    sign_in(home, "1", "work@example.com", plan="max")
    sign_in(home, "2", "home@example.com", plan="pro")
    use(home, "2")
    facts = agent.slot_facts({}, Slot(home), now=NOW)
    assert facts["accounts"] == [
        {"id": "1", "email": "work@example.com", "plan": "max", "active": False,
         "signed_in": True, "refresh_expires_at": LATER_MS / 1000},
        {"id": "2", "email": "home@example.com", "plan": "pro", "active": True,
         "signed_in": True, "refresh_expires_at": LATER_MS / 1000}]


def test_everything_else_is_about_the_account_in_use(home):
    sign_in(home, "1", "work@example.com", plan="max")
    sign_in(home, "2", "home@example.com", plan="pro")
    use(home, "2")
    facts = agent.slot_facts({}, Slot(home), now=NOW)
    assert facts["credentials"]["logged_in"] is True
    assert facts["credentials"]["subscription_type"] == "pro"


def test_no_claude_code_is_started_for_an_account_not_in_use(home):
    """Asking about the other two costs a read of their files, not a process each."""
    for account_id, email in (("1", "a@example.com"), ("2", "b@example.com"),
                              ("3", "c@example.com")):
        sign_in(home, account_id, email)
    use(home, "3")
    slot = Slot(home)
    agent.slot_facts({}, slot, now=NOW)
    assert slot.claude("auth", "status") == [str(place(home, "3")[0])]


def test_a_lapsed_account_is_reported_signed_out(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "old@example.com", refresh_ms=GONE_MS)
    facts = agent.slot_facts({}, Slot(home), now=NOW)
    assert [(a["id"], a["signed_in"]) for a in facts["accounts"]] == [("1", True), ("2", False)]


def test_the_account_in_use_is_judged_by_claude_code_itself(home):
    sign_in(home, "1", "work@example.com")
    facts = agent.slot_facts({}, Slot(home, broken=[home / ".claude"]), now=NOW)
    assert facts["accounts"][0]["signed_in"] is False


def test_an_address_too_long_to_be_one_is_not_sent_in_part(home):
    sign_in(home, "1", "a" * 250 + "@example.com")
    assert agent.slot_facts({}, Slot(home), now=NOW)["accounts"][0]["email"] == ""


def test_a_slot_nobody_has_signed_into_has_no_accounts(home):
    assert agent.slot_facts({}, Slot(home), now=NOW)["accounts"] == []


def test_only_the_address_leaves_never_a_token_an_organisation_or_an_id(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    flat = json.dumps(agent.slot_facts({}, Slot(home), now=NOW))
    for private in ("sk-ant-oat01", "sk-ant-ort01", "Holder Org", "uuid-", "CLI Org",
                    "cli-said@example.com"):
        assert private not in flat, f"{private} left the slot"


def test_usage_counts_every_account_on_the_slot(home):
    stamp = NOW - 60
    when = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    for account_id, tokens in (("1", 100), ("2", 20)):
        sign_in(home, account_id, f"{account_id}@example.com")
        transcript = place(home, account_id)[0] / "projects" / "w" / "s.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"timestamp": when, "message": {
            "usage": {"input_tokens": tokens}}}) + "\n")
        os.utime(transcript, (stamp, stamp))
    usage = agent.slot_facts({}, Slot(home), now=NOW)["usage"]
    assert usage["total_tokens"] == 120 and usage["sessions"] == 2
    assert sum(usage["by_hour"]["tokens"]) == 120


def test_the_windows_are_read_as_the_account_in_use(home, monkeypatch):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    use(home, "2")
    seen = []
    monkeypatch.setattr(agent, "read_quota", lambda runner, now=None, env_prefix=():
                        seen.append(tuple(env_prefix)) or {"session": {"used_pct": 5}})
    agent.slot_facts({"refresh_quota": True}, Slot(home), now=NOW)
    assert seen == [("env", f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}")]


def test_the_usage_screen_is_opened_as_the_account_named(home):
    slot = Slot(home)
    slot.pane = ("Current session\n███ 12% used\nResets 5pm\n"
                 "Current week (all models)\n██ 40% used\nResets Mon")
    assert agent.read_quota(slot, env_prefix=("env", "CLAUDE_CONFIG_DIR=/x"))["week"] == {
        "used_pct": 40, "resets": "Mon"}
    [command] = slot.started
    assert shlex.split(command) == ["env", "CLAUDE_CONFIG_DIR=/x",
                                    str(home / ".local/bin/claude")]


def test_claude_code_is_upgraded_as_the_account_in_use(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    use(home, "2")
    slot = Slot(home)
    agent.slot_facts({"claude_version": "2.1.300", "may_upgrade": True}, slot, now=NOW)
    assert slot.claude("install", "2.1.300") == [str(place(home, "2")[0])]


# -- signing another account in --------------------------------------------------------

def test_another_account_signs_in_to_a_private_place_of_its_own(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home)
    facts = agent.slot_facts(request(account="new"), slot, now=NOW)
    assert facts["login"] == {"state": "requested", "requested_at": 42.0}
    directory, global_config = place(home, "2")
    [command] = slot.started
    assert command_words(command) == ["env", f"CLAUDE_CONFIG_DIR={directory}",
                                      str(home / ".local/bin/claude"), "auth", "login",
                                      "--claudeai"]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(global_config.stat().st_mode) == 0o600
    # Past the prompts nobody could answer, or Remote Control would wait on one.
    seeded = json.loads(global_config.read_text())
    assert seeded["hasCompletedOnboarding"] is True and seeded["remoteDialogSeen"] is True
    assert seeded["projects"][str(home / "workspace")]["hasTrustDialogAccepted"] is True


def test_the_account_in_use_goes_on_working_while_another_signs_in(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home)
    agent.slot_facts(request(account="new"), slot, now=NOW)
    slot.pane = f"visit: {URL}\n"
    agent.slot_facts(request(account="new"), slot, now=NOW)
    assert not env_file(home).exists()
    assert slot.systemctl("restart") == []


def test_a_finished_sign_in_becomes_the_account_in_use_and_remote_control_follows(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home)
    facts = finish(home, slot, "2", "home@example.com", account="new")
    assert facts["login"] == {"state": "done", "requested_at": 42.0}
    assert env_file(home).read_text() == f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}\n"
    assert slot.systemctl("restart") == [RESTART]
    assert listed(facts) == [("1", False), ("2", True)]


def test_signing_in_again_moves_remote_control_onto_the_new_sign_in(home):
    """The bug this fixes. Remote Control was only ever started, never
    restarted, so signing in again as somebody else under a running one left
    the slot serving the old account."""
    sign_in(home, "1", "old@example.com")
    slot = Slot(home, rc="active")
    facts = finish(home, slot, "1", "new@example.com")
    assert facts["login"]["state"] == "done"
    assert slot.systemctl("restart") == [RESTART]
    assert [a["email"] for a in facts["accounts"]] == ["new@example.com"]


def test_a_sign_in_is_not_done_until_it_has_written_a_new_sign_in(home):
    """Signing in again while the old sign-in still works: the CLI says
    "signed in" from the first moment, about the old one. Only a new credential
    ends it — otherwise the pane was closed before the new one was written."""
    sign_in(home, "1", "me@example.com")
    slot = Slot(home)
    agent.slot_facts(request(), slot, now=NOW)
    slot.pane = f"visit: {URL}\n"
    agent.slot_facts(request(), slot, now=NOW)
    agent.slot_facts(request(code="c0de"), slot, now=NOW)
    facts = agent.slot_facts(request(code="c0de"), slot, now=NOW)
    assert "login" not in facts, "called done on the sign-in that was already there"
    assert slot.systemctl("restart") == []
    sign_in(home, "1", "me@example.com")
    assert agent.slot_facts(request(code="c0de"), slot, now=NOW)["login"]["state"] == "done"


def test_a_fourth_account_is_refused_and_nothing_is_started(home):
    for account_id, email in (("1", "a@example.com"), ("2", "b@example.com"),
                              ("3", "c@example.com")):
        sign_in(home, account_id, email)
    slot = Slot(home)
    facts = agent.slot_facts(request(account="new"), slot, now=NOW)
    assert facts["login"] == {"state": "failed", "detail": "this slot already has 3 accounts",
                              "requested_at": 42.0}
    assert slot.started == []
    # Said once. A server still offering it does not get it tried again.
    assert "login" not in agent.slot_facts(request(account="new"), slot, now=NOW)
    assert slot.started == []


def test_a_new_account_takes_the_first_place_free(home):
    sign_in(home, "2", "b@example.com")
    use(home, "2")
    slot = Slot(home)
    agent.slot_facts(request(account="new"), slot, now=NOW)
    [command] = slot.started
    assert command_words(command)[0] == str(home / ".local/bin/claude"), \
        "the first account's place was free, and was passed over"


def test_a_named_account_signs_in_again_where_it_is(home):
    sign_in(home, "1", "a@example.com")
    sign_in(home, "3", "c@example.com", refresh_ms=GONE_MS)
    slot = Slot(home)
    agent.slot_facts(request(account="3"), slot, now=NOW)
    [command] = slot.started
    assert command_words(command)[:2] == ["env", f"CLAUDE_CONFIG_DIR={place(home, '3')[0]}"]


@pytest.mark.parametrize("account", [None, "4", "../2"])
def test_a_sign_in_naming_no_account_goes_to_the_one_in_use(home, account):
    """What a server that predates accounts sends, and what a word nobody knows means."""
    sign_in(home, "1", "a@example.com")
    sign_in(home, "2", "b@example.com")
    use(home, "2")
    slot = Slot(home)
    login = {} if account is None else {"account": account}
    agent.slot_facts(request(**login), slot, now=NOW)
    [command] = slot.started
    assert command_words(command)[:2] == ["env", f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}"]


def test_the_first_account_signs_in_exactly_as_a_slot_always_did(home):
    slot = Slot(home)
    agent.slot_facts(request(), slot, now=NOW)
    [command] = slot.started
    assert command_words(command) == [str(home / ".local/bin/claude"), "auth", "login",
                                      "--claudeai"]


def test_a_device_token_is_minted_by_the_account_in_use(home):
    sign_in(home, "1", "a@example.com")
    sign_in(home, "2", "b@example.com")
    use(home, "2")
    slot = Slot(home)
    agent.slot_facts({"login": {"requested_at": 1.0, "kind": "token", "account": "new"}},
                     slot, now=NOW)
    [command] = slot.started
    assert command_words(command) == ["env", f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}",
                                      str(home / ".local/bin/claude"), "setup-token"]
    assert not place(home, "3")[0].exists(), "a token made a place for an account"


def test_the_same_account_signed_in_twice_keeps_only_the_new_sign_in(home):
    sign_in(home, "1", "me@example.com", uuid="U")
    (home / ".claude" / "settings.json").write_text("{}")
    slot = Slot(home)
    facts = finish(home, slot, "2", "me@example.com", uuid="U", account="new")
    assert not (home / ".claude" / ".credentials.json").exists()
    assert (home / ".claude" / "settings.json").exists(), "the slot's settings went with it"
    assert listed(facts) == [("2", True)]


def test_the_other_copy_stays_until_remote_control_has_moved_off_it(home):
    """Remote Control would not restart, so it may still be running as the old
    copy: that copy's sign-in is kept until a restart has actually happened."""
    sign_in(home, "1", "me@example.com", uuid="U")
    slot = Slot(home, restart_code=1)
    facts = finish(home, slot, "2", "me@example.com", uuid="U", account="new")
    assert facts["login"]["state"] == "done"
    assert (home / ".claude" / ".credentials.json").exists(), \
        "deleted the sign-in Remote Control may still be running as"
    slot.restart_code = 0
    facts = agent.slot_facts({}, slot, now=NOW)
    assert not (home / ".claude" / ".credentials.json").exists()
    assert listed(facts) == [("2", True)]


def test_a_second_copy_in_a_place_of_its_own_is_removed_whole(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "me@example.com", uuid="U")
    slot = Slot(home)
    facts = finish(home, slot, "3", "me@example.com", uuid="U", account="new")
    assert not place(home, "2")[0].exists()
    assert listed(facts) == [("1", False), ("3", True)]


def test_a_restart_that_failed_after_a_sign_in_is_tried_again(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home, restart_code=1)
    assert finish(home, slot, "2", "home@example.com",
                  account="new")["login"]["state"] == "done"
    assert slot_state(home)["account_restart"] == "owed"
    slot.restart_code = 0
    agent.slot_facts({}, slot, now=NOW)
    assert len(slot.systemctl("restart")) == 2
    assert "account_restart" not in slot_state(home)


# -- switching --------------------------------------------------------------------------

def test_one_click_moves_remote_control_to_another_signed_in_account(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    state = home / ".config" / "ccfleet" / "slot-state.json"
    state.write_text(json.dumps({"quota": {"week": {"used_pct": 97}, "ts": NOW - 5}}))
    slot = Slot(home)
    facts = agent.slot_facts(intent(), slot, now=NOW)
    assert facts["account_switch"] == {"requested_at": 7.0, "state": "done", "detail": ""}
    assert env_file(home).read_text() == f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}\n"
    assert stat.S_IMODE(env_file(home).stat().st_mode) == 0o600
    assert slot.systemctl("restart") == [RESTART]
    assert listed(facts) == [("1", False), ("2", True)]
    assert "quota" not in facts, "showed the last account's windows as this one's"


def test_a_switch_keeps_the_rest_of_the_env_file(home):
    kept = "CCFLEET_RC_ARGS=--permission-mode bypassPermissions\n# set by install.sh\n"
    env_file(home).write_text(kept)
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    slot = Slot(home)
    agent.slot_facts(intent(), slot, now=NOW)
    assert env_file(home).read_text() == kept + f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}\n"
    agent.slot_facts(intent(account_id="1", requested_at=8.0), slot, now=NOW)
    assert env_file(home).read_text() == kept


@pytest.mark.parametrize("target", ["3", "2"])    # never signed in; ran out yesterday
def test_switching_to_an_account_that_is_not_signed_in_changes_nothing(home, target):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "old@example.com", refresh_ms=GONE_MS)
    slot = Slot(home)
    facts = agent.slot_facts(intent(account_id=target), slot, now=NOW)
    assert facts["account_switch"] == {"requested_at": 7.0, "state": "failed",
                                       "detail": "that account is not signed in on this slot"}
    assert not env_file(home).exists()
    assert slot.systemctl("restart") == []


def test_a_sign_in_that_stopped_working_is_refused_before_anything_moves(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    slot = Slot(home, broken=[place(home, "2")[0]])
    facts = agent.slot_facts(intent(), slot, now=NOW)
    assert facts["account_switch"]["state"] == "failed"
    assert "sign in again" in facts["account_switch"]["detail"]
    assert not env_file(home).exists()
    assert slot.systemctl("restart") == []


@pytest.mark.parametrize("before", [None, "CCFLEET_RC_ARGS=x\n"])
def test_remote_control_that_will_not_restart_puts_the_previous_account_back(home, before):
    if before is not None:
        env_file(home).write_text(before)
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    slot = Slot(home, restart_code=1)
    facts = agent.slot_facts(intent(), slot, now=NOW)
    assert facts["account_switch"]["state"] == "failed"
    if before is None:
        assert not env_file(home).exists()
    else:
        assert env_file(home).read_text() == before
    assert listed(facts) == [("1", True), ("2", False)]


def test_switching_to_the_account_already_in_use_restarts_nothing(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home)
    facts = agent.slot_facts(intent(account_id="1"), slot, now=NOW)
    assert facts["account_switch"]["state"] == "done"
    assert slot.systemctl("restart") == []


def test_a_switch_is_made_once_and_repeated_until_the_server_hears_it(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    slot = Slot(home)
    first = agent.slot_facts(intent(), slot, now=NOW)["account_switch"]
    again = agent.slot_facts(intent(), slot, now=NOW)["account_switch"]
    assert again == first
    assert len(slot.systemctl("restart")) == 1, "switched twice for one click"
    assert "account_switch" not in agent.slot_facts({}, slot, now=NOW)


@pytest.mark.parametrize("raw", [
    {"action": "use", "id": "4", "requested_at": 1.0},
    {"action": "use", "id": "../2", "requested_at": 1.0},
    {"action": "use", "id": 2, "requested_at": 1.0},
    {"action": "wipe", "id": "2", "requested_at": 1.0},
    {"action": "forget", "id": "2", "requested_at": True},
    {"action": "forget", "id": "2"},
    "forget 2",
])
def test_an_intent_the_agent_does_not_recognise_does_nothing(home, raw):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    slot = Slot(home)
    facts = agent.slot_facts({"account": raw}, slot, now=NOW)
    assert "account_switch" not in facts
    assert (place(home, "2")[0] / ".credentials.json").exists()
    assert not env_file(home).exists()


# -- removing one ------------------------------------------------------------------------

def test_removing_an_account_not_in_use_signs_it_out_and_deletes_its_place(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    slot = Slot(home)
    facts = agent.slot_facts(intent("forget", "2"), slot, now=NOW)
    assert facts["account_switch"] == {"requested_at": 7.0, "state": "done", "detail": ""}
    assert slot.claude("auth", "logout") == [str(place(home, "2")[0])]
    assert not place(home, "2")[0].exists()
    assert slot.systemctl("stop") == [] and slot.systemctl("restart") == []
    assert listed(facts) == [("1", True)]


def test_the_first_accounts_directory_is_never_deleted(home):
    """It holds the slot's settings and history as well as a sign-in."""
    sign_in(home, "1", "work@example.com")
    (home / ".claude" / "settings.json").write_text("{}")
    (home / ".claude" / "projects").mkdir()
    agent.slot_facts(intent("forget", "1"), Slot(home), now=NOW)
    assert (home / ".claude" / "settings.json").exists()
    assert (home / ".claude" / "projects").is_dir()
    assert not (home / ".claude" / ".credentials.json").exists()


def test_removing_the_account_in_use_moves_to_the_next_one_still_signed_in(home):
    sign_in(home, "1", "a@example.com")
    sign_in(home, "2", "b@example.com")
    sign_in(home, "3", "c@example.com")
    use(home, "2")
    slot = Slot(home)
    facts = agent.slot_facts(intent("forget", "2"), slot, now=NOW)
    order = [argv[:3] for argv, _ in slot.calls]
    # Nothing goes on running as an account that is being removed.
    assert order.index(["systemctl", "--user", "stop"]) < order.index(
        [str(home / ".local/bin/claude"), "auth", "logout"])
    assert "CLAUDE_CONFIG_DIR" not in env_file(home).read_text()
    assert slot.systemctl("start"), "Remote Control was left stopped"
    assert listed(facts) == [("1", True), ("3", False)]


@pytest.mark.parametrize("stop_code", [1, None])      # refused; hung and killed
def test_the_account_in_use_is_not_removed_while_remote_control_may_still_run_as_it(
        home, stop_code):
    """Deleting the sign-in under a Remote Control that did not stop would
    leave it serving an account its holder has just removed."""
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    use(home, "2")
    slot = Slot(home, stop_code=stop_code)
    facts = agent.slot_facts(intent("forget", "2"), slot, now=NOW)
    assert facts["account_switch"] == {"requested_at": 7.0, "state": "failed",
                                       "detail": "Remote Control did not stop; nothing was "
                                                 "removed"}
    assert slot.claude("auth", "logout") == []
    assert (place(home, "2")[0] / ".credentials.json").exists()
    assert env_file(home).read_text() == f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}\n"


def test_the_next_account_is_one_claude_code_itself_still_accepts(home):
    """Its files say it is signed in, but the CLI says it no longer works:
    the one after it is taken instead."""
    sign_in(home, "1", "revoked@example.com")
    sign_in(home, "2", "b@example.com")
    sign_in(home, "3", "c@example.com")
    use(home, "2")
    facts = agent.slot_facts(intent("forget", "2"), Slot(home, broken=[home / ".claude"]),
                             now=NOW)
    assert env_file(home).read_text() == f"CLAUDE_CONFIG_DIR={place(home, '3')[0]}\n"
    assert listed(facts) == [("1", False), ("3", True)]


def test_nothing_is_removed_when_the_move_off_it_cannot_be_recorded(home):
    """The line is written before anything is deleted: failing there leaves the
    account, its sign-in and Remote Control as they were."""
    sign_in(home, "1", "a@example.com")
    sign_in(home, "2", "b@example.com")
    use(home, "2")
    slot = Slot(home)
    config = home / ".config" / "ccfleet"
    config.chmod(0o500)
    try:
        facts = agent.slot_facts(intent("forget", "2"), slot, now=NOW)
    finally:
        config.chmod(0o700)
    assert facts["account_switch"] == {"requested_at": 7.0, "state": "failed",
                                       "detail": "could not record the switch; nothing was "
                                                 "removed"}
    assert (place(home, "2")[0] / ".credentials.json").exists()
    assert slot.claude("auth", "logout") == []
    assert env_file(home).read_text() == f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}\n"
    assert slot.rc == "active", "left Remote Control stopped"


def test_the_removed_accounts_windows_go_with_it(home):
    sign_in(home, "1", "a@example.com")
    sign_in(home, "2", "b@example.com")
    use(home, "2")
    (home / ".config" / "ccfleet" / "slot-state.json").write_text(json.dumps(
        {"quota": {"week": {"used_pct": 97}, "ts": NOW - 5}}))
    facts = agent.slot_facts(intent("forget", "2"), Slot(home), now=NOW)
    assert "quota" not in facts, "showed the removed account's windows as the next one's"


def test_the_next_account_is_one_that_still_works(home):
    sign_in(home, "1", "old@example.com", refresh_ms=GONE_MS)
    sign_in(home, "2", "b@example.com")
    sign_in(home, "3", "c@example.com")
    use(home, "2")
    facts = agent.slot_facts(intent("forget", "2"), Slot(home), now=NOW)
    assert env_file(home).read_text() == f"CLAUDE_CONFIG_DIR={place(home, '3')[0]}\n"
    assert listed(facts) == [("1", False), ("3", True)]


def test_removing_the_last_account_leaves_remote_control_stopped(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home)
    facts = agent.slot_facts(intent("forget", "1"), slot, now=NOW)
    assert slot.systemctl("stop") == [["systemctl", "--user", "stop", RC]]
    assert slot.systemctl("start") == [] and slot.rc == "inactive"
    assert facts["accounts"] == []


def test_a_sign_out_that_fails_still_removes_the_sign_in_from_this_slot(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    facts = agent.slot_facts(intent("forget", "2"), Slot(home, logout_deletes=False),
                             now=NOW)
    assert facts["account_switch"]["state"] == "done"
    assert not place(home, "2")[0].exists()


def test_a_removal_that_cannot_delete_says_so_and_keeps_remote_control(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home, logout_deletes=False)
    (home / ".claude").chmod(0o500)
    try:
        facts = agent.slot_facts(intent("forget", "1"), slot, now=NOW)
    finally:
        (home / ".claude").chmod(0o700)
    assert facts["account_switch"] == {"requested_at": 7.0, "state": "failed",
                                       "detail": "could not remove that account"}
    assert slot.systemctl("start"), "left stopped although the account is still there"


# -- the process as the machine starts it ------------------------------------------------

def test_a_directory_inherited_from_the_caller_is_not_the_account_in_use(home, monkeypatch,
                                                                         tmp_path):
    sign_in(home, "1", "work@example.com")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(place(home, "3")[0]))
    monkeypatch.setattr(agent.os, "geteuid", lambda: 1001)
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    assert agent.slot_facts_main(io.StringIO("{}"), out, Slot(home)) == 0
    facts = json.loads(out.getvalue())
    assert facts["credentials"]["logged_in"] is True
    assert listed(facts) == [("1", True)]


# -- the edges ----------------------------------------------------------------------------

def test_a_directory_pointed_at_by_hand_is_what_the_slot_reports_on(home, tmp_path):
    """The holder can edit their own env file. Claude Code then runs as that
    directory, so the facts are about it — but it is none of the three."""
    sign_in(home, "1", "work@example.com")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env_file(home).write_text(f"CLAUDE_CONFIG_DIR={elsewhere}\n")
    slot = Slot(home)
    facts = agent.slot_facts({}, slot, now=NOW)
    assert slot.claude("auth", "status") == [str(elsewhere)]
    assert facts["credentials"]["logged_in"] is False
    assert listed(facts) == [("1", False)]


def test_an_env_file_that_cannot_be_read_is_never_written_over(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    env_file(home).write_text("CCFLEET_RC_ARGS=--keep-me\n")
    env_file(home).chmod(0)
    slot = Slot(home)
    try:
        facts = agent.slot_facts(intent(), slot, now=NOW)
    finally:
        env_file(home).chmod(0o600)
    assert facts["account_switch"] == {"requested_at": 7.0, "state": "failed",
                                       "detail": "could not record the switch"}
    assert env_file(home).read_text() == "CCFLEET_RC_ARGS=--keep-me\n"
    assert slot.systemctl("restart") == []


def test_a_sign_in_that_cannot_be_switched_to_leaves_remote_control_alone(home):
    sign_in(home, "1", "work@example.com")
    slot = Slot(home)
    agent.slot_facts(request(account="new"), slot, now=NOW)
    slot.pane = f"visit: {URL}\n"
    agent.slot_facts(request(account="new"), slot, now=NOW)
    agent.slot_facts(request(code="c0de", account="new"), slot, now=NOW)
    sign_in(home, "2", "home@example.com")
    env_file(home).write_text("")
    env_file(home).chmod(0)
    try:
        facts = agent.slot_facts(request(code="c0de", account="new"), slot, now=NOW)
    finally:
        env_file(home).chmod(0o600)
    assert facts["login"]["state"] == "done", "the sign-in itself worked"
    assert slot.systemctl("restart") == [], "restarted onto the account still recorded"


def test_an_account_place_that_is_a_link_is_unlinked_not_followed(home, tmp_path):
    sign_in(home, "1", "work@example.com")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not the slot's to delete")
    link = place(home, "2")[0]
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere, target_is_directory=True)
    sign_in(home, "2", "home@example.com")
    facts = agent.slot_facts(intent("forget", "2"), Slot(home), now=NOW)
    assert facts["account_switch"]["state"] == "done"
    assert not link.exists() and not link.is_symlink()
    assert (elsewhere / "keep.txt").exists()


def test_remote_control_switched_off_on_purpose_is_not_started_by_a_switch(home):
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    slot = Slot(home, rc="inactive", enabled="disabled")
    facts = agent.slot_facts(intent(), slot, now=NOW)
    assert facts["account_switch"]["state"] == "done"
    assert env_file(home).read_text() == f"CLAUDE_CONFIG_DIR={place(home, '2')[0]}\n"
    assert slot.systemctl("restart") == [] and slot.systemctl("start") == []


def test_a_switch_is_the_restart_an_upgrade_was_waiting_for(home):
    """Remote Control owed a restart onto a new version, and a switch has just
    given it one: it is not restarted a second time."""
    sign_in(home, "1", "work@example.com")
    sign_in(home, "2", "home@example.com")
    (home / ".config" / "ccfleet" / "slot-state.json").write_text(json.dumps({
        "restart": "waiting",
        "upgrade": {"from": "2.1.278", "to": "2.1.280", "ok": True, "ts": NOW - 100}}))
    slot = Slot(home)
    facts = agent.slot_facts({**intent(), "claude_version": "2.1.280", "may_upgrade": True},
                             slot, now=NOW)
    assert facts["upgrade"]["restart"] == "done"
    assert slot.systemctl("restart") == [RESTART]


def test_a_new_sign_in_that_cannot_start_still_ends_the_one_it_replaces(home):
    for account_id, email in (("1", "a@example.com"), ("2", "b@example.com"),
                              ("3", "c@example.com")):
        sign_in(home, account_id, email)
    (home / ".config" / "ccfleet" / "slot-state.json").write_text(json.dumps({
        "login": {"requested_at": 1.0, "phase": "url_ready", "kind": "login", "target": "3",
                  "target_dir": str(place(home, "3")[0]), "before": None}}))
    slot = Slot(home)
    facts = agent.slot_facts(request(2.0, account="new"), slot, now=NOW)
    assert facts["login"]["state"] == "failed"
    assert ["tmux", "-L", "ccfleet-login", "kill-session", "-t", "login"] in [
        argv for argv, _ in slot.calls]
