"""Separate SQLite connections cannot double-spend or leave a charged failed claim."""
from __future__ import annotations

import sqlite3
import threading

from ccfleetd import slots
from ccfleetd.store import InsufficientWalletCredit, NoSlotAvailable, Store
from tests.test_wallet_renewal import deliver, renew
from tests.test_wallet_store import NOW, accounts, buy, grant, machine


def database(tmp_path, *, credit="10", bob=False):
    path = str(tmp_path / "fleet.db")
    setup = Store(path)
    accounts(setup)
    machine(setup)
    machine(setup, "node-b")
    if credit is not None:
        grant(setup, amount=credit)
    if bob:
        grant(setup, "bob", amount="10", key="grant-bob")
    setup.close()
    return path


def race(left, right):
    start = threading.Barrier(3)
    results, errors = [], []

    def run(call):
        start.wait(timeout=5)
        try:
            results.append(call())
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run, args=(call,)) for call in (left, right)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return results, errors


def test_two_connections_cannot_spend_the_same_last_credit(tmp_path):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    try:
        orders, errors = race(lambda: buy(a, key="purchase-a"),
                             lambda: buy(b, node="node-b", key="purchase-b"))
        assert len(orders) == len(errors) == 1
        assert isinstance(errors[0], InsufficientWalletCredit)
        assert a.wallet_balance("alice") == 0
        assert len(a.list_slot_purchases("alice")) == 1
        assert len(a.list_wallet_entries("alice")) == 2
        assert sorted(slot["state"] for slot in a.list_slots()) == [slots.CLAIMING, slots.FREE]
    finally:
        a.close()
        b.close()


def test_two_accounts_cannot_buy_the_same_slot_through_different_connections(tmp_path):
    path = database(tmp_path, bob=True)
    a, b = Store(path), Store(path)
    try:
        orders, errors = race(lambda: buy(a, key="purchase-alice"),
                             lambda: buy(b, account="bob", key="purchase-bob"))
        assert len(orders) == len(errors) == 1
        assert isinstance(errors[0], NoSlotAvailable)
        assert sorted(a.wallet_balance(who) for who in ("alice", "bob")) == [0, 1000]
        assert a.get_slot(orders[0]["slot_id"])["held_by"] == orders[0]["account_id"]
    finally:
        a.close()
        b.close()


def test_duplicate_admin_grant_on_two_connections_credits_once(tmp_path):
    path = database(tmp_path, credit=None)
    a, b = Store(path), Store(path)
    try:
        entries, errors = race(lambda: grant(a), lambda: grant(b))
        assert not errors and len(entries) == 2 and entries[0] == entries[1]
        assert a.wallet_balance("alice") == 2000
        assert len(a.list_wallet_entries("alice")) == 1
    finally:
        a.close()
        b.close()


def test_failed_purchase_releases_write_lock_without_committing_claim_or_debit(tmp_path):
    path = database(tmp_path)
    a, b = Store(path), Store(path)
    entered, release, attempted, second_done = (threading.Event() for _ in range(4))
    errors, orders = [], []
    try:
        a._conn.execute("CREATE TRIGGER reject_first_order BEFORE INSERT ON slot_purchases "
            "WHEN NEW.node_id='node-a' BEGIN SELECT RAISE(ABORT, 'synthetic first failure'); END")
        a._conn.commit()
        original_claim = a._claim_slot_in_transaction

        def held_claim(*args, **kwargs):
            slot_id = original_claim(*args, **kwargs)
            entered.set()
            assert release.wait(timeout=5)
            return slot_id

        a._claim_slot_in_transaction = held_claim
        real_connection = b._conn

        class Connection:
            def execute(self, statement, parameters=()):
                if statement == "BEGIN IMMEDIATE":
                    attempted.set()
                return real_connection.execute(statement, parameters)

            def __getattr__(self, name):
                return getattr(real_connection, name)

        b._conn = Connection()

        def first():
            try:
                buy(a, key="first-failing-purchase")
            except Exception as error:
                errors.append(error)

        def second():
            try:
                orders.append(buy(b, node="node-b", key="second-purchase"))
            except Exception as error:
                errors.append(error)
            finally:
                second_done.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        assert entered.wait(timeout=5)
        second_thread.start()
        assert attempted.wait(timeout=5)
        assert not second_done.wait(timeout=0.1), "the second writer bypassed the write transaction"
        release.set()
        for thread in (first_thread, second_thread):
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert len(errors) == len(orders) == 1
        assert isinstance(errors[0], sqlite3.IntegrityError)
        assert a.get_slot("slot-node-a")["state"] == slots.FREE
        assert a.get_slot("slot-node-b")["state"] == slots.CLAIMING
        assert a.wallet_balance("alice") == 0
        assert len(a.list_wallet_entries("alice")) == 2
        assert len(a.list_slot_purchases("alice")) == 1
    finally:
        release.set()
        a.close()
        b.close()


def test_parallel_distinct_renewals_append_nonoverlapping_calendar_periods(tmp_path):
    path = database(tmp_path, credit="30")
    a, b = Store(path), Store(path)
    try:
        first = buy(a)
        deliver(a, first["slot_id"])
        original_slot = a.get_slot(first["slot_id"])
        orders, errors = race(
            lambda: renew(a, first["slot_id"], first["claim_id"], key="renew-a"),
            lambda: renew(b, first["slot_id"], first["claim_id"], key="renew-b"))
        assert not errors and len(orders) == 2
        early, later = sorted(orders, key=lambda order: order["period_start_at"])
        assert early["period_start_at"] == first["period_end_at"]
        assert later["period_start_at"] == early["period_end_at"]
        assert a.paid_slot_through("alice", first["slot_id"]) == later["period_end_at"]
        assert a.list_slot_purchases("alice")[0]["id"] == later["id"]
        assert a.wallet_balance("alice") == 0
        assert a.get_slot(first["slot_id"]) == original_slot
    finally:
        a.close()
        b.close()


def test_parallel_matching_renewal_replays_debit_and_extend_once(tmp_path):
    path = database(tmp_path, credit="30")
    a, b = Store(path), Store(path)
    try:
        first = buy(a)
        deliver(a, first["slot_id"])
        orders, errors = race(lambda: renew(a, first["slot_id"], first["claim_id"]),
                             lambda: renew(b, first["slot_id"], first["claim_id"]))
        assert not errors and len(orders) == 2 and orders[0] == orders[1]
        assert a.wallet_balance("alice") == 1000
        assert len(a.list_slot_purchases("alice")) == 2
    finally:
        a.close()
        b.close()


def test_two_connections_cannot_spend_the_last_renewal_credit_twice(tmp_path):
    path = database(tmp_path, credit="20")
    a, b = Store(path), Store(path)
    try:
        first = buy(a)
        deliver(a, first["slot_id"])
        orders, errors = race(
            lambda: renew(a, first["slot_id"], first["claim_id"], key="renew-a"),
            lambda: renew(b, first["slot_id"], first["claim_id"], key="renew-b"))
        assert len(orders) == len(errors) == 1
        assert isinstance(errors[0], InsufficientWalletCredit)
        assert a.wallet_balance("alice") == 0
        assert len(a.list_slot_purchases("alice")) == 2
        assert a.paid_slot_through("alice", first["slot_id"]) == orders[0]["period_end_at"]
    finally:
        a.close()
        b.close()


def test_matching_purchase_replay_survives_Store_restart(tmp_path):
    path = database(tmp_path, credit="20")
    a = Store(path)
    first = buy(a)
    original_slot = a.get_slot(first["slot_id"])
    a.close()
    b = Store(path)
    try:
        assert buy(b, now=NOW + 1000) == first
        assert b.wallet_balance("alice") == 1000
        assert b.get_slot(first["slot_id"]) == original_slot
    finally:
        b.close()
