"""Primary processing and enrollment are distinct; credentials never belong on the broker."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import stripe_config, subscription_config
from ccfleetd.config import Config, ConfigError


def active():
    return Config(
        usd_slot_credit_enabled=True,
        stripe_subscriptions_enabled=True,
        stripe_product_approved=True,
        stripe_mode="test",
        stripe_sandbox_database=True,
        public_url="https://recurring.example.invalid",
        stripe_expected_account_id="acct_Synthetic",
        stripe_api_key_file="/synthetic/key",
        stripe_subscription_webhook_secret_file="/synthetic/sub-whsec",
        stripe_subscription_portal_configuration_id="bpc_Synthetic",
    )


def test_fresh_disabled_reads_no_credential_or_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("fresh disabled billing must be inert")

    monkeypatch.setattr(stripe_config, "_secret", forbidden)
    store = SimpleNamespace(slot_subscription_processing_required=lambda: False)
    assert subscription_config.build_billing(Config(), store, opener=forbidden) is None


def test_broker_with_existing_shared_contracts_needs_no_keys_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("broker may only use saved local entitlements")

    monkeypatch.setattr(stripe_config, "_secret", forbidden)
    store = SimpleNamespace(slot_subscription_processing_required=forbidden)
    cfg = replace(
        active(),
        broker_only=True,
        stripe_subscriptions_enabled=False,
        stripe_api_key_file="",
        stripe_subscription_webhook_secret_file="",
    )
    assert subscription_config.build_billing(cfg, store, opener=forbidden) is None


def test_enrollment_off_keeps_existing_processing_and_portal(monkeypatch):
    paths = []

    def secret(path):
        paths.append(path)
        return "rk_test_SYNTHETICONLY12345" if path.endswith("key") else "whsec_SYNTHETICONLY12345"

    monkeypatch.setattr(stripe_config, "_secret", secret)

    def no_network(*args, **kwargs):
        pytest.fail("factory must never contact Stripe")

    store = SimpleNamespace(slot_subscription_processing_required=lambda: True)
    service = subscription_config.build_billing(
        replace(active(), stripe_subscriptions_enabled=False), store, opener=no_network
    )
    assert service is not None and service.store is store
    assert paths == ["/synthetic/key", "/synthetic/sub-whsec"]
    assert service.portal_configuration_id == "bpc_Synthetic"


@pytest.mark.parametrize(
    "change",
    [
        dict(stripe_subscription_webhook_secret_file=""),
        dict(stripe_subscription_portal_configuration_id=""),
        dict(stripe_product_approved=False),
        dict(usd_slot_credit_enabled=False),
        dict(public_url="https://ccfleet.io"),
        dict(stripe_sandbox_database=False),
    ],
)
def test_existing_contracts_cannot_be_orphaned_by_incomplete_primary_configuration(
    monkeypatch, change
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid configuration must be rejected before secrets or network")

    monkeypatch.setattr(stripe_config, "_secret", forbidden)
    store = SimpleNamespace(slot_subscription_processing_required=lambda: True)
    with pytest.raises(ConfigError):
        subscription_config.build_billing(
            replace(active(), stripe_subscriptions_enabled=False, **change), store, opener=forbidden
        )


def test_financial_state_read_failure_cannot_turn_off_existing_contracts():
    def failed():
        raise RuntimeError("synthetic-private-diagnostic")

    with pytest.raises(ConfigError) as error:
        subscription_config.build_billing(
            Config(), SimpleNamespace(slot_subscription_processing_required=failed)
        )
    assert "synthetic-private" not in str(error.value)
