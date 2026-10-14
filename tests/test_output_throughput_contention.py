"""Optional telemetry cannot hold authorization reads behind a busy DB writer."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from ccfleetd.store import Store
from ccfleetd.throughput import Recorder, Sample

NOW = 1_792_000_000.0


def test_gateway_telemetry_does_not_block_store_reads_on_external_write_contention(tmp_path):
    path = str(tmp_path / 'fleet.db')
    store = Store(path)
    external = sqlite3.connect(path)
    recorder = Recorder(store)
    entered = threading.Event()
    store._conn.set_trace_callback(lambda sql: entered.set() if sql == 'BEGIN IMMEDIATE' else None)
    external.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=1) as reader:
        try:
            assert recorder.submit(Sample(500, 10_000), now=NOW)
            assert entered.wait(1), 'Numeric writer did not attempt its transaction'
            assert reader.submit(store.list_nodes).result(timeout=1) == []
            with store._lock:
                assert store._conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        finally:
            external.rollback()
            external.close()
            recorder.close()
            store._conn.set_trace_callback(None)
    store.close()
