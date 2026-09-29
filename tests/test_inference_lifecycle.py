"""Managed inference claim/release lifecycle, using only synthetic policy files."""

from __future__ import annotations

import os
import pwd
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleet_agent import inference_policy as policy
from ccfleet_agent import machine
from ccfleetd.desired import desired_state
from tests.test_machine import NOW, Fake


@pytest.fixture
def gates(tmp_path, monkeypatch):
    # The production trust boundary is uid0. These files are owned by the
    # unprivileged test runner; no sudo or real /etc accesses are needed.
    monkeypatch.setattr(policy, "ROOT_UID", os.getuid())
    base = tmp_path.resolve() / "etc"
    base.mkdir(mode=0o755)
    directory = base / "local-relay"
    directory.mkdir(mode=0o755)
    return directory


def opt_in(gates, content=b"enabled\n"):
    path = gates.parent / "inference-enabled"
    path.write_bytes(content)
    path.chmod(0o644)
    return path


def marker(gates, name="slot01", content=policy.MANAGED_MARKER):
    path = gates / name
    path.write_bytes(content)
    path.chmod(0o644)
    return path


def assignment(**changes):
    return {"id": "s1", "unix_user": "slot01", "kind": "machine", "state": "active",
            "held_by": "synthetic-holder", **changes}


def desired(row=None):
    return desired_state({"pinned_version": "", "rc_expected": False},
                         slots=[assignment() if row is None else row])


def config(gates):
    return machine.MachineConfig.from_env({
        "CCFLEET_URL": "https://fleet.invalid", "CCFLEET_NODE_ID": "synthetic-machine",
        "CCFLEET_NODE_TOKEN": "synthetic", "CCFLEET_STATE_FILE": str(gates.parent / "state"),
        "CCFLEET_INFERENCE_POLICY_DIR": str(gates),
    })


def converge(gates, block=None, fake=None):
    block = desired() if block is None else block
    machine.converge_inference_access(block, machine.wanted_slots(block), config(gates),
                                     (fake or Fake(users=["slot01"])).system())


@pytest.mark.parametrize("state", ["free", "claiming", "claimed", "active", "releasing"])
@pytest.mark.parametrize("kind", ["machine", "owner", None, "unknown"])
@pytest.mark.parametrize("holder", ["holder", None, "", " ", False, 42])
def test_server_permission_requires_real_managed_assignment(state, kind, holder):
    block = desired(assignment(state=state, kind=kind, held_by=holder))["slots"][0]
    expected = state in ("claimed", "active") and kind == "machine" and holder == "holder"
    assert (block.get("inference_allowed") is True) is expected
    assert "held_by" not in block


@pytest.mark.parametrize("value", [False, None, 1, "true", "enabled", {}, []])
def test_client_accepts_no_truthy_permission_substitutes(value):
    block = {"slots": [{"unix_user": "slot01", "state": "active", "inference_allowed": value}]}
    assert "inference_allowed" not in machine.wanted_slots(block)[0]


@pytest.mark.parametrize("name", ["slot01\n", "../slot01", "slot01/other", "root", "Slot01"])
def test_unsafe_declarations_never_grant(gates, name):
    opt_in(gates)
    converge(gates, {"slots": [{"unix_user": name, "state": "active",
                               "inference_allowed": True}]})
    assert list(gates.iterdir()) == []


def test_claim_release_reclaim_is_idempotent_and_uses_actual_policy_reader(gates):
    opt_in(gates)
    fake = Fake(users=["slot01"])
    for row in [assignment(state="claiming", claimed_at=NOW), assignment(state="claimed"),
                assignment(state="active"), assignment(state="releasing"),
                assignment(state="free", held_by=None),
                assignment(state="claiming", claimed_at=NOW + 1),
                assignment(state="claimed", held_by="next-holder")]:
        block = desired(row)
        converge(gates, block, fake)
        converge(gates, block, fake)
        if row["state"] in ("claimed", "active"):
            assert (gates / "slot01").read_bytes() == policy.MANAGED_MARKER
            policy.require_enabled(gates, "slot01")
        else:
            assert not (gates / "slot01").exists()
            with pytest.raises(policy.PolicyError):
                policy.require_enabled(gates, "slot01")


@pytest.mark.parametrize("state", ["free", "claiming", "releasing"])
def test_terminal_states_revoke_even_with_stale_true_permission(gates, state):
    opt_in(gates)
    marker(gates)
    block = {"slots": [{"unix_user": "slot01", "state": state, "claimed_at": NOW,
                         "inference_allowed": True}]}
    converge(gates, block)
    assert not (gates / "slot01").exists()


@pytest.mark.parametrize("block", [{}, {"slots": []}, {"slots": "slot01"},
                                    {"slots": [None]}, {"slots": [{"state": "active"}]}])
def test_missing_or_malformed_assignment_revokes_orphan_gates(gates, block):
    opt_in(gates)
    marker(gates)
    marker(gates, "orphan")
    converge(gates, block)
    assert list(gates.iterdir()) == []


def test_missing_holder_or_account_revokes(gates):
    opt_in(gates)
    for block, fake in [(desired(assignment(held_by=None)), Fake(users=["slot01"])),
                        (desired(), Fake())]:
        marker(gates)
        converge(gates, block, fake)
        assert not (gates / "slot01").exists()


@pytest.mark.parametrize("other", [None, {"unix_user": "bad/slot"},
                                    {"unix_user": "slot02", "state": "free"},
                                    {"unix_user": "slot01", "state": "active"}])
def test_raw_multiple_declarations_fail_closed_even_if_filtered(other, gates):
    opt_in(gates)
    marker(gates)
    block = desired()
    block["slots"].append(other)
    converge(gates, block)
    assert list(gates.iterdir()) == []


@pytest.mark.parametrize("group", ["sudo", "admin", "wheel", "root", "docker", "lxd",
                                    "libvirt", "kvm", "adm", "disk", "shadow", "staff"])
def test_privileged_account_is_never_enabled(gates, group):
    opt_in(gates)
    marker(gates)
    converge(gates, fake=Fake(users=["slot01"], groups={"slot01": {"ccfleet-slots", group}}))
    assert not (gates / "slot01").exists()


@pytest.mark.parametrize("uid,gid,groups", [(0, 1000, {"ccfleet-slots"}),
                                           (65534, 1000, {"ccfleet-slots"}),
                                           (1001, 0, {"ccfleet-slots"}), (1001, 1001, set())])
def test_non_slot_account_is_never_enabled(gates, uid, gid, groups):
    opt_in(gates)
    fake = Fake(users=["slot01"], groups={"slot01": groups})
    fake.users["slot01"] = pwd.struct_passwd(("slot01", "x", uid, gid, "", "/do-not-open", "/bin/sh"))
    converge(gates, fake=fake)
    assert list(gates.iterdir()) == []


def test_removing_opt_in_immediately_denies_managed_gate_before_next_heartbeat(gates):
    enabled = opt_in(gates)
    converge(gates)
    policy.require_enabled(gates, "slot01")
    enabled.unlink()
    with pytest.raises(policy.PolicyError):
        policy.require_enabled(gates, "slot01")
    converge(gates)
    assert list(gates.iterdir()) == []


def test_legacy_manual_empty_gate_stays_compatible_until_machine_opts_in(gates):
    marker(gates, content=b"")
    policy.require_enabled(gates, "slot01")
    converge(gates, {"slots": []})
    assert (gates / "slot01").read_bytes() == b""
    opt_in(gates)
    converge(gates)
    assert (gates / "slot01").read_bytes() == policy.MANAGED_MARKER


@pytest.mark.parametrize("content", [b"disabled\n", b"enabled", b"true\n", b"", b"x" * 129])
def test_invalid_or_disabled_opt_in_does_not_grant(gates, content):
    opt_in(gates, content)
    marker(gates)
    with pytest.raises(policy.PolicyError):
        policy.require_enabled(gates, "slot01")
    converge(gates)
    assert list(gates.iterdir()) == []


def test_explicit_disable_revokes_legacy_gates_too(gates):
    marker(gates, content=b"")
    opt_in(gates, b"disabled\n")
    with pytest.raises(policy.PolicyError):
        policy.require_enabled(gates, "slot01")
    converge(gates)
    assert list(gates.iterdir()) == []


@pytest.mark.parametrize("target", ["base", "directory", "marker", "opt-in"])
def test_writable_policy_components_are_refused(gates, target):
    enabled = opt_in(gates)
    gate = marker(gates)
    path = {"base": gates.parent, "directory": gates, "marker": gate, "opt-in": enabled}[target]
    path.chmod(0o777 if path.is_dir() else 0o666)
    with pytest.raises(policy.PolicyError):
        policy.require_enabled(gates, "slot01")


@pytest.mark.parametrize("target", ["marker", "opt-in"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_special_policy_files_cannot_authorize(gates, target, kind):
    enabled = opt_in(gates)
    gate = marker(gates)
    path = gate if target == "marker" else enabled
    original = path.read_bytes()
    path.unlink()
    safe = gates.parent / "untouched"
    safe.write_bytes(original)
    safe.chmod(0o644)
    if kind == "symlink":
        path.symlink_to(safe)
    elif kind == "hardlink":
        os.link(safe, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    with pytest.raises(policy.PolicyError):
        policy.require_enabled(gates, "slot01")
    assert safe.read_bytes() == original


def test_reconcile_unlinks_stale_symlink_without_touching_target(gates):
    opt_in(gates)
    target = gates.parent / "preserve"
    target.write_text("not a gate")
    (gates / "orphan").symlink_to(target)
    converge(gates)
    assert target.read_text() == "not a gate"
    assert not (gates / "orphan").is_symlink()
    policy.require_enabled(gates, "slot01")


def test_unremovable_stale_entry_prevents_new_grant(gates):
    opt_in(gates)
    (gates / "orphan").mkdir()
    with pytest.raises(policy.PolicyError):
        policy.reconcile(gates, {"slot01"})
    assert not (gates / "slot01").exists()


def test_symlinked_directory_or_ancestor_is_never_followed(gates, tmp_path):
    opt_in(gates)
    marker(gates)
    alias = tmp_path / "alias"
    alias.symlink_to(gates.parent, target_is_directory=True)
    with pytest.raises(policy.PolicyError):
        policy.require_enabled(alias / "local-relay", "slot01")
    with pytest.raises(policy.PolicyError):
        policy.reconcile(alias / "local-relay", {"slot01"})
    assert (gates / "slot01").read_bytes() == policy.MANAGED_MARKER


def test_missing_policy_directory_is_created_only_after_opt_in(gates):
    gates.rmdir()
    converge(gates)
    assert not gates.exists()
    opt_in(gates)
    converge(gates)
    policy.require_enabled(gates, "slot01")
    assert gates.stat().st_mode & 0o777 == 0o755


def test_revocation_happens_before_a_wipe_retry_is_backed_off(gates, monkeypatch):
    opt_in(gates)
    marker(gates)
    fake = Fake(users=["slot01"], desired=desired(assignment(state="releasing")))
    monkeypatch.setattr(machine.core, "egress_ip", lambda *a, **k: {})
    state = {"slots": ["slot01"], "slot_states": {"slot01": "releasing"},
             "wipe_failed": {"slot01": {"ts": NOW, "error": "busy"}}}
    cfg = replace(config(gates), state_path=gates.parent / "machine.json")
    status, _, _ = machine.run_cycle(cfg, state, fake.system())
    assert status == 200 and fake.scripts == []
    assert not (gates / "slot01").exists()


def test_no_holder_home_is_opened_to_grant_access(gates, monkeypatch):
    opt_in(gates)
    real_open = os.open

    def guarded(path, *args, **kwargs):
        assert not str(path).startswith("/home/")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(policy.os, "open", guarded)
    converge(gates)
    policy.require_enabled(gates, "slot01")


@pytest.mark.parametrize("target", ["base", "directory", "marker", "opt-in"])
def test_unowned_policy_components_cannot_authorize(gates, monkeypatch, target):
    enabled = opt_in(gates)
    gate = marker(gates)
    path = {"base": gates.parent, "directory": gates, "marker": gate, "opt-in": enabled}[target]
    bad_inode = path.stat().st_ino
    original = os.fstat

    def not_owned(fd):
        info = original(fd)
        if info.st_ino != bad_inode:
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_uid=policy.ROOT_UID + 1,
                               st_nlink=info.st_nlink, st_size=info.st_size)

    monkeypatch.setattr(policy.os, "fstat", not_owned)
    with pytest.raises(policy.PolicyError):
        policy.require_enabled(gates, "slot01")


def test_partial_policy_writes_complete_and_repeated_reconcile_does_not_replace(gates, monkeypatch):
    opt_in(gates)
    original = os.write
    monkeypatch.setattr(policy.os, "write", lambda fd, data: original(fd, data[:3]))
    converge(gates)
    path = gates / "slot01"
    assert path.read_bytes() == policy.MANAGED_MARKER
    first_inode = path.stat().st_ino
    converge(gates)
    assert path.stat().st_ino == first_inode
    assert list(gates.iterdir()) == [path]


def test_actual_relay_gate_denies_released_slot_and_accepts_reclaim(gates, monkeypatch):
    from ccfleet_agent import local_relay

    monkeypatch.setattr(local_relay.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="slot01"))
    opt_in(gates)
    converge(gates)
    local_relay.require_enabled(gates)
    converge(gates, desired(assignment(state="releasing")))
    with pytest.raises(local_relay.RelayError) as denied:
        local_relay.require_enabled(gates)
    assert denied.value.status == 403
    converge(gates, desired(assignment(state="claimed", held_by="next-holder")))
    local_relay.require_enabled(gates)


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="managed nodes use Linux directory handles")
def test_slot_can_read_policy_below_a_traverse_only_parent(gates):
    opt_in(gates)
    marker(gates)
    # On production /etc/ccfleet is 0711 and the slot is not its owner. Remove
    # all read bits here to exercise the same permission boundary without root.
    gates.parent.chmod(0o111)
    try:
        policy.require_enabled(gates, "slot01")
    finally:
        gates.parent.chmod(0o755)
