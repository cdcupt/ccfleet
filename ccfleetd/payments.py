"""What people paid, as the operator wrote it down.

A record, never an enforcer. Payment happens outside ccfleet — a transfer, an
invoice, an arrangement between people who know each other — and the design
keeps it there: the one thing that reaches the product is the operator raising
somebody's allowance. So nothing about slots, claiming or releasing reads this.
A lapsed payment is something the operator sees and decides about; it never
takes a slot from anybody by itself, and it never stops a claim.

What it is for is the operator knowing at a glance who is paid up and who is
not, without keeping that in a spreadsheet beside the console.

Pure functions only: the store validates through them, and the pages and the
command line format through them, so there is one idea of what an amount or a
paid-through day is.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

#: "30", "30.5", "30.50". ASCII digits: `\d` takes "٣٠", which is a number to
#: Python and not to anybody reading the ledger.
AMOUNT_RE = re.compile(r"([0-9]{1,9})(?:\.([0-9]{1,2}))?")
CURRENCY_RE = re.compile(r"[A-Za-z]{3}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
#: How far ahead a payment may run: far enough for a year paid up front, near
#: enough that a slipped digit — 2062 for 2026 — is refused rather than
#: marking somebody paid for decades.
MAX_AHEAD_DAYS = 3 * 366
NOTE_MAX = 200

PAID, LAPSED, NONE = "paid", "lapsed", "none"


class PaymentError(ValueError):
    """Something the ledger will not write down."""


def parse_amount(text: str) -> int:
    """An amount as typed, in hundredths: "30.5" is 3050."""
    match = AMOUNT_RE.fullmatch(text or "")
    if not match:
        raise PaymentError("the amount needs to be a number like 30 or 30.50")
    return int(match.group(1)) * 100 + int((match.group(2) or "").ljust(2, "0"))


def parse_currency(text: str) -> str:
    # Matched before upper-casing: "ß".upper() is "SS", which would turn two
    # letters into a three-letter code.
    if not CURRENCY_RE.fullmatch(text or ""):
        raise PaymentError("the currency needs to be a three-letter code like USD or CNY")
    return text.upper()


def parse_through(text: str, today: date) -> str:
    """The last day a payment covers, as YYYY-MM-DD."""
    if not DATE_RE.fullmatch(text or ""):
        raise PaymentError("paid through needs to be a date, YYYY-MM-DD")
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise PaymentError(f"{text} is not a day on the calendar") from None
    if day > today + timedelta(days=MAX_AHEAD_DAYS):
        raise PaymentError(f"{text} is more than three years away; is the year right?")
    return day.isoformat()


def check_note(text: str) -> str:
    if len(text or "") > NOTE_MAX:
        raise PaymentError(f"a note is at most {NOTE_MAX} characters")
    return text or ""


def format_amount(minor: int, currency: str) -> str:
    return f"{minor // 100}.{minor % 100:02d} {currency}"


def today(now: float) -> date:
    """Paid-through days are calendar days, counted in UTC."""
    return datetime.fromtimestamp(now, tz=timezone.utc).date()


def month_through(start: date) -> str:
    """The last day a month paid from ``start`` covers: the day before the same
    day a month later, when the next one would be due. A day the next month
    lacks (the 31st, into a shorter month) is due on that month's last day."""
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    due = date(year, month, min(start.day, calendar.monthrange(year, month)[1]))
    return (due - timedelta(days=1)).isoformat()


def next_through(current: Optional[str], today: date) -> str:
    """The paid-through day to suggest for one more month.

    It runs on from the day after what is already paid for, so paying a few days
    early loses nobody those days, or from today when that has passed or
    nothing has been paid. Typed by hand, the first payment here was put a
    month short, on the day before it was recorded.
    """
    start = today
    if current:
        start = max(start, date.fromisoformat(current) + timedelta(days=1))
    return month_through(start)


def paid_through(rows: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """The furthest day any payment still standing covers. Voided ones do not count."""
    days = [r["paid_through"] for r in rows if r["voided_at"] is None]
    return max(days) if days else None


def standing(through: Optional[str], now: float) -> str:
    """Paid, lapsed, or nothing recorded. The day itself is still covered."""
    if through is None:
        return NONE
    return PAID if through >= today(now).isoformat() else LAPSED
