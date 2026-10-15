"""Exact supplier costs, narrow public labels and persistent node-scoped offers."""
import json
import sqlite3
from decimal import Decimal, localcontext

import pytest

from ccfleetd import cli, offers, pricing, slots
from ccfleetd.store import SCHEMA, Store, StoreError, hash_token


def cost(amount="5.35", currency="USD", period="month"):
    return offers.Cost(amount, currency, period)


@pytest.mark.parametrize("hosting,ip,expected", [
    (cost(), cost("0"), pricing.Price("5.89", "USD")),
    (cost("59.99", period="year"), cost("2.50"), pricing.Price("8.25", "USD")),
    (cost("5"), cost("3", period="30_days"), pricing.Price("8.85", "USD")),
    (cost("0"), cost("0"), pricing.Price("0", "USD")),
    (cost("10.005"), cost("0"), pricing.Price("11.01", "USD")),
    (cost("5", "JPY"), cost("0", "JPY"), pricing.Price("6", "JPY")),
    (cost("0.004"), cost("0.004"), pricing.Price("0.01", "USD")),
    (cost(), None, None), (None, cost("0"), None),
    (cost(), cost("0", "EUR"), None),
])
def test_costs_normalize_then_add_ten_percent_and_round_once(hosting, ip, expected):
    offer = offers.Offer(hosting=hosting, ip=ip)
    assert offers.monthly_price(offer) == expected
    with localcontext() as context:
        context.prec = 4
        assert offers.monthly_price(offer) == expected


@pytest.mark.parametrize("bad", [
    None, True, 2, 1.25, "", "-1", "+1", "1e2", " 1", "1\n", "NaN", "Infinity",
    "100000", "1.1234567", "１２", Decimal("NaN"), Decimal("Infinity"), Decimal("-1"),
    Decimal("1E-10000"), Decimal("1E10000"),
])
def test_cost_rejects_ambiguous_nonfinite_or_unbounded_numbers(bad):
    with pytest.raises(offers.OfferError):
        cost(bad)


def test_cost_decimal_and_currency_are_canonical():
    assert cost(Decimal("1.2300"), "usd").amount == "1.23"
    assert cost("00001.200000", "usd") == cost("1.2")
    assert cost("0.000000").amount == "0"
    for currency in ("BTC", "usd\n", "ßD", None, True):
        with pytest.raises(offers.OfferError):
            cost(currency=currency)
    for period in ("monthly", "30 days", "annual", None, True):
        with pytest.raises(offers.OfferError):
            cost(period=period)


@pytest.mark.parametrize("field", ["host_brand", "ip_brand"])
@pytest.mark.parametrize("bad", [None, True, "a" * 49, "<script>", 'a"b', "x@y.test",
                                 "https://secret.test", "203.0.113.9", "x\ny", "a\x00b",
                                 "a\u202eb"])
def test_public_brands_are_bounded_plain_labels(field, bad):
    with pytest.raises(offers.OfferError):
        offers.Offer(**{field: bad})


def test_public_helper_has_only_approved_fields_and_no_supplier_costs():
    offer = offers.Offer(" OVHcloud ", "AT&T", "residential", cost(), cost("0"))
    public = offers.public(offer)
    assert public == {"host_brand": "OVHcloud", "ip_brand": "AT&T", "ip_type": "residential",
                      "price": pricing.Price("5.89", "USD")}
    assert set(public) == {"host_brand", "ip_brand", "ip_type", "price"}
    assert offers.public({"access_host": "secret", "token": "secret"}) == offers.public(None)
    assert offers.from_json(offers.to_json(offer)) == offer
    assert offers.Offer("台北 VPS", "vps.taipei", "isp").host_brand == "台北 VPS"
    with pytest.raises(offers.OfferError):
        offers.Offer(ip_type="datacenter")
    with pytest.raises(offers.OfferError):
        offers.Offer(hosting={"amount": "1"})


def test_node_offer_unknown_nodes_and_invalid_inputs_do_not_change_existing_offer(store):
    for operation in (store.get_node_offer, store.clear_node_offer, store.set_node_offer):
        with pytest.raises(StoreError):
            operation("missing")
    store.add_node("node-a", "alice")
    assert store.get_node_offer("node-a") is None
    original = store.set_node_offer("node-a", host_brand="Provider", ip_type="isp",
                                    hosting=cost(), ip=cost("0"))
    with pytest.raises(StoreError):
        store.set_node_offer("node-a", host_brand="<img>")
    assert store.get_node_offer("node-a") == original
    assert store.list_nodes()[0]["offer"] == original
    store.clear_node_offer("node-a")
    assert store.get_node("node-a")["offer"] is None


def test_offer_survives_slot_lifecycle_and_node_rename_but_not_removed_hardware(store):
    token = store.add_node("node-a", "alice")
    offer = store.set_node_offer("node-a", host_brand="Host", ip_type="isp")
    store.add_slot("slot-a", "node-a", "slot01", now=0)
    store.apply_slot_report("node-a", [{"unix_user": "slot01", "present": False}], now=1)
    store.add_account("a1", "sub-a", "a@example.test", slot_quota=1, now=2)
    claimed = store.claim_slot("a1", now=3)
    store.move_slot(claimed["id"], slots.CLAIMED)
    store.move_slot(claimed["id"], slots.ACTIVE)
    store.begin_release(claimed["id"])
    store.finish_release(claimed["id"], now=4)
    assert store.get_node_offer("node-a") == offer
    store.rename_node("node-a", "renamed-node")
    assert store.get_node_offer("renamed-node") == offer
    assert store.node_for_token(token)["id"] == "renamed-node"


@pytest.mark.parametrize("raw", [
    "broken", "{}", "[]", '{"version":NaN}', "x" * 4097,
    '{"version":1,"version":1}',
])
def test_corrupt_configured_offer_stays_unpriced_instead_of_using_global_price(store, raw):
    store.add_node("node-a", "alice")
    store.set_price("20", "USD", by="operator", now=1)
    store._conn.execute("UPDATE nodes SET offer_json=? WHERE id='node-a'", (raw,))
    store._conn.commit()
    assert offers.from_json(raw) is None
    assert store.get_node_offer("node-a") == offers.Offer()
    assert offers.public(store.get_node("node-a")["offer"])["price"] is None
    assert store.get_price()["price"] == pricing.Price("20", "USD")


def test_invalid_stored_cost_or_extra_secret_field_is_not_returned(store):
    good = json.loads(offers.to_json(offers.Offer("Host", "Exit", "isp", cost(), cost("0"))))
    for changes in ({"hosting": {"amount": -1}}, {"access_host": "secret"},
                    {"host_brand": "<script>"}, {"ip_type": "unknown-secret"}):
        assert offers.from_json(json.dumps({**good, **changes})) is None


def test_old_schema_reopens_additively_and_offer_is_persistent(tmp_path):
    path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA.replace("    offer_json TEXT NOT NULL DEFAULT '',\n", ""))
    connection.execute("INSERT INTO nodes(id,owner,token_hash,created_at) VALUES(?,?,?,?)",
                       ("node-a", "alice", hash_token("original"), 1))
    connection.commit()
    connection.close()
    store = Store(path)
    assert store.get_node_offer("node-a") is None
    original = store.set_node_offer("node-a", host_brand="Host", hosting=cost(), ip=cost("0"))
    store.close()
    store = Store(path)
    try:
        assert store.get_node_offer("node-a") == original
        assert store.node_for_token("original")["id"] == "node-a"
        store.remove_node("node-a")
        store.add_node("node-a", "alice")
        assert store.get_node_offer("node-a") is None
    finally:
        store.close()


def test_server_cli_offer_show_set_clear_are_explicit_and_scoped(tmp_path, capsys):
    path = str(tmp_path / "cli.db")
    store = Store(path)
    store.add_node("node-a", "alice")
    store.add_node("node-b", "bob")
    store.close()
    prefix = ["--db", path, "node", "offer"]
    assert cli.main([*prefix, "show", "node-a"]) == 0
    assert "no offer metadata" in capsys.readouterr().out
    args = [*prefix, "set", "node-a", "--host-brand", "Host", "--ip-brand", "Bundled",
            "--ip-type", "residential", "--hosting-cost", "5.35", "--hosting-currency", "USD",
            "--hosting-period", "month", "--ip-cost", "0", "--ip-currency", "USD",
            "--ip-period", "month"]
    assert cli.main(args) == 0
    assert "$5.89 per slot per month" in capsys.readouterr().out
    assert cli.main([*prefix, "set", "node-a", "--ip-cost", "1"]) == 2
    assert "required together" in capsys.readouterr().err
    assert cli.main([*prefix, "show", "node-b"]) == 0
    assert "no offer metadata" in capsys.readouterr().out
    assert cli.main([*prefix, "set", "node-a", "--ip-type", "isp"]) == 0
    assert "price pending" in capsys.readouterr().out
    assert cli.main([*prefix, "clear", "node-a"]) == 0
    assert "fallback restored" in capsys.readouterr().out
    assert cli.main([*prefix, "show", "unknown"]) == 2
