"""Slot model v2 (Erik, 2026-09-23): one account, one slot, one machine.

A slot is named after whoever holds it, because claude.ai/code shows a machine
by its hostname and one machine is one slot. An owner's own node counts as a
slot they hold, so everything a person uses is one list — but nothing about
that bookkeeping may ever reach the node itself: it is never wiped, never
handed out and never told to provision anything.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from ccfleetd import names, slots
from ccfleetd.store import NoSlotAvailable, QuotaExceeded, Store, StoreError

NOW = 1_700_000_000.0


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


def machine(st, node_id, *, reserved_for=None):
    st.add_node(node_id, "op", now=NOW)
    st.add_slot(node_id, node_id, "slot01", now=NOW)
    st.apply_slot_report(node_id, [{"unix_user": "slot01", "present": False}], now=NOW)
    if reserved_for:
        st.reserve_machine(node_id, reserved_for)
    return node_id


def account(st, account_id, email=None, *, quota=1, handle=True):
    """Somebody who signed in. Their slots are named "<handle>-<n>" after a
    handle the operator set from their address, as these tests of numbering
    need; `handle=None` leaves the neutral names every claim gets by default."""
    email = email or f"{account_id}@example.com"
    made = st.add_account(account_id, f"sub-{account_id}", email, slot_quota=quota, now=NOW)
    if handle is not None:
        st.set_account_handle(account_id,
                              names.handle_from_email(email) if handle is True else handle)
    return made


def free_again(st, slot_id):
    """Give the slot back and have its machine confirm the wipe."""
    st.begin_release(slot_id)
    slot = st.get_slot(slot_id)
    st.apply_slot_report(slot["node_id"], [{"unix_user": slot["unix_user"], "present": False}],
                         now=NOW + 60)


# -- a slot's name is never its holder's address ----------------------------------------------

def test_a_claimed_slot_gets_a_neutral_name_never_its_holders(store):
    """Erik, 2026-09-24: the name is the machine's hostname, which claude.ai
    shows and Anthropic receives, so nothing of the holder's is in it."""
    machine(store, "pool-1")
    account(store, "a1", "Alice.Smith@example.com", handle=None)
    slot = store.claim_slot("a1", now=NOW)
    assert slot["id"] == "pool-1", "ids never change: sign-ins and pages are keyed on them"
    assert re.fullmatch(r"slot-[0-9]{4}", slot["name"])
    assert names.display(slot) == slot["name"]


def test_the_operators_handle_names_a_slot_only_when_set(store):
    machine(store, "pool-1")
    account(store, "a1", "Alice.Smith@example.com", handle="alice")
    assert store.claim_slot("a1", now=NOW)["name"] == "alice-1"


def test_the_operators_handle_wins_over_the_address(store):
    machine(store, "pool-1")
    account(store, "a1", "cdcupt@gmail.com")
    store.set_account_handle("a1", "erik")
    assert store.claim_slot("a1", now=NOW)["name"] == "erik-1"


def test_clearing_a_handle_goes_back_to_a_neutral_name(store):
    machine(store, "pool-1")
    account(store, "a1", "cdcupt@gmail.com")
    store.set_account_handle("a1", "erik")
    store.set_account_handle("a1", None)
    name = store.claim_slot("a1", now=NOW)["name"]
    assert re.fullmatch(r"slot-[0-9]{4}", name) and "cdcupt" not in name


@pytest.mark.parametrize("bad", ["Erik", "-erik", "erik-", "er ik", "x" * 21, "erik\n", ""])
def test_a_handle_that_is_not_hostname_safe_is_refused(store, bad):
    account(store, "a1", handle=None)
    with pytest.raises(StoreError):
        store.set_account_handle("a1", bad)
    assert store.get_account("a1")["handle"] is None


def test_a_handle_for_nobody_is_refused(store):
    with pytest.raises(StoreError):
        store.set_account_handle("ghost", "erik")


def test_the_number_skips_every_name_id_and_machine_already_in_use(store):
    """The name becomes a hostname: it may not collide with another slot's name,
    another slot's id, or a machine that already answers to it."""
    store.add_node("alice-1", "op", now=NOW)          # a machine called alice-1
    machine(store, "alice-2")                        # a slot whose id is alice-2
    machine(store, "pool-1")
    machine(store, "pool-2")
    account(store, "b1", "alice@elsewhere.example")
    store.set_account_handle("b1", "alice")
    store.claim_slot("b1", now=NOW, node_id="pool-1")   # alice-3: 1 is a machine, 2 a slot
    account(store, "a1", "alice@example.com")
    assert store.claim_slot("a1", now=NOW, node_id="pool-2")["name"] == "alice-4"
    assert store.get_slot("pool-1")["name"] == "alice-3"


def test_two_slots_of_one_person_get_two_numbers(store):
    machine(store, "pool-1")
    machine(store, "pool-2")
    account(store, "a1", "alice@example.com", quota=2)
    first = store.claim_slot("a1", now=NOW)
    second = store.claim_slot("a1", now=NOW)
    assert {first["name"], second["name"]} == {"alice-1", "alice-2"}


def test_two_slots_can_never_share_a_name(store):
    """Enforced by the database too, not only by the arithmetic above."""
    machine(store, "pool-1")
    machine(store, "pool-2")
    account(store, "a1", "alice@example.com")
    store.claim_slot("a1", now=NOW, node_id="pool-1")
    with pytest.raises(sqlite3.IntegrityError):
        with store._lock:
            store._conn.execute("UPDATE slots SET name = 'alice-1' WHERE id = 'pool-2'")
    store._conn.rollback()


def test_the_name_stays_through_the_wipe_and_goes_when_it_completes(store):
    machine(store, "pool-1")
    account(store, "a1", "alice@example.com")
    store.claim_slot("a1", now=NOW)
    store.begin_release("pool-1")
    assert store.get_slot("pool-1")["name"] == "alice-1", \
        "still theirs until the machine confirms the wipe"
    store.apply_slot_report("pool-1", [{"unix_user": "slot01", "present": False}], now=NOW + 60)
    slot = store.get_slot("pool-1")
    assert slot["state"] == slots.FREE and slot["name"] is None
    assert names.display(slot) == "pool-1"


def test_a_slot_freed_by_the_operator_loses_its_name_too(store):
    machine(store, "pool-1")
    account(store, "a1", "alice@example.com")
    store.claim_slot("a1", now=NOW)
    store.begin_release("pool-1")
    store.finish_release("pool-1", now=NOW + 60)
    assert store.get_slot("pool-1")["name"] is None


def test_the_next_holder_gets_their_own_name(store):
    machine(store, "pool-1")
    account(store, "a1", "alice@example.com")
    account(store, "b1", "bob@example.com")
    store.claim_slot("a1", now=NOW)
    free_again(store, "pool-1")
    assert store.claim_slot("b1", now=NOW + 120)["name"] == "bob-1"


# -- an owner's own node counts as their slot ---------------------------------------------

def owner_node(st, node_id="erik-1"):
    st.add_node(node_id, "erik", now=NOW)
    return node_id


def test_holding_an_owner_node_makes_it_a_slot_in_use(store):
    owner_node(store)
    account(store, "e1", "cdcupt@gmail.com")
    slot = store.hold_owner_node("erik-1", "e1", now=NOW)
    assert slot["id"] == slot["node_id"] == "erik-1"
    assert slot["kind"] == slots.OWNER_SLOT
    assert slot["state"] == slots.ACTIVE and slot["held_by"] == "e1"
    assert slot["unix_user"] == "erik", "the node's owner login, unless told otherwise"


def test_the_login_can_be_named(store):
    owner_node(store)
    account(store, "e1")
    assert store.hold_owner_node("erik-1", "e1", unix_user="dev", now=NOW)["unix_user"] == "dev"


def test_holding_counts_toward_the_allowance(store):
    owner_node(store)
    machine(store, "erik-2")
    account(store, "e1", "cdcupt@gmail.com", quota=1)
    store.claim_slot("e1", now=NOW)
    with pytest.raises(QuotaExceeded) as exc:
        store.hold_owner_node("erik-1", "e1", now=NOW)
    assert "allowance" in str(exc.value)
    assert store.get_slot("erik-1") is None
    store.set_slot_quota("e1", 2)
    store.hold_owner_node("erik-1", "e1", now=NOW)
    assert store.held_slot_count("e1") == 2


def test_an_account_with_no_allowance_holds_nothing(store):
    owner_node(store)
    account(store, "e1", quota=0)
    with pytest.raises(QuotaExceeded):
        store.hold_owner_node("erik-1", "e1", now=NOW)


def test_holding_again_updates_the_one_row(store):
    owner_node(store)
    account(store, "e1", quota=1)
    store.hold_owner_node("erik-1", "e1", now=NOW)
    store.hold_owner_node("erik-1", "e1", unix_user="dev", now=NOW)
    held = store.list_slots(node_id="erik-1")
    assert len(held) == 1 and held[0]["unix_user"] == "dev"
    assert store.held_slot_count("e1") == 1


def test_handing_an_owner_node_to_somebody_else_checks_their_allowance(store):
    owner_node(store)
    account(store, "e1", quota=1)
    account(store, "e2", quota=0)
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with pytest.raises(QuotaExceeded):
        store.hold_owner_node("erik-1", "e2", now=NOW)
    assert store.get_slot("erik-1")["held_by"] == "e1"


def test_a_shared_machine_cannot_be_held(store):
    machine(store, "pool-1")
    account(store, "e1", quota=2)
    with pytest.raises(StoreError) as exc:
        store.hold_owner_node("pool-1", "e1", now=NOW)
    assert "claimed" in str(exc.value)


def test_holding_needs_a_real_node_and_account(store):
    owner_node(store)
    account(store, "e1")
    with pytest.raises(StoreError):
        store.hold_owner_node("nope", "e1", now=NOW)
    with pytest.raises(StoreError):
        store.hold_owner_node("erik-1", "ghost", now=NOW)


def test_an_owner_slot_is_never_handed_out(store):
    """Even if something set it free behind our back, a claim passes it by."""
    owner_node(store)
    account(store, "e1")
    account(store, "a1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with store._lock:
        store._conn.execute("UPDATE slots SET state = 'free', held_by = NULL, present = 0 "
                            "WHERE id = 'erik-1'")
        store._conn.commit()
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW)


def test_an_owner_slot_is_never_released(store):
    """Releasing means wiping. Nothing on somebody's own node is ours to wipe."""
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with pytest.raises(StoreError) as exc:
        store.begin_release("erik-1")
    assert "own machine" in str(exc.value)
    assert store.get_slot("erik-1")["state"] == slots.ACTIVE


def test_an_owner_slot_is_never_moved_by_a_report(store):
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    moved = store.apply_slot_report("erik-1", [{"unix_user": "erik", "present": False}],
                                    now=NOW)
    assert moved == []
    slot = store.get_slot("erik-1")
    assert slot["state"] == slots.ACTIVE and slot["present"] is None


def test_an_owner_slot_is_not_one_of_its_nodes_machine_slots(store):
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    assert store.list_slots(node_id="erik-1", kind=slots.MACHINE_SLOT) == []
    assert [s["id"] for s in store.list_slots(node_id="erik-1")] == ["erik-1"]


def test_removing_an_owner_slot_points_at_letting_go(store):
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with pytest.raises(StoreError) as exc:
        store.remove_slot("erik-1")
    assert "node hold" in str(exc.value)


def test_letting_go_forgets_the_record_and_nothing_else(store):
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    store.request_login("erik-1", "", NOW)
    assert store.unhold_owner_node("erik-1") is True
    assert store.get_slot("erik-1") is None
    assert store.get_node("erik-1") is not None
    assert store.get_login("erik-1") is not None, "the node's own sign-in is the node's"
    assert store.unhold_owner_node("erik-1") is False


def test_renaming_an_owner_node_carries_its_slot(store):
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    store.rename_node("erik-1", "erik-9")
    slot = store.get_slot("erik-9")
    assert slot is not None and slot["node_id"] == "erik-9" and slot["held_by"] == "e1"
    assert store.get_slot("erik-1") is None


def test_an_owner_node_with_its_slot_is_still_not_a_machine_to_reserve(store):
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with pytest.raises(StoreError):
        store.reserve_machine("erik-1", "e1")


def test_removing_a_held_owner_node_points_at_letting_go(store):
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with pytest.raises(StoreError) as exc:
        store.remove_node("erik-1")
    assert "node hold erik-1 --none" in str(exc.value)


def test_renaming_an_owner_node_onto_a_slots_id_is_refused_whole(store):
    """No node is called erik-9, but a slot is: the owner slot, which is
    renamed with its node, would collide with it. Refused before anything
    moves, rather than failing halfway inside the database."""
    owner_node(store)
    machine(store, "pool-9")
    store.rename_slot("pool-9", "erik-9")    # pool-9's slot is now called erik-9
    assert store.get_node("erik-9") is None
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with pytest.raises(StoreError) as exc:
        store.rename_node("erik-1", "erik-9")
    assert "already answers to 'erik-9'" in str(exc.value)
    assert store.get_slot("erik-1")["node_id"] == "erik-1", "nothing half-renamed"
    assert store.get_node("erik-1") is not None


def test_the_number_skips_a_slot_id_that_is_no_machines_id(store):
    """A slot renamed to alice-1 on machine pool-7: nothing else answers to
    alice-1, but a new name must still not be that slot's id."""
    machine(store, "pool-7")
    store.rename_slot("pool-7", "alice-1")
    machine(store, "pool-1")
    account(store, "a1", "alice@example.com")
    assert store.claim_slot("a1", now=NOW, node_id="pool-1")["name"] == "alice-2"


@pytest.mark.parametrize("login", ["Bad User", "root;rm", "9lives"])
def test_holding_with_a_login_that_is_no_linux_login_is_refused(store, login):
    owner_node(store)
    account(store, "e1")
    with pytest.raises(StoreError):
        store.hold_owner_node("erik-1", "e1", unix_user=login, now=NOW)
    assert store.get_slot("erik-1") is None


# -- one slot per machine, in the store itself ------------------------------------------

def test_the_store_refuses_a_second_slot_on_a_machine(store):
    """Not only the operator's commands: every way in goes through here."""
    machine(store, "pool-1")
    with pytest.raises(StoreError) as exc:
        store.add_slot("pool-1-b", "pool-1", "slot02", now=NOW)
    assert "one machine is one slot" in str(exc.value) and "pool-1" in str(exc.value)
    assert [s["id"] for s in store.list_slots(node_id="pool-1")] == ["pool-1"]


@pytest.mark.parametrize("capacity", [2, 8])
def test_the_store_refuses_a_capacity_above_one(store, capacity):
    store.add_node("pool-1", "op", now=NOW)
    with pytest.raises(StoreError) as exc:
        store.set_machine_capacity("pool-1", capacity)
    assert "one machine is one slot" in str(exc.value)
    assert store.get_node("pool-1")["capacity"] == 1


def test_an_owner_slot_does_not_count_as_the_machines_slot(store):
    """Holding an owner's node is a record; it does not fill a slot place."""
    owner_node(store)
    account(store, "e1")
    store.hold_owner_node("erik-1", "e1", now=NOW)
    with pytest.raises(StoreError):          # it is still capacity 1 with a row on it
        store.add_slot("erik-1-x", "erik-1", "slot01", now=NOW)


def test_a_machine_with_two_slots_from_before_answers_to_its_own_id():
    """A database from before one slot per machine may still hold one. Its
    hostname is never one holder's name, which the other would be shown under."""
    from ccfleetd.desired import machine_hostname
    st = Store(":memory:", max_slots_per_machine=2)
    try:
        st.add_node("old-m", "op", now=NOW)
        st.set_machine_capacity("old-m", 2)
        for sid, user in (("old-m-01", "slot01"), ("old-m-02", "slot02")):
            st.add_slot(sid, "old-m", user, now=NOW)
        st.apply_slot_report("old-m", [{"unix_user": u, "present": False}
                                       for u in ("slot01", "slot02")], now=NOW)
        st.add_account("a1", "sub-a1", "alice@example.com", slot_quota=1, now=NOW)
        name = st.claim_slot("a1", now=NOW)["name"]
        rows = st.list_slots(node_id="old-m", kind=slots.MACHINE_SLOT)
        assert machine_hostname("old-m", rows) == "old-m"
        assert machine_hostname("old-m", rows[:1]) == name
    finally:
        st.close()


# -- the live database, as it stood on 2026-09-23 ---------------------------------------------

def live_shaped(path):
    """nodes erik-1 (owner), erik-2 (machine, kept for Erik, slot erik-2 in
    use) and pool-1 (machine, slot pool-1 free), in the schema from before
    names, handles and owner slots."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE nodes (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT '', token_hash TEXT NOT NULL UNIQUE,
            pinned_version TEXT NOT NULL DEFAULT '', rc_expected INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
            device_token_at REAL NOT NULL DEFAULT 0,
            capacity INTEGER NOT NULL DEFAULT 1, tier TEXT NOT NULL DEFAULT 'dedicated',
            reserved_for TEXT);
        CREATE TABLE accounts (id TEXT PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
            slot_quota INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE slots (id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
            unix_user TEXT NOT NULL, state TEXT NOT NULL,
            held_by TEXT REFERENCES accounts(id), claimed_at REAL, released_at REAL,
            device_token_at REAL NOT NULL DEFAULT 0, present INTEGER, reported_at REAL,
            UNIQUE (node_id, unix_user));
        INSERT INTO accounts (id, google_sub, email, role, slot_quota, created_at)
            VALUES ('uerik', 'g-erik', 'cdcupt@gmail.com', 'admin', 1, 1.0);
        INSERT INTO nodes (id, owner, region, token_hash, pinned_version, created_at)
            VALUES ('erik-1', 'erik', 'us-residential-att', 'h1', '2.1.278', 1.0);
        INSERT INTO nodes (id, owner, region, token_hash, pinned_version, created_at,
                           reserved_for)
            VALUES ('erik-2', 'erik', 'us-west-residential', 'h2', 'stable', 1.0, 'uerik');
        INSERT INTO nodes (id, owner, region, token_hash, pinned_version, created_at)
            VALUES ('pool-1', 'erik', 'us-west-residential', 'h3', 'stable', 1.0);
        INSERT INTO slots (id, node_id, unix_user, state, held_by, claimed_at, present,
                           reported_at)
            VALUES ('erik-2', 'erik-2', 'slot01', 'active', 'uerik', 2.0, 1, 3.0);
        INSERT INTO slots (id, node_id, unix_user, state, present, reported_at)
            VALUES ('pool-1', 'pool-1', 'slot01', 'free', 0, 3.0);
    """)
    con.commit()
    con.close()


def test_the_live_database_opens_unchanged(tmp_path):
    path = str(tmp_path / "live.db")
    live_shaped(path)
    st = Store(path)
    try:
        by_id = {s["id"]: s for s in st.list_slots()}
        assert set(by_id) == {"erik-2", "pool-1"}
        assert all(s["kind"] == slots.MACHINE_SLOT for s in by_id.values())
        assert all(s["name"] is None for s in by_id.values()), "names come with claims"
        assert [names.display(s) for s in st.list_slots()] == ["erik-2", "pool-1"]
        assert by_id["erik-2"]["state"] == slots.ACTIVE and by_id["erik-2"]["held_by"] == "uerik"
        erik = st.get_account("uerik")
        assert erik["slot_quota"] == 1 and erik["handle"] is None
        assert st.get_node("erik-2")["reserved_for"] == "uerik"
    finally:
        st.close()


def test_the_live_database_behaves(tmp_path):
    path = str(tmp_path / "live.db")
    live_shaped(path)
    st = Store(path)
    try:
        # Somebody new claims: they get pool-1, under a neutral name that says
        # nothing of theirs; erik-2 is kept.
        st.add_account("ualice", "g-alice", "alice@example.com", slot_quota=1, now=NOW)
        slot = st.claim_slot("ualice", now=NOW)
        assert slot["id"] == "pool-1" and re.fullmatch(r"slot-[0-9]{4}", slot["name"])
        # Erik's own node becomes his second slot once his allowance says so.
        st.set_account_handle("uerik", "erik")
        with pytest.raises(QuotaExceeded):
            st.hold_owner_node("erik-1", "uerik", now=NOW)
        st.set_slot_quota("uerik", 2)
        held = st.hold_owner_node("erik-1", "uerik", now=NOW)
        assert (held["id"], held["kind"], held["unix_user"]) == ("erik-1", "owner", "erik")
        assert sorted(s["id"] for s in st.list_slots(held_by="uerik")) == ["erik-1", "erik-2"]
        with pytest.raises(StoreError):
            st.begin_release("erik-1")
    finally:
        st.close()


# -- signing in on an owner slot is the node's own sign-in ---------------------------------

def held_owner(st):
    owner_node(st)
    account(st, "e1", "cdcupt@gmail.com")
    account(st, "x1", "someone@example.com")
    st.hold_owner_node("erik-1", "e1", now=NOW)


def test_signing_in_on_an_owner_slot_asks_the_node_itself(store):
    """The owner's own agent does the signing in; it reads the node's row."""
    held_owner(store)
    store.request_slot_login("erik-1", "", NOW, held_by="e1")
    assert store.get_login("erik-1")["state"] == "requested"
    assert store.get_login("slot:erik-1") is None
    assert store.login_for_slot(store.get_slot("erik-1"))["state"] == "requested"


def test_a_machine_slot_keeps_its_own_sign_in_row(store):
    machine(store, "pool-1")
    account(store, "a1")
    store.claim_slot("a1", now=NOW)
    store.apply_slot_report("pool-1", [{"unix_user": "slot01", "present": True,
                                        "provisioned_for": NOW}], now=NOW)
    store.request_slot_login("pool-1", "", NOW, held_by="a1")
    assert store.get_login("slot:pool-1")["state"] == "requested"
    assert store.get_login("pool-1") is None
    assert store.login_for_slot(store.get_slot("pool-1"))["state"] == "requested"


def test_only_the_holder_signs_in_on_an_owner_slot(store):
    from ccfleetd.store import NotYours
    held_owner(store)
    with pytest.raises(NotYours):
        store.request_slot_login("erik-1", "", NOW, held_by="x1")
    assert store.get_login("erik-1") is None


def test_the_code_goes_to_the_nodes_own_sign_in(store):
    held_owner(store)
    store.request_slot_login("erik-1", "", NOW, held_by="e1")
    store.submit_slot_login_code("erik-1", "abc#123", NOW, held_by="e1")
    assert store.get_login("erik-1")["state"] == "code_sent"


def test_cancelling_on_an_owner_slot_ends_the_nodes_sign_in(store):
    held_owner(store)
    store.request_slot_login("erik-1", "", NOW, held_by="e1")
    store.clear_slot_login("erik-1", held_by="e1")
    assert store.get_login("erik-1") is None


def test_a_token_minted_on_an_owner_slot_is_read_from_the_node(store):
    held_owner(store)
    store.request_slot_login("erik-1", "", NOW, kind="token", held_by="e1")
    requested_at = store.get_login("erik-1")["requested_at"]
    store.record_login_progress("erik-1", "ready", "", "", NOW + 5, requested_at,
                                secret="sk-ant-oat01-" + "x" * 90)
    assert store.read_slot_secret("erik-1", NOW + 6, held_by="e1").startswith("sk-ant-oat01-")
    assert store.get_node("erik-1")["device_token_at"] == NOW + 6
    from ccfleetd.store import NotYours
    with pytest.raises(NotYours):
        store.read_slot_secret("erik-1", NOW + 7, held_by="x1")


def test_handing_an_owner_node_to_somebody_else_drops_what_is_in_flight(store):
    """A token the last holder minted, or a sign-in they started, is theirs:
    the next holder of the record never inherits the node's row."""
    held_owner(store)
    store.set_slot_quota("x1", 1)
    store.request_slot_login("erik-1", "", NOW, kind="token", held_by="e1")
    requested_at = store.get_login("erik-1")["requested_at"]
    store.record_login_progress("erik-1", "ready", "", "", NOW + 5, requested_at,
                                secret="sk-ant-oat01-" + "y" * 90)
    store.hold_owner_node("erik-1", "x1", now=NOW + 10)
    assert store.get_login("erik-1") is None
    assert store.read_slot_secret("erik-1", NOW + 11, held_by="x1") == ""


def test_holding_again_for_the_same_person_keeps_what_is_in_flight(store):
    held_owner(store)
    store.request_slot_login("erik-1", "", NOW, held_by="e1")
    store.hold_owner_node("erik-1", "e1", unix_user="dev", now=NOW + 10)
    assert store.get_login("erik-1")["state"] == "requested"


def test_a_first_hold_drops_a_sign_in_nobody_on_the_page_started(store):
    """Whatever the console had in flight on the node is not the new holder's."""
    owner_node(store)
    account(store, "e1")
    store.request_login("erik-1", "", NOW)
    store.hold_owner_node("erik-1", "e1", now=NOW + 1)
    assert store.get_login("erik-1") is None


# -- no two things answer to one name ------------------------------------------------------

def held_as_alice(store):
    machine(store, "pool-1")
    account(store, "a1", "alice@example.com")
    store.claim_slot("a1", now=NOW)          # pool-1's slot now answers to alice-1


def test_a_slot_cannot_be_renamed_onto_a_name_in_use(store):
    held_as_alice(store)
    machine(store, "pool-2")
    with pytest.raises(StoreError) as exc:
        store.rename_slot("pool-2", "alice-1")
    assert "alice-1" in str(exc.value)
    assert store.get_slot("pool-2") is not None


def test_a_slot_cannot_be_renamed_onto_another_machines_id(store):
    """No slot is called pool-1 any more, but a machine is: a slot on pool-2
    answering to pool-1 would be a second machine under that name."""
    machine(store, "pool-1")
    store.rename_slot("pool-1", "p1-slot")
    machine(store, "pool-2")
    with pytest.raises(StoreError):
        store.rename_slot("pool-2", "pool-1")
    assert store.get_slot("pool-2") is not None


def test_a_slot_can_take_its_own_holders_name_as_its_id(store):
    held_as_alice(store)
    store.rename_slot("pool-1", "alice-1")
    assert store.get_slot("alice-1")["name"] == "alice-1"


def test_a_slot_cannot_be_declared_under_a_name_in_use(store):
    held_as_alice(store)
    store.add_node("pool-2", "op", now=NOW)
    with pytest.raises(StoreError):
        store.add_slot("alice-1", "pool-2", "slot01", now=NOW)


def test_a_slot_cannot_be_declared_under_another_machines_id(store):
    store.add_node("pool-1", "op", now=NOW)
    store.add_node("pool-2", "op", now=NOW)
    with pytest.raises(StoreError):
        store.add_slot("pool-1", "pool-2", "slot01", now=NOW)
    store.add_slot("pool-2", "pool-2", "slot01", now=NOW)      # its own machine's: the rule
    assert store.get_slot("pool-2")["node_id"] == "pool-2"


def test_a_node_cannot_be_added_under_a_name_in_use(store):
    held_as_alice(store)
    with pytest.raises(StoreError) as exc:
        store.add_node("alice-1", "op", now=NOW)
    assert "alice-1" in str(exc.value)
    assert store.get_node("alice-1") is None


def test_a_node_cannot_be_renamed_onto_a_name_in_use(store):
    held_as_alice(store)
    store.add_node("erik-1", "erik", now=NOW)
    with pytest.raises(StoreError):
        store.rename_node("erik-1", "alice-1")
    assert store.get_node("erik-1") is not None


def test_a_node_cannot_be_renamed_onto_another_machines_slot_id(store):
    machine(store, "pool-1")
    store.add_node("erik-1", "erik", now=NOW)
    store.remove_slot("pool-1")
    machine(store, "pool-3")
    store.rename_slot("pool-3", "pool-7")      # a slot called pool-7, on pool-3
    with pytest.raises(StoreError):
        store.rename_node("erik-1", "pool-7")


# -- one account, one node, with an owner's node counted as their slot -----------------------

FP, OTHER_FP = "0123456789abcdef", "fedcba9876543210"


def erik_on_both(st, *, slot_fp):
    """Erik's own node erik-1, held as his slot, signed in with FP; and his slot
    on the shared machine erik-2, in use, signed in with `slot_fp`."""
    from ccfleetd.config import Config
    owner_node(st)
    account(st, "e1", "cdcupt@gmail.com", quota=2)
    st.hold_owner_node("erik-1", "e1", now=NOW)
    machine(st, "erik-2")
    claim = st.claim_slot("e1", now=NOW)["claimed_at"]
    st.apply_slot_report("erik-2", [{"unix_user": "slot01", "present": True,
                                     "provisioned_for": claim}], now=NOW)
    signed = {"unix_user": "slot01", "present": True,
              "credentials": {"logged_in": True, "account_fp": slot_fp}}
    st.apply_slot_report("erik-2", [signed], now=NOW)
    st.insert_heartbeat("erik-1", NOW, {"node_id": "erik-1",
                                        "credentials": {"logged_in": True, "account_fp": FP}})
    st.insert_heartbeat("erik-2", NOW, {"node_id": "erik-2", "mode": "machine",
                                        "slots": [signed]})
    return Config()


def test_an_owner_slot_is_one_place_not_two(store):
    """The owner slot is a record over the node: the node is the one place its
    account is live, under the node's id, and the record adds no second."""
    from ccfleetd import rules
    cfg = erik_on_both(store, slot_fp=OTHER_FP)
    places = rules.account_places(store.list_nodes(), store.latest_heartbeats(),
                                  store.list_slots(), NOW, cfg)
    assert places == {FP: ["erik-1"], OTHER_FP: ["erik-2"]}


def test_two_different_accounts_on_ones_own_slots_raise_nothing(store):
    from ccfleetd.monitor import Monitor
    from ccfleetd.notify import LogNotifier
    cfg = erik_on_both(store, slot_fp=OTHER_FP)
    Monitor(store, cfg, LogNotifier(), clock=lambda: NOW).check_all()
    assert not [a for a in store.open_alerts() if a["rule"].startswith("account_")]


def test_one_account_on_ones_own_node_and_slot_is_flagged_on_both(store):
    from ccfleetd.monitor import Monitor
    from ccfleetd.notify import LogNotifier
    cfg = erik_on_both(store, slot_fp=FP)
    Monitor(store, cfg, LogNotifier(), clock=lambda: NOW).check_all()
    flagged = sorted((a["node_id"], a["rule"]) for a in store.open_alerts()
                     if a["rule"].startswith("account_"))
    assert flagged == [("erik-1", "account_elsewhere"), ("erik-2", "account_elsewhere:slot01")]
