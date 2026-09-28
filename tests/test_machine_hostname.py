"""A shared machine answers to its one slot's holder-chosen name."""

from __future__ import annotations

import os

import pytest

from ccfleet_agent import machine
from tests.test_machine import Fake

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
                 rc_active=(), scripted=None, **kwargs):
        super().__init__(**kwargs)
        # What `systemctl --user <verb>` answers, in turn, before the defaults.
        self.scripted = {verb: list(codes) for verb, codes in (scripted or {}).items()}
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
            if self.scripted.get(argv[2]):
                return self.scripted[argv[2]].pop(0), ""
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
    ours = [ln.split() for ln in host.hosts.read_text().splitlines()
            if ln.split()[:1] == ["127.0.1.1"]]
    assert ours == [["127.0.1.1", "pool-1"]]
    assert "alice-1" not in host.hosts.read_text()


def test_the_last_holders_name_is_not_left_behind(cfg, tmp_path):
    """/etc/hosts is readable by the next holder, and the old name is somebody's."""
    host = Host(tmp_path, hostname="alice-1", desired={"hostname": "bob-1", "slots": []})
    cycle(cfg, host)
    assert "alice-1" not in host.hosts.read_text()
    assert "127.0.1.1\tbob-1 pool-1" in host.hosts.read_text().splitlines()


def test_a_rename_that_failed_leaves_the_name_it_still_answers_to_and_no_other(cfg, tmp_path):
    """sudo still resolves the name the machine goes by, and the name it could
    not take — somebody's — is not left in a file every slot can read."""
    host = Host(tmp_path, hostname="bob-1", hostnamectl_works=False,
                desired={"hostname": "alice-2", "slots": []})
    cycle(cfg, host)
    assert host.name == "bob-1"
    ours = [ln.split() for ln in host.hosts.read_text().splitlines()
            if ln.split()[:1] == ["127.0.1.1"]]
    assert ours == [["127.0.1.1", "bob-1", "pool-1"]]
    assert "alice-2" not in host.hosts.read_text()
    assert not (host.cloud / "99-ccfleet.cfg").exists()


def test_while_renaming_the_running_name_resolves(cfg, tmp_path):
    """At the moment hostnamectl runs, /etc/hosts already names both."""
    host = Host(tmp_path, hostname="bob-1", desired={"hostname": "alice-2", "slots": []})
    seen = []
    real = host.set_hostname

    def checking(name):
        seen.append(host.hosts.read_text())
        return real(name)
    host.set_hostname = checking
    cycle(cfg, host)
    assert "127.0.1.1\talice-2 pool-1 bob-1" in seen[0].splitlines()


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


def test_a_name_that_took_stays_taken_even_when_later_tidying_fails(cfg, tmp_path):
    host = Host(tmp_path, users=("slot01",), rc_active=("slot01",),
                desired={"hostname": "alice-2", "slots": [
                    {"unix_user": "slot01", "state": "active"}]})
    (host.cloud / ".99-ccfleet.cfg.ccfleet-tmp").mkdir()      # the write there will fail
    cycle(cfg, host, {"slots": ["slot01"]})
    assert host.name == "alice-2"
    assert host.systemctl == []


def test_a_name_it_already_answers_to_is_not_taken_again(cfg, tmp_path):
    """No hostnamectl and no rewrite of a /etc/hosts that already says it —
    but the cloud-init drop-in that keeps the name across a reboot is made
    sure of, on every run, not only the one that renamed."""
    host = Host(tmp_path, desired={"hostname": "pool-1", "slots": []})
    cycle(cfg, host)
    assert host.order == [] and host.hosts.read_text() == HOSTS
    assert (host.cloud / "99-ccfleet.cfg").read_text() == (
        "preserve_hostname: true\nmanage_etc_hosts: false\n")


def test_no_name_asked_for_changes_nothing(cfg, tmp_path):
    host = Host(tmp_path, desired={"slots": []})
    cycle(cfg, host)
    assert host.order == [] and host.hosts.read_text() == HOSTS
    assert not (host.cloud / "99-ccfleet.cfg").exists()


def test_a_run_with_nothing_to_change_writes_nothing(cfg, tmp_path):
    host = Host(tmp_path, desired={"hostname": "alice-1", "slots": []})
    cycle(cfg, host)
    cloud = host.cloud / "99-ccfleet.cfg"
    before = (host.hosts.stat().st_ino, cloud.stat().st_ino)
    cycle(cfg, host)
    assert (host.hosts.stat().st_ino, cloud.stat().st_ino) == before


def test_a_last_holders_name_left_in_hosts_is_dropped_on_a_later_run(cfg, tmp_path):
    """As if the run that renamed could not tidy up: the next one does."""
    host = Host(tmp_path, hostname="alice-2", desired={"hostname": "alice-2", "slots": []})
    host.hosts.write_text("127.0.0.1 localhost\n127.0.1.1\talice-2 pool-1 bob-1\n")
    cycle(cfg, host)
    assert host.hosts.read_text() == "127.0.0.1 localhost\n127.0.1.1\talice-2 pool-1\n"
    assert host.order == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_hosts_file_that_cannot_be_read_is_left_alone_and_nothing_renamed(cfg, tmp_path):
    """Rewriting what could not be read could drop entries the machine needs."""
    host = Host(tmp_path, desired={"hostname": "alice-1", "slots": []})
    host.hosts.chmod(0)                             # there, writable around, unreadable
    try:
        cycle(cfg, host)
    finally:
        host.hosts.chmod(0o644)
    assert host.name == "pool-1" and host.order == [] and host.hosts.read_text() == HOSTS


def test_the_log_never_names_a_holder(cfg, tmp_path, caplog):
    """The names come from people's addresses. Renamed or failed, the log
    says what happened without saying who."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    renamed = Host(tmp_path / "a", hostname="bob-1", users=("slot01",), rc_active=("slot01",),
                   scripted={"restart": [1]}, desired={"hostname": "alice-2", "slots": [
                       {"unix_user": "slot01", "state": "active"}]})
    refused = Host(tmp_path / "b", hostname="bob-1", hostnamectl_works=False,
                   desired={"hostname": "alice-2", "slots": []})
    with caplog.at_level("DEBUG", logger="ccfleet-machine"):
        cycle(cfg, renamed, {"slots": ["slot01"]})
        cycle(cfg, refused)
    said = " ".join(r.getMessage() for r in caplog.records)
    assert said and "alice" not in said and "bob" not in said


@pytest.mark.parametrize("bad", ["Alice-1", "-alice", "alice-", "alice_1", "a.b", "a" * 64,
                                 "alice\n", "", "pool-1; reboot", 7, None, ["alice-1"]])
def test_a_name_that_is_no_hostname_is_never_applied(cfg, tmp_path, bad):
    """It reaches hostnamectl and /etc/hosts as root: checked here as well."""
    host = Host(tmp_path, desired={"hostname": bad, "slots": []})
    cycle(cfg, host)
    assert host.order == [] and host.hosts.read_text() == HOSTS


# -- order --------------------------------------------------------------------------------

def test_the_name_is_taken_before_a_claim_is_provisioned(cfg, tmp_path):
    """Provisioning sees the final machine identity from its first process."""
    host = Host(tmp_path, desired={"hostname": "alice-1", "slots": [
        {"unix_user": "slot01", "state": "claiming", "claimed_at": 1700000000.0}]})
    cycle(cfg, host)
    assert host.order[:2] == ["hostname alice-1", "script slot-add.sh slot01"]
