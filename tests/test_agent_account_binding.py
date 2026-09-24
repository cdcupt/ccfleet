"""A slot keeps its Claude account, and every node says which it has: the agent's half.

One Claude account, one node. A slot keeps the account it was first signed in
with; signing in again happens in a scratch directory, and only the same
account's fresh credential replaces the old one in ~/.claude. Another account's
is thrown away, and the sign-in the slot had is never touched. Every node and
slot reports a fingerprint of its account — a digest of the id, never the id —
so the server can see one account on two nodes.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
from pathlib import Path

import pytest

from ccfleet_agent import agent

from .test_agent import FakeResponse

NOW = 1_800_000_000.0
MINE, THEIRS = "uuid-mine-0001", "uuid-theirs-0002"


def fp_of(uuid):
    return hashlib.sha256(uuid.encode()).hexdigest()[:16]


def write_account(place_dir, global_config, uuid, access):
    """A Claude Code config signed in as `uuid`: a credential and the account block."""
    place_dir.mkdir(parents=True, exist_ok=True)
    (place_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": "sk-ant-ort01-SECRET",
        "expiresAt": 1_900_000_000_000, "subscriptionType": "max"}}))
    global_config.write_text(json.dumps({"hasCompletedOnboarding": True, "oauthAccount": {
        "accountUuid": uuid, "emailAddress": "holder@example.com"}}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "slot01"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agent, "find_claude", lambda: str(home / ".local/bin/claude"))
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    return home


def signed_in_as(home, uuid, access="sk-ant-oat01-OLD"):
    write_account(home / ".claude", home / ".claude.json", uuid, access)


def scratch(home):
    return home / ".config" / "ccfleet" / "signin-scratch"


def state(home):
    path = home / ".config" / "ccfleet" / "slot-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


class Slot:
    """The commands a slot's agent runs, answered the way a slot answers them.

    A sign-in runs wherever its command says — ~/.claude, or the directory
    named with `env CLAUDE_CONFIG_DIR=…` — and typing the code there signs in
    as `signs_in_as`. `claude auth status` answers for the directory its
    CLAUDE_CONFIG_DIR names, or ~/.claude.
    """

    def __init__(self, home, signs_in_as=MINE, *, says_whose=True):
        self.home, self.signs_in_as, self.says_whose = home, signs_in_as, says_whose
        self.pane, self.calls, self.place = "", [], None

    def _config(self, env):
        where = (env or {}).get(agent.CONFIG_DIR_VAR)
        return (Path(where), Path(where) / ".claude.json") if where else \
            (self.home / ".claude", self.home / ".claude.json")

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, dict(kwargs.get("env") or {})))
        out, code = "", 0
        if argv[0] == "tmux" and "new-session" in argv:
            command = argv[-1]
            self.place = (str(scratch(self.home)) if f"{agent.CONFIG_DIR_VAR}=" in command
                          else None)
        elif argv[0] == "tmux" and "capture-pane" in argv:
            out = self.pane
        elif argv[0] == "tmux" and "send-keys" in argv and argv[-1] != "Enter":
            place, global_config = self._config(
                {agent.CONFIG_DIR_VAR: self.place} if self.place else {})
            write_account(place, global_config, self.signs_in_as, "sk-ant-oat01-NEW")
            if not self.says_whose:
                global_config.write_text(json.dumps({"hasCompletedOnboarding": True}))
        elif argv[1:3] == ["auth", "status"]:
            place, _ = self._config(kwargs.get("env"))
            out = json.dumps({"loggedIn": (place / ".credentials.json").is_file()})
        elif argv[1:] == ["--version"]:
            out = "2.1.278 (Claude Code)"
        elif argv[:3] == ["systemctl", "--user", "is-active"]:
            out = "active"
        elif argv[:3] == ["systemctl", "--user", "is-enabled"]:
            out = "enabled"
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    def started(self):
        return [a[-1] for a, _ in self.calls if a[0] == "tmux" and "new-session" in a]

    def restarts(self):
        return [a for a, _ in self.calls if a[:3] == ["systemctl", "--user", "restart"]]

    def logouts(self):
        return [env for a, env in self.calls if a[1:3] == ["auth", "logout"]]


def sign_in(fake, requested_at=100.0, kind="login"):
    """Ask for a sign-in, see the link, send the code, and let it finish.
    Returns what the last step reported."""
    wanted = {"requested_at": requested_at, "kind": kind}
    agent.slot_facts({"login": wanted}, fake, now=NOW)
    fake.pane = "Visit https://claude.com/cai/oauth/authorize?code=true to continue"
    agent.slot_facts({"login": wanted}, fake, now=NOW)
    agent.slot_facts({"login": {**wanted, "code": "c"}}, fake, now=NOW)
    return agent.slot_facts({"login": {**wanted, "code": "c"}}, fake, now=NOW).get("login")


# -- which account ---------------------------------------------------------------------------

def test_the_fingerprint_is_a_digest_of_the_account_id_and_never_the_id(home):
    signed_in_as(home, MINE)
    assert agent.account_fingerprint(home / ".claude.json") == fp_of(MINE)
    facts = agent.slot_facts({}, Slot(home), now=NOW)
    assert facts["credentials"]["account_fp"] == fp_of(MINE)
    assert MINE not in json.dumps(facts)


@pytest.mark.parametrize("block", [{}, {"accountUuid": ""}, {"accountUuid": 7},
                                   {"emailAddress": "a@b.co"}])
def test_no_account_id_means_no_fingerprint(tmp_path, block):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"oauthAccount": block}))
    assert agent.account_fingerprint(path) is None
    assert agent.account_fingerprint(tmp_path / "missing.json") is None


def test_an_owner_node_reports_the_fingerprint_and_nothing_else_about_the_account(
        tmp_path, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    config = tmp_path / "claude"
    config.mkdir()
    (tmp_path / "claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": MINE, "emailAddress": "owner@example.com", "fullName": "An Owner"}}))
    cfg = agent.AgentConfig(url="https://f.example", node_id="n", token="t",
                            claude_config_dir=config, egress_targets=("https://ip.example",))
    payload = agent.build_payload(cfg, runner=lambda argv, **k: subprocess.CompletedProcess(
        argv, 0, stdout="", stderr=""), opener=lambda req, timeout: FakeResponse(b""),
        now=lambda: NOW)
    assert payload["credentials"]["account_fp"] == fp_of(MINE)
    blob = json.dumps(payload)
    for private in (MINE, "owner@example.com", "An Owner"):
        assert private not in blob


# -- the binding -----------------------------------------------------------------------------

def test_a_slot_signed_in_before_it_kept_its_account_binds_to_the_one_it_has(home):
    signed_in_as(home, MINE)
    agent.slot_facts({}, Slot(home), now=NOW)
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_a_slot_nobody_has_signed_into_is_bound_to_nobody(home):
    agent.slot_facts({}, Slot(home), now=NOW)
    assert "bound_fp" not in state(home)


def test_an_account_block_with_no_credential_binds_nobody(home):
    """~/.claude.json can name an account whose sign-in is long gone. Only a
    credential says the slot was signed in as it."""
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": MINE}}))
    agent.slot_facts({}, Slot(home), now=NOW)
    assert "bound_fp" not in state(home)


def test_a_slot_asked_to_sign_in_on_its_first_run_is_already_held_to_its_account(home):
    """Signed in before slots kept their account, and asked to sign in again on
    the very first run of this agent: that sign-in is already held to it."""
    signed_in_as(home, MINE)
    fake = Slot(home)
    agent.slot_facts({"login": {"requested_at": 5.0}}, fake, now=NOW)
    [command] = fake.started()
    assert f"{agent.CONFIG_DIR_VAR}={scratch(home)}" in command


def test_the_first_sign_in_goes_straight_in_and_binds(home):
    fake = Slot(home, signs_in_as=MINE)
    said = sign_in(fake)
    assert said["state"] == "done"
    assert all(agent.CONFIG_DIR_VAR not in cmd for cmd in fake.started()), "went to scratch"
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_the_binding_holds_whatever_is_signed_in_later(home):
    signed_in_as(home, MINE)
    agent.slot_facts({}, Slot(home), now=NOW)
    signed_in_as(home, THEIRS)                  # somebody's hand-made swap
    agent.slot_facts({}, Slot(home), now=NOW)
    assert state(home)["bound_fp"] == fp_of(MINE)


# -- signing in again ------------------------------------------------------------------------

def test_signing_in_again_as_the_same_account_replaces_the_credential_whole(home):
    signed_in_as(home, MINE)
    fake = Slot(home, signs_in_as=MINE)
    agent.slot_facts({}, fake, now=NOW)                                  # binds
    said = sign_in(fake)
    assert said["state"] == "done"
    [command] = fake.started()
    assert f"{agent.CONFIG_DIR_VAR}={scratch(home)}" in command, "signed in over ~/.claude"
    credential = home / ".claude" / ".credentials.json"
    assert "sk-ant-oat01-NEW" in credential.read_text()
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert not scratch(home).exists()
    assert len(fake.restarts()) == 1, "Remote Control stayed on the old sign-in"


def test_another_account_is_refused_and_the_old_sign_in_is_untouched(home):
    signed_in_as(home, MINE)
    before = (home / ".claude" / ".credentials.json").read_bytes()
    fake = Slot(home, signs_in_as=THEIRS)
    agent.slot_facts({}, fake, now=NOW)
    said = sign_in(fake)
    assert said == {"state": "failed", "detail": agent.OTHER_ACCOUNT, "requested_at": 100.0}
    assert (home / ".claude" / ".credentials.json").read_bytes() == before
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"]["accountUuid"] == MINE
    assert not scratch(home).exists(), "the other account's sign-in was kept"
    assert fake.restarts() == []
    assert [env.get(agent.CONFIG_DIR_VAR) for env in fake.logouts()] == [str(scratch(home))]
    assert state(home)["bound_fp"] == fp_of(MINE)


def test_a_sign_in_that_does_not_say_whose_it_is_is_refused(home):
    signed_in_as(home, MINE)
    before = (home / ".claude" / ".credentials.json").read_bytes()
    fake = Slot(home, signs_in_as=MINE, says_whose=False)
    agent.slot_facts({}, fake, now=NOW)
    said = sign_in(fake)
    assert said["state"] == "failed" and said["detail"] == agent.UNKNOWN_ACCOUNT
    assert (home / ".claude" / ".credentials.json").read_bytes() == before
    assert not scratch(home).exists()


def test_no_place_for_the_sign_in_fails_before_anything_starts(home, monkeypatch):
    signed_in_as(home, MINE)
    fake = Slot(home)
    agent.slot_facts({}, fake, now=NOW)
    monkeypatch.setattr(agent, "prepare_scratch", lambda: False)
    said = agent.slot_facts({"login": {"requested_at": 5.0}}, fake, now=NOW)["login"]
    assert said == {"state": "failed", "detail": agent.NO_SCRATCH, "requested_at": 5.0}
    assert fake.started() == []


def test_the_scratch_is_private_and_past_its_one_prompt(home):
    signed_in_as(home, MINE)
    fake = Slot(home)
    agent.slot_facts({}, fake, now=NOW)
    agent.slot_facts({"login": {"requested_at": 5.0}}, fake, now=NOW)
    assert stat.S_IMODE(scratch(home).stat().st_mode) == 0o700
    seeded = scratch(home) / ".claude.json"
    assert json.loads(seeded.read_text()) == {"hasCompletedOnboarding": True}
    assert stat.S_IMODE(seeded.stat().st_mode) == 0o600


def test_what_is_kept_is_the_credential_that_was_checked_not_the_file_again(home, monkeypatch):
    """Something swaps the scratch credential after the account was checked and
    before it is written: what lands in ~/.claude is still the one checked."""
    signed_in_as(home, MINE)
    write_account(scratch(home), scratch(home) / ".claude.json", MINE, "sk-ant-oat01-CHECKED")
    real = agent.fingerprint_of

    def checked_then_swapped(raw):
        answer = real(raw)
        (scratch(home) / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-ant-oat01-SWAPPED"}}))
        return answer

    monkeypatch.setattr(agent, "fingerprint_of", checked_then_swapped)
    assert agent.adopt_sign_in(fp_of(MINE), Slot(home)) == ("", fp_of(MINE))
    kept = (home / ".claude" / ".credentials.json").read_text()
    assert "sk-ant-oat01-CHECKED" in kept and "SWAPPED" not in kept


def test_a_credential_that_cannot_be_read_is_not_adopted(home):
    signed_in_as(home, MINE)
    before = (home / ".claude" / ".credentials.json").read_bytes()
    write_account(scratch(home), scratch(home) / ".claude.json", MINE, "x")
    (scratch(home) / ".credentials.json").unlink()
    assert agent.adopt_sign_in(fp_of(MINE), Slot(home)) == (agent.NOT_ADOPTED, fp_of(MINE))
    assert (home / ".claude" / ".credentials.json").read_bytes() == before


def test_a_bound_slot_reports_the_account_it_keeps_beside_the_one_it_has(home):
    signed_in_as(home, MINE)
    agent.slot_facts({}, Slot(home), now=NOW)
    signed_in_as(home, THEIRS)                  # changed some other way than the page
    creds = agent.slot_facts({}, Slot(home), now=NOW)["credentials"]
    assert creds["account_fp"] == fp_of(THEIRS) and creds["bound_fp"] == fp_of(MINE)


@pytest.mark.parametrize("whose", [MINE, THEIRS])
def test_adopting_or_refusing_leaves_no_scratch_behind(home, whose):
    signed_in_as(home, MINE)
    write_account(scratch(home), scratch(home) / ".claude.json", whose, "sk-ant-oat01-NEW")
    agent.adopt_sign_in(fp_of(MINE), Slot(home))
    assert not scratch(home).exists()


def test_a_leftover_scratch_is_cleared_when_nothing_is_signing_in(home):
    signed_in_as(home, MINE)
    write_account(scratch(home), scratch(home) / ".claude.json", THEIRS, "sk-ant-oat01-LEFT")
    agent.slot_facts({}, Slot(home), now=NOW)
    assert not scratch(home).exists()


def test_a_cancelled_sign_in_leaves_no_scratch(home):
    signed_in_as(home, MINE)
    fake = Slot(home)
    agent.slot_facts({}, fake, now=NOW)
    agent.slot_facts({"login": {"requested_at": 5.0}}, fake, now=NOW)
    assert scratch(home).exists()
    agent.slot_facts({}, fake, now=NOW)                                  # cancelled
    assert not scratch(home).exists()


def test_a_device_token_on_a_bound_slot_is_minted_from_its_own_account(home):
    signed_in_as(home, MINE)
    fake = Slot(home)
    agent.slot_facts({}, fake, now=NOW)
    agent.slot_facts({"login": {"requested_at": 5.0, "kind": "token"}}, fake, now=NOW)
    [command] = fake.started()
    assert "setup-token" in command and agent.CONFIG_DIR_VAR not in command
    assert not scratch(home).exists()


def test_the_scratch_follows_a_symlink_nowhere(home, tmp_path):
    """A link left where the scratch goes is removed as a link: what it points at
    is not the agent's to delete."""
    signed_in_as(home, MINE)
    elsewhere = tmp_path / "precious"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("mine")
    scratch(home).parent.mkdir(parents=True, exist_ok=True)
    scratch(home).symlink_to(elsewhere)
    agent.slot_facts({}, Slot(home), now=NOW)
    assert not scratch(home).exists() and (elsewhere / "keep.txt").read_text() == "mine"


def test_the_slot_facts_main_path_still_drops_an_inherited_directory(home, monkeypatch):
    signed_in_as(home, MINE)
    monkeypatch.setenv(agent.CONFIG_DIR_VAR, "/somewhere/else")
    out = io.StringIO()
    assert agent.slot_facts_main(io.StringIO("{}"), out, Slot(home)) == 0
    assert json.loads(out.getvalue())["credentials"]["account_fp"] == fp_of(MINE)
