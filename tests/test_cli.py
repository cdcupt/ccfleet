import re

import pytest

from ccfleetd import cli


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("CCFLEET_ADMIN_TOKEN", raising=False)
    return str(tmp_path / "fleet.db")


def test_node_lifecycle(db, capsys):
    assert cli.main(["--db", db, "node", "add", "node-a", "--owner", "erik", "--region", "us"]) == 0
    out = capsys.readouterr().out
    token = re.search(r"CCFLEET_NODE_TOKEN=([0-9a-f]{64})", out).group(1)
    assert "CCFLEET_NODE_ID=node-a" in out and len(token) == 64
    assert cli.main(["--db", db, "node", "pin", "node-a", "2.1.92"]) == 0
    assert cli.main(["--db", db, "node", "list"]) == 0
    listing = capsys.readouterr().out
    assert "node-a" in listing and "2.1.92" in listing and "never" in listing
    assert cli.main(["--db", db, "node", "rotate-token", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "disable", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "enable", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "remove", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "remove", "node-a"]) == 2


def test_check_and_serve_guard(db, capsys):
    assert cli.main(["--db", db, "node", "add", "node-a", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "check"]) == 0
    assert cli.main(["--db", db, "serve"]) == 2  # no admin token configured
    assert "CCFLEET_ADMIN_TOKEN" in capsys.readouterr().err


def test_alert_test_uses_log_notifier(db):
    assert cli.main(["--db", db, "alert-test"]) == 0


def test_bad_config_exits_2(db, monkeypatch, capsys):
    monkeypatch.setenv("CCFLEET_BIND", "nonsense")
    assert cli.main(["--db", db, "node", "list"]) == 2
    assert "CCFLEET_BIND" in capsys.readouterr().err


def test_rc_expected_toggle_and_listing(db, capsys):
    assert cli.main(["--db", db, "node", "add", "node-a", "--owner", "erik"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "node", "list"]) == 0
    assert " off " in capsys.readouterr().out  # default: no Remote Control alerting

    assert cli.main(["--db", db, "node", "rc-expected", "node-a", "on"]) == 0
    assert "will alert" in capsys.readouterr().out
    assert cli.main(["--db", db, "node", "list"]) == 0
    assert " on " in capsys.readouterr().out

    assert cli.main(["--db", db, "node", "rc-expected", "node-a", "off"]) == 0
    assert "will not alert" in capsys.readouterr().out
    assert cli.main(["--db", db, "node", "rc-expected", "ghost", "on"]) == 2


def test_rc_expected_rejects_bad_state(db):
    with pytest.raises(SystemExit):
        cli.main(["--db", db, "node", "rc-expected", "node-a", "maybe"])


def test_console_account_lifecycle(db, capsys):
    assert cli.main(["--db", db, "user", "add", "alice", "--owner", "alice"]) == 0
    out = capsys.readouterr().out
    password = re.search(r"\n  (\S{20})\n", out).group(1)
    assert "shown once" in out and "cannot be recovered" in out

    assert cli.main(["--db", db, "user", "list"]) == 0
    listing = capsys.readouterr().out
    assert "alice" in listing and "owner alice" in listing
    assert password not in listing, "a password must never appear in a listing"
    assert "pbkdf2" not in listing, "nor a hash"

    assert cli.main(["--db", db, "user", "passwd", "alice"]) == 0
    second = re.search(r"\n  (\S{20})\n", capsys.readouterr().out).group(1)
    assert second != password, "a reset must actually change it"

    assert cli.main(["--db", db, "user", "remove", "alice"]) == 0
    assert "removed alice" in capsys.readouterr().out


def test_the_password_is_verifiable_and_stored_only_as_a_hash(db, capsys):
    from ccfleetd.passwords import verify_password
    from ccfleetd.store import Store
    cli.main(["--db", db, "user", "add", "bob", "--password", "chosen-by-hand"])
    capsys.readouterr()
    store = Store(db)
    record = store.get_user("bob")
    store.close()
    assert verify_password("chosen-by-hand", record["password_hash"])
    assert "chosen-by-hand" not in record["password_hash"]


def test_an_admin_account_maps_to_no_single_owner(db, capsys):
    assert cli.main(["--db", db, "user", "add", "ops", "--role", "admin"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "user", "list"]) == 0
    assert "the whole fleet" in capsys.readouterr().out


def test_operating_on_a_missing_account_fails_rather_than_pretending(db, capsys):
    assert cli.main(["--db", db, "user", "passwd", "ghost"]) != 0
    assert cli.main(["--db", db, "user", "remove", "ghost"]) != 0


def test_an_empty_account_list_says_the_token_still_works(db, capsys):
    assert cli.main(["--db", db, "user", "list"]) == 0
    assert "admin token still works" in capsys.readouterr().out


# -- slots and accounts -------------------------------------------------------

def _account(db_path, email, *, quota=0):
    """Register somebody. Sign-up is Google's job; this is the test's stand-in."""
    from ccfleetd.store import Store
    st = Store(db_path)
    try:
        return st.add_account(email.split("@")[0], f"sub-{email}", email,
                              slot_quota=quota, now=1_700_000_000.0)
    finally:
        st.close()


def _confirm_empty(db, machine, *users):
    """What the machine agent does on its first check-in: report each slot's
    Linux user absent, which is what makes a free slot claimable."""
    from ccfleetd.store import Store
    st = Store(db)
    try:
        st.apply_slot_report(machine, [{"unix_user": u, "present": False} for u in users],
                             now=1.0)
    finally:
        st.close()


def test_slot_lifecycle_from_the_command_line(db, capsys):
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "slot", "capacity", "m1", "2"]) == 0
    assert cli.main(["--db", db, "slot", "add", "s1", "--machine", "m1",
                     "--unix-user", "slot01"]) == 0
    out = capsys.readouterr().out
    assert "free" in out
    # Free means the Linux user does not exist, so the operator is told the
    # machine makes it at claim time — and warned off making it by hand, which
    # is what this command used to ask for and which now reads as occupied.
    assert "slot-add.sh" in out
    assert "do not create it by hand" in out

    assert cli.main(["--db", db, "slot", "list"]) == 0
    listed = capsys.readouterr().out
    assert "slot01" in listed
    assert "not yet seen" in listed, "the list hid that no machine has vouched for it"


def test_declaring_more_slots_than_the_machine_holds_is_refused(db, capsys):
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "slot", "capacity", "m1", "1"]) == 0
    assert cli.main(["--db", db, "slot", "add", "s1", "--machine", "m1",
                     "--unix-user", "slot01"]) == 0
    assert cli.main(["--db", db, "slot", "add", "s2", "--machine", "m1",
                     "--unix-user", "slot02"]) != 0
    assert "capacity" in capsys.readouterr().err


def test_capacity_cannot_be_dropped_below_what_is_already_declared(db, capsys):
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "slot", "capacity", "m1", "2"]) == 0
    for n in (1, 2):
        assert cli.main(["--db", db, "slot", "add", f"s{n}", "--machine", "m1",
                         "--unix-user", f"slot0{n}"]) == 0
    assert cli.main(["--db", db, "slot", "capacity", "m1", "1"]) != 0
    assert "already has 2 slots" in capsys.readouterr().err


def test_releasing_from_the_command_line_only_starts_the_wipe(db, capsys):
    """The slot must not read as free until the machine says the user is gone."""
    from ccfleetd import slots
    from ccfleetd.store import Store
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "slot", "add", "s1", "--machine", "m1",
                     "--unix-user", "slot01"]) == 0
    _confirm_empty(db, "m1", "slot01")
    _account(db, "erik@example.com", quota=1)
    st = Store(db)
    try:
        st.claim_slot("erik", now=1_700_000_000.0)
    finally:
        st.close()

    assert cli.main(["--db", db, "slot", "release", "s1"]) == 0
    out = capsys.readouterr().out
    assert "slot-remove.sh" in out
    assert "reports its Linux user gone" in out, "it must not read as done"
    st = Store(db)
    try:
        row = st.get_slot("s1")
        assert row["state"] == slots.RELEASING
        assert row["held_by"] == "erik", "freed before the wipe was confirmed"
    finally:
        st.close()


def test_releasing_a_slot_that_is_not_there(db, capsys):
    assert cli.main(["--db", db, "slot", "release", "ghost"]) != 0
    assert "no slot 'ghost'" in capsys.readouterr().err


def test_granting_an_allowance(db, capsys):
    _account(db, "erik@example.com")
    assert cli.main(["--db", db, "account", "list"]) == 0
    assert "0" in capsys.readouterr().out

    assert cli.main(["--db", db, "account", "quota", "erik@example.com", "3"]) == 0
    assert "may claim 3 slots" in capsys.readouterr().out


def test_reducing_an_allowance_says_what_it_does_not_do(db, capsys):
    """The surprising half of the rule, said out loud: nobody is evicted."""
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "slot", "capacity", "m1", "2"]) == 0
    for n in (1, 2):
        assert cli.main(["--db", db, "slot", "add", f"s{n}", "--machine", "m1",
                         "--unix-user", f"slot0{n}"]) == 0
    _confirm_empty(db, "m1", "slot01", "slot02")
    _account(db, "erik@example.com", quota=2)
    from ccfleetd.store import Store
    st = Store(db)
    try:
        st.claim_slot("erik", now=1.0)
        st.claim_slot("erik", now=1.0)
    finally:
        st.close()

    assert cli.main(["--db", db, "account", "quota", "erik@example.com", "0"]) == 0
    out = capsys.readouterr().out
    assert "They keep the 2 they have" in out
    assert "slot release" in out

    st = Store(db)
    try:
        assert st.held_slot_count("erik") == 2
    finally:
        st.close()


def test_granting_an_allowance_to_a_stranger(db, capsys):
    assert cli.main(["--db", db, "account", "quota", "nobody@example.com", "5"]) != 0
    assert "nobody registered" in capsys.readouterr().err


def test_an_empty_fleet_says_so_rather_than_printing_a_header(db, capsys):
    assert cli.main(["--db", db, "slot", "list"]) == 0
    assert "no slots declared" in capsys.readouterr().out
    assert cli.main(["--db", db, "account", "list"]) == 0
    assert "nobody has registered" in capsys.readouterr().out


def test_setting_capacity_on_a_machine_that_is_not_there(db, capsys):
    assert cli.main(["--db", db, "slot", "capacity", "ghost", "4"]) != 0
    assert "no such machine" in capsys.readouterr().err


def test_removing_a_slot_from_the_command_line(db, capsys):
    from ccfleetd.store import Store
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "slot", "add", "s1", "--machine", "m1",
                     "--unix-user", "slot01"]) == 0
    _confirm_empty(db, "m1", "slot01")
    _account(db, "erik@example.com", quota=1)
    st = Store(db)
    try:
        st.claim_slot("erik", now=1.0)
    finally:
        st.close()

    # Held, so neither the slot nor its machine may be forgotten.
    assert cli.main(["--db", db, "slot", "remove", "s1"]) != 0
    assert "Release it first" in capsys.readouterr().err
    assert cli.main(["--db", db, "node", "remove", "m1"]) != 0
    assert "still has 1 slots" in capsys.readouterr().err

    assert cli.main(["--db", db, "slot", "release", "s1"]) == 0
    capsys.readouterr()
    st = Store(db)
    try:
        st.finish_release("s1", now=2.0)
    finally:
        st.close()
    assert cli.main(["--db", db, "slot", "remove", "s1"]) == 0
    assert "no longer declared" in capsys.readouterr().out
    assert cli.main(["--db", db, "node", "remove", "m1"]) == 0


def test_the_slot_list_lines_up_with_its_longest_value(db, capsys):
    """"not yet seen" is the widest thing the machine column holds; a column
    narrower than it pushed the holder out of line on every such row."""
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "slot", "add", "s1", "--machine", "m1",
                     "--unix-user", "slot01"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "slot", "list"]) == 0
    header, row = capsys.readouterr().out.splitlines()[:2]
    assert "not yet seen" in row
    assert row.rindex(" -") + 1 == header.index("held by")


# -- keeping a machine for one account ----------------------------------------------

def _machine_and_ana(db):
    from ccfleetd.store import Store
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "op"]) == 0
    assert cli.main(["--db", db, "slot", "capacity", "m1", "2"]) == 0
    st = Store(db)
    try:
        return st.upsert_account_from_google("sub-ana", "ana@example.com", now=1.0)
    finally:
        st.close()


def _node_row(listing, node_id):
    [row] = [line for line in listing.splitlines() if line.split()[:1] == [node_id]]
    return row


def test_keeping_a_machine_for_one_account_and_opening_it_again(db, capsys):
    _machine_and_ana(db)
    capsys.readouterr()
    assert cli.main(["--db", db, "node", "reserve", "m1", "ana@example.com"]) == 0
    assert "kept for ana@example.com" in capsys.readouterr().out
    assert cli.main(["--db", db, "node", "list"]) == 0
    listing = capsys.readouterr().out
    assert listing.splitlines()[0].rstrip().endswith("reserved")
    assert _node_row(listing, "m1").rstrip().endswith("ana@example.com")

    assert cli.main(["--db", db, "node", "reserve", "m1", "--none"]) == 0
    assert "open to anybody" in capsys.readouterr().out
    assert cli.main(["--db", db, "node", "list"]) == 0
    assert _node_row(capsys.readouterr().out, "m1").rstrip().endswith("-")


def test_keeping_a_machine_for_an_address_nobody_signed_in_with_fails(db, capsys):
    from ccfleetd.store import Store
    _machine_and_ana(db)
    capsys.readouterr()
    assert cli.main(["--db", db, "node", "reserve", "m1", "ana@exmaple.com"]) == 2
    assert "ana@exmaple.com" in capsys.readouterr().err
    st = Store(db)
    try:
        assert st.get_node("m1")["reserved_for"] is None
    finally:
        st.close()


def test_an_owner_node_cannot_be_kept_from_the_command_line(db, capsys):
    _machine_and_ana(db)
    assert cli.main(["--db", db, "node", "add", "laptop", "--owner", "erik"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "node", "reserve", "laptop", "ana@example.com"]) == 2
    assert "not a shared machine" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["m1"], ["m1", "ana@example.com", "--none"]])
def test_reserve_takes_an_address_or_none_but_not_both(db, capsys, args):
    _machine_and_ana(db)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", db, "node", "reserve", *args])
    assert exc.value.code == 2


def test_renaming_a_node_from_the_command_line(db, capsys):
    """The id moves, the token does not — and the operator is told the one
    thing the box itself still needs, because until then it is refused."""
    assert cli.main(["--db", db, "node", "add", "att3", "--owner", "erik"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "node", "rename", "att3", "erik-1"]) == 0
    out = capsys.readouterr().out
    assert "CCFLEET_NODE_ID=erik-1" in out
    assert "/etc/ccfleet/agent.env" in out and "~/.config/ccfleet/agent.env" in out
    assert "token is unchanged" in out
    assert cli.main(["--db", db, "node", "list"]) == 0
    listing = capsys.readouterr().out
    assert re.search(r"^erik-1 ", listing, re.M) and "att3" not in listing

    assert cli.main(["--db", db, "node", "rename", "att3", "erik-9"]) == 2      # gone
    assert "unknown node" in capsys.readouterr().err
    assert cli.main(["--db", db, "node", "rename", "erik-1", "Erik_1"]) == 2    # bad name
    assert "node id must be" in capsys.readouterr().err
    assert cli.main(["--db", db, "node", "add", "erik-2", "--owner", "erik"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "node", "rename", "erik-1", "erik-2"]) == 2    # taken
    assert "already exists" in capsys.readouterr().err


def test_renaming_a_slot_from_the_command_line(db, capsys):
    assert cli.main(["--db", db, "node", "add", "m1", "--owner", "op"]) == 0
    assert cli.main(["--db", db, "slot", "capacity", "m1", "2"]) == 0
    for sid, user in (("m1-01", "slot01"), ("m1-02", "slot02")):
        assert cli.main(["--db", db, "slot", "add", sid, "--machine", "m1",
                         "--unix-user", user]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "slot", "rename", "m1-01", "m1-a"]) == 0
    assert "Linux user" in capsys.readouterr().out
    assert cli.main(["--db", db, "slot", "list"]) == 0
    listing = capsys.readouterr().out
    assert re.search(r"^m1-a +m1 +slot01 ", listing, re.M) and "m1-01" not in listing

    assert cli.main(["--db", db, "slot", "rename", "m1-a", "m1-02"]) == 2       # taken
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["--db", db, "slot", "rename", "m1-a", "M1-A"]) == 2        # bad name
    assert "slot id must be" in capsys.readouterr().err
    assert cli.main(["--db", db, "slot", "rename", "nope", "m1-z"]) == 2        # unknown
    assert "no slot" in capsys.readouterr().err
