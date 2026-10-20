"""Real dedicated SQLite plus fake worker; no provider, model, SSH or credentials."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import devin_trial, trial_runtime
from ccfleetd.config import Config
from ccfleetd.store import Store
from ccfleetd.trial_worker_transport import MODEL
from tests.test_usersite import ssh_key


def body(**changes):
    return json.dumps(
        {
            "model": MODEL,
            "max_tokens": 100,
            "stream": False,
            "messages": [{"role": "user", "content": "PRIVATE PROMPT"}],
            "tools": [
                {"name": "Read", "description": "Local only", "input_schema": {"type": "object"}}
            ],
            **changes,
        }
    ).encode()


def receipt(amount=10, **changes):
    return {
        "version": 1,
        "cleanup_confirmed": True,
        "metering": "complete",
        "usage": {
            "input_tokens": amount,
            "output_tokens": 2,
            "cache_read_tokens": 3,
            "cache_creation_tokens": 4,
        },
        "message": {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [{"type": "text", "text": "PRIVATE REPLY"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": amount,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            },
        },
        "error": None,
        **changes,
    }


class FakeWorker:
    def __init__(self, target, cfg):
        self.target, self.cfg = target, cfg
        self.model_started, self.closed = False, False

    def perform(self, raw, authorized):
        assert authorized() is True
        self.model_started = True
        return receipt()

    def close(self):
        self.closed = True


@pytest.fixture
def trial(tmp_path):
    now = [float(20000 * 86400 + 100)]
    directory = tmp_path / "trial"
    directory.mkdir(mode=0o700)
    worker = tmp_path / "worker.json"
    worker.write_text(
        json.dumps(
            {
                "access_host": "worker.invalid",
                "access_port": 22,
                "unix_user": "trial",
                "ssh_host_key": ssh_key(),
                "policy_sha256": "a" * 64,
            }
        )
    )
    worker.chmod(0o600)
    cfg = replace(
        Config(),
        devin_trial_enabled=True,
        devin_trial_directory=str(directory),
        devin_trial_worker_file=str(worker),
        devin_trial_ssh_key_file="/unused-trial-key",
        public_url="https://fleet.invalid",
        cookie_secret="c" * 32,
        google_client_id="client",
        google_client_secret="secret",
    )
    store = Store(":memory:")
    alice = store.upsert_account_from_google("google-alice", "alice@example.invalid", now=now[0])
    bob = store.upsert_account_from_google("google-bob", "bob@example.invalid", now=now[0])
    runtime = trial_runtime.TrialRuntime(cfg, store, transport=FakeWorker, clock=lambda: now[0])
    yield SimpleNamespace(
        runtime=runtime,
        store=store,
        cfg=cfg,
        now=now,
        alice=alice,
        bob=bob,
        directory=directory,
        worker=worker,
    )
    store.close()


def test_disabled_default_does_no_file_or_transport_work(monkeypatch):
    monkeypatch.setattr(trial_runtime, "TrialRuntime", lambda *a: pytest.fail("disabled IO"))
    assert trial_runtime.build(Config(), object()) is None
    assert (
        trial_runtime.build(replace(Config(), broker_only=True, devin_trial_enabled=True), object())
        is None
    )


def test_complete_turn_counts_all_cache_usage_and_never_changes_product_store(trial):
    t = trial
    before = list(t.store._conn.iterdump())
    token = t.runtime.create_key(t.alice)
    message, streaming = t.runtime.turn(token, body(metadata={"user_id": "PRIVATE_ID"}))
    assert message["content"][0]["text"] == "PRIVATE REPLY" and streaming is False
    allowance = t.runtime.allowance(t.alice)
    assert allowance["reported_tokens"] == 19 and allowance["reserved_tokens"] == 0
    assert t.runtime.allowance(t.bob)["reported_tokens"] == 0
    assert list(t.store._conn.iterdump()) == before
    for name in ("allowance.sqlite3", "access.sqlite3"):
        raw = (t.directory / name).read_bytes()
        assert b"PRIVATE" not in raw and token.encode() not in raw


def test_stock_inert_placeholder_reaches_worker_without_annotation(trial):
    from tests.test_stock_native_request import stock_body

    captured = []

    class Capture(FakeWorker):
        def perform(self, raw, authorized):
            captured.append(json.loads(raw))
            return super().perform(raw, authorized)

    trial.runtime.transport = Capture
    token = trial.runtime.create_key(trial.alice)
    original = stock_body()
    trial.runtime.turn(token, json.dumps(original).encode())
    assert original["tools"][0]["defer_loading"] is True
    assert "defer_loading" not in captured[0]["tools"][0]
    assert captured[0]["tools"][1] == original["tools"][1]
    assert captured[0]["messages"] == original["messages"]


def test_key_lasts_across_midnight_replacement_and_revoke_are_own_scoped(trial):
    t = trial
    a, b = t.runtime.create_key(t.alice), t.runtime.create_key(t.bob)
    t.runtime.turn(a, body())
    t.now[0] += 86400
    assert t.runtime.allowance(t.alice)["reported_tokens"] == 0
    t.runtime.turn(a, body())
    assert t.runtime.allowance(t.alice)["reported_tokens"] == 19
    replacement = t.runtime.create_key(t.alice)
    assert t.runtime.keys.resolve(a) is None and t.runtime.keys.resolve(replacement)
    assert t.runtime.keys.resolve(b)
    t.runtime.revoke(t.alice)
    assert t.runtime.keys.resolve(replacement) is None and t.runtime.keys.resolve(b)
    t.now[0] += 30 * 86400
    assert t.runtime.keys.resolve(b) is None


@pytest.mark.parametrize("bad", [None, "cct_fake", "owner_key", "cct_" + "0" * 64])
def test_trial_key_scope_never_resolves_normal_owner_or_foreign_key(trial, bad):
    with pytest.raises(trial_runtime.TrialUnavailable) as error:
        trial.runtime.turn(bad, body())
    assert error.value.status == 401


def test_non_google_identity_never_becomes_trial_principal(trial):
    with pytest.raises(trial_runtime.TrialUnavailable):
        trial.runtime.create_key({"id": trial.alice["id"], "email": "verified@invalid"})


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "opus"},
        {"model": "other"},
        {"temperature": 1},
        {"max_tokens": True},
        {"tools": []},
    ],
)
def test_unsupported_request_before_reservation_or_worker(trial, changes):
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable) as error:
        trial.runtime.turn(token, body(**changes))
    assert error.value.status == 400
    assert trial.runtime.allowance(trial.alice)["remaining_requests"] == 20


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 5, "output_tokens": 1},
        {
            "input_tokens": 5,
            "output_tokens": 1,
            "cache_read_tokens": None,
            "cache_creation_tokens": 0,
        },
        {
            "input_tokens": True,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
    ],
)
def test_unknown_incomplete_usage_latches_pause_preserves_reservation_no_money(trial, usage):
    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            return receipt(usage=usage)

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable):
        trial.runtime.turn(token, body())
    allowance = trial.runtime.allowance(trial.alice)
    assert allowance["paused"] is True and allowance["reserved_tokens"] == 20000
    assert allowance["reported_tokens"] == 0
    trial.now[0] += 86400
    assert trial.runtime.allowance(trial.bob)["paused"] is True


def test_cleanup_unknown_latches_pause_and_full_usage_is_not_false_zero(trial):
    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            return receipt(cleanup_confirmed=False)

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable):
        trial.runtime.turn(token, body())
    value = trial.runtime.allowance(trial.alice)
    assert value["paused"] and value["reserved_tokens"] == 20000


def test_actual_usage_overshoot_is_charged_fully_and_next_turn_denied(trial):
    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            return receipt(100001)

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    trial.runtime.turn(token, body())
    value = trial.runtime.allowance(trial.alice)
    assert value["reported_tokens"] == 100010 and value["available_tokens"] == 0
    with pytest.raises(trial_runtime.TrialUnavailable) as error:
        trial.runtime.turn(token, body())
    assert error.value.status == 429


def test_one_global_active_turn_without_idle_user_holding_worker(trial):
    entered, release = threading.Event(), threading.Event()
    seen = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            seen.append(raw)
            entered.set()
            assert release.wait(3)
            return receipt()

    trial.runtime.transport = Worker
    a, b = trial.runtime.create_key(trial.alice), trial.runtime.create_key(trial.bob)
    results = []
    thread = threading.Thread(target=lambda: results.append(trial.runtime.turn(a, body())))
    thread.start()
    assert entered.wait(3)
    with pytest.raises(trial_runtime.TrialUnavailable, match="trial_busy"):
        trial.runtime.turn(b, body())
    assert len(seen) == 1
    release.set()
    thread.join(3)
    assert len(results) == 1
    trial.runtime.transport = FakeWorker
    trial.runtime.turn(b, body())
    assert trial.runtime.allowance(trial.bob)["reported_tokens"] == 19


def test_revocation_during_turn_cancels_admission_and_suppresses_reply(trial):
    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            trial.runtime.revoke(trial.alice)
            assert authorized() is False
            return receipt()

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable, match="trial_key_revoked"):
        trial.runtime.turn(token, body())
    assert trial.runtime.allowance(trial.alice)["reported_tokens"] == 19


@pytest.mark.parametrize("started", [False, True])
def test_transport_failure_no_automatic_retry_and_only_proven_presend_refunds(trial, started):
    attempts = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            attempts.append(self)
            self.model_started = started
            raise OSError("PRIVATE_PROVIDER_ERROR")

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable) as error:
        trial.runtime.turn(token, body())
    assert "PRIVATE" not in str(error.value) and len(attempts) == 1 and attempts[0].closed
    assert trial.runtime.allowance(trial.alice)["paused"] is started


def test_runtime_refuses_product_db_and_unsafe_worker_file(trial):
    with sqlite3.connect(trial.directory / "access.sqlite3") as db:
        db.execute("CREATE TABLE wallet_entries(id)")
    with pytest.raises(devin_trial.TrialError):
        trial_runtime.TrialRuntime(trial.cfg, trial.store)
    trial.worker.chmod(0o666)
    with pytest.raises(ValueError, match="trial_worker_file_guard"):
        trial_runtime.TrialRuntime(trial.cfg, trial.store)


@pytest.mark.parametrize(
    "change", ["allowance_link", "access_link", "allowance_foreign_table", "access_foreign_table"]
)
def test_after_start_database_substitution_or_foreign_schema_refuses_before_worker(trial, change):
    token = trial.runtime.create_key(trial.alice)
    name = "allowance.sqlite3" if change.startswith("allowance") else "access.sqlite3"
    path = trial.directory / name
    if change.endswith("link"):
        saved = trial.directory / (name + ".saved")
        path.rename(saved)
        path.symlink_to(saved)
    else:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE wallet_entries(id)")
    with pytest.raises(devin_trial.TrialError):
        trial.runtime.turn(token, body())


def test_trial_key_creation_is_bounded_and_existing_key_not_revoked_on_limit(trial):
    for _ in range(32):
        token = trial.runtime.create_key(trial.alice)
    with pytest.raises(devin_trial.TrialError, match="trial_key_creation_limit"):
        trial.runtime.create_key(trial.alice)
    assert trial.runtime.keys.resolve(token)


@pytest.mark.parametrize(
    "change",
    [
        {"message": {"credential": "PRIVATE_SECRET"}},
        {"message": {**receipt()["message"], "credential": "PRIVATE_SECRET"}},
        {"message": {**receipt()["message"], "model": "other"}},
        {"message": {**receipt()["message"], "usage": {"input_tokens": 0}}},
        {"message": {**receipt()["message"], "content": [{"type": "image", "secret": "PRIVATE"}]}},
    ],
)
def test_response_schema_rejects_unknown_fields_model_content_or_missing_cache_usage(trial, change):
    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            return receipt(**change)

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable, match="trial_result_invalid"):
        trial.runtime.turn(token, body())
    # Real complete usage remains charged even when response serialization fails.
    assert trial.runtime.allowance(trial.alice)["reported_tokens"] == 19


def test_proven_presend_failure_does_not_burn_model_turn_time_or_daily_lease_budget(trial):
    calls = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            calls.append(1)
            assert authorized()
            raise OSError("model-free status unavailable")

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    for _ in range(25):
        with pytest.raises(trial_runtime.TrialUnavailable):
            trial.runtime.turn(token, body())
    assert len(calls) == 25
    value = trial.runtime.allowance(trial.alice)
    assert value["reported_tokens"] == value["reserved_tokens"] == 0
    assert value["remaining_requests"] == 20 and not value["paused"]
    with trial.runtime._ledger() as book:
        assert book.connection.execute(
            "SELECT requests,runtime_seconds,leases FROM devin_trial_global_days"
        ).fetchone() == (0, 0, 0)
        assert (
            book.connection.execute(
                "SELECT COUNT(*) FROM devin_trial_sessions WHERE state='closed'"
            ).fetchone()[0]
            == 25
        )


def test_api_bucket_is_per_account_persistent_across_key_rotation_and_precedes_json(trial):
    first = trial.runtime.create_key(trial.alice)
    for _ in range(3):
        trial.runtime.admit_api(first)
    replacement = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable, match="trial_request_rate_limit"):
        trial.runtime.admit_api(replacement)
    trial.runtime.admit_api(trial.runtime.create_key(trial.bob))
    trial.now[0] += 1.5
    trial.runtime.admit_api(replacement)
    with pytest.raises(trial_runtime.TrialUnavailable, match="trial_request_rate_limit"):
        trial.runtime.admit_api(replacement)
    trial.now[0] += 60
    for _ in range(3):
        trial.runtime.admit_api(replacement)
    with pytest.raises(trial_runtime.TrialUnavailable, match="trial_request_rate_limit"):
        trial.runtime.admit_api(replacement)


def test_runtime_request_boundary_is192kib_not_old64k_and_overbound_never_reserves(trial):
    token = trial.runtime.create_key(trial.alice)
    raw = body()
    exact = raw + b" " * (192 * 1024 - len(raw))
    assert len(exact) == devin_trial.MAX_REQUEST_BYTES == 192 * 1024
    trial.runtime.turn(token, exact)
    with pytest.raises(trial_runtime.TrialUnavailable) as error:
        trial.runtime.turn(token, exact + b" ")
    assert error.value.status == 413
    assert trial.runtime.allowance(trial.alice)["remaining_requests"] == 19
