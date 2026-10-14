"""Idle retention and failure isolation of optional numeric output telemetry."""

import logging

from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier

NOW = 1_792_000_000.0


def test_idle_monitor_prunes_expired_numeric_output_samples(store, cfg):
    store.record_gateway_throughput(500, 10_000, now=NOW - 8 * 86_400)
    assert store._conn.execute("SELECT COUNT(*) FROM gateway_throughput_days").fetchone()[0] == 1
    monitor = Monitor(store, cfg, LogNotifier(), clock=lambda: NOW)
    monitor._maybe_prune(NOW)
    assert store._conn.execute("SELECT COUNT(*) FROM gateway_throughput_days").fetchone()[0] == 0


def test_output_retention_failure_preserves_monitor_and_does_not_log_private_data(
        store, cfg, monkeypatch, caplog):
    def unavailable(_now):
        raise RuntimeError("PRIVATE_TELEMETRY_DETAIL")

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(store, "prune_gateway_throughput", unavailable)
    monitor = Monitor(store, cfg, LogNotifier(), clock=lambda: NOW)
    monitor._maybe_prune(NOW)
    assert monitor._last_prune == NOW
    assert "PRIVATE_TELEMETRY_DETAIL" not in caplog.text
    assert "gateway output-rate retention" in caplog.text
