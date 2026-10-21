"""Synthetic kernel counters only: no interface/credential/network discovery."""
import json
from pathlib import Path

import pytest

from ccfleet_agent import network_traffic as net
from ccfleetd import traffic
from ccfleetd.config import Config
from ccfleetd.heartbeat import validate_heartbeat
from tests.test_performance import NOW, Fleet, report

BOOT = "01234567-1234-5678-90ab-0123456789ab"


def reading(at=NOW, rx=1000, tx=2000, epoch="a" * 64, scope="machine_nic"):
    return {"version": 1, "scope": scope, "observed_at": at, "counter_epoch": epoch,
            "received_bytes": rx, "sent_bytes": tx}


def line(name, rx, tx):
    return f"{name}: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n"


def test_kernel_collection_counts_only_device_nics_and_never_discloses_names():
    files = {net.PROC_NETWORK: "headers\nheaders\n" + line("lo", 999999, 999999)
             + line("ens_PRIVATE", 1000, 4000) + line("docker0", 9000, 9000)
             + line("veth0", 9000, 9000), net.BOOT_ID: BOOT,
             "/sys/class/net/ens_PRIVATE/ifindex": "2\n"}
    reads = []

    def reader(path, limit=net.MAX_FILE):
        reads.append(path)
        return files[path]

    result = net.collect(now=NOW, reader=reader,
        device_exists=lambda path: path == "/sys/class/net/ens_PRIVATE/device")
    assert result["received_bytes"] == 1000 and result["sent_bytes"] == 4000
    assert result["scope"] == "machine_nic"
    assert "PRIVATE" not in json.dumps(result) and BOOT not in json.dumps(result)
    assert not any("lo/" in path or "veth0/" in path for path in reads)
    container = dict(files)
    container["/sys/class/net/docker0/ifindex"] = "3"
    container["/sys/class/net/veth0/ifindex"] = "4"
    service = net.collect(now=NOW, scope="service_namespace", reader=lambda p, n=0: container[p])
    assert service["received_bytes"] == 19000 and service["sent_bytes"] == 22000
    assert service["scope"] == "service_namespace"
    assert service["counter_epoch"] != result["counter_epoch"]


@pytest.mark.parametrize("broken", ["", "headers\nheaders\nbroken", "headers\nheaders\n../secret: 1",
    "headers\nheaders\n" + line("ens0", -1, 0), "x" * 65537,
    "headers\nheaders\n" + line("ens0", 10**16, 0)])
def test_unusable_kernel_view_is_unknown_not_zero(broken):
    files = {net.PROC_NETWORK: broken, net.BOOT_ID: BOOT,
             "/sys/class/net/ens0/ifindex": "1"}
    assert net.collect(now=NOW, reader=lambda p, n=0: files[p], device_exists=lambda p: True) is None


@pytest.mark.parametrize("value", [None, {}, True, float("nan"), -1, 10**500])
def test_invalid_clocks_are_unavailable(value):
    assert net.collect(now=value if value is not None else -1, reader=lambda *a: "") is None
    assert net.summarize([reading()], value)["state"] == "unavailable"


@pytest.mark.parametrize("change", [{"received_bytes": True}, {"sent_bytes": -1},
    {"sent_bytes": 10**100}, {"observed_at": float("inf")}, {"version": True},
    {"counter_epoch": "BOOT_PRIVATE"}, {"scope": "host"}, {"ip": "PRIVATE"}])
def test_heartbeat_strictly_rejects_bad_counters_and_extra_private_fields(change):
    result = validate_heartbeat({"node_id": "n", "network": {**reading(), **change}}, "n")
    assert "network" not in result
    assert "PRIVATE" not in json.dumps(result)


def test_heartbeat_keeps_only_fixed_validated_network_contract():
    raw = reading()
    result = validate_heartbeat({"node_id": "n", "network": raw}, "n")
    assert result["network"] == raw and result["network"] is not raw


def test_first_sample_needs_baseline_and_real_zero_delta_is_known_zero():
    result = net.summarize([reading(NOW - 15)], NOW)
    assert result["state"] == "warming" and result["received_bytes"] is None
    result = net.summarize([reading(NOW - 15), reading()], NOW)
    assert result["state"] == "ready" and result["coverage_s"] == 15
    assert result["received_bytes"] == result["sent_bytes"] == 0
    assert result["receive_bytes_per_s"] == result["send_bytes_per_s"] == 0


def test_measured_bytes_rates_and_claim_boundary_are_independent_of_kernel_lifetime_counts():
    rows = [reading(NOW - 30, 1_000_000, 2_000_000), reading(NOW - 15, 1_001_500, 2_003_000),
            reading(NOW, 1_004_500, 2_009_000)]
    result = net.summarize(rows, NOW)
    assert result["received_bytes"] == 4500 and result["sent_bytes"] == 9000
    assert result["receive_bytes_per_s"] == 200 and result["send_bytes_per_s"] == 400
    assert result["measurement_started_at"] == NOW - 30 and result["coverage_s"] == 30
    later = net.summarize(rows, NOW, since=NOW - 15)
    assert later["received_bytes"] == 3000 and later["coverage_s"] == 15


def test_reset_reboot_gap_missing_and_out_of_order_never_create_negative_or_guessed_transfer():
    rows = [reading(NOW - 120, 100, 200), reading(NOW - 105, 200, 400),
            reading(NOW - 90, 2, 2), reading(NOW - 75, 10, 10, "b" * 64),
            None, reading(NOW - 45, 100, 100, "b" * 64),
            reading(NOW - 30, 200, 300, "b" * 64), reading(NOW, 500, 700, "b" * 64)]
    result = net.summarize(rows, NOW, max_gap_s=20)
    assert result["received_bytes"] == 200 and result["sent_bytes"] == 400
    assert result["coverage_s"] == 30 and result["resets"] == 2 and result["gaps"] == 2
    assert result["receive_bytes_per_s"] is None
    assert net.summarize([reading(), reading(NOW - 1)], NOW)["coverage_s"] == 0
    stale = net.summarize([reading(NOW - 31), reading(NOW - 16)], NOW, max_gap_s=15)
    assert stale["state"] == "stale" and stale["received_bytes"] is None
    future = net.summarize([reading(NOW + 10)], NOW)
    assert future["state"] == "unavailable"


def test_snapshot_boundary_is_bounded_and_keeps_host_scope():
    raw = {"version": 1, "observations": [reading(NOW - 15), reading(NOW, 2000, 4000)]}
    assert net.validate_snapshot(raw, NOW)["received_bytes"] == 1000
    for bad in ({**raw, "secret": "PRIVATE"}, {**raw, "version": True},
                {**raw, "observations": [reading(scope="service_namespace")]},
                {**raw, "observations": [reading()] * 513}):
        assert net.validate_snapshot(bad, NOW) is None


class TrafficFleet(Fleet):
    def recent_heartbeats(self, node_id, limit=2):
        assert limit <= traffic.MAX_OBSERVATIONS
        latest = self.beats[node_id]
        return [latest, {"ts": NOW - 15, "payload": {"network": reading(NOW - 15)}}]


def test_slot_network_and_api_are_separate_and_no_transcript_content_is_used():
    fleet = TrafficFleet([report(10, 8)])
    fleet.beats["node-0"]["payload"]["network"] = reading(NOW, 2500, 5000)
    result = traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)
    assert result["network"]["received_bytes"] == 1500
    assert result["network"]["receive_bytes_per_s"] == 100
    assert result["api"]["requests"] == 10 and result["api"]["errors"] == 2
    assert result["api"]["tokens"] is None and result["api"]["usage_samples"] is None
    assert result["api_scope"] == "bound_slot_relay"
    assert "private-model" not in json.dumps(result)
    fleet.slots.append({**fleet.slots[0], "id": "other"})
    assert traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)["network_attribution"] == "shared_machine"


def test_slot_claim_and_freshness_cannot_reveal_previous_holder_or_replayed_network():
    fleet = TrafficFleet([report(10, 8)])
    fleet.beats["node-0"]["payload"]["network"] = reading(NOW - 10000)
    result = traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)
    assert result["network"]["received_bytes"] is None
    fleet.slots[0]["claimed_at"] = NOW
    fleet.beats["node-0"]["payload"]["network"] = reading()
    assert traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)["network"]["state"] == "warming"


def test_controller_includes_only_current_bound_active_reports_and_does_not_invent_missing_tokens():
    fleet = TrafficFleet([report(10, 8), report(20, 20)])
    result = traffic.controller_summary(fleet, Config(), NOW)
    assert result["host"]["state"] == result["service"]["state"] == "unavailable"
    assert result["api"]["requests"] == 30 and result["reporting_slots"] == 2
    assert result["api"]["tokens"] is None and result["api"]["usage_samples"] is None
    fleet.entry()["credentials"]["bound_fp"] = "f" * 16
    result = traffic.controller_summary(fleet, Config(), NOW)
    assert result["api"]["requests"] == 20 and result["reporting_slots"] == 1


def test_primary_observer_is_cached_scoped_and_suppresses_stale_values():
    clock = [NOW]
    reads = []
    calls = []
    def collector(**kw):
        calls.append(kw)
        return reading(kw["now"], 1000 + int(kw["now"] - NOW) * 10, 2000,
                       scope=kw["scope"])
    host = {"version": 1, "observations": [reading(NOW - 15), reading()]}
    def reader(path, limit):
        reads.append((path, limit))
        return json.dumps(host)
    observer = traffic.Observer(collector=collector, reader=reader, clock=lambda: clock[0])
    assert observer.snapshot(NOW)["host"]["state"] == "unavailable"
    assert not calls and not reads
    observer.sample_once()
    assert observer.snapshot(NOW)["host"]["state"] == "ready"
    clock[0] += 15
    observer.sample_once()
    result = observer.snapshot(clock[0])
    assert result["service"]["scope"] == "service_namespace"
    assert result["service"]["received_bytes"] == 150
    result["host"]["received_bytes"] = "PRIVATE"
    assert observer.snapshot(clock[0])["host"]["received_bytes"] == 0
    assert len(reads) == 2 and reads[0][0] == traffic.HOST_SNAPSHOT
    assert observer.snapshot(clock[0] + 61)["service"]["state"] == "stale"
    assert observer.snapshot(clock[0] - 6)["service"]["received_bytes"] is None


def test_host_deployment_has_no_network_namespace_or_docker_privilege():
    base = Path(__file__).resolve().parents[1]
    service = (base / "deploy/ccfleet-network-traffic.service").read_text()
    compose = (base / "deploy/host-network-compose.yml").read_text()
    assert "PrivateNetwork" not in service and "ProtectHome=yes" in service
    assert "Group=999" in service and "StateDirectoryMode=0750" in service
    assert "/var/lib/ccfleet-network:/run/ccfleet-network:ro" in compose
    assert "privileged:" not in compose and "network_mode:" not in compose


def host_writer(monkeypatch, tmp_path):
    import os
    directory = tmp_path / "metrics"
    directory.mkdir(mode=0o750)
    target = directory / "network.json"
    original_lstat = os.lstat
    def rooted_lstat(path):
        info = original_lstat(path)
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                              0, 999, info.st_size, info.st_atime, info.st_mtime, info.st_ctime))
    monkeypatch.setattr(net, "HOST_DIRECTORY", str(directory))
    monkeypatch.setattr(net, "HOST_FILE", str(target))
    monkeypatch.setattr(net.os, "lstat", rooted_lstat)
    monkeypatch.setattr(net.os, "geteuid", lambda: 0)
    monkeypatch.setattr(net.os, "getegid", lambda: 999)
    ownership = []
    monkeypatch.setattr(net.os, "fchown", lambda fd, uid, gid: ownership.append((uid, gid)))
    return directory, target, ownership


def test_root_host_writer_preserves_bounded_history_and_atomic_numeric_only_output(monkeypatch, tmp_path):
    directory, target, ownership = host_writer(monkeypatch, tmp_path)
    monkeypatch.setattr(net, "collect", lambda: reading(NOW - 15))
    net.write_host_snapshot()
    assert target.stat().st_mode & 0o777 == 0o640
    monkeypatch.setattr(net, "collect", lambda: reading(NOW, 1500, 3000))
    net.write_host_snapshot()
    raw = json.loads(target.read_text())
    assert len(raw["observations"]) == 2
    assert net.validate_snapshot(raw, NOW)["received_bytes"] == 500
    assert ownership == [(0, 999)] * 2
    assert not list(directory.glob(".network-*"))
    target.write_text("broken JSON")
    net.write_host_snapshot()
    assert len(json.loads(target.read_text())["observations"]) == 1


@pytest.mark.parametrize("kind", ["directory_write", "file_write", "symlink", "hardlink", "not_root"])
def test_root_host_writer_refuses_unsafe_destinations_without_replacing_them(monkeypatch, tmp_path, kind):
    import os
    directory, target, _ = host_writer(monkeypatch, tmp_path)
    monkeypatch.setattr(net, "collect", lambda: reading())
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("PRIVATE")
    if kind == "directory_write":
        directory.chmod(0o770)
    elif kind == "file_write":
        target.write_text("PRIVATE")
        target.chmod(0o660)
    elif kind == "symlink":
        target.symlink_to(sentinel)
    elif kind == "hardlink":
        os.link(sentinel, target)
        target.chmod(0o640)
    else:
        monkeypatch.setattr(net.os, "geteuid", lambda: 10001)
    with pytest.raises(PermissionError):
        net.write_host_snapshot()
    assert sentinel.read_text() == "PRIVATE"
    assert not list(directory.glob(".network-*"))


def test_optional_agent_collector_failure_does_not_break_the_heartbeat(monkeypatch):
    from ccfleet_agent import agent
    monkeypatch.setattr(net, "collect", lambda: reading())
    assert agent.network_summary() == reading()
    def broken():
        raise OSError("PRIVATE")
    monkeypatch.setattr(net, "collect", broken)
    assert agent.network_summary() is None


def test_cached_network_metadata_is_allowlisted_before_private_dto_copy():
    fleet = TrafficFleet([])
    good = net.summarize([reading(NOW - 15), reading(NOW, 2000, 4000)], NOW)
    assert net.validate_summary(good, "machine_nic") == good
    for change in ({"ip": "PRIVATE"}, {"scope": "PRIVATE"}, {"received_bytes": True},
                   {"receive_bytes_per_s": float("inf")}, {"state": "PRIVATE"}):
        result = traffic.controller_summary(fleet, Config(), NOW,
            network_report={"host": {**good, **change}})
        assert result["host"]["state"] == "unavailable"
        assert "PRIVATE" not in json.dumps(result)


def test_counter_epoch_changes_with_reboot_or_interface_identity():
    files = {net.PROC_NETWORK: "headers\nheaders\n" + line("ens0", 100, 200),
             net.BOOT_ID: BOOT, "/sys/class/net/ens0/ifindex": "1"}
    def collect():
        return net.collect(now=NOW, reader=lambda path, limit=0: files[path],
                           device_exists=lambda path: True)
    original = collect()["counter_epoch"]
    files["/sys/class/net/ens0/ifindex"] = "2"
    changed_index = collect()["counter_epoch"]
    files[net.BOOT_ID] = "11234567-1234-5678-90ab-0123456789ab"
    assert len({original, changed_index, collect()["counter_epoch"]}) == 3


def test_overflowing_rate_or_total_is_unknown_and_no_metadata_is_returned():
    result = net.summarize([reading(0, 0, 0), reading(1e-200, net.MAX_COUNTER, 0)], 1e-200)
    assert result["state"] == "unavailable" and result["received_bytes"] is None


def test_api_token_samples_caches_and_observed_totals_remain_independent():
    from tests.test_public_usage import reading as usage_reading
    fleet = TrafficFleet([usage_reading(), usage_reading(cache=False, samples=3, eligible=5)])
    result = traffic.controller_summary(fleet, Config(), NOW)
    assert result["api"]["usage_samples"] == 23
    assert result["api"]["usage_eligible_messages"] == 25
    assert result["api"]["tokens"] == {"input_tokens": 200, "output_tokens": 400,
        "cache_read_input_tokens": 10, "cache_creation_input_tokens": 20, "total_tokens": 630}
    assert result["api"]["token_state"] == "partial"
    first = traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)["api"]
    assert first["token_state"] == "complete" and first["usage_samples"] == 20
    fleet = TrafficFleet([usage_reading(cache=False)])
    result = traffic.controller_summary(fleet, Config(), NOW)["api"]
    assert result["tokens"]["cache_read_input_tokens"] is None
    assert result["tokens"]["cache_creation_input_tokens"] is None
    assert result["tokens"]["total_tokens"] == 300
    assert result["token_state"] == "partial"


def test_stale_api_is_unknown_while_independent_fresh_network_can_still_be_measured():
    fleet = TrafficFleet([report(10, 8, observed=NOW - 10000)])
    fleet.beats["node-0"]["payload"]["network"] = reading(NOW, 2500, 5000)
    result = traffic.slot_summary(fleet, Config(), fleet.slots[0], NOW)
    assert result["network"]["state"] == "ready"
    assert result["api"]["state"] == "unavailable" and result["api"]["requests"] is None


def test_common_machine_facts_carry_network_without_requiring_a_machine_agent_upgrade(monkeypatch):
    from ccfleet_agent import agent
    monkeypatch.setattr(agent, "network_summary", lambda: reading())
    assert agent.system_info()["network"] == reading()
    # Both installed legacy machines and the current machine compose payloads
    # through this existing function, so no lifecycle/provider code needs changing.
    payload = {"node_id": "n", "mode": "machine"}
    payload.update(agent.system_info())
    assert validate_heartbeat(payload, "n")["network"] == reading()
    monkeypatch.setattr(agent, "network_summary", lambda: None)
    assert "network" not in agent.system_info()


@pytest.mark.parametrize("group", [0, -1, True, 2**31, "999"])
def test_host_writer_refuses_root_or_invalid_reader_group(monkeypatch, tmp_path, group):
    host_writer(monkeypatch, tmp_path)
    monkeypatch.setattr(net.os, "getegid", lambda: group)
    with pytest.raises(PermissionError):
        net.write_host_snapshot()
