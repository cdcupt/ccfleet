"""USD service credits never silently round money or inherit decimal context."""
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from ccfleetd import wallet


@pytest.mark.parametrize("value,cents", [
    ("1", 100), ("0.01", 1), ("20.50", 2050), ("0001.2", 120),
    (Decimal("20.50"), 2050), ("999999999.99", wallet.MAX_CENTS),
    (Decimal("999999999.99"), wallet.MAX_CENTS),
])
def test_exact_positive_USD(value, cents):
    assert wallet.usd_cents(value) == cents
    with localcontext() as context:
        context.prec = 3
        assert wallet.usd_cents(value) == cents


@pytest.mark.parametrize("value", [
    None, True, 1, 1.25, "0", "-1", "+1", "1e2", " 1", "1 ", "1\n", "١", "１",
    "1.001", "1.000", "1000000000", "NaN", "Infinity", "0.00000001",
    Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-1"),
    Decimal("1.001"), Decimal("1E-100000"), Decimal("1E100000"),
])
def test_invalid_or_inexact_money_is_refused(value):
    with pytest.raises(wallet.WalletError):
        wallet.usd_cents(value)


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "100", wallet.MAX_CENTS + 1])
def test_expected_cents_are_positive_integers(value):
    with pytest.raises(wallet.WalletError):
        wallet.positive_cents(value)


def test_ledger_labels_and_identities_are_bounded():
    assert wallet.reason("人工充值") == "人工充值"
    assert wallet.request_id("admin-grant:0001") == "admin-grant:0001"
    for value in ("", " ", " leading", "trailing ", "line\nbreak", "line\u2028break",
                  "line\u2029break", "x" * 201, "x\x00"):
        with pytest.raises(wallet.WalletError):
            wallet.reason(value)
    for value in ("", "has spaces", "x/../x", "x\n", "x" * 129, True):
        with pytest.raises(wallet.WalletError):
            wallet.request_id(value)


@pytest.mark.parametrize("start,end", [
    ("2026-01-31T12:34:56+00:00", "2026-02-28T12:34:56+00:00"),
    ("2028-01-31T00:00:00+00:00", "2028-02-29T00:00:00+00:00"),
    ("2026-12-15T01:02:03+00:00", "2027-01-15T01:02:03+00:00"),
])
def test_one_calendar_UTC_month_clamps_missing_days(start, end):
    instant = datetime.fromisoformat(start).timestamp()
    assert wallet.month_period(instant) == (instant, datetime.fromisoformat(end).timestamp())
    assert datetime.fromtimestamp(wallet.month_period(instant)[1], timezone.utc).isoformat() == end


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), -1, 10**1000])
def test_invalid_timestamps_are_refused_without_conversion_errors(value):
    with pytest.raises(wallet.WalletError):
        wallet.instant(value)


def test_USD_display_uses_signed_integer_cents():
    assert wallet.display(0) == "$0.00"
    assert wallet.display(123) == "$1.23"
    assert wallet.display(-123) == "-$1.23"
    with pytest.raises(wallet.WalletError):
        wallet.display(1.23)
