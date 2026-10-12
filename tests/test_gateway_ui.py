"""Account endpoint forms expose only owned slots and never redisplay minted secrets."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ccfleetd import gateway_ui, usersite
from ccfleetd.config import Config
from ccfleetd.store import Store

NOW = 1700000000.0


@pytest.fixture
def context():
    store = Store(":memory:", max_slots_per_machine=8)
    store.add_node("machine", "operator", now=NOW)
    store.set_machine_capacity("machine", 3)
    account = store.add_account("alice", "alice-google", "alice@example.com", slot_quota=2, now=NOW)
    other = store.add_account("bob", "bob-google", "bob@example.com", slot_quota=1, now=NOW)
    for number in range(1, 4):
        store.add_slot("slot-" + str(number), "machine", "slot0" + str(number), now=NOW)
    store.apply_slot_report("machine", [{"unix_user": "slot0" + str(n), "present": False}
                                        for n in range(1, 4)], now=NOW)
    one = store.claim_slot(account["id"], now=NOW)
    two = store.claim_slot(account["id"], now=NOW)
    third = store.claim_slot(other["id"], now=NOW)
    cfg = Config(public_url="https://fleet.invalid", cookie_secret="x" * 32)
    yield SimpleNamespace(store=store, account=account, other=other, one=one, two=two, third=third,
                          cfg=cfg)
    store.close()


def enabled(monkeypatch):
    monkeypatch.setattr(gateway_ui, "available", lambda cfg: True)


def test_panel_lists_only_own_choices_and_root_base_url(context, monkeypatch):
    enabled(monkeypatch)
    c = context
    key = c.store.create_gateway_key("alice", "Laptop", [c.two["id"], c.one["id"]], now=NOW)
    c.store.create_gateway_key("bob", "Private Bob endpoint", [c.third["id"]], now=NOW)
    html = gateway_ui.panel(c.store, c.cfg, c.account, "csrf", c.store.list_slots(held_by="alice"))
    assert "https://fleet.invalid" in html
    assert 'name="slot.' + c.one["id"] + '"' in html
    assert 'name="slot.' + c.third["id"] + '"' not in html
    assert "Private Bob endpoint" not in html
    assert key["key_token"] not in html and "token_hash" not in html
    assert 'name="slot.' + c.two["id"] + '" value="1"' in html
    assert 'name="slot.' + c.one["id"] + '" value="2"' in html


def test_selection_keeps_priority_and_rejects_other_owner(context):
    c = context
    assert gateway_ui.selected_slots(c.store, "alice", {"slot." + c.one["id"]: "2",
                                                         "slot." + c.two["id"]: "1"}) == [c.two["id"], c.one["id"]]
    with pytest.raises(ValueError):
        gateway_ui.selected_slots(c.store, "alice", {"slot." + c.third["id"]: "1"})


@pytest.mark.parametrize("priorities", [("1", "1"), ("0", "2"), ("101", "2"), ("-1", "2"), ("1.5", "2"), ("", "")])
def test_invalid_priorities_rejected(context, priorities):
    c = context
    with pytest.raises(ValueError):
        gateway_ui.selected_slots(c.store, "alice", {"slot." + c.one["id"]: priorities[0],
                                                     "slot." + c.two["id"]: priorities[1]})


def test_create_shows_once_then_listing_hides_secret(context, monkeypatch):
    enabled(monkeypatch)
    c = context
    form = {"slot." + c.one["id"]: "1", "slot." + c.two["id"]: "2",
            "mode": "sticky", "name": "My laptop"}
    result = usersite.act(c.store, c.cfg, c.account, "/account/gateway/create", form, NOW, "session")
    assert result.status == 200 and "ccf_api_" in result.body and "shown once" in result.body
    listing = gateway_ui.panel(c.store, c.cfg, c.account, "csrf", c.store.list_slots(held_by="alice"))
    assert "ccf_api_" not in listing and "My laptop" in listing
    key = c.store.list_gateway_keys("alice")[0]
    update = {"key_id": key["id"], "mode": "priority", "name": "Single account",
              "slot." + c.two["id"]: "1"}
    outcome = usersite.act(c.store, c.cfg, c.account, "/account/gateway/update", update, NOW + 1)
    assert outcome.status == 303
    saved = c.store.list_gateway_keys("alice")[0]
    assert saved["slot_ids"] == [c.two["id"]] and saved["mode"] == "priority"
    revoked = usersite.act(c.store, c.cfg, c.account, "/account/gateway/revoke",
                           {"key_id": saved["id"]}, NOW + 2)
    assert revoked.status == 303 and not c.store.list_gateway_keys("alice")


def test_update_other_account_endpoint_leaves_it_unchanged(context, monkeypatch):
    enabled(monkeypatch)
    c = context
    key = c.store.create_gateway_key("bob", "Bob", [c.third["id"]], now=NOW)
    outcome = usersite.act(c.store, c.cfg, c.account, "/account/gateway/update",
                           {"key_id": key["id"], "slot." + c.one["id"]: "1", "mode": "priority", "name": "No"}, NOW)
    assert outcome.status == 303 and "gateway-invalid" in outcome.location
    assert c.store.list_gateway_keys("bob")[0]["slot_ids"] == [c.third["id"]]
    assert key["key_token"] not in outcome.body


def test_disabled_panel_and_actions_expose_no_keys(context):
    c = context
    assert gateway_ui.panel(c.store, c.cfg, c.account, "csrf", []) == ""
    result = usersite.act(c.store, c.cfg, c.account, "/account/gateway/create", {}, NOW)
    assert result.status == 404 and not c.store.list_gateway_keys("alice")


def test_endpoint_guidance_distinguishes_fixed_pool_and_gateway_retry_boundary(context, monkeypatch):
    enabled(monkeypatch)
    c = context
    c.store.create_gateway_key("alice", "Fixed", [c.one["id"]], now=NOW)
    c.store.create_gateway_key("alice", "Pool", [c.one["id"], c.two["id"]], now=NOW)
    html = gateway_ui.panel(c.store, c.cfg, c.account, "csrf", c.store.list_slots(held_by="alice"))
    assert "Fixed" in html and "One fixed account" in html
    assert "Pool" in html and "Keep conversations together" in html
    assert "without one, requests use priority order" in html
    assert "X-CCFleet-Session" in html and "long idle period" in html
    assert "briefly move behind other choices" in html
    assert "outcome is unknown" in html and "after response headers have been sent" in html
    assert "Applications may have their own retry behavior" in html
    assert "with no automatic switch to another account" in html
