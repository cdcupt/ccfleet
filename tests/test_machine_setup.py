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
                      ("home", "CCFLEET_HOME_ROOT"),
                      ("sshd", "CCFLEET_SSHD_DROPIN_DIR")):
        monkeypatch.setenv(var, str(root / name))
    (root / "units").mkdir(parents=True)
    (root / "home").mkdir()
    (root / "sshd").mkdir()
    sshd_config = root / "sshd_config"
    sshd_config.write_text(f"Include {root / 'sshd'}/*.conf\n")
    monkeypatch.setenv("CCFLEET_SSHD_CONFIG", str(sshd_config))
    monkeypatch.setenv("CCFLEET_MANAGED_SETTINGS_FILE", str(root / "managed/90-egress.json"))
    monkeypatch.setenv("CCFLEET_SOURCE_DIR", str(REPO))
    # If anything tried to fetch, it would fail loudly rather than reach GitHub.
    monkeypatch.setenv("CCFLEET_REPO_RAW", "http://127.0.0.1:9/never")
    return root


def fakebin(tmp_path, *, timer_state="active", first_run_ok=True, sshd_ok=True,
            keys_audit_ok=True, shell_audit_ok=True, egress_ok=True,
            slot_group_exists=True, addgroup_ok=True):
    """Root's view of the world: `id -u` is 0, and the rest just logs."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    calls.write_text("")
    password_set = tmp_path / "password-set.marker"
    slot_group = tmp_path / "slot-group.marker"
    if slot_group_exists:
        slot_group.touch()
    stubs = {
        "id": textwrap.dedent("""\
            if [ "$1" = -u ] && [ $# = 1 ]; then echo 0; exit 0; fi
            if [ "$1" = -u ]; then echo "${FAKE_SLOT_UID:-1001}"; exit 0; fi
            if [ "$1" = -nG ]; then echo "${FAKE_SLOT_GROUPS:-ccfleet-slots}"; exit 0; fi
            exit 1"""),
        "apt-get": 'echo "apt-get $*" >> "__LOG__"',
        "chown": 'echo "chown $*" >> "__LOG__"',
        "curl": 'echo "curl $*" >> "__LOG__"; exit 1',
        "getent": textwrap.dedent("""\
            if [ "$1" = passwd ]; then
              echo "$2:x:1001:1001::/home/$2:${FAKE_SLOT_SHELL:-/bin/bash}"
              exit 0
            fi
            if [ "$1" = group ] && [ "$2" = ccfleet-slots ]; then
              [ "${FAKE_GROUP_LOOKUP_ERROR:-0}" -eq 0 ] || exit "$FAKE_GROUP_LOOKUP_ERROR"
              [ -f "__SLOT_GROUP__" ] || exit 2
              echo "ccfleet-slots:x:999:${FAKE_SLOT_MEMBERS:-}"
              exit 0
            fi
            exit 2"""),
        "addgroup": textwrap.dedent("""\
            echo "addgroup $*" >> "__LOG__"
            [ "$*" = "--system ccfleet-slots" ] || exit 1
            [ __ADDGROUP__ -eq 0 ] || exit 1
            touch "__SLOT_GROUP__"
            """),
        "usermod": 'echo "usermod $*" >> "__LOG__"',
        "python3": textwrap.dedent("""\
            echo "python3 $*" >> "__LOG__"
            case "$2" in
              */egress.py) exit __EGRESS__ ;;
            esac
            case "$3" in
              --audit-authorized-keys) exit __KEYS_AUDIT__ ;;
              --audit-login-shell) exit __SHELL_AUDIT__ ;;
            esac
            exit 1"""),
        "passwd": textwrap.dedent("""\
            if [ "$1" = -S ]; then
              if [ -f "__PASSWORD_SET__" ]; then echo "$2 P 01/01/1970 0 99999 7 -1";
              else echo "$2 L 01/01/1970 0 99999 7 -1"; fi
              exit 0
            fi
            exit 1"""),
        "chpasswd": textwrap.dedent("""\
            IFS=: read -r user secret
            [ "$user" = slot01 ] && [ ${#secret} -ge 64 ] || exit 1
            echo "chpasswd slot01:<redacted>" >> "__LOG__"
            touch "__PASSWORD_SET__"
            """),
        "sshd": 'echo "sshd $*" >> "__LOG__"; exit __SSHD__',
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
                        .replace("__PASSWORD_SET__", str(password_set))
                        .replace("__SLOT_GROUP__", str(slot_group))
                        .replace("__ADDGROUP__", "0" if addgroup_ok else "1")
                        .replace("__TIMER__", timer_state)
                        .replace("__START__", "0" if first_run_ok else "1")
                        .replace("__KEYS_AUDIT__", "0" if keys_audit_ok else "2")
                        .replace("__SHELL_AUDIT__", "0" if shell_audit_ok else "2")
                        .replace("__EGRESS__", "0" if egress_ok else "2")
                        .replace("__SSHD__", "0" if sshd_ok else "1") + "\n")
        path.chmod(0o755)
    return bindir, calls


def run(args, bindir=None, *, canonical_egress_dir=None):
    env = dict(os.environ)
    if bindir is not None:
        env["PATH"] = f"{bindir}:{env['PATH']}"
    script = SETUP
    if canonical_egress_dir is not None:
        # A test-only script copy maps its fixed system path into the sandbox.
        # Production has no configurable runtime path or bypass for this guard.
        script = canonical_egress_dir.parent / "sandbox-machine-setup.sh"
        original = 'CANONICAL_EGRESS_ETC_DIR="/etc/ccfleet"'
        assert SETUP.read_text().count(original) == 1
        script.write_text(SETUP.read_text().replace(
            original, f'CANONICAL_EGRESS_ETC_DIR="{canonical_egress_dir}"'))
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
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

@pytest.mark.parametrize("group_exists", [False, True])
def test_slot_group_exists_before_existing_slot_audits_and_reruns_do_not_recreate_it(
        tmp_path, sandbox, group_exists):
    """Real getent exit 2 on an empty fresh host must not abort under pipefail."""
    bindir, calls = fakebin(tmp_path, slot_group_exists=group_exists)
    result = run(GOOD, bindir)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    creation = "addgroup --system ccfleet-slots"
    audit = f"python3 -I {sandbox / 'lib/ccfleet_agent/machine.py'} --audit-login-shell"
    assert lines.count(creation) == (0 if group_exists else 1)
    if not group_exists:
        assert lines.index(creation) < lines.index(audit)
    assert "systemctl start ccfleet-machine.service" in lines
    calls.write_text("")
    result = run(GOOD, bindir)
    assert result.returncode == 0, result.stderr
    assert creation not in calls.read_text()
    assert audit in calls.read_text()


def test_slot_group_creation_failure_stops_before_audits_ssh_and_timer(tmp_path, sandbox):
    bindir, calls = fakebin(tmp_path, slot_group_exists=False, addgroup_ok=False)
    result = run(GOOD, bindir)
    assert result.returncode != 0 and "could not create the managed slot group" in result.stderr
    assert not (sandbox / "sshd/02-ccfleet-slots.conf").exists()
    assert not any("--audit-" in line or line.startswith(("sshd ", "systemctl "))
                   for line in calls.read_text().splitlines())


def test_slot_group_lookup_error_does_not_attempt_creation(tmp_path, sandbox, monkeypatch):
    bindir, calls = fakebin(tmp_path, slot_group_exists=False)
    monkeypatch.setenv("FAKE_GROUP_LOOKUP_ERROR", "1")
    result = run(GOOD, bindir)
    assert result.returncode != 0 and "could not look up the managed slot group" in result.stderr
    assert "addgroup " not in calls.read_text()
    assert "--audit-" not in calls.read_text()
    assert not (sandbox / "sshd/02-ccfleet-slots.conf").exists()


def test_it_installs_the_agent_the_scripts_and_the_timer(tmp_path, sandbox):
    bindir, calls = fakebin(tmp_path)
    r = run(GOOD, bindir)
    assert r.returncode == 0, r.stderr
    lib = sandbox / "lib"
    for rel in ("ccfleet_agent/__init__.py", "ccfleet_agent/agent.py",
                "ccfleet_agent/machine.py", "ccfleet_agent/local_relay.py",
                "ccfleet_agent/model_usage.py", "ccfleet_agent/relay_metrics.py",
                "ccfleet_agent/network_traffic.py",
                "ccfleet_agent/inference_policy.py",
                "ccfleet_agent/egress.py",
                "ccfleet_agent/compatibility.py", "ccfleet_agent/inference_client.py",
                "slot-add.sh", "slot-remove.sh",
                "slot-entry.sh",
                "systemd/ccfleet-shell.service"):
        installed = lib / rel
        source = REPO / ("node/" + rel if rel.endswith(".sh") or rel.startswith("systemd")
                         else rel)
        expected = source.read_bytes()
        if rel == "systemd/ccfleet-shell.service":
            expected = expected.replace(b"%h/.local/bin/claude --dangerously-skip-permissions",
                                        f"/usr/bin/python3 -I {lib}/ccfleet_agent/egress.py exec -- "
                                        "%h/.local/bin/claude --dangerously-skip-permissions".encode())
        assert installed.read_bytes() == expected, rel
    assert mode(lib / "slot-add.sh") == 0o755 and mode(lib / "slot-remove.sh") == 0o755
    assert mode(lib / "slot-entry.sh") == 0o755
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
    assert mode(sandbox / "etc") == 0o711 and mode(sandbox / "state") == 0o700
    assert mode(sandbox / "etc" / "authorized_keys") == 0o755
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values == {"CCFLEET_URL": "https://fleet.example.com",
                      "CCFLEET_NODE_ID": "shared-1", "CCFLEET_NODE_TOKEN": TOKEN,
                      "CCFLEET_LIB_DIR": str(sandbox / "lib"),
                      "CCFLEET_STATE_FILE": str(sandbox / "state" / "machine.json"),
                      "CCFLEET_AUTHORIZED_KEYS_DIR":
                          str(sandbox / "etc" / "authorized_keys"),
                      "CCFLEET_INFERENCE_POLICY_DIR":
                          str(sandbox / "etc" / "local-relay")}
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
        "ccfleet-egress.service", "ccfleet-machine.service", "ccfleet-machine.timer"], \
        "left a sed backup behind"


def test_it_turns_the_timer_on_and_runs_once(tmp_path):
    bindir, calls = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0
    log = calls.read_text().splitlines()
    systemctl = [line for line in log if line.startswith("systemctl")]
    assert systemctl == ["systemctl reload ssh",
                         "systemctl daemon-reload",
                         "systemctl enable --now ccfleet-machine.timer",
                         "systemctl start ccfleet-machine.service",
                         "systemctl is-active ccfleet-machine.timer"]
    install = next(line for line in log if " install " in line)
    for package in ("python3", "tmux", "sudo", "adduser", "libpam-systemd",
                    "git", "gh", "rsync"):
        assert package in install.split(), f"{package} not installed"


def test_opted_in_proxy_and_native_settings_precede_timer_and_any_new_claim(tmp_path, sandbox):
    bindir, calls = fakebin(tmp_path)
    etc = sandbox / "etc"
    etc.mkdir()
    (etc / "egress.json").write_text('{"version":1,"enabled":true}')
    assert run(GOOD, bindir, canonical_egress_dir=etc).returncode == 0
    log = calls.read_text().splitlines()
    marker_check = next(i for i, line in enumerate(log) if "egress.py" in line
                        and line.endswith(" check"))
    private_check = next(i for i, line in enumerate(log) if line.endswith(" check-upstream"))
    settings = next(i for i, line in enumerate(log) if " install-native-settings " in line)
    start = log.index("systemctl enable --now ccfleet-egress.service")
    active = log.index("systemctl is-active --quiet ccfleet-egress.service")
    timer = log.index("systemctl enable --now ccfleet-machine.timer")
    first = log.index("systemctl start ccfleet-machine.service")
    assert marker_check < private_check < settings < start < active < timer < first
    unit = (sandbox / "units/ccfleet-egress.service").read_text()
    assert f"{sandbox / 'lib'}/ccfleet_agent/egress.py" in unit
    assert f"--policy-file {etc}/egress.json" in unit
    assert f"--upstream-file {etc}/egress-upstream.json" in unit
    assert "--password" not in unit and "--username" not in unit


def test_invalid_opt_in_prevents_starting_machine_or_proxy(tmp_path, sandbox):
    bindir, calls = fakebin(tmp_path, egress_ok=False)
    (sandbox / "etc").mkdir()
    (sandbox / "etc/egress.json").write_text("not-valid-json")
    result = run(GOOD, bindir, canonical_egress_dir=sandbox / "etc")
    assert result.returncode != 0 and "egress policy is invalid" in result.stderr
    assert "enable --now ccfleet-machine.timer" not in calls.read_text()
    assert "enable --now ccfleet-egress.service" not in calls.read_text()


def test_existing_multi_holder_machine_refuses_proxy_before_any_install(tmp_path, sandbox,
                                                                     monkeypatch):
    bindir, calls = fakebin(tmp_path)
    (sandbox / "etc").mkdir()
    (sandbox / "etc/egress.json").write_text('{"version":1,"enabled":true}')
    monkeypatch.setenv("FAKE_SLOT_MEMBERS", "slot01,slot02")
    result = run(GOOD, bindir, canonical_egress_dir=sandbox / "etc")
    assert result.returncode != 0 and "requires one slot per machine" in result.stderr
    assert not (sandbox / "lib").exists()
    assert "apt-get" not in calls.read_text() and "systemctl" not in calls.read_text()


def test_custom_config_path_with_egress_refuses_before_any_install(tmp_path, sandbox):
    etc = sandbox / "etc"
    etc.mkdir()
    (etc / "egress.json").write_text('{"version":1,"enabled":true}')
    bindir, calls = fakebin(tmp_path)
    result = run(GOOD, bindir)
    assert result.returncode != 0 and "canonical /etc/ccfleet" in result.stderr
    assert not (sandbox / "lib").exists()
    assert calls.read_text() == ""


def test_it_restricts_slot_ssh_and_validates_before_reload(tmp_path, sandbox):
    bindir, calls = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0
    dropin = sandbox / "sshd" / "02-ccfleet-slots.conf"
    assert dropin.read_text() == textwrap.dedent(f"""\
        # Slot keys live only in root-controlled files, never holder-writable homes.
        # Each audited key supplies its own forced command: a global ForceCommand
        # would override the service key's inference-only command.
        Match Group ccfleet-slots
            AuthorizedKeysFile {sandbox / "etc" / "authorized_keys"}/%u
            AuthorizedKeysCommand none
            TrustedUserCAKeys none
            AuthenticationMethods publickey
            PubkeyAuthentication yes
            PasswordAuthentication no
            KbdInteractiveAuthentication no
            AllowAgentForwarding no
            AllowTcpForwarding no
            AllowStreamLocalForwarding no
            X11Forwarding no
            PermitTunnel no
            PermitUserRC no
        Match all
        """)
    lines = calls.read_text().splitlines()
    audit = f"python3 -I {sandbox / 'lib' / 'ccfleet_agent' / 'machine.py'} --audit-authorized-keys {sandbox / 'etc' / 'authorized_keys'}"
    assert lines.index(audit) < lines.index(f"sshd -t -f {sandbox / 'sshd_config'}")
    assert lines.index(f"sshd -t -f {sandbox / 'sshd_config'}") < lines.index(
        "systemctl reload ssh")


def test_unforced_key_audit_failure_preserves_existing_sshd_config(tmp_path, sandbox):
    dropin = sandbox / "sshd" / "02-ccfleet-slots.conf"
    original = "Match Group ccfleet-slots\n    ForceCommand /original-entry\n\n"
    dropin.write_text(original)
    bindir, calls = fakebin(tmp_path, keys_audit_ok=False)
    result = run(GOOD, bindir)
    assert result.returncode != 0 and "forced-command audit" in result.stderr
    assert dropin.read_text() == original
    assert not any(line.startswith("sshd ") or line.startswith("systemctl reload")
                   for line in calls.read_text().splitlines())


def test_shell_audit_failure_stops_before_slot_or_sshd_changes(tmp_path, sandbox):
    bindir, calls = fakebin(tmp_path, shell_audit_ok=False)
    result = run(GOOD, bindir)
    assert result.returncode != 0 and "slot login shell is not safe" in result.stderr
    assert not (sandbox / "sshd" / "02-ccfleet-slots.conf").exists()
    assert not any(line.startswith(("usermod ", "sshd ", "systemctl reload"))
                   for line in calls.read_text().splitlines())


def test_existing_validated_slot_uses_dash_before_per_key_sshd_activation(tmp_path, sandbox, monkeypatch):
    monkeypatch.setenv("FAKE_SLOT_MEMBERS", "slot01")
    bindir, calls = fakebin(tmp_path)
    result = run(GOOD, bindir)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert lines.index("usermod -s /bin/dash slot01") < lines.index("systemctl reload ssh")


@pytest.mark.parametrize("uid,groups", [("0", "ccfleet-slots"), ("65534", "ccfleet-slots"),
                                       ("1001", "ccfleet-slots sudo")])
def test_setup_does_not_change_a_non_slot_or_privileged_account(tmp_path, monkeypatch, uid, groups):
    monkeypatch.setenv("FAKE_SLOT_MEMBERS", "slot01")
    monkeypatch.setenv("FAKE_SLOT_UID", uid)
    monkeypatch.setenv("FAKE_SLOT_GROUPS", groups)
    bindir, calls = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode != 0
    assert not any(line.startswith(("usermod ", "sshd ", "systemctl reload"))
                   for line in calls.read_text().splitlines())


def test_it_makes_existing_locked_slots_public_key_eligible(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLOT_MEMBERS", "slot01")
    bindir, calls = fakebin(tmp_path)
    result = run(GOOD, bindir)
    assert result.returncode == 0, result.stderr
    assert "chpasswd slot01:<redacted>" in calls.read_text()
    assert "public-key eligible" in result.stdout


def test_rejected_slot_ssh_config_is_restored_and_not_reloaded(tmp_path, sandbox):
    dropin = sandbox / "sshd" / "02-ccfleet-slots.conf"
    dropin.write_text("old configuration\n\n")
    bindir, calls = fakebin(tmp_path, sshd_ok=False)
    result = run(GOOD, bindir)
    assert result.returncode != 0
    assert "configuration was restored" in result.stderr
    assert dropin.read_text() == "old configuration\n\n"
    assert not any("reload ssh" in line for line in calls.read_text().splitlines())


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
    assert mode(sandbox / "etc") == 0o711 and mode(sandbox / "state") == 0o700
    assert mode(sandbox / "etc" / "authorized_keys") == 0o755


def test_the_last_words_declare_its_one_slot_the_way_the_server_takes_it(tmp_path):
    """One machine is one slot, named after the machine: the command it prints
    is one the server accepts, and there is no capacity to raise."""
    bindir, _ = fakebin(tmp_path)
    out = run(GOOD, bindir).stdout
    assert "ccfleetd slot add shared-1 --machine shared-1 --unix-user slot01" in out
    assert "slot capacity" not in out and "shared-1-01" not in out


@pytest.mark.parametrize("contents", [None, b"enabled\n", b"disabled\n"])
def test_setup_preserves_explicit_inference_policy_without_implicitly_enabling(tmp_path, sandbox,
                                                                            contents):
    policy = sandbox / "etc" / "inference-enabled"
    policy.parent.mkdir(parents=True)
    if contents is not None:
        policy.write_bytes(contents)
        policy.chmod(0o644)
    bindir, _ = fakebin(tmp_path)
    assert run(GOOD, bindir).returncode == 0
    if contents is None:
        assert not policy.exists()
    else:
        assert policy.read_bytes() == contents
        assert mode(policy) == 0o644


def test_missing_new_dependency_does_not_replace_existing_machine_agent(tmp_path, sandbox,
                                                                       monkeypatch):
    source = tmp_path / "incomplete-source"
    (source / "ccfleet_agent").mkdir(parents=True)
    for name in ("__init__.py", "agent.py", "machine.py"):
        (source / "ccfleet_agent" / name).write_bytes((REPO / "ccfleet_agent" / name).read_bytes())
    monkeypatch.setenv("CCFLEET_SOURCE_DIR", str(source))
    installed = sandbox / "lib" / "ccfleet_agent" / "machine.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("previous working machine agent\n")
    bindir, _ = fakebin(tmp_path)
    result = run(GOOD, bindir)
    assert result.returncode != 0
    assert "inference_policy.py" in result.stderr
    assert installed.read_text() == "previous working machine agent\n"
