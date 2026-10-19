"""Exact USD service-credit values, independent of any payment provider.

Credits buy Fleet service only. This module does not move money, grant access,
call a payment provider, convert historical payments or apply renewal policy.
"""
from __future__ import annotations

import calendar
import math
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any

MAX_CENTS = 99_999_999_999
MAX_DOLLARS = Decimal("999999999.99")
REASON_MAX = 200
IDENTITY_MAX = 128
AMOUNT = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,2})?\Z")
REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
# Manual credit changes require a real DB admin. Provider actors reserved by the
# ledger schema are admitted only through verified provider settlement methods.
ACTOR_KINDS = frozenset({"account", "console_user"})


class WalletError(ValueError):
    """A value cannot be represented safely in the credit ledger."""


def usd_cents(value: Any) -> int:
    """Parse positive USD with at most two decimal places; never round money."""
    if isinstance(value, str):
        if not AMOUNT.fullmatch(value):
            raise WalletError("USD credit needs a positive amount like 20 or 20.50")
        number = Decimal(value)
    elif isinstance(value, Decimal):
        if (not value.is_finite() or value.as_tuple().exponent < -2
                or not 0 < value <= MAX_DOLLARS):
            raise WalletError("USD credit must be finite, positive and use at most two decimals")
        number = value
    else:
        raise WalletError("USD credit must be an exact decimal amount, not a float")
    with localcontext() as context:
        context.prec = 40
        cents = int(number * 100)
    return positive_cents(cents)


def positive_cents(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CENTS:
        raise WalletError("USD cents must be a positive bounded integer")
    return value


def reason(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= REASON_MAX
            or value != value.strip()
            or any(unicodedata.category(char).startswith("C")
                   or unicodedata.category(char) in {"Zl", "Zp"} for char in value)):
        raise WalletError("credit reason must be one line of 1–200 characters")
    return value


def request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST.fullmatch(value):
        raise WalletError("request identity must use 1–128 ASCII letters, digits, "
                          "dots, colons, underscores or dashes")
    return value


def identity(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= IDENTITY_MAX
            or value != value.strip()
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise WalletError("account and actor identities must be nonempty bounded text")
    return value


def instant(value: Any) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 10**11 or not math.isfinite(value):
        raise WalletError("a finite UTC timestamp is required")
    return float(value)


def freshness(value: Any) -> float:
    if (type(value) not in (int, float) or not 0 < value <= 86400
            or not math.isfinite(value)):
        raise WalletError("the report age limit must be between zero and one day")
    return float(value)


def month_period(now: Any) -> tuple[float, float]:
    """One calendar UTC month from this instant, clamping a missing month day."""
    start = datetime.fromtimestamp(instant(now), tz=timezone.utc)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = start.replace(year=year, month=month,
                        day=min(start.day, calendar.monthrange(year, month)[1]))
    return start.timestamp(), end.timestamp()


def display(cents: Any) -> str:
    """Render a balance or signed ledger amount without floating-point conversion."""
    if type(cents) is not int or not -MAX_CENTS <= cents <= MAX_CENTS:
        raise WalletError("USD cents must be a bounded integer")
    sign, amount = ("-", -cents) if cents < 0 else ("", cents)
    return f"{sign}${amount // 100}.{amount % 100:02d}"
