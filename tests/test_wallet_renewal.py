"""Manual renewal stays tied to one owner and rental; it never changes access."""
import sqlite3
from datetime import datetime, timezone

import pytest

from ccfleetd import offers, slots
from ccfleetd.store import (
    InsufficientWalletCredit,
    NotYours,
    StoreError,
    WalletQuoteChanged,
    WalletReplayMismatch,
)
from tests.test_wallet_store import NOW, buy, grant, machine, snapshot
from tests.test_wallet_store import store as store


def deliver(store, slot_id, at=NOW):
    current = store.get_slot(slot_id)
    store.apply_slot_report(current["node_id"], [{"unix_user": current["unix_user"],
        "present": True, "provisioned_for": current["claimed_at"],
        "credentials": {"logged_in": False}}], now=at)
    store.insert_heartbeat(current["node_id"], at, {"node_id": current["node_id"], "mode": "machine"})


def renew(store, slot_id, claim_id, *, key="renew-1", now=NOW, cents=1000, account="alice"):
    return store.renew_paid_slot(account, slot_id, claim_id=claim_id, expected_cents=cents,
                                 idempotency_key=key, now=now, max_age_s=90)


def purchased(store, amount="30"):
    slot_id = machine(store)
    grant(store, amount=amount)
    first = buy(store)
    deliver(store, slot_id)
    return slot_id, first


def test_early_manual_renewal_extends_from_paid_end_and_replays_once(store):
    slot_id, first = purchased(store)
    old_slot = store.get_slot(slot_id)
    order = renew(store, slot_id, first["claim_id"], now=NOW + 10)
    assert order["operation"] == "renewal" and order["period_start_at"] == first["period_end_at"]
    assert order["period_end_at"] == datetime(2024, 1, 14, 22, 13, 20, tzinfo=timezone.utc).timestamp()
    assert store.paid_slot_through("alice", slot_id, claim_id=first["claim_id"]) == order["period_end_at"]
    assert renew(store, slot_id, first["claim_id"], now=NOW + 20) == order
    assert store.wallet_balance("alice") == 1000
    assert len(store.list_slot_purchases("alice")) == 2
    assert store.get_slot(slot_id) == old_slot
    assert store.get_account("alice")["slot_quota"] == 0


def test_expired_renewal_starts_now_without_charging_the_unpaid_gap(store):
    slot_id, first = purchased(store)
    later = first["period_end_at"] + 3600
    deliver(store, slot_id, at=later)
    order = renew(store, slot_id, first["claim_id"], now=later)
    assert order["period_start_at"] == later
    assert order["period_end_at"] == datetime(2024, 1, 14, 23, 13, 20, tzinfo=timezone.utc).timestamp()
    assert store.wallet_balance("alice") == 1000
    assert store.get_slot(slot_id)["state"] == slots.CLAIMED


def test_zero_credit_does_not_change_an_already_paid_slot_or_period(store):
    slot_id, first = purchased(store, amount="10")
    before = snapshot(store)
    assert store.wallet_balance("alice") == 0
    assert store.paid_slot_through("alice", slot_id) == first["period_end_at"]
    assert store.get_slot(slot_id)["state"] == slots.CLAIMED
    with pytest.raises(InsufficientWalletCredit):
        renew(store, slot_id, first["claim_id"])
    assert snapshot(store) == before


def test_renewal_uses_current_server_price_and_preserves_first_quote(store):
    slot_id, first = purchased(store)
    store.set_node_offer("node-a", hosting=offers.Cost("9", "USD"), ip=offers.Cost("0", "USD"))
    before = snapshot(store)
    with pytest.raises(WalletQuoteChanged):
        renew(store, slot_id, first["claim_id"])
    assert snapshot(store) == before
    order = renew(store, slot_id, first["claim_id"], cents=1100)
    assert order["amount_cents"] == 1100
    assert store.get_slot_purchase("alice", first["id"])["amount_cents"] == 1000


def test_legacy_claim_cannot_be_silently_converted_by_renewal(store):
    slot_id = machine(store)
    store.set_slot_quota("alice", 1)
    old = store.claim_slot("alice", now=NOW)
    deliver(store, slot_id)
    grant(store)
    before = snapshot(store)
    assert store.paid_slot_through("alice", slot_id) is None
    with pytest.raises(StoreError, match="legacy"):
        renew(store, slot_id, old["gateway_claim_id"])
    assert snapshot(store) == before


@pytest.mark.parametrize("state", [slots.CLAIMING, slots.RELEASING])
def test_undelivered_or_releasing_claim_cannot_be_renewed(store, state):
    slot_id, first = purchased(store)
    store._conn.execute("UPDATE slots SET state=? WHERE id=?", (state, slot_id))
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(StoreError, match="delivered"):
        renew(store, slot_id, first["claim_id"])
    assert snapshot(store) == before


def test_different_holder_and_reclaimed_rental_have_no_inherited_paid_period(store):
    slot_id, first = purchased(store)
    store.begin_release(slot_id, held_by="alice")
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=NOW + 1)
    store.set_slot_quota("bob", 1)
    later = store.claim_slot("bob", node_id="node-a", now=NOW + 2)
    deliver(store, slot_id, at=NOW + 2)
    grant(store, "bob", key="grant-bob")
    before = snapshot(store)
    with pytest.raises(NotYours):
        renew(store, slot_id, first["claim_id"])
    with pytest.raises(NotYours):
        store.paid_slot_through("alice", slot_id, claim_id=first["claim_id"])
    assert store.paid_slot_through("bob", slot_id, claim_id=later["gateway_claim_id"]) is None
    with pytest.raises(StoreError, match="legacy"):
        renew(store, slot_id, later["gateway_claim_id"], account="bob", now=NOW + 2)
    assert snapshot(store) == before


def test_old_tab_or_renewal_key_cannot_renew_a_new_claim_by_the_same_account(store):
    slot_id, first = purchased(store, amount="60")
    renew(store, slot_id, first["claim_id"])
    store.begin_release(slot_id, held_by="alice")
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=NOW + 1)
    later = buy(store, key="purchase-2", now=NOW + 2)
    deliver(store, slot_id, at=NOW + 2)
    before = snapshot(store)
    with pytest.raises(NotYours):
        renew(store, slot_id, first["claim_id"], key="old-tab", now=NOW + 2)
    with pytest.raises(WalletReplayMismatch):
        renew(store, slot_id, later["claim_id"], now=NOW + 2)
    with pytest.raises(NotYours):
        store.paid_slot_through("alice", slot_id, claim_id=first["claim_id"])
    assert store.paid_slot_through("alice", slot_id) == later["period_end_at"]
    assert snapshot(store) == before


@pytest.mark.parametrize("fault", ["unpriced", "non-USD", "disabled", "absent", "stale"])
def test_renewal_rechecks_current_machine_and_quote_without_spending_on_refusal(store, fault):
    slot_id, first = purchased(store)
    if fault == "unpriced":
        store.clear_node_offer("node-a")
    elif fault == "non-USD":
        store.set_node_offer("node-a", hosting=offers.Cost("8", "EUR"), ip=offers.Cost("0", "EUR"))
    elif fault == "disabled":
        store.set_enabled("node-a", False)
    elif fault == "absent":
        store._conn.execute("UPDATE slots SET present=0 WHERE id=?", (slot_id,))
    else:
        store._conn.execute("UPDATE slots SET reported_at=? WHERE id=?", (NOW - 91, slot_id))
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(StoreError):
        renew(store, slot_id, first["claim_id"])
    assert snapshot(store) == before


@pytest.mark.parametrize("key", ["grant-1", "purchase-1"])
def test_renewal_cannot_reuse_a_grant_or_initial_purchase_identity(store, key):
    slot_id, first = purchased(store)
    before = snapshot(store)
    with pytest.raises(WalletReplayMismatch):
        renew(store, slot_id, first["claim_id"], key=key)
    assert snapshot(store) == before


def test_failed_renewal_order_rolls_back_credit_and_paid_through(store):
    slot_id, first = purchased(store)
    store._conn.execute("CREATE TRIGGER reject_renew BEFORE INSERT ON slot_purchases "
        "WHEN NEW.operation='renewal' BEGIN SELECT RAISE(ABORT, 'synthetic renewal failure'); END")
    store._conn.commit()
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic renewal failure"):
        renew(store, slot_id, first["claim_id"])
    assert store.paid_slot_through("alice", slot_id) == first["period_end_at"]
    assert snapshot(store) == before
