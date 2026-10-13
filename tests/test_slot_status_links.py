"""A holder can identify their own machine on the neutral public status page."""
from __future__ import annotations

import pytest

from ccfleetd import slots, usersite
from ccfleetd.config import Config
from tests.test_usersite import site  # noqa: F401 (fixture)
from tests.test_usersite_v2 import one_slot_machine


def test_each_holder_links_only_their_own_machine_status(site):  # noqa: F811
    store, sign_in, _ = site
    one_slot_machine(store, "private-a")
    one_slot_machine(store, "private-b")
    a = sign_in(sub="sub-a", email="a@example.com", quota=1)
    b = sign_in(sub="sub-b", email="b@example.com", quota=1)
    store.reserve_machine("private-a", a.account["id"])
    store.reserve_machine("private-b", b.account["id"])
    assert a.press("/account/claim").status == 303
    assert b.press("/account/claim").status == 303
    numbers = {name: store.get_node(name)["public_status_number"]
               for name in ("private-a", "private-b")}
    assert numbers["private-a"] != numbers["private-b"]
    for viewer, own, other in ((a, "private-a", "private-b"), (b, "private-b", "private-a")):
        for prefix in ("", "/zh"):
            body = viewer.call("GET", prefix + "/account").body
            assert f'href="{prefix}/status#machine-{numbers[own]}"' in body
            assert f'#machine-{numbers[other]}' not in body
            if prefix:
                assert f'公共状态：主机 {numbers[own]}' in body
            else:
                assert f'Public status: Machine {numbers[own]}' in body


@pytest.mark.parametrize("number", [None, 0, -1, True, "1", '<img src=x>'])
def test_an_invalid_public_number_cannot_create_a_status_link(number):
    slot = {"id": "slot-a", "state": slots.CLAIMING, "name": "chosen-name"}
    body = usersite._slot_card(slot, {"public_status_number": number}, None, {},
                              "csrf", Config(), 1_790_000_000.0)
    assert "Public status: Machine" not in body
    assert "/status#machine-" not in body
    assert '<img' not in body


def test_an_owner_node_also_links_to_the_same_public_machine_label():
    slot = {"id": "own-slot", "kind": slots.OWNER_SLOT, "state": slots.ACTIVE}
    body = usersite._slot_card(slot, {"public_status_number": 7}, {"ts": 1_790_000_000.0}, {},
                              "csrf", Config(), 1_790_000_000.0)
    assert 'href="/status#machine-7"' in body
    assert 'Public status: Machine 7' in body


def test_a_machine_without_a_report_has_its_label_but_no_missing_status_anchor():
    slot = {"id": "slot-a", "state": slots.CLAIMING, "name": "chosen-name"}
    body = usersite._slot_card(slot, {"public_status_number": 7}, None, {},
                              "csrf", Config(), 1_790_000_000.0)
    assert 'Public status: Machine 7' in body
    assert 'Not publicly monitored yet' in body
    assert '/status#machine-7' not in body
