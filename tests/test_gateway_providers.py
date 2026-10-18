"""Provider readiness is explicit, scoped and stricter than a native PONG."""

from dataclasses import replace

import pytest

from ccfleetd import gateway_providers as providers
from ccfleetd.config import Config

NOW = 1000.0


def report(**changes):
    return {"account_fp": "a" * 64, "identity_verified": True,
            "bridge_contract": providers.BRIDGE_CONTRACT,
            "capabilities": dict.fromkeys(providers.REQUIRED_CAPABILITIES, True),
            "native_version": "3000.11.92", "adapter_revision": "b" * 64,
            "model_map_digest": "c" * 64, "observed_at": NOW, **changes}


def scope(**changes):
    return {"id": "s1", "kind": "machine", "state": "active", "present": True,
            "held_by": "a1", "gateway_claim_id": "d" * 32, **changes}


def binding(**changes):
    return {**report(), "provider": "devin", "owner_account_id": "a1", "slot_id": "s1",
            "claim_id": "d" * 32, "generation": 1, "revoked_at": None, **changes}


def test_flag_defaults_off_and_anthropic_remains_the_only_option(cfg):
    assert Config.from_env({}).gateway_devin_enabled is False
    assert Config.from_env({"CCFLEET_GATEWAY_DEVIN_ENABLED": "true"}).gateway_devin_enabled is True
    configured = replace(cfg, public_url="https://fleet.example.com", gateway_enabled=True,
                         gateway_ssh_key_path="/unused", gateway_ssh_public_key="unused")
    assert providers.available_providers(configured, binding(), scope(), "a1", now=NOW) == (
        "anthropic",)
    configured = replace(configured, gateway_devin_enabled=True)
    assert providers.available_providers(configured, binding(), scope(), "a1", now=NOW) == (
        "anthropic",)
    configured = replace(configured, gateway_devin_native_version="3000.11.92",
        gateway_devin_adapter_revision="b" * 64, gateway_devin_model_map_digest="c" * 64)
    native = binding(bridge_contract=providers.NATIVE_AGENT_CONTRACT,
                     capabilities=dict.fromkeys(providers.NATIVE_AGENT_CAPABILITIES, True))
    assert providers.available_providers(configured, native, scope(), "a1", now=NOW) == (
        "anthropic", "devin")
    assert providers.available_providers(configured, None, scope(), "a1", now=NOW) == ("anthropic",)
    assert providers.available_providers(configured, binding(), scope(), "a2", now=NOW) == (
        "anthropic",)
    assert providers.available_providers(replace(configured, gateway_enabled=False), binding(),
                                         scope(), "a1", now=NOW) == ("anthropic",)


@pytest.mark.parametrize("value", [None, "", "Devin", "native", "../devin", [], True])
def test_unknown_providers_never_default_to_anthropic(value):
    assert providers.normalize_provider(value) is None


@pytest.mark.parametrize("changes", [
    {"provider": "anthropic"}, {"owner_account_id": "a2"}, {"slot_id": "s2"},
    {"claim_id": "e" * 32}, {"generation": 0}, {"generation": True}, {"revoked_at": NOW},
    {"identity_verified": False}, {"account_fp": None},
    {"observed_at": NOW - providers.MAX_BINDING_AGE_S - 1}, {"observed_at": NOW + 1},
    {"bridge_contract": "text-only-pong"}, {"capabilities": {"messages": True}},
    {"adapter_revision": ""}, {"model_map_digest": ""}, {"native_version": "unknown"},
])
def test_missing_stale_revoked_or_wrong_scope_cannot_be_ready(changes):
    assert not providers.binding_ready(binding(**changes), scope(), "a1", "devin", now=NOW)


@pytest.mark.parametrize("changes", [
    {"held_by": "a2"}, {"gateway_claim_id": "e" * 32}, {"state": "releasing"},
    {"state": "claiming"}, {"kind": "owner"}, {"present": False}, {"present": None},
])
def test_provider_evidence_cannot_replace_current_physical_rental(changes):
    assert not providers.binding_ready(binding(), scope(**changes), "a1", "devin", now=NOW)


@pytest.mark.parametrize("changes", [
    {"token": "synthetic-secret"}, {"credential_path": "/private/file"},
    {"account_fp": "token-is-not-an-account"}, {"identity_verified": 1},
    {"capabilities": {"native_pong": True}}, {"capabilities": {"messages": 1}},
    {"observed_at": float("nan")}, {"observed_at": True}, {"observed_at": 10**1000},
])
def test_report_rejects_secret_fields_and_unverified_shortcuts(changes):
    with pytest.raises(ValueError):
        providers.clean_report(report(**changes))


def test_partial_native_identity_can_be_recorded_without_claiming_readiness():
    partial = report(account_fp=None, identity_verified=False, capabilities={})
    assert providers.clean_report(partial)["account_fp"] is None
    assert not providers.binding_ready(binding(**partial), scope(), "a1", "devin", now=NOW)


def test_every_capability_is_required_and_returned_evidence_is_not_aliased():
    source = report()
    clean = providers.clean_report(source)
    clean["capabilities"].clear()
    assert source["capabilities"]
    for capability in providers.REQUIRED_CAPABILITIES:
        missing = {key: True for key in providers.REQUIRED_CAPABILITIES - {capability}}
        assert not providers.binding_ready(binding(capabilities=missing), scope(), "a1", "devin",
                                            now=NOW)
