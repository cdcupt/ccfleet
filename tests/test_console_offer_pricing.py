"""Operator catalogue: current machine prices and safe retirement visibility."""
from __future__ import annotations

import re
import time

import pytest

from ccfleetd import offers

from .test_console_slots import (
    console as console,
)
from .test_console_slots import (
    held_slot,
    machine_said,
    own_machine,
    row_of,
    rows,
    shared,
    slots_card,
    text,
    under_manage,
)


def offer(store, node="m1", *, currency="USD", hosting="5.35", ip="5.40",
          hosting_period="month", ip_period="30_days", brand="Example Host"):
    store.set_node_offer(node, host_brand=brand, ip_brand="Example ISP", ip_type="isp",
                         hosting=offers.Cost(hosting, currency, hosting_period),
                         ip=offers.Cost(ip, currency, ip_period) if ip is not None else None)


def price_card(page):
    start = page.index('<h2 id="price">')
    end = page.find('<h2 ', start + 1)
    return page[start:end] if end >= 0 else page[start:]


def test_retired_empty_candidates_disappear_without_deleting_their_records(console):
    store, call = console
    shared(store, users=("slot01",))
    shared(store, node="retired-free", users=("slot01",))
    shared(store, node="retired-empty", users=())
    store.insert_heartbeat("retired-empty", time.time(), {"mode": "machine", "slots": []})
    store.set_enabled("retired-free", False)
    store.set_enabled("retired-empty", False)
    before = store.list_nodes(), store.list_slots()
    page = call("GET", "/admin").body
    assert [name for name, _ in rows(slots_card(page))] == ["m1"]
    assert (store.list_nodes(), store.list_slots()) == before


@pytest.mark.parametrize("releasing", [False, True])
def test_disabled_held_or_releasing_slot_stays_visible_for_recovery(console, releasing):
    store, call = console
    slot = held_slot(store)
    if releasing:
        store.begin_release(slot["id"])
    store.set_enabled("m1", False)
    card = slots_card(call("GET", "/admin").body)
    row = row_of(card, "m1")
    assert "machine disabled" in text(row)
    assert "ana@example.com" in row
    assert "Take back" in row
    assert store.get_slot(slot["id"])["held_by"] == slot["held_by"]


def test_disabled_held_owner_node_is_not_hidden(console):
    store, call = console
    own_machine(store)
    store.set_enabled("erik-1", False)
    row = row_of(slots_card(call("GET", "/admin").body), "erik-1")
    assert "own machine" in row and "machine disabled" in row


def test_enabled_machine_without_a_recent_heartbeat_is_not_retired(console):
    store, call = console
    shared(store, users=("slot01",))
    assert not store.recent_heartbeats("m1")
    assert row_of(slots_card(call("GET", "/admin").body), "m1")


def test_disabled_legacy_multiple_slots_remain_visible_for_diagnostics(console):
    store, call = console
    shared(store)
    store.set_enabled("m1", False)
    card = slots_card(call("GET", "/admin").body)
    assert len(rows(card)) == 2 and "more than one slot" in card


def test_machine_cost_strategy_replaces_global_price_in_front_of_console(console):
    store, call = console
    shared(store, users=("slot01",))
    offer(store)
    store.set_price("777", "USD", by="old-default", now=1)
    before = store.get_price()
    page = call("GET", "/admin").body
    card = price_card(page)
    assert page.index('id="accounts"') < page.index('id="slots"') < page.index('id="price"')
    assert "Alpha slot pricing" in card and "max($2.00, 10% of resource cost)" in card
    for said in ("$5.35 USD", "$5.40 USD", "per 30 days", "$10.83 USD", "$12.83 USD / month"):
        assert said in text(card)
    assert "× 365 / 360" in text(card) and "annual costs ÷ 12" in text(card)
    assert "/actions/price/slot/" not in page and "$777" not in card
    assert "old-default" not in card and store.get_price() == before
    assert '<table>' in card and 'scope="col"' in card and 'scope="row"' in card
    assert 'role="region"' in card and 'tabindex="0"' in card


def test_pricing_lists_enabled_offers_usd_first_and_keeps_original_currency(console):
    store, call = console
    shared(store, node="a-cny", users=("slot01",))
    shared(store, node="z-usd", users=("slot01",))
    shared(store, node="retired", users=("slot01",))
    offer(store, "a-cny", currency="CNY", hosting="149", ip="0", ip_period="month",
          brand="CNY Host")
    offer(store, "z-usd", brand="USD Host")
    offer(store, "retired", brand="Retired Host")
    store.set_enabled("retired", False)
    card = price_card(call("GET", "/admin").body)
    assert card.index("USD Host") < card.index("CNY Host")
    assert "¥149.00 CNY" in text(card) and "Pricing unavailable" in text(card)
    assert "¥163.90 CNY / month" not in text(card)
    assert "Retired Host" not in card
    assert "currencies are never converted automatically" in text(card)


def test_disabled_configured_offer_does_not_restore_legacy_editable_price(console):
    store, call = console
    shared(store, users=("slot01",))
    offer(store)
    store.set_enabled("m1", False)
    store.set_price("777", "USD", by="old-default", now=1)
    page = call("GET", "/admin").body
    card = price_card(page)
    assert "No enabled machine offers yet" in card
    assert "/actions/price/slot/" not in page and "$777" not in card


def test_annual_and_monthly_invoice_costs_keep_the_correct_calculated_rate(console):
    store, call = console
    shared(store, users=("slot01",))
    offer(store, hosting="120", hosting_period="year", ip="7.20")
    card = price_card(call("GET", "/admin").body)
    assert "$120.00 USD" in text(card) and "per year" in text(card)
    assert "$17.30 USD" in text(card) and "$19.30 USD / month" in text(card)


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_mixed_supplier_costs_never_fall_back_to_global_price(console, missing):
    store, call = console
    shared(store, users=("slot01",))
    store.set_node_offer("m1", host_brand="Example Host", ip_brand="Example ISP", ip_type="isp",
                         hosting=offers.Cost("5", "USD"),
                         ip=None if missing else offers.Cost("5", "CNY"))
    store.set_price("777", "USD", by="old-default", now=1)
    card = price_card(call("GET", "/admin").body)
    assert "Pricing unavailable" in card and "Needs recorded USD costs" in card
    assert "$777" not in card and "/actions/price/slot/" not in card


def test_offer_editor_groups_invoice_fields_without_changing_action_scope(console):
    store, call = console
    shared(store, users=("slot01",))
    offer(store)
    machine_said(store)
    row = row_of(slots_card(call("GET", "/admin").body), "m1")
    panel = under_manage(row)
    assert 'action="/actions/machine/m1/offer"' in panel
    assert 'action="/actions/machine/m1/offer-clear"' in panel
    assert '<div class="offer-fields">' in panel
    assert panel.count('<fieldset>') == panel.count('</fieldset>') == 3
    legends = {text(item) for item in re.findall(r"<legend[^>]*>(.*?)</legend>", panel, re.S)}
    assert legends == {"Providers and IP type", "Hosting cost", "IP cost"}
    for name in ("host_brand", "ip_brand", "ip_type", "hosting_cost", "hosting_currency",
                 "hosting_period", "ip_cost", "ip_currency", "ip_period"):
        assert f'name="{name}"' in panel
    assert "Saved customer price" in panel and "$12.83 USD / month" in text(panel)
    assert "Monthly service fee: $2.00" in text(panel)
    assert "Save to update the displayed preview" in text(panel)


def test_datacenter_offer_is_named_and_selected_without_a_pricing_change(console):
    store, call = console
    shared(store, users=("slot01",))
    machine_said(store)
    store.set_node_offer("m1", host_brand="Example Host", ip_brand="Example Network",
                         ip_type="datacenter", hosting=offers.Cost("5.35", "USD"),
                         ip=offers.Cost("0", "USD"))
    page = call("GET", "/admin").body
    panel = under_manage(row_of(slots_card(page), "m1"))
    assert '<option value="datacenter" selected>Datacenter</option>' in panel
    assert "Datacenter" in price_card(page) and "$7.35" in price_card(page)


def test_owner_login_cannot_see_private_cost_strategy(console):
    store, call = console
    shared(store, users=("slot01",))
    offer(store)
    page = call("GET", "/admin", who="owner").body
    assert "Monthly hosting" not in page and "Supplier / month" not in page
    assert "Example Host" not in page and 'class="offer-fields"' not in page
