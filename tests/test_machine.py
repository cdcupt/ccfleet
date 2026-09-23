"""The machine agent: root on a shared machine, looking after every slot on it.

What it does as root is narrow on purpose — run slot-add, run slot-remove, and
start the ordinary agent as each slot's own user — so the tests are mostly
about the edges of that: what it will and will not run, what reaches a slot's
process, and what a slot's own report is allowed to change.
"""

from __future__ import annotations

import io
import json
import pwd
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccfleet_agent import machine

NOW = 1_700_000_000.0
CLAIM = NOW - 30.0


def account(name="slot01", uid=1001, home=None):
    return pwd.struct_passwd((name, "x", uid, uid, "", home or f"/home/{name}", "/bin/bash"))


class Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Fake:
    """A machine that exists only in memory: users, groups, scripts, server."""

    def __init__(self, users=(), groups=None, desired=None, facts=None, script_codes=None,
                 status=200):
        self.users = {u: account(u, 1001 + i) for i, u in enumerate(users)}
        self.groups = groups if groups is not None else {u: {"ccfleet-slots"} for u in users}
        self.desired = desired or {}
        self.facts = facts if facts is not None else {"claude": {"version": "2.1.278"}}
        self.script_codes = script_codes or {}
        self.status = status
        self.spawned = []
        self.scripts = []
        self.posted = []

    def lookup(self, name):
        return self.users.get(name)

    def groups_of(self, acct):
        return set(self.groups.get(acct.pw_name, set()))

    def spawn(self, argv, **kwargs):
        self.spawned.append((argv, kwargs))
        return 0, json.dumps(self.facts)

    def runner(self, argv, **kwargs):
        self.scripts.append(argv)
        code, text = self.script_codes.get((Path(argv[0]).name, argv[2]), (0, "done\n"))
        kwargs["stdout"].write(text.encode())
        if code == 0 and argv[0].endswith("slot-add.sh"):
            self.users[argv[2]] = account(argv[2], 2000 + len(self.users))
            self.groups[argv[2]] = {"ccfleet-slots"}
        if code == 0 and argv[0].endswith("slot-remove.sh"):
            self.users.pop(argv[2], None)
        return subprocess.CompletedProcess(argv, code)

    def opener(self, request, timeout=None):
        self.posted.append(json.loads(request.data))
        if self.status != 200:
            import urllib.error
            raise urllib.error.HTTPError(request.full_url, self.status, "no", {}, io.BytesIO(b"{}"))
        return Reply(json.dumps({"ok": True, "desired": self.desired}).encode())

    def system(self):
        return machine.System(lookup=self.lookup, groups_of=self.groups_of, spawn=self.spawn,
                              runner=self.runner, opener=self.opener, clock=lambda: NOW)


@pytest.fixture
def cfg(tmp_path):
    return machine.MachineConfig.from_env({
        "CCFLEET_URL": "https://fleet.example", "CCFLEET_NODE_ID": "shared-1",
        "CCFLEET_NODE_TOKEN": "t" * 64, "CCFLEET_LIB_DIR": str(tmp_path / "lib"),
        "CCFLEET_STATE_FILE": str(tmp_path / "state" / "machine.json"),
        "CCFLEET_EGRESS_TARGETS": "https://egress.invalid"})


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr(machine.core, "egress_ip", lambda *a, **k: {"ip": None, "source": None})


# -- configuration ---------------------------------------------------------------

def test_the_machine_needs_the_same_three_values_as_an_owner_node():
    with pytest.raises(machine.core.AgentConfigError):
        machine.MachineConfig.from_env({"CCFLEET_URL": "https://f", "CCFLEET_NODE_ID": "m"})


def test_by_default_everything_lives_where_only_root_can_change_it():
    cfg = machine.MachineConfig.from_env({"CCFLEET_URL": "https://f", "CCFLEET_NODE_ID": "m",
                                          "CCFLEET_NODE_TOKEN": "t"})
    assert cfg.state_path == Path("/var/lib/ccfleet/machine.json")
    assert cfg.slot_add == Path("/usr/local/lib/ccfleet/slot-add.sh")
    assert cfg.slot_remove == Path("/usr/local/lib/ccfleet/slot-remove.sh")
    # A slot is asked by the agent that ships beside this one, never another copy.
    assert cfg.slot_agent == Path(machine.__file__).resolve().with_name("agent.py")


# -- what the server asks for ----------------------------------------------------

def test_the_servers_slots_are_checked_before_anything_runs_as_root():
    wanted = machine.wanted_slots({"slots": [
        {"unix_user": "slot01", "state": "claiming", "claimed_at": CLAIM},
        {"unix_user": "slot02", "state": "releasing", "claimed_at": CLAIM},
        {"unix_user": "../etc", "state": "releasing"},
        {"unix_user": "root user", "state": "releasing"},
        {"unix_user": "Slot03", "state": "free"},
        {"unix_user": "slot04", "state": "vanished"},
        {"unix_user": "slot05", "state": "claiming"},
        {"unix_user": "slot06", "state": "claiming", "claimed_at": True},
        {"unix_user": "slot07", "state": "claiming", "claimed_at": "12"},
        {"unix_user": "slot01", "state": "releasing"},
        "slot08", None,
    ]})
    assert wanted == [
        {"unix_user": "slot01", "state": "claiming", "claimed_at": CLAIM},
        {"unix_user": "slot02", "state": "releasing", "claimed_at": None},
    ]


@pytest.mark.parametrize("desired", [{}, {"slots": None}, {"slots": "slot01"},
                                     {"slots": {"unix_user": "slot01"}}, {"slots": 5},
                                     {"slots": True}])
def test_no_list_of_slots_is_no_slots(desired):
    assert machine.wanted_slots(desired) == []


def test_only_an_account_slot_add_made_is_one_to_act_as():
    """A slot declared under the operator's own login name must not have the
    operator's Claude Code driven and reported as though it were a customer's."""
    assert machine.is_slot_account(account(), {"ccfleet-slots", "slot01"})
    assert not machine.is_slot_account(account(), {"slot01", "sudo"})
    assert not machine.is_slot_account(account(uid=999), {"ccfleet-slots"})
    assert not machine.is_slot_account(account(uid=65534), {"ccfleet-slots"})


# -- what reaches a slot's process -----------------------------------------------

def test_a_slots_process_gets_its_own_environment_and_none_of_roots(monkeypatch):
    """The machine's token is in root's environment. A slot's holder can read
    their own processes' environments, so nothing of root's may reach one."""
    monkeypatch.setenv("CCFLEET_NODE_TOKEN", "the-machine-token")
    monkeypatch.setenv("PYTHONPATH", "/home/slot01/evil")
    env = machine.slot_env(account("slot01", 1001))
    assert "the-machine-token" not in json.dumps(env)
    assert set(env) == {"HOME", "USER", "LOGNAME", "PATH", "LANG", "DISABLE_AUTOUPDATER",
                        "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    assert env["HOME"] == "/home/slot01" and env["USER"] == "slot01"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1001"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    # The slot's own bin directory comes last, so a tmux it drops there is not
    # the one its own report runs.
    assert env["PATH"].split(":")[-1] == "/home/slot01/.local/bin"


def test_a_slot_is_asked_as_itself_with_no_groups(cfg):
    fake = Fake(users=["slot01"])
    machine.ask_slot(fake.lookup("slot01"), cfg, fake.system(), {"refresh_quota": True})
    [(argv, kwargs)] = fake.spawned
    assert argv == [sys.executable, "-I", str(cfg.slot_agent), "--slot-facts"]
    assert kwargs["user"] == 1001 and kwargs["group"] == 1001
    assert kwargs["extra_groups"] == [], "the account's groups travel with it"
    assert kwargs["cwd"] == "/", "root would have walked into the slot's home"
    assert kwargs["env"] == machine.slot_env(fake.lookup("slot01"))
    assert json.loads(kwargs["input_text"]) == {"refresh_quota": True}
    assert kwargs["limit"] == machine.MAX_SLOT_REPORT_BYTES


@pytest.mark.parametrize("result", [(1, '{"claude": {}}'), (0, None), (None, None),
                                    (0, "not json"), (0, '["a list"]')])
def test_a_slot_that_cannot_report_reports_nothing(cfg, result):
    fake = Fake(users=["slot01"])
    system = machine.System(lookup=fake.lookup, groups_of=fake.groups_of,
                            spawn=lambda argv, **kw: result)
    assert machine.ask_slot(fake.lookup("slot01"), cfg, system, {}) == {}


def test_a_slot_cannot_speak_for_what_only_root_knows(cfg):
    """Whether the user exists and whether it was provisioned are what move a
    slot between people. The slot's holder can shape its own report, so those
    come from root's records and nothing the slot says can overwrite them."""
    fake = Fake(users=["slot01"], facts={
        "unix_user": "slot02", "present": False, "provisioned_for": 1.0,
        "wipe_error": None, "claude": {"version": "2.1.278"}, "credentials": {"logged_in": True}})
    state = {"provisioned": {"slot01": CLAIM}}
    entry = machine.slot_report("slot01", state, cfg, fake.system(), False)
    assert entry["unix_user"] == "slot01"
    assert entry["present"] is True
    assert entry["provisioned_for"] == CLAIM
    assert "wipe_error" not in entry
    assert entry["claude"] == {"version": "2.1.278"}
    assert entry["credentials"] == {"logged_in": True}


def test_an_account_that_is_not_a_slot_is_reported_but_never_asked(cfg):
    fake = Fake(users=["erik"], groups={"erik": {"sudo"}})
    entry = machine.slot_report("erik", {}, cfg, fake.system(), True)
    assert entry == {"unix_user": "erik", "present": True}
    assert fake.spawned == [], "ran a process as an account slot-add never made"


def test_a_missing_user_is_reported_absent_and_not_asked(cfg):
    fake = Fake(users=[])
    assert machine.slot_report("slot01", {}, cfg, fake.system(), True) == {
        "unix_user": "slot01", "present": False}
    assert fake.spawned == []


def test_what_went_wrong_is_reported_with_the_claim_it_was_for(cfg):
    fake = Fake(users=["slot01"])
    state = {"provision_failed": {"slot01": {"for": CLAIM, "error": "no network"}},
             "wipe_failed": {"slot01": {"error": "still running", "ts": NOW}}}
    entry = machine.slot_report("slot01", state, cfg, fake.system(), False)
    assert entry["provision_failed_for"] == CLAIM
    assert entry["provision_error"] == "no network"
    assert entry["wipe_error"] == "still running"


def test_the_machine_reports_as_a_machine(cfg):
    fake = Fake(users=["slot01", "slot02"])
    payload = machine.machine_payload(cfg, {"slots": ["slot01", "slot02"]}, fake.system(),
                                      refresh_for="slot02")
    assert payload["mode"] == "machine" and payload["node_id"] == "shared-1"
    # No owner login of its own: those sections would read as a broken node.
    for key in ("claude", "credentials", "remote_control", "quota", "usage"):
        assert key not in payload
    assert [s["unix_user"] for s in payload["slots"]] == ["slot01", "slot02"]
    asked = [json.loads(kw["input_text"])["refresh_quota"] for _, kw in fake.spawned]
    assert asked == [False, True], "one quota read per run, for the slot whose turn it is"


# -- acting ----------------------------------------------------------------------

def slot(user, state, claimed_at=None):
    return {"unix_user": user, "state": state, "claimed_at": claimed_at}


def test_a_claim_is_provisioned_once(cfg):
    fake = Fake()
    state = machine.act_on_slots([slot("slot01", "claiming", CLAIM)], {}, cfg, fake.system())
    assert fake.scripts == [[str(cfg.slot_add), "--slot", "slot01"]]
    assert state["provisioned"] == {"slot01": CLAIM}
    machine.act_on_slots([slot("slot01", "claiming", CLAIM)], state, cfg, fake.system())
    assert len(fake.scripts) == 1, "provisioned the same claim twice"


def test_provisioning_that_failed_is_reported_against_its_claim_and_not_retried(cfg):
    fake = Fake(script_codes={("slot-add.sh", "slot01"): (1, "step\nerror: no network\n")})
    state = machine.act_on_slots([slot("slot01", "claiming", CLAIM)], {}, cfg, fake.system())
    assert state["provision_failed"] == {"slot01": {"for": CLAIM, "error": "error: no network"}}
    assert "slot01" not in state["provisioned"]
    machine.act_on_slots([slot("slot01", "claiming", CLAIM)], state, cfg, fake.system())
    assert len(fake.scripts) == 1, "the server moves a failed claim on; retrying repeats it"


def test_a_new_claim_of_the_same_slot_is_provisioned_afresh(cfg):
    fake = Fake()
    state = {"provisioned": {"slot01": CLAIM - 999}}
    state = machine.act_on_slots([slot("slot01", "claiming", CLAIM)], state, cfg, fake.system())
    assert fake.scripts == [[str(cfg.slot_add), "--slot", "slot01"]]
    assert state["provisioned"] == {"slot01": CLAIM}


def test_a_release_is_wiped(cfg):
    fake = Fake(users=["slot01"])
    state = machine.act_on_slots([slot("slot01", "releasing")],
                                 {"provisioned": {"slot01": CLAIM}}, cfg, fake.system())
    assert fake.scripts == [[str(cfg.slot_remove), "--slot", "slot01"]]
    assert state["wipe_failed"] == {}
    assert state["provisioned"] == {}, "an old claim outlived its release"


def test_a_wipe_that_worked_is_not_reported_as_failed(cfg):
    """The script said it worked, but the account still resolves — a name
    cache a beat behind, say. An earlier failure must not be reported against
    a wipe that has since succeeded; the next run simply looks again."""
    fake = Fake(users=["slot01"])
    lingering = machine.System(lookup=fake.lookup, groups_of=fake.groups_of,
                               spawn=fake.spawn, clock=lambda: NOW,
                               runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    earlier = {"wipe_failed": {"slot01": {"error": "still running",
                                          "ts": NOW - machine.WIPE_RETRY_AFTER_S - 1}}}
    state = machine.act_on_slots([slot("slot01", "releasing")], earlier, cfg, lingering)
    assert "wipe_error" not in machine.slot_report("slot01", state, cfg, lingering, False)


def test_a_release_with_nothing_left_on_the_machine_runs_nothing(cfg):
    fake = Fake(users=[])
    state = machine.act_on_slots([slot("slot01", "releasing")],
                                 {"wipe_failed": {"slot01": {"error": "x", "ts": NOW}}},
                                 cfg, fake.system())
    assert fake.scripts == []
    assert state["wipe_failed"] == {}, "a wipe that is done still reads as failed"


def test_a_failed_wipe_is_remembered_and_retried_later_not_every_minute(cfg):
    fake = Fake(users=["slot01"],
                script_codes={("slot-remove.sh", "slot01"): (1, "error: still running\n")})
    state = machine.act_on_slots([slot("slot01", "releasing")], {}, cfg, fake.system())
    assert state["wipe_failed"]["slot01"] == {"error": "error: still running", "ts": NOW}
    machine.act_on_slots([slot("slot01", "releasing")], state, cfg, fake.system())
    assert len(fake.scripts) == 1, "retried inside the back-off"

    later = {**state, "wipe_failed": {"slot01": {"error": "x",
                                                 "ts": NOW - machine.WIPE_RETRY_AFTER_S - 1}}}
    machine.act_on_slots([slot("slot01", "releasing")], later, cfg, fake.system())
    assert len(fake.scripts) == 2, "never retried at all"


def test_wipes_go_before_provisioning(cfg):
    fake = Fake(users=["slot02"])
    machine.act_on_slots([slot("slot01", "claiming", CLAIM), slot("slot02", "releasing")],
                         {}, cfg, fake.system())
    assert [Path(argv[0]).name for argv in fake.scripts] == ["slot-remove.sh", "slot-add.sh"]


def test_a_free_slot_carries_nothing_of_its_last_claim(cfg):
    fake = Fake()
    state = {"provisioned": {"slot01": CLAIM},
             "provision_failed": {"slot01": {"for": CLAIM, "error": "x"}},
             "wipe_failed": {"slot01": {"error": "y", "ts": NOW}}}
    state = machine.act_on_slots([slot("slot01", "free")], state, cfg, fake.system())
    assert state["provisioned"] == {} and state["provision_failed"] == {}
    assert state["wipe_failed"] == {}
    assert fake.scripts == []


def test_a_failed_claim_stays_reported_through_its_wipe(cfg):
    """So the operator's warning lasts until the slot is clean again, rather
    than flashing up for one heartbeat and closing."""
    fake = Fake(users=["slot01"])
    state = {"provision_failed": {"slot01": {"for": CLAIM, "error": "x"}}}
    state = machine.act_on_slots([slot("slot01", "releasing")], state, cfg, fake.system())
    assert state["provision_failed"] == {"slot01": {"for": CLAIM, "error": "x"}}


def test_a_slot_no_longer_declared_is_forgotten(cfg):
    fake = Fake()
    state = {"provisioned": {"gone": CLAIM}, "wipe_failed": {"gone": {"error": "x", "ts": NOW}},
             "slots": ["gone"]}
    state = machine.act_on_slots([], state, cfg, fake.system())
    assert state["provisioned"] == {} and state["wipe_failed"] == {}
    assert state["slots"] == []


def test_the_next_report_covers_exactly_what_was_asked_about(cfg):
    fake = Fake()
    state = machine.act_on_slots([slot("slot02", "free"), slot("slot01", "active")],
                                 {"slots": ["old"]}, cfg, fake.system())
    assert state["slots"] == ["slot02", "slot01"]


# -- running the scripts ---------------------------------------------------------

def _script(tmp_path, body):
    path = tmp_path / "script.sh"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_a_script_that_worked(tmp_path):
    ok, why = machine.run_script(_script(tmp_path, "echo fine\n"), "slot01", 10,
                                 machine.System())
    assert (ok, why) == (True, "")


def test_a_script_that_failed_says_why_in_its_own_last_words(tmp_path):
    path = _script(tmp_path, "echo step one\necho 'error: no network' >&2\nexit 3\n")
    ok, why = machine.run_script(path, "slot01", 10, machine.System())
    assert not ok and why == "error: no network"


def test_a_script_that_hangs_is_given_up_on(tmp_path):
    ok, why = machine.run_script(_script(tmp_path, "sleep 30\n"), "slot01", 0.5,
                                 machine.System())
    assert not ok and "did not finish within" in why


def test_a_script_that_cannot_start_says_so(tmp_path):
    ok, why = machine.run_script(tmp_path / "missing.sh", "slot01", 5, machine.System())
    assert not ok and "missing.sh could not run" in why


def test_something_a_script_leaves_running_does_not_hold_the_agent(tmp_path):
    """slot-add starts a slot's services; if one of them kept the script's
    output open, a pipe would keep this run waiting for as long as it lived."""
    path = _script(tmp_path, "sleep 20 &\necho started\nexit 0\n")
    started = time.monotonic()
    ok, _ = machine.run_script(path, "slot01", 30, machine.System())
    assert ok
    assert time.monotonic() - started < 10, "waited on a process the script left behind"


# -- a child whose output is not trusted -----------------------------------------

PY = sys.executable


def test_a_well_behaved_child_is_heard_out():
    code, out = machine.run_bounded([PY, "-c", "import sys; print(sys.stdin.read().upper())"],
                                    input_text="hello", limit=1000, timeout=10)
    assert code == 0 and out.strip() == "HELLO"


def test_too_much_output_is_refused_rather_than_cut_short():
    code, out = machine.run_bounded([PY, "-c", "print('x' * 5000)"], input_text="",
                                    limit=100, timeout=10)
    assert out is None


def test_a_child_that_hangs_is_abandoned_on_time():
    started = time.monotonic()
    code, out = machine.run_bounded([PY, "-c", "import time; time.sleep(30)"],
                                    input_text="", limit=100, timeout=0.5)
    assert out is None
    assert time.monotonic() - started < 10


def test_a_pipe_held_open_by_a_leftover_does_not_hold_the_agent():
    """The child exits, but something it started still holds its output. EOF
    never comes; the clock is what ends the wait."""
    started = time.monotonic()
    code, out = machine.run_bounded(["/bin/sh", "-c", "sleep 30 & echo '{}'"],
                                    input_text="", limit=100, timeout=1)
    assert out is None
    assert time.monotonic() - started < 10


def test_a_child_that_ignores_its_input_is_no_trouble():
    code, out = machine.run_bounded([PY, "-c", "print('ok')"], input_text="x" * 200_000,
                                    limit=100, timeout=10)
    assert out is not None and out.strip() == "ok"


def test_a_child_that_finishes_talking_but_never_leaves_is_not_believed(monkeypatch):
    """It closed its output, so everything looks said — but it will not exit,
    and an exit code is part of the answer. It is killed and not heard."""
    monkeypatch.setattr(machine, "EXIT_GRACE_S", 0.3)
    started = time.monotonic()
    code, out = machine.run_bounded(
        [PY, "-c", "import os, time; os.write(1, b'{}'); os.close(1); time.sleep(30)"],
        input_text="", limit=100, timeout=10)
    assert out is None
    assert time.monotonic() - started < 8


def test_accounts_and_groups_come_from_the_system():
    import getpass
    me = machine.lookup_user(getpass.getuser())
    assert me is not None and me.pw_uid == __import__("os").getuid()
    assert machine.lookup_user("ccfleet-no-such-user-here") is None
    import grp
    assert grp.getgrgid(me.pw_gid).gr_name in machine.group_names(me)


def test_a_child_that_cannot_start():
    assert machine.run_bounded(["/nonexistent/binary"], input_text="", limit=10,
                               timeout=1) == (None, None)


# -- the cycle -------------------------------------------------------------------

def test_one_run_reports_then_acts(cfg):
    fake = Fake(users=["slot02"], desired={"slots": [
        {"unix_user": "slot01", "state": "claiming", "claimed_at": CLAIM},
        {"unix_user": "slot02", "state": "releasing"}]})
    status, _desired, state = machine.run_cycle(cfg, {}, fake.system())
    assert status == 200
    [posted] = fake.posted
    assert posted["mode"] == "machine" and posted["slots"] == []
    assert [Path(a[0]).name for a in fake.scripts] == ["slot-remove.sh", "slot-add.sh"]
    assert json.loads(cfg.state_path.read_text())["provisioned"] == {"slot01": CLAIM}

    # The next run reports what this one did.
    machine.run_cycle(cfg, state, fake.system())
    report = {s["unix_user"]: s for s in fake.posted[1]["slots"]}
    assert report["slot01"]["present"] is True and report["slot01"]["provisioned_for"] == CLAIM
    assert report["slot02"]["present"] is False


def test_a_reply_naming_many_slots_is_read_whole(cfg):
    """An owner node's reply fits in 4 KB; a machine's names every slot. Cut
    short it parses as nothing, and the machine would quietly stop acting."""
    slots = [{"unix_user": f"slot{n:03d}", "state": "free"} for n in range(120)]
    slots.append({"unix_user": "last", "state": "claiming", "claimed_at": CLAIM})
    fake = Fake(desired={"slots": slots})
    machine.run_cycle(cfg, {}, fake.system())
    assert fake.scripts == [[str(cfg.slot_add), "--slot", "last"]]


def test_a_refused_heartbeat_changes_nothing(cfg, monkeypatch):
    monkeypatch.setattr(machine.core, "RETRY_DELAYS_S", ())
    fake = Fake(status=401, desired={"slots": [
        {"unix_user": "slot01", "state": "claiming", "claimed_at": CLAIM}]})
    status, desired, state = machine.run_cycle(cfg, {"slots": ["slot01"]}, fake.system())
    assert status == 401 and desired == {}
    assert state == {"slots": ["slot01"]}
    assert fake.scripts == []


def test_quota_reads_take_turns_across_runs(cfg):
    fake = Fake(users=["slot01", "slot02"], desired={"slots": [
        {"unix_user": "slot01", "state": "active"}, {"unix_user": "slot02", "state": "active"}]})
    state = {"slots": ["slot01", "slot02"]}
    turns = []
    for _ in range(3):
        fake.spawned.clear()
        _, _, state = machine.run_cycle(cfg, state, fake.system())
        turns.append([json.loads(kw["input_text"])["refresh_quota"] for _, kw in fake.spawned])
    assert turns == [[True, False], [False, True], [True, False]]


# -- the command -----------------------------------------------------------------

def _env_file(tmp_path, cfg):
    path = tmp_path / "agent.env"
    path.write_text(f"CCFLEET_URL={cfg.url}\nCCFLEET_NODE_ID={cfg.node_id}\n"
                    f"CCFLEET_NODE_TOKEN={cfg.token}\nCCFLEET_STATE_FILE={cfg.state_path}\n")
    return path


def test_it_will_not_run_as_anyone_but_root(tmp_path, cfg, monkeypatch, capsys):
    monkeypatch.setattr(machine.os, "geteuid", lambda: 1000)
    assert machine.main(["--env-file", str(_env_file(tmp_path, cfg))]) == 2
    assert "runs as root" in capsys.readouterr().err


def test_an_incomplete_configuration_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CCFLEET_NODE_TOKEN", raising=False)
    empty = tmp_path / "empty.env"
    empty.write_text("")
    assert machine.main(["--env-file", str(empty)]) == 2
    assert "error" in capsys.readouterr().err


def test_printing_sends_nothing(tmp_path, cfg, monkeypatch, capsys):
    monkeypatch.setattr(machine.os, "geteuid", lambda: 0)
    fake = Fake()
    assert machine.main(["--env-file", str(_env_file(tmp_path, cfg)), "--print"],
                        fake.system()) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "machine"
    assert fake.posted == []


def test_a_run_posts_and_says_whether_it_was_heard(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(machine.os, "geteuid", lambda: 0)
    fake = Fake()
    assert machine.main(["--env-file", str(_env_file(tmp_path, cfg))], fake.system()) == 0
    assert len(fake.posted) == 1
    monkeypatch.setattr(machine.core, "RETRY_DELAYS_S", ())
    fake.status = 401
    assert machine.main(["--env-file", str(_env_file(tmp_path, cfg))], fake.system()) == 1


def test_a_second_run_leaves_the_first_to_it(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(machine.os, "geteuid", lambda: 0)
    monkeypatch.setattr(machine.core, "hold_the_only_run", lambda path: None)
    fake = Fake()
    assert machine.main(["--env-file", str(_env_file(tmp_path, cfg))], fake.system()) == 0
    assert fake.posted == [], "two runs acted on the same slots at once"


def test_the_installed_layout_imports_without_the_package(tmp_path):
    """Installed, the two files sit side by side and run with -I, which keeps
    even the script's own directory off the path."""
    lib = tmp_path / "ccfleet_agent"
    lib.mkdir()
    src = Path(machine.__file__).resolve().parent
    for name in ("agent.py", "machine.py"):
        (lib / name).write_text((src / name).read_text())
    proc = subprocess.run([PY, "-I", str(lib / "machine.py"), "--help"], capture_output=True,
                          text=True, timeout=30, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "shared machine" in proc.stdout



# -- signing in ------------------------------------------------------------------

LOGIN = {"requested_at": 42.0, "kind": "login", "email": "me@example.com"}


def test_a_sign_in_goes_down_only_to_a_slot_set_up_and_held():
    wanted = machine.wanted_slots({"slots": [
        {"unix_user": "slot01", "state": "active", "login": LOGIN},
        {"unix_user": "slot02", "state": "claimed", "login": {**LOGIN, "code": "c0de"}},
        {"unix_user": "slot03", "state": "releasing", "login": LOGIN},
        {"unix_user": "slot04", "state": "free", "login": LOGIN},
    ]})
    assert wanted[0]["login"] == LOGIN
    assert wanted[1]["login"]["code"] == "c0de"
    assert "login" not in wanted[2] and "login" not in wanted[3]


@pytest.mark.parametrize("login,expected", [
    ({"requested_at": True}, None),                     # not a timestamp
    ({"requested_at": "42"}, None),
    ({}, None),
    ({"requested_at": 1.0, "kind": "shell"}, {"requested_at": 1.0, "kind": "login"}),
    ({"requested_at": 1.0, "code": "c" * 5000}, {"requested_at": 1.0, "kind": "login"}),
    ({"requested_at": 1.0, "email": 7, "argv": ["rm"]}, {"requested_at": 1.0, "kind": "login"}),
])
def test_a_sign_in_is_copied_field_by_field_before_a_slot_sees_it(login, expected):
    [slot] = machine.wanted_slots({"slots": [
        {"unix_user": "slot01", "state": "active", "login": login}]})
    assert slot.get("login") == expected


def test_a_slot_is_handed_its_own_sign_in_and_its_progress_comes_back(cfg):
    fake = Fake(users=["slot01", "slot02"],
                facts={"claude": {"version": "2.1.280"},
                       "login": {"state": "url_ready", "url": "https://claude.com/x"}})
    state = {"slots": ["slot01", "slot02"], "slot_logins": {"slot01": LOGIN}}
    payload = machine.machine_payload(cfg, state, fake.system())
    asked = {kw["env"]["USER"]: json.loads(kw["input_text"])["login"]
             for _, kw in fake.spawned}
    assert asked == {"slot01": LOGIN, "slot02": None}, "a sign-in went to the wrong slot"
    assert payload["slots"][0]["login"]["state"] == "url_ready"


def test_a_fast_poll_asks_only_whoever_is_signing_in(cfg):
    fake = Fake(users=["slot01", "slot02"])
    state = {"slots": ["slot01", "slot02"], "slot_logins": {"slot01": LOGIN},
             "heard": {"slot02": {"claude": {"version": "2.1.279"}}}}
    payload = machine.machine_payload(cfg, state, fake.system(), fast=True)
    assert [kw["env"]["USER"] for _, kw in fake.spawned] == ["slot01"]
    # Everybody is still in the report — nothing reads as gone between two
    # full ones — with what they said last time, and root's facts fresh.
    other = payload["slots"][1]
    assert other["unix_user"] == "slot02" and other["present"] is True
    assert other["claude"] == {"version": "2.1.279"}


def test_a_minted_token_is_reported_once_and_never_kept_on_disk(cfg):
    token = "sk-ant-oat01-" + "Z" * 40
    fake = Fake(users=["slot01"], desired={"slots": [
                    {"unix_user": "slot01", "state": "active", "login": LOGIN}]},
                facts={"claude": {"version": "2.1.280"},
                       "login": {"state": "ready", "secret": token, "requested_at": 42.0}})
    _, _, state = machine.run_cycle(cfg, {"slots": ["slot01"], "slot_logins": {"slot01": LOGIN}},
                                    fake.system())
    assert fake.posted[0]["slots"][0]["login"]["secret"] == token
    assert token not in cfg.state_path.read_text()
    assert "login" not in state["heard"]["slot01"]


def test_a_sign_in_the_server_still_wants_is_handed_on_to_the_next_run(cfg):
    fake = Fake(users=["slot01"], desired={"slots": [
        {"unix_user": "slot01", "state": "active", "login": LOGIN}]})
    _, _, state = machine.run_cycle(cfg, {}, fake.system())
    assert state["slot_logins"] == {"slot01": LOGIN}
    fake.desired = {"slots": [{"unix_user": "slot01", "state": "active"}]}
    _, _, state = machine.run_cycle(cfg, state, fake.system())
    assert state["slot_logins"] == {}, "a finished sign-in kept being run"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _resident(fake, clock):
    return machine.System(lookup=fake.lookup, groups_of=fake.groups_of, spawn=fake.spawn,
                          runner=fake.runner, opener=fake.opener, clock=lambda: NOW,
                          monotonic=clock.monotonic, sleep=clock.sleep)


def test_it_stays_while_somebody_signs_in_and_leaves_when_they_are_done(cfg):
    signing = {"slots": [{"unix_user": "slot01", "state": "active", "login": LOGIN},
                         {"unix_user": "slot02", "state": "active"}],
               "poll_s": 5}
    fake = Fake(users=["slot01", "slot02"], desired=signing)
    clock = Clock()
    system = _resident(fake, clock)

    polls = []

    def opener(request, timeout=None):
        polls.append(json.loads(request.data))
        if len(polls) == 20:
            fake.desired = {"slots": [{"unix_user": "slot01", "state": "active"},
                                      {"unix_user": "slot02", "state": "active"}]}
        return fake.opener(request, timeout)
    system = machine.System(**{**system.__dict__, "opener": opener})
    state = {"slots": ["slot01", "slot02"], "slot_logins": {"slot01": LOGIN}}
    assert machine.stay_for_sign_ins(cfg, state, dict(signing), system) == 0
    assert len(polls) == 20, "left before the sign-in finished, or stayed after"
    assert set(clock.slept) == {5.0}
    # Most polls ask only the slot signing in; a full report still goes out
    # about once a minute, so other slots are not left waiting. Twenty polls
    # five seconds apart is a hundred seconds: one full report, maybe two.
    asked = [kw["env"]["USER"] for _, kw in fake.spawned]
    assert asked.count("slot01") == 20
    assert 1 <= asked.count("slot02") <= 2
    # A quota read opens a session; only a full report may start one.
    refreshed = [json.loads(kw["input_text"])["refresh_quota"] for _, kw in fake.spawned]
    assert sum(refreshed) <= asked.count("slot02")


def test_a_sign_in_nobody_finishes_is_given_up_on_and_the_server_told(cfg):
    signing = {"slots": [{"unix_user": "slot01", "state": "active", "login": LOGIN}],
               "poll_s": 30}
    fake = Fake(users=["slot01"], desired=signing)
    clock = Clock()
    state = {"slots": ["slot01"], "slot_logins": {"slot01": LOGIN}}
    assert machine.stay_for_sign_ins(cfg, state, dict(signing), _resident(fake, clock)) == 0
    assert clock.now >= machine.core.LOGIN_WINDOW_S
    last = fake.posted[-1]["slots"][0]
    assert last["login"]["state"] == "failed"
    assert last["login"]["requested_at"] == 42.0
    # And the slot was told to let the pane go: no sign-in handed on.
    assert json.loads(fake.spawned[-1][1]["input_text"])["login"] is None


def test_a_refused_poll_ends_the_stay(cfg, monkeypatch):
    monkeypatch.setattr(machine.core, "RETRY_DELAYS_S", ())
    signing = {"slots": [{"unix_user": "slot01", "state": "active", "login": LOGIN}]}
    fake = Fake(users=["slot01"], desired=signing, status=401)
    state = {"slots": ["slot01"], "slot_logins": {"slot01": LOGIN}}
    assert machine.stay_for_sign_ins(cfg, state, dict(signing), _resident(fake, Clock())) == 1


def test_a_run_that_finds_somebody_signing_in_stays_for_them(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(machine.os, "geteuid", lambda: 0)
    signing = {"slots": [{"unix_user": "slot01", "state": "active", "login": LOGIN}],
               "poll_s": 5}
    fake = Fake(users=["slot01"], desired=signing)
    clock = Clock()
    system = _resident(fake, clock)

    def opener(request, timeout=None):
        reply = fake.opener(request, timeout)
        if len(fake.posted) == 3:
            fake.desired = {"slots": [{"unix_user": "slot01", "state": "active"}]}
        return reply
    system = machine.System(**{**system.__dict__, "opener": opener})
    assert machine.main(["--env-file", str(_env_file(tmp_path, cfg))], system) == 0
    assert len(fake.posted) == 4, "a timer run left somebody mid-sign-in"


def test_a_give_up_the_server_did_not_hear_is_a_failed_run(cfg, monkeypatch):
    monkeypatch.setattr(machine.core, "RETRY_DELAYS_S", ())
    signing = {"slots": [{"unix_user": "slot01", "state": "active", "login": LOGIN}],
               "poll_s": 30}
    fake = Fake(users=["slot01"], desired=signing)
    clock = Clock()
    system = _resident(fake, clock)

    def opener(request, timeout=None):
        # Refuse only the report that gives up, and nothing before it.
        gives_up = any((s.get("login") or {}).get("state") == "failed"
                       for s in json.loads(request.data)["slots"])
        fake.status = 401 if gives_up else 200
        return fake.opener(request, timeout)
    system = machine.System(**{**system.__dict__, "opener": opener})
    state = {"slots": ["slot01"], "slot_logins": {"slot01": LOGIN}}
    assert machine.stay_for_sign_ins(cfg, state, dict(signing), system) == 1
    assert (fake.posted[-1]["slots"][0]["login"] or {})["state"] == "failed"


def test_a_fast_poll_does_not_move_the_quota_turn_on(cfg):
    """Otherwise how many polls a sign-in took decides whose quota is read
    next, and a slot can be skipped for as long as somebody keeps signing in."""
    fake = Fake(users=["slot01", "slot02"], desired={"slots": [
        {"unix_user": "slot01", "state": "active"}, {"unix_user": "slot02", "state": "active"}]})
    state = {"slots": ["slot01", "slot02"], "quota_turn": 3}
    _, _, state = machine.run_cycle(cfg, state, fake.system(), fast=True)
    assert state["quota_turn"] == 3
    _, _, state = machine.run_cycle(cfg, state, fake.system())
    assert state["quota_turn"] == 4


def _server_by_clock(fake, clock, script):
    """An opener whose reply depends on the time and on what was just posted:
    script(now, this_post) -> desired."""
    def opener(request, timeout=None):
        fake.desired = script(clock.now, json.loads(request.data))
        return fake.opener(request, timeout)
    return opener


def test_a_sign_in_started_late_in_the_run_gets_its_own_window(cfg):
    """One deadline for the whole run gave a sign-in started ten minutes in
    about four minutes before it was abandoned."""
    window = machine.core.LOGIN_WINDOW_S
    first, second = {**LOGIN, "requested_at": 1.0}, {**LOGIN, "requested_at": 2.0}
    failed = {}

    handed_during_slot01s_give_up = []
    asked_before = [0]

    def script(now, this_post):
        # The children for this post were asked since the last one: this cycle's.
        this_cycle = fake.spawned[asked_before[0]:]
        asked_before[0] = len(fake.spawned)
        for report in (this_post or {}).get("slots", []):
            if (report.get("login") or {}).get("state") == "failed":
                if report["unix_user"] == "slot01" and "slot01" not in failed:
                    handed_during_slot01s_give_up.extend(
                        json.loads(kw["input_text"])["login"] for _, kw in this_cycle
                        if kw["env"]["USER"] == "slot02")
                failed.setdefault(report["unix_user"], now)
        slots = []
        if "slot01" not in failed:
            slots.append({"unix_user": "slot01", "state": "active", "login": first})
        else:
            slots.append({"unix_user": "slot01", "state": "active"})
        if now >= 600 and "slot02" not in failed:
            slots.append({"unix_user": "slot02", "state": "active", "login": second})
        else:
            slots.append({"unix_user": "slot02", "state": "active"})
        return {"slots": slots, "poll_s": 5}

    fake = Fake(users=["slot01", "slot02"])
    clock = Clock()
    system = machine.System(**{**_resident(fake, clock).__dict__,
                               "opener": _server_by_clock(fake, clock, script)})
    state = {"slots": ["slot01", "slot02"], "slot_logins": {"slot01": first}}
    assert machine.stay_for_sign_ins(cfg, state, script(0, None), system) == 0
    assert window <= failed["slot01"] < window + 10
    assert 600 + window <= failed["slot02"] < 600 + window + 10, "cut short by slot01's clock"
    # Giving up on slot01 must not touch slot02's sign-in: had slot02 been
    # handed nothing in that same cycle, its pane would have been torn down and
    # started over, and the URL its holder was looking at would stop working.
    assert handed_during_slot01s_give_up == [second], \
        "slot02's sign-in was dropped, or not carried on, in the cycle slot01 gave up"


def test_a_server_that_keeps_offering_a_given_up_attempt_does_not_spin_the_agent(cfg):
    signing = {"slots": [{"unix_user": "slot01", "state": "active", "login": LOGIN}],
               "poll_s": 30}
    fake = Fake(users=["slot01"], desired=signing)      # never drops it
    clock = Clock()
    state = {"slots": ["slot01"], "slot_logins": {"slot01": LOGIN}}
    assert machine.stay_for_sign_ins(cfg, state, dict(signing), _resident(fake, clock)) == 0
    gave_up = [p for p in fake.posted
               if (p["slots"][0].get("login") or {}).get("state") == "failed"]
    assert len(gave_up) == 1, "gave the same attempt up again, and again"


def test_a_stream_of_sign_ins_cannot_keep_the_agent_for_ever(cfg):
    """A new attempt every five minutes, each inside its own window: without a
    bound on the run itself, it would never leave."""
    def script(now, this_post):
        return {"slots": [{"unix_user": "slot01", "state": "active",
                           "login": {**LOGIN, "requested_at": float(int(now // 300))}}],
                "poll_s": 30}
    fake = Fake(users=["slot01"])
    clock = Clock()
    system = machine.System(**{**_resident(fake, clock).__dict__,
                               "opener": _server_by_clock(fake, clock, script)})
    state = {"slots": ["slot01"], "slot_logins": {"slot01": LOGIN}}
    assert machine.stay_for_sign_ins(cfg, state, script(0, None), system) == 0
    assert machine.MAX_RESIDENT_S <= clock.now < machine.MAX_RESIDENT_S + 60


def test_nobody_is_cut_short_by_the_run_ending(cfg):
    """slot01 keeps signing in (a fresh attempt every five minutes, so none
    ever runs out) until well past the hour. slot02 starts just before the
    hour. slot02 still gets its whole window; slot01's attempts after the hour
    are never started here and never failed here — the next run takes them."""
    window, cap = machine.core.LOGIN_WINDOW_S, machine.MAX_RESIDENT_S
    failed, slot02_at = {}, cap - 100

    def script(now, this_post):
        for report in (this_post or {}).get("slots", []):
            if (report.get("login") or {}).get("state") == "failed":
                failed.setdefault((report["unix_user"], report["login"]["requested_at"]), now)
        slots = [{"unix_user": "slot01", "state": "active",
                  "login": {**LOGIN, "requested_at": float(int(now // 300))}}]
        if now >= slot02_at and not any(u == "slot02" for u, _ in failed):
            slots.append({"unix_user": "slot02", "state": "active",
                          "login": {**LOGIN, "requested_at": 99.0}})
        else:
            slots.append({"unix_user": "slot02", "state": "active"})
        return {"slots": slots, "poll_s": 30}

    fake = Fake(users=["slot01", "slot02"])
    clock = Clock()
    system = machine.System(**{**_resident(fake, clock).__dict__,
                               "opener": _server_by_clock(fake, clock, script)})
    state = {"slots": ["slot01", "slot02"], "slot_logins": {"slot01": {**LOGIN,
                                                                        "requested_at": 0.0}}}
    assert machine.stay_for_sign_ins(cfg, state, script(0, None), system) == 0

    assert slot02_at + window <= failed[("slot02", 99.0)] < slot02_at + window + 60, \
        "slot02 was cut short by the hour"
    assert not any(user == "slot01" for user, _ in failed), "a post-hour attempt was failed"
    handed = {json.loads(kw["input_text"])["login"]["requested_at"] for _, kw in fake.spawned
              if kw["env"]["USER"] == "slot01" and json.loads(kw["input_text"])["login"]}
    assert max(handed) < cap // 300, "an attempt arriving after the hour was started"
    assert clock.now < slot02_at + window + 60, "stayed on after its last attempt"



# -- following the machine's pin -----------------------------------------------------

def asked(fake):
    """What each slot was asked, by user, from the last run's spawns."""
    out = {}
    for _argv, kw in fake.spawned:
        user = kw["env"]["USER"]
        out[user] = (json.loads(kw["input_text"]), kw["timeout"])
    return out


def two_runs(cfg, fake):
    """The pin and each slot's state arrive in one reply; the next run acts on them."""
    _, _, state = machine.run_cycle(cfg, {}, fake.system())
    fake.spawned.clear()
    machine.run_cycle(cfg, state, fake.system())
    return asked(fake)


def test_slots_somebody_holds_are_told_the_machines_pin(cfg):
    # slot04's wipe fails, so it is still there to be asked about while releasing.
    fake = Fake(users=["slot01", "slot02", "slot03", "slot04"],
                script_codes={("slot-remove.sh", "slot04"): (1, "busy\n")}, desired={
        "claude_version": "2.1.300", "slots": [
            {"unix_user": "slot01", "state": "claimed"},
            {"unix_user": "slot02", "state": "active"},
            {"unix_user": "slot03", "state": "free"},
            {"unix_user": "slot04", "state": "releasing"}]})
    requests = two_runs(cfg, fake)
    for held in ("slot01", "slot02"):
        request, _ = requests[held]
        assert request["claude_version"] == "2.1.300" and request["may_upgrade"] is True
    for other in ("slot03", "slot04"):
        request, _ = requests[other]
        assert "claude_version" not in request and "may_upgrade" not in request, \
            f"{other} is not held, and was told to follow the pin"


def test_a_slot_being_signed_into_is_told_to_stay_put(cfg):
    """A restart in the middle of somebody's sign-in would cut it short."""
    login = {"requested_at": NOW, "kind": "login"}
    fake = Fake(users=["slot01"], desired={"claude_version": "2.1.300", "slots": [
        {"unix_user": "slot01", "state": "active", "login": login}]})
    request, timeout = two_runs(cfg, fake)["slot01"]
    assert request["claude_version"] == "2.1.300" and request["may_upgrade"] is False
    assert timeout == machine.SLOT_FACTS_TIMEOUT_S, "waited for an install that is not allowed"


@pytest.mark.parametrize("sent", ["--force", "stable; rm -rf /", 2.1, None, "x" * 50])
def test_a_version_the_installer_must_not_see_never_reaches_a_slot(cfg, sent):
    desired = {"slots": [{"unix_user": "slot01", "state": "active"}]}
    if sent is not None:
        desired["claude_version"] = sent
    fake = Fake(users=["slot01"], desired=desired)
    request, timeout = two_runs(cfg, fake)["slot01"]
    assert request["claude_version"] == ""
    assert timeout == machine.SLOT_FACTS_TIMEOUT_S


def test_a_channel_is_passed_on_as_a_channel(cfg):
    fake = Fake(users=["slot01"], desired={"claude_version": "stable", "slots": [
        {"unix_user": "slot01", "state": "active"}]})
    request, _ = two_runs(cfg, fake)["slot01"]
    assert request["claude_version"] == "stable"


def test_a_slot_that_may_upgrade_is_given_time_for_the_installer(cfg):
    """A download, not a probe: the ordinary budget would kill it half way."""
    fake = Fake(users=["slot01"], desired={"claude_version": "2.1.300", "slots": [
        {"unix_user": "slot01", "state": "active"}]})
    _, timeout = two_runs(cfg, fake)["slot01"]
    assert timeout == machine.SLOT_FACTS_TIMEOUT_S + machine.core.INSTALL_TIMEOUT_S


def test_what_a_slot_says_about_its_upgrade_reaches_the_server(cfg):
    fake = Fake(users=["slot01"], facts={
        "claude": {"version": "2.1.300"},
        "upgrade": {"from": "2.1.278", "to": "2.1.300", "ok": True, "restart": "waiting"}})
    entry = machine.slot_report("slot01", {}, cfg, fake.system(), False)
    assert entry["upgrade"] == {"from": "2.1.278", "to": "2.1.300", "ok": True,
                                "restart": "waiting"}


def test_an_upgrade_report_that_is_not_a_record_is_dropped(cfg):
    fake = Fake(users=["slot01"], facts={"claude": {"version": "2.1.300"},
                                         "upgrade": "installed everything"})
    assert "upgrade" not in machine.slot_report("slot01", {}, cfg, fake.system(), False)


def test_the_machine_says_when_its_os_wants_a_reboot(cfg, tmp_path, monkeypatch):
    flag = tmp_path / "reboot-required"
    monkeypatch.setenv("CCFLEET_REBOOT_REQUIRED_FILE", str(flag))
    fake = Fake()
    assert machine.machine_payload(cfg, {}, fake.system())["reboot_required"] is False
    flag.write_text("")
    assert machine.machine_payload(cfg, {}, fake.system())["reboot_required"] is True
