"""Account-wide saved connections stay distinct from each slot's readiness."""

from __future__ import annotations

import pytest

from ccfleetd import slots, usersite
from ccfleetd.bilingual import project_html
from tests import test_account_setup_flow as setup
from tests.test_account_setup_flow import NOW, Flow, rendered
from tests.test_cli_access import public_key


@pytest.fixture
def setup_context():
    yield from setup.setup_context.__wrapped__()


def pair(c, slot="claude", owner="alice", name="Existing laptop", byte=20):
    code = c.store.request_cli_pairing(slot, owner, now=NOW)
    return c.store.register_cli_device(code, public_key(byte), name, now=NOW + 1)


def computer_panel(page, identifier):
    [panel] = page.find("div", id="slot-" + identifier + "-computers")
    return panel


@pytest.mark.parametrize("language", ["en", "zh"])
def test_one_saved_computer_is_shown_for_each_owned_slot(setup_context, language):
    c = setup_context
    pair(c)
    foreign = pair(c, "foreign", "bob", "Foreign private laptop", 21)
    page = Flow(rendered(c, language))
    prefix = "/zh" if language == "zh" else ""
    for identifier in ("claude", "devin"):
        panel = computer_panel(page, identifier)
        assert "Existing laptop" in panel.text
        assert "Foreign private laptop" not in panel.text
        assert foreign["device_token"] not in panel.text
        assert ("does not require pairing" if language == "en" else "无需让那台电脑再次配对") in panel.text
        primary = [element for element in page.inside(panel) if element.has_class("primary")]
        assert len(primary) == 1 and primary[0].tag == "a"
        assert ("Use a connected computer" if language == "en" else "使用已连接的电脑") in primary[0].text
        forms = page.inside(panel, "form")
        assert all(form.attrs["action"].startswith(prefix + "/account/slots/claude/") for form in forms)
        assert "data-fleet-readonly" in panel.attrs
        assert ("Remove account pairing" if language == "en" else "移除账号配对") in panel.text
        assert ("Saved API connection keys are managed separately" if language == "en" else
                "已保存的 API 连接密钥需在下方单独管理") in panel.text
    [guide] = page.find("details", data_slot_connect="devin")
    commands = [element.text for element in page.inside(guide, "code")]
    assert "ccfleet gateway setup --slots alice-devin --provider devin" in commands
    assert not any("--slot alice-claude" in command for command in commands)


@pytest.mark.parametrize("fault", ["revoked", "released", "reassigned", "disabled", "unconfigured"])
def test_unusable_pairing_never_claims_a_shared_connection(setup_context, fault):
    c = setup_context
    made = pair(c)
    if fault == "revoked":
        c.store.revoke_cli_device("claude", made["device_id"], held_by="alice")
    elif fault == "released":
        c.store.begin_release("claude", held_by="alice")
    elif fault == "reassigned":
        # A stale registration must not inherit a new holder's account.
        c.store._conn.execute("UPDATE cli_devices SET account_id='bob' WHERE id=?",
                              (made["device_id"],))
        c.store._conn.commit()
    elif fault == "disabled":
        c.store._conn.execute("UPDATE nodes SET enabled=0 WHERE id='machine-1'")
        c.store._conn.commit()
    else:
        c.store.clear_node_access("machine-1")
    source = rendered(c)
    assert "Your Fleet account has a saved computer connection" not in source
    assert "A computer connection is already saved for your Fleet account" not in source
    assert "Existing laptop" not in source
    if fault in {"disabled", "unconfigured"}:
        page = Flow(source)
        [guide] = page.find("details", data_slot_connect="devin")
        assert not page.inside(guide, "code")
        assert not page.inside(guide, "form")


def test_expired_source_keeps_existing_account_pairing_but_cannot_pair_a_new_computer(
        setup_context, monkeypatch):
    c = setup_context
    made = pair(c)
    monkeypatch.setattr(usersite.wallet_access, "paid_through",
                        lambda store, slot: NOW - 1 if slot["id"] == "claude" else None)
    # Match the gateway API's existing-token authorization, which is distinct
    # from the paid entitlement required to pair another computer.
    assert c.store.resolve_cli_device(made["device_token"], now=NOW, touch=False) is not None
    page = Flow(rendered(c))
    panel = computer_panel(page, "devin")
    assert "Your Fleet account has a saved computer connection" in panel.text
    assert "Existing laptop" in panel.text
    [guide] = page.find("details", data_slot_connect="devin")
    assert any("ccfleet gateway setup" in element.text for element in page.inside(guide, "code"))
    assert not page.inside(guide, "form")
    assert not page.find("form", action="/account/slots/claude/cli")
    assert not page.find("details", id="slot-claude-connect-anthropic")
    [paused] = page.find("div", id="slot-claude-connect-instructions")
    assert "data-fleet-readonly" in paused.attrs and not page.inside(paused, "code")


def test_active_but_logged_out_slot_does_not_link_to_a_missing_connection_guide(setup_context):
    c = setup_context
    pair(c)
    c.store.insert_heartbeat("machine-1", NOW, {"node_id": "machine-1", "mode": "machine",
        "slots": [{"unix_user": "slot01", "credentials": {"logged_in": False}}]})
    assert c.store.get_slot("claude")["state"] == slots.ACTIVE
    page = Flow(rendered(c))
    panel = computer_panel(page, "claude")
    assert not page.find("details", id="slot-claude-connect-anthropic")
    [instructions] = page.find("div", id="slot-claude-connect-instructions")
    assert "data-fleet-readonly" in instructions.attrs
    [primary] = [element for element in page.inside(panel, "a") if element.has_class("primary")]
    assert primary.attrs["href"] == "/docs/guide#add-slot"
    assert "Finish Claude sign-in" in panel.text


def test_old_last_seen_is_not_presented_as_online_or_as_revoked(setup_context):
    c = setup_context
    made = pair(c)
    c.store._conn.execute("UPDATE cli_devices SET last_seen_at=? WHERE id=?",
                          (NOW - 100000, made["device_id"]))
    c.store._conn.commit()
    page = Flow(rendered(c))
    panel = computer_panel(page, "devin")
    assert "Existing laptop" in panel.text
    assert "do not show which computers are online" in panel.text
    assert "local session has selected" in panel.text


@pytest.mark.parametrize("language", ["en", "zh"])
def test_new_unsigned_slot_reuses_account_connection_but_still_needs_Claude(setup_context, language):
    c = setup_context
    pair(c)
    c.store._conn.execute("DELETE FROM gateway_provider_bindings WHERE slot_id='devin'")
    c.store._conn.commit()
    page = Flow(rendered(c, language))
    panel = computer_panel(page, "devin")
    assert "Existing laptop" in panel.text
    assert ("Finish Claude sign-in for this slot" if language == "en" else
            "请先完成此槽位的 Claude 登录") in panel.text
    card = page.find("div", id="slot-devin")[0]
    guides = [item for item in page.inside(card, "details") if "data-slot-connect" in item.attrs]
    assert guides == []
    [account] = [item for item in page.inside(card, "details") if item.has_class("slot-account")]
    assert "open" in account.attrs


@pytest.mark.parametrize("language", ["en", "zh"])
def test_Anthropic_is_primary_even_when_Devin_is_ready(setup_context, language):
    page = Flow(rendered(setup_context, language))
    card = page.find("div", id="slot-devin")[0]
    [account] = [item for item in page.inside(card, "details") if item.has_class("slot-account")]
    assert "open" in account.attrs
    assert "Anthropic" in account.text
    assert ("optional" if language == "en" else "可选") not in account.text
    assert not [item for item in page.inside(card, "details") if item.has_class("provider-other")]
    about = [item for item in page.inside(card, "details") if item.has_class("slot-provider")][0]
    assert page.elements.index(account) < page.elements.index(about)


@pytest.mark.parametrize("state", [slots.CLAIMING, slots.CLAIMED, slots.ACTIVE])
def test_held_lifecycle_badge_is_blue_without_recoloring_machine_health(setup_context, state):
    c = setup_context
    slot = dict(c.store.get_slot("claude"), state=state)
    body = usersite._slot_card(slot, c.store.get_node("machine-1"),
        c.store.latest_heartbeats()["machine-1"], {}, "csrf", c.cfg, NOW,
        machine_line='<p class="bad-text">Machine unreachable</p>')
    page = Flow(body)
    heading = page.find("h2")[0]
    [pill] = [element for element in page.inside(heading, "span") if element.has_class("pill")]
    assert pill.has_class("assigned")
    assert not pill.has_class("ok") and not pill.has_class("disabled")
    [health] = [element for element in page.find("p") if element.has_class("bad-text")]
    assert health.text == "Machine unreachable"


def test_returning_slot_stays_distinct_from_a_held_blue_slot(setup_context):
    c = setup_context
    c.store.begin_release("claude", held_by="alice")
    page = Flow(rendered(c))
    card = page.find("div", id="slot-claude")[0]
    [pill] = [element for element in page.inside(card, "span") if element.has_class("pill")]
    assert pill.has_class("disabled") and not pill.has_class("assigned")


@pytest.mark.parametrize("language", ["en", "zh"])
def test_removing_pairing_receipt_does_not_claim_saved_api_keys_were_revoked(setup_context, language):
    c = setup_context
    made = pair(c)
    c.store.revoke_cli_device("claude", made["device_id"], held_by="alice")
    raw = usersite.page(c.store, c.cfg, c.account, setup.SESSION, NOW, note="cli-revoked")
    page = Flow(project_html(raw, language, target="/" + language + "/account"))
    [banner] = [element for element in page.find("div") if element.has_class("note-banner")]
    assert ("Saved API connection keys remain active" if language == "en" else
            "已保存的 API 连接密钥仍然有效") in banner.text
    assert ("separately" if language == "en" else "单独撤销") in banner.text
    assert c.store.list_gateway_keys("alice")
