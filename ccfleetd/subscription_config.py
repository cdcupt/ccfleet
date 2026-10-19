"""Default-off enrollment; existing recurring contracts keep their processing configuration."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, Callable, Optional

from . import stripe_checkout, stripe_config, stripe_subscriptions, subscription_billing
from .config import ConfigError


def validate(config: Any, *, existing_contracts: bool = False) -> bool:
    enabled = getattr(config, "stripe_subscriptions_enabled", False)
    if type(enabled) is not bool or type(existing_contracts) is not bool:
        raise ConfigError("Recurring enrollment must use an explicit boolean")
    if not enabled and not existing_contracts:
        return False
    if getattr(config, "broker_only", False) is True:
        raise ConfigError("Recurring subscriptions require the customer-facing server")
    projected = SimpleNamespace(**vars(config))
    projected.stripe_topup_enabled = True
    projected.stripe_webhook_secret_file = getattr(
        config, "stripe_subscription_webhook_secret_file", ""
    )
    try:
        stripe_config.validate(projected)
        if (
            re.fullmatch(
                r"bpc_[A-Za-z0-9_]{1,195}",
                getattr(config, "stripe_subscription_portal_configuration_id", ""),
            )
            is None
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ConfigError(
            "Recurring contracts require a configured merchant, protected credentials, "
            "dedicated webhook and approved cancellation portal"
        ) from None
    return True


def build_billing(
    config: Any, store: Any, *, opener: Optional[Callable] = None
) -> Optional[subscription_billing.Billing]:
    # The broker uses local paid entitlements only; credentials belong to the primary.
    if getattr(config, "broker_only", False) is True:
        return None
    try:
        existing = store.slot_subscription_processing_required()
    except Exception:
        raise ConfigError(
            "Existing recurring contracts could not be checked; billing cannot be disabled"
        ) from None
    if not validate(config, existing_contracts=existing):
        return None
    try:
        settings = stripe_checkout.Settings(
            mode=config.stripe_mode,
            expected_account_id=config.stripe_expected_account_id,
            api_key=stripe_config._secret(config.stripe_api_key_file),
            webhook_secret=stripe_config._secret(config.stripe_subscription_webhook_secret_file),
            public_origin=stripe_config._origin(config.public_url),
        )
        client = (
            stripe_subscriptions.Client(settings)
            if opener is None
            else stripe_subscriptions.Client(settings, opener=opener)
        )
        return subscription_billing.Billing(
            store,
            client,
            sandbox_database=config.stripe_sandbox_database,
            portal_configuration_id=config.stripe_subscription_portal_configuration_id,
        )
    except Exception:
        raise ConfigError(
            "Recurring payment processing credentials are unavailable; "
            "existing contracts must remain manageable"
        ) from None
