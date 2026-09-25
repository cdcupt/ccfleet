"""The names people see.

One machine is one slot, and claude.ai/code shows a machine by its hostname, so
a slot's name is also its machine's hostname, which Anthropic receives. Every
name made here therefore has to be a hostname, never one that another slot or
machine already answers to, and never anything from the holder's address
(Erik, 2026-09-24): see test_slot_names.py.
"""

from __future__ import annotations

import pytest

from ccfleetd import names

# -- the handle: set by the operator, only when a person asks ----------------------------

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


def test_every_name_made_from_a_valid_handle_is_a_valid_hostname():
    for handle in ("a", "erik", "zhang-san", "x" * names.MAX_HANDLE, "9"):
        assert names.valid_handle(handle)
        assert names.valid_hostname(names.next_name(handle, set()))


def test_the_product_never_makes_a_name_from_an_address():
    assert not hasattr(names, "handle_from_email")


def test_the_display_name_is_the_name_or_else_the_id():
    assert names.display({"id": "pool-1", "name": None}) == "pool-1"
    assert names.display({"id": "pool-1", "name": "alice-1"}) == "alice-1"
    assert names.display({"id": "pool-1"}) == "pool-1"
