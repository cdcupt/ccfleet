from __future__ import annotations

import os
import pwd

from ccfleet_agent import machine
from ccfleetd.desired import desired_state
from ccfleetd.store import Store

NOW = 1_700_000_000.0


def test_desired_state_revokes_a_legacy_holder_key(store):
    store.add_node("m1", "operator", now=NOW)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with store._write_txn() as conn:
        conn.execute(
            "UPDATE slots SET state = 'active', ssh_public_key = 'legacy-key' WHERE id = 's1'"
        )
    block = desired_state(store.get_node("m1"), slots=[store.get_slot("s1")])["slots"][0]
    assert block["ssh_public_key"] == ""


def test_startup_erases_a_legacy_holder_key(tmp_path):
    path = tmp_path / "ccfleet.db"
    first = Store(str(path))
    first.add_node("m1", "operator", now=NOW)
    first.add_slot("s1", "m1", "slot01", now=NOW)
    with first._write_txn() as conn:
        conn.execute("UPDATE slots SET ssh_public_key = 'legacy-key' WHERE id = 's1'")
    first.close()

    reopened = Store(str(path))
    try:
        assert reopened.get_slot("s1")["ssh_public_key"] == ""
    finally:
        reopened.close()


def test_machine_removes_a_legacy_authorized_keys_file(tmp_path):
    home = tmp_path / "slot01"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True, mode=0o700)
    keys = ssh / "authorized_keys"
    keys.write_text("a legacy key\n")
    acct = pwd.struct_passwd(("slot01", "x", os.getuid(), os.getgid(), "", str(home),
                              "/bin/bash"))

    ok, why = machine.install_authorized_key(acct, "")

    assert ok, why
    assert not keys.exists()
