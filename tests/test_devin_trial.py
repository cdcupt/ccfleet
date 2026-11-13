"""Offline trial guards with fake accounts/time, never provider credentials or calls."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ccfleetd import devin_trial as trial


@pytest.fixture
def scene():
    now = [1_800_000_000.0]
    connection = sqlite3.connect(":memory:")
    ledger = trial.TrialLedger(connection, enabled=True, clock=lambda: now[0])
    yield ledger, connection, now
    connection.close()


@contextlib.contextmanager
def predecessor_daily_caps():
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name in ('DAILY_REQUESTS', 'GLOBAL_DAILY_REQUESTS', 'DAILY_LEASES', 'GLOBAL_DAILY_LEASES'):
            monkeypatch.setattr(trial, name, 20)
        monkeypatch.setattr(trial, 'DAILY_RUNTIME_S', 1200)
        monkeypatch.setattr(trial, 'GLOBAL_DAILY_RUNTIME_S', 1200)
        yield


@pytest.fixture
def legacy_runtime_scene():
    # Build the historical policy record before exercising its old boundary.
    with predecessor_daily_caps():
        yield from scene.__wrapped__()


def predecessor_ledger(connection, **kwargs):
    with predecessor_daily_caps():
        return trial.TrialLedger(connection, **kwargs)


ALICE = trial.VerifiedAccount("alice", email_verified=True)
BOB = trial.VerifiedAccount("bob", email_verified=True)


def admitted(ledger, account=ALICE, **kwargs):
    result = ledger.admit(account, **kwargs)
    assert result.code == "admitted"
    return result.lease


def started(ledger, lease, account=ALICE, seconds=10, request=b"{}"):
    result = ledger.start_step(account, lease, request=request, seconds=seconds)
    assert result.code == "step_reserved"
    return result.step


def finished(ledger, lease, step, account=ALICE):
    assert (
        ledger.finish_step(
            account,
            lease,
            step,
            cleanup_confirmed=True,
            usage=trial.VerifiedUsage(0, complete=True),
        ).code
        == "step_finished"
    )


def test_default_daily_admission_caps_expand_without_changing_tokens_or_per_turn_bounds(scene):
    ledger, _, _ = scene
    allowance = ledger.allowance(ALICE)
    assert allowance.token_limit == allowance.available_tokens == 1_000_000
    assert allowance.global_token_limit == allowance.global_available_tokens == 10_000_000
    assert trial.DAILY_REQUESTS == trial.GLOBAL_DAILY_REQUESTS == 200
    assert trial.DAILY_RUNTIME_S == trial.GLOBAL_DAILY_RUNTIME_S == 12000
    assert trial.DAILY_LEASES == trial.GLOBAL_DAILY_LEASES == 200
    assert trial.TURN_RESERVATION_TOKENS == 20_000
    assert trial.MAX_STEP_S == 340 and trial.MAX_RUNTIME_S == 360 and trial.MAX_LEASE_S == 600


def test_allowance_exposes_both_configured_token_limits():
    connection = sqlite3.connect(":memory:")
    try:
        ledger = trial.TrialLedger(connection, daily_token_limit=234_567,
                                   global_daily_token_limit=765_432)
        allowance = dataclasses.asdict(ledger.allowance(ALICE))
        assert allowance["token_limit"] == 234_567
        assert allowance["global_token_limit"] == 765_432
    finally:
        connection.close()


def test_allowance_exposes_independent_runtime_request_and_lease_capacity(scene):
    book, _, _ = scene
    allowance = book.allowance(ALICE)
    assert allowance.remaining_runtime_seconds == allowance.runtime_limit_seconds == 12000
    assert allowance.global_remaining_runtime_seconds == allowance.global_runtime_limit_seconds == 12000
    assert allowance.turn_reservation_seconds == 340
    assert allowance.remaining_requests == allowance.remaining_global_requests == 200
    assert allowance.remaining_leases == allowance.remaining_global_leases == 200
    assert allowance.request_limit == allowance.global_request_limit == 200
    assert allowance.lease_limit == allowance.global_lease_limit == 200
    assert allowance.remaining_turns == 35


def test_expanded_runtime_admits75_full_turns_before_shared_daily_exhaustion(scene):
    book, connection, _ = scene
    for _ in range(75):
        lease = admitted(book, lease_seconds=180)
        step = started(book, lease, seconds=160)
        finished(book, lease, step)
        book.close(ALICE, lease, cleanup_confirmed=True)
    allowance = book.allowance(ALICE)
    assert allowance.remaining_runtime_seconds == allowance.global_remaining_runtime_seconds == 0
    assert allowance.remaining_requests == allowance.remaining_global_requests == 125
    assert allowance.remaining_leases == allowance.remaining_global_leases == 125
    assert allowance.remaining_turns == 0
    assert book.admit(BOB).code == 'global_daily_limit_reached'
    assert connection.execute('SELECT requests,runtime_seconds FROM devin_trial_global_days').fetchone() == (
        75, 12000)


def test_seven_reserved_turns_show80_seconds_and_zero_additional_turns_without_reset(legacy_runtime_scene):
    book, connection, _ = legacy_runtime_scene
    for _ in range(7):
        lease = admitted(book)
        step = started(book, lease, seconds=160)
        finished(book, lease, step)
        book.close(ALICE, lease, cleanup_confirmed=True)
    before = database_snapshot(connection)
    allowance = book.allowance(ALICE)
    assert allowance.remaining_requests == allowance.remaining_global_requests == 13
    assert allowance.remaining_leases == allowance.remaining_global_leases == 13
    assert allowance.remaining_runtime_seconds == allowance.global_remaining_runtime_seconds == 80
    assert allowance.remaining_turns == 0
    assert allowance.available_tokens == 1_000_000 and not allowance.paused
    assert database_snapshot(connection) == before


def test_another_accounts_reserved_runtime_exhausts_shared_turn_capacity(legacy_runtime_scene):
    book, _, _ = legacy_runtime_scene
    for _ in range(7):
        lease = admitted(book, BOB)
        step = started(book, lease, BOB, seconds=160)
        finished(book, lease, step, BOB)
        book.close(BOB, lease, cleanup_confirmed=True)
    allowance = book.allowance(ALICE)
    assert allowance.remaining_requests == allowance.remaining_leases == 20
    assert allowance.remaining_global_requests == allowance.remaining_global_leases == 13
    assert allowance.remaining_runtime_seconds == 1200
    assert allowance.global_remaining_runtime_seconds == 80
    assert allowance.remaining_turns == 0


@pytest.mark.parametrize('budget', ['request', 'global_request', 'lease', 'global_lease'])
def test_remaining_turn_upper_bound_includes_each_request_and_lease_budget(scene, budget):
    book, connection, _ = scene
    book.allowance(ALICE)
    table = 'devin_trial_global_days' if budget.startswith('global_') else 'devin_trial_account_days'
    column = 'leases' if budget.endswith('lease') else 'requests'
    connection.execute(f'UPDATE {table} SET {column}=199')
    connection.commit()
    allowance = book.allowance(ALICE)
    assert allowance.remaining_turns == 1
    assert allowance.remaining_runtime_seconds == allowance.global_remaining_runtime_seconds == 12000


@pytest.mark.parametrize('gate', ['disabled', 'paused', 'personal_tokens', 'shared_tokens'])
def test_no_remaining_turns_when_trial_disabled_paused_or_token_allowance_empty(scene, gate):
    book, connection, now = scene
    book.allowance(ALICE)
    if gate == 'disabled':
        book = trial.TrialLedger(connection, enabled=False, clock=lambda: now[0])
    elif gate == 'paused':
        book.pause()
    else:
        table = 'devin_trial_account_days' if gate == 'personal_tokens' else 'devin_trial_global_days'
        limit = 1_000_000 if gate == 'personal_tokens' else 10_000_000
        connection.execute(f'UPDATE {table} SET reserved_tokens=?', (limit,))
        connection.commit()
    before = database_snapshot(connection)
    allowance = book.allowance(ALICE)
    assert allowance.remaining_turns == 0
    assert database_snapshot(connection) == before


def test_expanded_shared_pool_keeps_ten_separate_full_personal_token_budgets(scene):
    ledger, _, _ = scene
    for index in range(10):
        account = trial.VerifiedAccount("visitor" + str(index), True)
        lease = admitted(ledger, account)
        step = started(ledger, lease, account, seconds=1)
        ledger.finish_step(account, lease, step, cleanup_confirmed=True,
                           usage=trial.VerifiedUsage(1_000_000, True))
        ledger.close(account, lease, cleanup_confirmed=True)
        allowance = ledger.allowance(account)
        assert allowance.available_tokens == 0
        assert allowance.reported_tokens == 1_000_000
        assert allowance.global_available_tokens == (9 - index) * 1_000_000
    assert ledger.admit(ALICE).code == "global_daily_limit_reached"


def test_default_instance_cannot_admit_or_run_even_with_an_existing_lease(scene):
    ledger, connection, now = scene
    lease = admitted(ledger)
    disabled = trial.TrialLedger(connection, clock=lambda: now[0])
    assert disabled.admit(BOB).code == "trial_disabled"
    assert disabled.start_step(ALICE, lease, request=b"{}").code == "trial_disabled"
    assert disabled.close(ALICE, lease, cleanup_confirmed=True).code == "trial_closed"


@pytest.mark.parametrize(
    "account",
    [
        None,
        "alice",
        {"account_id": "alice", "email_verified": True},
        trial.VerifiedAccount("alice"),
        trial.VerifiedAccount("alice", email_verified=1),
        trial.VerifiedAccount("alice@example.com", email_verified=True),
        trial.VerifiedAccount("", email_verified=True),
        trial.VerifiedAccount("x" * 129, True),
    ],
)
def test_only_explicit_server_verified_account_identity_is_admitted(scene, account):
    ledger, connection, _ = scene
    assert ledger.admit(account).code == "verified_account_required"
    assert connection.execute("SELECT count(*) FROM devin_trial_sessions").fetchone()[0] == 0


def test_secret_is_hashed_no_conversation_is_stored_and_history_namespace_is_unique(scene):
    ledger, connection, _ = scene
    first = admitted(ledger)
    assert first.secret not in repr(first)
    row = connection.execute("SELECT * FROM devin_trial_sessions").fetchone()
    assert first.secret not in row
    assert hashlib.sha256(first.secret.encode()).hexdigest() in row
    payload = b"private fake prompt never stored"
    step = started(ledger, first, request=payload)
    assert step.history_namespace == first.history_namespace
    assert payload.decode() not in repr(
        connection.execute("SELECT * FROM devin_trial_sessions").fetchall()
    )
    finished(ledger, first, step)
    ledger.close(ALICE, first, cleanup_confirmed=True)
    second = admitted(ledger, BOB)
    assert first.history_namespace != second.history_namespace
    assert first.trial_id != second.trial_id


def test_busy_rejects_instantly_without_burning_other_accounts_attempt(scene):
    ledger, connection, _ = scene
    lease = admitted(ledger)
    for _ in range(3):
        assert ledger.admit(BOB).code == "trial_busy"
    assert connection.execute("SELECT count(*) FROM devin_trial_sessions").fetchone()[0] == 1
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(BOB).code == "admitted"


def test_closed_account_can_return_without_erasing_its_daily_history(scene):
    ledger, connection, now = scene
    lease = admitted(ledger)
    assert ledger.cancel(ALICE, lease).code == "trial_cancelled"
    assert ledger.start_step(ALICE, lease, request=b"{}").code == "trial_closed"
    assert ledger.close(ALICE, lease, cleanup_confirmed=True).code == "trial_closed"
    reopened = trial.TrialLedger(connection, enabled=True, clock=lambda: now[0])
    assert reopened.admit(ALICE).code == "admitted"


@pytest.mark.parametrize("seconds", [0, -1, 601, True, 1.5, "600"])
def test_ten_minute_lease_is_a_hard_admission_limit(scene, seconds):
    ledger, _, _ = scene
    assert ledger.admit(ALICE, lease_seconds=seconds).code == "invalid_lease_seconds"


def test_unused_lease_expiry_allows_same_day_return_with_bounded_admissions(scene):
    ledger, _, now = scene
    lease = admitted(ledger)
    assert lease.expires_at == now[0] + 600
    now[0] = lease.expires_at
    assert ledger.start_step(ALICE, lease, request=b"{}").code == "trial_closed"
    assert ledger.allowance(ALICE).reported_tokens == 0
    assert ledger.admit(BOB).code == "admitted"


@pytest.mark.parametrize("body", [b"", b"a" * (trial.MAX_REQUEST_BYTES + 1), "{}", bytearray(b"{}"), None])
def test_request_limit_is_checked_before_a_step_reservation(scene, body):
    ledger, connection, _ = scene
    lease = admitted(ledger)
    assert ledger.start_step(ALICE, lease, request=body).code == "request_size_limit"
    assert connection.execute("SELECT steps FROM devin_trial_sessions").fetchone()[0] == 0


def test_exact_request_byte_limit_is_accepted(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    started(ledger, lease, request=b"a" * trial.MAX_REQUEST_BYTES)


@pytest.mark.parametrize("seconds", [0, -1, 341, True, 1.5, "340"])
def test_step_cannot_reserve_more_than_the_bounded_turn(scene, seconds):
    ledger, _, _ = scene
    lease = admitted(ledger)
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=seconds).code == "step_time_limit"


def test_total_runtime_is_reserved_before_execution_and_never_refunded(scene):
    ledger, connection, _ = scene
    lease = admitted(ledger)
    for seconds in (340, 20):
        step = started(ledger, lease, seconds=seconds)
        finished(ledger, lease, step)
    assert (
        connection.execute("SELECT reserved_seconds FROM devin_trial_sessions").fetchone()[0] == 360
    )
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=1).code == "trial_limit_reached"
    assert ledger.admit(BOB).code == "trial_busy", "final history cleanup still required"
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(BOB).code == "admitted"


def test_partial_remaining_runtime_can_only_be_reserved_within_the_budget(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    for seconds in (200, 140):
        step = started(ledger, lease, seconds=seconds)
        finished(ledger, lease, step)
    assert (
        ledger.start_step(ALICE, lease, request=b"{}", seconds=21).code
        == "runtime_reservation_limit"
    )
    started(ledger, lease, seconds=20)


def test_step_limit_counts_reservations_not_just_successful_answers(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    for _ in range(5):
        step = started(ledger, lease, seconds=1)
        finished(ledger, lease, step)
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=1).code == "trial_limit_reached"


def test_only_one_step_can_be_in_flight_and_old_receipt_cannot_finish_a_new_step(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    first = started(ledger, lease)
    assert ledger.start_step(ALICE, lease, request=b"{}").code == "step_busy"
    finished(ledger, lease, first)
    second = started(ledger, lease)
    assert (
        ledger.finish_step(ALICE, lease, first, cleanup_confirmed=True).code
        == "invalid_step_receipt"
    )
    assert ledger.authorize_step(ALICE, lease, second)


def test_step_must_fit_in_remaining_lease_time(scene):
    ledger, _, now = scene
    lease = admitted(ledger, lease_seconds=15)
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=16).code == "lease_time_limit"
    step = started(ledger, lease, seconds=15)
    assert step.deadline == lease.expires_at
    now[0] += 14.99
    assert ledger.authorize_step(ALICE, lease, step)
    now[0] = lease.expires_at
    assert not ledger.authorize_step(ALICE, lease, step)
    assert ledger.admit(BOB).code == "trial_paused"


def test_runtime_deadline_expiry_revokes_authorization_and_latches_global_pause(scene):
    ledger, _, now = scene
    lease = admitted(ledger)
    step = started(ledger, lease, seconds=1)
    now[0] = step.deadline
    assert not ledger.authorize_step(ALICE, lease, step)
    assert ledger.admit(BOB).code == "trial_paused"
    finished(ledger, lease, step)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(BOB).code == "trial_paused", "late cleanup cannot silently resume admission"


def test_lease_expiry_after_a_finished_step_does_not_claim_history_was_cleaned(scene):
    ledger, _, now = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    finished(ledger, lease, step)
    now[0] = lease.expires_at
    assert ledger.admit(BOB).code == "trial_paused"


def test_cancelled_worker_that_misses_its_deadline_also_latches_global_pause(scene):
    ledger, _, now = scene
    lease = admitted(ledger)
    step = started(ledger, lease, seconds=1)
    ledger.cancel(ALICE, lease)
    now[0] = step.deadline
    assert ledger.admit(BOB).code == "trial_paused"
    finished(ledger, lease, step)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(BOB).code == "trial_paused"


def test_cancellation_revokes_an_active_permit_permanently_but_allows_cleanup(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    assert ledger.authorize_step(ALICE, lease, step)
    assert ledger.cancel(ALICE, lease).code == "trial_cancelled"
    assert not ledger.authorize_step(ALICE, lease, step)
    assert ledger.close(ALICE, lease, cleanup_confirmed=True).code == "step_cleanup_required"
    assert ledger.admit(BOB).code == "trial_busy"
    finished(ledger, lease, step)
    assert ledger.start_step(ALICE, lease, request=b"{}").code == "trial_closed"
    assert ledger.admit(BOB).code == "trial_busy"
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.allowance(ALICE).reported_tokens == 0
    assert ledger.admit(BOB).code == "admitted"


@pytest.mark.parametrize("cleanup", [False, None, 1, "true"])
def test_failed_worker_cleanup_pauses_all_future_admissions_even_after_reopen(scene, cleanup):
    ledger, connection, now = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    assert (
        ledger.finish_step(ALICE, lease, step, cleanup_confirmed=cleanup).code
        == "cleanup_unconfirmed"
    )
    assert not ledger.authorize_step(ALICE, lease, step)
    finished(ledger, lease, step)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    reopened = trial.TrialLedger(connection, enabled=True, clock=lambda: now[0])
    assert reopened.admit(BOB).code == "trial_paused"


def test_failed_trial_cleanup_pauses_and_repeated_closed_receipt_never_reopens_slot(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    assert ledger.close(ALICE, lease, cleanup_confirmed=False).code == "cleanup_unconfirmed"
    assert ledger.admit(BOB).code == "trial_paused"
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.close(ALICE, lease, cleanup_confirmed=False).code == "trial_closed"


def test_later_expiry_and_operator_pause_preserve_first_unconfirmed_cleanup_reason(scene):
    ledger, connection, now = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    ledger.finish_step(ALICE, lease, step, cleanup_confirmed=False)
    now[0] = lease.expires_at + 86400
    assert ledger.allowance(BOB).paused
    ledger.pause()
    assert connection.execute(
        "SELECT paused,reason FROM devin_trial_control WHERE id=1"
    ).fetchone() == (1, "worker_cleanup_unconfirmed")
    assert ledger.admit(BOB).code == "trial_paused"


def test_operator_pause_revokes_active_work_and_cannot_prevent_cancellation(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    ledger.pause()
    assert not ledger.authorize_step(ALICE, lease, step)
    assert ledger.admit(BOB).code == "trial_paused"
    assert ledger.cancel(ALICE, lease).code == "trial_cancelled"
    finished(ledger, lease, step)
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(BOB).code == "trial_paused"


def test_foreign_account_and_forged_lease_cannot_spend_cancel_or_close(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    assert not ledger.authorize_step(BOB, lease, step)
    assert ledger.cancel(BOB, lease).code == "invalid_trial_lease"
    assert ledger.close(BOB, lease, cleanup_confirmed=True).code == "invalid_trial_lease"
    for forged in (
        dataclasses.replace(lease, secret="a" * 64),
        dataclasses.replace(lease, history_namespace="trial-foreign"),
        dataclasses.replace(lease, expires_at=lease.expires_at + 600),
    ):
        assert ledger.cancel(ALICE, forged).code == "invalid_trial_lease"
    assert ledger.authorize_step(ALICE, lease, step)


@pytest.mark.parametrize(
    "replacement",
    [
        {"trial_id": "a" * 64},
        {"step_id": "b" * 64},
        {"history_namespace": "trial-foreign"},
        {"deadline": 1_900_000_000.0},
        {"reserved_seconds": 60},
        {"reserved_seconds": True},
    ],
)
def test_forged_permit_cannot_authorize_or_submit_a_cleanup_receipt(scene, replacement):
    ledger, _, _ = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    forged = dataclasses.replace(step, **replacement)
    assert not ledger.authorize_step(ALICE, lease, forged)
    assert (
        ledger.finish_step(ALICE, lease, forged, cleanup_confirmed=True).code
        == "invalid_step_receipt"
    )
    assert ledger.authorize_step(ALICE, lease, step)


def test_clock_regression_latches_pause_instead_of_extending_execution(scene):
    ledger, _, now = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    now[0] -= 1
    assert not ledger.authorize_step(ALICE, lease, step)
    assert ledger.admit(BOB).code == "trial_paused"


@pytest.mark.parametrize("bad_clock", [True, float("nan"), float("inf"), -1, 10**12])
def test_invalid_clock_never_creates_an_admission(scene, bad_clock):
    ledger, connection, now = scene
    now[0] = bad_clock
    with pytest.raises(trial.TrialError, match="invalid_trial_clock"):
        ledger.admit(ALICE)
    assert connection.execute("SELECT count(*) FROM devin_trial_sessions").fetchone()[0] == 0


def test_existing_product_database_is_refused_without_adding_trial_tables():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE customer_balances (amount INTEGER)")
    with pytest.raises(trial.TrialError, match="dedicated_trial_database_required"):
        trial.TrialLedger(connection)
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
        ("customer_balances",)
    ]
    connection.close()


def test_database_lock_is_bounded_and_never_becomes_an_unlimited_waiting_queue(tmp_path):
    path = tmp_path / "offline-trials.db"
    first, second = sqlite3.connect(path), sqlite3.connect(path)
    trial.TrialLedger(first)
    ledger = trial.TrialLedger(second, enabled=True)
    first.execute("BEGIN IMMEDIATE")
    try:
        assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 100
        with pytest.raises(trial.TrialError, match="trial_ledger_busy"):
            ledger.admit(ALICE)
    finally:
        first.rollback()
        first.close()
        second.close()


def test_two_independent_connections_cannot_admit_two_simultaneous_visitors(tmp_path):
    path = tmp_path / "offline-trials.db"
    connection = sqlite3.connect(path)
    trial.TrialLedger(connection)
    connection.close()
    barrier = Barrier(2)

    def contender(account):
        current = sqlite3.connect(path)
        ledger = trial.TrialLedger(current, enabled=True, clock=lambda: 1_800_000_000.0)
        barrier.wait(timeout=5)
        try:
            return ledger.admit(account).code
        finally:
            current.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(contender, (ALICE, BOB)))
    assert sorted(outcomes) == ["admitted", "trial_busy"]


def test_legacy1200_second_policy_keeps_its_explicit_runtime_boundary(legacy_runtime_scene):
    ledger, connection, _ = legacy_runtime_scene
    assert trial.DAILY_RUNTIME_S == trial.GLOBAL_DAILY_RUNTIME_S == 1200
    for _ in range(7):
        lease = admitted(ledger)
        step = started(ledger, lease, seconds=160)
        finished(ledger, lease, step)
        ledger.close(ALICE, lease, cleanup_confirmed=True)
    lease = admitted(ledger)
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=160).code == (
        "runtime_reservation_limit"
    )
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    # Another account cannot spend the shared last80s as a full160s turn.
    lease = admitted(ledger, BOB)
    assert ledger.start_step(BOB, lease, request=b"{}", seconds=160).code == (
        "runtime_reservation_limit"
    )
    step = started(ledger, lease, BOB, seconds=80)
    finished(ledger, lease, step, BOB)
    assert connection.execute(
        "SELECT runtime_seconds FROM devin_trial_global_days"
    ).fetchone() == (1200,)


def test_160_second_permit_expires_without_extending_or_discarding_the_unknown_hold(scene):
    ledger, connection, now = scene
    lease = admitted(ledger, lease_seconds=180)
    step = started(ledger, lease, seconds=160)
    now[0] += 159.99
    assert ledger.authorize_step(ALICE, lease, step)
    now[0] = step.deadline
    assert not ledger.authorize_step(ALICE, lease, step)
    assert connection.execute(
        "SELECT state,active_seconds,active_tokens,step_deadline FROM devin_trial_sessions"
    ).fetchone() == ("closing", 160, 20000, step.deadline)
    assert connection.execute(
        "SELECT paused,reason FROM devin_trial_control"
    ).fetchone() == (1, "expired_worker_or_history_cleanup_required")
    assert ledger.admit(BOB).code == "trial_paused"


# Exact deployed predecessor, independently recorded rather than derived from
# the new constants. Migration must recognize these constraints without
# rewriting an old deadline, hold, pause or measured usage.
LEGACY_SESSIONS = """
    CREATE TABLE devin_trial_sessions (
        trial_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, secret_hash TEXT NOT NULL,
        history_namespace TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','closing','closed')),
        steps INTEGER NOT NULL CHECK(steps BETWEEN 0 AND 5),
        reserved_seconds INTEGER NOT NULL CHECK(reserved_seconds BETWEEN 0 AND 120),
        active_step TEXT, step_deadline REAL,
        active_seconds INTEGER CHECK(active_seconds BETWEEN 1 AND 60),
        utc_day INTEGER NOT NULL, active_tokens INTEGER CHECK(active_tokens>0))
"""


def legacy_database(connection, state="closing"):
    now = 1_800_000_000.0
    ledger = predecessor_ledger(connection, enabled=True, clock=lambda: now,
                                daily_token_limit=100_000, global_daily_token_limit=1_000_000)
    lease = admitted(ledger)
    step = started(ledger, lease, seconds=60)
    assert ledger.finish_step(ALICE, lease, step, cleanup_confirmed=True,
        usage=trial.VerifiedCacheReadUnmeasured(86780, True)).code == "step_finished_unmeasured"
    lease = admitted(ledger, BOB)
    step = started(ledger, lease, BOB, seconds=60)
    assert ledger.finish_step(BOB, lease, step, cleanup_confirmed=True,
        usage=trial.VerifiedUsage(314, True)).code == "step_finished"
    ledger.close(BOB, lease, cleanup_confirmed=True)
    lease = admitted(ledger)
    step = started(ledger, lease, seconds=60)
    if state == "closing":
        ledger.finish_step(ALICE, lease, step, cleanup_confirmed=True)
        assert connection.execute(
            "SELECT active_tokens FROM devin_trial_sessions WHERE trial_id=?", (lease.trial_id,)
        ).fetchone() == (13220,)
    elif state == "closed":
        finished(ledger, lease, step)
        ledger.close(ALICE, lease, cleanup_confirmed=True)
    connection.execute("ALTER TABLE devin_trial_sessions RENAME TO fixture_new_sessions")
    connection.execute(LEGACY_SESSIONS)
    connection.execute("INSERT INTO devin_trial_sessions SELECT * FROM fixture_new_sessions")
    connection.execute("DROP TABLE fixture_new_sessions")
    connection.execute("CREATE UNIQUE INDEX devin_trial_one_active "
        "ON devin_trial_sessions ((1)) WHERE state IN ('open','closing')")
    connection.execute("CREATE UNIQUE INDEX devin_trial_one_account_active "
        "ON devin_trial_sessions(account_id) WHERE state IN ('open','closing')")
    connection.commit()
    return lease, step


def database_snapshot(connection):
    return {name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
            for name in trial._TABLES}


def expanded_policy_snapshot(before):
    control = list(before["devin_trial_control"][0])
    limits = json.loads(control[4])
    assert limits in ([4, 20000, 100000, 1000000, 20, 20, 1200, 1200, 20, 20],
                      [4, 20000, 1000000, 10000000, 20, 20, 1200, 1200, 20, 20])
    limits = [4, 20000, 1000000, 10000000, 200, 200, 12000, 12000, 200, 200]
    control[4] = json.dumps(limits, separators=(",", ":"))
    return {**before, "devin_trial_control": [tuple(control)]}


@pytest.mark.parametrize("state", ["open", "closing", "closed"])
def test_legacy_time_constraint_migration_preserves_every_row_and_is_idempotent(state):
    connection = sqlite3.connect(":memory:")
    lease, step = legacy_database(connection, state)
    before = database_snapshot(connection)
    expected = expanded_policy_snapshot(before)
    trial.TrialLedger(connection, enabled=True)
    assert database_snapshot(connection) == expected
    schema = list(connection.execute("SELECT * FROM sqlite_master ORDER BY name"))
    version = connection.execute("PRAGMA schema_version").fetchone()
    trial.TrialLedger(connection, enabled=True)
    assert database_snapshot(connection) == expected
    assert list(connection.execute("SELECT * FROM sqlite_master ORDER BY name")) == schema
    assert connection.execute("PRAGMA schema_version").fetchone() == version
    assert connection.execute(
        "SELECT active_seconds,step_deadline FROM devin_trial_sessions WHERE trial_id=?",
        (lease.trial_id,),
    ).fetchone() == ((None, None) if state == "closed" else (60, step.deadline))
    # New constraints are wider, but remain explicit hard SQLite bounds.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE devin_trial_sessions SET active_seconds=341")
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE devin_trial_sessions SET reserved_seconds=361")
    connection.rollback()
    if state == "closed":
        ledger = trial.TrialLedger(connection, enabled=True, clock=lambda: 1_800_000_000.0)
        started(ledger, admitted(ledger), seconds=160)
    else:
        assert database_snapshot(connection) == expected
    connection.close()


@pytest.mark.parametrize("change", ["index", "trigger", "view", "constraint"])
def test_migration_refuses_unknown_schema_without_altering_incident_or_schema(change):
    connection = sqlite3.connect(":memory:")
    legacy_database(connection)
    if change == "index":
        connection.execute("CREATE INDEX unexpected ON devin_trial_sessions(account_id)")
    elif change == "trigger":
        connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON devin_trial_unmeasured "
            "BEGIN SELECT 1; END")
    elif change == "view":
        connection.execute("CREATE VIEW unexpected AS SELECT trial_id FROM devin_trial_sessions")
    else:
        connection.execute("ALTER TABLE devin_trial_sessions RENAME TO fixture_old_sessions")
        connection.execute(LEGACY_SESSIONS.replace("BETWEEN 0 AND 120", "BETWEEN 0 AND 121"))
        connection.execute("INSERT INTO devin_trial_sessions SELECT * FROM fixture_old_sessions")
        connection.execute("DROP TABLE fixture_old_sessions")
    connection.commit()
    before = database_snapshot(connection)
    schema = list(connection.execute("SELECT * FROM sqlite_master ORDER BY name"))
    with pytest.raises(trial.TrialError, match="trial_schema_changed"):
        trial.TrialLedger(connection, enabled=True)
    assert database_snapshot(connection) == before
    assert list(connection.execute("SELECT * FROM sqlite_master ORDER BY name")) == schema
    assert not connection.in_transaction
    connection.close()


def test_failed_migration_copy_rolls_back_ddl_and_all_incident_rows():
    class FailingCopy(sqlite3.Connection):
        blocked = False

        def execute(self, sql, *args):
            if self.blocked and sql.startswith("INSERT INTO devin_trial_sessions("):
                raise sqlite3.OperationalError("synthetic interrupted copy")
            return super().execute(sql, *args)

    connection = sqlite3.connect(":memory:", factory=FailingCopy)
    legacy_database(connection)
    before = database_snapshot(connection)
    schema = list(connection.execute("SELECT * FROM sqlite_master ORDER BY name"))
    connection.blocked = True
    with pytest.raises(sqlite3.OperationalError, match="synthetic interrupted copy"):
        trial.TrialLedger(connection, enabled=True)
    assert database_snapshot(connection) == before
    assert list(connection.execute("SELECT * FROM sqlite_master ORDER BY name")) == schema
    assert not connection.in_transaction
    connection.close()


@pytest.mark.parametrize("paused", [False, True])
def test_known_token_expansion_preserves_usage_holds_active_permit_pause_and_history(paused):
    connection = sqlite3.connect(":memory:")
    now = 1_800_000_000.0
    try:
        old = predecessor_ledger(connection, enabled=True, clock=lambda: now,
                                 daily_token_limit=100_000, global_daily_token_limit=1_000_000)
        lease = admitted(old)
        step = started(old, lease)
        old.finish_step(ALICE, lease, step, cleanup_confirmed=True,
                        usage=trial.VerifiedUsage(321, True))
        old.close(ALICE, lease, cleanup_confirmed=True)
        lease = admitted(old)
        step = started(old, lease)
        old.finish_step(ALICE, lease, step, cleanup_confirmed=True,
                        usage=trial.VerifiedCacheReadUnmeasured(100, True))
        old.close(ALICE, lease, cleanup_confirmed=True)
        active = admitted(old, BOB)
        permit = started(old, active, BOB, seconds=60)
        if paused:
            old.pause()
        before = database_snapshot(connection)
        schema = list(connection.execute("SELECT * FROM sqlite_master ORDER BY name"))
        upgraded = trial.TrialLedger(connection, enabled=True, clock=lambda: now)
        expected = expanded_policy_snapshot(before)
        assert database_snapshot(connection) == expected
        assert list(connection.execute("SELECT * FROM sqlite_master ORDER BY name")) == schema
        allowance = upgraded.allowance(ALICE)
        assert allowance.reported_tokens == 321
        assert allowance.reserved_tokens == allowance.unmeasured_reserved_tokens == 20_000
        assert allowance.partial_reported_tokens == 100
        assert allowance.available_tokens == 979_679
        assert allowance.global_available_tokens == 9_959_679
        assert allowance.paused is paused
        assert upgraded.authorize_step(BOB, active, permit) is not paused
        assert permit.reserved_tokens == 20_000 and permit.reserved_seconds == 60
        trial.TrialLedger(connection, enabled=True, clock=lambda: now)
        assert database_snapshot(connection) == expected
    finally:
        connection.close()


@pytest.mark.parametrize("index,value", [(0, 3), (1, 21_000), (2, 100_001),
                                         (3, 1_000_001), (4, 21), (6, 1201)])
def test_token_expansion_refuses_unknown_limits_without_mutation(index, value):
    connection = sqlite3.connect(":memory:")
    try:
        predecessor_ledger(connection, daily_token_limit=100_000,
                           global_daily_token_limit=1_000_000)
        limits = json.loads(connection.execute("SELECT limits FROM devin_trial_control").fetchone()[0])
        limits[index] = value
        connection.execute("UPDATE devin_trial_control SET limits=?",
                           (json.dumps(limits, separators=(",", ":")),))
        connection.commit()
        before = list(connection.iterdump())
        with pytest.raises(trial.TrialError, match="trial_limits_changed"):
            trial.TrialLedger(connection)
        assert list(connection.iterdump()) == before
        assert not connection.in_transaction
    finally:
        connection.close()


@pytest.mark.parametrize('daily,shared', [(100_000, 1_000_000), (1_000_000, 10_000_000)])
def test_old_configuration_cannot_roll_back_expanded_budgets_or_reset_history(scene, daily, shared):
    ledger, connection, _ = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    ledger.finish_step(ALICE, lease, step, cleanup_confirmed=True)
    before = list(connection.iterdump())
    with predecessor_daily_caps(), pytest.raises(trial.TrialError, match="trial_limits_changed"):
        trial.TrialLedger(connection, daily_token_limit=daily, global_daily_token_limit=shared)
    assert list(connection.iterdump()) == before


@pytest.mark.parametrize('daily,shared', [(100_000, 1_000_000), (1_000_000, 10_000_000)])
def test_token_expansion_rolls_back_if_later_initialization_fails(daily, shared):
    class FailingInitialize(sqlite3.Connection):
        blocked = False

        def execute(self, sql, *args):
            if self.blocked and sql.startswith("INSERT OR IGNORE INTO devin_trial_control"):
                raise sqlite3.OperationalError("synthetic interrupted initialization")
            return super().execute(sql, *args)

    connection = sqlite3.connect(":memory:", factory=FailingInitialize)
    try:
        old = predecessor_ledger(connection, enabled=True, daily_token_limit=daily,
                                 global_daily_token_limit=shared)
        old.pause()
        before = list(connection.iterdump())
        connection.blocked = True
        with pytest.raises(sqlite3.OperationalError, match="synthetic interrupted initialization"):
            trial.TrialLedger(connection)
        assert list(connection.iterdump()) == before
        assert not connection.in_transaction
    finally:
        connection.close()


def test_original_token_upgrade_remains_a_strict_intermediate_policy_before_daily_expansion():
    connection = sqlite3.connect(':memory:')
    try:
        old = predecessor_ledger(connection, enabled=True, daily_token_limit=100_000,
                                 global_daily_token_limit=1_000_000)
        old.pause()
        original = database_snapshot(connection)
        with predecessor_daily_caps():
            trial.TrialLedger(connection, enabled=True)
        middle = database_snapshot(connection)
        middle_limits = json.loads(middle['devin_trial_control'][0][4])
        assert middle_limits == [4, 20000, 1000000, 10000000, 20, 20, 1200, 1200, 20, 20]
        for name in trial._TABLES - {'devin_trial_control'}:
            assert middle[name] == original[name]
        assert middle['devin_trial_control'][0][:-1] == original['devin_trial_control'][0][:-1]
        trial.TrialLedger(connection, enabled=True)
        assert database_snapshot(connection) == expanded_policy_snapshot(middle)
    finally:
        connection.close()


@pytest.mark.parametrize('recovered', [False, True])
def test_admission_expansion_preserves143121_reserved_including_unknown20k(recovered):
    connection = sqlite3.connect(':memory:')
    now = 1_800_000_000.0
    try:
        with predecessor_daily_caps():
            old = trial.TrialLedger(connection, enabled=True, clock=lambda: now)
            for usage in (trial.VerifiedUsage(321, True),
                          trial.VerifiedCacheReadUnmeasured(123121, True)):
                lease = admitted(old, lease_seconds=180)
                step = started(old, lease, seconds=160)
                old.finish_step(ALICE, lease, step, cleanup_confirmed=True, usage=usage)
                old.close(ALICE, lease, cleanup_confirmed=True)
            lease = admitted(old, lease_seconds=180)
            step = started(old, lease, seconds=160)
            assert old.finish_step(ALICE, lease, step, cleanup_confirmed=True).code == 'usage_unaccounted'
        if recovered:
            # Synthetic representation of a separately approved prior recovery:
            # the unknown hold stays charged, without a fabricated usage row.
            connection.execute("UPDATE devin_trial_sessions SET state='closed',active_step=NULL,"
                'step_deadline=NULL,active_seconds=NULL,active_tokens=NULL WHERE trial_id=?',
                (lease.trial_id,))
            connection.execute("UPDATE devin_trial_control SET paused=0,reason=''")
            connection.commit()
        before = database_snapshot(connection)
        upgraded = trial.TrialLedger(connection, enabled=True, clock=lambda: now)
        expected = expanded_policy_snapshot(before)
        assert database_snapshot(connection) == expected
        allowance = upgraded.allowance(ALICE)
        assert allowance.reserved_tokens == 143121
        assert allowance.unmeasured_reserved_tokens == allowance.partial_reported_tokens == 123121
        assert allowance.reported_tokens == 321
        assert allowance.available_tokens == 856558
        assert allowance.remaining_runtime_seconds == allowance.global_remaining_runtime_seconds == 11520
        assert allowance.paused is not recovered
        assert allowance.remaining_turns == (33 if recovered else 0)
        trial.TrialLedger(connection, enabled=True, clock=lambda: now)
        assert database_snapshot(connection) == expected
    finally:
        connection.close()


def test_daily_expansion_gives_new_capacity_without_refunding_previous1120_seconds():
    connection = sqlite3.connect(':memory:')
    now = 1_800_000_000.0
    try:
        with predecessor_daily_caps():
            old = trial.TrialLedger(connection, enabled=True, clock=lambda: now)
            for _ in range(7):
                lease = admitted(old, lease_seconds=180)
                step = started(old, lease, seconds=160)
                finished(old, lease, step)
                old.close(ALICE, lease, cleanup_confirmed=True)
            assert old.allowance(ALICE).remaining_turns == 0
        before = database_snapshot(connection)
        upgraded = trial.TrialLedger(connection, enabled=True, clock=lambda: now)
        allowance = upgraded.allowance(ALICE)
        assert allowance.remaining_runtime_seconds == allowance.global_remaining_runtime_seconds == 10880
        assert allowance.remaining_turns == 32
        assert allowance.remaining_requests == allowance.remaining_leases == 193
        assert database_snapshot(connection) == expanded_policy_snapshot(before)
    finally:
        connection.close()


@pytest.mark.parametrize('index,value', [(0, 5), (1, 20001), (2, 1000001), (3, 10000001),
    (4, 200), (5, 200), (6, 12000), (7, 12000), (8, 200), (9, 200)])
def test_daily_expansion_rejects_partial_or_unreviewed_middle_policies(index, value):
    connection = sqlite3.connect(':memory:')
    try:
        predecessor_ledger(connection)
        limits = json.loads(connection.execute('SELECT limits FROM devin_trial_control').fetchone()[0])
        limits[index] = value
        connection.execute('UPDATE devin_trial_control SET limits=?',
                           (json.dumps(limits, separators=(',', ':')),))
        connection.commit()
        before = list(connection.iterdump())
        with pytest.raises(trial.TrialError, match='trial_limits_changed'):
            trial.TrialLedger(connection)
        assert list(connection.iterdump()) == before
        assert not connection.in_transaction
    finally:
        connection.close()
