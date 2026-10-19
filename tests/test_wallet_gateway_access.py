"""A paid month gates new model/device work without cancelling an admitted stream."""
from __future__ import annotations

import threading
import time

import pytest

from ccfleetd import slots, wallet_access
from tests.test_gateway_end_to_end import (  # noqa: F401 -- fixture and synthetic HTTP harness
    STREAM,
    public_key,
    request,
    scenario,  # noqa: F401 -- imported pytest fixture
)
from tests.test_wallet_store import machine


@pytest.fixture
def paid(scenario):  # noqa: F811 -- imported pytest fixture
    c = scenario
    now = time.time()
    c.store.add_account("credit-admin", "sub-credit-admin", "credit-admin@example.invalid",
                        role="admin", now=now)
    identifier = machine(c.store, "paid-node", at=now)
    c.store.set_node_access("paid-node", "192.0.2.55", 2222, public_key(55))
    c.store.grant_wallet_credit("owner-a", "20", actor_id="credit-admin", reason="Synthetic",
        idempotency_key="gateway-grant", now=now)
    c.order = c.store.purchase_slot("owner-a", "paid-node", expected_cents=1000,
        idempotency_key="gateway-paid-month", now=now, max_age_s=90)
    c.store.move_slot(identifier, slots.CLAIMED)
    c.store.move_slot(identifier, slots.ACTIVE)
    c.key = c.store.create_gateway_key("owner-a", "Paid connection", [identifier], now=now)
    c.stamp = [now]
    c.runtime.wall_clock = lambda: c.stamp[0]
    c.paid_id = identifier
    return c


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens"])
def test_new_messages_and_count_tokens_fail_at_paid_boundary_even_with_mode_off(paid, path):
    c = paid
    assert c.cfg.usd_slot_credit_enabled is False
    c.stamp[0] = c.order["period_end_at"]
    before = c.store.get_slot(c.paid_id)
    code, body, _ = request(c, path=path)
    assert code == 403 and wallet_access.EXPIRED.encode() in body
    assert not c.pool.requests and not c.pool.attempts
    assert c.store.get_slot(c.paid_id) == before
    status = c.runtime.status(c.key)
    assert status["billing_paused"] is True and status["detail"] == wallet_access.EXPIRED


def test_expiry_while_transport_prepares_rejects_before_model_send_with_renewal_detail(paid):
    c = paid
    c.pool.plans[c.paid_id].append({"block_ready": True})
    result = []
    worker = threading.Thread(target=lambda: result.append(request(c)))
    worker.start()
    limit = time.monotonic() + 2
    while not c.pool.attempts and time.monotonic() < limit:
        time.sleep(.01)
    attempt = c.pool.attempts[0]
    assert attempt.ready.wait(2)
    c.stamp[0] = c.order["period_end_at"]
    # The fake's gate opens without marking it cancelled.
    attempt.closed.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert result[0][0] == 403 and wallet_access.EXPIRED.encode() in result[0][1]
    assert not c.pool.requests


def test_paid_expiry_during_successful_stream_keeps_admitted_request(paid):
    c = paid
    original = c.pool.__class__.__call__
    def factory(target, cfg):
        attempt = original(c.pool, target, cfg)
        def chunks():
            c.stamp[0] = c.order["period_end_at"]
            yield STREAM[:30]
            yield STREAM[30:]
        attempt.chunks = chunks
        return attempt
    c.runtime.transport_factory = factory
    code, body, _ = request(c)
    assert code == 200 and body == STREAM and len(c.pool.requests) == 1
    code, body, _ = request(c)
    assert code == 403 and wallet_access.EXPIRED.encode() in body
    assert len(c.pool.requests) == 1


def test_grandfathered_claim_still_runs_and_missing_financial_authority_fails_closed(
        scenario):  # noqa: F811 -- imported pytest fixture
    c = scenario
    assert request(c)[0] == 200
    original = c.store.paid_slot_through
    def unavailable(*args, **kwargs):
        raise RuntimeError("PRIVATE FINANCIAL FAILURE")
    c.store.paid_slot_through = unavailable
    code, body, _ = request(c)
    assert code == 403 and wallet_access.UNAVAILABLE.encode() in body
    assert b"PRIVATE" not in body
    c.store.paid_slot_through = original


def test_expired_owned_enrollment_keeps_renewal_403_without_issuing_a_key(paid, monkeypatch):
    from tests.test_gateway_end_to_end import raw_request
    from tests.test_gateway_security import paired_device_token
    c = paid
    token = paired_device_token(c)
    before = c.store.list_gateway_keys("owner-a")
    monkeypatch.setattr("ccfleetd.api.time.time", lambda: c.order["period_end_at"])
    import json
    body = json.dumps({"slot_ids": [c.paid_id]}).encode()
    answer = raw_request(c, "Content-Type: application/json\r\nContent-Length: "
                         + str(len(body)) + "\r\n", target="/api/cli/gateway", body=body,
                         token=token)
    assert int(answer.split(b" ", 2)[1]) == 403
    assert wallet_access.EXPIRED.encode() in answer
    assert c.store.list_gateway_keys("owner-a") == before
    assert not c.pool.requests
