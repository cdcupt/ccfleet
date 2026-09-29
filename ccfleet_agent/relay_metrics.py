"""Content-free, account-scoped inference aggregates over seven UTC calendar days.

Only slot-user-owned numeric aggregates are persisted. The caller provides the
already verified account binding; this helper never opens a Claude credential,
request, response or history file. ``validate_report`` is pure and can also be
used at the fleet server's heartbeat boundary.

Callers must treat MetricsError as non-fatal and must not retry inference or
change its result merely because recording a metric failed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

WINDOW_DAYS = 7
DAY_SECONDS = 86400
MAX_BYTES = 64 * 1024
MAX_COUNTER = 2**53 - 1
MAX_LATENCY_MS = 3_600_000
MAX_EPOCH = 253402300799
LOCK_WAIT_SECONDS = 0.5
FILE_NAME = "inference-metrics.json"
LOCK_NAME = "inference-metrics.lock"
OUTCOMES = (
    "success",
    "auth_errors",
    "permission_errors",
    "rate_limits",
    "upstream_errors",
    "connection_errors",
    "cancelled",
    "input_errors",
)
COUNTERS = ("requests", *OUTCOMES)
TIMINGS = ("connect_ms", "first_byte_ms", "total_ms")
SUMMARY_KEYS = {"version", "observed_at", "window_days", "last_success_at", "latencies", *COUNTERS}


class MetricsError(ValueError):
    """A fixed-message failure which must never affect an inference result."""


def _number(value: Any, upper: float = MAX_EPOCH) -> bool:
    return type(value) in (int, float) and 0 <= value <= upper and math.isfinite(value)


def _counter(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNTER


def _binding(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{16}", value) is None:
        raise MetricsError("invalid local metrics binding")


def _clock(now: Any) -> float:
    if not _number(now):
        raise MetricsError("invalid metrics observation time")
    return float(now)


def _counts(raw: Any) -> dict[str, int]:
    if (
        not isinstance(raw, dict)
        or set(raw) != set(COUNTERS)
        or any(not _counter(raw[key]) for key in COUNTERS)
        or sum(raw[key] for key in OUTCOMES) != raw["requests"]
    ):
        raise MetricsError("invalid metrics counters")
    return {key: raw[key] for key in COUNTERS}


def _empty_bucket() -> dict[str, Any]:
    return {
        "counts": dict.fromkeys(COUNTERS, 0),
        "last_success_at": None,
        "updated_at": 0.0,
        "timings": {key: {"count": 0, "sum": 0.0, "max": 0.0} for key in TIMINGS},
    }


def _bucket(raw: Any, day: int) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "counts",
        "last_success_at",
        "updated_at",
        "timings",
    }:
        raise MetricsError("invalid metrics bucket")
    counts = _counts(raw["counts"])
    updated = raw["updated_at"]
    if (
        not _number(updated)
        or int(updated // DAY_SECONDS) != day
        or not isinstance(raw["timings"], dict)
        or set(raw["timings"]) != set(TIMINGS)
    ):
        raise MetricsError("invalid metrics bucket time or timings")
    success = raw["last_success_at"]
    if (counts["success"] == 0 and success is not None) or (
        counts["success"] > 0
        and (not _number(success) or success > updated or int(success // DAY_SECONDS) != day)
    ):
        raise MetricsError("invalid metrics success time")
    timings = {}
    for key in TIMINGS:
        value = raw["timings"][key]
        if (
            not isinstance(value, dict)
            or set(value) != {"count", "sum", "max"}
            or not _counter(value["count"])
            or value["count"] > counts["requests"]
            or not _number(value["sum"], MAX_COUNTER * MAX_LATENCY_MS)
            or not _number(value["max"], MAX_LATENCY_MS)
        ):
            raise MetricsError("invalid metrics timing sample")
        if (
            value["count"] == 0
            and (value["sum"] != 0 or value["max"] != 0)
            or value["count"] > 0
            and (
                value["max"] > value["sum"] + 0.001
                or value["sum"] > value["max"] * value["count"] + 0.001
            )
        ):
            raise MetricsError("inconsistent metrics timing sample")
        timings[key] = {
            "count": value["count"],
            "sum": float(value["sum"]),
            "max": float(value["max"]),
        }
    return {
        "counts": counts,
        "updated_at": float(updated),
        "last_success_at": float(success) if success is not None else None,
        "timings": timings,
    }


def _state(raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "bound_fp", "buckets"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
        or not isinstance(raw["buckets"], dict)
        or len(raw["buckets"]) > WINDOW_DAYS
    ):
        raise MetricsError("invalid persisted metrics format")
    _binding(raw["bound_fp"])
    buckets = {}
    for day, value in raw["buckets"].items():
        if (
            not isinstance(day, str)
            or re.fullmatch(r"0|[1-9][0-9]{0,6}", day) is None
            or int(day) > MAX_EPOCH // DAY_SECONDS
        ):
            raise MetricsError("invalid metrics day")
        buckets[day] = _bucket(value, int(day))
    return {"version": 1, "bound_fp": raw["bound_fp"], "buckets": buckets}


def _current(
    state: dict[str, Any], bound_fp: str, now: float, *, hide_future: bool = True
) -> dict[str, Any]:
    today = int(now // DAY_SECONDS)
    # A backward wall-clock jump must not report future events. Hide the whole
    # affected bucket because no individual event log exists. Writers preserve
    # same-day aggregates: concurrent callers can acquire the lock out of order.
    buckets = (
        {}
        if state["bound_fp"] != bound_fp
        else {
            day: bucket
            for day, bucket in state["buckets"].items()
            if 0 <= today - int(day) < WINDOW_DAYS
            and (not hide_future or bucket["updated_at"] <= now)
        }
    )
    return {"version": 1, "bound_fp": bound_fp, "buckets": buckets}


def _directory_open(path: Any, *, dir_fd: Optional[int] = None, readable: bool = False) -> int:
    flags = os.O_RDONLY if readable else getattr(os, "O_PATH", os.O_RDONLY)
    return os.open(path, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _owned_directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise MetricsError("metrics directory is not controlled by the slot user")


def _directory(home: Path, *, create: bool) -> Optional[int]:
    path = Path(home)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise MetricsError("invalid metrics home")
    fd = None
    try:
        fd = _directory_open(path.anchor)
        for part in path.parts[1:]:
            child = _directory_open(part, dir_fd=fd)
            os.close(fd)
            fd = child
        _owned_directory(fd)
        for part in (".config", "ccfleet"):
            try:
                child = _directory_open(part, dir_fd=fd, readable=part == "ccfleet")
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass  # Another writer may have created it; still validate below.
                child = _directory_open(part, dir_fd=fd, readable=part == "ccfleet")
            os.close(fd)
            fd = child
            _owned_directory(fd)
        answer, fd = fd, None
        return answer
    finally:
        if fd is not None:
            os.close(fd)


def _owned_file(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise MetricsError("metrics file is not private and slot-user-owned")
    return info


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MetricsError("duplicate metrics field")
        result[key] = value
    return result


def _load(directory: int, bound_fp: str) -> dict[str, Any]:
    try:
        fd = os.open(FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return {"version": 1, "bound_fp": bound_fp, "buckets": {}}
    try:
        if _owned_file(fd).st_size > MAX_BYTES:
            raise MetricsError("metrics file exceeds the storage limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise MetricsError("metrics file exceeds the storage limit")
        return _state(json.loads(data, object_pairs_hook=_unique_pairs))
    finally:
        os.close(fd)


def _acquire(directory: int) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(LOCK_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    except FileExistsError:
        fd = os.open(LOCK_NAME, flags, dir_fd=directory)
    try:
        _owned_file(fd)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise MetricsError("metrics writer is busy") from exc
                time.sleep(0.01)
    except BaseException:
        os.close(fd)
        raise


def _save(directory: int, state: dict[str, Any]) -> None:
    data = (
        json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    if len(data) > MAX_BYTES:
        raise MetricsError("metrics file exceeds the storage limit")
    temporary = ".inference-metrics-" + uuid.uuid4().hex
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        os.fchmod(fd, 0o600)
        remaining = memoryview(data)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise MetricsError("metrics write made no progress")
            remaining = remaining[count:]
        os.fsync(fd)
        os.replace(temporary, FILE_NAME, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def record(home: Path, bound_fp: str, outcome: str, timings: dict[str, Any], now: float, *,
           still_bound: Optional[Callable[[], bool]] = None) -> None:
    """Record one completed attempt, using milliseconds for available stages only.

    Each call increments requests and exactly one OUTCOMES counter. A failed or
    cancelled attempt is still an attempt; never record it a second time merely
    because this best-effort operation raised MetricsError.
    """
    _binding(bound_fp)
    observed = _clock(now)
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise MetricsError("invalid metrics outcome")
    if (
        not isinstance(timings, dict)
        or set(timings) - set(TIMINGS)
        or any(not _number(value, MAX_LATENCY_MS) for value in timings.values())
    ):
        raise MetricsError("invalid metrics timing")
    directory = lock = None
    try:
        directory = _directory(home, create=True)
        if directory is None:
            raise MetricsError("metrics directory is unavailable")
        lock = _acquire(directory)
        # Check after taking the writer lock, before an old request can reset a
        # newer account's aggregate. A failed guard is never recorded/retried.
        if still_bound is not None:
            try:
                if still_bound() is not True:
                    return
            except Exception as exc:
                raise MetricsError("metrics account binding could not be confirmed") from exc
        state = _current(_load(directory, bound_fp), bound_fp, observed, hide_future=False)
        bucket = state["buckets"].setdefault(str(int(observed // DAY_SECONDS)), _empty_bucket())
        bucket["counts"]["requests"] += 1
        bucket["counts"][outcome] += 1
        bucket["updated_at"] = max(bucket["updated_at"], observed)
        if outcome == "success":
            bucket["last_success_at"] = max(bucket["last_success_at"] or 0, observed)
        for name, value in timings.items():
            sample = bucket["timings"][name]
            measured = round(float(value), 3)
            sample["count"] += 1
            sample["sum"] = round(sample["sum"] + measured, 3)
            sample["max"] = max(sample["max"], measured)
        _state(state)  # Bound counters and cumulative sums before committing.
        _save(directory, state)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, MetricsError):
            raise
        raise MetricsError("metrics could not be recorded safely") from exc
    finally:
        if lock is not None:
            os.close(lock)
        if directory is not None:
            os.close(directory)


def _summary(state: dict[str, Any], now: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": 1,
        "observed_at": now,
        "window_days": WINDOW_DAYS,
        **dict.fromkeys(COUNTERS, 0),
        "last_success_at": None,
    }
    timings = {key: {"count": 0, "sum": 0.0, "max": 0.0} for key in TIMINGS}
    for bucket in state["buckets"].values():
        for key in COUNTERS:
            result[key] += bucket["counts"][key]
        success = bucket["last_success_at"]
        if success is not None and (
            result["last_success_at"] is None or success > result["last_success_at"]
        ):
            result["last_success_at"] = success
        for key in TIMINGS:
            sample = bucket["timings"][key]
            timings[key]["count"] += sample["count"]
            timings[key]["sum"] += sample["sum"]
            timings[key]["max"] = max(timings[key]["max"], sample["max"])
    result["latencies"] = {
        key: {
            "count": sample["count"],
            "mean": round(sample["sum"] / sample["count"], 3) if sample["count"] else 0.0,
            "max": sample["max"],
        }
        for key, sample in timings.items()
    }
    return validate_report(result)


def report(home: Path, bound_fp: str, now: float) -> dict[str, Any]:
    """Read-only aggregate view; a missing or different-account store reports zero."""
    _binding(bound_fp)
    observed = _clock(now)
    directory = None
    try:
        directory = _directory(home, create=False)
        state = (
            _load(directory, bound_fp)
            if directory is not None
            else {"version": 1, "bound_fp": bound_fp, "buckets": {}}
        )
        return _summary(_current(state, bound_fp, observed), observed)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, MetricsError):
            raise
        raise MetricsError("metrics could not be read safely") from exc
    finally:
        if directory is not None:
            os.close(directory)


def validate_report(raw: Any) -> dict[str, Any]:
    """Reject unexpected/nonfinite fields and return a detached numeric-only report."""
    if (
        not isinstance(raw, dict)
        or set(raw) != SUMMARY_KEYS
        or type(raw["version"]) is not int
        or raw["version"] != 1
        or type(raw["window_days"]) is not int
        or raw["window_days"] != WINDOW_DAYS
    ):
        raise MetricsError("invalid inference metrics report")
    now = _clock(raw["observed_at"])
    counts = _counts({key: raw[key] for key in COUNTERS})
    success = raw["last_success_at"]
    first_day = max(0, int(now // DAY_SECONDS) - WINDOW_DAYS + 1) * DAY_SECONDS
    if (counts["success"] == 0 and success is not None) or (
        counts["success"] > 0 and (not _number(success) or not first_day <= success <= now)
    ):
        raise MetricsError("invalid reported metrics success time")
    if not isinstance(raw["latencies"], dict) or set(raw["latencies"]) != set(TIMINGS):
        raise MetricsError("invalid reported metrics latencies")
    latencies = {}
    for key in TIMINGS:
        sample = raw["latencies"][key]
        if (
            not isinstance(sample, dict)
            or set(sample) != {"count", "mean", "max"}
            or not _counter(sample["count"])
            or sample["count"] > counts["requests"]
            or not _number(sample["mean"], MAX_LATENCY_MS)
            or not _number(sample["max"], MAX_LATENCY_MS)
            or sample["mean"] > sample["max"]
            or (sample["count"] == 0 and (sample["mean"] != 0 or sample["max"] != 0))
        ):
            raise MetricsError("invalid reported metrics latency sample")
        latencies[key] = {
            "count": sample["count"],
            "mean": float(sample["mean"]),
            "max": float(sample["max"]),
        }
    return {
        "version": 1,
        "observed_at": now,
        "window_days": WINDOW_DAYS,
        **counts,
        "last_success_at": float(success) if success is not None else None,
        "latencies": latencies,
    }
