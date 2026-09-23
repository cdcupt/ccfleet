"""Slot model v2 (Erik, 2026-09-23): one account, one slot, one machine.

A slot is named after whoever holds it, because claude.ai/code shows a machine
by its hostname and one machine is one slot. An owner's own node counts as a
slot they hold, so everything a person uses is one list — but nothing about
that bookkeeping may ever reach the node itself: it is never wiped, never
handed out and never told to provision anything.
"""

from __future__ import annotations

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


def account(st, account_id, email=None, *, quota=1):
    return st.add_account(account_id, f"sub-{account_id}", email or f"{account_id}@example.com",
                          slot_quota=quota, now=NOW)


def free_again(st, slot_id):
    """Give the slot back and have its machine confirm the wipe."""
    st.begin_release(slot_id)
    slot = st.get_slot(slot_id)
    st.apply_slot_report(slot["node_id"], [{"unix_user": slot["unix_user"], "present": False}],
                         now=NOW + 60)


# -- a slot is named after its holder -------------------------------------------------------

def test_a_claimed_slot_is_named_after_its_holder(store):
    machine(store, "pool-1")
    account(store, "a1", "Alice.Smith@example.com")
    slot = store.claim_slot("a1", now=NOW)
    assert slot["id"] == "pool-1", "ids never change: sign-ins and pages are keyed on them"
    assert slot["name"] == "alice-smith-1"
    assert names.display(slot) == "alice-smith-1"


def test_the_operators_handle_wins_over_the_address(store):
    machine(store, "pool-1")
    account(store, "a1", "cdcupt@gmail.com")
    store.set_account_handle("a1", "erik")
    assert store.claim_slot("a1", now=NOW)["name"] == "erik-1"


def test_clearing_a_handle_goes_back_to_the_address(store):
    machine(store, "pool-1")
    account(store, "a1", "cdcupt@gmail.com")
    store.set_account_handle("a1", "erik")
    store.set_account_handle("a1", None)
    assert store.claim_slot("a1", now=NOW)["name"] == "cdcupt-1"


@pytest.mark.parametrize("bad", ["Erik", "-erik", "erik-", "er ik", "x" * 21, "erik\n", ""])
def test_a_handle_that_is_not_hostname_safe_is_refused(store, bad):
    account(store, "a1")
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
        # Somebody new claims: they get pool-1, named after them; erik-2 is kept.
        st.add_account("ualice", "g-alice", "alice@example.com", slot_quota=1, now=NOW)
        slot = st.claim_slot("ualice", now=NOW)
        assert (slot["id"], slot["name"]) == ("pool-1", "alice-1")
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
