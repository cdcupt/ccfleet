"""What a slot costs, as the operator shows it.

Shown, never charged. ccfleet takes no payment and enforces none: the price is a
line on the public pages, so somebody deciding whether to buy knows what to
expect, and the operator changes it from the console. What grants a slot is
still only the allowance the operator sets, and the payments ledger is still
only a record (see payments.py). Nothing that claims or releases a slot reads
this.

Pure functions only: the store validates through them and every page formats
through them, so there is one idea of what a price is and how it reads.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

#: The currencies the console offers, in the order it offers them.
CURRENCIES = ("USD", "EUR", "GBP", "CNY", "HKD", "SGD", "JPY")
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CNY": "¥", "HKD": "HK$",
           "SGD": "S$", "JPY": "¥"}
#: Two currencies share ¥, and a customer must not be left guessing which.
SHARED_SYMBOL = frozenset({"CNY", "JPY"})
#: No minor unit: a decimal point in a yen price is a typo, not a price.
WHOLE_ONLY = frozenset({"JPY"})
#: At most 99999.99. ASCII digits only: `\d` takes "٣٠" and "２０", which are
#: numbers to Python and not to anybody reading the page.
AMOUNT_RE = re.compile(r"([0-9]{1,5})(?:\.([0-9]{1,2}))?")
CURRENCY_RE = re.compile(r"[A-Za-z]{3}")
#: One slot is one machine, rented by the month. Not a setting: nobody asked.
PERIOD = "month"
#: Where the store keeps it.
SETTING_KEY = "price"


class PriceError(ValueError):
    """A price the operator would not want published."""


@dataclass(frozen=True)
class Price:
    """A price as it is kept: the amount in its shortest exact form ("20",
    "20.50"), and the currency's code."""

    amount: str
    currency: str


def parse(amount: Any, currency: Any) -> Price:
    """A price as typed, checked. Raises PriceError with a sentence to show."""
    code = _currency(currency)
    return Price(_amount(amount, code), code)


def _currency(text: Any) -> str:
    # Matched before upper-casing: "ß".upper() is "SS", which would turn two
    # letters into a three-letter code.
    if not isinstance(text, str) or not CURRENCY_RE.fullmatch(text):
        raise PriceError(f"the currency needs to be one of {', '.join(CURRENCIES)}")
    code = text.upper()
    if code not in CURRENCIES:
        raise PriceError(f"the currency needs to be one of {', '.join(CURRENCIES)}")
    return code


def _amount(text: Any, code: str) -> str:
    # fullmatch, so a sign, an exponent, a space or a trailing newline is
    # refused rather than quietly dropped.
    match = AMOUNT_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise PriceError("the price needs to be a number like 20 or 20.50")
    whole, fraction = int(match.group(1)), match.group(2)
    if fraction is not None and code in WHOLE_ONLY:
        raise PriceError(f"{code} has no decimal places; type a whole number")
    cents = int((fraction or "").ljust(2, "0"))
    if whole == 0 and cents == 0:
        raise PriceError("the price needs to be more than zero")
    # Shortest exact form: 20.00 is 20, and 20.5 is 20.50 because money is read
    # in cents.
    return str(whole) if cents == 0 else f"{whole}.{cents:02d}"


def display(price: Price) -> str:
    """The price as people read it: "$20", "HK$20.50", "¥150 CNY"."""
    shown = f"{SYMBOLS.get(price.currency, '')}{price.amount}"
    return f"{shown} {price.currency}" if price.currency in SHARED_SYMBOL else shown


def per_slot(price: Price) -> str:
    """The price with what it buys: "$20 per slot per month"."""
    return f"{display(price)} per slot per {PERIOD}"


def to_json(price: Price) -> str:
    return json.dumps({"amount": price.amount, "currency": price.currency}, sort_keys=True)


def from_json(text: Any) -> Optional[Price]:
    """A stored price, checked again on the way out. A row edited by hand into
    something parse() would refuse reads as no price, so the pages fall back
    to their own words rather than publish it."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return parse(data.get("amount"), data.get("currency"))
    except PriceError:
        return None
