"""Hidden own-account provider controls; local HTTP and fake status only, no native calls."""

from __future__ import annotations

import http.client
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import gateway_providers as providers
from ccfleetd import gateway_ui, slots
from ccfleetd.api import Context, build_server
from ccfleetd.config import Config, ConfigError
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import StoreError
from tests.test_gateway_access import add_held_slot, public_key


def configured(cfg, **changes):
    return replace(cfg, public_url="https://fleet.example.com", gateway_enabled=True,
        gateway_ssh_key_path="/unused", gateway_ssh_public_key=public_key(9),
        gateway_devin_enabled=True, gateway_devin_native_version="3000.11.92",
        gateway_devin_adapter_revision="b"*64, gateway_devin_model_map_digest="c"*64, **changes)


def native_report(now, **changes):
    return {"account_fp": "a"*64, "identity_verified": True,
        "bridge_contract": providers.NATIVE_AGENT_CONTRACT,
        "capabilities": dict.fromkeys(providers.NATIVE_AGENT_CAPABILITIES, True),
        "native_version": "3000.11.92", "adapter_revision": "b"*64,
        "model_map_digest": "c"*64, "observed_at": now, **changes}


@pytest.fixture
def controls(store, cfg):
    now = time.time()
    store.add_account("a1", "sub1", "a@example.test", slot_quota=3, now=now)
    store.add_account("a2", "sub2", "b@example.test", slot_quota=1, now=now)
    for n, account in ((1, "a1"), (2, "a1"), (3, "a2")):
        add_held_slot(store, n, account, now=now)
    store._conn.execute("UPDATE slots SET present=1")
    store._conn.commit()
    config = configured(cfg)
    yield SimpleNamespace(store=store, cfg=config, account={"id": "a1"}, now=now,
                          runtime=providers.runtime_expectations(config))


def bind(c, slot="s1", *, generation=0, **changes):
    own = c.store.get_slot(slot)
    return c.store.bind_gateway_provider(slot, own["held_by"], "devin", own["gateway_claim_id"],
        native_report(c.now, **changes), expected_generation=generation, now=c.now)


def native_key(c):
    return c.store.create_gateway_key("a1", "Existing laptop", ["s1"], now=c.now,
        provider="devin", allow_devin=True, provider_runtime=c.runtime)


def native_targets(c, key, **changes):
    return c.store.gateway_targets(key, allow_devin=True, provider_runtime=c.runtime,
                                   now=c.now, **changes)


def test_native_agent_contract_does_not_claim_raw_messages_authority(controls):
    c = controls
    binding = bind(c)
    slot = c.store.get_slot("s1")
    assert providers.binding_ready(binding, slot, "a1", "devin", now=c.now,
                                    provider_runtime=c.runtime)
    assert not providers.binding_ready(binding, slot, "a1", "devin", now=c.now)
    assert not {"system", "history", "request_features"} & providers.NATIVE_AGENT_CAPABILITIES
    mixed = native_report(c.now, capabilities={**binding["capabilities"], "system": True})
    with pytest.raises(ValueError):
        providers.clean_report(mixed)
    for capability in providers.NATIVE_AGENT_CAPABILITIES:
        partial = {**binding, "capabilities": dict(binding["capabilities"], **{capability: False})}
        assert not providers.binding_ready(partial, slot, "a1", "devin", now=c.now,
                                            provider_runtime=c.runtime)


@pytest.mark.parametrize("field", ["native_version", "adapter_revision", "model_map_digest"])
def test_expected_runtime_and_model_identity_are_compared_not_just_well_formed(controls, field):
    c = controls
    changed = "3000.11.93" if field == "native_version" else "f"*64
    bind(c, **{field: changed})
    assert gateway_ui.ready_provider_slots(c.store, c.cfg, "a1", c.store.list_slots(held_by="a1"),
                                            now=c.now) == []
    with pytest.raises(StoreError):
        native_key(c)


def test_runtime_profile_never_implicitly_enables_devin(controls):
    c = controls
    bind(c)
    disabled = replace(c.cfg, gateway_devin_enabled=False)
    assert providers.runtime_expectations(disabled) == c.runtime
    assert providers.available_providers(disabled, c.store.gateway_provider_binding("s1", "a1", "devin"),
        c.store.get_slot("s1"), "a1", now=c.now) == ("anthropic",)
    assert providers.runtime_expectations(Config()) is None
    assert Config.from_env({"CCFLEET_GATEWAY_DEVIN_ENABLED": "true"}).gateway_devin_enabled is True


@pytest.mark.parametrize("suffix,value", [("NATIVE_VERSION", "latest"), ("NATIVE_VERSION", "3.1"),
    ("ADAPTER_REVISION", "secret-path"), ("MODEL_MAP_DIGEST", "F"*64)])
def test_invalid_profile_configuration_fails_even_with_feature_disabled(suffix, value):
    with pytest.raises(ConfigError):
        Config.from_env({"CCFLEET_GATEWAY_DEVIN_"+suffix: value})


def test_valid_profile_config_is_explicit_and_default_off():
    config = Config.from_env({"CCFLEET_GATEWAY_DEVIN_NATIVE_VERSION": "3000.11.92",
        "CCFLEET_GATEWAY_DEVIN_ADAPTER_REVISION": "b"*64,
        "CCFLEET_GATEWAY_DEVIN_MODEL_MAP_DIGEST": "c"*64})
    assert not config.gateway_devin_enabled
    assert providers.runtime_expectations(config) == {"bridge_contract": "native-agent-v1",
        "native_version": "3000.11.92", "adapter_revision": "b"*64, "model_map_digest": "c"*64}


def test_native_claimed_slot_needs_no_fake_claude_login_and_remains_dormant_by_default(controls):
    c = controls
    c.store._conn.execute("UPDATE slots SET state=? WHERE id='s1'", (slots.CLAIMED,))
    c.store._conn.commit()
    binding = bind(c)
    key = native_key(c)
    assert c.store.gateway_targets(key) == []
    [target] = native_targets(c, key)
    assert target == {"slot_id": "s1", "node_id": "m1", "unix_user": "slot01",
        "claimed_at": c.store.get_slot("s1")["claimed_at"],
        "claim_generation": c.store.get_slot("s1")["gateway_generation"],
        "claim_id": binding["claim_id"], "access_host": "192.0.2.1", "access_port": 22,
        "ssh_host_key": public_key(1), "provider": "devin", "provider_binding_generation": 1,
        "provider_account_fp": "a"*64, **c.runtime}
    assert c.store.get_slot("s1")["state"] == slots.CLAIMED
    page = gateway_ui.panel(c.store, c.cfg, c.account, "csrf",
                            c.store.list_slots(held_by="a1"), now=c.now)
    assert "Devin ready" in page and "Devin 已就绪" in page
    assert 'value="devin"' in page
    assert not c.store.gateway_request_is_active(key["id"], key["generation"], "s1",
                                                target["claim_generation"])
    assert c.store.gateway_request_is_active(key["id"], key["generation"], "s1",
        target["claim_generation"], allow_devin=True, provider_runtime=c.runtime, now=c.now)
    assert c.store.gateway_public_keys("s1", public_key(9)) == []
    assert c.store.gateway_public_keys("s1", public_key(9), allow_devin=True,
        provider_runtime=c.runtime, now=c.now) == [public_key(9)]


def test_native_binding_change_revocation_release_and_stale_claim_invalidate_targets(controls):
    c = controls
    binding = bind(c)
    key = native_key(c)
    [target] = native_targets(c, key)
    assert c.store.gateway_targets(key, allow_devin=True, provider_runtime=c.runtime,
                                    now=c.now+301) == []
    changed = bind(c, generation=1, account_fp="e"*64)
    assert native_targets(c, key) == []
    current = c.store.resolve_gateway_key(key["key_token"], now=c.now)
    assert current["generation"] == key["generation"]+1
    assert native_targets(c, current)[0]["provider_account_fp"] == "e"*64
    assert not c.store.gateway_request_is_active(key["id"], key["generation"], "s1",
        target["claim_generation"], allow_devin=True, provider_runtime=c.runtime, now=c.now)
    assert c.store.revoke_gateway_provider_binding("s1", "a1", "devin", binding["claim_id"],
                                                    changed["generation"], now=c.now)
    assert native_targets(c, c.store.resolve_gateway_key(key["key_token"], now=c.now)) == []


def test_native_route_does_not_borrow_anthropic_targets_or_another_owners_binding(controls):
    c = controls
    bind(c)
    key = native_key(c)
    c.store.set_enabled("m1", False)
    assert native_targets(c, key) == []
    assert c.store.gateway_targets(key, allow_devin=True, provider_runtime=None, now=c.now) == []
    c.store.set_enabled("m1", True)
    assert native_targets(c, {**key, "account_id": "a2"}) == []
    c.store.begin_release("s1", held_by="a1")
    assert native_targets(c, key) == []


def form(**changes):
    return {"name": "Existing laptop", "slot.s1": "1", "mode": "sticky", **changes}


def test_selector_is_hidden_until_verified_and_updates_existing_cli_key_without_rotating_token(controls):
    c = controls
    key = c.store.create_gateway_key("a1", "Existing laptop", ["s1"], now=c.now)
    def page(config=c.cfg):
        return gateway_ui.panel(c.store, config, c.account, "csrf",
                                c.store.list_slots(held_by="a1"), now=c.now)
    assert 'value="devin"' not in page()
    bind(c)
    visible = page()
    assert 'select name="provider"' in visible and 'value="devin"' in visible
    assert "模型服务" in visible and "Claude 智能体" in visible
    assert "Feature behavior can differ" in visible
    assert "a"*64 not in visible and "account_fp" not in visible and key["key_token"] not in visible
    outcome = gateway_ui.act(c.store, c.cfg, c.account, "/account/gateway/update",
        form(key_id=key["id"], provider="devin"), c.now)
    assert "gateway-updated" in outcome.location
    changed = c.store.resolve_gateway_key(key["key_token"], now=c.now)
    assert changed["provider"] == "devin" and changed["id"] == key["id"]
    assert changed["generation"] == key["generation"]+1
    unavailable = replace(c.cfg, gateway_devin_enabled=False)
    assert 'value="devin"' not in page(unavailable)
    assert 'name="provider" value="anthropic"' in page(unavailable)
    reverted = gateway_ui.act(c.store, unavailable, c.account, "/account/gateway/update",
        form(key_id=key["id"], provider="anthropic"), c.now)
    assert "gateway-updated" in reverted.location
    assert c.store.resolve_gateway_key(key["key_token"], now=c.now)["provider"] == "anthropic"


@pytest.mark.parametrize("gate", ["flag", "identity", "subject", "stale", "node", "present", "owner"])
def test_unavailable_identity_runtime_or_rental_never_exposes_selector(controls, gate):
    c = controls
    config = c.cfg
    changes = {}
    if gate == "identity":
        changes["identity_verified"] = False
    if gate == "subject":
        changes.update(account_fp=None, identity_verified=False)
    if gate == "stale":
        changes["observed_at"] = c.now-301
    bind(c, slot="s3" if gate == "owner" else "s1", **changes)
    if gate == "flag":
        config = replace(config, gateway_devin_enabled=False)
    if gate == "node":
        c.store.set_enabled("m1", False)
    if gate == "present":
        c.store._conn.execute("UPDATE slots SET present=0 WHERE id='s1'")
        c.store._conn.commit()
    html = gateway_ui.panel(c.store, config, c.account, "csrf",
                            c.store.list_slots(held_by="a1"), now=c.now)
    assert 'value="devin"' not in html and 'select name="provider"' not in html
    result = gateway_ui.act(c.store, config, c.account, "/account/gateway/create",
                             form(provider="devin"), c.now)
    assert "gateway-invalid" in result.location
    assert c.store.list_gateway_keys("a1") == []


@pytest.mark.parametrize("changes", [{"provider": "Devin"}, {"provider": "../devin"},
    {"provider": "devin", "slot.s2": "2"}, {"provider": "devin", "slot.s1": "", "slot.s3": "1"}])
def test_forged_selector_or_unowned_multislot_native_choices_cannot_create_keys(controls, changes):
    c = controls
    bind(c)
    outcome = gateway_ui.act(c.store, c.cfg, c.account, "/account/gateway/create", form(**changes), c.now)
    assert "gateway-invalid" in outcome.location
    assert c.store.list_gateway_keys("a1") == []


@pytest.fixture
def control_http(controls):
    c = controls
    pair = c.store.request_cli_pairing("s1", "a1", now=c.now)
    device = c.store.register_cli_device(pair, public_key(8), "Laptop", now=c.now)
    context = Context(c.store, c.cfg, Monitor(c.store, c.cfg, LogNotifier()))
    context.gateway.close()
    class MetadataOnly:
        enabled = True
        def status(self, key, pinned=""):
            return {"ready": False, "mode": key["mode"], "base_url": c.cfg.public_url,
                "slots": [{"id": "s1", "name": "Owned", "state": "active", "ready": False,
                           "provider_account_fp": "a"*64}],
                "key": {**key, "account_fp": "a"*64}, "binding": {"account_fp": "a"*64}}
    context.gateway = MetadataOnly()
    server = build_server(context, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(method, path, body=None, token=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connection.request(method, path, body=None if body is None else json.dumps(body),
            headers={"Host": "fleet.example.com", "Content-Type": "application/json",
                     "Authorization": "Bearer "+(device["device_token"] if token is None else token)})
        response = connection.getresponse()
        answer = response.status, json.loads(response.read())
        connection.close()
        return answer
    yield c, context, call
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_api_options_enrollment_and_status_are_provider_aware_without_identity_leaks(control_http):
    c, _, call = control_http
    assert call("GET", "/api/cli/gateway/options")[1]["providers"] == ["anthropic"]
    assert call("POST", "/api/cli/gateway", {"slot_ids": ["s1"], "provider": "devin"})[0] == 400
    bind(c)
    status, options = call("GET", "/api/cli/gateway/options")
    assert status == 200 and options["providers"] == ["anthropic", "devin"]
    assert next(row for row in options["slots"] if row["id"] == "s1")["providers"] == options["providers"]
    status, made = call("POST", "/api/cli/gateway", {"slot_ids": ["s1"], "provider": "devin"})
    assert status == 201 and made["key"]["provider"] == "devin"
    status, result = call("GET", "/api/gateway/status", token=made["secret"])
    assert status == 200 and result["provider"] == "devin"
    assert result["bridge_contract"] == "native-agent-v1" and result["ready"] is False
    assert "a"*64 not in json.dumps([options, made, result])
    assert "account_fp" not in json.dumps([options, made, result])
    assert "binding" not in result and "provider_account_fp" not in result["slots"][0]
    status, legacy = call("POST", "/api/cli/gateway", {"slot_ids": ["s1"]})
    assert status == 201 and legacy["key"]["provider"] == "anthropic"


@pytest.mark.parametrize("changes", [{"gateway_devin_model_map_digest": ""},
                                     {"gateway_devin_enabled": False}])
def test_api_does_not_accept_native_selection_without_configured_runtime(control_http, changes):
    c, context, call = control_http
    bind(c)
    context.cfg = replace(c.cfg, **changes)
    assert call("GET", "/api/cli/gateway/options")[1]["providers"] == ["anthropic"]
    assert call("POST", "/api/cli/gateway", {"slot_ids": ["s1"], "provider": "devin"})[0] == 400
