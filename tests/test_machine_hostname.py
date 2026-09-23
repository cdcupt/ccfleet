"""A shared machine answers to its slot's name (slot model v2, I2).

One machine is one slot, and claude.ai/code shows a machine by its hostname,
so the server tells the machine what to call itself — its holder's name while
the slot is held, its own id while it is free — and the machine agent, as
root, makes it so: before it provisions a claim, so the holder's Remote
Control first registers under their name, and for a slot already running
Remote Control, by restarting it so the new name takes.
"""

from __future__ import annotations

import pytest

from ccfleet_agent import machine
from tests.test_machine import Fake, account

HOSTS = ("127.0.0.1\tlocalhost\n"
         "10.1.2.3 C202608222119842.local C202608222119842\n"
         "127.0.1.1 pool-1\n"
         "::1 localhost ip6-localhost ip6-loopback\n")


@pytest.fixture
def cfg(tmp_path):
    return machine.MachineConfig.from_env({
        "CCFLEET_URL": "https://fleet.example", "CCFLEET_NODE_ID": "pool-1",
        "CCFLEET_NODE_TOKEN": "t" * 64, "CCFLEET_LIB_DIR": str(tmp_path / "lib"),
        "CCFLEET_STATE_FILE": str(tmp_path / "state" / "machine.json"),
        "CCFLEET_EGRESS_TARGETS": "https://egress.invalid"})


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr(machine.core, "egress_ip", lambda *a, **k: {"ip": None, "source": None})


class Host(Fake):
    """A machine with a hostname, an /etc/hosts and cloud-init, all on disk in
    a temporary directory, and slot users whose Remote Control is running or not."""

    def __init__(self, tmp_path, hostname="pool-1", cloud=True, hostnamectl_works=True,
                 rc_active=(), **kwargs):
        super().__init__(**kwargs)
        self.name = hostname
        self.hosts = tmp_path / "hosts"
        self.hosts.write_text(HOSTS)
        self.cloud = tmp_path / "cloud.cfg.d"
        if cloud:
            self.cloud.mkdir()
        self.hostnamectl_works = hostnamectl_works
        self.rc_active = set(rc_active)
        self.order: list[str] = []
        self.systemctl: list[tuple[str, tuple[str, ...]]] = []

    def set_hostname(self, name):
        self.order.append(f"hostname {name}")
        if self.hostnamectl_works:
            self.name = name
        return self.hostnamectl_works

    def runner(self, argv, **kwargs):
        self.order.append(f"script {argv[0].rsplit('/', 1)[-1]} {argv[2]}")
        return super().runner(argv, **kwargs)

    def spawn(self, argv, **kwargs):
        if argv[:2] == ["systemctl", "--user"]:
            user = next(u for u, a in self.users.items() if a.pw_uid == kwargs["user"])
            self.systemctl.append((user, tuple(argv[2:])))
            if argv[2] == "is-active":
                return (0 if user in self.rc_active else 3), ""
            return 0, ""
        return super().spawn(argv, **kwargs)

    def system(self):
        return machine.System(lookup=self.lookup, groups_of=self.groups_of, spawn=self.spawn,
                              runner=self.runner, opener=self.opener, clock=lambda: machine.time.time(),
                              hostname=lambda: self.name, set_hostname=self.set_hostname,
                              hosts_path=self.hosts, cloud_cfg_dir=self.cloud)


def cycle(cfg, host, state=None):
    return machine.run_cycle(cfg, state or {}, host.system())


# -- taking the name ---------------------------------------------------------------------

def test_the_machine_takes_the_name_it_is_given(cfg, tmp_path):
    host = Host(tmp_path, desired={"hostname": "alice-1", "slots": []})
    cycle(cfg, host)
    assert host.name == "alice-1"
    lines = host.hosts.read_text().splitlines()
    assert [ln for ln in lines if ln.split()[:1] == ["127.0.1.1"]] == ["127.0.1.1\talice-1 pool-1"], \
        "one 127.0.1.1 line: the new name, with the node's id beside it so sudo always resolves"
    assert (host.cloud / "99-ccfleet.cfg").read_text() == (
        "preserve_hostname: true\nmanage_etc_hosts: false\n")


def test_every_other_hosts_line_is_left_alone(cfg, tmp_path):
    host = Host(tmp_path, desired={"hostname": "alice-1", "slots": []})
    cycle(cfg, host)
    text = host.hosts.read_text()
    for kept in ("127.0.0.1\tlocalhost", "10.1.2.3 C202608222119842.local C202608222119842",
                 "::1 localhost ip6-localhost ip6-loopback"):
        assert kept in text


def test_a_hosts_file_with_no_127_0_1_1_line_gets_one(cfg, tmp_path):
    host = Host(tmp_path, desired={"hostname": "alice-1", "slots": []})
    host.hosts.write_text("127.0.0.1 localhost\n")
    cycle(cfg, host)
    assert "127.0.1.1\talice-1 pool-1" in host.hosts.read_text().splitlines()


def test_the_node_id_is_not_repeated_when_it_is_the_name(cfg, tmp_path):
    host = Host(tmp_path, hostname="alice-1", desired={"hostname": "pool-1", "slots": []})
    cycle(cfg, host)
    assert "127.0.1.1\tpool-1" in host.hosts.read_text().splitlines()


def test_the_last_holders_name_is_not_left_behind(cfg, tmp_path):
    """/etc/hosts is readable by the next holder, and the old name is somebody's."""
    host = Host(tmp_path, hostname="alice-1", desired={"hostname": "bob-1", "slots": []})
    cycle(cfg, host)
    assert "alice-1" not in host.hosts.read_text()
    assert "127.0.1.1\tbob-1 pool-1" in host.hosts.read_text().splitlines()


def test_while_a_rename_fails_the_running_name_still_resolves(cfg, tmp_path):
    """So sudo never meets a hostname /etc/hosts does not know."""
    host = Host(tmp_path, hostname="bob-1", hostnamectl_works=False,
                desired={"hostname": "alice-2", "slots": []})
    cycle(cfg, host)
    assert host.name == "bob-1"
    assert "127.0.1.1\talice-2 pool-1 bob-1" in host.hosts.read_text().splitlines()
    assert not (host.cloud / "99-ccfleet.cfg").exists()


def test_the_machine_reports_the_name_it_answers_to(cfg, tmp_path):
    host = Host(tmp_path, hostname="alice-1", desired={"slots": []})
    cycle(cfg, host)
    assert host.posted[-1]["hostname"] == "alice-1"


def test_without_cloud_init_there_is_no_cloud_file_and_nothing_to_warn_about(
        cfg, tmp_path, caplog):
    host = Host(tmp_path, cloud=False, desired={"hostname": "alice-1", "slots": []})
    with caplog.at_level("WARNING", logger="ccfleet-machine"):
        cycle(cfg, host)
    assert host.name == "alice-1" and not host.cloud.exists()
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_a_name_that_took_is_followed_even_when_tidying_up_after_it_fails(cfg, tmp_path):
    """The rename has happened once hostnamectl says so: Remote Control must
    follow it whatever goes wrong after, or claude.ai keeps the old name."""
    host = Host(tmp_path, users=("slot01",), rc_active=("slot01",),
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "active"}]})
    (host.cloud / ".99-ccfleet.cfg.ccfleet-tmp").mkdir()      # the write there will fail
    cycle(cfg, host, {"slots": ["slot01"]})
    assert host.name == "alice-2"
    assert ("slot01", ("restart", "claude-remote-control.service")) in host.systemctl


def test_a_name_it_already_answers_to_changes_nothing(cfg, tmp_path):
    host = Host(tmp_path, desired={"hostname": "pool-1", "slots": []})
    cycle(cfg, host)
    assert host.order == [] and host.hosts.read_text() == HOSTS
    assert not (host.cloud / "99-ccfleet.cfg").exists()


def test_no_name_asked_for_changes_nothing(cfg, tmp_path):
    host = Host(tmp_path, desired={"slots": []})
    cycle(cfg, host)
    assert host.order == [] and host.hosts.read_text() == HOSTS


@pytest.mark.parametrize("bad", ["Alice-1", "-alice", "alice-", "alice_1", "a.b", "a" * 64,
                                 "alice\n", "", "pool-1; reboot", 7, None, ["alice-1"]])
def test_a_name_that_is_no_hostname_is_never_applied(cfg, tmp_path, bad):
    """It reaches hostnamectl and /etc/hosts as root: checked here as well."""
    host = Host(tmp_path, desired={"hostname": bad, "slots": []})
    cycle(cfg, host)
    assert host.order == [] and host.hosts.read_text() == HOSTS


# -- order --------------------------------------------------------------------------------

def test_the_name_is_taken_before_a_claim_is_provisioned(cfg, tmp_path):
    """So the holder's Remote Control first registers under their name."""
    host = Host(tmp_path, desired={"hostname": "alice-1", "slots": [
        {"unix_user": "slot01", "state": "claiming", "claimed_at": 1700000000.0}]})
    cycle(cfg, host)
    assert host.order[:2] == ["hostname alice-1", "script slot-add.sh slot01"]


# -- Remote Control follows the name -------------------------------------------------------

def test_a_running_remote_control_restarts_under_the_new_name(cfg, tmp_path):
    host = Host(tmp_path, users=("slot01",), rc_active=("slot01",),
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "active"}]})
    cycle(cfg, host, {"slots": ["slot01"]})
    assert host.systemctl == [
        ("slot01", ("daemon-reload",)),
        ("slot01", ("is-active", "--quiet", "claude-remote-control.service")),
        ("slot01", ("restart", "claude-remote-control.service"))]


def test_a_remote_control_that_is_not_running_is_not_started(cfg, tmp_path):
    """Reloaded, so it starts under the new name whenever it does; not started."""
    host = Host(tmp_path, users=("slot01",), rc_active=(),
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "claimed"}]})
    cycle(cfg, host, {"slots": ["slot01"]})
    assert ("slot01", ("daemon-reload",)) in host.systemctl
    assert not [c for c in host.systemctl if c[1][0] == "restart"]


def test_no_rename_restarts_nothing(cfg, tmp_path):
    host = Host(tmp_path, hostname="alice-2", users=("slot01",), rc_active=("slot01",),
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "active"}]})
    cycle(cfg, host, {"slots": ["slot01"]})
    assert host.systemctl == []


def test_a_rename_that_failed_restarts_nothing(cfg, tmp_path):
    host = Host(tmp_path, users=("slot01",), rc_active=("slot01",), hostnamectl_works=False,
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "active"}]})
    cycle(cfg, host, {"slots": ["slot01"]})
    assert host.systemctl == [] and host.name == "pool-1"


def test_only_slot_users_are_touched(cfg, tmp_path):
    """A login on the machine that is not a slot's is nobody this agent acts as."""
    host = Host(tmp_path, users=("slot01", "erik"), rc_active=("slot01", "erik"),
                groups={"slot01": {"ccfleet-slots"}, "erik": {"sudo"}},
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "active"}]})
    cycle(cfg, host, {"slots": ["slot01", "erik"]})
    assert {user for user, _ in host.systemctl} == {"slot01"}


def test_the_agent_runs_as_the_slot_user_with_its_own_environment(cfg, tmp_path):
    host = Host(tmp_path, users=("slot01",), rc_active=("slot01",),
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "active"}]})
    seen = []
    real = host.spawn

    def spy(argv, **kwargs):
        if argv[:2] == ["systemctl", "--user"]:
            seen.append(kwargs)
        return real(argv, **kwargs)
    host.spawn = spy
    cycle(cfg, host, {"slots": ["slot01"]})
    slot = account("slot01", host.users["slot01"].pw_uid)
    assert seen and all(k["user"] == slot.pw_uid and k["extra_groups"] == [] and
                        k["env"]["XDG_RUNTIME_DIR"] == f"/run/user/{slot.pw_uid}" and
                        "CCFLEET_NODE_TOKEN" not in k["env"] for k in seen)
