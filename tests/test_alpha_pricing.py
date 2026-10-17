"""Customer Alpha quotes use procurement records, exact money and explicit unknowns."""
from decimal import Decimal, localcontext

import pytest

from ccfleetd import offers, pricing


def offer(hosting, ip="0", *, kind="isp", currency="USD", host_period="month",
          ip_period="month"):
    return offers.Offer("Host", "Exit", kind,
                        offers.Cost(hosting, currency, host_period) if hosting is not None else None,
                        offers.Cost(ip, currency, ip_period) if ip is not None else None)


@pytest.mark.parametrize("resource,fee,total", [
    ("8.68", "2.00", "10.68"), ("10.83", "2.00", "12.83"),
    ("22.28", "2.23", "24.51"), ("27.44", "2.74", "30.18"),
    ("19.99", "2.00", "21.99"), ("20.00", "2.00", "22.00"),
    ("20.01", "2.00", "22.01"), ("20.04", "2.00", "22.04"),
    ("20.05", "2.01", "22.06"), ("100", "10.00", "110.00"),
    ("0", "2.00", "2.00"),
])
@pytest.mark.parametrize("kind", ["isp", "residential"])
def test_alpha_acceptance_threshold_and_identical_network_type_policy(resource, fee, total, kind):
    value = offers.quote(offer(resource, kind=kind))
    assert value.resource == pricing.Price(f"{Decimal(resource):.2f}", "USD")
    assert value.service_fee == pricing.Price(fee, "USD")
    assert value.total == offers.monthly_price(offer(resource, kind=kind))
    assert value.total == pricing.Price(total, "USD")
    assert Decimal(value.resource.amount) + Decimal(value.service_fee.amount) == Decimal(total)


@pytest.mark.parametrize("raw,rounded,fee,total", [
    ("10.825", "10.83", "2.00", "12.83"),
    ("22.284649", "22.28", "2.23", "24.51"),
    ("19.995", "20.00", "2.00", "22.00"),
    ("20.044999", "20.04", "2.00", "22.04"),
    ("20.045", "20.05", "2.01", "22.06"),
])
def test_resource_is_rounded_before_the_fee_and_total(raw, rounded, fee, total):
    value = offers.quote(offer(raw))
    assert (value.resource.amount, value.service_fee.amount, value.total.amount) == (
        rounded, fee, total)
    with localcontext() as context:
        context.prec = 3
        assert offers.quote(offer(raw)) == value


def test_existing_calendar_normalization_and_recorded_procurement_costs_are_preserved():
    value = offers.quote(offer("5.35", "5.40", ip_period="30_days"))
    assert value.hosting.amount == "5.35" and value.ip.amount == "5.48"
    assert value.resource.amount == "10.83" and value.total.amount == "12.83"
    annual = offers.quote(offer("120", "24", host_period="year", ip_period="year"))
    assert annual.resource.amount == "12.00" and annual.total.amount == "14.00"


@pytest.mark.parametrize("cost", [None, offer(None), offer("5.35", None),
                                 offer("149", currency="CNY"),
                                 offers.Offer(hosting=offers.Cost("5", "USD"),
                                              ip=offers.Cost("3", "EUR"))])
def test_missing_or_unconverted_costs_never_invent_a_customer_quote(cost):
    assert offers.quote(cost) is None


def test_explicit_included_ip_is_bundled_but_blank_is_unknown():
    value = offers.quote(offer("27.44", "0", kind="residential"))
    assert value.bundled and value.resource.amount == "27.44"
    assert value.total.amount == "30.18"
    assert not offers.quote(offer("5.35", "3.33")).bundled
    assert offers.quote(offer("27.44", None)) is None


@pytest.mark.parametrize("host,ip,adjustment", [("0.004", "0.004", "0.01"),
                                               ("0.005", "0.005", "-0.01")])
def test_component_rounding_difference_is_explicit_and_reconciles_the_quote(host, ip, adjustment):
    value = offers.quote(offer(host, ip))
    assert value.rounding_adjustment == Decimal(adjustment)
    assert (Decimal(value.hosting.amount) + Decimal(value.ip.amount)
            + value.rounding_adjustment == Decimal(value.resource.amount))
    assert (Decimal(value.resource.amount) + Decimal(value.service_fee.amount)
            == Decimal(value.total.amount))


def test_computing_a_quote_never_rewrites_recorded_invoice_costs():
    original = offer("5.35", "5.40", ip_period="30_days")
    before = offers.to_json(original)
    for _ in range(3):
        assert offers.quote(original).total.amount == "12.83"
    assert offers.to_json(original) == before
