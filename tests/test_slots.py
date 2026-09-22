"""The slot lifecycle, and the two things it exists to make impossible:

handing somebody a slot that still holds another person's work, and handing
somebody more slots than they are entitled to.
"""

from __future__ import annotations

import threading

import pytest

from ccfleetd import slots
from ccfleetd.store import NoSlotAvailable, QuotaExceeded, Store, StoreError

NOW = 1_700_000_000.0


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


def machine(st, node_id="m1", *, capacity=4, enabled=True):
    st.add_node(node_id, "owner", now=NOW)
    with st._lock:
        st._conn.execute("UPDATE nodes SET capacity = ?, enabled = ? WHERE id = ?",
                         (capacity, 1 if enabled else 0, node_id))
        st._conn.commit()
    return node_id


def account(st, account_id="a1", *, quota=0, sub=None):
    return st.add_account(account_id, sub or f"sub-{account_id}",
                          f"{account_id}@example.com", slot_quota=quota, now=NOW)


# -- the state machine on its own ---------------------------------------------

@pytest.mark.parametrize("frm", [s for s in slots.STATES if s != slots.RELEASING])
def test_free_is_reachable_only_out_of_releasing(frm):
    """The invariant the whole module exists for. `free` means the Linux user
    is gone; only a finished wipe can say that, so no other state may declare
    it — including `claiming`, whose provisioning may well have created the
    account before it failed."""
    assert not slots.can_move(frm, slots.FREE)
    with pytest.raises(slots.TransitionError) as exc:
        slots.check_move(frm, slots.FREE)
    assert "releasing" in str(exc.value), "the refusal should name the way out"


def test_releasing_does_reach_free():
    assert slots.can_move(slots.RELEASING, slots.FREE)


def test_a_slot_in_use_has_exactly_one_exit():
    """Nothing about an active slot may be undone except by wiping it."""
    assert slots.ALLOWED[slots.ACTIVE] == (slots.RELEASING,)


@pytest.mark.parametrize("frm", sorted(slots.RELEASABLE))
def test_every_state_a_person_can_hold_can_be_released(frm):
    assert slots.can_move(frm, slots.RELEASING)


def test_a_slot_being_wiped_still_counts_as_held():
    """The wipe has not finished. Handing the slot out now hands out the files."""
    assert slots.RELEASING in slots.HELD
    assert slots.FREE not in slots.HELD


@pytest.mark.parametrize("bad", ["", "FREE", "gone", "active "])
def test_states_that_do_not_exist_are_refused(bad):
    assert not slots.can_move(bad, slots.FREE)
    assert not slots.can_move(slots.ACTIVE, bad)
    with pytest.raises(slots.TransitionError):
        slots.check_move(slots.ACTIVE, bad)


def test_every_state_is_described_and_owned():
    """The console renders these; a state with no meaning shows as a blank cell."""
    assert set(slots.MEANING) == set(slots.STATES)
    assert set(slots.MOVED_ON_BY) == set(slots.STATES)
    assert set(slots.ALLOWED) == set(slots.STATES)


# -- releasing, which is built before claiming --------------------------------

def test_finishing_a_release_leaves_nothing_of_whoever_held_it(store):
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.claim_slot("a1", now=NOW)
    store.move_slot("s1", slots.CLAIMED)
    store.move_slot("s1", slots.ACTIVE)
    with store._lock:
        store._conn.execute("UPDATE slots SET device_token_at = ? WHERE id = 's1'",
                            (NOW,))
        store._conn.commit()

    store.begin_release("s1")
    assert store.finish_release("s1", now=NOW + 60)

    row = store.get_slot("s1")
    assert row["state"] == slots.FREE
    assert row["held_by"] is None, "a free slot still naming its last holder"
    assert row["claimed_at"] is None
    assert row["device_token_at"] == 0, "the next holder inherits a token history"
    assert row["released_at"] == NOW + 60


def test_a_slot_cannot_be_declared_free_without_the_wipe(store):
    """The whole point, at the store level rather than the state machine's."""
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.claim_slot("a1", now=NOW)
    store.move_slot("s1", slots.CLAIMED)
    store.move_slot("s1", slots.ACTIVE)

    with pytest.raises(slots.TransitionError):
        store.move_slot("s1", slots.FREE)
    with pytest.raises(slots.TransitionError):
        store.finish_release("s1", now=NOW)
    assert store.get_slot("s1")["state"] == slots.ACTIVE
    assert store.get_slot("s1")["held_by"] == "a1"


def test_releasing_a_slot_nobody_holds_is_refused(store):
    machine(store)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(slots.TransitionError):
        store.begin_release("s1")


def test_moving_a_slot_that_does_not_exist_says_so(store):
    with pytest.raises(StoreError):
        store.move_slot("nope", slots.RELEASING)
    with pytest.raises(StoreError):
        store.finish_release("nope", now=NOW)


# -- the allowance ------------------------------------------------------------

def test_a_new_account_can_claim_nothing(store):
    """Zero by default is the economics. A bug that grants nobody anything is a
    support message; a bug that grants everybody a slot is a bill."""
    machine(store)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    acct = account(store)
    assert acct["slot_quota"] == 0
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)
    assert store.get_slot("s1")["state"] == slots.FREE


def test_the_allowance_is_a_ceiling_not_a_starting_point(store):
    machine(store)
    account(store, quota=2)
    for n in (1, 2, 3):
        store.add_slot(f"s{n}", "m1", f"slot0{n}", now=NOW)
    store.claim_slot("a1", now=NOW)
    store.claim_slot("a1", now=NOW)
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)
    assert len(store.list_slots(held_by="a1")) == 2
    assert store.get_slot("s3")["state"] == slots.FREE


def test_a_slot_being_wiped_still_spends_the_allowance(store):
    """Until the wipe finishes the slot is not free, so it cannot be double-spent
    by releasing one and claiming another in the gap."""
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.add_slot("s2", "m1", "slot02", now=NOW)
    store.claim_slot("a1", now=NOW)
    store.begin_release("s1")

    assert store.held_slot_count("a1") == 1
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)

    store.finish_release("s1", now=NOW + 1)
    assert store.held_slot_count("a1") == 0
    assert store.claim_slot("a1", now=NOW + 2)["id"] in ("s1", "s2")


def test_reducing_an_allowance_takes_nothing_away(store):
    """Dropping somebody to zero stops them claiming more. Taking a held slot
    back is a release — a deliberate act with a wipe attached, never a side
    effect of a number changing."""
    machine(store)
    account(store, quota=2)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.add_slot("s2", "m1", "slot02", now=NOW)
    store.claim_slot("a1", now=NOW)
    store.claim_slot("a1", now=NOW)

    assert store.set_slot_quota("a1", 0)

    assert len(store.list_slots(held_by="a1")) == 2
    assert all(s["state"] != slots.FREE for s in store.list_slots(held_by="a1"))
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)


def test_an_allowance_cannot_go_negative(store):
    account(store)
    with pytest.raises(StoreError):
        store.set_slot_quota("a1", -1)
    with pytest.raises(StoreError):
        store.add_account("a2", "sub-a2", "a2@example.com", slot_quota=-1, now=NOW)


# -- claiming -----------------------------------------------------------------

def test_no_allowance_and_nothing_free_are_different_answers(store):
    """The page says different things. Conflating them into one absent return
    tells somebody who paid to 'try again later' forever."""
    machine(store)
    account(store, quota=1)
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW)

    store.set_slot_quota("a1", 0)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)


def test_a_disabled_machine_hands_out_nothing(store):
    machine(store, "m1", enabled=False)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW)
    assert store.get_slot("s1")["state"] == slots.FREE


def test_a_claim_can_ask_for_one_machine(store):
    machine(store, "m1")
    machine(store, "m2")
    account(store, quota=2)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.add_slot("s2", "m2", "slot01", now=NOW)
    assert store.claim_slot("a1", now=NOW, node_id="m2")["id"] == "s2"
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW, node_id="m2")


def test_claiming_for_an_account_that_does_not_exist_says_so(store):
    machine(store)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(StoreError):
        store.claim_slot("ghost", now=NOW)
    assert store.get_slot("s1")["state"] == slots.FREE


def test_two_tabs_cannot_both_get_the_last_slot(store):
    """The allowance is checked against what is held in the same lock and the
    same transaction that takes the slot. Without that, two requests each read
    'you hold none, here is a free one' before either writes."""
    machine(store, capacity=8)
    account(store, quota=1)
    for n in range(8):
        store.add_slot(f"s{n}", "m1", f"slot0{n}", now=NOW)

    got, refused = [], []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        try:
            got.append(store.claim_slot("a1", now=NOW)["id"])
        except (QuotaExceeded, NoSlotAvailable) as exc:
            refused.append(type(exc).__name__)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(got) == 1, f"allowance of 1 handed out {len(got)}: {got}"
    assert len(refused) == 7
    assert len(store.list_slots(held_by="a1")) == 1
    assert len([s for s in store.list_slots() if s["state"] == slots.FREE]) == 7


def test_one_free_slot_goes_to_exactly_one_of_many_accounts(store):
    """The other half of the race: plenty of allowance, one slot."""
    machine(store)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    for n in range(6):
        account(store, f"a{n}", quota=5)

    got, refused = [], []
    barrier = threading.Barrier(6)

    def grab(who):
        barrier.wait()
        try:
            got.append((who, store.claim_slot(who, now=NOW)["id"]))
        except NoSlotAvailable:
            refused.append(who)

    threads = [threading.Thread(target=grab, args=(f"a{n}",)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(got) == 1, f"one slot went to {len(got)} people: {got}"
    assert len(refused) == 5
    holders = {s["held_by"] for s in store.list_slots()}
    assert holders == {got[0][0]}


def test_a_released_slot_can_be_given_to_somebody_else_clean(store):
    machine(store)
    account(store, "a1", quota=1)
    account(store, "a2", quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)

    store.claim_slot("a1", now=NOW)
    store.move_slot("s1", slots.CLAIMED)
    store.move_slot("s1", slots.ACTIVE)
    store.begin_release("s1")
    store.finish_release("s1", now=NOW + 1)

    taken = store.claim_slot("a2", now=NOW + 2)
    assert taken["id"] == "s1"
    assert taken["held_by"] == "a2"
    assert taken["claimed_at"] == NOW + 2
    assert taken["released_at"] is None, "carries the previous holder's release"
    assert store.held_slot_count("a1") == 0


# -- declaring slots ----------------------------------------------------------

def test_a_machine_will_not_hold_more_slots_than_it_declares(store):
    """Capacity is what the operator says they sold. Better refused here than
    discovered as a box that will not hold them."""
    machine(store, capacity=2)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.add_slot("s2", "m1", "slot02", now=NOW)
    with pytest.raises(StoreError) as exc:
        store.add_slot("s3", "m1", "slot03", now=NOW)
    assert "capacity" in str(exc.value)
    assert len(store.list_slots(node_id="m1")) == 2


def test_a_slot_needs_a_machine_that_exists(store):
    with pytest.raises(StoreError):
        store.add_slot("s1", "ghost", "slot01", now=NOW)


def test_the_same_unix_user_cannot_be_two_slots_on_one_machine(store):
    machine(store)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(StoreError):
        store.add_slot("s2", "m1", "slot01", now=NOW)


@pytest.mark.parametrize("bad", ["", "Slot01", "-s", "s" * 65, "a"])
def test_slot_ids_are_checked(store, bad):
    machine(store)
    with pytest.raises(StoreError):
        store.add_slot(bad, "m1", "slot01", now=NOW)


@pytest.mark.parametrize("bad", ["", "Slot01", "1slot", "root user", "x" * 33])
def test_unix_user_names_are_checked(store, bad):
    machine(store)
    with pytest.raises(StoreError):
        store.add_slot("s1", "m1", bad, now=NOW)


def test_a_new_slot_starts_free_and_holds_nobody(store):
    machine(store)
    row = store.add_slot("s1", "m1", "slot01", now=NOW)
    assert row["state"] == slots.FREE
    assert row["held_by"] is None and row["claimed_at"] is None


# -- accounts -----------------------------------------------------------------

def test_an_account_is_found_by_google_subject_not_email(store):
    """People change their email address. The subject is what stays."""
    account(store, "a1", sub="google-123")
    assert store.account_by_google_sub("google-123")["id"] == "a1"
    assert store.account_by_google_sub("nobody") is None


def test_the_same_google_subject_cannot_register_twice(store):
    account(store, "a1", sub="google-123")
    with pytest.raises(StoreError):
        store.add_account("a2", "google-123", "other@example.com", now=NOW)


@pytest.mark.parametrize("bad", ["owner", "", "Admin", "superuser"])
def test_an_account_role_is_one_of_two_things(store, bad):
    with pytest.raises(StoreError):
        store.add_account("a1", "sub", "a@example.com", role=bad, now=NOW)


def test_setting_a_quota_on_nobody_reports_it(store):
    assert store.set_slot_quota("ghost", 3) is False
