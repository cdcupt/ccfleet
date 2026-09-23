"""The names people see: a slot is named after whoever holds it.

One machine is one slot, and claude.ai/code shows a machine by its hostname, so
a slot's name is also its machine's hostname. Every name made here therefore
has to be a hostname a person would recognise as theirs, and never one that
another slot or machine already answers to.
"""

from __future__ import annotations

import pytest

from ccfleetd import names


# -- the handle: who a name is about ----------------------------------------------------

@pytest.mark.parametrize("email, handle", [
    ("alice@example.com", "alice"),
    ("Alice.Smith@example.com", "alice-smith"),
    ("zhang_san1990@example.com", "zhang-san1990"),
    ("a..b__c@example.com", "a-b-c"),
    ("-lead-@example.com", "lead"),
    ("cdcupt@gmail.com", "cdcupt"),
])
def test_the_handle_is_the_address_before_the_at(email, handle):
    assert names.handle_from_email(email) == handle


def test_a_handle_keeps_only_lowercase_letters_and_digits():
    # Anything else collapses to one hyphen, including letters outside ASCII:
    # a hostname has no room for them.
    assert names.handle_from_email("Zoë.Öz@example.com") == "zo-z"


def test_a_long_handle_is_cut_to_twenty_and_never_ends_in_a_hyphen():
    handle = names.handle_from_email("abcdefghijklmnopqrs-tuvwxyz@example.com")
    assert handle == "abcdefghijklmnopqrs"
    assert len(names.handle_from_email("x" * 60 + "@example.com")) == names.MAX_HANDLE


@pytest.mark.parametrize("email", ["@example.com", "...@example.com", "漢字@example.com", ""])
def test_an_address_with_nothing_usable_becomes_user(email):
    assert names.handle_from_email(email) == names.FALLBACK_HANDLE == "user"


@pytest.mark.parametrize("handle", ["erik", "a", "a1", "zhang-san", "x" * 20])
def test_an_operator_may_pick_any_hostname_safe_handle(handle):
    assert names.valid_handle(handle)


@pytest.mark.parametrize("handle", ["", "Erik", "-erik", "erik-", "er ik", "x" * 21,
                                    "erik\n", "erik.1", "ér", None, 5])
def test_a_handle_that_could_not_start_a_hostname_is_refused(handle):
    assert not names.valid_handle(handle)


# -- the name: handle plus the first free number ------------------------------------------

def test_the_first_name_is_number_one():
    assert names.next_name("alice", set()) == "alice-1"


def test_the_number_skips_every_name_already_answered_to():
    assert names.next_name("alice", {"alice-1", "alice-2", "alice-4"}) == "alice-3"


def test_a_name_is_never_one_already_taken():
    taken = {f"pool-{k}" for k in range(1, 50)}
    name = names.next_name("pool", taken)
    assert name not in taken and name == "pool-50"


@pytest.mark.parametrize("name", ["alice-1", "pool-1", "a", "a" * 63, "erik-2"])
def test_a_name_is_a_valid_hostname_label(name):
    assert names.valid_hostname(name)


@pytest.mark.parametrize("name", ["", "-a", "a-", "A-1", "a_1", "a.b", "a" * 64, "a\n",
                                  None, 7])
def test_anything_else_is_not_a_hostname(name):
    assert not names.valid_hostname(name)


def test_every_name_made_from_any_address_is_a_valid_hostname():
    for email in ("alice@x.com", "-@x.com", "Zoë@x.com", "x" * 80 + "@x.com", "9@x.com"):
        handle = names.handle_from_email(email)
        assert names.valid_handle(handle)
        assert names.valid_hostname(names.next_name(handle, set()))


def test_the_display_name_is_the_name_or_else_the_id():
    assert names.display({"id": "pool-1", "name": None}) == "pool-1"
    assert names.display({"id": "pool-1", "name": "alice-1"}) == "alice-1"
    assert names.display({"id": "pool-1"}) == "pool-1"
