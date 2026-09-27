from __future__ import annotations

import base64
import os
import pwd
import struct

import pytest

from ccfleet_agent import machine
from ccfleetd import slots, sshkeys
from ccfleetd.desired import desired_state
from ccfleetd.store import BadSSHKey, NotYours

NOW = 1_700_000_000.0


def public_key(comment="person@example.com"):
    kind = b"ssh-ed25519"
    blob = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + b"k" * 32
    encoded = base64.b64encode(blob).decode("ascii")
    return f"ssh-ed25519 {encoded} {comment}".rstrip()


def held_slot(store, account_id="a1"):
    store.add_node("m1", "operator", now=NOW)
    store.add_account(account_id, f"sub-{account_id}", f"{account_id}@example.com",
                      slot_quota=1, now=NOW)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW)
    claimed = store.claim_slot(account_id, now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": claimed["claimed_at"]}], now=NOW + 1)
    return store.get_slot("s1")


def test_public_key_is_canonical_and_its_comment_is_not_stored():
    normalized = sshkeys.normalize(public_key("private-address@example.com"))
    assert normalized.split() == public_key("").split()
    assert "example.com" not in normalized
    assert sshkeys.fingerprint(normalized).startswith("SHA256:")


@pytest.mark.parametrize("bad", [
    "", "not-a-key", "command=evil ssh-ed25519 AAAA", "ssh-ed25519 !!!",
    "ssh-ed25519 AAAA\nssh-rsa AAAA", "ssh-dss AAAA",
])
def test_bad_public_keys_are_refused(bad):
    with pytest.raises(sshkeys.PublicKeyError):
        sshkeys.normalize(bad)


def test_holder_key_reaches_only_its_slot_and_release_revokes_it(store):
    held_slot(store)
    normalized = store.set_slot_ssh_key("s1", public_key(), held_by="a1")
    row = store.get_slot("s1")
    assert row["ssh_public_key"] == normalized
    block = desired_state(store.get_node("m1"), slots=[row])["slots"][0]
    assert block["ssh_public_key"] == normalized

    store.begin_release("s1", held_by="a1")
    assert store.get_slot("s1")["ssh_public_key"] == ""
    assert "ssh_public_key" not in desired_state(
        store.get_node("m1"), slots=[store.get_slot("s1")])["slots"][0]


def test_another_holder_cannot_probe_or_change_a_slots_key(store):
    held_slot(store)
    store.add_account("a2", "sub-a2", "a2@example.com", slot_quota=1, now=NOW)
    with pytest.raises(NotYours):
        store.set_slot_ssh_key("s1", "not a key", held_by="a2")
    assert store.get_slot("s1")["ssh_public_key"] == ""


def test_a_bad_key_changes_nothing(store):
    held_slot(store)
    with pytest.raises(BadSSHKey):
        store.set_slot_ssh_key("s1", "ssh-ed25519 !!!", held_by="a1")
    assert store.get_slot("s1")["ssh_public_key"] == ""


def test_machine_installs_one_restricted_key_and_removes_it(tmp_path):
    home = tmp_path / "slot01"
    home.mkdir(mode=0o700)
    acct = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "", str(home),
                              "/bin/bash"))
    normalized = sshkeys.normalize(public_key())
    ok, why = machine.install_authorized_key(acct, normalized)
    assert ok, why
    keys = home / ".ssh" / "authorized_keys"
    assert keys.read_text() == f"{machine.AUTHORIZED_KEY_OPTIONS} {normalized}\n"
    assert keys.stat().st_mode & 0o777 == 0o600
    assert machine.authorized_key_fingerprint(acct) == sshkeys.fingerprint(normalized)

    ok, why = machine.install_authorized_key(acct, "")
    assert ok, why
    assert not keys.exists()


def test_machine_refuses_to_follow_a_holder_controlled_ssh_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "slot01"
    home.mkdir()
    (home / ".ssh").symlink_to(outside, target_is_directory=True)
    acct = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "", str(home),
                              "/bin/bash"))
    ok, why = machine.install_authorized_key(acct, sshkeys.normalize(public_key()))
    assert not ok and "could not update" in why
    assert list(outside.iterdir()) == []


def test_machine_converges_the_desired_key_and_reports_its_fingerprint():
    normalized = sshkeys.normalize(public_key())
    seen = {}
    acct = pwd.struct_passwd(("slot01", "x", 1001, 1001, "", "/home/slot01", "/bin/bash"))

    def install(account, key):
        seen[account.pw_name] = key
        return True, ""

    system = machine.System(
        lookup=lambda name: acct if name == "slot01" else None,
        groups_of=lambda account: {machine.SLOT_GROUP},
        install_ssh_key=install,
        ssh_key_fingerprint=lambda account: sshkeys.fingerprint(seen[account.pw_name]),
    )
    machine.converge_ssh_access(
        [{"unix_user": "slot01", "state": slots.ACTIVE, "ssh_public_key": normalized}], system)
    assert seen == {"slot01": normalized}
    assert system.ssh_key_fingerprint(acct) == sshkeys.fingerprint(normalized)

    releasing = machine.wanted_slots(
        {"slots": [{"unix_user": "slot01", "state": slots.RELEASING}]})
    machine.converge_ssh_access(releasing, system)
    assert seen == {"slot01": ""}, "a failed wipe must still revoke SSH"
