"""machine-setup.sh: turning a server into a shared machine.

It runs as root and writes to /usr/local, /etc and /var. Every one of those is
overridable, and the autouse fixture below points all of them at a sandbox —
the lesson of a slot-script test that reached the real /etc/sudoers.d and was
only caught by CI. The commands it reaches for as root are stubbed on PATH,
and the files it installs are copied from this checkout, never fetched.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "node" / "machine-setup.sh"
TOKEN = "a" * 64
GOOD = ["--server", "https://fleet.example.com", "--node", "shared-1", "--token", TOKEN]


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "system"
    for name, var in (("lib", "CCFLEET_LIB_DIR"), ("etc", "CCFLEET_ETC_DIR"),
                      ("state", "CCFLEET_STATE_DIR"), ("units", "CCFLEET_UNIT_DIR"),
                      ("home", "CCFLEET_HOME_ROOT")):
        monkeypatch.setenv(var, str(root / name))
    (root / "units").mkdir(parents=True)
    (root / "home").mkdir()
    monkeypatch.setenv("CCFLEET_SOURCE_DIR", str(REPO))
    # If anything tried to fetch, it would fail loudly rather than reach GitHub.
    monkeypatch.setenv("CCFLEET_REPO_RAW", "http://127.0.0.1:9/never")
    return root


def fakebin(tmp_path, *, timer_state="active", first_run_ok=True):
    """Root's view of the world: `id -u` is 0, and the rest just logs."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    calls.write_text("")
    stubs = {
        "id": 'if [ "$1" = "-u" ]; then echo 0; exit 0; fi; exit 1',
        "apt-get": 'echo "apt-get $*" >> "__LOG__"',
        "chown": 'echo "chown $*" >> "__LOG__"',
        "curl": 'echo "curl $*" >> "__LOG__"; exit 1',
        "systemctl": textwrap.dedent("""\
            echo "systemctl $*" >> "__LOG__"
            case "$1" in
              is-active) echo "__TIMER__" ;;
              start) exit __START__ ;;
            esac
            exit 0"""),
    }
    for name, body in stubs.items():
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body.replace("__LOG__", str(calls))
                        .replace("__TIMER__", timer_state)
                        .replace("__START__", "0" if first_run_ok else "1") + "\n")
        path.chmod(0o755)
    return bindir, calls


def run(args, bindir=None):
    env = dict(os.environ)
    if bindir is not None:
        env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(["bash", str(SETUP), *args], capture_output=True, text=True,
                          timeout=60, env=env)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# -- refusals, before anything is touched ----------------------------------------

def test_it_ships_and_parses():
    subprocess.run(["bash", "-n", str(SETUP)], check=True, timeout=30)


def test_no_arguments_prints_usage():
    r = run([])
    assert r.returncode == 2 and "--server" in r.stdout + r.stderr


def test_usage_works_when_piped_into_bash():
    r = subprocess.run(["bash", "-s", "--"], input=SETUP.read_text(), capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 2 and "--server" in r.stdout + r.stderr


@pytest.mark.parametrize("flag,bad", [
    ("--server", "fleet.example.com"), ("--server", "https://x y"),
    ("--node", "Shared-1"), ("--node", "s"), ("--node", "a;b"),
    ("--token", "short"), ("--token", "g" * 64),
])
def test_bad_arguments_are_refused_without_root(flag, bad):
    args = list(GOOD)
    args[args.index(flag) + 1] = bad
    r = run(args)
    assert r.returncode != 0
    assert flag in r.stderr or "http" in r.stderr


def test_good_arguments_stop_at_the_root_check_when_not_root(sandbox):
    r = run(GOOD)
    assert r.returncode != 0 and "root" in r.stderr
    assert not (sandbox / "lib").exists()


def test_an_owner_agent_already_reporting_as_this_node_stops_it_cold(tmp_path, sandbox):
    """Two agents under one node id would take turns telling the server what
    the machine is. Refused before a single file is written."""
    env_file = sandbox / "home" / "erik" / ".config" / "ccfleet" / "agent.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("CCFLEET_URL=https://f\nCCFLEET_NODE_ID=shared-1\nCCFLEET_NODE_TOKEN=x\n")
    bindir, calls = fakebin(tmp_path)
    r = run(GOOD, bindir)
    assert r.returncode != 0
    assert "already reports as shared-1" in r.stderr
    assert not (sandbox / "lib").exists() and not (sandbox / "etc").exists()
    assert calls.read_text() == "", "changed the machine before refusing"


def test_an_owner_agent_for_another_node_is_no_obstacle(tmp_path, sandbox):
    env_file = sandbox / "home" / "erik" / ".config" / "ccfleet" / "agent.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("CCFLEET_NODE_ID=shared-10\n")
    bindir, _ = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0


# -- what it installs ------------------------------------------------------------

def test_it_installs_the_agent_the_scripts_and_the_timer(tmp_path, sandbox):
    bindir, calls = fakebin(tmp_path)
    r = run(GOOD, bindir)
    assert r.returncode == 0, r.stderr
    lib = sandbox / "lib"
    for rel in ("ccfleet_agent/__init__.py", "ccfleet_agent/agent.py",
                "ccfleet_agent/machine.py", "slot-add.sh", "slot-remove.sh",
                "systemd/ccfleet-shell.service", "systemd/claude-remote-control.service"):
        installed = lib / rel
        source = REPO / ("node/" + rel if rel.endswith(".sh") or rel.startswith("systemd")
                         else rel)
        assert installed.read_bytes() == source.read_bytes(), rel
    assert mode(lib / "slot-add.sh") == 0o755 and mode(lib / "slot-remove.sh") == 0o755
    assert mode(lib / "ccfleet_agent" / "agent.py") == 0o644
    # Root runs these and every slot's user runs agent.py: nobody else may write.
    for directory in (lib, lib / "ccfleet_agent", lib / "systemd"):
        assert mode(directory) == 0o755
    assert f"chown -R root:root {lib}" in calls.read_text()
    fetched = [line for line in calls.read_text().splitlines() if line.startswith("curl ")]
    assert fetched == [], "fetched instead of using the checkout"


def test_the_token_lives_in_one_file_only_root_can_read(tmp_path, sandbox):
    bindir, _ = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0
    env_file = sandbox / "etc" / "agent.env"
    assert mode(env_file) == 0o600
    assert mode(sandbox / "etc") == 0o700 and mode(sandbox / "state") == 0o700
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values == {"CCFLEET_URL": "https://fleet.example.com",
                      "CCFLEET_NODE_ID": "shared-1", "CCFLEET_NODE_TOKEN": TOKEN,
                      "CCFLEET_LIB_DIR": str(sandbox / "lib"),
                      "CCFLEET_STATE_FILE": str(sandbox / "state" / "machine.json")}
    for unit in (sandbox / "units").iterdir():
        assert TOKEN not in unit.read_text(), f"the token leaked into {unit.name}"


def test_the_service_runs_what_was_just_installed(tmp_path, sandbox):
    bindir, _ = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0
    unit = (sandbox / "units" / "ccfleet-machine.service").read_text()
    assert f"{sandbox / 'lib'}/ccfleet_agent/machine.py" in unit
    assert f"--env-file {sandbox / 'etc'}/agent.env" in unit
    assert "/usr/local/lib/ccfleet" not in unit and "/etc/ccfleet" not in unit
    assert sorted(p.name for p in (sandbox / "units").iterdir()) == [
        "ccfleet-machine.service", "ccfleet-machine.timer"], "left a sed backup behind"


def test_it_turns_the_timer_on_and_runs_once(tmp_path):
    bindir, calls = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0
    log = calls.read_text().splitlines()
    systemctl = [line for line in log if line.startswith("systemctl")]
    assert systemctl == ["systemctl daemon-reload",
                         "systemctl enable --now ccfleet-machine.timer",
                         "systemctl start ccfleet-machine.service",
                         "systemctl is-active ccfleet-machine.timer"]
    install = next(line for line in log if " install " in line)
    for package in ("python3", "tmux", "sudo", "adduser", "libpam-systemd"):
        assert package in install.split(), f"{package} not installed"


def test_a_first_run_that_fails_is_a_failed_setup(tmp_path):
    bindir, _ = fakebin(tmp_path, first_run_ok=False)
    r = run(GOOD, bindir)
    assert r.returncode != 0 and "journalctl -u ccfleet-machine.service" in r.stderr


def test_a_timer_that_did_not_come_up_is_a_failed_setup(tmp_path):
    """Reporting once and never again looks exactly like a working machine."""
    bindir, _ = fakebin(tmp_path, timer_state="inactive")
    r = run(GOOD, bindir)
    assert r.returncode != 0 and "never again" in r.stderr


def test_running_it_again_tightens_a_token_file_someone_loosened(tmp_path, sandbox):
    """umask only shapes a file being created. Written over an existing one,
    the old mode stays — so a hand-made agent.env left world-readable would
    keep the token readable by every slot on the machine."""
    (sandbox / "etc").mkdir()
    loose = sandbox / "etc" / "agent.env"
    loose.write_text("old\n")
    loose.chmod(0o644)
    bindir, _ = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0
    assert mode(loose) == 0o600


def test_a_loose_umask_does_not_leave_roots_code_writable(tmp_path, sandbox):
    """A group-writable umask would let a group member change what root runs
    next. The directories are set, not left to whoever ran this."""
    bindir, _ = fakebin(tmp_path)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run(["bash", "-c", 'umask 000; exec bash "$0" "$@"', str(SETUP), *GOOD],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    lib = sandbox / "lib"
    for directory in (lib, lib / "ccfleet_agent", lib / "systemd"):
        assert mode(directory) == 0o755, directory
    for script in ("slot-add.sh", "slot-remove.sh"):
        assert mode(lib / script) == 0o755
    assert mode(lib / "ccfleet_agent" / "machine.py") == 0o644
    assert mode(sandbox / "etc") == 0o700 and mode(sandbox / "state") == 0o700
