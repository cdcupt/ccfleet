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


class _PauseAfter:
    """A real connection that stops once a named read has happened.

    This forces the interleaving that a wall-clock race only reaches by luck:
    the second claim reads "you hold none", the first claim then completes, and
    only afterwards does the second one write. A claim holding SQLite's write
    lock across its own read cannot be interleaved that way — the other
    connection waits — so this is the difference the lock makes, made
    deterministic instead of hoped for.
    """

    def __init__(self, conn, gate, marker):
        self._conn, self._gate, self._marker = conn, gate, marker
        #: Set once the read has happened and this connection is holding.
        #: Without waiting on it the other thread can finish first and the
        #: interleaving under test never occurs — the test then passes because
        #: nothing raced, not because racing was prevented.
        self.reached = threading.Event()

    def execute(self, sql, *args):
        cur = self._conn.execute(sql, *args)
        if self._marker in sql:
            self.reached.set()
            self._gate.wait(20)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_second_connection_cannot_decide_on_a_stale_read(tmp_path):
    """An allowance of one, two free slots, and two connections — `ccfleetd
    serve` and a `ccfleetd` command, say. The instance lock does not span them,
    and Python's sqlite3 leaves a bare SELECT outside any transaction, so
    without the write lock the second claim decides "you hold none" while the
    first is still deciding, and both take a slot.
    """
    db = str(tmp_path / "fleet.db")
    setup = Store(db)
    try:
        machine(setup, capacity=2)
        account(setup, quota=1)
        setup.add_slot("s1", "m1", "slot01", now=NOW)
        setup.add_slot("s2", "m1", "slot02", now=NOW)
    finally:
        setup.close()

    gate = threading.Event()
    reached = threading.Event()
    outcomes: dict[str, str] = {}

    def slow_claim():
        own = Store(db)
        own._conn = _PauseAfter(own._conn, gate,
                                "COUNT(*) AS n FROM slots WHERE held_by")
        own._conn.reached = reached
        try:
            outcomes["slow"] = own.claim_slot("a1", now=NOW)["id"]
        except (QuotaExceeded, NoSlotAvailable) as exc:
            outcomes["slow"] = type(exc).__name__
        finally:
            own.close()

    def plain_claim():
        own = Store(db)
        try:
            outcomes["plain"] = own.claim_slot("a1", now=NOW + 1)["id"]
        except (QuotaExceeded, NoSlotAvailable) as exc:
            outcomes["plain"] = type(exc).__name__
        finally:
            own.close()

    slow = threading.Thread(target=slow_claim)
    slow.start()
    assert reached.wait(10), "the first claim never reached its quota read"
    plain = threading.Thread(target=plain_claim)
    plain.start()
    # It either blocks on the write lock or races ahead. Give it long enough to
    # do the wrong thing, then let the first claim finish.
    plain.join(timeout=1.5)
    gate.set()
    slow.join(timeout=30)
    plain.join(timeout=30)

    after = Store(db)
    try:
        held = after.held_slot_count("a1")
        taken = [s["id"] for s in after.list_slots(held_by="a1")]
    finally:
        after.close()
    assert held == 1, f"allowance of 1 handed out {held} slots ({taken}): {outcomes}"
    assert "QuotaExceeded" in outcomes.values(), outcomes


def test_a_second_connection_cannot_overfill_a_machine(tmp_path):
    """The same shape as the stale claim, over a different decision: count the
    slots on a machine, then insert one. Two connections each counting before
    either inserts declare a machine past the capacity its operator sold."""
    db = str(tmp_path / "fleet.db")
    setup = Store(db)
    try:
        machine(setup, capacity=1)
    finally:
        setup.close()

    gate = threading.Event()
    reached = threading.Event()
    outcomes: dict[str, str] = {}

    def slow_add():
        own = Store(db)
        own._conn = _PauseAfter(own._conn, gate,
                                "COUNT(*) AS n FROM slots WHERE node_id")
        own._conn.reached = reached
        try:
            own.add_slot("s1", "m1", "slot01", now=NOW)
            outcomes["slow"] = "declared"
        except StoreError:
            outcomes["slow"] = "refused"
        finally:
            own.close()

    def plain_add():
        own = Store(db)
        try:
            own.add_slot("s2", "m1", "slot02", now=NOW)
            outcomes["plain"] = "declared"
        except StoreError:
            outcomes["plain"] = "refused"
        finally:
            own.close()

    slow = threading.Thread(target=slow_add)
    slow.start()
    assert reached.wait(10), "the first declaration never reached its count"
    plain = threading.Thread(target=plain_add)
    plain.start()
    plain.join(timeout=1.5)
    gate.set()
    slow.join(timeout=30)
    plain.join(timeout=30)

    after = Store(db)
    try:
        declared = after.list_slots(node_id="m1")
    finally:
        after.close()
    assert len(declared) == 1, (
        f"capacity 1 took {len(declared)} slots "
        f"({[d['id'] for d in declared]}): {outcomes}")
    assert "refused" in outcomes.values(), outcomes


# -- forgetting things --------------------------------------------------------

def test_a_machine_will_not_be_forgotten_while_it_has_slots(store):
    """SQLite does not enforce the foreign key, so deleting the machine leaves
    its slot rows behind — and the id is the operator's to choose, so the next
    machine registered under that name inherits them."""
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.claim_slot("a1", now=NOW)

    with pytest.raises(StoreError) as exc:
        store.remove_node("m1")
    assert "still has 1 slots" in str(exc.value)
    assert "s1" in str(exc.value), "the message should name what is held"
    assert store.get_node("m1") is not None
    assert store.get_slot("s1") is not None


def test_even_free_slots_keep_a_machine_from_being_forgotten(store):
    machine(store)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(StoreError) as exc:
        store.remove_node("m1")
    assert "all free" in str(exc.value)


def test_a_machine_registered_again_inherits_nothing(store):
    """The failure the refusal exists to prevent, followed through: take the
    slots off properly and the name comes back clean."""
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.claim_slot("a1", now=NOW)
    store.begin_release("s1")
    store.finish_release("s1", now=NOW + 1)
    store.remove_slot("s1")
    store.remove_node("m1")

    machine(store)
    assert store.list_slots(node_id="m1") == []
    assert store.held_slot_count("a1") == 0


def test_a_slot_is_only_safe_to_forget_once_it_is_wiped(store):
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.claim_slot("a1", now=NOW)

    with pytest.raises(StoreError) as exc:
        store.remove_slot("s1")
    assert "claiming" in str(exc.value) and "a1" in str(exc.value)
    assert store.get_slot("s1") is not None
    assert store.held_slot_count("a1") == 1, "stopped counting while still there"


@pytest.mark.parametrize("state", sorted(slots.RELEASABLE))
def test_no_state_but_free_may_be_forgotten(store, state):
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.claim_slot("a1", now=NOW)
    while store.get_slot("s1")["state"] != state:
        nxt = slots.CLAIMED if store.get_slot("s1")["state"] == slots.CLAIMING \
            else slots.ACTIVE
        store.move_slot("s1", nxt)
    with pytest.raises(StoreError):
        store.remove_slot("s1")


def test_forgetting_a_slot_that_is_not_there(store):
    with pytest.raises(StoreError, match="no slot"):
        store.remove_slot("ghost")
