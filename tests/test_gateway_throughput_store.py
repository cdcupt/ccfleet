"""Anonymous synthetic throughput storage only; no network or provider calls."""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from ccfleet_agent.relay_metrics import (
    DAY_SECONDS,
    MAX_COUNTER,
    MAX_EPOCH,
    MAX_LATENCY_MS,
    MAX_USAGE_TOKENS,
)
from ccfleetd.store import Store, StoreError

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp()
TODAY = "2026-10-13"
START = "2026-10-07"
TABLE = "gateway_throughput_days"


def rows(store):
    return list(store._conn.execute(f"SELECT * FROM {TABLE} ORDER BY day"))


def insert(store, day=TODAY, samples=1, tokens=5, elapsed=125):
    store._conn.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?)", (day, samples, tokens, elapsed))
    store._conn.commit()


def test_empty_summary_is_numeric_read_only_and_has_no_backfill(store):
    before = store._conn.total_changes
    result = store.gateway_throughput_summary(NOW)
    assert result == {"samples": 0, "output_tokens": 0, "stream_ms": 0.0,
                      "window_start_day": START, "collection_start_day": None}
    assert store._conn.total_changes == before
    assert rows(store) == []
    columns = [row[1] for row in store._conn.execute(f"PRAGMA table_info({TABLE})")]
    assert columns == ["day", "samples", "output_tokens_sum", "stream_ms_sum"]


def test_matching_samples_accumulate_without_any_individual_fields(store):
    store.record_gateway_throughput(500, 1000.25, now=NOW)
    store.record_gateway_throughput(250, 500.5, now=NOW + 100)
    assert store.gateway_throughput_summary(NOW + 200) == {
        "samples": 2, "output_tokens": 750, "stream_ms": 1500.75,
        "window_start_day": START, "collection_start_day": TODAY,
    }
    assert [tuple(row) for row in rows(store)] == [(TODAY, 2, 750, 1500.75)]


def test_zero_tokens_is_an_actual_sample_not_missing_data(store):
    store.record_gateway_throughput(0, 100, now=NOW)
    result = store.gateway_throughput_summary(NOW)
    assert result["samples"] == 1 and result["output_tokens"] == 0
    assert result["stream_ms"] == 100.0 and result["collection_start_day"] == TODAY


@pytest.mark.parametrize("tokens", [None, True, False, "5", 5.0, -1, MAX_USAGE_TOKENS + 1,
                                    float("nan"), float("inf"), 10**400])
def test_invalid_token_samples_leave_no_rows(store, tokens):
    with pytest.raises(StoreError, match="invalid gateway throughput sample"):
        store.record_gateway_throughput(tokens, 125, now=NOW)
    assert rows(store) == []


@pytest.mark.parametrize("elapsed", [None, True, False, "125", -1, 0, 99.99,
                                     MAX_LATENCY_MS + 1, float("nan"), float("inf"), 10**400])
def test_invalid_duration_samples_leave_no_rows(store, elapsed):
    with pytest.raises(StoreError, match="invalid gateway throughput sample"):
        store.record_gateway_throughput(5, elapsed, now=NOW)
    assert rows(store) == []


@pytest.mark.parametrize("now", [None, True, False, "now", -1, MAX_EPOCH + 1,
                                 float("nan"), float("inf"), 10**400])
def test_all_methods_reject_invalid_clocks_without_mutation(store, now):
    for call in (lambda: store.record_gateway_throughput(5, 125, now=now),
                 lambda: store.gateway_throughput_summary(now),
                 lambda: store.prune_gateway_throughput(now)):
        with pytest.raises(StoreError, match="invalid gateway throughput clock"):
            call()
    assert rows(store) == []


def test_upper_sample_bounds_and_epoch_edges_are_supported(store):
    store.record_gateway_throughput(MAX_USAGE_TOKENS, MAX_LATENCY_MS, now=0)
    assert store.gateway_throughput_summary(0)["collection_start_day"] == "1970-01-01"
    store.record_gateway_throughput(MAX_USAGE_TOKENS, MAX_LATENCY_MS, now=MAX_EPOCH)
    result = store.gateway_throughput_summary(MAX_EPOCH)
    assert result["samples"] == 1 and result["collection_start_day"] == "9999-12-31"


def test_utc_midnight_changes_day_and_window_not_the_sample_assignment(store):
    midnight = int(NOW // DAY_SECONDS) * DAY_SECONDS
    store.record_gateway_throughput(10, 100, now=midnight - 0.01)
    store.record_gateway_throughput(20, 200, now=midnight)
    assert [tuple(row) for row in rows(store)] == [
        ("2026-10-12", 1, 10, 100.0), (TODAY, 1, 20, 200.0),
    ]
    result = store.gateway_throughput_summary(midnight)
    assert result["samples"] == 2 and result["stream_ms"] == 300.0
    assert result["window_start_day"] == START and result["collection_start_day"] == "2026-10-12"


def test_late_previous_day_writer_does_not_discard_the_newer_days_samples(store):
    store.record_gateway_throughput(10, 125, now=NOW + DAY_SECONDS)
    store.record_gateway_throughput(20, 150, now=NOW)
    result = store.gateway_throughput_summary(NOW + DAY_SECONDS)
    assert result["samples"] == 2 and result["output_tokens"] == 30
    assert result["stream_ms"] == 275 and result["collection_start_day"] == TODAY


def test_write_prunes_old_days_and_collection_date_means_retained_samples(store):
    for day in range(8):
        store.record_gateway_throughput(day, 100, now=NOW + day * DAY_SECONDS)
    assert len(rows(store)) == 7
    result = store.gateway_throughput_summary(NOW + 7 * DAY_SECONDS)
    assert result["samples"] == 7 and result["output_tokens"] == sum(range(1, 8))
    assert result["window_start_day"] == result["collection_start_day"] == "2026-10-14"


def test_reads_filter_expired_and_future_rows_without_writes_and_idle_prune_expires(store):
    insert(store, day="2026-10-06")
    insert(store, day="2026-10-14")
    before = [tuple(row) for row in rows(store)]
    changes = store._conn.total_changes
    result = store.gateway_throughput_summary(NOW)
    assert result["samples"] == 0 and result["collection_start_day"] is None
    assert [tuple(row) for row in rows(store)] == before and store._conn.total_changes == changes
    assert store.prune_gateway_throughput(NOW) == 1
    assert [row[0] for row in rows(store)] == ["2026-10-14"]
    assert store.prune_gateway_throughput(NOW + 8 * DAY_SECONDS) == 1
    assert rows(store) == []


def test_zero_count_day_does_not_invent_an_earlier_collection_date(store):
    insert(store, day=START, samples=0, tokens=0, elapsed=0)
    store.record_gateway_throughput(5, 100, now=NOW)
    assert store.gateway_throughput_summary(NOW)["collection_start_day"] == TODAY


@pytest.mark.parametrize("field,value", [
    ("day", "2026-10-09x"), ("day", "2026-10-0a"), ("samples", -1), ("samples", 1.5),
    ("samples", "PRIVATE_TAINT"), ("samples", MAX_COUNTER + 1),
    ("output_tokens_sum", -1), ("output_tokens_sum", 1.5),
    ("output_tokens_sum", MAX_USAGE_TOKENS + 1), ("output_tokens_sum", "PRIVATE_TAINT"),
    ("stream_ms_sum", 99), ("stream_ms_sum", MAX_LATENCY_MS + 1),
    ("stream_ms_sum", float("inf")), ("stream_ms_sum", "PRIVATE_TAINT"),
])
def test_tainted_in_window_rows_are_unknown_and_never_silently_repaired(store, field, value):
    row = {"day": TODAY, "samples": 1, "output_tokens_sum": 5, "stream_ms_sum": 125}
    row[field] = value
    insert(store, row["day"], row["samples"], row["output_tokens_sum"], row["stream_ms_sum"])
    before = [tuple(row) for row in rows(store)]
    with pytest.raises(StoreError) as error:
        store.gateway_throughput_summary(NOW)
    assert "PRIVATE_TAINT" not in str(error.value)
    with pytest.raises(StoreError):
        store.record_gateway_throughput(5, 125, now=NOW)
    assert [tuple(row) for row in rows(store)] == before


@pytest.mark.parametrize("kind", ["tokens", "duration", "window"])
def test_overflow_is_refused_atomically(store, kind):
    if kind in ("tokens", "window"):
        samples = (MAX_COUNTER + MAX_USAGE_TOKENS - 1) // MAX_USAGE_TOKENS
        insert(store, day="2026-10-12" if kind == "window" else TODAY,
               samples=samples, tokens=MAX_COUNTER, elapsed=samples * 100)
        token_sample, duration_sample = 1, 100
    else:
        samples = (MAX_COUNTER + MAX_LATENCY_MS - 1) // MAX_LATENCY_MS
        insert(store, samples=samples, tokens=0, elapsed=MAX_COUNTER)
        token_sample, duration_sample = 0, 100
    before = [tuple(row) for row in rows(store)]
    with pytest.raises(StoreError):
        store.record_gateway_throughput(token_sample, duration_sample, now=NOW)
    assert [tuple(row) for row in rows(store)] == before


def test_summary_refuses_window_overflow_across_individually_valid_days(store):
    samples = (MAX_COUNTER + MAX_USAGE_TOKENS - 1) // MAX_USAGE_TOKENS
    insert(store, day="2026-10-12", samples=samples, tokens=MAX_COUNTER, elapsed=samples * 100)
    insert(store, tokens=1, elapsed=100)
    with pytest.raises(StoreError, match="limit"):
        store.gateway_throughput_summary(NOW)


def test_late_sql_failure_rolls_back_aggregation_and_retention(store):
    insert(store, day="2026-10-06")
    insert(store)
    store._conn.executescript(f"CREATE TRIGGER refuse_throughput BEFORE DELETE ON {TABLE} "
                            "BEGIN SELECT RAISE(ABORT, 'synthetic refusal'); END;")
    before = [tuple(row) for row in rows(store)]
    with pytest.raises(sqlite3.IntegrityError, match="synthetic refusal"):
        store.record_gateway_throughput(5, 125, now=NOW)
    assert [tuple(row) for row in rows(store)] == before


def test_two_store_connections_do_not_lose_concurrent_samples(tmp_path):
    path = str(tmp_path / "throughput.db")
    a, b = Store(path), Store(path)
    barrier = threading.Barrier(4)
    def record_many(index):
        barrier.wait()
        for _ in range(50):
            (a if index % 2 else b).record_gateway_throughput(5, 125.5, now=NOW)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(record_many, range(4)))
        expected = {"samples": 200, "output_tokens": 1000, "stream_ms": 25100.0,
                    "window_start_day": START, "collection_start_day": TODAY}
        assert a.gateway_throughput_summary(NOW) == b.gateway_throughput_summary(NOW) == expected
    finally:
        a.close()
        b.close()
    reopened = Store(path)
    try:
        assert reopened.gateway_throughput_summary(NOW) == expected
    finally:
        reopened.close()


def test_existing_account_identity_and_settings_rows_are_unchanged(store):
    store.add_node("synthetic-node", "synthetic-owner", now=NOW)
    store.add_account("synthetic-account", "synthetic-sub", "synthetic@example.com", now=NOW)
    tables = [row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") if row[0] != TABLE]
    snapshot = {table: [tuple(row) for row in store._conn.execute(f"SELECT * FROM {table}")] for table in tables}
    store.record_gateway_throughput(5, 125, now=NOW)
    store.gateway_throughput_summary(NOW)
    store.prune_gateway_throughput(NOW + 8 * DAY_SECONDS)
    assert {table: [tuple(row) for row in store._conn.execute(f"SELECT * FROM {table}")] for table in tables} == snapshot


def test_opening_a_database_without_throughput_adds_only_the_anonymous_table(tmp_path):
    path = str(tmp_path / "old.db")
    original = Store(path)
    try:
        original._conn.execute(f"DROP TABLE {TABLE}")
        original._conn.commit()
        tables = {row[0] for row in original._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        settings = [tuple(row) for row in original._conn.execute("SELECT * FROM settings")]
    finally:
        original.close()
    upgraded = Store(path)
    try:
        assert {row[0] for row in upgraded._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables | {TABLE}
        assert [tuple(row) for row in upgraded._conn.execute("SELECT * FROM settings")] == settings
        assert upgraded.gateway_throughput_summary(NOW)["samples"] == 0
    finally:
        upgraded.close()


def test_try_recorder_never_waits_for_another_thread_holding_store_lock(store):
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with store._lock:
            acquired.set()
            release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert acquired.wait(1)
        began = time.monotonic()
        assert store.try_record_gateway_throughput(5, 125, now=NOW) is False
        assert time.monotonic() - began < 0.5
    finally:
        release.set()
        thread.join(2)
    assert store.gateway_throughput_summary(NOW)["samples"] == 0


def test_try_recorder_refuses_to_commit_an_existing_caller_transaction(store):
    timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    store._conn.execute("BEGIN IMMEDIATE")
    store._conn.execute("INSERT INTO settings (key,value,updated_at,updated_by) "
                        "VALUES ('synthetic-caller','pending',0,'')")
    assert store.try_record_gateway_throughput(5, 125, now=NOW) is False
    assert store._conn.in_transaction
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == timeout
    store._conn.rollback()
    assert store._conn.execute("SELECT value FROM settings WHERE key='synthetic-caller'").fetchone() is None
    assert store.gateway_throughput_summary(NOW)["samples"] == 0


def test_try_recorder_external_writer_is_prompt_and_restores_timeout(tmp_path):
    path = str(tmp_path / "busy.db")
    store = Store(path)
    writer = sqlite3.connect(path)
    try:
        store._conn.execute("PRAGMA busy_timeout = 2300")
        writer.execute("BEGIN IMMEDIATE")
        began = time.monotonic()
        assert store.try_record_gateway_throughput(5, 125, now=NOW) is False
        assert time.monotonic() - began < 0.5
        assert not store._conn.in_transaction
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2300
        # WAL readers and the shared Store lock remain usable while the other
        # connection still holds its write transaction.
        began = time.monotonic()
        assert store.gateway_throughput_summary(NOW)["samples"] == 0
        assert time.monotonic() - began < 0.5
        writer.rollback()
        assert store.try_record_gateway_throughput(5, 125, now=NOW) is True
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2300
        assert store.gateway_throughput_summary(NOW)["samples"] == 1
    finally:
        writer.close()
        store.close()


@pytest.mark.parametrize("message,code,busy", [
    ("database is locked", None, True), ("database table is locked", None, True),
    ("database table is locked: synthetic", None, True),
    ("database schema is locked", None, True), ("database schema is locked: main", None, True),
    ("synthetic message", 5, True), ("synthetic message", 6, True),
    ("synthetic message", 517, True),
    ("not safely locked", None, False), ("database is locked", 1, False),
])
def test_try_recorder_only_returns_false_for_known_busy_and_rolls_back(store, monkeypatch,
                                                                     message, code, busy):
    store._conn.execute("PRAGMA busy_timeout = 1700")
    def failing(*_args, **_kwargs):
        error = sqlite3.OperationalError(message)
        if code is not None:
            error.sqlite_errorcode = code
        if busy:
            store._conn.execute("BEGIN IMMEDIATE")
        raise error
    monkeypatch.setattr(store, "record_gateway_throughput", failing)
    if busy:
        assert store.try_record_gateway_throughput(5, 125, now=NOW) is False
        assert not store._conn.in_transaction
    else:
        with pytest.raises(sqlite3.OperationalError, match=message):
            store.try_record_gateway_throughput(5, 125, now=NOW)
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1700
    assert store.gateway_throughput_summary(NOW)["samples"] == 0


def test_try_recorder_restores_timeout_after_validation_error(store):
    store._conn.execute("PRAGMA busy_timeout = 1900")
    with pytest.raises(StoreError, match="invalid gateway throughput sample"):
        store.try_record_gateway_throughput(True, 125, now=NOW)
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1900
    assert store.gateway_throughput_summary(NOW)["samples"] == 0
