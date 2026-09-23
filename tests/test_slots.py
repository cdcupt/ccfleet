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
    st = Store(":memory:", max_slots_per_machine=8)
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


def declare(st, slot_id, node_id, unix_user):
    """A slot whose machine has reported its Linux user absent.

    The only kind a claim will take: free is a statement about the machine, and
    a slot nobody on the machine's side has vouched for is not handed out.
    """
    st.add_slot(slot_id, node_id, unix_user, now=NOW)
    st.apply_slot_report(node_id, [{"unix_user": unix_user, "present": False}], now=NOW)


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
    declare(store, "s1", "m1", "slot01")
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
    declare(store, "s1", "m1", "slot01")
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
    declare(store, "s1", "m1", "slot01")
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
    declare(store, "s1", "m1", "slot01")
    acct = account(store)
    assert acct["slot_quota"] == 0
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)
    assert store.get_slot("s1")["state"] == slots.FREE


def test_the_allowance_is_a_ceiling_not_a_starting_point(store):
    machine(store)
    account(store, quota=2)
    for n in (1, 2, 3):
        declare(store, f"s{n}", "m1", f"slot0{n}")
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
    declare(store, "s1", "m1", "slot01")
    declare(store, "s2", "m1", "slot02")
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
    declare(store, "s1", "m1", "slot01")
    declare(store, "s2", "m1", "slot02")
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
    declare(store, "s1", "m1", "slot01")
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)


def test_a_disabled_machine_hands_out_nothing(store):
    machine(store, "m1", enabled=False)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW)
    assert store.get_slot("s1")["state"] == slots.FREE


def test_a_claim_can_ask_for_one_machine(store):
    machine(store, "m1")
    machine(store, "m2")
    account(store, quota=2)
    declare(store, "s1", "m1", "slot01")
    declare(store, "s2", "m2", "slot01")
    assert store.claim_slot("a1", now=NOW, node_id="m2")["id"] == "s2"
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW, node_id="m2")


def test_claiming_for_an_account_that_does_not_exist_says_so(store):
    machine(store)
    declare(store, "s1", "m1", "slot01")
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
        declare(store, f"s{n}", "m1", f"slot0{n}")

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
    declare(store, "s1", "m1", "slot01")
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
    declare(store, "s1", "m1", "slot01")

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
    declare(store, "s1", "m1", "slot01")
    declare(store, "s2", "m1", "slot02")
    with pytest.raises(StoreError) as exc:
        store.add_slot("s3", "m1", "slot03", now=NOW)
    assert "capacity" in str(exc.value)
    assert len(store.list_slots(node_id="m1")) == 2


def test_a_slot_needs_a_machine_that_exists(store):
    with pytest.raises(StoreError):
        store.add_slot("s1", "ghost", "slot01", now=NOW)


def test_the_same_unix_user_cannot_be_two_slots_on_one_machine(store):
    machine(store)
    declare(store, "s1", "m1", "slot01")
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
    setup = Store(db, max_slots_per_machine=8)
    try:
        machine(setup, capacity=2)
        account(setup, quota=1)
        declare(setup, "s1", "m1", "slot01")
        declare(setup, "s2", "m1", "slot02")
    finally:
        setup.close()

    gate = threading.Event()
    reached = threading.Event()
    outcomes: dict[str, str] = {}

    def slow_claim():
        own = Store(db, max_slots_per_machine=8)
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
        own = Store(db, max_slots_per_machine=8)
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

    after = Store(db, max_slots_per_machine=8)
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
    setup = Store(db, max_slots_per_machine=8)
    try:
        machine(setup, capacity=1)
    finally:
        setup.close()

    gate = threading.Event()
    reached = threading.Event()
    outcomes: dict[str, str] = {}

    def slow_add():
        own = Store(db, max_slots_per_machine=8)
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
        own = Store(db, max_slots_per_machine=8)
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

    after = Store(db, max_slots_per_machine=8)
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
    declare(store, "s1", "m1", "slot01")
    store.claim_slot("a1", now=NOW)

    with pytest.raises(StoreError) as exc:
        store.remove_node("m1")
    assert "still has 1 slots" in str(exc.value)
    assert "s1" in str(exc.value), "the message should name what is held"
    assert store.get_node("m1") is not None
    assert store.get_slot("s1") is not None


def test_even_free_slots_keep_a_machine_from_being_forgotten(store):
    machine(store)
    declare(store, "s1", "m1", "slot01")
    with pytest.raises(StoreError) as exc:
        store.remove_node("m1")
    assert "all free" in str(exc.value)


def test_a_machine_registered_again_inherits_nothing(store):
    """The failure the refusal exists to prevent, followed through: take the
    slots off properly and the name comes back clean."""
    machine(store)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")
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
    declare(store, "s1", "m1", "slot01")
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
    declare(store, "s1", "m1", "slot01")
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


def test_free_cannot_be_reached_through_the_generic_move(store):
    """The lifecycle allows releasing -> free, but not by this door. Only
    finish_release clears the holder, the claim time and the device-token mark,
    and it does it in the same statement that sets the state — so a slot cannot
    read as nobody's while still naming the person whose files may be on it."""
    machine(store)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")
    store.claim_slot("a1", now=NOW)
    store.begin_release("s1")

    with pytest.raises(slots.TransitionError, match="finish_release"):
        store.move_slot("s1", slots.FREE)

    row = store.get_slot("s1")
    assert row["state"] == slots.RELEASING
    assert row["held_by"] == "a1"
    assert store.held_slot_count("a1") == 1


@pytest.mark.parametrize("name", ["a", "_slot", "1slot", "Slot01", "s" * 33,
                                  "slot 01", "slot.01"])
def test_a_slot_name_the_machine_would_refuse_is_refused_here(store, name):
    """node/slot-add.sh takes 2-32 characters starting with a lowercase letter.
    A name recorded here that the script rejects is a slot in our records that
    can never exist on the machine."""
    machine(store)
    with pytest.raises(StoreError):
        store.add_slot("s1", "m1", name, now=NOW)


def test_the_fleet_and_the_provisioning_script_accept_the_same_names():
    """Two files, one rule. Drift means the console records slots the operator
    cannot create, and the mismatch only shows up on the machine."""
    import pathlib as _p
    import re as _re

    from ccfleetd.store import UNIX_USER_RE
    script = (_p.Path(__file__).resolve().parents[1] / "node" / "slot-add.sh").read_text()
    found = _re.search(r"grep -qE '\^(\[a-z\]\[a-z0-9_-\]\{1,31\})\$'", script)
    assert found, "slot-add.sh no longer validates --slot the way this test expects"
    assert UNIX_USER_RE.pattern == f"^{found.group(1)}$".replace("\\", "")


# -- what the machine says moves the slot ---------------------------------------
#
# A shared machine reports each of its slots on every heartbeat. These moves are
# the system's own — provisioning finished, a login appeared, a wipe completed —
# and each asks for the evidence that actually proves it.

CLAIM = NOW + 100.0


def report(user="slot01", **fields):
    return {"unix_user": user, **fields}


def _reports_for_every_shape():
    """Enough report shapes to find a move that should not happen."""
    yield {}
    for present in (True, False, None):
        yield {"present": present}
        for claim in (CLAIM, CLAIM - 50.0, None, True):
            yield {"present": present, "provisioned_for": claim}
            yield {"present": present, "provision_failed_for": claim}
        for logged_in in (True, False, None):
            yield {"present": present, "credentials": {"logged_in": logged_in}}


@pytest.mark.parametrize("state", slots.STATES)
def test_a_report_only_ever_proposes_a_move_the_lifecycle_allows(state):
    for shape in _reports_for_every_shape():
        to = slots.next_state(state, CLAIM, shape)
        if to is not None:
            assert slots.can_move(state, to), (state, shape, to)


def test_a_report_never_starts_a_claim_or_a_release():
    """Taking a slot and giving one back are things people do. No report may
    stand in for either — least of all a report that could be forged by the
    very machine whose slots are at stake."""
    for shape in _reports_for_every_shape():
        assert slots.next_state(slots.FREE, None, shape) is None
        assert slots.next_state(slots.ACTIVE, CLAIM, shape) is None


def test_provisioning_finished_completes_only_the_claim_it_was_for():
    assert slots.next_state(slots.CLAIMING, CLAIM, {
        "present": True, "provisioned_for": CLAIM}) == slots.CLAIMED
    # The same slot, released and claimed again: news about the claim before
    # must not mark this one ready while it is still being set up.
    assert slots.next_state(slots.CLAIMING, CLAIM, {
        "present": True, "provisioned_for": CLAIM - 50.0}) is None


def test_provisioning_finished_needs_the_user_to_actually_be_there():
    for present in (False, None):
        assert slots.next_state(slots.CLAIMING, CLAIM, {
            "present": present, "provisioned_for": CLAIM}) is None


def test_a_claim_timestamp_survives_the_round_trip_exactly():
    """It goes down as JSON, is kept by the machine as JSON and comes back as
    JSON — which is why an exact comparison is enough."""
    import json
    import time
    claimed_at = time.time()
    echoed = json.loads(json.dumps(json.loads(json.dumps({"t": claimed_at}))))["t"]
    assert slots.next_state(slots.CLAIMING, claimed_at, {
        "present": True, "provisioned_for": echoed}) == slots.CLAIMED
    assert slots.next_state(slots.CLAIMING, claimed_at, {
        "present": True, "provisioned_for": claimed_at + 1e-3}) is None


def test_a_timestamp_that_is_not_a_number_matches_nothing():
    for reported in (str(CLAIM), [CLAIM], {"t": CLAIM}):
        assert slots.next_state(slots.CLAIMING, CLAIM, {
            "present": True, "provisioned_for": reported}) is None
        assert slots.next_state(slots.CLAIMING, CLAIM, {
            "provision_failed_for": reported}) is None


def test_a_claim_with_no_timestamp_is_completed_by_nothing():
    """claim_slot always stamps one, so this is a damaged row. It must be left
    alone rather than crash the heartbeat that happens to mention it."""
    assert slots.next_state(slots.CLAIMING, None, {
        "present": True, "provisioned_for": CLAIM}) is None
    assert slots.next_state(slots.CLAIMING, None, {
        "provision_failed_for": CLAIM}) is None


def test_a_login_report_of_the_wrong_shape_is_not_a_login():
    for credentials in ("logged in", ["logged_in"], None, 1):
        assert slots.next_state(slots.CLAIMED, CLAIM, {
            "present": True, "credentials": credentials}) is None


def test_true_is_not_a_timestamp():
    """bool is an int in Python, so without the check `True` matches a claim
    made at 1.0 — the sort of thing a buggy agent would send."""
    assert slots.next_state(slots.CLAIMING, 1.0, {
        "present": True, "provisioned_for": True}) is None
    assert slots.next_state(slots.CLAIMING, 1.0, {
        "provision_failed_for": True}) is None


def test_provisioning_that_failed_is_wiped_not_freed():
    """It may have created the account before it died, so the way out is the
    wipe — and only for this claim, not one before it."""
    assert slots.next_state(slots.CLAIMING, CLAIM, {
        "present": True, "provision_failed_for": CLAIM}) == slots.RELEASING
    assert slots.next_state(slots.CLAIMING, CLAIM, {
        "present": True, "provision_failed_for": CLAIM - 50.0}) is None


def test_a_working_login_is_what_makes_a_slot_active():
    assert slots.next_state(slots.CLAIMED, CLAIM, {
        "present": True, "credentials": {"logged_in": True}}) == slots.ACTIVE
    for logged_in in (False, None):
        assert slots.next_state(slots.CLAIMED, CLAIM, {
            "present": True, "credentials": {"logged_in": logged_in}}) is None
    # A login reported for a user the machine says is not there is nonsense.
    assert slots.next_state(slots.CLAIMED, CLAIM, {
        "present": False, "credentials": {"logged_in": True}}) is None


def test_only_the_user_being_gone_ends_a_release():
    assert slots.next_state(slots.RELEASING, None, {"present": False}) == slots.FREE
    # "Could not tell" is not "gone". A None here freeing the slot would hand
    # out whatever the wipe had not got to.
    for shape in ({}, {"present": None}, {"present": True}):
        assert slots.next_state(slots.RELEASING, None, shape) is None


def test_a_machines_report_records_what_it_saw(store):
    machine(store)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    assert store.get_slot("s1")["present"] is None, "a new slot was vouched for by nobody"

    store.apply_slot_report("m1", [report(present=True)], now=NOW + 5)
    assert store.get_slot("s1")["present"] == 1
    assert store.get_slot("s1")["reported_at"] == NOW + 5

    store.apply_slot_report("m1", [report(present=None)], now=NOW + 9)
    assert store.get_slot("s1")["present"] is None, "could-not-tell was stored as absent"


def test_a_slot_nobody_on_the_machine_has_vouched_for_is_not_handed_out(store):
    """Declared a minute ago, never reported: the records say free, the machine
    has said nothing, and the Linux user may well be sitting there."""
    machine(store)
    account(store, quota=1)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW)

    store.apply_slot_report("m1", [report(present=False)], now=NOW)
    assert store.claim_slot("a1", now=NOW)["id"] == "s1"


def test_a_free_slot_whose_user_exists_is_held_back(store):
    """Somebody created the account by hand, or a wipe went wrong after the
    fact. Either way free is not true of it, and handing it out would hand
    over whatever is in that home."""
    machine(store)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")
    store.apply_slot_report("m1", [report(present=True)], now=NOW + 1)
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW + 2)


def test_a_machine_gone_quiet_is_not_handed_a_claim(store):
    machine(store)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")  # reported at NOW
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a1", now=NOW + 3600, heard_since=NOW + 3000)
    assert store.claim_slot("a1", now=NOW + 60, heard_since=NOW - 60)["id"] == "s1"


def test_a_machine_cannot_move_another_machines_slots(store):
    """The unix user is only unique per machine. A report is matched against
    the slots on the machine that sent it, and nowhere else."""
    machine(store, "m1")
    machine(store, "m2")
    account(store, quota=1)
    declare(store, "s2", "m2", "slot01")
    store.claim_slot("a1", now=CLAIM)
    store.begin_release("s2")

    moved = store.apply_slot_report("m1", [report(present=False)], now=NOW + 9)
    assert moved == []
    row = store.get_slot("s2")
    assert row["state"] == slots.RELEASING, "m1's report freed a slot on m2"
    assert row["held_by"] == "a1"


def test_the_first_word_on_a_slot_is_the_one_taken(store):
    """A report naming one user twice can only be a broken or hostile agent.
    Taking the first means a trailing entry cannot overrule it."""
    machine(store)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")
    store.claim_slot("a1", now=CLAIM)
    store.begin_release("s1")
    store.apply_slot_report("m1", [report(present=True), report(present=False)],
                            now=NOW + 9)
    assert store.get_slot("s1")["state"] == slots.RELEASING


@pytest.mark.parametrize("reports", [None, "slot01", {"unix_user": "slot01"},
                                     [None, 3, {"unix_user": 7}]])
def test_a_report_that_is_not_a_list_of_slots_changes_nothing(store, reports):
    machine(store)
    declare(store, "s1", "m1", "slot01")
    assert store.apply_slot_report("m1", reports, now=NOW + 9) == []
    assert store.get_slot("s1")["reported_at"] == NOW


def test_one_slot_from_claim_to_wipe_to_somebody_else(store):
    """The whole loop, driven only by what a machine reports."""
    machine(store)
    account(store, "a1", quota=1)
    account(store, "a2", quota=1)
    declare(store, "s1", "m1", "slot01")

    claimed_at = store.claim_slot("a1", now=CLAIM)["claimed_at"]
    assert store.get_slot("s1")["state"] == slots.CLAIMING

    moved = store.apply_slot_report(
        "m1", [report(present=True, provisioned_for=claimed_at)], now=CLAIM + 60)
    assert moved == [{"slot": "s1", "from": slots.CLAIMING, "to": slots.CLAIMED}]

    store.apply_slot_report(
        "m1", [report(present=True, credentials={"logged_in": True})], now=CLAIM + 120)
    assert store.get_slot("s1")["state"] == slots.ACTIVE

    store.begin_release("s1")
    # The wipe has not happened yet: the user is still there.
    store.apply_slot_report("m1", [report(present=True)], now=CLAIM + 180)
    assert store.get_slot("s1")["state"] == slots.RELEASING
    assert store.held_slot_count("a1") == 1

    store.apply_slot_report("m1", [report(present=False)], now=CLAIM + 240)
    row = store.get_slot("s1")
    assert row["state"] == slots.FREE
    assert row["held_by"] is None and row["claimed_at"] is None
    assert row["released_at"] == CLAIM + 240
    assert store.held_slot_count("a1") == 0

    # And it goes to the next person clean.
    assert store.claim_slot("a2", now=CLAIM + 300)["held_by"] == "a2"


def test_news_about_an_old_claim_does_not_complete_a_new_one(store):
    machine(store)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")
    first = store.claim_slot("a1", now=CLAIM)["claimed_at"]
    store.begin_release("s1")
    store.apply_slot_report("m1", [report(present=False)], now=CLAIM + 10)

    second = store.claim_slot("a1", now=CLAIM + 20)["claimed_at"]
    assert second != first
    store.apply_slot_report("m1", [report(present=True, provisioned_for=first)],
                            now=CLAIM + 30)
    assert store.get_slot("s1")["state"] == slots.CLAIMING


def test_a_claim_that_never_finishes_is_given_up_into_a_wipe(store):
    """Provisioning stalled or the machine went quiet. The claim ends, but into
    releasing: whatever was half-made on the machine is cleared before anybody
    else is handed that slot."""
    machine(store)
    account(store, "a1", quota=2)
    declare(store, "s1", "m1", "slot01")
    declare(store, "s2", "m1", "slot02")
    store.claim_slot("a1", now=CLAIM)
    store.claim_slot("a1", now=CLAIM + 600)

    stale = store.expire_claims(older_than=CLAIM + 300)
    assert stale == ["s1"]
    assert store.get_slot("s1")["state"] == slots.RELEASING
    assert store.get_slot("s1")["held_by"] == "a1", "freed before the wipe"
    assert store.get_slot("s2")["state"] == slots.CLAIMING


def test_only_claims_time_out(store):
    machine(store)
    account(store, quota=1)
    declare(store, "s1", "m1", "slot01")
    store.claim_slot("a1", now=CLAIM)
    store.apply_slot_report("m1", [report(present=True, provisioned_for=CLAIM)],
                            now=CLAIM + 5)
    assert store.expire_claims(older_than=CLAIM + 10 ** 6) == []
    assert store.get_slot("s1")["state"] == slots.CLAIMED


# -- keeping a machine for one account ----------------------------------------------

def test_a_machine_kept_for_somebody_hands_nobody_else_its_free_slots(store):
    machine(store, capacity=2)
    declare(store, "m1-01", "m1", "slot01")
    account(store, "a1", quota=1)
    account(store, "a2", quota=1)
    store.reserve_machine("m1", "a1")
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a2", now=NOW)
    assert store.get_slot("m1-01")["state"] == slots.FREE
    assert store.claim_slot("a1", now=NOW)["id"] == "m1-01"


def test_the_account_a_machine_is_kept_for_is_given_it_first(store):
    """Ahead of an open machine that sorts earlier: what they were promised is
    what they get, and the open machine stays for everybody else."""
    machine(store, "a-open", capacity=1)
    declare(store, "a-open-01", "a-open", "slot01")
    machine(store, "z-kept", capacity=1)
    declare(store, "z-kept-01", "z-kept", "slot01")
    account(store, "a1", quota=1)
    account(store, "a2", quota=1)
    store.reserve_machine("z-kept", "a1")
    assert store.claim_slot("a1", now=NOW)["id"] == "z-kept-01"
    assert store.claim_slot("a2", now=NOW)["id"] == "a-open-01"


def test_a_machine_kept_for_nobody_is_claimed_as_before(store):
    """Keeping one machine changes nothing about the others."""
    machine(store, "m1", capacity=1)
    declare(store, "m1-01", "m1", "slot01")
    machine(store, "m2", capacity=1)
    declare(store, "m2-01", "m2", "slot01")
    account(store, "a1", quota=1)
    account(store, "a2", quota=2)
    store.reserve_machine("m1", "a1")
    assert store.claim_slot("a2", now=NOW)["id"] == "m2-01"
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a2", now=NOW)


def test_a_kept_machine_does_not_lift_the_allowance(store):
    machine(store, capacity=2)
    declare(store, "m1-01", "m1", "slot01")
    account(store, "a1", quota=0)
    store.reserve_machine("m1", "a1")
    with pytest.raises(QuotaExceeded):
        store.claim_slot("a1", now=NOW)


def test_asking_for_a_kept_machine_by_name_is_refused_to_others(store):
    machine(store, capacity=1)
    declare(store, "m1-01", "m1", "slot01")
    account(store, "a1", quota=1)
    account(store, "a2", quota=1)
    store.reserve_machine("m1", "a1")
    with pytest.raises(NoSlotAvailable):
        store.claim_slot("a2", now=NOW, node_id="m1")


def test_keeping_a_machine_takes_back_nothing_already_held(store):
    """A reservation is about the next claim. Taking a held slot back is a
    release, with the wipe it implies, and never a side effect of this."""
    machine(store, capacity=1)
    declare(store, "m1-01", "m1", "slot01")
    account(store, "a1", quota=1)
    account(store, "a2", quota=1)
    store.claim_slot("a2", now=NOW)
    store.reserve_machine("m1", "a1")
    slot = store.get_slot("m1-01")
    assert slot["held_by"] == "a2" and slot["state"] == slots.CLAIMING


def test_opening_a_kept_machine_again_lets_anybody_claim_it(store):
    machine(store, capacity=1)
    declare(store, "m1-01", "m1", "slot01")
    account(store, "a1", quota=1)
    account(store, "a2", quota=1)
    store.reserve_machine("m1", "a1")
    store.reserve_machine("m1", None)
    assert store.get_node("m1")["reserved_for"] is None
    assert store.claim_slot("a2", now=NOW)["id"] == "m1-01"


@pytest.mark.parametrize("capacity, with_slot", [(2, False), (1, True)])
def test_a_shared_machine_can_be_kept(store, capacity, with_slot):
    """A machine is what the console lists as one: capacity for more than one
    slot, or a slot already declared on it."""
    machine(store, capacity=capacity)
    if with_slot:
        declare(store, "m1-01", "m1", "slot01")
    account(store, "a1")
    store.reserve_machine("m1", "a1")
    assert store.get_node("m1")["reserved_for"] == "a1"


def test_an_owner_node_cannot_be_kept_for_anybody(store):
    """Somebody's own node has no slots to hand out, so keeping it for somebody
    is a mistake the operator should hear about, not a silent no-op."""
    store.add_node("laptop", "erik", now=NOW)
    account(store, "a1")
    with pytest.raises(StoreError, match="not a shared machine"):
        store.reserve_machine("laptop", "a1")
    assert store.get_node("laptop")["reserved_for"] is None


def test_a_machine_cannot_be_kept_for_an_account_that_does_not_exist(store):
    machine(store, capacity=2)
    with pytest.raises(StoreError, match="no account"):
        store.reserve_machine("m1", "nobody")
    assert store.get_node("m1")["reserved_for"] is None


def test_keeping_a_machine_that_does_not_exist_says_so(store):
    account(store, "a1")
    for keep_for in ("a1", None):
        with pytest.raises(StoreError, match="no machine"):
            store.reserve_machine("nowhere", keep_for)


class _PauseBefore:
    """Holds one connection just before it runs a statement."""

    def __init__(self, conn, gate, marker):
        self._conn = conn
        self._gate = gate
        self._marker = marker
        self.reached = threading.Event()

    def execute(self, sql, *args):
        if self._marker in sql and not self.reached.is_set():
            self.reached.set()
            self._gate.wait(20)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_machine_kept_while_a_claim_waits_for_the_lock_is_not_handed_out(tmp_path):
    """The claim decides inside its own write transaction. "That machine is
    open", read before it, can be stale by the time the slot is taken — and the
    slot goes to somebody the machine is no longer for."""
    db = str(tmp_path / "fleet.db")
    setup = Store(db, max_slots_per_machine=8)
    try:
        machine(setup, capacity=1)
        declare(setup, "s1", "m1", "slot01")
        account(setup, "a1", quota=1)
        account(setup, "a2", quota=1)
    finally:
        setup.close()

    gate = threading.Event()
    outcome: dict[str, str] = {}
    claimer = Store(db, max_slots_per_machine=8)
    claimer._conn = _PauseBefore(claimer._conn, gate, "BEGIN IMMEDIATE")

    def claim():
        try:
            outcome["a2"] = claimer.claim_slot("a2", now=NOW)["id"]
        except NoSlotAvailable:
            outcome["a2"] = "refused"

    thread = threading.Thread(target=claim)
    thread.start()
    try:
        assert claimer._conn.reached.wait(10), "the claim never reached its transaction"
        operator = Store(db, max_slots_per_machine=8)
        try:
            operator.reserve_machine("m1", "a1")
        finally:
            operator.close()
    finally:
        gate.set()
        thread.join(timeout=30)
        claimer.close()
    assert outcome == {"a2": "refused"}


def test_a_database_from_before_reservations_opens_with_every_machine_open(tmp_path):
    """The nodes table as it was before this column: the column is added on
    open, every machine reads as kept for nobody, and claims work as before."""
    import sqlite3
    path = str(tmp_path / "before.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE nodes (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT '', token_hash TEXT NOT NULL UNIQUE,
            pinned_version TEXT NOT NULL DEFAULT '', rc_expected INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
            device_token_at REAL NOT NULL DEFAULT 0,
            capacity INTEGER NOT NULL DEFAULT 1, tier TEXT NOT NULL DEFAULT 'dedicated');
        INSERT INTO nodes (id, owner, token_hash, created_at, capacity)
        VALUES ('m1', 'op', 'deadbeef', 1.0, 2);
    """)
    con.commit()
    con.close()

    st = Store(path)
    try:
        assert st.get_node("m1")["reserved_for"] is None
        declare(st, "m1-01", "m1", "slot01")
        account(st, "a2", quota=1)
        assert st.claim_slot("a2", now=NOW)["id"] == "m1-01"
    finally:
        st.close()
