"""Explicit server activation and protected-file credentials for hosted Checkout.

Validation is pure. Disabled checkout reads no files and constructs no client.
Factory construction never contacts Stripe; the adapter verifies the pinned
merchant when a later checkout operation is actually requested.
"""
from __future__ import annotations

import os
import re
import stat
import urllib.parse
from typing import Any, Callable, Optional

from . import stripe_billing, stripe_checkout
from .config import ConfigError, _public_origin

MAX_SECRET_BYTES = 512
# These origins serve the production database, including migration aliases.
PRODUCTION_HOSTS = frozenset({'ccfleet.io', 'www.ccfleet.io', 'ccfleet.daichenlab.com'})


def _need(condition: Any, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _path(value: Any) -> str:
    _need(isinstance(value, str) and 0 < len(value) <= 4096 and os.path.isabs(value)
          and '\x00' not in value, 'Stripe credentials require absolute protected file paths')
    return value


def _origin(value: Any) -> str:
    try:
        normalized = _public_origin(value)
        parsed = urllib.parse.urlsplit(normalized)
        _need(parsed.scheme == 'https' and parsed.port is None,
              'Stripe checkout requires an HTTPS public origin without a custom port')
    except (ConfigError, ValueError, TypeError):
        raise ConfigError('Stripe checkout requires an HTTPS public origin without a custom port') \
            from None
    return normalized


def validate(config: Any) -> bool:
    """Validate opt-in metadata without opening any file or making any request."""
    enabled = getattr(config, 'stripe_topup_enabled', False)
    _need(type(enabled) is bool, 'Stripe checkout activation must be an explicit boolean')
    if not enabled:
        return False
    _need(getattr(config, 'usd_slot_credit_enabled', False) is True,
          'Stripe top-ups require USD slot credit to be enabled')
    _need(getattr(config, 'stripe_product_approved', False) is True,
          'Stripe top-ups require explicit approval for the Fleet service-credit product')
    mode = getattr(config, 'stripe_mode', '')
    _need(isinstance(mode, str) and mode in {'test', 'live'},
          'Stripe checkout requires an explicit test or live mode')
    merchant = getattr(config, 'stripe_expected_account_id', '')
    _need(isinstance(merchant, str) and re.fullmatch(r'acct_[A-Za-z0-9_]{1,195}', merchant)
          is not None, 'Stripe checkout requires the pinned merchant account')
    sandbox = getattr(config, 'stripe_sandbox_database', False)
    _need(type(sandbox) is bool and sandbox is (mode == 'test'),
          'Stripe test payments require an explicitly isolated sandbox database')
    origins = getattr(config, 'public_url_aliases', ())
    _need(isinstance(origins, tuple) and len(origins) <= 8,
          'Stripe checkout aliases must be configured HTTPS origins')
    origins = (_origin(getattr(config, 'public_url', '')), *(_origin(item) for item in origins))
    if mode == 'test':
        _need(not any(urllib.parse.urlsplit(item).hostname in PRODUCTION_HOSTS
                      for item in origins),
              'Stripe test payments cannot use production Fleet origins or aliases')
    _path(getattr(config, 'stripe_api_key_file', ''))
    _path(getattr(config, 'stripe_webhook_secret_file', ''))
    return True


def _secret(path: str) -> str:
    """Read one bounded, private regular file through an unshared descriptor."""
    _path(path)
    _need(hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_CLOEXEC'),
          'Protected Stripe credential files are unsupported on this host')
    descriptor = None
    try:
        # NONBLOCK lets us reject a FIFO using fstat without waiting for a writer.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                             | getattr(os, 'O_NONBLOCK', 0))
        info = os.fstat(descriptor)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
              and info.st_uid in {0, os.geteuid()} and not stat.S_IMODE(info.st_mode) & 0o077
              and 0 < info.st_size <= MAX_SECRET_BYTES,
              'Stripe credential file protection is invalid')
        raw = bytearray()
        while len(raw) <= MAX_SECRET_BYTES:
            chunk = os.read(descriptor, MAX_SECRET_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        _need(0 < len(raw) <= MAX_SECRET_BYTES, 'Stripe credential file size is invalid')
        value = bytes(raw).decode('ascii')
        if value.endswith('\r\n'):
            value = value[:-2]
        elif value.endswith('\n'):
            value = value[:-1]
        _need(bool(value) and not any(char.isspace() or ord(char) < 32 or ord(char) == 127
                                     for char in value),
              'Stripe credential file content is invalid')
        return value
    except (OSError, UnicodeError, ValueError):
        raise ConfigError('Stripe protected credentials could not be loaded') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def build_billing(config: Any, store: Any, *, opener: Optional[Callable] = None
                  ) -> Optional[stripe_billing.Billing]:
    """Return None while off; otherwise construct an inert, explicitly scoped service."""
    if not validate(config):
        return None
    try:
        settings = stripe_checkout.Settings(mode=config.stripe_mode,
            expected_account_id=config.stripe_expected_account_id,
            api_key=_secret(config.stripe_api_key_file),
            webhook_secret=_secret(config.stripe_webhook_secret_file),
            public_origin=_origin(config.public_url))
        client = (stripe_checkout.Client(settings) if opener is None else
                  stripe_checkout.Client(settings, opener=opener))
        return stripe_billing.Billing(store, client,
                                      sandbox_database=config.stripe_sandbox_database)
    except (stripe_checkout.StripeCheckoutError, stripe_billing.BillingError):
        raise ConfigError('Stripe checkout credentials or service mode could not be confirmed') \
            from None
