"""Burst backpressure stays before request bodies, slot selection and dispatch."""

import contextlib
import json
import socket
import threading
import weakref
from dataclasses import replace

import pytest

from ccfleetd import gateway
from tests.test_gateway_end_to_end import blocked_request, request, wait_for
from tests.test_gateway_end_to_end import scenario as scenario


def capacity(scenario, **overrides):
    scenario.runtime.cfg = replace(scenario.cfg, gateway_max_active=2,
                                   gateway_max_per_key=1, **overrides)


def pending_request(scenario, **kwargs):
    completed, result, errors = threading.Event(), [], []

    def send():
        try:
            result.append(request(scenario, **kwargs))
        except Exception as error:
            errors.append(error)
        finally:
            completed.set()

    worker = threading.Thread(target=send, daemon=True)
    worker.start()

    def finish():
        assert completed.wait(3), "queued request did not finish"
        worker.join(timeout=1)
        assert not errors
        return result[0]

    return finish


def release_first(scenario, conn, answer):
    answer.close()
    conn.close()
    scenario.pool.attempts[0].close()


def queued_peer(scenario, body=b'{"model":"opus","messages":[]}'):
    peer = socket.create_connection(scenario.server.server_address, timeout=3)
    peer.sendall(("POST /v1/messages HTTP/1.1\r\nHost: fleet.example.com\r\n"
                  "Authorization: Bearer " + scenario.key["key_token"]
                  + "\r\nContent-Type: application/json\r\nContent-Length: "
                  + str(len(body)) + "\r\n\r\n").encode())
    return peer


def assert_empty(scenario):
    wait_for(lambda: scenario.runtime.active == scenario.runtime.body_bytes == 0)
    assert not scenario.runtime.by_key
    assert not scenario.runtime.waiters
    assert not scenario.runtime.queued_by_key


def test_queued_requests_resume_once_in_fifo_order(scenario):
    capacity(scenario)
    conn, answer = blocked_request(scenario)
    try:
        first = pending_request(scenario, body={"model": "first", "messages": []})
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        second = pending_request(scenario, body={"model": "second", "messages": []})
        wait_for(lambda: len(scenario.runtime.waiters) == 2)
        assert len(scenario.pool.requests) == 1
        release_first(scenario, conn, answer)
        assert first()[0] == second()[0] == 200
        assert [item["body"]["model"] for item in scenario.pool.requests] \
            == ["opus", "first", "second"]
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_body_is_not_read_or_reserved_until_admission(scenario):
    capacity(scenario)
    conn, answer = blocked_request(scenario)
    retained = scenario.runtime.body_bytes
    body = b'{"model":"after-wait","messages":[]}'
    peer = queued_peer(scenario, body)
    try:
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        assert scenario.runtime.body_bytes == retained
        assert len(scenario.pool.attempts) == 1
        # The queued socket sent headers only. It must not reserve capacity or
        # block body reads while another request holds this key's active slot.
        release_first(scenario, conn, answer)
        wait_for(lambda: not scenario.runtime.waiters)
        assert scenario.runtime.body_bytes == len(body)
        assert len(scenario.pool.attempts) == 1
        peer.sendall(body)
        response = bytearray()
        while True:
            part = peer.recv(65536)
            if not part:
                break
            response.extend(part)
        assert response.startswith(b"HTTP/1.1 200")
        assert len(scenario.pool.requests) == 2
    finally:
        peer.close()
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_saturated_key_does_not_block_another_key_or_owner(scenario):
    capacity(scenario)
    other = scenario.store.create_gateway_key("owner-b", "Other owner", [scenario.foreign],
                                              now=7)
    conn, answer = blocked_request(scenario)
    try:
        finish = pending_request(scenario)
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        assert request(scenario, token=other["key_token"])[0] == 200
        assert scenario.pool.requests[-1]["slot"] == scenario.foreign
        assert len(scenario.runtime.waiters) == 1
        release_first(scenario, conn, answer)
        assert finish()[0] == 200
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_first_eligible_waiter_skips_a_key_that_is_still_saturated(scenario):
    capacity(scenario)
    original = scenario.key["id"]
    other = scenario.store.create_gateway_key("owner-b", "Other owner", [scenario.foreign],
                                              now=7)
    assert scenario.runtime._reserve(original, 0)
    assert scenario.runtime._reserve("synthetic-second-active-key", 0)
    held_original = held_second = True
    try:
        first = pending_request(scenario)
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        second = pending_request(scenario, token=other["key_token"])
        wait_for(lambda: len(scenario.runtime.waiters) == 2)
        scenario.runtime._release("synthetic-second-active-key", 0)
        held_second = False
        assert second()[0] == 200
        assert scenario.pool.requests[0]["slot"] == scenario.foreign
        assert len(scenario.runtime.waiters) == 1
        scenario.runtime._release(original, 0)
        held_original = False
        assert first()[0] == 200
        assert scenario.pool.requests[1]["slot"] == scenario.own[0]
    finally:
        if held_original:
            scenario.runtime._release(original, 0)
        if held_second:
            scenario.runtime._release("synthetic-second-active-key", 0)
    assert_empty(scenario)


@pytest.mark.parametrize("global_full", [False, True])
def test_queue_limits_reject_without_an_extra_dispatch(scenario, global_full):
    capacity(scenario, gateway_max_queued=1 if global_full else 2,
             gateway_max_queued_per_key=1)
    conn, answer = blocked_request(scenario)
    try:
        finish = pending_request(scenario)
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        status, raw, headers = request(scenario)
        assert status == 503 and headers["Retry-After"] == "3"
        expected = "gateway_queue_full" if global_full else "gateway_per_key_queue_full"
        assert json.loads(raw)["error"]["code"] == expected
        assert len(scenario.pool.requests) == 1
        release_first(scenario, conn, answer)
        assert finish()[0] == 200
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_queue_timeout_releases_waiter_and_reports_safe_retry(scenario):
    capacity(scenario, gateway_queue_timeout_s=1)
    conn, answer = blocked_request(scenario)
    try:
        status, raw, headers = request(scenario)
        assert status == 503 and headers["Retry-After"] == "3"
        assert json.loads(raw)["error"]["code"] == "gateway_queue_timeout"
        assert not scenario.runtime.waiters and not scenario.runtime.queued_by_key
        assert scenario.runtime.active == 1
        assert len(scenario.pool.requests) == 1
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_effective_queue_preserves_connection_headroom(scenario):
    capacity(scenario, gateway_max_connections=10)
    conn, answer = blocked_request(scenario)
    try:
        status, raw, _ = request(scenario)
        assert status == 503
        assert json.loads(raw)["error"]["code"] == "gateway_queue_full"
        assert not scenario.runtime.waiters
        assert len(scenario.pool.requests) == 1
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_disconnected_waiter_is_removed_without_reading_body(scenario):
    capacity(scenario)
    conn, answer = blocked_request(scenario)
    peer = queued_peer(scenario)
    try:
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        peer.close()
        wait_for(lambda: not scenario.runtime.waiters)
        assert not scenario.runtime.queued_by_key
        assert scenario.runtime.active == 1
        assert len(scenario.pool.requests) == 1
    finally:
        peer.close()
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_shutdown_wakes_queued_work_without_dispatch(scenario):
    capacity(scenario)
    conn, answer = blocked_request(scenario)
    try:
        finish = pending_request(scenario)
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        scenario.runtime.close()
        status, raw, _ = finish()
        assert status == 503 and json.loads(raw)["error"]["code"] == "gateway_stopping"
        assert len(scenario.pool.requests) == 1
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


@pytest.mark.parametrize("change", ["revoke", "selection"])
def test_queued_request_cannot_use_revoked_or_reconfigured_key(scenario, change):
    capacity(scenario)
    conn, answer = blocked_request(scenario)
    try:
        finish = pending_request(scenario)
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        if change == "revoke":
            scenario.store.revoke_gateway_key(scenario.key["id"], "owner-a")
        else:
            scenario.store.update_gateway_key(scenario.key["id"], "owner-a",
                                               [scenario.own[1]], "priority")
        release_first(scenario, conn, answer)
        assert finish()[0] == 403
        assert len(scenario.pool.requests) == 1
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_body_budget_is_rechecked_after_wait(scenario):
    capacity(scenario, gateway_body_budget_bytes=1100)
    body = json.dumps({"model": "opus", "messages": [{"role": "user",
                                                   "content": "x" * 900}]}).encode()
    conn, answer = blocked_request(scenario, body=body)
    other = scenario.store.create_gateway_key("owner-a", "Another key", scenario.own, now=7)
    try:
        finish = pending_request(scenario, body=body, token=other["key_token"])
        wait_for(lambda: len(scenario.runtime.waiters) == 1)
        assert scenario.runtime.body_bytes == len(body)
        assert len(scenario.pool.requests) == 1
        release_first(scenario, conn, answer)
        assert finish()[0] == 200
        assert len(scenario.pool.requests) == 2
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_status_probe_is_nonblocking_and_does_not_join_the_queue(scenario):
    capacity(scenario)
    conn, answer = blocked_request(scenario)
    try:
        result = scenario.runtime.status(scenario.key)
        assert not result["ready"]
        assert not scenario.runtime.waiters
        assert len(scenario.pool.requests) == 1
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_admission_diagnostics_contain_only_fixed_reasons_and_aggregate_counters(scenario, caplog):
    capacity(scenario, gateway_queue_timeout_s=0)
    conn, answer = blocked_request(scenario)
    try:
        with caplog.at_level("INFO", logger="ccfleetd.gateway"):
            status, _, _ = request(scenario, body={"model": "PRIVATE_MODEL", "messages": []})
        assert status == 503
        assert "outcome=per_key_capacity" in caplog.text
        assert "active=1 queued=0" in caplog.text
        assert scenario.key["key_token"] not in caplog.text
        assert scenario.key["id"] not in caplog.text
        assert "owner-a" not in caplog.text and "PRIVATE_MODEL" not in caplog.text
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


def test_parsed_request_is_released_while_response_stream_is_still_active(scenario, monkeypatch):
    class Payload(dict):
        pass

    original = gateway.wire._json
    parsed = []

    def track(raw):
        value = Payload(original(raw))
        parsed.append(weakref.ref(value))
        return value

    monkeypatch.setattr(gateway.wire, "_json", track)
    conn, answer = blocked_request(scenario)
    try:
        assert scenario.runtime.active == 1
        assert parsed and all(reference() is None for reference in parsed)
    finally:
        release_first(scenario, conn, answer)
    assert_empty(scenario)


@pytest.mark.skipif(not hasattr(gateway.select, "POLLRDHUP"), reason="Linux peer-close signal")
def test_peer_close_is_detected_even_with_an_unread_queued_body():
    local, remote = socket.socketpair()
    try:
        remote.sendall(b"PRIVATE_UNREAD_BODY")
        remote.close()
        handler = type("Handler", (), {"connection": local})()
        assert gateway.GatewayRuntime._disconnected(handler)
        assert local.recv(64) == b"PRIVATE_UNREAD_BODY"
    finally:
        with contextlib.suppress(OSError):
            remote.close()
        local.close()
