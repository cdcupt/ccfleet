"""Synthetic numeric metrics only: no provider, model, real profile or network."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ccfleet_agent import relay_metrics as metrics

NOW = 1790712000.0
BINDING = "0123456789abcdef"
OTHER_BINDING = "fedcba9876543210"
SECRET = "request_model_email_ip_path_must_not_escape"


@pytest.fixture
def home(tmp_path):
    directory = tmp_path.resolve() / "slot"
    directory.mkdir(mode=0o750)
    return directory


def target(home):
    return home / ".config/ccfleet/inference-metrics.json"


def record(home, outcome="success", timings=None, now=NOW, binding=BINDING):
    metrics.record(home, binding, outcome, {} if timings is None else timings, now)


def test_missing_report_is_zero_numeric_only_and_read_only(home):
    result = metrics.report(home, BINDING, NOW)
    assert result["requests"] == result["success"] == 0
    assert result["last_success_at"] is None
    assert result["latencies"] == {
        name: {"count": 0, "mean": 0.0, "max": 0.0} for name in metrics.TIMINGS
    }
    assert set(result) == metrics.SUMMARY_KEYS
    assert not (home / ".config").exists()
    assert BINDING not in json.dumps(result) and str(home) not in json.dumps(result)


def test_all_outcomes_and_available_timings_aggregate_without_events(home):
    for index, outcome in enumerate(metrics.OUTCOMES):
        record(home, outcome, {"total_ms": 100 + index}, NOW + index)
    record(home, timings={"connect_ms": 2, "first_byte_ms": 20, "total_ms": 200}, now=NOW + 20)
    record(home, timings={"connect_ms": 4, "first_byte_ms": 40, "total_ms": 300}, now=NOW + 21)
    result = metrics.report(home, BINDING, NOW + 30)
    assert result["requests"] == 10 and result["success"] == 3
    assert all(result[name] == 1 for name in metrics.OUTCOMES if name != "success")
    assert result["last_success_at"] == NOW + 21
    assert result["latencies"]["connect_ms"] == {"count": 2, "mean": 3.0, "max": 4.0}
    assert result["latencies"]["first_byte_ms"] == {"count": 2, "mean": 30.0, "max": 40.0}
    assert result["latencies"]["total_ms"] == {"count": 10, "mean": 132.8, "max": 300.0}
    stored = json.loads(target(home).read_text())
    assert set(stored) == {"version", "bound_fp", "buckets"}
    assert len(stored["buckets"]) == 1 and "events" not in target(home).read_text()
    assert target(home).stat().st_mode & 0o777 == 0o600
    assert (target(home).parent / metrics.LOCK_NAME).stat().st_mode & 0o777 == 0o600
    assert target(home).parent.stat().st_mode & 0o777 == 0o700
    assert (home / ".config").stat().st_mode & 0o777 == 0o700
    assert home.stat().st_mode & 0o777 == 0o750
    assert len(target(home).read_bytes()) <= metrics.MAX_BYTES


def test_existing_owned_nonwritable_shared_config_mode_is_preserved(home):
    directory = home / ".config/ccfleet"
    directory.mkdir(parents=True, mode=0o755)
    directory.chmod(0o755)
    record(home)
    assert directory.stat().st_mode & 0o777 == 0o755
    assert target(home).stat().st_mode & 0o777 == 0o600


def test_same_day_out_of_order_writers_do_not_lose_counts(home):
    record(home, now=NOW + 1)
    record(home, now=NOW)
    result = metrics.report(home, BINDING, NOW + 2)
    assert result["success"] == result["requests"] == 2
    assert result["last_success_at"] == NOW + 1
    assert metrics.report(home, BINDING, NOW)["requests"] == 0


def test_rounding_accumulation_does_not_make_valid_metrics_inconsistent(home):
    for _ in range(10):
        record(home, timings={"connect_ms": 1.0005, "total_ms": 0.0005})
    result = metrics.report(home, BINDING, NOW)
    assert result["requests"] == 10
    assert result["latencies"]["connect_ms"]["mean"] <= result["latencies"]["connect_ms"]["max"]


def test_seven_day_calendar_retention_prunes_on_write_and_report(home):
    for day in range(12):
        record(home, now=NOW + day * metrics.DAY_SECONDS)
    result = metrics.report(home, BINDING, NOW + 11 * metrics.DAY_SECONDS)
    assert result["requests"] == 7 and result["window_days"] == 7
    stored = json.loads(target(home).read_text())
    assert len(stored["buckets"]) == 7
    before = target(home).read_bytes()
    assert metrics.report(home, BINDING, NOW + 18 * metrics.DAY_SECONDS)["requests"] == 0
    assert target(home).read_bytes() == before


def test_future_day_buckets_are_not_reported_and_are_pruned_before_record(home):
    record(home, now=NOW + metrics.DAY_SECONDS)
    assert metrics.report(home, BINDING, NOW)["requests"] == 0
    record(home, now=NOW)
    stored = json.loads(target(home).read_text())
    assert set(stored["buckets"]) == {str(int(NOW // metrics.DAY_SECONDS))}
    assert metrics.report(home, BINDING, NOW)["requests"] == 1


def test_changed_account_has_no_old_usage_or_exported_account_tag(home):
    record(home)
    before = target(home).read_bytes()
    changed = metrics.report(home, OTHER_BINDING, NOW)
    assert changed["requests"] == 0 and target(home).read_bytes() == before
    record(home, "auth_errors", binding=OTHER_BINDING)
    result = metrics.report(home, OTHER_BINDING, NOW)
    assert result["requests"] == result["auth_errors"] == 1 and result["success"] == 0
    assert json.loads(target(home).read_text())["bound_fp"] == OTHER_BINDING
    assert BINDING not in json.dumps(result) and OTHER_BINDING not in json.dumps(result)


@pytest.mark.parametrize(
    "binding",
    [None, True, "", "ABCDEF0123456789", "a" * 15, "a" * 17, "../a", SECRET, {"fp": BINDING}],
)
def test_invalid_account_binding_never_creates_files(home, binding):
    for operation in (
        lambda: metrics.record(home, binding, "success", {}, NOW),
        lambda: metrics.report(home, binding, NOW),
    ):
        with pytest.raises(metrics.MetricsError):
            operation()
    assert not (home / ".config").exists()


@pytest.mark.parametrize(
    "now", [-1, True, None, "now", float("inf"), float("nan"), metrics.MAX_EPOCH + 1, 10**400]
)
def test_invalid_time_never_creates_files(home, now):
    with pytest.raises(metrics.MetricsError):
        record(home, now=now)
    with pytest.raises(metrics.MetricsError):
        metrics.report(home, BINDING, now)
    assert not (home / ".config").exists()


@pytest.mark.parametrize("outcome", [None, False, "requests", "model", SECRET, {}, "success\n"])
def test_unknown_outcomes_never_persist_content(home, outcome):
    with pytest.raises(metrics.MetricsError):
        record(home, outcome)
    assert not target(home).exists()


@pytest.mark.parametrize(
    "timings",
    [
        None,
        [],
        {"request_id": SECRET},
        {"model": "opus"},
        {"connect_ms": -1},
        {"total_ms": "1"},
        {"total_ms": True},
        {"first_byte_ms": float("nan")},
        {"total_ms": float("inf")},
        {"total_ms": 3_600_001},
    ],
)
def test_invalid_timing_never_persists_sensitive_or_unbounded_values(home, timings):
    with pytest.raises(metrics.MetricsError):
        metrics.record(home, BINDING, "success", timings, NOW)
    assert not target(home).exists()


@pytest.mark.parametrize("component", ["home", ".config", "ccfleet"])
def test_symlinked_directory_never_followed(home, component):
    outside = home.parent / "outside"
    outside.mkdir()
    if component == "home":
        path = home.parent / "alias"
        path.symlink_to(home, target_is_directory=True)
        selected = path
    elif component == ".config":
        (home / ".config").symlink_to(outside, target_is_directory=True)
        selected = home
    else:
        (home / ".config").mkdir()
        (home / ".config/ccfleet").symlink_to(outside, target_is_directory=True)
        selected = home
    with pytest.raises(metrics.MetricsError):
        record(selected)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("component", ["home", ".config", "ccfleet"])
def test_other_user_writable_directory_is_refused_without_chmod(home, component):
    directory = home / ".config/ccfleet"
    directory.mkdir(parents=True)
    chosen = (
        home if component == "home" else home / ".config" if component == ".config" else directory
    )
    chosen.chmod(0o777)
    with pytest.raises(metrics.MetricsError):
        record(home)
    assert chosen.stat().st_mode & 0o777 == 0o777
    assert not target(home).exists()


@pytest.mark.parametrize("name", [metrics.FILE_NAME, metrics.LOCK_NAME])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "public"])
def test_unsafe_metrics_and_lock_files_are_refused_without_touching_target(home, name, kind):
    directory = home / ".config/ccfleet"
    directory.mkdir(parents=True, mode=0o700)
    selected = directory / name
    preserve = home / "preserve"
    preserve.write_text(SECRET)
    preserve.chmod(0o600)
    if kind == "symlink":
        selected.symlink_to(preserve)
    elif kind == "hardlink":
        os.link(preserve, selected)
    elif kind == "fifo":
        os.mkfifo(selected, 0o600)
    elif kind == "directory":
        selected.mkdir()
    else:
        selected.write_text("{}")
        selected.chmod(0o644)
    with pytest.raises(metrics.MetricsError):
        record(home)
    if name == metrics.FILE_NAME:
        with pytest.raises(metrics.MetricsError):
            metrics.report(home, BINDING, NOW)
    assert preserve.read_text() == SECRET


def test_wrong_owner_file_is_refused(home, monkeypatch):
    record(home)
    original = os.fstat
    inode = target(home).stat().st_ino

    def changed(fd):
        info = original(fd)
        if info.st_ino == inode:
            parts = list(info)
            parts[4] = os.getuid() + 1
            return os.stat_result(parts)
        return info

    monkeypatch.setattr(metrics.os, "fstat", changed)
    with pytest.raises(metrics.MetricsError):
        record(home)
    with pytest.raises(metrics.MetricsError):
        metrics.report(home, BINDING, NOW)


@pytest.mark.parametrize(
    "data",
    [
        "{" + SECRET,
        "x" * (metrics.MAX_BYTES + 1),
        '{"version":1,"version":1,"bound_fp":"0123456789abcdef","buckets":{}}',
        '{"version":true,"bound_fp":"0123456789abcdef","buckets":{}}',
    ],
)
def test_invalid_or_large_persisted_file_is_not_replaced_or_exposed(home, data):
    target(home).parent.mkdir(parents=True, mode=0o700)
    target(home).write_text(data)
    target(home).chmod(0o600)
    with pytest.raises(metrics.MetricsError) as error:
        record(home)
    assert SECRET not in str(error.value) and target(home).read_text() == data
    with pytest.raises(metrics.MetricsError):
        metrics.report(home, BINDING, NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("counts", {"requests": 1}),
        ("updated_at", NOW + metrics.DAY_SECONDS),
        ("last_success_at", NOW + 1),
        ("timings", {"body": SECRET}),
    ],
)
def test_malformed_persisted_bucket_fails_closed(home, field, value):
    record(home)
    stored = json.loads(target(home).read_text())
    next(iter(stored["buckets"].values()))[field] = value
    target(home).write_text(json.dumps(stored))
    with pytest.raises(metrics.MetricsError):
        metrics.report(home, BINDING, NOW)


def test_partial_writes_complete_and_zero_progress_preserves_previous_file(home, monkeypatch):
    original = os.write

    def partial(fd, data):
        return original(fd, data[:7])

    monkeypatch.setattr(metrics.os, "write", partial)
    record(home)
    before = target(home).read_bytes()
    monkeypatch.setattr(metrics.os, "write", lambda *args: 0)
    with pytest.raises(metrics.MetricsError):
        record(home)
    assert target(home).read_bytes() == before
    assert not list(target(home).parent.glob(".inference-metrics-*"))


def test_failed_atomic_replace_preserves_existing_report(home, monkeypatch):
    record(home)
    before = target(home).read_bytes()

    def fail(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(metrics.os, "replace", fail)
    with pytest.raises(metrics.MetricsError) as error:
        record(home)
    assert SECRET not in str(error.value) and target(home).read_bytes() == before
    assert not list(target(home).parent.glob(".inference-metrics-*"))


def test_locked_writer_is_bounded_and_report_never_waits_for_writer(home, monkeypatch):
    record(home)
    monkeypatch.setattr(metrics, "LOCK_WAIT_SECONDS", 0.02)
    fd = os.open(target(home).parent / metrics.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(metrics.MetricsError, match="busy"):
            record(home)
        assert metrics.report(home, BINDING, NOW)["requests"] == 1
    finally:
        os.close(fd)


def test_real_concurrent_processes_do_not_lose_updates(home, monkeypatch):
    # The full suite can run with a non-repository cwd or a customized
    # PYTHONPATH. Load the exact helper under isolated Python, not an ambient
    # package with the same name. Keep the real multi-process locking test.
    (home / "ccfleet_agent.py").write_text("raise RuntimeError('untrusted import shadow')\n")
    monkeypatch.chdir(home)
    monkeypatch.setenv("PYTHONPATH", str(home))
    source = str(Path(metrics.__file__).resolve())
    script = (
        "import importlib.util, sys; from pathlib import Path; "
        "spec = importlib.util.spec_from_file_location('synthetic_relay_metrics', sys.argv[2]); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "[(module.record(Path(sys.argv[1]), sys.argv[3], 'success', "
        "{'total_ms': 10}, float(sys.argv[4]))) "
        "for _ in range(15)]"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", script, str(home), source, BINDING, str(NOW)],
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(6)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, (stdout + stderr).decode("utf-8", "replace")
        result = metrics.report(home, BINDING, NOW)
        assert result["requests"] == result["success"] == 90
        assert result["latencies"]["total_ms"] == {"count": 90, "mean": 10.0, "max": 10.0}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


def test_validate_report_is_pure_detached_and_strictly_numeric(home, monkeypatch):
    record(home, timings={"total_ms": 12})
    original = metrics.report(home, BINDING, NOW)

    def forbidden(*args, **kwargs):
        pytest.fail("report validation must not access any filesystem or network")

    monkeypatch.setattr(metrics.os, "open", forbidden)
    clean = metrics.validate_report(original)
    clean["latencies"]["total_ms"]["mean"] = 0
    assert original["latencies"]["total_ms"]["mean"] == 12
    assert "bound_fp" not in clean
    assert all(type(clean[key]) is int for key in metrics.COUNTERS)


@pytest.mark.parametrize(
    "key,value",
    [
        ("bound_fp", BINDING),
        ("model", SECRET),
        ("requests", True),
        ("requests", -1),
        ("success", 2),
        ("observed_at", "now"),
        ("observed_at", float("nan")),
        ("last_success_at", NOW + 1),
        ("last_success_at", NOW - 8 * metrics.DAY_SECONDS),
        ("window_days", 8),
        ("version", True),
    ],
)
def test_validate_report_rejects_unsafe_or_inconsistent_fields(home, key, value):
    record(home)
    raw = metrics.report(home, BINDING, NOW)
    raw[key] = value
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(raw)


@pytest.mark.parametrize(
    "sample",
    [
        {"count": True, "mean": 1, "max": 1},
        {"count": 2, "mean": 1, "max": 1},
        {"count": 0, "mean": 1, "max": 1},
        {"count": 1, "mean": 2, "max": 1},
        {"count": 1, "mean": "1", "max": 1},
        {"count": 1, "mean": float("inf"), "max": float("inf")},
        {"count": 1, "mean": 1, "max": 1, "request_id": SECRET},
    ],
)
def test_validate_report_rejects_invalid_latency_samples(home, sample):
    record(home)
    raw = metrics.report(home, BINDING, NOW)
    raw["latencies"]["total_ms"] = sample
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(raw)


def test_reports_never_serialize_account_tag_paths_or_other_unrequested_values(home):
    record(home)
    raw = metrics.report(home, BINDING, NOW)
    tainted = copy.deepcopy(raw)
    tainted["email"] = SECRET
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(tainted)
    serialized = json.dumps(metrics.validate_report(raw))
    assert all(
        value not in serialized for value in (BINDING, str(home), SECRET, "slot01", "api.anthropic")
    )


@pytest.mark.parametrize("selected", [None, "relative/path", "/", "/tmp/../private"])
def test_invalid_home_never_exposes_a_raw_path_error(selected):
    with pytest.raises(metrics.MetricsError):
        record(selected)
    with pytest.raises(metrics.MetricsError):
        metrics.report(selected, BINDING, NOW)


def test_counter_overflow_preserves_previous_store(home):
    record(home)
    stored = json.loads(target(home).read_text())
    counts = next(iter(stored["buckets"].values()))["counts"]
    counts["requests"] = counts["success"] = metrics.MAX_COUNTER
    target(home).write_text(json.dumps(stored))
    before = target(home).read_bytes()
    with pytest.raises(metrics.MetricsError):
        record(home)
    assert target(home).read_bytes() == before


def test_epoch_zero_is_valid_and_does_not_confuse_success_with_missing(home):
    record(home, now=0)
    result = metrics.report(home, BINDING, 0)
    assert result["requests"] == result["success"] == 1
    assert result["last_success_at"] == 0
