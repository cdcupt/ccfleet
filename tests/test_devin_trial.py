"""Offline trial guards with fake accounts/time, never provider credentials or calls."""

from __future__ import annotations

import dataclasses
import hashlib
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
    assert ledger.finish_step(account, lease, step, cleanup_confirmed=True).code == "step_finished"


def test_default_instance_cannot_admit_or_run_even_with_an_existing_lease(scene):
    ledger, connection, now = scene
    lease = admitted(ledger)
    disabled = trial.TrialLedger(connection, clock=lambda: now[0])
    assert disabled.admit(BOB).code == "trial_disabled"
    assert disabled.start_step(ALICE, lease, request=b"{}").code == "trial_disabled"
    assert disabled.close(ALICE, lease, cleanup_confirmed=True).code == "trial_closed"


@pytest.mark.parametrize("account", [
    None, "alice", {"account_id": "alice", "email_verified": True},
    trial.VerifiedAccount("alice"), trial.VerifiedAccount("alice", email_verified=1),
    trial.VerifiedAccount("alice@example.com", email_verified=True),
    trial.VerifiedAccount("", email_verified=True), trial.VerifiedAccount("x" * 129, True),
])
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
    assert payload.decode() not in repr(connection.execute("SELECT * FROM devin_trial_sessions").fetchall())
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


def test_one_attempt_remains_used_after_cancel_close_and_reopen(scene):
    ledger, connection, now = scene
    lease = admitted(ledger)
    assert ledger.cancel(ALICE, lease).code == "trial_cancelled"
    assert ledger.start_step(ALICE, lease, request=b"{}").code == "trial_closed"
    assert ledger.close(ALICE, lease, cleanup_confirmed=True).code == "trial_closed"
    reopened = trial.TrialLedger(connection, enabled=True, clock=lambda: now[0])
    assert reopened.admit(ALICE).code == "trial_already_used"


@pytest.mark.parametrize("seconds", [0, -1, 601, True, 1.5, "600"])
def test_ten_minute_lease_is_a_hard_admission_limit(scene, seconds):
    ledger, _, _ = scene
    assert ledger.admit(ALICE, lease_seconds=seconds).code == "invalid_lease_seconds"


def test_unused_lease_expires_at_exact_deadline_and_is_still_a_consumed_trial(scene):
    ledger, _, now = scene
    lease = admitted(ledger)
    assert lease.expires_at == now[0] + 600
    now[0] = lease.expires_at
    assert ledger.start_step(ALICE, lease, request=b"{}").code == "trial_closed"
    assert ledger.admit(ALICE).code == "trial_already_used"
    assert ledger.admit(BOB).code == "admitted"


@pytest.mark.parametrize("body", [b"", b"a" * (64 * 1024 + 1), "{}", bytearray(b"{}"), None])
def test_request_limit_is_checked_before_a_step_reservation(scene, body):
    ledger, connection, _ = scene
    lease = admitted(ledger)
    assert ledger.start_step(ALICE, lease, request=body).code == "request_size_limit"
    assert connection.execute("SELECT steps FROM devin_trial_sessions").fetchone()[0] == 0


def test_exact_request_byte_limit_is_accepted(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    started(ledger, lease, request=b"a" * (64 * 1024))


@pytest.mark.parametrize("seconds", [0, -1, 61, True, 1.5, "60"])
def test_step_cannot_reserve_more_than_one_minute(scene, seconds):
    ledger, _, _ = scene
    lease = admitted(ledger)
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=seconds).code == "step_time_limit"


def test_total_runtime_is_reserved_before_execution_and_never_refunded(scene):
    ledger, connection, _ = scene
    lease = admitted(ledger)
    for _ in range(2):
        step = started(ledger, lease, seconds=60)
        finished(ledger, lease, step)
    assert connection.execute("SELECT reserved_seconds FROM devin_trial_sessions").fetchone()[0] == 120
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=1).code == "trial_limit_reached"
    assert ledger.admit(BOB).code == "trial_busy", "final history cleanup still required"
    ledger.close(ALICE, lease, cleanup_confirmed=True)
    assert ledger.admit(BOB).code == "admitted"


def test_partial_remaining_runtime_can_only_be_reserved_within_the_budget(scene):
    ledger, _, _ = scene
    lease = admitted(ledger)
    for seconds in (60, 40):
        step = started(ledger, lease, seconds=seconds)
        finished(ledger, lease, step)
    assert ledger.start_step(ALICE, lease, request=b"{}", seconds=21).code == "runtime_reservation_limit"
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
    assert ledger.finish_step(ALICE, lease, first, cleanup_confirmed=True).code == "invalid_step_receipt"
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
    assert ledger.admit(ALICE).code == "trial_already_used"
    assert ledger.admit(BOB).code == "admitted"


@pytest.mark.parametrize("cleanup", [False, None, 1, "true"])
def test_failed_worker_cleanup_pauses_all_future_admissions_even_after_reopen(scene, cleanup):
    ledger, connection, now = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    assert ledger.finish_step(ALICE, lease, step, cleanup_confirmed=cleanup).code == "cleanup_unconfirmed"
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
    for forged in (dataclasses.replace(lease, secret="a" * 64),
                   dataclasses.replace(lease, history_namespace="trial-foreign"),
                   dataclasses.replace(lease, expires_at=lease.expires_at + 600)):
        assert ledger.cancel(ALICE, forged).code == "invalid_trial_lease"
    assert ledger.authorize_step(ALICE, lease, step)


@pytest.mark.parametrize("replacement", [
    {"trial_id": "a" * 64}, {"step_id": "b" * 64}, {"history_namespace": "trial-foreign"},
    {"deadline": 1_900_000_000.0}, {"reserved_seconds": 60}, {"reserved_seconds": True},
])
def test_forged_permit_cannot_authorize_or_submit_a_cleanup_receipt(scene, replacement):
    ledger, _, _ = scene
    lease = admitted(ledger)
    step = started(ledger, lease)
    forged = dataclasses.replace(step, **replacement)
    assert not ledger.authorize_step(ALICE, lease, forged)
    assert ledger.finish_step(ALICE, lease, forged, cleanup_confirmed=True).code == "invalid_step_receipt"
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
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("customer_balances",)]
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
