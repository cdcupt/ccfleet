"""Offline UTC-day reported usage controls; no provider/runtime/production DB."""

import dataclasses
import socket
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ccfleetd import devin_trial as t

ALICE = t.VerifiedAccount("alice", True)
BOB = t.VerifiedAccount("bob", True)
DAY = 20_000 * 86400


@pytest.fixture(autouse=True)
def no_runtime_or_network(monkeypatch):
    def blocked(*a, **kw):
        pytest.fail("Offline ledger cannot run a provider, worker or network")

    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def scene():
    now = [float(DAY + 100)]
    db = sqlite3.connect(":memory:")
    ledger = t.TrialLedger(
        db, enabled=True, clock=lambda: now[0], daily_token_limit=100, global_daily_token_limit=150
    )
    yield ledger, db, now
    db.close()


def enter(ledger, who=ALICE):
    result = ledger.admit(who)
    assert result.code == "admitted"
    return result.lease


def begin(ledger, lease, who=ALICE, seconds=1):
    result = ledger.start_step(who, lease, request=b"private synthetic prompt", seconds=seconds)
    assert result.code == "step_reserved"
    return result.step


def finish(ledger, lease, permit, amount, who=ALICE):
    return ledger.finish_step(
        who, lease, permit, cleanup_confirmed=True, usage=t.VerifiedUsage(amount, complete=True)
    )


def test_visible_allowance_has_utc_reset_and_never_claims_hard_generation_stop(scene):
    ledger, _, _ = scene
    value = ledger.allowance(ALICE)
    assert (
        value.available_tokens == value.token_limit == 100
        and value.reported_tokens == value.reserved_tokens == 0
    )
    assert value.resets_at == DAY + 86400 and value.global_available_tokens == 150
    assert value.hard_generation_stop is False and value.enabled is True and value.paused is False


def test_whole_remaining_reservation_is_atomic_before_any_external_call_and_settlement_is_actual(
    scene,
):
    ledger, db, _ = scene
    lease = enter(ledger)
    permit = begin(ledger, lease)
    assert permit.reserved_tokens == 100 and ledger.allowance(ALICE).available_tokens == 0
    assert (
        ledger.allowance(ALICE).reported_tokens == 0
        and ledger.allowance(BOB).global_available_tokens == 50
    )
    assert ledger.admit(BOB).code == "trial_busy"
    assert finish(ledger, lease, permit, 60).code == "step_finished"
    assert (
        ledger.allowance(ALICE).available_tokens == 40
        and ledger.allowance(ALICE).reported_tokens == 60
    )
    assert (
        ledger.finish_step(
            ALICE, lease, permit, cleanup_confirmed=True, usage=t.VerifiedUsage(60, True)
        ).code
        == "invalid_step_receipt"
    )
    assert ledger.allowance(ALICE).reported_tokens == 60
    assert "private synthetic prompt" not in repr(
        db.execute("SELECT * FROM devin_trial_sessions").fetchall()
    )


def test_remaining_budget_follows_account_across_closed_leases_and_global_budget_across_users(
    scene,
):
    ledger, _, _ = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    finish(ledger, lease, step, 60)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    second = enter(ledger)
    step = begin(ledger, second)
    assert step.reserved_tokens == 40
    finish(ledger, second, step, 40)
    ledger.close(ALICE, second, cleanup_confirmed=True)
    assert ledger.admit(ALICE).code == "daily_limit_reached"
    other = enter(ledger, BOB)
    permit = begin(ledger, other, BOB)
    assert permit.reserved_tokens == 50
    finish(ledger, other, permit, 50, BOB)
    ledger.close(BOB, other, cleanup_confirmed=True)
    assert ledger.admit(t.VerifiedAccount("carol", True)).code == "global_daily_limit_reached"


def test_full_reported_overshoot_is_charged_and_blocks_next_work_without_truncating_usage(scene):
    ledger, _, _ = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    assert finish(ledger, lease, step, 125).code == "step_finished_over_allowance"
    assert (
        ledger.allowance(ALICE).reported_tokens == 125
        and ledger.allowance(ALICE).available_tokens == 0
    )
    assert ledger.allowance(BOB).global_available_tokens == 25
    assert ledger.start_step(ALICE, lease, request=b"{}").code == "trial_closed"
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(ALICE).code == "daily_limit_reached"


@pytest.mark.parametrize(
    "usage",
    [
        None,
        0,
        {},
        False,
        t.VerifiedUsage(0),
        t.VerifiedUsage(-1, True),
        t.VerifiedUsage(True, True),
        t.VerifiedUsage(10**12 + 1, True),
    ],
)
def test_unknown_incomplete_or_untrusted_usage_never_becomes_zero_and_keeps_reservation(
    scene, usage
):
    ledger, _, _ = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    assert (
        ledger.finish_step(ALICE, lease, step, cleanup_confirmed=True, usage=usage).code
        == "usage_unaccounted"
    )
    value = ledger.allowance(ALICE)
    assert value.reported_tokens == 0 and value.reserved_tokens == 100 and value.paused is True
    assert ledger.admit(BOB).code == "trial_paused"
    assert ledger.close(ALICE, lease, cleanup_confirmed=True).code == "step_cleanup_required"
    assert finish(ledger, lease, step, 7).code == "step_finished"
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.allowance(ALICE).reported_tokens == 7 and ledger.admit(BOB).code == "trial_paused"


def test_cleanup_failure_cannot_release_budget_even_with_a_reported_total(scene):
    ledger, _, _ = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    assert (
        ledger.finish_step(
            ALICE, lease, step, cleanup_confirmed=False, usage=t.VerifiedUsage(10, True)
        ).code
        == "cleanup_unconfirmed"
    )
    assert (
        ledger.allowance(ALICE).reserved_tokens == 100
        and ledger.allowance(ALICE).reported_tokens == 0
    )
    assert ledger.admit(BOB).code == "trial_paused"


def test_measured_zero_is_different_from_missing_usage(scene):
    ledger, _, _ = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    assert finish(ledger, lease, step, 0).code == "step_finished"
    assert (
        ledger.allowance(ALICE).available_tokens == 100
        and ledger.allowance(ALICE).reserved_tokens == 0
    )


def test_daily_utc_rollover_preserves_old_counters_and_grants_only_new_days_allowance(scene):
    ledger, db, now = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    finish(ledger, lease, step, 100)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(ALICE).code == "daily_limit_reached"
    old_day = ledger.allowance(ALICE).utc_day
    now[0] = float(DAY + 86400)
    value = ledger.allowance(ALICE)
    assert value.utc_day != old_day and value.available_tokens == 100 and value.reported_tokens == 0
    assert db.execute(
        "SELECT used_tokens FROM devin_trial_account_days WHERE account_id=? ORDER BY utc_day",
        ("alice",),
    ).fetchall() == [(100,), (0,)]
    assert ledger.admit(ALICE).code == "admitted"


def test_day_rollover_does_not_erase_unresolved_worker_or_pause(scene):
    ledger, db, now = scene
    now[0] = float(DAY + 86400 - 2)
    lease = enter(ledger)
    step = begin(ledger, lease, seconds=2)
    assert lease.expires_at == DAY + 86400
    now[0] = float(DAY + 86400)
    assert (
        not ledger.authorize_step(ALICE, lease, step) and ledger.admit(BOB).code == "trial_paused"
    )
    finish(ledger, lease, step, 125)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert (
        db.execute(
            "SELECT used_tokens FROM devin_trial_account_days WHERE account_id=? AND utc_day=?",
            ("alice", DAY // 86400),
        ).fetchone()[0]
        == 125
    )
    today = ledger.allowance(ALICE)
    assert (
        today.reported_tokens == today.reserved_tokens == 0
        and today.available_tokens == 100
        and today.paused is True
    )
    assert db.execute(
        "SELECT used_tokens,reserved_tokens FROM devin_trial_global_days WHERE utc_day=?",
        (DAY // 86400 + 1,),
    ).fetchone() == (0, 0)
    assert ledger.admit(BOB).code == "trial_paused"


def test_configured_allowance_cannot_be_changed_by_reopening_ledger(scene):
    _, db, now = scene
    with pytest.raises(t.TrialError, match="trial_limits_changed"):
        t.TrialLedger(
            db,
            enabled=True,
            clock=lambda: now[0],
            daily_token_limit=101,
            global_daily_token_limit=150,
        )
    assert db.execute("SELECT count(*) FROM devin_trial_sessions").fetchone()[0] == 0


def test_same_day_limits_survive_process_reopen(tmp_path):
    path = tmp_path / "daily.sqlite3"
    first = sqlite3.connect(path)
    ledger = t.TrialLedger(
        first,
        enabled=True,
        clock=lambda: float(DAY),
        daily_token_limit=10,
        global_daily_token_limit=20,
    )
    lease = enter(ledger)
    step = begin(ledger, lease)
    finish(ledger, lease, step, 11)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    first.close()
    second = sqlite3.connect(path)
    try:
        reopened = t.TrialLedger(
            second,
            enabled=True,
            clock=lambda: float(DAY + 1),
            daily_token_limit=10,
            global_daily_token_limit=20,
        )
        assert (
            reopened.allowance(ALICE).reported_tokens == 11
            and reopened.admit(ALICE).code == "daily_limit_reached"
        )
    finally:
        second.close()


def test_daily_requests_and_admission_caps_are_not_refunded_by_zero_usage(scene):
    ledger, _, _ = scene
    for _ in range(4):
        lease = enter(ledger)
        for _ in range(5):
            finish(ledger, lease, begin(ledger, lease), 0)
        ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert (
        ledger.allowance(ALICE).remaining_requests == 0
        and ledger.admit(ALICE).code == "daily_limit_reached"
    )
    # The hard global turn cap is now also twenty. Other accounts cannot
    # bypass it after this user's twenty requests, even with measured zero.
    assert ledger.admit(BOB).code == "global_daily_limit_reached"


def test_runtime_reservations_accumulate_across_leases(scene):
    ledger, _, _ = scene
    for _ in range(t.DAILY_RUNTIME_S // 60):
        lease = enter(ledger)
        step = begin(ledger, lease, seconds=60)
        finish(ledger, lease, step, 0)
        ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(ALICE).code == "daily_limit_reached"


def test_forged_token_reservation_cannot_settle_or_authorize(scene):
    ledger, _, _ = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    for replacement in (0, 99, True):
        fake = dataclasses.replace(step, reserved_tokens=replacement)
        assert not ledger.authorize_step(ALICE, lease, fake)
        assert finish(ledger, lease, fake, 1).code == "invalid_step_receipt"
    assert ledger.allowance(ALICE).reserved_tokens == 100


def test_old_one_time_or_product_store_database_is_not_silently_migrated():
    for tables in (("devin_trial_control", "devin_trial_sessions"), ("wallet_entries", "users")):
        db = sqlite3.connect(":memory:")
        try:
            for name in tables:
                db.execute("CREATE TABLE " + name + "(id INTEGER)")
            before = db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            with pytest.raises(t.TrialError, match="dedicated_trial_database_required"):
                t.TrialLedger(db)
            assert (
                db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                == before
            )
        finally:
            db.close()


def test_two_connections_cannot_double_reserve_same_remaining_daily_budget(tmp_path):
    path = tmp_path / "daily.sqlite3"
    db = sqlite3.connect(path)
    ledger = t.TrialLedger(db, enabled=True, clock=lambda: float(DAY))
    lease = enter(ledger)
    db.close()
    barrier = Barrier(2)

    def contender(_):
        connection = sqlite3.connect(path)
        try:
            current = t.TrialLedger(connection, enabled=True, clock=lambda: float(DAY))
            barrier.wait(timeout=5)
            return current.start_step(ALICE, lease, request=b"{}", seconds=1).code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(contender, range(2)))
    assert sorted(results) == ["step_busy", "step_reserved"]
    db = sqlite3.connect(path)
    try:
        assert (
            db.execute("SELECT reserved_tokens FROM devin_trial_account_days").fetchone()[0]
            == t.TURN_RESERVATION_TOKENS
        )
    finally:
        db.close()


def test_actual_offline_product_store_is_refused_without_any_schema_or_row_change(store):
    before = list(store._conn.iterdump())
    with pytest.raises(t.TrialError, match="dedicated_trial_database_required"):
        t.TrialLedger(store._conn)
    assert list(store._conn.iterdump()) == before


def test_global_overshoot_is_accounted_and_blocks_other_users(scene):
    ledger, _, _ = scene
    lease = enter(ledger)
    step = begin(ledger, lease)
    assert finish(ledger, lease, step, 200).code == "step_finished_over_allowance"
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.allowance(BOB).global_available_tokens == 0
    assert ledger.admit(BOB).code == "global_daily_limit_reached"


def test_global_daily_request_cap_is_shared_across_accounts():
    db = sqlite3.connect(":memory:")
    ledger = t.TrialLedger(db, enabled=True, clock=lambda: float(DAY))
    try:
        for number in range(t.GLOBAL_DAILY_REQUESTS):
            user = t.VerifiedAccount("user" + str(number), True)
            lease = enter(ledger, user)
            finish(ledger, lease, begin(ledger, lease, user), 0, user)
            ledger.close(user, lease, cleanup_confirmed=True)
        assert ledger.admit(t.VerifiedAccount("next", True)).code == "global_daily_limit_reached"
    finally:
        db.close()


def test_global_runtime_cap_is_charged_before_execution_and_not_refunded():
    db = sqlite3.connect(":memory:")
    ledger = t.TrialLedger(db, enabled=True, clock=lambda: float(DAY))
    try:
        for number in range(t.GLOBAL_DAILY_RUNTIME_S // 120):
            user = t.VerifiedAccount("user" + str(number), True)
            lease = enter(ledger, user)
            for _ in range(2):
                finish(ledger, lease, begin(ledger, lease, user, seconds=60), 0, user)
            ledger.close(user, lease, cleanup_confirmed=True)
        assert ledger.admit(t.VerifiedAccount("next", True)).code == "global_daily_limit_reached"
    finally:
        db.close()


def test_boolean_deadline_cannot_forge_the_numeric_one_second_deadline():
    db = sqlite3.connect(":memory:")
    try:
        ledger = t.TrialLedger(db, enabled=True, clock=lambda: 0)
        lease = enter(ledger)
        permit = begin(ledger, lease, seconds=1)
        assert permit.deadline == 1.0
        forged = dataclasses.replace(permit, deadline=True)
        assert not ledger.authorize_step(ALICE, lease, forged)
        assert finish(ledger, lease, forged, 0).code == "invalid_step_receipt"
    finally:
        db.close()


def test_racing_first_initializers_cannot_use_different_limits_than_winner(tmp_path):
    path = tmp_path / "first-initializers.sqlite3"
    barrier = Barrier(2)

    def contender(limit):
        connection = sqlite3.connect(path)
        barrier.wait(timeout=5)
        try:
            t.TrialLedger(
                connection,
                enabled=True,
                clock=lambda: float(DAY),
                daily_token_limit=limit,
                global_daily_token_limit=100,
            )
            return "ready", limit
        except t.TrialError as error:
            return str(error), limit
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(contender, (10, 20)))
    assert sorted(code for code, _ in outcomes) == ["ready", "trial_limits_changed"]
    winner = next(limit for code, limit in outcomes if code == "ready")
    connection = sqlite3.connect(path)
    try:
        ledger = t.TrialLedger(
            connection,
            enabled=True,
            clock=lambda: float(DAY),
            daily_token_limit=winner,
            global_daily_token_limit=100,
        )
        assert ledger.allowance(ALICE).token_limit == winner
    finally:
        connection.close()


def test_product_initialization_before_our_write_lock_refuses_before_any_trial_ddl(tmp_path):
    path = tmp_path / "product-initializer-race.sqlite3"

    class ProductWinsConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                other = sqlite3.connect(path)
                try:
                    other.execute("CREATE TABLE customer_balances(amount INTEGER)")
                    other.execute("INSERT INTO customer_balances VALUES(42)")
                    other.commit()
                finally:
                    other.close()
            return super().execute(sql, *args)

    connection = sqlite3.connect(path, factory=ProductWinsConnection)
    try:
        with pytest.raises(t.TrialError, match="dedicated_trial_database_required"):
            t.TrialLedger(connection)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("customer_balances",)]
        assert connection.execute("SELECT * FROM customer_balances").fetchall() == [(42,)]
    finally:
        connection.close()


def test_extra_product_table_after_initialization_refuses_next_ledger_write(scene):
    ledger, db, _ = scene
    db.execute("CREATE TABLE nodes(id TEXT)")
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(t.TrialError, match="dedicated_trial_database_required"):
        ledger.admit(ALICE)
    assert list(db.iterdump()) == before


def test_initializer_ddl_is_atomic_and_rolls_back_partial_schema(tmp_path):
    class FailedDDLConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.strip().startswith("CREATE TABLE IF NOT EXISTS devin_trial_global_days"):
                raise sqlite3.OperationalError("synthetic full disk")
            return super().execute(sql, *args)

    connection = sqlite3.connect(tmp_path / "ddl.sqlite3", factory=FailedDDLConnection)
    try:
        with pytest.raises(sqlite3.OperationalError):
            t.TrialLedger(connection)
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )
        assert connection.in_transaction is False
    finally:
        connection.close()
