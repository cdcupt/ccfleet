"""Conservative cache-only missing meter is distinct from unknown execution."""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from ccfleetd import devin_trial as t
from ccfleetd import trial_runtime
from tests.test_devin_trial_runtime import FakeWorker, body, receipt
from tests.test_devin_trial_runtime import trial as trial

A, B = t.VerifiedAccount("alice", True), t.VerifiedAccount("bob", True)
DAY = 20_000 * 86400


def partial_receipt(known=10, **changes):
    value = receipt(known)
    value["metering"] = "cache_read_unmeasured"
    value["usage"]["cache_read_tokens"] = None
    del value["message"]["usage"]["cache_read_input_tokens"]
    return {**value, **changes}


def test_runtime_verified_clean_cache_only_missing_holds20k_and_releases_for_next_user(trial):
    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            assert authorized()
            self.model_started = True
            return partial_receipt()

    trial.runtime.transport = Worker
    a, b = trial.runtime.create_key(trial.alice), trial.runtime.create_key(trial.bob)
    assert trial.runtime.turn(a, body())[0]["content"]
    allowance = trial.runtime.allowance(trial.alice)
    assert allowance["paused"] is False and allowance["reported_tokens"] == 0
    assert allowance["partial_reported_tokens"] == 16
    assert allowance["unmeasured_reserved_tokens"] == allowance["reserved_tokens"] == 20000
    assert allowance["available_tokens"] == 980_000
    assert allowance["global_available_tokens"] == 9_980_000
    assert trial.runtime.turn(b, body())[0]["content"]
    assert trial.runtime.allowance(trial.bob)["unmeasured_reserved_tokens"] == 20000
    for _ in range(4):
        trial.runtime.turn(a, body())
    # Five conservative 20k holds no longer exhaust the expanded daily budget.
    # Existing independent runtime/turn limits remain covered separately.
    assert trial.runtime.turn(a, body())[0]["content"]
    assert trial.runtime.allowance(trial.alice)["available_tokens"] == 880_000
    assert trial.runtime.allowance(trial.alice)["partial_reported_tokens"] == 96


@pytest.mark.parametrize("known", [0, 1234, 25000, 1_000_001])
def test_durable_partial_marker_no_replay_and_known_overshoot_charged_in_full(tmp_path, known):
    path = tmp_path / "ledger"
    db = sqlite3.connect(path)
    book = t.TrialLedger(db, enabled=True, clock=lambda: DAY + 1)
    lease = book.admit(A).lease
    permit = book.start_step(A, lease, request=b"{}", seconds=60).step
    assert permit.reserved_tokens == 20000
    partial = t.VerifiedCacheReadUnmeasured(known, True)
    assert (
        book.finish_step(A, lease, permit, cleanup_confirmed=True, usage=partial).code
        == "step_finished_unmeasured"
    )
    expected = max(20000, known)
    value = book.allowance(A)
    assert value.unmeasured_reserved_tokens == value.reserved_tokens == expected
    assert value.partial_reported_tokens == known and value.reported_tokens == 0
    assert value.available_tokens == max(0, value.token_limit - expected) and value.paused is False
    old = tuple(db.execute("SELECT * FROM devin_trial_unmeasured").fetchone())
    assert (
        book.finish_step(A, lease, permit, cleanup_confirmed=True, usage=partial).code
        == "invalid_step_receipt"
    )
    assert (
        book.finish_step(B, lease, permit, cleanup_confirmed=True, usage=partial).code
        == "invalid_step_receipt"
    )
    assert (
        book.finish_step(
            A,
            lease,
            dataclasses.replace(permit, reserved_tokens=1),
            cleanup_confirmed=True,
            usage=partial,
        ).code
        == "invalid_step_receipt"
    )
    assert tuple(db.execute("SELECT * FROM devin_trial_unmeasured").fetchone()) == old
    db.close()
    db = sqlite3.connect(path)
    try:
        reopened = t.TrialLedger(db, enabled=True, clock=lambda: DAY + 2)
        assert reopened.allowance(A).unmeasured_reserved_tokens == expected
        tomorrow = t.TrialLedger(db, enabled=True, clock=lambda: DAY + 86401)
        assert tomorrow.allowance(A).available_tokens == 1_000_000
        assert tomorrow.allowance(A).unmeasured_reserved_tokens == 0
        assert tuple(db.execute("SELECT * FROM devin_trial_unmeasured").fetchone()) == old
    finally:
        db.close()


@pytest.mark.parametrize(
    "change",
    [
        {"metering": "unconfirmed"},
        {"metering": "unexpected"},
        {"cleanup_confirmed": False},
        {"error": "native_request_failed"},
        {"message": {"PRIVATE_UNKNOWN": "not a verified reply"}},
        {
            "usage": {
                "input_tokens": None,
                "output_tokens": 2,
                "cache_creation_tokens": 4,
                "cache_read_tokens": None,
            }
        },
        {
            "usage": {
                "input_tokens": 3,
                "output_tokens": None,
                "cache_creation_tokens": 4,
                "cache_read_tokens": None,
            }
        },
        {
            "usage": {
                "input_tokens": 3,
                "output_tokens": 2,
                "cache_creation_tokens": None,
                "cache_read_tokens": None,
            }
        },
        {
            "usage": {
                "input_tokens": True,
                "output_tokens": 2,
                "cache_creation_tokens": 4,
                "cache_read_tokens": None,
            }
        },
    ],
)
def test_partial_escape_hatch_refuses_every_other_unknown_identity_or_cleanup(trial, change):
    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            return partial_receipt(**change)

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable):
        trial.runtime.turn(token, body())
    value = trial.runtime.allowance(trial.alice)
    assert value["paused"] is True and value["reserved_tokens"] == 20000
    assert value["unmeasured_reserved_tokens"] == value["partial_reported_tokens"] == 0


@pytest.mark.parametrize(
    "usage",
    [
        t.VerifiedCacheReadUnmeasured(5, False),
        t.VerifiedCacheReadUnmeasured(True, True),
        t.VerifiedCacheReadUnmeasured(-1, True),
        {"known_tokens": 5, "verified_clean_reply": True},
    ],
)
def test_only_exact_trusted_partial_type_can_settle(usage):
    db = sqlite3.connect(":memory:")
    try:
        book = t.TrialLedger(db, enabled=True, clock=lambda: DAY + 1)
        lease = book.admit(A).lease
        permit = book.start_step(A, lease, request=b"{}").step
        assert (
            book.finish_step(A, lease, permit, cleanup_confirmed=True, usage=usage).code
            == "usage_unaccounted"
        )
        assert book.allowance(A).paused
    finally:
        db.close()


def test_hard_global_twenty_turn_cap_even_complete_zero_usage_cannot_be_bypassed():
    db = sqlite3.connect(":memory:")
    try:
        book = t.TrialLedger(db, enabled=True, clock=lambda: DAY + 1)
        for index in range(20):
            who = t.VerifiedAccount("account" + str(index), True)
            lease = book.admit(who).lease
            permit = book.start_step(who, lease, request=b"{}", seconds=60).step
            assert (
                book.finish_step(
                    who, lease, permit, cleanup_confirmed=True, usage=t.VerifiedUsage(0, True)
                ).code
                == "step_finished"
            )
            book.close(who, lease, cleanup_confirmed=True)
        assert book.admit(A).code == "global_daily_limit_reached"
        assert db.execute(
            "SELECT requests,runtime_seconds FROM devin_trial_global_days"
        ).fetchone() == (20, 1200)
    finally:
        db.close()


@pytest.mark.parametrize("started,cleanup", [(True, True), (None, True), (False, False)])
def test_unsent_refund_requires_exact_transport_and_cleanup_proof(started, cleanup):
    db = sqlite3.connect(":memory:")
    try:
        book = t.TrialLedger(db, enabled=True, clock=lambda: DAY + 1)
        lease = book.admit(A).lease
        step = book.start_step(A, lease, request=b"{}").step
        assert (
            book.finish_unsent(
                A, lease, step, model_started=started, cleanup_confirmed=cleanup
            ).code
            == "invalid_unsent_receipt"
        )
        assert book.allowance(A).reserved_tokens == 20000
    finally:
        db.close()


def test_unsent_refund_is_foreign_safe_idempotent_and_closed_before_expiry_tick():
    now = [DAY + 1.0]
    db = sqlite3.connect(":memory:")
    try:
        book = t.TrialLedger(db, enabled=True, clock=lambda: now[0])
        lease = book.admit(A).lease
        step = book.start_step(A, lease, request=b"{}").step
        assert (
            book.finish_unsent(B, lease, step, model_started=False, cleanup_confirmed=True).code
            == "invalid_unsent_receipt"
        )
        now[0] += 65  # Proven no frame ever sent remains safe despite slow readiness.
        assert (
            book.finish_unsent(A, lease, step, model_started=False, cleanup_confirmed=True).code
            == "step_not_sent"
        )
        assert (
            book.finish_unsent(A, lease, step, model_started=False, cleanup_confirmed=True).code
            == "invalid_unsent_receipt"
        )
        assert not book.allowance(A).paused and book.allowance(A).remaining_requests == 20
        assert book.admit(B).code == "admitted"
    finally:
        db.close()
