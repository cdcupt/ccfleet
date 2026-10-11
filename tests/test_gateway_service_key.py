"""The gateway service key is a narrow forced command, separate from CLI keys."""

from __future__ import annotations

import base64
import os
import pwd
import subprocess
from pathlib import Path

import pytest

from ccfleet_agent import machine
from ccfleetd.desired import desired_state


def public_key(byte=1):
    kind = b"ssh-ed25519"
    blob = len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


def test_service_key_has_no_pty_and_only_gateway_entrypoint_while_cli_keys_stay_unchanged(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    directory = tmp_path / "authorized_keys"
    directory.mkdir()
    account = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "", str(home), "/bin/dash"))
    device, service = public_key(1), public_key(2)
    ok, why = machine.install_authorized_key(
        account, {"device_keys": [device], "gateway_keys": [service]}, directory)
    assert ok, why
    device_line, service_line = (directory / "slot01").read_text().splitlines()
    assert device_line == f"{machine.AUTHORIZED_KEY_OPTIONS} {device}"
    assert service_line == f"{machine.GATEWAY_AUTHORIZED_KEY_OPTIONS} {service}"
    assert " --gateway-only\"" in service_line and "no-pty" in service_line
    for restriction in ("no-port-forwarding", "no-agent-forwarding", "no-X11-forwarding", "no-user-rc"):
        assert restriction in service_line
    assert (directory / "slot01").stat().st_uid == os.getuid()
    assert (directory / "slot01").stat().st_mode & 0o022 == 0
    # Rolling back to an ordinary desired state removes the service grant and
    # retains the exact existing CLI key line and interactive privileges.
    ok, why = machine.install_authorized_key(account, [device], directory)
    assert ok, why
    assert (directory / "slot01").read_text() == device_line + "\n"


@pytest.mark.parametrize("value", [
    {"device_keys": [], "gateway_keys": [public_key(), public_key(2)]},
    {"device_keys": [], "gateway_keys": ['command="sh" ' + public_key()]},
    {"device_keys": [public_key()], "gateway_keys": [public_key()]},
    {"device_keys": [], "gateway_keys": [public_key() + "\n" + public_key(2)]},
    {"device_keys": [], "gateway_keys": [public_key()], "shell": True},
])
def test_malformed_or_mixed_privilege_input_cannot_install_service_key(tmp_path, value):
    account = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "", str(tmp_path), "/bin/dash"))
    directory = tmp_path / "authorized_keys"
    directory.mkdir()
    ok, _ = machine.install_authorized_key(account, value, directory)
    assert not ok and not (directory / "slot01").exists()


def test_gateway_desired_state_separates_keys_and_revokes_on_signin_release_and_missing_selection():
    node = {"id": "m1", "pinned_version": "", "rc_expected": False}
    slot = {"id": "s1", "unix_user": "slot01", "state": "active", "held_by": "a1", "kind": "machine"}
    kwargs = {"slot_cli_keys": {"s1": [public_key()]}, "slot_gateway_keys": {"s1": [public_key(2)]}}
    [block] = desired_state(node, slots=[slot], **kwargs)["slots"]
    assert block["ssh_public_keys"] == [public_key()]
    assert block["ssh_gateway_public_keys"] == [public_key(2)]
    wanted = machine.wanted_slots({"slots": [block]})[0]
    assert wanted["ssh_gateway_public_keys"] == [public_key(2)]
    pending = {"state": "requested", "requested_at": 10, "kind": "switch"}
    [during] = desired_state(node, slots=[slot], slot_logins={"s1": pending}, **kwargs)["slots"]
    assert during["ssh_gateway_public_keys"] == []
    [released] = desired_state(node, slots=[{**slot, "state": "releasing"}], **kwargs)["slots"]
    assert released["ssh_gateway_public_keys"] == []
    [absent] = desired_state(node, slots=[slot], slot_gateway_keys={})["slots"]
    assert absent["ssh_gateway_public_keys"] == []


@pytest.mark.parametrize("raw_service", ["invalid", [public_key() + "\n"], [public_key()], [public_key(2), public_key(3)]])
def test_malformed_service_intent_revokes_service_and_keeps_valid_cli_keys(raw_service):
    [wanted] = machine.wanted_slots({"slots": [{"unix_user": "slot01", "state": "active",
                                                "inference_allowed": True,
                                                "ssh_public_keys": [public_key()],
                                                "ssh_gateway_public_keys": raw_service}]})
    assert wanted["ssh_public_keys"] == [public_key()]
    assert wanted["ssh_gateway_public_keys"] == []


@pytest.mark.parametrize("original", ["", "sh", "ccfleet-inference-v1", "ccfleet-project-v1",
                                      "ccfleet-live-v1 " + "a" * 32, "ccfleet-session open ccfleet manual",
                                      "ccfleet-gateway-v1\nsh", "ccfleet-gateway-v1 --shell"])
def test_gateway_only_forced_entry_rejects_every_other_protocol(original):
    script = Path(__file__).parents[1] / "node" / "slot-entry.sh"
    result = subprocess.run(["bash", str(script), "--gateway-only"],
                            env={**os.environ, "SSH_ORIGINAL_COMMAND": original},
                            input=b"", capture_output=True, timeout=5)
    assert result.returncode == 2
    assert b"unsupported CC Fleet gateway request" in result.stderr


def test_gateway_command_is_dispatched_only_under_gateway_forced_option(tmp_path):
    script = tmp_path / "slot-entry.sh"
    script.write_text((Path(__file__).parents[1] / "node" / "slot-entry.sh").read_text())
    relay = tmp_path / "ccfleet_agent" / "local_relay.py"
    relay.parent.mkdir()
    relay.write_text("import sys\nprint('RELAY:' + ','.join(sys.argv[1:]))\n")
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": "ccfleet-gateway-v1"}
    allowed = subprocess.run(["bash", str(script), "--gateway-only"], env=env,
                             input=b"", capture_output=True, timeout=5)
    assert allowed.returncode == 0 and allowed.stdout == b"RELAY:--gateway-protocol-v1\n"
    device = subprocess.run(["bash", str(script)], env=env, input=b"", capture_output=True, timeout=5)
    assert device.returncode == 2 and b"RELAY:" not in device.stdout


def test_unsafe_login_shell_revokes_service_key_and_preserves_device_pairing(tmp_path):
    directory = tmp_path / "authorized_keys"
    directory.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    safe = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "", str(home), "/bin/dash"))
    unsafe = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "", str(home), "/bin/bash"))
    selection = {"device_keys": [public_key()], "gateway_keys": [public_key(2)]}
    assert machine.install_authorized_key(safe, selection, directory)[0]
    ok, why = machine.install_authorized_key(unsafe, selection, directory)
    assert not ok and "login shell must be /bin/dash" in why
    assert (directory / "slot01").read_text() == f"{machine.AUTHORIZED_KEY_OPTIONS} {public_key()}\n"


@pytest.fixture
def audit_directory(tmp_path, monkeypatch):
    directory = tmp_path / "authorized_keys"
    directory.mkdir(mode=0o755)
    monkeypatch.setattr(machine, "AUTHORIZED_KEYS_ROOT_UID", os.getuid())
    # Isolate line/file validation from the test host's system ancestry. The
    # real root/Linux sshd fixture exercises the production root-only path.
    monkeypatch.setattr(machine, "_open_authorized_keys_directory",
                        lambda path: machine._open_dir(path))
    return directory


def test_read_only_key_audit_accepts_only_exact_generated_device_and_service_lines(audit_directory):
    path = audit_directory / "slot01"
    content = (f"# operator note\n{machine.AUTHORIZED_KEY_OPTIONS} {public_key()}\n"
               f"{machine.GATEWAY_AUTHORIZED_KEY_OPTIONS} {public_key(2)}\n")
    path.write_text(content)
    path.chmod(0o644)
    ok, why = machine.audit_authorized_key_files(audit_directory)
    assert ok, why
    assert path.read_text() == content


@pytest.mark.parametrize("content", [public_key(), 'command="/bin/bash" ' + public_key(),
                                      machine.AUTHORIZED_KEY_OPTIONS + ',pty ' + public_key(),
                                      machine.AUTHORIZED_KEY_OPTIONS + ' ' + public_key() + ' comment',
                                      machine.AUTHORIZED_KEY_OPTIONS + ' not-a-key'])
def test_unmanaged_key_audit_refuses_without_repairing_or_deleting(audit_directory, content):
    path = audit_directory / "slot01"
    path.write_text(content + "\n")
    path.chmod(0o644)
    ok, why = machine.audit_authorized_key_files(audit_directory)
    assert not ok and "forced command" in why
    assert path.read_text() == content + "\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "writable", "fifo"])
def test_key_audit_refuses_unsafe_files(audit_directory, tmp_path, kind):
    path = audit_directory / "slot01"
    outside = tmp_path / "outside"
    outside.write_text(f"{machine.AUTHORIZED_KEY_OPTIONS} {public_key()}\n")
    outside.chmod(0o644)
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text(outside.read_text())
        path.chmod(0o666)
    assert not machine.audit_authorized_key_files(audit_directory)[0]
    assert outside.read_text() == f"{machine.AUTHORIZED_KEY_OPTIONS} {public_key()}\n"


def test_key_audit_refuses_unsafe_directory_path(tmp_path):
    path = tmp_path / "unsafe"
    path.mkdir(mode=0o777)
    assert not machine.audit_authorized_key_files(path)[0]
    assert not machine.audit_authorized_key_files(Path("relative/keys"))[0]


def test_read_only_audit_mode_requires_root_and_does_not_load_agent_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(machine.os, "geteuid", lambda: 0)
    monkeypatch.setattr(machine, "audit_authorized_key_files", lambda path: (True, "safe"))
    monkeypatch.setattr(machine.core, "load_env_file", lambda path: pytest.fail("agent env loaded"))
    assert machine.main(["--audit-authorized-keys", str(tmp_path)]) == 0
    monkeypatch.setattr(machine.os, "geteuid", lambda: 1000)
    assert machine.main(["--audit-authorized-keys", str(tmp_path)]) == 2
