"""Relay instrumentation and reporting, with only synthetic profiles and upstreams."""

import copy
import hashlib
import io
import itertools
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import agent, machine
from ccfleet_agent import local_relay as relay
from ccfleet_agent import relay_metrics as metrics
from ccfleetd import client_status, heartbeat, usersite
from ccfleetd.config import Config
from tests import test_local_relay
from tests.test_local_relay import Upstream, frame, request, response, run
from tests.test_machine import Fake, account

slot = test_local_relay.slot
NOW = time.time()
BOUND = hashlib.sha256(b"owner-a").hexdigest()[:16]
OTHER = "0123456789abcdef"


def metric_file(slot):
    return slot / ".config/ccfleet" / metrics.FILE_NAME


def report(slot, *, now=NOW):
    return metrics.report(slot, BOUND, now)


def clock(monkeypatch):
    ticks = itertools.count(100.0, 0.001)
    monkeypatch.setattr(relay, "time", SimpleNamespace(
        time=lambda: NOW, monotonic=lambda: next(ticks)))


def test_completed_request_records_one_content_free_outcome_and_real_timing_samples(slot, monkeypatch):
    clock(monkeypatch)
    code, raw, upstream = run(slot, request(b'{"model":"SECRET_MODEL","messages":[]}'))
    assert code == 0 and response(raw)[0]["status"] == 200 and len(upstream.calls) == 1
    result = report(slot)
    assert result["requests"] == result["success"] == 1
    assert result["latencies"]["connect_ms"] == {"count": 1, "mean": 1.0, "max": 1.0}
    assert result["latencies"]["first_byte_ms"]["count"] == 1
    assert result["latencies"]["total_ms"]["count"] == 1
    saved = metric_file(slot).read_text()
    assert "SECRET_MODEL" not in saved and "slot-test-token" not in saved
    assert str(slot) not in saved and "owner-a" not in saved
    assert BOUND not in json.dumps(result)


@pytest.mark.parametrize("status,outcome", [(401, "auth_errors"), (403, "permission_errors"),
                                           (429, "rate_limits"), (500, "upstream_errors")])
def test_http_failures_are_counted_once_without_forwarding_provider_content(slot, monkeypatch,
                                                                         status, outcome):
    clock(monkeypatch)
    code, raw, upstream = run(slot, upstream=Upstream(status=status))
    result = report(slot)
    assert code == 2 and response(raw)[0]["status"] == status
    assert result["requests"] == result[outcome] == 1 and result["success"] == 0
    assert len(upstream.calls) == 1
    assert result["latencies"]["first_byte_ms"]["count"] == 0


@pytest.mark.parametrize("input_", [frame({"version": 2, "operation": "status"}),
                                    b"malformed frame", frame({"version": 1, "operation": "request"})])
def test_status_and_undecodable_requests_are_never_counted(slot, input_):
    run(slot, input_)
    assert not metric_file(slot).exists()


def test_decoded_but_invalid_request_has_an_input_error_not_a_model_call(slot, monkeypatch):
    clock(monkeypatch)
    code, _, upstream = run(slot, request(method="DELETE"))
    assert code == 2 and not upstream.calls
    assert report(slot)["input_errors"] == 1
    assert report(slot)["latencies"]["connect_ms"]["count"] == 0


@pytest.mark.parametrize("status", [200, 429])
def test_recording_or_missing_collector_never_changes_the_inference_result(slot, monkeypatch, status):
    clock(monkeypatch)
    attempts = []

    def broken(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("SECRET_LOG_DETAIL")

    monkeypatch.setattr(metrics, "record", broken)
    code, raw, upstream = run(slot, upstream=Upstream(status=status))
    assert code == (0 if status == 200 else 2)
    assert response(raw)[0]["status"] == status and len(upstream.calls) == 1
    assert attempts == [True] and b"SECRET_LOG_DETAIL" not in raw
    monkeypatch.setattr(relay, "_metrics_module", lambda: (_ for _ in ()).throw(ImportError()))
    assert run(slot)[0] == 0


def test_partial_response_is_counted_as_connection_error_not_completed_success(slot, monkeypatch):
    clock(monkeypatch)
    code, raw, _ = run(slot, upstream=Upstream(failure="truncated"))
    assert code == 2
    with pytest.raises(EOFError):
        response(raw)
    result = report(slot)
    assert result["requests"] == result["connection_errors"] == 1 and result["success"] == 0


def test_disconnected_stream_counts_one_cancelled_request_without_success_terminator(slot):
    reader, writer = socket.socketpair()
    source = reader.makefile("rb")
    upstream, output, result = Upstream(), io.BytesIO(), []
    entered = threading.Event()

    def read(size):
        entered.set()
        assert upstream.sock.cancelled.wait(3)
        return b""

    upstream.read1 = read
    worker = threading.Thread(target=lambda: result.append(relay.serve_one(
        source, output, slot, policy=lambda: None, connect=lambda: upstream, watch=True)))
    writer.sendall(request())
    worker.start()
    try:
        assert entered.wait(2)
        writer.shutdown(socket.SHUT_WR)
        worker.join(timeout=4)
        assert result == [2] and not worker.is_alive()
        with pytest.raises(EOFError):
            response(output.getvalue())
        summary = metrics.report(slot, BOUND, time.time())
        assert summary["requests"] == summary["cancelled"] == 1 and summary["success"] == 0
    finally:
        writer.close()
        worker.join(timeout=3)
        source.close()
        reader.close()


def test_late_old_account_writer_cannot_reset_new_account_metrics(slot, monkeypatch):
    metrics.record(slot, OTHER, "success", {}, NOW)
    before = metric_file(slot).read_bytes()
    bindings = iter([BOUND, OTHER])
    monkeypatch.setattr(relay, "bound_account", lambda _: next(bindings))
    relay._record_metrics(slot, BOUND, "cancelled", {"total_ms": 2})
    assert metric_file(slot).read_bytes() == before
    assert metrics.report(slot, OTHER, NOW)["success"] == 1


def test_binding_guard_is_executed_with_writer_lock_held_before_any_state_reset(slot, monkeypatch):
    metrics.record(slot, OTHER, "success", {}, NOW)
    before = metric_file(slot).read_bytes()
    calls = []
    original = metrics._acquire

    def acquired(directory):
        fd = original(directory)
        calls.append("locked")
        return fd

    def current():
        assert calls == ["locked"]
        calls.append("checked")
        return False

    monkeypatch.setattr(metrics, "_acquire", acquired)
    metrics.record(slot, BOUND, "success", {}, NOW, still_bound=current)
    assert calls == ["locked", "checked"] and metric_file(slot).read_bytes() == before


def test_failed_binding_check_is_fixed_error_and_never_changes_new_account_data(slot):
    metrics.record(slot, OTHER, "success", {}, NOW)
    before = metric_file(slot).read_bytes()
    with pytest.raises(metrics.MetricsError) as error:
        metrics.record(slot, BOUND, "success", {}, NOW,
                       still_bound=lambda: (_ for _ in ()).throw(RuntimeError("SECRET_DETAIL")))
    assert "SECRET" not in str(error.value) and metric_file(slot).read_bytes() == before


def facts(slot, monkeypatch):
    monkeypatch.setenv("HOME", str(slot))
    monkeypatch.setattr(agent.os, "geteuid", lambda: 1001)
    state = {"bound_fp": BOUND}
    creds = {"bound_fp": BOUND, "account_fp": BOUND, "logged_in": True}
    return creds, state


def test_slot_user_reads_own_metrics_and_machine_forwards_only_reported_facts(slot, monkeypatch):
    creds, state = facts(slot, monkeypatch)
    metrics.record(slot, BOUND, "success", {"connect_ms": 12}, NOW)
    answer = agent._slot_relay_report(creds, state, NOW)
    assert answer["requests"] == 1 and BOUND not in json.dumps(answer)
    fake = Fake(facts={"relay": answer, "private_extra": {"token": "SECRET"}})
    cfg = machine.MachineConfig.from_env({"CCFLEET_URL": "https://fleet.invalid",
                                          "CCFLEET_NODE_ID": "synthetic",
                                          "CCFLEET_NODE_TOKEN": "synthetic"})
    forwarded = machine.ask_slot(account(), cfg, fake.system(), {})
    assert forwarded == {"relay": answer}
    assert fake.spawned[0][1]["user"] == account().pw_uid


@pytest.mark.parametrize("case", ["root", "mismatch", "missing", "signed_out", "transition"])
def test_slot_metrics_are_not_read_as_root_or_without_current_account_binding(slot, monkeypatch, case):
    creds, state = facts(slot, monkeypatch)
    if case == "root":
        monkeypatch.setattr(agent.os, "geteuid", lambda: 0)
    elif case == "mismatch":
        creds["account_fp"] = OTHER
    elif case == "missing":
        creds.pop("bound_fp")
    elif case == "signed_out":
        creds["logged_in"] = False
    else:
        state["account_restart"] = "owed"
    monkeypatch.setattr(metrics, "report", lambda *args: pytest.fail("must not read slot metrics"))
    assert agent._slot_relay_report(creds, state, NOW) is None


def test_account_transition_while_reading_discards_metric_snapshot(slot, monkeypatch):
    creds, state = facts(slot, monkeypatch)
    original = metrics.report

    def read(*args):
        result = original(*args)
        (slot / ".config/ccfleet/slot-state.json").write_text(json.dumps({"bound_fp": OTHER}))
        return result

    monkeypatch.setattr(metrics, "report", read)
    assert agent._slot_relay_report(creds, state, NOW) is None


def payload(value):
    return {"node_id": "synthetic", "mode": "machine", "slots": [
        {"unix_user": "slot01", "present": True, "relay": value}]}


def test_heartbeat_accepts_only_numeric_report_without_account_tag_or_request_data(slot):
    metrics.record(slot, BOUND, "success", {"total_ms": 4}, NOW)
    summary = report(slot)
    checked = heartbeat.validate_heartbeat(payload(summary), "synthetic", now=NOW)
    assert checked["slots"][0]["relay"] == summary
    assert BOUND not in json.dumps(checked)
    for field in ("bound_fp", "model", "request_id", "headers", "body", "email", "path"):
        poisoned = {**summary, field: "SECRET"}
        checked = heartbeat.validate_heartbeat(payload(poisoned), "synthetic", now=NOW)
        assert "relay" not in checked["slots"][0] and "SECRET" not in json.dumps(checked)


@pytest.mark.parametrize("value", [True, -1, "SECRET", float("nan"), float("inf"), 10**400])
def test_invalid_metric_numerics_are_dropped_not_coerced(slot, value):
    summary = report(slot)
    summary["requests"] = value
    checked = heartbeat.validate_heartbeat(payload(summary), "synthetic", now=NOW)
    assert "relay" not in checked["slots"][0]


@pytest.mark.parametrize("offset,accepted", [(-7 * 86400 - 1, False), (-7 * 86400, True),
                                             (60, True), (61, False)])
def test_server_clock_bounds_metrics_observation_age_and_future_skew(slot, offset, accepted):
    summary = report(slot, now=NOW + offset)
    checked = heartbeat.validate_heartbeat(payload(summary), "synthetic", now=NOW)
    assert ("relay" in checked["slots"][0]) is accepted


def test_metrics_ui_labels_samples_and_never_turns_missing_samples_into_zero_latency(slot):
    empty = report(slot)
    body = usersite._relay_usage(empty, NOW, NOW, 300)
    assert "No timing samples" in body and "0.0 ms" not in body
    assert "Separate from the subscription quota" in body and "native transcript" in body
    metrics.record(slot, BOUND, "success", {"connect_ms": 12, "total_ms": 80}, NOW)
    body = usersite._relay_usage(report(slot), NOW, NOW, 300)
    assert "12.0 ms mean" in body and "1 sample" in body and "Fresh report" in body
    assert "Last completed transfer" in body
    assert "does not prove model-task success" in body
    stale = copy.deepcopy(report(slot))
    stale["observed_at"] = NOW - 1000
    stale["last_success_at"] = NOW - 1000
    assert "Stale report" in usersite._relay_usage(stale, NOW, NOW, 300)


def test_metrics_never_make_stale_credentials_provider_ready(slot):
    summary = report(slot)
    state = client_status.health({"state": "active"}, {"relay": summary, "credentials": {
        "bound_fp": BOUND, "account_fp": BOUND, "logged_in": True,
        "expires_at": (NOW + 3600) * 1000}}, {}, heard=NOW - 1000, now=NOW, max_age=300)
    assert not state["ready"]
    assert state["reason"] == "observation_stale"


@pytest.mark.parametrize("field", ["claimed_at", "account_switched_at"])
def test_account_card_omits_metrics_cached_before_current_assignment(slot, field):
    from ccfleetd.store import Store
    from tests.test_cli_access import active_slot

    store = Store(":memory:")
    try:
        row = active_slot(store)
        node = store.get_node(row["node_id"])
        row[field] = NOW + 1
        observed = {"ts": NOW + 2, "payload": {"slots": [{
            "unix_user": row["unix_user"], "credentials": {"logged_in": False},
            "relay": report(slot)}]}}
        body = usersite._slot_card(row, node, observed, {}, "csrf", Config(), NOW + 2)
        assert "Relay measurements" not in body
        observed["payload"]["slots"][0]["relay"] = report(slot, now=NOW + 1)
        body = usersite._slot_card(row, node, observed, {}, "csrf", Config(), NOW + 2)
        assert "Relay measurements" in body
    finally:
        store.close()


def test_privacy_distinguishes_aggregate_window_from_heartbeat_retention():
    body = usersite.privacy_page(Config(retention_days=30))
    assert "seven UTC calendar days" in body
    assert "distinct from how long heartbeat snapshots" in body
    assert "model names, request IDs, headers, bodies, paths, email addresses or credentials" in body


def test_installer_installs_optional_metrics_before_its_runtime_consumers():
    script = (Path(__file__).resolve().parents[1] / "node/machine-setup.sh").read_text()
    for consumer in ("agent.py", "machine.py", "local_relay.py"):
        assert script.index("fetch ccfleet_agent/relay_metrics.py") < script.index(
            "fetch ccfleet_agent/" + consumer)


def test_read_only_status_does_not_create_even_empty_metrics_storage(slot):
    output = io.BytesIO()
    assert relay.serve_one(io.BytesIO(frame({"version": 2, "operation": "status"})),
                           output, slot, policy=lambda: None) == 0
    assert not metric_file(slot).exists()
    assert not (slot / ".config/ccfleet" / metrics.LOCK_NAME).exists()
