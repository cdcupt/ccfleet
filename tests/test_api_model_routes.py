"""One rental per provider account and explicit model selection through the public gateway."""
import http.client
import json
import threading

import pytest

from ccfleet_agent import api_model_relay as relay
from ccfleetd import api_model_routes
from ccfleetd.gateway_transport import ApiModelAttempt, transport_attempt
from ccfleetd.store import StoreError
from tests.test_gateway_end_to_end import public_key, request, wait_for

pytest_plugins = ("tests.test_gateway_end_to_end",)


def bound(s, home, provider, slot, key, scope=None):
    account = scope or ("org-example" if provider == "openai" else "1234")
    model = "gpt-5.4" if provider == "openai" else "gemini-2.5-flash"
    receipt = relay.enroll(home, s.store.reserve_api_model(key["id"], slot, provider, account, model),
                           "synthetic-" + provider * 4)
    s.store.grant_api_model(key["id"], slot, receipt)
    return receipt


def catalog(s, key, pin=None):
    c = http.client.HTTPConnection(*s.server.server_address)
    headers = {"x-api-key": key}
    if pin:
        headers["X-CCFleet-Slot"] = pin
    c.request("GET", "/v1/models", headers=headers)
    r = c.getresponse()
    value = json.loads(r.read())
    c.close()
    return r.status, value


def test_model_switch_is_pinned_to_one_claim_across_turns(scenario, tmp_path):
    s = scenario
    base, other = s.own
    key = s.store.create_gateway_key("owner-a", "picker", [base], now=7)
    bound(s, tmp_path, "openai", other, key)
    bound(s, tmp_path, "gemini", base, key)
    items = catalog(s, key["key_token"])[1]["data"]
    assert {row["id"] for row in items} >= set(api_model_routes.ALIASES.values())
    assert not any("credential_fp" in row or "account_fp" in row for row in items)
    assert catalog(s, "unauthorized")[0] == 401
    assert all(row["id"] != "fleet-gpt" for row in catalog(s, key["key_token"], base)[1]["data"])
    history = [{"role": "user", "content": "Edit the local file"}]
    for model, target in [("opus", base), ("fleet-gpt", other), ("fleet-gemini", base)]:
        body = {"model": model, "max_tokens": 256, "messages": history, "stream": True}
        assert request(s, body, token=key["key_token"])[0] == 200
        assert s.pool.requests[-1]["slot"] == target
        assert s.pool.requests[-1]["body"]["messages"] == history
        if model == "opus":
            history.extend([{"role": "assistant", "content": [{"type": "tool_use", "id": "call1",
                             "name": "Write", "input": {"file_path": "sample.txt"}}]},
                            {"role": "user", "content": [{"type": "tool_result",
                             "tool_use_id": "call1", "content": "file written"}]}])
    calls = len(s.pool.requests)
    for token, model, pin in [(s.key["key_token"], "fleet-gpt", ""),
                              (key["key_token"], "fleet-gpt", base),
                              (key["key_token"], "fleet-typo", "")]:
        code, _, _ = request(s, {"model": model}, token=token,
                             headers={"X-CCFleet-Slot": pin} if pin else {})
        assert code >= 400
    assert len(s.pool.requests) == calls
    s.pool.plans[other].append({"status": 429, "retryable": True})
    code, _, headers = request(s, {"model": "fleet-gpt"}, token=key["key_token"])
    assert code == 429 and headers["x-should-retry"] == "false"
    assert len(s.pool.requests) == calls + 1
    assert s.store.gateway_public_keys(other, public_key(9)) == [public_key(9)]


def test_concurrent_enrollment_cannot_duplicate_same_account(scenario, tmp_path):
    s = scenario
    key = s.store.create_gateway_key("owner-a", "local", [s.own[0]], now=7)
    barrier = threading.Barrier(2)
    outcomes = []

    def enroll(index):
        home = tmp_path / str(index)
        home.mkdir()
        slot = s.own[index]
        barrier.wait()
        try:
            reservation = s.store.reserve_api_model(key["id"], slot, "openai", "org-verified", "gpt-5.4")
            receipt = relay.enroll(home, reservation, ("synthetic-a" + str(index)) * 2)
            s.store.grant_api_model(key["id"], slot, receipt)
            outcomes.append(True)
        except StoreError:
            outcomes.append(False)
    threads = [threading.Thread(target=enroll, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == [False, True]
    assert len(s.store.api_model_targets(key["id"], 1)) == 1


def test_rebind_revoke_claim_change_and_foreign_owner(scenario, tmp_path):
    s = scenario
    base, other = s.own
    key = s.store.create_gateway_key("owner-a", "local", [base], now=7)
    info = bound(s, tmp_path, "openai", other, key)
    target, = s.store.api_model_targets(key["id"], 1)
    assert isinstance(transport_attempt(target, s.cfg), ApiModelAttempt)
    assert relay.load_profile(tmp_path, info)["model"] == "gpt-5.4"
    with pytest.raises(StoreError):
        bound(s, tmp_path, "gemini", s.foreign, key)
    with pytest.raises(StoreError):
        s.store.grant_api_model(s.key["id"], other, info)  # original key is a pool
    with pytest.raises(StoreError):
        s.store.grant_api_model(key["id"], other, dict(info, claim_id="a" * 32))
    assert not s.store.remove_api_model(other, "openai", "b" * 32)
    assert s.store.remove_api_model(other, "openai", info["claim_id"])
    assert s.store.api_model_targets(key["id"], 1) == []
    with pytest.raises(StoreError):
        s.store.grant_api_model(key["id"], other, info)
    info = bound(s, tmp_path, "openai", other, key)
    assert not s.runtime._authorized({**key, "provider": "openai"}, target)
    s.store.update_gateway_key(key["id"], "owner-a", [base], "priority")
    assert s.store.api_model_targets(key["id"], 1) == s.store.api_model_targets(key["id"], 2) == []
    s.store.grant_api_model(key["id"], other, info)
    s.store.begin_release(other)
    assert not s.store.api_model_targets(key["id"], 2)
    s.store.finish_release(other, now=22)
    assert s.store._conn.execute("SELECT COUNT(*) FROM api_model_bindings").fetchone()[0] == 0
    assert s.store._conn.execute("SELECT COUNT(*) FROM api_model_grants").fetchone()[0] == 0


def test_api_model_revocation_stream_cancels(scenario, tmp_path):
    s = scenario
    base, other = s.own
    key = s.store.create_gateway_key("owner-a", "picker", [base], now=7)
    info = bound(s, tmp_path, "openai", other, key)
    s.pool.plans[other].append({"block": True})
    ended = threading.Event()

    def consume():
        try:
            request(s, {"model": "fleet-gpt"}, token=key["key_token"])
        except (OSError, http.client.IncompleteRead):
            pass
        finally:
            ended.set()
    thread = threading.Thread(target=consume)
    thread.start()
    wait_for(lambda: s.pool.attempts and s.pool.attempts[0].started.is_set())
    assert s.store.remove_api_model(other, "openai", info["claim_id"])
    assert ended.wait(3)
    thread.join()
    assert s.pool.attempts[0].closed.is_set() and len(s.pool.attempts) == 1


def test_claimed_api_slot_gets_only_the_forced_service_key(scenario, tmp_path):
    from ccfleet_agent.machine import wanted_slots
    from ccfleetd.desired import desired_state
    s = scenario
    base, other = s.own
    key = s.store.create_gateway_key("owner-a", "picker", [base], now=7)
    with s.store._write_txn() as conn:
        conn.execute("UPDATE slots SET state='claimed' WHERE id=?", (other,))
    slot = s.store.get_slot(other)
    assert not s.store.claimed_api_slot(slot)
    info = bound(s, tmp_path, "gemini", other, key)
    slot["api_model_granted"] = s.store.claimed_api_slot(slot)
    assert slot["api_model_granted"]
    service = s.store.gateway_public_keys(other, public_key(9))
    desired = desired_state(s.store.get_node(slot["node_id"]), slots=[slot],
                            slot_gateway_keys={other: service})
    assert wanted_slots(desired)[0]["ssh_gateway_public_keys"] == [public_key(9)]
    assert wanted_slots(desired)[0]["ssh_public_keys"] == []
    assert s.store.remove_api_model(other, "gemini", info["claim_id"])
    assert not s.store.claimed_api_slot(slot)


def test_provider_account_is_exclusive_across_fleet_owners_even_with_distinct_keys(scenario, tmp_path):
    s = scenario
    key = s.store.create_gateway_key("owner-a", "picker", [s.own[0]], now=7)
    first = s.store.reserve_api_model(key["id"], s.own[0], "openai", "org-same", "gpt-5.4")
    other = s.store.create_gateway_key("owner-b", "picker", [s.foreign], now=7)
    with pytest.raises(StoreError, match="already has an assigned slot"):
        s.store.reserve_api_model(other["id"], s.foreign, "openai", "org-same", "gpt-5.4")
    assert not s.store.api_model_targets(key["id"], key["generation"])
    assert s.store.remove_api_model(s.own[0], "openai", first["claim_id"])
    new = s.store.reserve_api_model(other["id"], s.foreign, "openai", "org-same", "gpt-5.4")
    assert new["binding_id"] != first["binding_id"]
