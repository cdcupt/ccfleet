"""Small payment boundaries; HTTP authentication/Host/timeouts stay in the handler.

The caller authenticates the customer and verifies that session's CSRF before
``start``. Webhook helpers preserve the exact signed bytes; no browser return
URL, query string or user-supplied account identity can settle credit here.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import stripe_billing, stripe_checkout, wallet
from .bilingual import label

MAX_BODY = stripe_checkout.MAX_BODY
MESSAGES = {
    'disabled': ('Online top-ups are not available yet. Your Fleet credit has not changed.',
                 '在线充值暂未开通，您的 Fleet 余额没有变化。'),
    'request': ('Choose a listed USD amount and try again.', '请选择列表中的美元金额后重试。'),
    'signin': ('Sign in to add Fleet credit.', '请先登录，再添加 Fleet 余额。'),
    'unconfirmed': ('Checkout could not be confirmed. Return to your balance and retry '
                    'the same request.', '暂时无法确认付款页面。请返回余额页面，重试同一次充值。'),
}


def enabled(billing: Any, cfg: Any) -> bool:
    """A ready server context plus explicit existing feature flags, never browser input."""
    return (billing is not None and getattr(cfg, 'broker_only', False) is False
            and getattr(cfg, 'usd_slot_credit_enabled', False) is True
            and getattr(cfg, 'stripe_topup_enabled', False) is True
            and getattr(cfg, 'stripe_product_approved', False) is True)


def _failure(code: str, status: int):
    from . import usersite
    body = ('<section class="card"><h1>'
            + label('Add Fleet credit', '添加 Fleet 余额', inline=True) + '</h1><p>'
            + label(*MESSAGES[code], inline=True) + '</p><a class="btn" href="/account#wallet">'
            + label('Back to your balance', '返回余额页面', inline=True) + '</a></section>')
    return usersite.Outcome(status, body=usersite._shell('Add Fleet credit', body, door=True))


def _checkout_url(session: Any, mode: str) -> str:
    if not isinstance(session, stripe_checkout.HostedSession) or mode not in {'test', 'live'}:
        raise stripe_checkout.StripeCheckoutError('invalid hosted payment result')
    identity = stripe_checkout._provider_id(session.id, 'cs_' + mode + '_')
    return stripe_checkout._hosted_url(session.url, identity)


def start(billing: Any, cfg: Any, account: Any, form: Mapping[str, str], now: float):
    """After caller auth/CSRF, start an own-account preset top-up or return safe HTML."""
    from . import usersite
    if not enabled(billing, cfg):
        return _failure('disabled', 503)
    if not isinstance(account, Mapping):
        return _failure('signin', 401)
    try:
        account_id = wallet.identity(account.get('id'))
    except wallet.WalletError:
        return _failure('signin', 401)
    try:
        if (not isinstance(form, Mapping)
                or set(form) != {'csrf', 'idempotency_key', 'amount_cents'}
                or not all(isinstance(value, str) for value in form.values())
                or not form['csrf'] or len(form['csrf']) > 1024
                or re.fullmatch(r'[1-9][0-9]{0,4}', form['amount_cents']) is None):
            return _failure('request', 400)
        amount = int(form['amount_cents'])
        if amount not in stripe_checkout.DEFAULT_AMOUNTS:
            return _failure('request', 400)
        identity = wallet.request_id(form['idempotency_key'])
        moment = wallet.instant(now)
    except (ValueError, wallet.WalletError):
        return _failure('request', 400)
    try:
        session = billing.start_checkout(account_id, amount, idempotency_key=identity, now=moment)
        location = _checkout_url(session, getattr(cfg, 'stripe_mode', ''))
    except stripe_billing.BillingError as error:
        return _failure('request' if error.status_code == 400 else 'unconfirmed',
                        400 if error.status_code == 400 else 503)
    except Exception:
        # The HTTP boundary must not reflect an injected transport/provider
        # diagnostic, request body or secret in an HTML error response.
        return _failure('unconfirmed', 503)
    return usersite.Outcome(303, location=location)


class WebhookRequestError(ValueError):
    def __init__(self, status: int = 400) -> None:
        super().__init__('invalid payment notification framing')
        self.status = status


def webhook_frame(headers: Any) -> tuple[int, str]:
    """Validate raw HTTP headers before the handler reads any body bytes."""
    try:
        names = [name.lower() for name in headers]
        lengths = headers.get_all('Content-Length', [])
        signatures = headers.get_all('Stripe-Signature', [])
        if (len(names) != len(set(names)) or len(signatures) != 1
                or any(headers.get_all(name, []) for name in
                       ('Transfer-Encoding', 'Content-Encoding', 'Expect'))):
            raise WebhookRequestError()
        if not lengths:
            raise WebhookRequestError(411)
        if len(lengths) != 1:
            raise WebhookRequestError()
        length, signature = lengths[0], signatures[0]
        if (not isinstance(length, str) or re.fullmatch(r'[1-9][0-9]{0,9}', length) is None
                or not isinstance(signature, str) or not signature.isascii()
                or not 0 < len(signature) <= 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in signature)):
            raise WebhookRequestError()
        size = int(length)
        if size > MAX_BODY:
            raise WebhookRequestError(413)
        return size, signature
    except WebhookRequestError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise WebhookRequestError() from None


def read_webhook_body(stream: Any, length: int) -> bytes:
    """Exact bounded read under the caller's socket deadline; truncated bodies fail."""
    if type(length) is not int or not 0 < length <= MAX_BODY:
        raise WebhookRequestError(413 if type(length) is int and length > MAX_BODY else 400)
    result = bytearray()
    try:
        while len(result) < length:
            chunk = stream.read(length - len(result))
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > length - len(result):
                raise WebhookRequestError()
            result.extend(chunk)
    except WebhookRequestError:
        raise
    except (OSError, ValueError, AttributeError, TypeError):
        raise WebhookRequestError() from None
    return bytes(result)


def webhook(billing: Any, cfg: Any, raw_body: bytes, signature: str,
            now: float) -> tuple[int, dict[str, Any]]:
    """No reads or HTTP writes: verified billing either acknowledges or safely declines."""
    if not enabled(billing, cfg):
        return 503, {'error': 'online top-ups are unavailable'}
    if not isinstance(raw_body, bytes) or not 0 < len(raw_body) <= MAX_BODY:
        return (413 if isinstance(raw_body, bytes) and len(raw_body) > MAX_BODY else 400,
                {'error': 'invalid payment notification'})
    if (not isinstance(signature, str) or not signature.isascii()
            or not 0 < len(signature) <= 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in signature)):
        return 400, {'error': 'invalid payment notification'}
    try:
        result = billing.handle_webhook(raw_body, signature, now=wallet.instant(now))
        if result not in {'credited', 'held', 'ignored'}:
            return 503, {'error': 'payment notification could not be confirmed'}
    except stripe_billing.BillingError as error:
        return (400 if error.status_code == 400 else 503,
                {'error': ('payment notification could not be verified' if error.status_code == 400
                           else 'payment notification could not be confirmed')})
    except Exception:
        return 503, {'error': 'payment notification could not be confirmed'}
    return 200, {'received': True}
