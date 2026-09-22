"""The payments ledger: what people paid, as the operator wrote it down.

A record, never an enforcer. The test that matters most is the one that says
so: a lapsed payment stops no claim and takes no slot, because the design keeps
payment outside the system and lets only the operator's allowance reach it.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from ccfleetd import cli, payments, slots
from ccfleetd.store import Store, StoreError

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc).timestamp()
TODAY = date(2026, 9, 22)


# -- reading what was typed --------------------------------------------------------

@pytest.mark.parametrize("typed,minor", [
    ("30", 3000), ("30.5", 3050), ("30.50", 3050), ("0", 0), ("0.05", 5),
    ("999999999.99", 99999999999),
])
def test_an_amount_is_read_in_hundredths(typed, minor):
    assert payments.parse_amount(typed) == minor


@pytest.mark.parametrize("typed", [
    "", "-1", "1e3", "30.555", "30.", ".5", "1,000", " 30", "30 ", "0x1F",
    "\u0663\u0660",          # Arabic-Indic 30: a number to Python, not to a reader
    "\uff13\uff10",          # fullwidth 30
    "1000000000",            # ten digits
])
def test_an_amount_that_is_not_plainly_a_number_is_refused(typed):
    with pytest.raises(payments.PaymentError):
        payments.parse_amount(typed)


def test_a_currency_is_three_letters_and_comes_out_upper_case():
    assert payments.parse_currency("usd") == "USD"
    assert payments.parse_currency("CNY") == "CNY"
    for typed in ("", "US", "USDT", "U$D", "\u00dfd", "\u00df\u00df\u00df", "12D"):
        with pytest.raises(payments.PaymentError):
            payments.parse_currency(typed)


def test_a_paid_through_day_is_a_real_calendar_day():
    assert payments.parse_through("2026-10-22", TODAY) == "2026-10-22"
    assert payments.parse_through("2025-01-31", TODAY) == "2025-01-31", "backfilling history"
    for typed in ("", "2026-02-30", "2026-9-1", "20261022", "2026-10-22T00:00",
                  "22/10/2026", "2026-13-01"):
        with pytest.raises(payments.PaymentError):
            payments.parse_through(typed, TODAY)


def test_a_slipped_year_is_refused_rather_than_paying_somebody_up_for_decades():
    furthest = TODAY + timedelta(days=payments.MAX_AHEAD_DAYS)
    assert payments.parse_through(furthest.isoformat(), TODAY) == furthest.isoformat()
    with pytest.raises(payments.PaymentError, match="is the year right"):
        payments.parse_through((furthest + timedelta(days=1)).isoformat(), TODAY)


def test_a_note_has_a_length_limit():
    assert payments.check_note("x" * payments.NOTE_MAX) == "x" * payments.NOTE_MAX
    with pytest.raises(payments.PaymentError):
        payments.check_note("x" * (payments.NOTE_MAX + 1))


def test_an_amount_is_written_with_two_places():
    assert payments.format_amount(3050, "USD") == "30.50 USD"
    assert payments.format_amount(5, "CNY") == "0.05 CNY"


@pytest.fixture
def in_los_angeles(monkeypatch):
    """A server whose clock is set to Los Angeles, whatever runs the test."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_days_are_counted_in_utc(in_los_angeles):
    """02:00 UTC on the 23rd is still the evening of the 22nd in Los Angeles.
    The ledger says the 23rd, whatever zone the server keeps."""
    instant = datetime(2026, 9, 23, 2, 0, tzinfo=timezone.utc).timestamp()
    assert datetime.fromtimestamp(instant).date() == date(2026, 9, 22), "zone not in force"
    assert payments.today(instant) == date(2026, 9, 23)


# -- what a record adds up to -------------------------------------------------------

def row(through, voided=None):
    return {"paid_through": through, "voided_at": voided}


def test_paid_through_is_the_furthest_day_still_standing():
    assert payments.paid_through([row("2026-10-22"), row("2026-12-01"),
                                  row("2026-11-01")]) == "2026-12-01"
    assert payments.paid_through([row("2026-10-22"), row("2027-01-01", voided=NOW)]) \
        == "2026-10-22"
    assert payments.paid_through([row("2027-01-01", voided=NOW)]) is None
    assert payments.paid_through([]) is None


def test_the_last_day_is_still_paid_and_the_day_after_is_not():
    assert payments.standing("2026-09-22", NOW) == payments.PAID
    assert payments.standing("2026-09-21", NOW) == payments.LAPSED
    assert payments.standing(None, NOW) == payments.NONE


# -- the store -----------------------------------------------------------------------

@pytest.fixture
def store():
    st = Store(":memory:")
    st.add_account("ana", "sub-ana", "ana@example.com", slot_quota=1, now=NOW)
    st.add_account("bo", "sub-bo", "bo@example.com", slot_quota=0, now=NOW)
    yield st
    st.close()


def pay(store, who="ana", through="2026-10-22", amount="30", at=NOW, note=""):
    return store.record_payment(who, amount=amount, currency="usd", through=through,
                                note=note, recorded_by="admin token", now=at)


def test_a_payment_is_written_down_as_typed(store):
    payment_id = pay(store, note="transfer 22 Sep")
    [written] = store.list_payments("ana")
    assert written["id"] == payment_id
    assert (written["amount_minor"], written["currency"], written["paid_through"]) == \
        (3000, "USD", "2026-10-22")
    assert (written["note"], written["recorded_by"], written["voided_at"]) == \
        ("transfer 22 Sep", "admin token", None)


def test_nothing_is_written_for_somebody_who_is_not_there(store):
    with pytest.raises(StoreError, match="no such account"):
        pay(store, who="nobody")
    assert store.list_payments() == []


def test_a_bad_amount_is_a_store_refusal_like_any_other(store):
    """The console and the command line answer StoreError with a sentence."""
    with pytest.raises(StoreError, match="like 30 or 30.50"):
        pay(store, amount="thirty")
    assert store.list_payments() == []


def test_payments_come_back_newest_first_and_by_account(store):
    first = pay(store, at=NOW - 60)
    second = pay(store, who="bo", at=NOW - 30)
    third = pay(store, at=NOW)
    assert [p["id"] for p in store.list_payments()] == [third, second, first]
    assert [p["id"] for p in store.list_payments("ana")] == [third, first]


def test_voiding_keeps_the_record_and_happens_once(store):
    payment_id = pay(store)
    store.void_payment(payment_id, now=NOW + 5)
    [kept] = store.list_payments("ana")
    assert kept["voided_at"] == NOW + 5
    with pytest.raises(StoreError, match="left to void"):
        store.void_payment(payment_id, now=NOW + 9)
    assert store.list_payments("ana")[0]["voided_at"] == NOW + 5
    with pytest.raises(StoreError):
        store.void_payment(12345, now=NOW)


def test_a_lapsed_payment_stops_no_claim_and_takes_no_slot(store):
    """The design's rule: payment stays outside, and only the allowance reaches in."""
    store.add_node("m1", "op", now=NOW)
    store.set_machine_capacity("m1", 2)
    store.add_slot("m1-01", "m1", "slot01", now=NOW)
    store.add_slot("m1-02", "m1", "slot02", now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False},
                                   {"unix_user": "slot02", "present": False}], now=NOW)
    held = store.claim_slot("ana", now=NOW)
    pay(store, through="2026-01-31")                            # long lapsed
    assert payments.standing(payments.paid_through(store.list_payments("ana")), NOW) \
        == payments.LAPSED
    assert store.get_slot(held["id"])["state"] == slots.CLAIMING
    store.set_slot_quota("ana", 2)
    assert store.claim_slot("ana", now=NOW)["held_by"] == "ana"
    store.void_payment(store.list_payments("ana")[0]["id"], now=NOW)
    assert store.get_account("ana")["slot_quota"] == 2
    assert store.held_slot_count("ana") == 2


# -- the command line ------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("CCFLEET_ADMIN_TOKEN", raising=False)
    path = str(tmp_path / "fleet.db")
    st = Store(path)
    st.add_account("ana", "sub-ana", "ana@example.com", slot_quota=1, now=NOW)
    st.close()
    return path


def ahead(days):
    """A day relative to today as the ledger counts it: in UTC, not this machine's zone."""
    return (payments.today(time.time()) + timedelta(days=days)).isoformat()


def test_recording_listing_and_voiding_from_the_command_line(db, capsys):
    through = ahead(30)
    assert cli.main(["--db", db, "payment", "add", "ana@example.com", "30.5", "usd",
                     through, "--note", "wire"]) == 0
    assert f"paid through {through}" in capsys.readouterr().out
    assert cli.main(["--db", db, "payment", "list"]) == 0
    listing = capsys.readouterr().out
    assert "ana@example.com" in listing and "30.50 USD" in listing and "wire" in listing
    assert "[voided]" not in listing
    payment_id = int(listing.splitlines()[1].split()[0])
    assert cli.main(["--db", db, "payment", "void", str(payment_id)]) == 0
    assert cli.main(["--db", db, "payment", "list", "ana@example.com"]) == 0
    assert "[voided]" in capsys.readouterr().out
    st = Store(db)
    try:
        assert st.list_payments("ana")[0]["recorded_by"] == "server command line"
    finally:
        st.close()


def test_the_command_line_refuses_in_words(db, capsys):
    assert cli.main(["--db", db, "payment", "add", "nobody@example.com", "30", "USD",
                     ahead(30)]) != 0
    assert "nobody registered" in capsys.readouterr().err
    assert cli.main(["--db", db, "payment", "add", "ana@example.com", "30", "USD",
                     "2026-02-30"]) != 0
    assert "not a day on the calendar" in capsys.readouterr().err
    assert cli.main(["--db", db, "payment", "list", "nobody@example.com"]) != 0
    assert cli.main(["--db", db, "payment", "void", "999"]) != 0
    assert "left to void" in capsys.readouterr().err
    assert cli.main(["--db", db, "payment", "list"]) == 0
    assert "no payments recorded" in capsys.readouterr().out


def test_the_account_list_says_who_is_paid_up(db, capsys):
    assert cli.main(["--db", db, "account", "list"]) == 0
    [line] = [x for x in capsys.readouterr().out.splitlines() if "ana@" in x]
    assert line.split()[-1] == "-"
    assert cli.main(["--db", db, "payment", "add", "ana@example.com", "30", "USD",
                     ahead(-3)]) == 0
    assert cli.main(["--db", db, "account", "list"]) == 0
    assert f"{ahead(-3)} (lapsed)" in capsys.readouterr().out
    assert cli.main(["--db", db, "payment", "add", "ana@example.com", "30", "USD",
                     ahead(27)]) == 0
    assert cli.main(["--db", db, "account", "list"]) == 0
    out = capsys.readouterr().out
    assert ahead(27) in out and "(lapsed)" not in out
