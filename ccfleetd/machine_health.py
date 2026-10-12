"""Cached, numeric-only health of the primary service's Linux machine.

The Linux proc view describes the shared VPS, not its provider's physical
hardware. The cgroup v2 view describes this service and its configured limits,
not reserved resources. The data filesystem may be shared with other services;
statvfs measures capacity, not the size of Fleet's files or disk I/O speed.

Collection uses fixed read-only kernel files and statvfs on /data. It needs no
Docker socket, commands, network, credentials, process inventory or database.
Only an explicitly enabled primary service should run the observer. Rendering
reads its cached snapshot; constructing it performs no I/O.
"""

from __future__ import annotations

import copy
import math
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

INTERVAL_S = 15
STALE_AFTER_S = 60
FUTURE_SKEW_S = 5
MAX_COUNTER = 2**63 - 1
MAX_BYTES = 2**60
MAX_CPUS = 65536
MAX_SERVICE_PCT = 1_000_000
MAX_EPOCH = 253402300799

PROC_STAT = "/proc/stat"
PROC_MEMORY = "/proc/meminfo"
PROC_LOAD = "/proc/loadavg"
OWN_CGROUP = "/proc/self/cgroup"
CPU_STAT = "/sys/fs/cgroup/cpu.stat"
CPU_MAX = "/sys/fs/cgroup/cpu.max"
MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
MEMORY_MAX = "/sys/fs/cgroup/memory.max"
DATA_PATH = "/data"
_FILE_LIMITS = {
    PROC_STAT: 256 * 1024,
    PROC_MEMORY: 16 * 1024,
    PROC_LOAD: 1024,
    OWN_CGROUP: 4096,
    CPU_STAT: 4096,
    CPU_MAX: 1024,
    MEMORY_CURRENT: 1024,
    MEMORY_MAX: 1024,
}
_HOST_FIELDS = (
    "cpu_pct", "io_wait_pct", "steal_pct", "logical_cpus", "load_1m",
    "memory_used_bytes", "memory_total_bytes", "memory_pct",
)
_SERVICE_FIELDS = (
    "cpu_budget_pct", "cpu_quota_cores", "memory_used_bytes", "memory_limit_bytes",
    "memory_pct",
)
_STORAGE_FIELDS = ("used_bytes", "total_bytes", "available_bytes", "used_pct")

Reader = Callable[[str], str]
DiskReader = Callable[[str], Any]
Clock = Callable[[], float]


def _blank() -> dict[str, Any]:
    return {
        "state": "unavailable", "observed_at": None, "interval_s": INTERVAL_S,
        "host": dict.fromkeys(_HOST_FIELDS),
        "service": dict.fromkeys(_SERVICE_FIELDS),
        "storage": dict.fromkeys(_STORAGE_FIELDS),
    }


def _read_file(path: str) -> str:
    """Bound every read before decoding, and reject unexpected file paths."""
    limit = _FILE_LIMITS[path]
    with open(path, "rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversized machine metric")
    return raw.decode("ascii")


def _integer(value: Any, maximum: int = MAX_COUNTER) -> Optional[int]:
    if (not isinstance(value, str) or len(value) > 19
            or re.fullmatch(r"[0-9]+", value) is None):
        return None
    number = int(value)
    return number if number <= maximum else None


def _number(value: Any, maximum: float) -> Optional[float]:
    if type(value) not in (int, float):
        return None
    if not 0 <= value <= maximum or not math.isfinite(value):
        return None
    return float(value)


def _percent(part: Any, whole: Any, maximum: float = 100) -> Optional[float]:
    if part is None or whole is None or whole <= 0:
        return None
    return _number(part / whole * 100, maximum)


def _cpu_counters(text: Optional[str]) -> Optional[tuple[int, ...]]:
    if text is None:
        return None
    lines = [line.split() for line in text.splitlines()
             if line.split() and line.split()[0] == "cpu"]
    if len(lines) != 1 or len(lines[0]) < 9:
        return None
    # The last two guest columns are already included in user and nice.
    fields = tuple(_integer(item) for item in lines[0][1:9])
    return fields if all(item is not None for item in fields) else None


def _logical_cpus(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    names = [line.split()[0] for line in text.splitlines()
             if line.split() and re.fullmatch(r"cpu[0-9]+", line.split()[0])]
    if not names or len(names) > MAX_CPUS or len(names) != len(set(names)):
        return None
    return len(names)


def _memory_value(text: Optional[str], key: str) -> Optional[int]:
    if text is None:
        return None
    matches = [line.split() for line in text.splitlines()
               if line.split() and line.split()[0] == key + ":"]
    if len(matches) != 1 or len(matches[0]) != 3 or matches[0][2] != "kB":
        return None
    value = _integer(matches[0][1], MAX_BYTES // 1024)
    return value * 1024 if value is not None else None


def _load(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    parts = text.split()
    if len(parts) != 5 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", parts[0]) is None:
        return None
    try:
        return _number(float(parts[0]), 1_000_000)
    except (OverflowError, ValueError):
        return None


def _cpu_usage(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    matches = [line.split() for line in text.splitlines()
               if line.split() and line.split()[0] == "usage_usec"]
    if len(matches) != 1 or len(matches[0]) != 2:
        return None
    return _integer(matches[0][1])


def _cpu_quota(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    quota = _integer(parts[0])
    period = _integer(parts[1], 1_000_000_000)
    if quota is None or not quota or period is None or not period:
        return None
    cores = _number(quota / period, MAX_CPUS)
    return cores if cores else None


def _bytes(text: Optional[str]) -> Optional[int]:
    return _integer(text.strip(), MAX_BYTES) if text is not None else None


def _storage(raw: Any) -> dict[str, Any]:
    result = dict.fromkeys(_STORAGE_FIELDS)
    values = [getattr(raw, name, None) for name in
              ("f_frsize", "f_blocks", "f_bfree", "f_bavail")]
    size, blocks, free, available = (
        value if type(value) is int and 0 <= value <= MAX_BYTES else None
        for value in values
    )
    if not size or not blocks or blocks * size > MAX_BYTES:
        return result
    result["total_bytes"] = blocks * size
    if free is not None and free <= blocks:
        result["used_bytes"] = (blocks - free) * size
        result["used_pct"] = _percent(blocks - free, blocks)
        if available is not None and available <= free:
            result["available_bytes"] = available * size
    return result


class Observer:
    """One sampler, many inexpensive readers; no work happens at construction.

    Reader and clocks can be injected for deterministic tests. Sampling failures
    affect individual fields. Counter resets, bad elapsed time and gaps longer
    than the freshness limit require a new CPU baseline. A normal private Docker
    cgroup namespace exposes the service at the cgroup v2 root. Other namespaces
    are deliberately unsupported: never mistake an ancestor for this service.
    """

    def __init__(self, *, reader: Reader = _read_file,
                 disk_reader: DiskReader = os.statvfs,
                 clock: Clock = time.time, monotonic: Clock = time.monotonic) -> None:
        self._reader = reader
        self._disk_reader = disk_reader
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._cached = _blank()
        self._previous: Optional[tuple[float, Optional[tuple[int, ...]],
                                       Optional[int], Optional[float]]] = None

    def _text(self, path: str) -> Optional[str]:
        try:
            value = self._reader(path)
            limit = _FILE_LIMITS[path]
            if not isinstance(value, str) or len(value) > limit or not value.isascii():
                return None
            return value
        except Exception:  # noqa: BLE001 - absent kernel views are an unknown metric
            return None

    def sample_once(self) -> None:
        with self._sample_lock:
            self._sample_locked()

    def _sample_locked(self) -> None:
        report = _blank()
        try:
            observed = _number(self._clock(), MAX_EPOCH)
            mono = _number(self._monotonic(), MAX_COUNTER)
        except Exception:  # noqa: BLE001 - a failed clock must not publish old values
            observed = mono = None
        if observed is None or mono is None:
            self._previous = None
            with self._lock:
                self._cached = report
            return

        host = report["host"]
        cpu_text = self._text(PROC_STAT)
        counters = _cpu_counters(cpu_text)
        host["logical_cpus"] = _logical_cpus(cpu_text)
        host["load_1m"] = _load(self._text(PROC_LOAD))
        memory = self._text(PROC_MEMORY)
        total = _memory_value(memory, "MemTotal")
        available = _memory_value(memory, "MemAvailable")
        host["memory_total_bytes"] = total if total else None
        if total and available is not None and available <= total:
            host["memory_used_bytes"] = total - available
            host["memory_pct"] = _percent(total - available, total)

        service = report["service"]
        usage = quota = None
        # No arbitrary cgroup path is resolved or traversed. In another
        # namespace the root might be an ancestor rather than this container.
        own_group = self._text(OWN_CGROUP)
        if own_group is not None and own_group.strip() == "0::/":
            usage = _cpu_usage(self._text(CPU_STAT))
            quota = _cpu_quota(self._text(CPU_MAX))
            service["cpu_quota_cores"] = quota
            current = _bytes(self._text(MEMORY_CURRENT))
            limit = _bytes(self._text(MEMORY_MAX))
            service["memory_used_bytes"] = current
            service["memory_limit_bytes"] = limit if limit else None
            service["memory_pct"] = _percent(current, limit, MAX_SERVICE_PCT)

        previous = self._previous
        elapsed = mono - previous[0] if previous is not None else 0
        if previous is not None and 0 < elapsed <= STALE_AFTER_S:
            before = previous[1]
            if counters is not None and before is not None:
                delta = tuple(after - old for after, old in zip(counters, before))
                aggregate = sum(delta)
                if aggregate > 0 and all(value >= 0 for value in delta):
                    executing = sum(delta[index] for index in (0, 1, 2, 5, 6))
                    host["cpu_pct"] = _percent(executing, aggregate)
                    host["io_wait_pct"] = _percent(delta[4], aggregate)
                    host["steal_pct"] = _percent(delta[7], aggregate)
            old_usage, old_quota = previous[2:]
            if (usage is not None and old_usage is not None and quota is not None
                    and quota == old_quota and usage >= old_usage):
                service["cpu_budget_pct"] = _percent(
                    (usage - old_usage) / 1_000_000, elapsed * quota, MAX_SERVICE_PCT)
        self._previous = (mono, counters, usage, quota)

        try:
            report["storage"] = _storage(self._disk_reader(DATA_PATH))
        except Exception:  # noqa: BLE001 - missing /data must not affect other metrics
            pass
        metrics = [value for group in ("host", "service", "storage")
                   for value in report[group].values()]
        report["state"] = ("ready" if all(value is not None for value in metrics)
                           else "partial" if any(value is not None for value in metrics)
                           else "unavailable")
        report["observed_at"] = observed
        with self._lock:
            self._cached = report

    def snapshot(self, now: float) -> dict[str, Any]:
        """An independent allowlisted copy, with all stale values suppressed."""
        with self._lock:
            report = copy.deepcopy(self._cached)
        observed = report["observed_at"]
        instant = _number(now, MAX_EPOCH)
        if observed is not None and (
                instant is None or instant - observed > STALE_AFTER_S
                or observed - instant > FUTURE_SKEW_S):
            stale = _blank()
            stale.update(state="stale", observed_at=observed)
            return stale
        return report

    def run(self, stop: threading.Event) -> None:
        """Sample immediately, then wait interruptibly between observations."""
        while not stop.is_set():
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 - an observer can never stop the service
                with self._sample_lock:
                    self._previous = None
                    with self._lock:
                        self._cached = _blank()
            stop.wait(INTERVAL_S)
