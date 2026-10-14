"""Best-effort numeric output-rate observations at the HTTPS gateway.

This is final provider output_tokens divided by the elapsed time between the
first and last nonempty response-body chunks forwarded by the gateway. It is
not first-token latency or an isolated provider generation benchmark. Parsing
and storage failures never change an inference response. No content, model,
account, request identity or exact event timestamp reaches the recorder queue.
"""
from __future__ import annotations

import contextlib
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from ccfleet_agent.relay_metrics import MAX_EPOCH, MAX_USAGE_TOKENS, UsageCollector

DAY_SECONDS = 86400
MIN_STREAM_MS = 100
MAX_STREAM_MS = 3_600_000
QUEUE_CAPACITY = 128


def _number(value: Any, upper: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= upper


@dataclass(frozen=True)
class Sample:
    output_tokens: int
    stream_ms: float


def _valid(sample: Sample) -> bool:
    return (type(sample.output_tokens) is int and 0 <= sample.output_tokens <= MAX_USAGE_TOKENS
            and _number(sample.stream_ms, MAX_STREAM_MS) and sample.stream_ms >= MIN_STREAM_MS)


class StreamObserver:
    """Bounded transient SSE parsing; finish returns at most two numeric fields."""

    def __init__(self, media_type: str, *, clock=time.monotonic) -> None:
        self.clock = clock
        self.first: Optional[float] = None
        self.last: Optional[float] = None
        self.chunks = 0
        self.collector = UsageCollector(media_type) if media_type == "text/event-stream" else None

    def feed(self, part: bytes) -> None:
        if self.collector is None or not part:
            return
        try:
            now = self.clock()
            if (not _number(now, MAX_EPOCH)
                    or self.last is not None and now < self.last):
                self.discard()
                return
            self.first = now if self.first is None else self.first
            self.last = now
            self.chunks = min(2, self.chunks + 1)
            self.collector.feed(part)
        except Exception:
            self.discard()

    def finish(self) -> Optional[Sample]:
        collector, self.collector = self.collector, None
        try:
            if collector is None:
                return None
            usage = collector.finish()
            if (usage is None or self.chunks < 2 or self.first is None or self.last is None):
                return None
            sample = Sample(usage["output_tokens"], (self.last - self.first) * 1000)
            return sample if _valid(sample) else None
        except Exception:
            return None
        finally:
            self.first = self.last = None
            self.chunks = 0

    def discard(self) -> None:
        collector, self.collector = self.collector, None
        if collector is not None:
            with contextlib.suppress(Exception):
                collector.finish()
        self.first = self.last = None
        self.chunks = 0


class Recorder:
    """Lazy, bounded numeric queue; inference threads never wait for storage."""

    def __init__(self, store: Any, *, capacity: int = QUEUE_CAPACITY) -> None:
        if type(capacity) is not int or not 1 <= capacity <= QUEUE_CAPACITY:
            raise ValueError("invalid throughput queue capacity")
        self.store = store
        self._queue: queue.Queue[tuple[int, int, float]] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def submit(self, sample: Sample, *, now: Optional[float] = None) -> bool:
        try:
            observed = time.time() if now is None else now
            if not _valid(sample) or not _number(observed, MAX_EPOCH):
                return False
            item = (int(observed // DAY_SECONDS) * DAY_SECONDS,
                    sample.output_tokens, float(sample.stream_ms))
            if self._closed.is_set() or not self._lock.acquire(blocking=False):
                return False
            try:
                if self._closed.is_set():
                    return False
                if self._thread is None:
                    worker = threading.Thread(target=self._run, name="ccfleet-throughput",
                                              daemon=True)
                    worker.start()
                    self._thread = worker
                if self._closed.is_set():
                    return False
                self._queue.put_nowait(item)
                return True
            finally:
                self._lock.release()
        except Exception:
            return False

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                day, tokens, stream_ms = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not self._closed.is_set():
                    writer = getattr(self.store, "try_record_gateway_throughput", None)
                    if callable(writer):
                        for attempt in range(3):
                            if self._closed.is_set():
                                break
                            # Only exact False means the storage operation is
                            # known not to have written. Never retry exceptions
                            # or an ambiguous result, and never retry inference.
                            if writer(tokens, stream_ms, now=day) is not False:
                                break
                            if attempt == 2 or self._closed.wait(0.01):
                                break
                    else:
                        self.store.record_gateway_throughput(tokens, stream_ms, now=day)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 1) -> bool:
        """Stop accepting samples; discard pending work and join with a hard bound."""
        self._closed.set()
        wait = min(float(timeout), 5) if _number(timeout, MAX_EPOCH) else 0
        until = time.monotonic() + wait
        if not self._lock.acquire(timeout=wait):
            return False
        try:
            worker = self._thread
        finally:
            self._lock.release()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        if worker is not None:
            worker.join(timeout=max(0, until - time.monotonic()))
        return worker is None or not worker.is_alive()
