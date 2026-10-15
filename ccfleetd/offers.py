"""Operator-declared slot offers; labels and exact costs, never access details.

Costs normalize to an average calendar month: annual / 12; 30-day * 365 / 360.
Sum before applying the fixed 10% markup and rounding once, half up, to the
currency's minor unit. Missing or mixed-currency costs stay unpriced. These
display-only offers never grant a slot, collect payment or alter its lifecycle.
"""
from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Optional

from . import pricing

IP_TYPES = ("residential", "isp", "unknown")
PERIODS = ("month", "30_days", "year")
MAX_LABEL = 48
MAX_COST = Decimal("99999.999999")
AMOUNT = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,6})?")


class OfferError(ValueError):
    """Invalid public metadata or exact supplier cost."""


def _label(value: Any) -> str:
    if (not isinstance(value, str) or len(value) > MAX_LABEL
            or any(not (unicodedata.category(char)[0] in {"L", "N"}
                        or char in " &+().,_-") for char in value)):
        raise OfferError("brands must be plain labels of at most 48 characters")
    value = value.strip()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise OfferError("brands must be labels, not IP addresses")


def _amount(value: Any) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite() or not 0 <= value <= MAX_COST:
            raise OfferError("cost must be finite, non-negative and at most 99999.999999")
        # Avoid rendering an arbitrarily large exponent even for exact Decimal inputs.
        if value == 0:
            return "0"
        if value < Decimal("0.000001") or len(value.as_tuple().digits) > 32:
            raise OfferError("cost supports at most six decimal places")
        value = format(value, "f")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
    if not isinstance(value, str) or AMOUNT.fullmatch(value) is None:
        raise OfferError("cost needs an exact number with up to six decimal places")
    amount = Decimal(value)
    if not 0 <= amount <= MAX_COST:
        raise OfferError("cost is outside the supported range")
    value = format(amount, "f")
    return value.rstrip("0").rstrip(".") if "." in value else str(int(value))


@dataclass(frozen=True)
class Cost:
    amount: str
    currency: str
    period: str = "month"

    def __post_init__(self) -> None:
        code = self.currency
        if (not isinstance(code, str) or re.fullmatch(r"[A-Za-z]{3}", code) is None
                or code.upper() not in pricing.CURRENCIES):
            raise OfferError("unsupported cost currency")
        if self.period not in PERIODS:
            raise OfferError("cost period must be month, 30_days or year")
        object.__setattr__(self, "amount", _amount(self.amount))
        object.__setattr__(self, "currency", code.upper())

    def monthly(self) -> Decimal:
        value = Decimal(self.amount)
        with localcontext() as context:
            context.prec = 40
            if self.period == "year":
                return value / Decimal(12)
            if self.period == "30_days":
                return value * Decimal(365) / Decimal(360)
        return value


@dataclass(frozen=True)
class Offer:
    host_brand: str = ""
    ip_brand: str = ""
    ip_type: str = "unknown"
    hosting: Optional[Cost] = None
    ip: Optional[Cost] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "host_brand", _label(self.host_brand))
        object.__setattr__(self, "ip_brand", _label(self.ip_brand))
        if self.ip_type not in IP_TYPES:
            raise OfferError("IP type must be residential, isp or unknown")
        if any(value is not None and not isinstance(value, Cost)
               for value in (self.hosting, self.ip)):
            raise OfferError("supplier costs must be validated Cost values")


def monthly_price(offer: Offer) -> Optional[pricing.Price]:
    if offer.hosting is None or offer.ip is None or offer.hosting.currency != offer.ip.currency:
        return None
    currency = offer.hosting.currency
    unit = Decimal("1") if currency in pricing.WHOLE_ONLY else Decimal("0.01")
    with localcontext() as context:
        context.prec = 40
        amount = ((offer.hosting.monthly() + offer.ip.monthly()) * Decimal("1.10")).quantize(
            unit, rounding=ROUND_HALF_UP)
    # Computed free offers deliberately preserve measured zero; the separate
    # legacy global-price form still refuses zero exactly as it always has.
    shown = format(amount, "f")
    if shown.endswith(".00"):
        shown = shown[:-3]
    return pricing.Price(shown, currency)


def public(offer: Optional[Offer]) -> dict[str, Any]:
    """Only approved brands/type/computed price; never raw costs or node fields."""
    if not isinstance(offer, Offer):
        return {"host_brand": "", "ip_brand": "", "ip_type": "unknown", "price": None}
    return {"host_brand": offer.host_brand, "ip_brand": offer.ip_brand,
            "ip_type": offer.ip_type, "price": monthly_price(offer)}


def to_json(offer: Offer) -> str:
    def cost(value):
        return ({"amount": value.amount, "currency": value.currency, "period": value.period}
                if value is not None else None)
    return json.dumps({"version": 1, "host_brand": offer.host_brand, "ip_brand": offer.ip_brand,
                       "ip_type": offer.ip_type, "hosting": cost(offer.hosting),
                       "ip": cost(offer.ip)}, sort_keys=True, ensure_ascii=False)


def from_json(raw: Any) -> Optional[Offer]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise OfferError("duplicate offer field")
            result[key] = value
        return result

    def cost(value):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"amount", "currency", "period"}:
            raise OfferError("invalid supplier cost")
        return Cost(**value)
    try:
        if not isinstance(raw, str) or len(raw) > 4096:
            return None
        data = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(data, dict) or set(data) != {
                "version", "host_brand", "ip_brand", "ip_type", "hosting", "ip"}
                or type(data["version"]) is not int or data["version"] != 1):
            return None
        return Offer(data["host_brand"], data["ip_brand"], data["ip_type"],
                     cost(data["hosting"]), cost(data["ip"]))
    except (ValueError, TypeError, RecursionError):
        return None
