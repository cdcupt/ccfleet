"""Infrastructure figures are scoped, numeric-only and independent of traffic."""

import math
import threading
from types import SimpleNamespace

import pytest

from ccfleetd import machine_health as health

NOW = 2_000_000_000.0


def cpu_line(values, guests=(0, 0)):
    return "cpu " + " ".join(str(value) for value in (*values, *guests)) + "\n"


def sources():
    return {
        health.PROC_STAT: cpu_line((100, 20, 40, 400, 30, 5, 5, 0))
        + "cpu0 100 20 40 400 30 5 5 0 0 0\ncpu1 0 0 0 0 0 0 0 0 0 0\n",
        health.PROC_MEMORY: "MemTotal: 8192 kB\nMemFree: 100 kB\nMemAvailable: 6144 kB\n",
        health.PROC_LOAD: "0.50 0.20 0.10 1/100 987654\n",
        health.OWN_CGROUP: "0::/\n",
        health.CPU_STAT: "usage_usec 1000000\nuser_usec 700000\nsystem_usec 300000\n",
        health.CPU_MAX: "100000 100000\n",
        health.MEMORY_CURRENT: "201326592\n",
        health.MEMORY_MAX: "805306368\n",
    }


class Harness:
    def __init__(self):
        self.files = sources()
        self.now = NOW
        self.mono = 100.0
        self.reads = []
        self.disk_reads = []
        self.disk = SimpleNamespace(f_frsize=4096, f_blocks=1000, f_bfree=400, f_bavail=350)
        self.observer = health.Observer(reader=self.read, disk_reader=self.statvfs,
                                        clock=lambda: self.now, monotonic=lambda: self.mono)

    def read(self, path):
        self.reads.append(path)
        result = self.files[path]
        if isinstance(result, Exception):
            raise result
        return result

    def statvfs(self, path):
        self.disk_reads.append(path)
        if isinstance(self.disk, Exception):
            raise self.disk
        return self.disk

    def next(self, seconds=15):
        self.now += seconds
        self.mono += seconds
        self.files[health.PROC_STAT] = cpu_line(
            (130, 25, 50, 450, 40, 7, 8, 10), (1_000_000, 1_000_000)) + (
                "cpu0 0 0 0 0 0 0 0 0\ncpu1 0 0 0 0 0 0 0 0\n")
        self.files[health.CPU_STAT] = "usage_usec 4750000\n"
        self.observer.sample_once()
        return self.snapshot()

    def snapshot(self):
        return self.observer.snapshot(self.now)


def all_metrics(report):
    return [value for group in ("host", "service", "storage")
            for value in report[group].values()]


def test_constructor_and_many_snapshots_do_no_io_and_return_independent_fixed_contract():
    h = Harness()
    before = h.snapshot()
    assert before["state"] == "unavailable"
    assert before["observed_at"] is None
    assert before["interval_s"] == 15
    assert set(before) == {"state", "observed_at", "interval_s", "host", "service", "storage"}
    assert all(value is None for value in all_metrics(before))
    assert not h.reads and not h.disk_reads
    before["host"]["cpu_pct"] = "private injected value"
    assert h.snapshot()["host"]["cpu_pct"] is None
    assert not h.reads and not h.disk_reads


def test_first_sample_reports_ram_and_disk_but_cpu_needs_two_valid_samples():
    h = Harness()
    h.observer.sample_once()
    report = h.snapshot()
    assert report["state"] == "partial"
    assert report["observed_at"] == NOW
    assert report["host"]["cpu_pct"] is None
    assert report["host"]["io_wait_pct"] is None
    assert report["host"]["steal_pct"] is None
    assert report["service"]["cpu_budget_pct"] is None
    assert report["host"]["logical_cpus"] == 2
    assert report["host"]["load_1m"] == 0.5
    assert report["host"]["memory_total_bytes"] == 8192 * 1024
    assert report["host"]["memory_used_bytes"] == 2048 * 1024
    assert report["host"]["memory_pct"] == 25
    assert report["service"]["memory_used_bytes"] == 201326592
    assert report["service"]["memory_limit_bytes"] == 805306368
    assert report["service"]["memory_pct"] == 25
    assert report["storage"] == {
        "total_bytes": 4096000, "used_bytes": 2457600,
        "available_bytes": 1433600, "used_pct": 60,
    }
    assert h.disk_reads == ["/data"]
    assert set(h.reads) == set(health._FILE_LIMITS)


def test_two_samples_exclude_guest_double_count_and_distinguish_wait_steal_and_quota():
    h = Harness()
    h.observer.sample_once()
    report = h.next()
    # Deltas: executing50, idle50, IOwait10, steal10; total120.
    assert report["state"] == "ready"
    assert report["host"]["cpu_pct"] == pytest.approx(50 / 120 * 100)
    assert report["host"]["io_wait_pct"] == pytest.approx(10 / 120 * 100)
    assert report["host"]["steal_pct"] == pytest.approx(10 / 120 * 100)
    assert report["service"]["cpu_quota_cores"] == 1
    assert report["service"]["cpu_budget_pct"] == 25
    # Host logical CPUs are not used as the container quota denominator.
    assert report["host"]["logical_cpus"] == 2


def test_cpu_uses_monotonic_elapsed_when_wall_clock_jumps():
    h = Harness()
    h.observer.sample_once()
    h.now += 10000
    report = h.next()
    assert report["service"]["cpu_budget_pct"] == 25
    assert report["observed_at"] == h.now


@pytest.mark.parametrize("elapsed", [0, -1, 60.001, 10000])
def test_bad_elapsed_or_sampling_gap_requires_new_cpu_baseline(elapsed):
    h = Harness()
    h.observer.sample_once()
    report = h.next(elapsed)
    assert report["host"]["cpu_pct"] is None
    assert report["service"]["cpu_budget_pct"] is None
    assert report["host"]["memory_pct"] == 25
    h.files[health.CPU_STAT] = "usage_usec 8500000\n"
    h.mono += 15
    h.now += 15
    h.observer.sample_once()
    assert h.snapshot()["service"]["cpu_budget_pct"] == 25


def test_negative_counters_unknown_and_next_good_sample_recovers():
    h = Harness()
    h.observer.sample_once()
    h.files[health.PROC_STAT] = cpu_line((0, 0, 0, 0, 0, 0, 0, 0))
    h.files[health.CPU_STAT] = "usage_usec 0\n"
    h.now += 15
    h.mono += 15
    h.observer.sample_once()
    reset = h.snapshot()
    assert reset["host"]["cpu_pct"] is None
    assert reset["service"]["cpu_budget_pct"] is None
    h.files[health.PROC_STAT] = cpu_line((15, 0, 0, 15, 0, 0, 0, 0))
    h.files[health.CPU_STAT] = "usage_usec 3750000\n"
    h.now += 15
    h.mono += 15
    h.observer.sample_once()
    assert h.snapshot()["host"]["cpu_pct"] == 50
    assert h.snapshot()["service"]["cpu_budget_pct"] == 25


def test_invalid_counter_snapshot_cannot_bridge_over_an_unobserved_interval():
    h = Harness()
    h.observer.sample_once()
    h.files[health.PROC_STAT] = "cpu bad counters\n"
    h.files[health.CPU_STAT] = "usage_usec bad\n"
    h.now += 15
    h.mono += 15
    h.observer.sample_once()
    recovered = h.next()
    assert recovered["host"]["cpu_pct"] is None
    assert recovered["service"]["cpu_budget_pct"] is None


def test_idle_and_unchanging_service_counter_are_real_zero_not_unknown():
    h = Harness()
    h.observer.sample_once()
    h.files[health.PROC_STAT] = cpu_line((100, 20, 40, 450, 30, 5, 5, 0))
    h.now += 15
    h.mono += 15
    h.observer.sample_once()
    report = h.snapshot()
    assert report["host"]["cpu_pct"] == 0
    assert report["service"]["cpu_budget_pct"] == 0


def test_no_host_counter_progress_is_unknown():
    h = Harness()
    h.observer.sample_once()
    h.now += 15
    h.mono += 15
    h.observer.sample_once()
    assert h.snapshot()["host"]["cpu_pct"] is None


def test_changed_quota_requires_a_new_service_cpu_baseline():
    h = Harness()
    h.observer.sample_once()
    h.files[health.CPU_MAX] = "50000 100000\n"
    report = h.next()
    assert report["service"]["cpu_quota_cores"] == 0.5
    assert report["service"]["cpu_budget_pct"] is None
    h.files[health.CPU_STAT] = "usage_usec 8500000\n"
    h.now += 15
    h.mono += 15
    h.observer.sample_once()
    assert h.snapshot()["service"]["cpu_budget_pct"] == 50


def test_service_percentages_may_exceed_limit_briefly_without_silent_clamping():
    h = Harness()
    h.observer.sample_once()
    h.files[health.MEMORY_CURRENT] = str(2 * 805306368)
    h.files[health.CPU_STAT] = "usage_usec 31000000\n"
    h.now += 15
    h.mono += 15
    h.observer.sample_once()
    report = h.snapshot()
    assert report["service"]["cpu_budget_pct"] == 200
    assert report["service"]["memory_pct"] == 200


@pytest.mark.parametrize("quota", [
    "max 100000", "0 100000", "100000 0", "100000 100000 extra",
    "-1 100000", "NaN 100000", "100000 1000000001", "999999999999999999999 1",
    "65537 1", "", "1 inf",
])
def test_unlimited_invalid_or_unsafe_quota_never_uses_host_cpu_count(quota):
    h = Harness()
    h.files[health.CPU_MAX] = quota
    h.observer.sample_once()
    report = h.next()
    assert report["service"]["cpu_quota_cores"] is None
    assert report["service"]["cpu_budget_pct"] is None
    assert report["host"]["cpu_pct"] is not None


@pytest.mark.parametrize("namespace", [
    "0::/ancestor/container-id", "1:memory:/container-id", "0::/\n1:memory:/",
    "0::/\n0::/", "0::/../../private", "", "0:://",
])
def test_unknown_namespace_does_not_read_or_publish_ancestor_container_metrics(namespace):
    h = Harness()
    h.files[health.OWN_CGROUP] = namespace
    h.observer.sample_once()
    assert all(value is None for value in h.snapshot()["service"].values())
    assert not set(h.reads) & {health.CPU_STAT, health.CPU_MAX,
                               health.MEMORY_CURRENT, health.MEMORY_MAX}
    assert h.snapshot()["host"]["memory_pct"] == 25


@pytest.mark.parametrize("available", ["9000", "-1", "NaN", "999999999999999999999"])
def test_bad_memavailable_never_falls_back_to_memfree(available):
    h = Harness()
    h.files[health.PROC_MEMORY] = (
        f"MemTotal: 8192 kB\nMemFree: 100 kB\nMemAvailable: {available} kB\n")
    h.observer.sample_once()
    report = h.snapshot()
    assert report["host"]["memory_total_bytes"] == 8192 * 1024
    assert report["host"]["memory_used_bytes"] is None
    assert report["host"]["memory_pct"] is None


@pytest.mark.parametrize("memory", [
    "MemTotal: 8192 kB\nMemAvailable: 4096 MB\n",
    "MemTotal: 8192 kB\nMemAvailable: 4096 kB\nMemAvailable: 4096 kB\n",
    "MemTotal: 8192 kB\nMemAvailable: 4096 kB extra\n",
])
def test_memory_units_and_duplicate_fields_are_not_guessed(memory):
    h = Harness()
    h.files[health.PROC_MEMORY] = memory
    h.observer.sample_once()
    assert h.snapshot()["host"]["memory_pct"] is None


def test_unlimited_memory_preserves_measured_charges_but_limit_percent_unknown():
    h = Harness()
    h.files[health.MEMORY_MAX] = "max\n"
    h.observer.sample_once()
    report = h.snapshot()
    assert report["service"]["memory_used_bytes"] == 201326592
    assert report["service"]["memory_limit_bytes"] is None
    assert report["service"]["memory_pct"] is None


@pytest.mark.parametrize("path,value", [
    (health.PROC_LOAD, "nan 0 0 1/2 3"),
    (health.PROC_LOAD, "inf 0 0 1/2 3"),
    (health.PROC_LOAD, "1000001 0 0 1/2 3"),
    (health.MEMORY_CURRENT, str(2**60 + 1)),
    (health.MEMORY_MAX, "0"),
    (health.CPU_STAT, "usage_usec 1\nusage_usec 2\n"),
    (health.PROC_STAT, "cpu 1 2 3 4 5 6 7 -1\n"),
    (health.PROC_STAT, "cpu 1 2 3 4 5 6 7 8\ncpu 1 2 3 4 5 6 7 8\n"),
])
def test_malformed_metrics_do_not_poison_other_groups_or_emit_nonfinite_values(path, value):
    h = Harness()
    h.files[path] = value
    h.observer.sample_once()
    report = h.next() if path != health.PROC_STAT else h.snapshot()
    assert report["storage"]["used_pct"] == 60
    assert all(value is None or (type(value) in (int, float) and math.isfinite(value))
               for value in all_metrics(report))


@pytest.mark.parametrize("value", [OSError("private path"), RuntimeError("private detail"),
                                  "x" * (16 * 1024 + 1), "秘密", b"bad type"])
def test_missing_failed_oversized_or_nonascii_file_only_hides_its_metrics(value):
    h = Harness()
    h.files[health.PROC_MEMORY] = value
    h.observer.sample_once()
    report = h.snapshot()
    assert report["host"]["memory_total_bytes"] is None
    assert report["service"]["memory_pct"] == 25
    assert report["storage"]["used_pct"] == 60
    assert "private" not in repr(report) and "秘密" not in repr(report)


def test_all_unreadable_sources_are_unavailable_not_zero():
    h = Harness()
    h.files = {path: OSError("private") for path in h.files}
    h.disk = OSError("private")
    h.observer.sample_once()
    report = h.snapshot()
    assert report["state"] == "unavailable"
    assert report["observed_at"] == NOW
    assert all(value is None for value in all_metrics(report))


@pytest.mark.parametrize("field,value", [
    ("f_frsize", 0), ("f_blocks", 0), ("f_blocks", 2**60),
    ("f_bfree", 1001), ("f_bfree", -1), ("f_bavail", 401),
    ("f_bavail", -1), ("f_frsize", True),
])
def test_invalid_filesystem_values_do_not_publish_impossible_space(field, value):
    h = Harness()
    setattr(h.disk, field, value)
    h.observer.sample_once()
    report = h.snapshot()
    assert report["host"]["memory_pct"] == 25
    assert report["storage"]["available_bytes"] is None
    if report["storage"]["used_pct"] is not None:
        assert 0 <= report["storage"]["used_pct"] <= 100


def test_reserved_blocks_are_not_falsely_counted_as_filesystem_used_space():
    h = Harness()
    h.disk.f_bavail = 0
    h.observer.sample_once()
    report = h.snapshot()
    assert report["storage"]["used_pct"] == 60
    assert report["storage"]["available_bytes"] == 0
    assert report["storage"]["used_bytes"] < report["storage"]["total_bytes"]


@pytest.mark.parametrize("age", [60.0001, 10000, -5.0001])
def test_stale_snapshot_retains_age_timestamp_but_hides_every_number(age):
    h = Harness()
    h.observer.sample_once()
    ready = h.next()
    reads = list(h.reads)
    disk_reads = list(h.disk_reads)
    report = h.observer.snapshot(h.now + age)
    assert report["state"] == "stale"
    assert report["observed_at"] == ready["observed_at"]
    assert all(value is None for value in all_metrics(report))
    assert h.reads == reads and h.disk_reads == disk_reads


def test_exact_freshness_boundary_and_small_wall_clock_skew_are_accepted():
    h = Harness()
    h.observer.sample_once()
    assert h.observer.snapshot(h.now + 60)["state"] == "partial"
    assert h.observer.snapshot(h.now - 5)["state"] == "partial"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "private", 2**63])
def test_invalid_render_clock_never_presents_measurements_as_fresh(bad):
    h = Harness()
    h.observer.sample_once()
    report = h.observer.snapshot(bad)
    assert report["state"] == "stale"
    assert all(value is None for value in all_metrics(report))


@pytest.mark.parametrize("clock_name", ["_clock", "_monotonic"])
def test_failed_sample_clock_clears_previous_values_and_cpu_baseline(clock_name):
    h = Harness()
    h.observer.sample_once()
    h.next()
    old = getattr(h.observer, clock_name)
    setattr(h.observer, clock_name, lambda: float("nan"))
    h.observer.sample_once()
    assert h.snapshot()["state"] == "unavailable"
    assert all(value is None for value in all_metrics(h.snapshot()))
    setattr(h.observer, clock_name, old)
    assert h.next()["service"]["cpu_budget_pct"] is None


def test_kernel_reader_caps_bytes_before_decode_and_rejects_unapproved_paths(monkeypatch):
    reads = []

    class Source:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            reads.append(size)
            return b"x" * size

    monkeypatch.setattr("builtins.open", lambda *args: Source())
    with pytest.raises(ValueError, match="oversized"):
        health._read_file(health.PROC_LOAD)
    assert reads == [health._FILE_LIMITS[health.PROC_LOAD] + 1]
    with pytest.raises(KeyError):
        health._read_file("/private/secret")
    assert len(reads) == 1


def test_worker_waits_interruptibly_and_keeps_serving_after_observer_exception(monkeypatch):
    h = Harness()
    h.observer.sample_once()
    stop = threading.Event()
    calls = []
    waits = []
    real_sample = h.observer.sample_once

    def sample():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("private detail")
        real_sample()

    def wait(interval):
        waits.append(interval)
        if len(waits) == 1:
            assert h.snapshot()["state"] == "unavailable"
        else:
            stop.set()

    monkeypatch.setattr(h.observer, "sample_once", sample)
    monkeypatch.setattr(stop, "wait", wait)
    h.observer.run(stop)
    assert len(calls) == 2
    assert waits == [15, 15]
    assert h.snapshot()["state"] == "partial"


def test_already_stopped_worker_does_no_collection():
    h = Harness()
    stop = threading.Event()
    stop.set()
    h.observer.run(stop)
    assert not h.reads and not h.disk_reads
