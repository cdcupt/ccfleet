"""The price of a slot: set by the operator in the console, shown on the public pages.

Shown, never charged. The tests that matter most are about what a price may be
(a typo must be refused rather than published), what a customer reads with and
without one, and that only the operator can change it.
"""

from __future__ import annotations

import base64
import http.client
import sqlite3
import threading
import time
import urllib.parse

import pytest

from ccfleetd import cli, customer_docs, oauth, pricing
from ccfleetd.api import Context, build_server, csrf_token
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.passwords import hash_password
from ccfleetd.store import Store, StoreError
from tests.test_admin_host import Browser

ADMIN_TOKEN = "admin-token-long-enough-to-pass"
NO_PRICE_OVERVIEW = "Slots are sold directly by the operator; price and payment are agreed with them."
NO_PRICE_GUIDE = "and pay them. They switch your slot on for that account."
NO_PRICE_TERMS = ("Price and payment are agreed directly with the operator, who switches your slot "
                  "on when you pay.")


# -- what a price may be --------------------------------------------------------------

@pytest.mark.parametrize("amount,currency,want", [
    ("20", "USD", pricing.Price("20", "USD")),
    ("20.5", "USD", pricing.Price("20.50", "USD")),
    ("20.50", "EUR", pricing.Price("20.50", "EUR")),
    ("20.05", "GBP", pricing.Price("20.05", "GBP")),
    ("20.00", "USD", pricing.Price("20", "USD")),
    ("0.5", "SGD", pricing.Price("0.50", "SGD")),
    ("020", "HKD", pricing.Price("20", "HKD")),
    ("99999.99", "USD", pricing.Price("99999.99", "USD")),
    ("150", "CNY", pricing.Price("150", "CNY")),
    ("3000", "JPY", pricing.Price("3000", "JPY")),
    ("20", "usd", pricing.Price("20", "USD")),
])
def test_a_price_is_read_the_way_the_operator_meant_it(amount, currency, want):
    assert pricing.parse(amount, currency) == want


@pytest.mark.parametrize("amount", [
    "", "0", "0.00", "00", "-5", "+5", "1e3", "2E1", " 20", "20 ", "2 0", "20.", ".5",
    "20.555", "100000", "99999.999", "20,50", "２０", "٣٠", "twenty",
    "20\n", "\t20", "0x14", "NaN", "inf",
])
def test_an_amount_that_is_not_a_plain_positive_number_is_refused(amount):
    with pytest.raises(pricing.PriceError):
        pricing.parse(amount, "USD")


@pytest.mark.parametrize("amount", ["3000.5", "3000.00", "3000.0"])
def test_yen_takes_no_decimal_places(amount):
    with pytest.raises(pricing.PriceError, match="decimal"):
        pricing.parse(amount, "JPY")


@pytest.mark.parametrize("currency", ["", "XYZ", "US", "USDD", "ÜSD", "ßSD", "U$D",
                                      "CAD", "AUD", " USD", "USD ",
                                      # "ſ" (long s) upper-cases to "S": only the
                                      # ASCII check stops "uſd" becoming USD.
                                      "uſd", "ſGD"])
def test_only_the_listed_currencies_are_accepted(currency):
    with pytest.raises(pricing.PriceError):
        pricing.parse("20", currency)


def test_the_list_is_the_one_the_console_offers():
    assert pricing.CURRENCIES == ("USD", "EUR", "GBP", "CNY", "HKD", "SGD", "JPY")


@pytest.mark.parametrize("price,shown", [
    (pricing.Price("20", "USD"), "$20"),
    (pricing.Price("20.50", "USD"), "$20.50"),
    (pricing.Price("20", "EUR"), "€20"),
    (pricing.Price("20", "GBP"), "£20"),
    (pricing.Price("20", "HKD"), "HK$20"),
    (pricing.Price("20", "SGD"), "S$20"),
    (pricing.Price("150", "CNY"), "¥150 CNY"),
    (pricing.Price("3000", "JPY"), "¥3000 JPY"),
])
def test_a_price_is_shown_with_its_symbol_and_yen_says_which(price, shown):
    assert pricing.display(price) == shown


def test_the_period_is_a_month_per_slot():
    assert pricing.per_slot(pricing.Price("20", "USD")) == "$20 per slot per month"


def test_what_is_stored_is_read_back_and_checked_again():
    price = pricing.Price("20.50", "EUR")
    assert pricing.from_json(pricing.to_json(price)) == price
    for bad in ("", "not json", "[]", '{"amount":"0","currency":"USD"}',
                '{"amount":"20","currency":"XYZ"}', '{"amount":20,"currency":"USD"}',
                '{"currency":"USD"}', "null"):
        assert pricing.from_json(bad) is None, bad


# -- the store ------------------------------------------------------------------------

@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_there_is_no_price_until_the_operator_sets_one(store):
    assert store.get_price() is None


def test_setting_a_price_records_who_and_when(store):
    price = store.set_price("20", "USD", by="erik@example.com", now=1000.0)
    assert price == pricing.Price("20", "USD")
    got = store.get_price()
    assert got == {"price": pricing.Price("20", "USD"), "updated_at": 1000.0,
                   "updated_by": "erik@example.com"}
    store.set_price("25.5", "EUR", by="admin token", now=2000.0)
    got = store.get_price()
    assert got["price"] == pricing.Price("25.50", "EUR")
    assert (got["updated_at"], got["updated_by"]) == (2000.0, "admin token")


def test_the_price_lives_under_one_key_as_json(store):
    store.set_price("20", "USD", by="x", now=1.0)
    rows = store._conn.execute("SELECT key, value FROM settings").fetchall()
    assert [tuple(r) for r in rows] == [("price", '{"amount": "20", "currency": "USD"}')]


@pytest.mark.parametrize("amount,currency", [("0", "USD"), ("20", "XYZ"), ("3000.5", "JPY"),
                                             ("1e3", "USD")])
def test_a_bad_price_changes_nothing(store, amount, currency):
    store.set_price("20", "USD", by="op", now=1.0)
    with pytest.raises(StoreError):
        store.set_price(amount, currency, by="op", now=2.0)
    assert store.get_price() == {"price": pricing.Price("20", "USD"), "updated_at": 1.0,
                                 "updated_by": "op"}


def test_clearing_takes_the_price_away(store):
    store.set_price("20", "USD", by="op", now=1.0)
    assert store.clear_price() is True
    assert store.get_price() is None
    assert store.clear_price() is False, "nothing left to clear"


def test_a_hand_edited_price_that_no_longer_passes_reads_as_none(store):
    store._conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) "
                        "VALUES ('price', '{\"amount\":\"-1\",\"currency\":\"USD\"}', 1, 'x')")
    assert store.get_price() is None


def test_a_database_written_before_settings_still_opens(tmp_path):
    """A live database has no settings table; opening it adds one and changes
    nothing else."""
    path = str(tmp_path / "live.db")
    old = Store(path)
    old.add_node("erik-2", "erik", now=1.0)
    account = old.upsert_account_from_google("sub-1", "erik@example.com", now=1.0)
    old.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE settings")
    con.commit()
    con.close()

    s = Store(path)
    try:
        assert s.get_price() is None
        assert s.get_node("erik-2")["owner"] == "erik"
        assert s.get_account(account["id"])["email"] == "erik@example.com"
        s.set_price("20", "USD", by="op", now=5.0)
        assert s.get_price()["price"] == pricing.Price("20", "USD")
    finally:
        s.close()


# -- the console ----------------------------------------------------------------------

@pytest.fixture
def console():
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN)
    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    store.add_user("ana", hash_password("owner-password"), "owner", "ana", time.time())
    store.add_user("op", hash_password("operator-password"), "admin", "", time.time())

    def call(method, path, form=None, who="admin", csrf=None):
        creds = {"admin": f"admin:{ADMIN_TOKEN}", "owner": "ana:owner-password",
                 "op": "op:operator-password"}.get(who)
        headers = {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()} \
            if creds else {}
        body = None
        if form is not None:
            token = csrf_token(cfg) if csrf is None else csrf
            body = urllib.parse.urlencode({"csrf": token, **form}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers)
        reply = conn.getresponse()
        reply.body = reply.read().decode("utf-8", "replace")
        conn.close()
        return reply

    yield store, call
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    store.close()


def price_card(page):
    """The Price card's own lines, up to its closing note."""
    rest = page[page.index('id="price"'):]
    end = rest.find('<p class="note">')
    return rest if end == -1 else rest[:end]


def test_the_console_shows_there_is_no_price_yet(console):
    _, call = console
    card = price_card(call("GET", "/admin").body)
    assert "No price set" in card
    assert 'action="/actions/price/slot/set"' in card
    assert 'action="/actions/price/slot/clear"' not in card, "nothing to clear yet"
    for code in pricing.CURRENCIES:
        assert f'<option value="{code}"' in card


def test_the_operator_sets_the_price_from_the_console(console):
    store, call = console
    reply = call("POST", "/actions/price/slot/set", {"amount": "20", "currency": "USD"})
    assert reply.status == 303 and reply.getheader("Location") == "/admin#price"
    assert store.get_price()["price"] == pricing.Price("20", "USD")
    assert store.get_price()["updated_by"] == "admin token"
    card = price_card(call("GET", "/admin").body)
    assert "$20 per slot per month" in card and "set by admin token" in card
    assert 'value="20"' in card and '<option value="USD" selected>' in card
    assert 'action="/actions/price/slot/clear"' in card


def test_a_named_operator_is_who_set_it(console):
    store, call = console
    call("POST", "/actions/price/slot/set", {"amount": "150", "currency": "CNY"}, who="op")
    assert store.get_price()["updated_by"] == "op"
    assert "¥150 CNY per slot per month" in price_card(call("GET", "/admin").body)


@pytest.mark.parametrize("form,said", [
    ({"amount": "0", "currency": "USD"}, "more than zero"),
    ({"amount": "20", "currency": "XYZ"}, "USD, EUR, GBP, CNY, HKD, SGD, JPY"),
    ({"amount": "1e3", "currency": "USD"}, "a number like 20 or 20.50"),
    ({"amount": "3000.5", "currency": "JPY"}, "decimal"),
    ({"currency": "USD"}, "a number like 20 or 20.50"),
])
def test_a_bad_price_is_refused_with_a_reason_and_changes_nothing(console, form, said):
    store, call = console
    store.set_price("20", "USD", by="op", now=1.0)
    reply = call("POST", "/actions/price/slot/set", form)
    assert reply.status == 400 and said in reply.body
    assert store.get_price()["price"] == pricing.Price("20", "USD")


def test_clearing_from_the_console(console):
    store, call = console
    store.set_price("20", "USD", by="op", now=1.0)
    reply = call("POST", "/actions/price/slot/clear", {})
    assert reply.status == 303 and store.get_price() is None
    assert "No price set" in price_card(call("GET", "/admin").body)


def test_an_owner_login_cannot_set_or_clear_the_price(console):
    """Their credentials are fine; the action is not theirs. 403, not 401."""
    store, call = console
    store.set_price("20", "USD", by="op", now=1.0)
    assert call("POST", "/actions/price/slot/set", {"amount": "1", "currency": "USD"},
                who="owner").status == 403
    assert call("POST", "/actions/price/slot/clear", {}, who="owner").status == 403
    assert store.get_price()["price"] == pricing.Price("20", "USD")
    assert 'id="price"' not in call("GET", "/admin", who="owner").body


def test_nobody_signed_in_cannot_set_the_price(console):
    store, call = console
    assert call("POST", "/actions/price/slot/set", {"amount": "1", "currency": "USD"},
                who=None).status == 401
    assert store.get_price() is None


def test_the_form_token_is_required(console):
    store, call = console
    reply = call("POST", "/actions/price/slot/set", {"amount": "1", "currency": "USD"},
                 csrf="not-the-token")
    assert reply.status == 403 and store.get_price() is None


def test_an_unknown_price_action_is_not_found(console):
    store, call = console
    assert call("POST", "/actions/price/slot/raise", {"amount": "1"}).status == 404
    assert call("POST", "/actions/price/elsewhere/set",
                {"amount": "1", "currency": "USD"}).status == 404
    assert store.get_price() is None


def test_who_set_it_is_shown_as_text(console):
    store, call = console
    store.set_price("20", "USD", by="o'neil&<b>x</b>@example.com", now=time.time())
    card = price_card(call("GET", "/admin").body)
    assert "<b>x</b>" not in card and "o&#x27;neil&amp;&lt;b&gt;x&lt;/b&gt;" in card


def test_a_customer_session_is_no_console_identity(monkeypatch):
    """A signed-in customer is nobody on the console, as for every other console
    action: 401, and nothing changes."""
    monkeypatch.setattr(oauth, "exchange_code", lambda **kw: "access-token")
    monkeypatch.setattr(oauth, "fetch_identity",
                        lambda token, **kw: {"sub": "g-alice", "email": "alice@example.com"})
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN, public_url="https://fleet.example.com",
                 google_client_id="cid", google_client_secret="secret",
                 cookie_secret="0123456789abcdef0123456789abcdef", cookie_secure=False)
    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        alice = Browser(srv.server_address[1], "fleet.example.com")
        alice.sign_in_with_google()
        assert alice.call("GET", "/account").status == 200, "signed in as a customer"
        reply = alice.call("POST", "/actions/price/slot/set",
                           form={"csrf": csrf_token(cfg), "amount": "1", "currency": "USD"})
        assert reply.status == 401
        assert store.get_price() is None
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        store.close()


# -- the public pages -----------------------------------------------------------------

def render(path, price=None):
    return customer_docs.page_for(path)(Config(), price=price)


def test_without_a_price_the_pages_say_it_is_agreed_with_the_operator():
    assert NO_PRICE_OVERVIEW in render("/docs")
    assert NO_PRICE_GUIDE in render("/docs/guide")
    assert NO_PRICE_TERMS in render("/docs/terms")


@pytest.mark.parametrize("path", ["/docs", "/docs/guide", "/docs/terms"])
def test_with_a_price_each_page_says_what_a_slot_costs(path):
    page = render(path, pricing.Price("20", "USD"))
    assert "$20" in page and "a month" in page
    assert "paid directly to" in page and "no card" in page
    for fallback in (NO_PRICE_OVERVIEW, NO_PRICE_GUIDE, NO_PRICE_TERMS):
        assert fallback not in page


def test_the_overview_keeps_its_honest_points_with_a_price():
    page = render("/docs", pricing.Price("20", "USD"))
    assert "A slot costs <strong>$20</strong> a month, paid directly to the operator" in page
    assert "ccfleet sells the machine, not Claude" in page


def test_yen_on_the_pages_says_which_yen():
    assert "¥150 CNY" in render("/docs", pricing.Price("150", "CNY"))


def test_the_price_on_a_page_is_escaped():
    """Validation keeps markup out of a price, and the page escapes it anyway."""
    odd = pricing.Price("<b>20</b>", "USD")
    page = render("/docs/terms", odd)
    assert "<b>20</b>" not in page and "&lt;b&gt;20&lt;/b&gt;" in page


def test_the_how_it_works_page_takes_the_same_arguments():
    assert "Where" in render("/docs/how-it-works", pricing.Price("20", "USD"))


def test_the_pages_over_http_show_the_operators_price(console):
    store, call = console
    assert NO_PRICE_OVERVIEW in call("GET", "/docs").body
    store.set_price("20", "USD", by="erik@example.com", now=time.time())
    page = call("GET", "/docs").body
    assert "A slot costs <strong>$20</strong> a month" in page
    assert "erik@example.com" not in page, "a customer never sees who set it"
    assert "$20" in call("GET", "/docs/terms").body


# -- the command line -----------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("CCFLEET_ADMIN_TOKEN", raising=False)
    return str(tmp_path / "fleet.db")


def test_the_price_from_the_command_line(db, capsys):
    assert cli.main(["--db", db, "price", "show"]) == 0
    assert "no price set" in capsys.readouterr().out
    assert cli.main(["--db", db, "price", "set", "20", "USD"]) == 0
    assert "$20 per slot per month" in capsys.readouterr().out
    assert cli.main(["--db", db, "price", "show"]) == 0
    out = capsys.readouterr().out
    assert "$20 per slot per month" in out and "server command line" in out
    assert cli.main(["--db", db, "price", "clear"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "price", "show"]) == 0
    assert "no price set" in capsys.readouterr().out


def test_a_bad_price_on_the_command_line_is_refused(db, capsys):
    assert cli.main(["--db", db, "price", "set", "20", "USD"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "price", "set", "0", "USD"]) == 2
    assert "more than zero" in capsys.readouterr().err
    assert cli.main(["--db", db, "price", "show"]) == 0
    assert "$20 per slot per month" in capsys.readouterr().out
